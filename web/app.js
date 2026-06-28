// 매물 지도 SPA — listings/config/coords/naver_map 4개 JSON을 클라이언트에서 조인해
// 카카오맵에 매매·임대 마커를 찍는다. 빌드 도구 없음(정적). index.html이
// kakao.maps.load(window.initApp)으로 호출한다.
//
// 데이터 소스(읽기 전용):
//   ../data/listings.json  매물 단일 소스(가격·지역 등)
//   ../data/config.json    groups[](매매/월세전세) > cities > districts > names 분류
//   ../data/coords.json    geocode.py가 채운 {단지명:{lat,lng}}
//   ../data/naver_map.json 매매 단지의 hscpNo(네이버 단지 링크용)

(function () {
  var DATA = '../data/';
  var SALE = '매매', RENT = '월세/전세';
  var REPO = 'CosmoInDev/house_plan';             // 삭제요청 이슈 생성 대상 저장소
  var DELETED_KEY = 'house_plan.deletedListings'; // localStorage: 지도에서 숨긴 매물명(이 브라우저 한정)

  function loadDeleted() {
    try { return new Set(JSON.parse(localStorage.getItem(DELETED_KEY) || '[]')); }
    catch (e) { return new Set(); }
  }
  function saveDeleted(set) {
    try { localStorage.setItem(DELETED_KEY, JSON.stringify(Array.from(set))); } catch (e) {}
  }
  // GitHub '새 이슈' 프리필 URL(제목·본문·라벨 자동 입력). 토큰 불필요 — 로그인 사용자가 Submit만 누르면 저장된다.
  function deletionIssueUrl(name, type, listing, c) {
    var typeLabel = type === 'sale' ? '매매' : '월세/전세';
    var body = [
      '지도 SPA에서 삭제 요청된 매물입니다.', '',
      '- 단지명: ' + name,
      '- 거래유형: ' + typeLabel,
      '- 지역: ' + ((listing && listing['지역']) || ''),
      '- 좌표: ' + (c ? c.lat + ', ' + c.lng : ''),
      '- 요청 시각: ' + new Date().toLocaleString('ko-KR'), '',
      '> 확인 후 data/listings.json·config.json(및 Notion)에서 제거하면 완전 삭제됩니다.'
    ].join('\n');
    return 'https://github.com/' + REPO + '/issues/new'
      + '?title=' + encodeURIComponent('[삭제요청] ' + name)
      + '&body=' + encodeURIComponent(body)
      + '&labels=' + encodeURIComponent('deletion-request');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  // 가격 필드는 '  • 보증금 1.5억\n  • 월 130~160만' 식 — escape 후 줄바꿈을 <br>로
  function multiline(s) { return escapeHtml(s).replace(/\n/g, '<br>'); }

  // config.groups에서 매물명→거래유형('sale'|'rent') 매핑을 만든다
  function buildTypeIndex(config) {
    var idx = {};
    (config.groups || []).forEach(function (g) {
      var t = g.title === RENT ? 'rent' : 'sale';
      (g.cities || []).forEach(function (c) {
        (c.districts || []).forEach(function (d) {
          (d.names || []).forEach(function (name) { idx[name] = t; });
        });
      });
    });
    return idx;
  }

  // 네이버 부동산 링크: 매매+hscpNo 있으면 단지 페이지, 그 외는 단지명 검색 폴백
  function naverUrl(name, type, naverMap) {
    var entry = naverMap[name];
    var hscp = entry && entry.hscpNo;
    if (Array.isArray(hscp)) hscp = hscp[0];
    if (type === 'sale' && hscp) {
      return 'https://new.land.naver.com/complexes/' + encodeURIComponent(hscp);
    }
    return 'https://m.land.naver.com/search/result/' + encodeURIComponent(name);
  }

  // 마커 마우스오버 툴팁 HTML
  function tipHtml(name, type, listing) {
    var rows;
    if (type === 'sale') {
      rows = [['호가', listing['호가(억)']], ['실거래', listing['최근 실거래가(억)']]];
    } else {
      rows = [['월세', listing['월세 가격']], ['전세', listing['전세 가격']]];
    }
    var body = rows.map(function (r) {
      return '<div class="trow"><span class="tlabel">' + r[0] + '</span> ' +
             (r[1] ? multiline(r[1]) : '<span class="tlabel">—</span>') + '</div>';
    }).join('');
    return '<div class="tip"><div class="tname">' + escapeHtml(name) + '</div>' +
           body + '<div class="thint">클릭 → 네이버 부동산 · 우클릭 → 삭제요청</div>' +
           '<div class="tarrow"></div></div>';
  }

  // DOM 핀 요소를 만든다(카카오 Marker 대신 CustomOverlay로 렌더).
  // 툴팁(.tip)을 핀의 자식으로 넣어 순수 CSS(.pin:hover .tip)로 표시 → JS 토글 없음 = 깜빡임 없음.
  function makePin(color, tipHtmlStr) {
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="34" viewBox="0 0 24 34">' +
      '<path d="M12 0C5.37 0 0 5.37 0 12c0 8.25 12 22 12 22s12-13.75 12-22C24 5.37 18.63 0 12 0z" ' +
      'fill="' + color + '" stroke="#ffffff" stroke-width="1.5"/>' +
      '<circle cx="12" cy="12" r="4.5" fill="#ffffff"/></svg>';
    var el = document.createElement('div');
    el.className = 'pin';
    el.innerHTML = tipHtmlStr +
      '<img src="data:image/svg+xml;utf8,' + encodeURIComponent(svg) +
      '" width="24" height="34" alt="">';
    return el;
  }

  window.initApp = function () {
    Promise.all([
      fetch(DATA + 'listings.json').then(function (r) { return r.json(); }),
      fetch(DATA + 'config.json').then(function (r) { return r.json(); }),
      fetch(DATA + 'coords.json').then(function (r) { return r.json(); }),
      fetch(DATA + 'naver_map.json').then(function (r) { return r.json(); })
    ]).then(function (res) {
      render(res[0], res[1], res[2], res[3]);
    }).catch(function (e) {
      var n = document.getElementById('notice');
      n.style.display = 'block';
      n.innerHTML = '데이터 로드 실패: ' + escapeHtml(e.message) +
        '<br>로컬 파일은 <code>python3 -m http.server 8000</code> 로 띄운 뒤 ' +
        '<code>http://localhost:8000/web/index.html</code> 로 여세요(file://은 차단됨).';
    });
  };

  function render(listings, config, coords, naverMap) {
    var typeOf = buildTypeIndex(config);
    var deleted = loadDeleted();         // 이 브라우저에서 숨긴 매물명(localStorage)
    var current = 'sale';                // 현재 보이는 거래유형
    var map = new kakao.maps.Map(document.getElementById('map'), {
      center: new kakao.maps.LatLng(37.366, 127.108), // 정자역 부근 초기 중심
      level: 8
    });

    var sets = { sale: [], rent: [] };   // 거래유형별 핀(CustomOverlay) 목록
    var missing = [];                    // 좌표 없어 못 찍은 매물

    Object.keys(listings).forEach(function (name) {
      var type = typeOf[name];
      if (!type) return;                 // config 분류에 없는 매물은 제외
      var c = coords[name];
      if (!c || c.lat == null || c.lng == null) { missing.push(name); return; }

      var pos = new kakao.maps.LatLng(c.lat, c.lng);
      var url = naverUrl(name, type, naverMap);

      // DOM 핀 + 자식 툴팁: 호버 표시는 CSS(.pin:hover .tip), 클릭은 네이버 이동만 JS로
      var el = makePin(type === 'sale' ? '#2563eb' : '#059669',
                       tipHtml(name, type, listings[name]));
      var pin = new kakao.maps.CustomOverlay({
        position: pos, content: el, xAnchor: 0.5, yAnchor: 1.0, zIndex: 2, clickable: true
      });
      pin._name = name;
      // 호버 시 이 핀 오버레이를 최상단으로 → 툴팁이 옆 핀에 가리지 않음(z-index만 변경, 깜빡임 없음)
      el.addEventListener('mouseenter', function () { pin.setZIndex(10000); });
      el.addEventListener('mouseleave', function () { pin.setZIndex(2); });
      el.addEventListener('click', function () { window.open(url, '_blank'); });
      // 우클릭 → 지도에서 숨김(localStorage) + GitHub 삭제요청 이슈 작성 페이지 오픈
      el.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        if (!confirm("'" + name + "' 매물을 지도에서 삭제하고 GitHub 삭제요청 이슈를 만들까요?\n\n" +
                     "· 지도에서 숨겨집니다(이 브라우저, 새로고침 후에도 유지)\n" +
                     "· 새 탭에 삭제요청 이슈 작성 페이지가 열립니다 → 'Submit'을 눌러 저장하세요")) return;
        deleted.add(name); saveDeleted(deleted);
        pin.setMap(null);
        refresh(false);
        window.open(deletionIssueUrl(name, type, listings[name], c), '_blank');
      });

      sets[type].push(pin);
    });

    // fit=true면 보이는 마커에 맞춰 지도 범위를 다시 잡는다(유형 전환·복원 시). 단일 삭제 땐 fit=false.
    function refresh(fit) {
      ['sale', 'rent'].forEach(function (t) {
        sets[t].forEach(function (m) {
          m.setMap((t === current && !deleted.has(m._name)) ? map : null);
        });
      });
      var vis = sets[current].filter(function (m) { return !deleted.has(m._name); });
      if (fit && vis.length) {
        var b = new kakao.maps.LatLngBounds();
        vis.forEach(function (m) { b.extend(m.getPosition()); });
        map.setBounds(b);
      }
      document.getElementById('btn-sale').classList.toggle('active', current === 'sale');
      document.getElementById('btn-rent').classList.toggle('active', current === 'rent');
      var hiddenHere = sets[current].filter(function (m) { return deleted.has(m._name); }).length;
      var miss = missing.length ? '  ·  좌표 미확보 ' + missing.length + '건' : '';
      var hid = hiddenHere ? '  ·  숨김 ' + hiddenHere + '건' : '';
      document.getElementById('count').textContent =
        (current === 'sale' ? '매매' : '월세/전세') + ' ' + vis.length + '건' + miss + hid;
    }

    function show(type) { current = type; refresh(true); }

    document.getElementById('btn-sale').addEventListener('click', function () { show('sale'); });
    document.getElementById('btn-rent').addEventListener('click', function () { show('rent'); });

    // 숨김 초기화: localStorage를 비워 숨긴 매물을 모두 복원(GitHub 이슈는 영향 없음)
    var resetBtn = document.getElementById('btn-reset');
    if (resetBtn) {
      resetBtn.addEventListener('click', function () {
        if (!deleted.size) { alert('지도에서 숨긴 매물이 없습니다.'); return; }
        if (!confirm('지도에서 숨긴 매물 ' + deleted.size + '건을 모두 복원할까요?\n(GitHub 이슈는 영향받지 않습니다.)')) return;
        deleted.clear(); saveDeleted(deleted); refresh(true);
      });
    }

    show('sale');   // 기본 매매 뷰
  }
})();
