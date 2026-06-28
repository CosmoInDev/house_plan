# -*- coding: utf-8 -*-
"""네이버 부동산에서 매매 '호가(억)'를 받아 listings.json을 채운다(pull).

`최근 실거래가(억)`(국토부 API, molit_pull.py)와 달리 **호가는 공공 API가 없다**.
네이버는 약관상 자동수집을 금지하므로 이 스크립트는 비공식 경로를 쓰는 **회색지대**
도구다 — 개인용·소량 전제로만 쓴다.

작동 방식(검증 완료):
  1) new.land 홈을 한 번 방문해 쿠키(REALESTATE 등)를 얻는다(쿠키 없으면 429).
  2) 단지 페이지 HTML에 박혀 있는 Bearer 토큰(JWT, 유효 3시간)을 정규식으로 추출한다.
  3) 그 토큰으로 articles API를 호출해 단지·전용면적별 매매 호가를 받는다.
  m.land 모바일 ajax는 비브라우저에 null을 반환(조용한 차단)하므로 쓰지 않는다.

매칭표 data/naver_map.json
  {단지명: {"hscpNo": "<단지번호>" 또는 [여러 개], "areas": {평형: 전용면적㎡}, "skip": 사유}}
  hscpNo는 --resolve로 자동 채울 수 있다(new.land 검색).

실행
  python3 naver_pull.py                 # 호가 조회→평형 매칭→listings.json '호가(억)' 기록
  python3 naver_pull.py --dry-run       # 적용 없이 제안만
  python3 naver_pull.py --only "관악드림타운(삼성·동아)"
  python3 naver_pull.py --resolve       # naver_map.json의 빈 hscpNo를 검색해 채움
  python3 naver_pull.py --sleep 2.5     # 요청 간 대기(초, 기본 1.5) — 429 나면 늘린다

작업 순서는 molit과 동일: notion_pull.py(선행) → naver_pull.py → make_notion.py(배포).
"""
import argparse
import collections
import http.cookiejar
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

# urllib이 IPv6를 먼저 시도하다 지연되는 환경 회피(다른 스크립트와 동일).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, *a, **k):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *a, **k)


socket.getaddrinfo = _ipv4_only
socket.setdefaulttimeout(30)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE = "https://new.land.naver.com"
AREA_TOL = 2.0          # 전용면적 매칭 허용 오차(㎡): area2가 정수로 잘려 와도 평형 구분엔 충분(25↔34평 차 큼)
PAGE_CAP = 12           # 단지당 최대 페이지(폭주 방지)
# 월세 보증금 상한(만원): 매물_관리.md [가격 기준]의 월세 보증금 상한(2026-06-28 1.5억으로 하향).
# 보증금이 이 값을 넘는 월세는 반전세성으로 자본이 묶여 '여유자금 투자' 목적에 어긋나므로 후보에서 제외한다.
# 전세금에는 적용하지 않는다(전세는 cap 없이 참고 정보로 병기).
WOLSE_DEPOSIT_CAP = 15000   # 1.5억
TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=collections.OrderedDict)


def http_get(url, referer=BASE + "/", token=None):
    h = {"User-Agent": UA, "Referer": referer,
         "Accept": "application/json, text/plain, */*", "Accept-Language": "ko-KR"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=h)
    try:
        with _opener.open(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace").replace("\n", " ")
        hint = ("\n→ 429는 요청 과다(IP)입니다. --sleep을 늘리고 잠시 뒤 재시도하세요."
                if e.code == 429 else
                "\n→ 401은 토큰 만료입니다. 다시 실행하면 새 토큰을 받습니다." if e.code == 401 else "")
        raise SystemExit("HTTP %s: %s%s" % (e.code, detail, hint))


def warmup_and_token(seed_complex):
    """쿠키 워밍업 후 단지 페이지에서 Bearer 토큰을 추출한다."""
    http_get(BASE + "/")                       # 쿠키 획득(REALESTATE 등)
    time.sleep(1)
    html = http_get(BASE + "/complexes/%s" % seed_complex)
    m = TOKEN_RE.search(html)
    if not m:
        raise SystemExit("new.land 페이지에서 인증 토큰을 찾지 못했습니다(구조 변경 가능성).")
    return m.group(0)


def search_complexes(keyword, token):
    body = http_get(BASE + "/api/search?keyword=%s&page=1" % urllib.parse.quote(keyword),
                    token=token)
    try:
        return json.loads(body).get("complexes", [])
    except json.JSONDecodeError:
        return []


def parse_price_eok(s):
    """'11억', '11억 1,000', '8,500'(만원) → 억(float). 실패 시 None."""
    if not s:
        return None
    t = s.replace(" ", "").replace(",", "")
    try:
        if "억" in t:
            eok_s, rest = t.split("억", 1)
            return int(eok_s) + (int(rest) / 10000.0 if rest else 0.0)
        return int(t) / 10000.0
    except ValueError:
        return None


def parse_manwon(s):
    """'3억 3,000', '3,000', '1억' → 만원(int). 실패 시 None."""
    if not s:
        return None
    t = str(s).replace(" ", "").replace(",", "")
    try:
        if "억" in t:
            eok_s, rest = t.split("억", 1)
            return int(eok_s) * 10000 + (int(rest) if rest else 0)
        return int(t)
    except ValueError:
        return None


def fmt_eok(v):
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return s if "." in s else s + ".0"


def fmt_dep(manwon):
    """보증금(만원)을 '3억'·'5,000만' 식으로 사람이 읽기 좋게."""
    if manwon >= 10000:
        return fmt_eok(manwon / 10000.0) + "억"
    return "%s만" % format(manwon, ",")


def fetch_sale_articles(complexno, token, sleep):
    """단지 한 곳의 매매(A1) 매물을 페이지네이션으로 수집 → [(area2, eok)]."""
    out = []
    for page in range(1, PAGE_CAP + 1):
        url = (BASE + "/api/articles/complex/%s?realEstateType=APT&tradeType=A1"
               "&order=prc&page=%d&complexNo=%s&showArticle=true&sameAddressGroup=false"
               % (complexno, page, complexno))
        data = json.loads(http_get(url, referer=BASE + "/complexes/%s" % complexno, token=token))
        arts = data.get("articleList", [])
        for a in arts:
            if a.get("tradeTypeName") not in (None, "매매"):
                continue
            try:
                area = float(a.get("area2"))
            except (TypeError, ValueError):
                continue
            eok = parse_price_eok(a.get("dealOrWarrantPrc"))
            if eok is not None:
                out.append((area, eok))
        if not data.get("isMoreData") or not arts:
            break
        time.sleep(sleep)
    return out


def fetch_lease_articles(complexno, token, sleep, tradetype):
    """단지 한 곳의 전세(B1)·월세(B2) 매물을 페이지네이션으로 수집.

    반환: B1 → [(area2, 전세금_만원)], B2 → [(area2, 보증금_만원, 월세_만원)].
    `dealOrWarrantPrc`가 전세금/월세 보증금, `rentPrc`가 월세(만원)다.
    """
    out = []
    for page in range(1, PAGE_CAP + 1):
        url = (BASE + "/api/articles/complex/%s?realEstateType=APT&tradeType=%s"
               "&order=prc&page=%d&complexNo=%s&showArticle=true&sameAddressGroup=false"
               % (complexno, tradetype, page, complexno))
        data = json.loads(http_get(url, referer=BASE + "/complexes/%s" % complexno, token=token))
        arts = data.get("articleList", [])
        for a in arts:
            if a.get("tradeTypeName") not in (None, "전세", "월세"):
                continue
            try:
                area = float(a.get("area2"))
            except (TypeError, ValueError):
                continue
            dep = parse_manwon(a.get("dealOrWarrantPrc"))
            if dep is None:
                continue
            if tradetype == "B1":
                out.append((area, dep))
            else:
                rent = parse_manwon(a.get("rentPrc"))
                if rent is not None:
                    out.append((area, dep, rent))
        if not data.get("isMoreData") or not arts:
            break
        time.sleep(sleep)
    return out


def resolve_lease(entry, token, sleep):
    """단지 한 곳의 평형별 전세 최저 호가·월세(보증금 최저 매물) 호가를 매칭.

    반환: (전세 가격 문자열 or None, 월세 가격 문자열 or None, 경고 리스트).
    """
    hscpnos = entry["hscpNo"]
    if isinstance(hscpnos, str):
        hscpnos = [hscpnos]
    jeonse, wolse = [], []
    for i, no in enumerate(hscpnos):
        if i:
            time.sleep(sleep)
        jeonse += fetch_lease_articles(no, token, sleep, "B1")
        time.sleep(sleep)
        wolse += fetch_lease_articles(no, token, sleep, "B2")

    j_lines, w_lines, warns = [], [], []
    for label, target in entry["areas"].items():
        jc = [dep for area, dep in jeonse if abs(area - target) <= AREA_TOL]
        if jc:
            j_lines.append("  • %s 전세 %s억 (최저, %d건)" % (label, fmt_eok(min(jc) / 10000.0), len(jc)))
        else:
            warns.append("%s 전세 매물 없음" % label)
        # 보증금 상한(WOLSE_DEPOSIT_CAP) 이하 월세만 후보로 본다 — 반전세성(보증금 과다) 매물 제외.
        wc = [(dep, rent) for area, dep, rent in wolse
              if abs(area - target) <= AREA_TOL and dep <= WOLSE_DEPOSIT_CAP]
        if wc:
            dep, rent = min(wc)  # 보증금 최저 매물(여유자금 확보 목적에 부합)
            w_lines.append("  • %s 보증금 %s / 월 %d만 (최저보증금, %d건)"
                           % (label, fmt_dep(dep), rent, len(wc)))
        else:
            warns.append("%s 월세 매물 없음(보증금 %s 이하)" % (label, fmt_dep(WOLSE_DEPOSIT_CAP)))
    return ("\n".join(j_lines) or None), ("\n".join(w_lines) or None), warns


def resolve(entry, token, sleep):
    """단지 한 곳의 평형별 '최저 호가'를 매칭해 (값 문자열 or None, 경고 리스트)."""
    hscpnos = entry["hscpNo"]
    if isinstance(hscpnos, str):
        hscpnos = [hscpnos]
    arts = []
    for i, no in enumerate(hscpnos):
        if i:
            time.sleep(sleep)
        arts += fetch_sale_articles(no, token, sleep)

    lines, warns = [], []
    for label, target in entry["areas"].items():
        cands = [eok for area, eok in arts if abs(area - target) <= AREA_TOL]
        if cands:
            lines.append("%s (%s, 최저 %d건)" % (fmt_eok(min(cands)), label, len(cands)))
        else:
            warns.append("%s 매칭 매물 없음(전용 %.1f㎡ ±%.1f)" % (label, target, AREA_TOL))
    return ("\n".join(lines) if lines else None), warns


def set_hoga(listing, value):
    """호가(억)을 최근 실거래가(억) 앞(없으면 지역 뒤)에 배치해 순서를 보기 좋게."""
    new = collections.OrderedDict()
    placed = False
    for k, v in listing.items():
        if k == "호가(억)":
            continue
        if k == "최근 실거래가(억)" and not placed:
            new["호가(억)"] = value
            placed = True
        new[k] = v
        if k == "지역" and "최근 실거래가(억)" not in listing and not placed:
            new["호가(억)"] = value
            placed = True
    if not placed:
        new["호가(억)"] = value
    return new


def gu_of(listings, name):
    """매물의 지역 문자열에서 '구'를 뽑는다.

    지역 형식이 두 가지다:
      - 서울: '서울 관악구 봉천동'  → '구' 접미사가 붙어 있어 정규식이 바로 잡힌다.
      - 경기: '성남 분당 구미동'    → 구가 접미사 없이('분당'·'수지'·'영통') 적혀 정규식이 못 잡는다.
    경기 형식은 '시 구 동' 3토큰이 일관되므로, 정규식 실패 시 두 번째 토큰을 구로 보고 '구'를 보정한다.
    """
    z = listings.get(name, {}).get("지역", "")
    m = re.search(r"(\S+구)", z)
    if m:
        return m.group(1)
    toks = z.split()
    return (toks[1] + "구") if len(toks) >= 3 else ""


def do_resolve(mapping, listings, token, sleep, out_name="naver_map.json"):
    """빈 hscpNo를 new.land 검색으로 채운다(단지명+행정구 매칭). 변경분을 파일에 기록."""
    changed = 0
    for name, entry in mapping.items():
        if name.startswith("_") or entry.get("skip") or entry.get("hscpNo"):
            continue
        gu = gu_of(listings, name)
        kw = re.sub(r"\(.*?\)", "", name).replace("·", "").replace(" ", "").strip()
        cs = search_complexes(kw, token)
        hit = [c for c in cs if gu and gu in c.get("cortarAddress", "")] or cs
        print("• %s (검색='%s', 구=%s)" % (name, kw, gu))
        for c in hit[:5]:
            print("    No=%s %s %s %s세대" % (c.get("complexNo"), c.get("complexName"),
                                           c.get("cortarAddress"), c.get("totalHouseholdCount")))
        if len(hit) == 1:
            entry["hscpNo"] = hit[0]["complexNo"]
            changed += 1
            print("    → 자동 채움: %s" % entry["hscpNo"])
        elif hit:
            print("    → 후보 여럿: hscpNo를 직접 골라 naver_map.json에 넣으세요")
        time.sleep(sleep)
    if changed:
        with open(os.path.join(DATA_DIR, out_name), "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        print("\n%s에 hscpNo %d개 자동 기록." % (out_name, changed))


def fill_lease(mapping, listings, token, args):
    """--lease: 전세/월세 호가를 listings.json '전세 가격'·'월세 가격'에 채운다."""
    filled, skipped, nomatch, nokey = [], [], [], []
    for name, entry in mapping.items():
        if name.startswith("_") or name not in listings:
            continue
        if args.only and name != args.only:
            continue
        if entry.get("skip"):
            skipped.append((name, entry["skip"]))
            continue
        if not entry.get("hscpNo"):
            nokey.append(name)
            continue
        jval, wval, warns = resolve_lease(entry, token, args.sleep)
        if jval or wval:
            # 한쪽이라도 매물을 받았으면(=조회 성공) 양쪽을 현재 호가로 덮어쓴다.
            # 조건에 맞는 매물이 없는 쪽(예: 보증금 1.5억 초과뿐인 월세)은 빈칸으로 비워
            # stale 값이 남지 않게 한다(매물_관리.md: 네이버에 매물 없으면 그 평형은 비워 둔다).
            listings[name]["전세 가격"] = jval or ""
            listings[name]["월세 가격"] = wval or ""
            filled.append((name, jval, wval, warns))
        else:
            nomatch.append((name, warns))
        time.sleep(args.sleep)

    print("=== 채움 (%d) ===" % len(filled))
    for name, jval, wval, warns in filled:
        print("  %s:" % name)
        if jval:
            print("    전세 %s" % jval.replace("\n", " /").strip())
        if wval:
            print("    월세 %s" % wval.replace("\n", " /").strip())
        if warns:
            print("    ⚠ %s" % "; ".join(warns))
    print("=== 매칭 없음 — 빈칸 유지 (%d) ===" % len(nomatch))
    for name, warns in nomatch:
        print("  %s: %s" % (name, "; ".join(warns)))
    if nokey:
        print("=== hscpNo 미입력 (%d) — `--resolve --lease`로 채우세요 ===" % len(nokey))
        for name in nokey:
            print("  %s" % name)
    if skipped:
        print("=== 건너뜀 skip (%d) ===" % len(skipped))
        for name, reason in skipped:
            print("  %s: %s" % (name, reason))

    if args.dry_run:
        print("\n[dry-run] listings.json은 변경하지 않았습니다.")
        return
    if not filled:
        print("\n채운 값이 없어 listings.json을 그대로 둡니다.")
        return
    with open(os.path.join(DATA_DIR, "listings.json"), "w", encoding="utf-8") as f:
        json.dump(listings, f, ensure_ascii=False, indent=2)
    print("\nlistings.json에 %d개 단지 전세/월세 호가 기록 완료. 이어서 `python3 make_notion.py`로 배포하세요."
          % len(filled))


def main():
    ap = argparse.ArgumentParser(description="네이버 매매/전세·월세 호가를 listings.json에 채운다")
    ap.add_argument("--dry-run", action="store_true", help="적용 없이 제안만 출력")
    ap.add_argument("--only", help="이 단지명 하나만 처리")
    ap.add_argument("--resolve", action="store_true", help="빈 hscpNo를 검색해 채우고 종료")
    ap.add_argument("--lease", action="store_true",
                    help="전세(B1)/월세(B2) 호가를 naver_lease_map.json 기준으로 채운다")
    ap.add_argument("--sleep", type=float, default=1.5, help="요청 간 대기 초(기본 1.5)")
    args = ap.parse_args()

    map_name = "naver_lease_map.json" if args.lease else "naver_map.json"
    listings = load_json("listings.json")
    mapping = load_json(map_name)

    # 토큰 추출용 seed 단지(아무 hscpNo나 하나)
    seed = next((e["hscpNo"][0] if isinstance(e["hscpNo"], list) else e["hscpNo"]
                 for k, e in mapping.items()
                 if not k.startswith("_") and e.get("hscpNo")), None)
    if not seed:
        # hscpNo가 하나도 없으면 잘 알려진 단지로 토큰만 받는다
        seed = "2987"
    token = warmup_and_token(seed)

    if args.resolve:
        do_resolve(mapping, listings, token, args.sleep, map_name)
        return

    if args.lease:
        fill_lease(mapping, listings, token, args)
        return

    filled, skipped, nomatch, nokey = [], [], [], []
    for name, entry in mapping.items():
        if name.startswith("_") or name not in listings:
            continue
        if args.only and name != args.only:
            continue
        if entry.get("skip"):
            skipped.append((name, entry["skip"]))
            continue
        if not entry.get("hscpNo"):
            nokey.append(name)
            continue
        value, warns = resolve(entry, token, args.sleep)
        if value:
            listings[name] = set_hoga(listings[name], value)
            filled.append((name, value, warns))
        else:
            nomatch.append((name, warns))
        time.sleep(args.sleep)

    print("=== 채움 (%d) ===" % len(filled))
    for name, value, warns in filled:
        print("  %s: %s%s" % (name, value.replace("\n", " / "),
                              ("  ⚠ " + "; ".join(warns)) if warns else ""))
    print("=== 매칭 없음 — 빈칸 유지 (%d) ===" % len(nomatch))
    for name, warns in nomatch:
        print("  %s: %s" % (name, "; ".join(warns)))
    if nokey:
        print("=== hscpNo 미입력 (%d) — --resolve로 채우세요 ===" % len(nokey))
        for name in nokey:
            print("  %s" % name)
    if skipped:
        print("=== 건너뜀 skip (%d) ===" % len(skipped))
        for name, reason in skipped:
            print("  %s: %s" % (name, reason))

    if args.dry_run:
        print("\n[dry-run] listings.json은 변경하지 않았습니다.")
        return
    if not filled:
        print("\n채운 값이 없어 listings.json을 그대로 둡니다.")
        return
    with open(os.path.join(DATA_DIR, "listings.json"), "w", encoding="utf-8") as f:
        json.dump(listings, f, ensure_ascii=False, indent=2)
    print("\nlistings.json에 %d개 단지 호가 기록 완료. 이어서 `python3 make_notion.py`로 배포하세요."
          % len(filled))


if __name__ == "__main__":
    main()
