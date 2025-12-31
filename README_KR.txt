[목표]
- 사용자가 "연산동", "강남", "온천장역"처럼 짧게 입력해도
  가까운 앤텔레콤 개통센터(센터명/전화/주소/도착팁) + 지도 링크 + 가능하면 건물(스트리트뷰) 이미지를 반환하는 API

[대표님이 해야 할 일(코딩 없이 클릭/붙여넣기)]
1) GitHub에 새 저장소(Repository) 만들기
2) 이 폴더의 파일을 그대로 업로드
   - main.py
   - requirements.txt
   - render.yaml
   - data/centers.csv
   - data/alias_overrides.csv
   - data/station_defaults.csv
3) Render.com → New + → Blueprint → GitHub 저장소 선택 → Deploy
4) Render 서비스 환경변수(Environment) 설정 (Settings → Environment)
   - ANTEL_ACTIONS_KEY = (대표님이 Actions에 넣은 그 키)
   - GOOGLE_MAPS_API_KEY = (구글 Geocoding + Street View 활성화된 키)
   - (선택) NAVER_MAPS_KEY_ID, NAVER_MAPS_KEY = (네이버 지오코딩을 쓰고 싶을 때)
5) 배포 후 확인
   - https://<서비스도메인>/docs 열기 (API 문서)
   - https://<서비스도메인>/privacy 열기 (Actions 저장 통과용)
   - Actions의 OpenAPI URL은: https://<서비스도메인>/openapi.json

[중요]
- Actions가 실패로 표시되지 않도록, 이 API는 "사진 없음"도 404가 아니라 200으로 응답합니다.
- 지도/사진 검색어는 센터명이 아니라 "주소만" 사용해야 정확도가 높습니다. (GPT 지침에 이미 반영)

[원클릭 좌표 캐시(권장)]
- GOOGLE_MAPS_API_KEY 설정 후 한 번만 실행:
  POST https://<서비스도메인>/v1/admin/warmup
  헤더: X-API-KEY: ANTEL_ACTIONS_KEY
