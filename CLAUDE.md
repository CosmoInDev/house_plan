# house_plan — 주거 계획 매물 비교 자료

다음 주거 계획을 위해 후보 매물을 분류·비교하는 자료를 관리하는 디렉토리.
정리된 데이터를 `data/listings.json`으로 관리해 **Notion 페이지에 표로 배포**한다.

## 핵심 원칙

- 데이터 단일 소스는 `data/listings.json`(매물)과 `data/config.json`(거래유형>시>구 분류·열 순서)이다. `scripts/listings.py`는 이 JSON을 읽어 로직(렌더링·검증)을 제공하는 코드일 뿐, 데이터를 담지 않는다.
- 동기화는 양방향이다: `make_notion.py`가 JSON을 Notion 표로 그리고(push), `notion_pull.py`가 Notion 표의 편집을 JSON으로 되받는다(pull).
- **매매 표의 `호가(억)`·`최근 실거래가(억)`는 반드시 같은 전용면적끼리 비교한다.** `호가`는 현재 네이버 매도 호가, `최근 실거래가`는 동일 전용면적의 가장 최근 실거래로, **국토부 실거래가 오픈API로 조회**한다. **실거래가를 조회·채울 때는 인증키를 `.molit.json`에서 읽는다.** 호가는 공공 API가 없어 `scripts/naver_pull.py`(네이버 new.land에서 쿠키 워밍업+Bearer 토큰 추출로 조회, **약관 회색지대·개인용 전제**)로 받거나 사용자가 직접 채운다. 자세한 절차는 [매물_관리.md](매물_관리.md) 참고.
- **[필수 규칙] 매물을 탐색·추천·추가·수정하거나 Notion에 배포하는 작업을 시작하기 전에는, 항상 먼저 `python3 scripts/notion_pull.py`를 실행해 Notion의 최신 편집을 JSON으로 회수한다.** pull을 건너뛰면 사용자가 Notion에서 직접 고친 내용을 다음 배포 때 덮어쓰게 된다. (단 `.notion.json`의 토큰·`block_ids`가 아직 없으면 pull이 불가하므로 생략한다.)

## 작업별 참고 문서

- **매물 추가·수정·탐색·추천** → [매물_관리.md](매물_관리.md)
  (거래유형>시>구 분류 기준, 통근·예산 등 가구 조건/제약, `listings.json` 편집·검증법)
- **Notion 표 갱신·인증** → [노션.md](노션.md)
  (배포 방법, 토큰 발급, 보안)

## 디렉토리 구조

```
house_plan/
├── CLAUDE.md              # (이 파일) 디렉토리 개요 + 문서 포인터
├── 매물_관리.md            # 매물 분류 기준·추천 조건·편집법
├── 노션.md                 # Notion 배포·인증
├── .gitignore             # .notion.json·.molit.json·.kakao.json·web/map_key.js 등 시크릿 제외
├── .notion.json           # Notion 토큰/페이지/블록 id (시크릿, 커밋 금지)
├── .molit.json            # 국토부 실거래가 오픈API 인증키 (시크릿, 커밋 금지)
├── .kakao.json            # 카카오 로컬/지도 API 인증키 (시크릿, 커밋 금지)
├── notion_export/         # 원본 Notion 추출본 (Markdown & CSV)
├── data/
│   ├── listings.json      # ★ 매물 데이터 단일 소스(single source of truth)
│   ├── config.json        # 거래유형별 열 순서(groups[].attrs)·시>구별 매물 배치(groups[].cities[].districts[].names)
│   ├── molit_map.json     # 국토부 실거래가 API 매칭표(단지→시군구코드·단지명 패턴·전용면적)
│   ├── naver_map.json     # 네이버 호가 매칭표(단지→hscpNo·전용면적)
│   └── coords.json        # 카카오 지오코딩 좌표 캐시(단지→lat·lng), 지도 SPA 마커용
├── web/                   # 매물 지도 SPA (정적, 빌드 없음)
│   ├── index.html         # 카카오맵 + 매매/임대 토글 + 마커 툴팁/네이버 링크
│   ├── app.js             # data/*.json fetch·조인·마커·툴팁·클릭 로직
│   ├── map_key.example.js # 카카오 JS 키 템플릿 (복사해 map_key.js 작성)
│   └── map_key.js         # 카카오 JavaScript 키 (시크릿, 커밋 금지)
└── scripts/
    ├── listings.py        # data/*.json 로더 + 표 렌더링·검증 로직
    ├── make_notion.py     # push: listings.py로 데이터를 읽어 Notion 페이지에 표로 배포
    ├── notion_pull.py     # pull: Notion 표의 편집을 data/*.json으로 회수(merge)
    ├── molit_pull.py      # pull: 국토부 API로 매매 '최근 실거래가(억)'을 채움(.molit.json 키 사용)
    ├── naver_pull.py      # pull: 네이버 new.land에서 매매 '호가(억)'을 채움(쿠키+토큰, 회색지대)
    └── geocode.py         # pull: 카카오 API로 매물 좌표를 data/coords.json에 채움(.kakao.json 키 사용)
```

## 지도 SPA (web/)

등록 매물을 카카오맵에 마커로 띄워 위치를 한눈에 본다. 매매/월세·전세 토글, 마커 마우스오버
시 가격 툴팁, 클릭 시 네이버 부동산 단지(또는 검색) 페이지로 이동. `data/listings.json`은
읽기만 하고 수정하지 않으며, 좌표는 보조 테이블 `data/coords.json`에 분리 저장한다.

- 좌표 채우기: `.kakao.json`에 `{"rest_key":..,"js_key":..}` 작성 후
  `SSL_CERT_FILE=/etc/ssl/cert.pem python3 scripts/geocode.py` (증분, `--force`로 전체 재조회).
- 실행: `web/map_key.example.js`를 `web/map_key.js`로 복사해 JS 키 입력 →
  루트에서 `python3 -m http.server 8000` → `http://localhost:8000/web/index.html`.
  (카카오 콘솔 Web 플랫폼에 `http://localhost:8000` 도메인 등록 필요. `file://`은 fetch 차단.)
