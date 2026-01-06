from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from fastapi import FastAPI, Header, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from fastapi.security.api_key import APIKeyHeader

APP_NAME = "ntele-center-api"
DATA_DIR = Path(__file__).parent / "data"
CENTERS_CSV = DATA_DIR / "centers.csv"
ALIAS_CSV = DATA_DIR / "alias_overrides.csv"
STATION_CSV = DATA_DIR / "station_defaults.csv"
CACHE_PATH = DATA_DIR / "center_geocache.json"

ANTEL_ACTIONS_KEY = os.getenv("ANTEL_ACTIONS_KEY", "").strip()
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
NAVER_MAPS_KEY_ID = os.getenv("NAVER_MAPS_KEY_ID", "").strip()
NAVER_MAPS_KEY = os.getenv("NAVER_MAPS_KEY", "").strip()

api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)


# ---------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------
app = FastAPI(title="Antel Center Locator API", version="1.0.0")


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def require_api_key(x_api_key: Optional[str]):
    """
    If ANTEL_ACTIONS_KEY is set on the server, require a matching X-API-KEY header.
    If it's not set, allow requests (useful for local testing).
    """
    if ANTEL_ACTIONS_KEY:
        if not x_api_key or x_api_key.strip() != ANTEL_ACTIONS_KEY:
            # Return 200 with a structured error to avoid Actions hard-failing on 401/403.
            # (You can change this to 401 if you prefer.)
            return False
    return True


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class LatLng(BaseModel):
    lat: float
    lng: float


class ResolveOriginRequest(BaseModel):
    user_text: str = Field(..., description="예: 온천장, 온천장역, 중앙동, 자갈치")
    region_hint: Optional[str] = Field(None, description="예: 부산, 서울, 경남 김해시")
    prefer: str = Field("AUTO", description="NAVER | GOOGLE | AUTO")


class GeocodeCandidate(BaseModel):
    formatted: str
    lat: float
    lng: float
    confidence: float = 0.5


class ResolveOriginResponse(BaseModel):
    ok: bool
    status: str
    normalized_query: str
    region_hint_used: Optional[str] = None
    provider_used: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    confidence: float = 0.0
    candidates: List[GeocodeCandidate] = []  # can be empty
    message: Optional[str] = None


class NearestCentersRequest(BaseModel):
    origin: LatLng
    limit: int = 3


class CenterResult(BaseModel):
    center_id: str
    center_name: str
    display_name: Optional[str] = None
    phone: Optional[str] = None
    address: str
    arrival_hint: Optional[str] = None
    center_lat: Optional[float] = None
    center_lng: Optional[float] = None
    distance_m: Optional[int] = None
    distance_km: Optional[float] = None
    direction: Optional[str] = None
    naver_map_url: Optional[str] = None
    google_map_url: Optional[str] = None


class NearestCentersResponse(BaseModel):
    ok: bool
    status: str
    origin: LatLng
    centers: List[CenterResult] = []
    message: Optional[str] = None


class StreetviewRequest(BaseModel):
    # IMPORTANT: address-only searching (센터명 금지)
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class StreetviewResponse(BaseModel):
    ok: bool
    status: str
    image_url: Optional[str] = None
    google_maps_url: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------------
# Load CSV helpers (no pandas to keep it light)
# ---------------------------------------------------------------------
import csv


def _load_csv_dicts(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


_CENTERS = _load_csv_dicts(CENTERS_CSV)
_ALIAS = _load_csv_dicts(ALIAS_CSV)
_STATIONS = _load_csv_dicts(STATION_CSV)

_ALIAS_MAP = {row["alias"].strip(): row for row in _ALIAS if row.get("alias")}
_STATION_MAP = {row["keyword"].strip(): row for row in _STATIONS if row.get("keyword")}


def normalize_spaces(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def apply_alias_overrides(user_text: str, region_hint: Optional[str]) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Returns: (normalized_query, normalized_region_hint, note)
    """
    note = None
    q = normalize_spaces(user_text)
    rh = normalize_spaces(region_hint or "") or None

    # Exact alias rewrite
    if q in _ALIAS_MAP:
        row = _ALIAS_MAP[q]
        resolution = (row.get("resolution") or "").strip()
        canonical = (row.get("canonical") or "").strip()
        if resolution == "rewrite":
            q = canonical
            note = f"alias rewrite: {row.get('alias')} -> {canonical}"
        elif resolution == "rewrite_if_region_hint":
            # Only rewrite if region hint suggests it; else leave q and let GPT ask once.
            # (We don't ask here. This is API; GPT handles the 1-time question rule.)
            if rh and any(k in rh for k in ["경남", "창원", "마산", "진해", "경상남도"]):
                q = canonical
                note = f"alias conditional rewrite: {row.get('alias')} -> {canonical}"

    return q, rh, note


def apply_station_default(user_text: str) -> Tuple[str, Optional[str]]:
    """
    If the input is a "core place name" (e.g., 강남) without '역', map to default station (강남역).
    """
    q = normalize_spaces(user_text)
    note = None
    if q in _STATION_MAP and "역" not in q:
        q2 = normalize_spaces(_STATION_MAP[q].get("default_station", ""))
        if q2:
            note = f"station default: {q} -> {q2}"
            q = q2
    return q, note


# ---------------------------------------------------------------------
# Geocoding providers
# ---------------------------------------------------------------------
def geocode_google(query: str) -> List[GeocodeCandidate]:
    if not GOOGLE_MAPS_API_KEY:
        return []
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": query, "key": GOOGLE_MAPS_API_KEY, "language": "ko"}
    r = requests.get(url, params=params, timeout=12)
    data = r.json()
    out: List[GeocodeCandidate] = []
    if data.get("status") != "OK":
        return out
    results = data.get("results", [])[:5]
    for i, it in enumerate(results):
        loc = it["geometry"]["location"]
        formatted = it.get("formatted_address") or query
        # crude confidence: earlier results higher
        conf = 0.85 if i == 0 else 0.65
        out.append(GeocodeCandidate(formatted=formatted, lat=float(loc["lat"]), lng=float(loc["lng"]), confidence=conf))
    return out


def geocode_naver(query: str) -> List[GeocodeCandidate]:
    if not (NAVER_MAPS_KEY_ID and NAVER_MAPS_KEY):
        return []
    url = "https://naveropenapi.apigw.ntruss.com/map-geocode/v2/geocode"
    headers = {
        "X-NCP-APIGW-API-KEY-ID": NAVER_MAPS_KEY_ID,
        "X-NCP-APIGW-API-KEY": NAVER_MAPS_KEY,
    }
    params = {"query": query}
    r = requests.get(url, headers=headers, params=params, timeout=12)
    data = r.json()
    out: List[GeocodeCandidate] = []
    addrs = data.get("addresses", [])[:5]
    for i, it in enumerate(addrs):
        # Naver returns x=lng, y=lat
        lat = float(it["y"])
        lng = float(it["x"])
        formatted = it.get("roadAddress") or it.get("jibunAddress") or query
        conf = 0.85 if i == 0 else 0.65
        out.append(GeocodeCandidate(formatted=formatted, lat=lat, lng=lng, confidence=conf))
    return out


def geocode_auto(query: str, prefer: str) -> Tuple[str, List[GeocodeCandidate]]:
    prefer = (prefer or "AUTO").upper()
    if prefer == "NAVER":
        cand = geocode_naver(query)
        return ("NAVER", cand) if cand else ("NAVER", [])
    if prefer == "GOOGLE":
        cand = geocode_google(query)
        return ("GOOGLE", cand) if cand else ("GOOGLE", [])
    # AUTO: try NAVER first if keys exist, else GOOGLE
    cand = geocode_naver(query)
    if cand:
        return "NAVER", cand
    cand = geocode_google(query)
    if cand:
        return "GOOGLE", cand
    return "NONE", []


# ---------------------------------------------------------------------
# Distance & direction helpers
# ---------------------------------------------------------------------
def haversine_m(lat1, lng1, lat2, lng2) -> float:
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c


def bearing_deg(lat1, lng1, lat2, lng2) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lng2 - lng1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    brng = math.degrees(math.atan2(y, x))
    return (brng + 360.0) % 360.0


def direction_kr(bearing: float) -> str:
    # 8-way
    dirs = ["북", "북동", "동", "남동", "남", "남서", "서", "북서"]
    idx = int((bearing + 22.5) // 45) % 8
    return f"{dirs[idx]}쪽"


def naver_search_url_by_address(address: str) -> str:
    # IMPORTANT: address only
    return f"https://map.naver.com/p/search/{quote(address)}"


def google_dir_url(origin: LatLng, dest_lat: Optional[float], dest_lng: Optional[float], dest_address: Optional[str]) -> str:
    # If dest coords exist, use them; else fallback to address.
    if dest_lat is not None and dest_lng is not None:
        return f"https://www.google.com/maps/dir/?api=1&origin={origin.lat},{origin.lng}&destination={dest_lat},{dest_lng}"
    if dest_address:
        return f"https://www.google.com/maps/dir/?api=1&origin={origin.lat},{origin.lng}&destination={quote(dest_address)}"
    return "https://www.google.com/maps"


# ---------------------------------------------------------------------
# Center coordinate caching (one-time geocode per center)
# ---------------------------------------------------------------------
def load_cache() -> Dict[str, Dict[str, Any]]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_center_address(row: Dict[str, str]) -> str:
    return normalize_spaces(row.get("address") or "")


def get_center_display_name(row: Dict[str, str]) -> str:
    return normalize_spaces(row.get("display_name") or row.get("center_name") or "")


def get_center_arrival_hint(row: Dict[str, str]) -> Optional[str]:
    hint = normalize_spaces(row.get("arrival_hint") or "")
    return hint or None


def center_latlng(center_id: str, address: str, prefer: str = "AUTO") -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Returns (lat, lng, provider_used). Uses cache; if missing, geocodes address and stores.
    """
    cache = load_cache()
    if center_id in cache and "lat" in cache[center_id] and "lng" in cache[center_id]:
        c = cache[center_id]
        return float(c["lat"]), float(c["lng"]), c.get("provider")

    if not address:
        return None, None, None

    provider, cands = geocode_auto(address, prefer)
    if not cands:
        return None, None, provider

    lat, lng = cands[0].lat, cands[0].lng
    cache[center_id] = {"lat": lat, "lng": lng, "provider": provider, "updated_at": _now_iso()}
    save_cache(cache)
    return lat, lng, provider


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(f"<h3>{APP_NAME}</h3><p>OK</p><p><a href='/docs'>/docs</a></p>")


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    # Minimal policy page to satisfy Actions validation.
    html = """
    <html><head><meta charset="utf-8"><title>Privacy Policy</title></head>
    <body>
    <h2>개인정보 처리방침</h2>
    <p>이 서비스는 사용자가 입력한 출발지(동/역/주소 등)를 바탕으로 가까운 앤텔레콤 개통센터 위치를 안내합니다.</p>
    <p>서비스 제공을 위해 출발지 텍스트가 외부 지도 API(지오코딩, 길찾기, 스트리트뷰 조회)에 전송될 수 있습니다.</p>
    <p>서비스는 사용자의 정확한 집 주소 입력을 요구하지 않으며, 입력값을 별도로 저장하지 않습니다.</p>
    <p>문의: qmpzo@naver.com</p>
    </body></html>
    """
    return HTMLResponse(html)


@app.post("/v1/resolve-origin", response_model=ResolveOriginResponse)
def resolve_origin(req: ResolveOriginRequest, x_api_key: Optional[str] = Depends(api_key_header)):
    if not require_api_key(x_api_key):
        return ResolveOriginResponse(ok=False, status="UNAUTHORIZED", normalized_query=normalize_spaces(req.user_text), message="Invalid X-API-KEY")

    # 1) normalize / overrides
    q0 = normalize_spaces(req.user_text)
    q1, rh1, note1 = apply_alias_overrides(q0, req.region_hint)
    q2, note2 = apply_station_default(q1)

    normalized_query = q2
    # 2) attach region hint to help geocoder, but keep normalized_query for logs
    geocode_query = normalized_query if not rh1 else f"{rh1} {normalized_query}"

    provider, cands = geocode_auto(geocode_query, req.prefer)

    if not cands:
        return ResolveOriginResponse(
            ok=False,
            status="NO_RESULT",
            normalized_query=normalized_query,
            region_hint_used=rh1,
            provider_used=provider,
            candidates=[],
            message="지오코딩 결과가 없습니다. (도시+동) 또는 (역 이름) 형태로 한 번만 더 입력해주세요."
        )

    best = cands[0]
    msg_notes = " / ".join([n for n in [note1, note2] if n])
    return ResolveOriginResponse(
        ok=True,
        status="OK",
        normalized_query=normalized_query,
        region_hint_used=rh1,
        provider_used=provider,
        lat=best.lat,
        lng=best.lng,
        confidence=best.confidence,
        candidates=cands,
        message=msg_notes or None
    )


@app.post("/v1/recommend-centers", response_model=NearestCentersResponse)
def recommend_centers(req: NearestCentersRequest, x_api_key: Optional[str] = Depends(api_key_header)):
    if not require_api_key(x_api_key):
        return NearestCentersResponse(ok=False, status="UNAUTHORIZED", origin=req.origin, message="Invalid X-API-KEY")

    origin = req.origin
    limit = max(1, min(int(req.limit or 3), 5))

    # Ensure all centers have lat/lng (lazy caching; geocode as needed)
    results: List[CenterResult] = []
    for row in _CENTERS:
        cid = (row.get("center_id") or "").strip()
        if not cid:
            continue
        address = get_center_address(row)
        lat, lng, _provider = center_latlng(cid, address, prefer="AUTO")  # uses cache
        if lat is None or lng is None:
            # skip centers without coordinates; still could be included, but distance unknown
            continue
        dist_m = haversine_m(origin.lat, origin.lng, lat, lng)
        brng = bearing_deg(origin.lat, origin.lng, lat, lng)
        direction = direction_kr(brng)

        display_name = get_center_display_name(row)
        phone = (row.get("phone") or "").strip() or None
        arrival_hint = get_center_arrival_hint(row)

        naver_url = naver_search_url_by_address(address) if address else None
        google_url = google_dir_url(origin, lat, lng, address)

        results.append(CenterResult(
            center_id=cid,
            center_name=(row.get("center_name") or "").strip(),
            display_name=display_name or None,
            phone=phone,
            address=address,
            arrival_hint=arrival_hint,
            center_lat=lat,
            center_lng=lng,
            distance_m=int(dist_m),
            distance_km=round(dist_m/1000.0, 2),
            direction=direction,
            naver_map_url=naver_url,
            google_map_url=google_url,
        ))

    if not results:
        return NearestCentersResponse(ok=False, status="NO_CENTER_RESULT", origin=origin, centers=[], message="센터 좌표가 준비되지 않았습니다. (센터 주소 지오코딩 키 설정 필요)")

    results.sort(key=lambda x: x.distance_m if x.distance_m is not None else 10**18)
    return NearestCentersResponse(ok=True, status="OK", origin=origin, centers=results[:limit])


@app.post("/v1/image/streetview", response_model=StreetviewResponse)

def streetview_jpg(lat: float, lng: float):
    """
    Returns an image (JPEG). Public endpoint (no header required) because the chat UI fetches images without custom headers.
    The Google key is still hidden because the server calls Google.
    """
    if not GOOGLE_MAPS_API_KEY:
        return Response(content=b"", media_type="image/jpeg", status_code=200)

    # tiny cache (lat/lng rounded) to reduce repeated calls
    cache_key = f"{round(lat, 5)},{round(lng, 5)}"
    img_cache_path = DATA_DIR / "streetview_cache"
    img_cache_path.mkdir(exist_ok=True)
    cache_file = img_cache_path / (re.sub(r"[^0-9\.,-]", "_", cache_key) + ".jpg")
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return Response(content=cache_file.read_bytes(), media_type="image/jpeg", status_code=200)

    url = "https://maps.googleapis.com/maps/api/streetview"
    params = {
        "size": "640x360",
        "location": f"{lat},{lng}",
        "fov": "80",
        "pitch": "0",
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.content:
            try:
                cache_file.write_bytes(r.content)
            except Exception:
                pass
        return Response(content=r.content, media_type="image/jpeg", status_code=200)
    except Exception:
        return Response(content=b"", media_type="image/jpeg", status_code=200)
        
@app.post("/v1/image/streetview", response_model=StreetviewResponse)
def streetview(req: StreetviewRequest, request: Request, x_api_key: Optional[str] = Depends(api_key_header)):
    if not require_api_key(x_api_key):
        return StreetviewResponse(ok=False, status="UNAUTHORIZED", message="Invalid X-API-KEY")

    # IMPORTANT: address-only searching (센터명 금지) should be enforced by the caller (GPT) too.
    address = normalize_spaces(req.address or "")
    lat = req.lat
    lng = req.lng

    # If no lat/lng, try to geocode by address (fallback)
    if (lat is None or lng is None) and address:
        provider, cands = geocode_auto(address, "AUTO")
        if cands:
            lat, lng = cands[0].lat, cands[0].lng

    google_maps_url = None
    if lat is not None and lng is not None:
        google_maps_url = f"https://www.google.com/maps?q={lat},{lng}"
    elif address:
        google_maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(address)}"

    # Always return 200 JSON (never 404) to avoid Actions failing.
    if not GOOGLE_MAPS_API_KEY:
        return StreetviewResponse(
            ok=True,
            status="NO_API_KEY",
            image_url=None,
            google_maps_url=google_maps_url,
            message="GOOGLE_MAPS_API_KEY가 설정되지 않아 스트리트뷰 이미지를 제공할 수 없습니다. 지도 링크를 이용해주세요."
        )

    if lat is None or lng is None:
        return StreetviewResponse(
            ok=True,
            status="NO_LOCATION",
            image_url=None,
            google_maps_url=google_maps_url,
            message="주소로 위치를 찾지 못했습니다. 주소를 조금 더 자세히 입력해 주세요."
        )

    # We proxy the image through our own endpoint so the key doesn't leak.
    base_url = str(request.base_url).rstrip("/")
    image_url = f"{base_url}/v1/image/streetview.jpg?lat={lat}&lng={lng}"
    return StreetviewResponse(
        ok=True,
        status="OK",
        image_url=image_url,
        google_maps_url=google_maps_url,
        message=None
    )


@app.post("/v1/admin/warmup")
def warmup(x_api_key: Optional[str] = Depends(api_key_header)):
    """
    Optional: Pre-geocode all center addresses and cache lat/lng.
    Useful one-time after setting geocoding keys.
    """
    if not require_api_key(x_api_key):
        return JSONResponse({"ok": False, "status": "UNAUTHORIZED"}, status_code=200)

    ok_count = 0
    fail_count = 0
    for row in _CENTERS:
        cid = (row.get("center_id") or "").strip()
        address = get_center_address(row)
        lat, lng, provider = center_latlng(cid, address, prefer="AUTO")
        if lat is not None and lng is not None:
            ok_count += 1
        else:
            fail_count += 1

    return {"ok": True, "status": "OK", "geocoded": ok_count, "failed": fail_count, "cache_path": str(CACHE_PATH)}
