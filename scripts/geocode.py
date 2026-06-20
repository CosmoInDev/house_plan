# -*- coding: utf-8 -*-
"""listings.json의 단지를 카카오 지오코딩해 data/coords.json에 좌표를 채운다(pull).

지도 SPA(web/)가 마커를 찍으려면 매물별 위경도가 필요한데, listings.json에도
네이버·국토부 API 응답에도 좌표가 없다. 이 스크립트가 카카오 로컬 API로 단지명을
질의해 좌표를 받아 보조 테이블 data/coords.json에 캐시한다(증분).

좌표는 Notion과 sync되는 데이터가 아니므로 listings.json에 넣지 않고 별도 파일로 둔다
(molit_map.json·naver_map.json과 같은 보조 매핑 위치).

작동 방식
  1) listings.json의 단지명 + '지역' 텍스트로 질의 후보를 만든다.
  2) 카카오 키워드 검색(단지명 우선) → 실패 시 주소 검색(지역) → 결합 재시도.
  3) '지역'의 시 토큰(성남·용인·수원·서울 등)이 결과 주소에 들어간 후보를 우선 채택해
     동명 단지 오매칭을 줄인다.

매칭표 data/coords.json
  {단지명: {"lat": .., "lng": .., "matched": "채택한 질의", "source": "keyword|address"}}

인증키 .kakao.json  (커밋 금지)
  {"rest_key": "<카카오 REST API 키>", "js_key": "<카카오 JavaScript 키>"}

실행
  python3 geocode.py                 # 좌표 없는 단지만 채움(증분)
  python3 geocode.py --force         # 전체 재조회
  python3 geocode.py --only "관악드림타운(삼성·동아)"
  python3 geocode.py --dry-run       # coords.json에 쓰지 않고 결과만 출력
  python3 geocode.py --sleep 0.5     # 요청 간 대기(초, 기본 0.3)
"""
import argparse
import collections
import json
import os
import re
import socket
import sys
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
KAKAO_KEY_FILE = os.path.join(HERE, "..", ".kakao.json")

KAKAO = "https://dapi.kakao.com/v2/local/search/%s.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=collections.OrderedDict)


def load_rest_key():
    try:
        with open(KAKAO_KEY_FILE, encoding="utf-8") as f:
            key = json.load(f).get("rest_key")
    except FileNotFoundError:
        raise SystemExit(
            ".kakao.json이 없습니다. {\"rest_key\": \"...\", \"js_key\": \"...\"} 형식으로 만드세요.")
    if not key:
        raise SystemExit(".kakao.json에 rest_key가 비어 있습니다.")
    return key


def kakao_get(kind, query, rest_key):
    """카카오 로컬 검색. kind='keyword'|'address'. documents 리스트 반환."""
    url = KAKAO % kind + "?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(
        url, headers={"Authorization": "KakaoAK " + rest_key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("documents", [])
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace").replace("\n", " ")
        if e.code in (401, 403):
            raise SystemExit("HTTP %s: 카카오 키 인증 실패. .kakao.json의 rest_key와 "
                             "허용 IP/플랫폼 설정을 확인하세요. (%s)" % (e.code, detail))
        if e.code == 429:
            raise SystemExit("HTTP 429: 카카오 쿼터 초과. 잠시 후 재시도하세요. (%s)" % detail)
        raise SystemExit("HTTP %s: %s" % (e.code, detail))


def city_token(region):
    """'지역'에서 시 토큰을 뽑아 오매칭 방지용 substring으로 쓴다.

    예: '서울 관악구 봉천동'→'서울', '성남시 분당구 야탑동 (오리역)'→'성남',
        '용인 기흥 흥덕'→'용인', '수원 권선구'→'수원'.
    """
    tok = region.split()[0] if region.split() else ""
    return re.sub(r"(특별시|광역시|시|도)$", "", tok)


def clean_region(region):
    """주소 검색용으로 '지역'을 정제: 괄호 힌트 제거('(오리역)' 등)."""
    return re.sub(r"\(.*?\)", "", region).strip()


def query_candidates(name, region):
    """질의 후보를 (kind, query) 순서로 만든다. 단지명 키워드 우선."""
    city = city_token(region)
    clean_name = re.sub(r"\(.*?\)", "", name).strip()      # '관악드림타운(삼성·동아)'→'관악드림타운'
    addr = clean_region(region)
    cands = [
        ("keyword", name),
        ("keyword", clean_name),
        ("keyword", (city + " " + clean_name).strip()),
        ("address", addr),
    ]
    # 중복 질의 제거(순서 유지)
    seen, out = set(), []
    for kind, q in cands:
        q = q.strip()
        if q and (kind, q) not in seen:
            seen.add((kind, q))
            out.append((kind, q))
    return out


def geocode(name, region, rest_key, sleep):
    """단지 하나의 좌표를 (record, 후보질의수) 또는 (None, n)로 반환."""
    city = city_token(region)
    tried = 0
    fallback = None     # 시 토큰 불일치라도 마지막 보루로 둘 첫 결과
    for kind, q in query_candidates(name, region):
        tried += 1
        docs = kakao_get(kind, q, rest_key)
        time.sleep(sleep)
        if not docs:
            continue
        # 시 토큰이 결과 주소에 들어간 첫 후보를 우선 채택
        # (키워드·주소 검색 모두 address_name/road_address_name을 top-level로 준다)
        for d in docs:
            addr = d.get("road_address_name") or d.get("address_name") or ""
            if city and city in addr:
                return ({"lat": float(d["y"]), "lng": float(d["x"]),
                         "matched": q, "source": kind}, tried)
        if fallback is None:
            d = docs[0]
            fallback = {"lat": float(d["y"]), "lng": float(d["x"]),
                        "matched": q, "source": kind + "?"}   # '?'=시 토큰 미확인
    return (fallback, tried)


def main():
    ap = argparse.ArgumentParser(description="카카오 지오코딩으로 coords.json을 채운다")
    ap.add_argument("--force", action="store_true", help="이미 좌표가 있어도 전체 재조회")
    ap.add_argument("--only", help="이 단지명 하나만 처리")
    ap.add_argument("--dry-run", action="store_true", help="coords.json에 쓰지 않고 결과만 출력")
    ap.add_argument("--sleep", type=float, default=0.3, help="요청 간 대기 초(기본 0.3)")
    args = ap.parse_args()

    rest_key = load_rest_key()
    listings = load_json("listings.json")
    try:
        coords = load_json("coords.json")
    except FileNotFoundError:
        coords = collections.OrderedDict()
        coords["_comment"] = ("카카오 지오코딩 좌표 캐시 (geocode.py 전용). "
                              "key=listings.json 단지명. source 끝의 '?'는 시 토큰 미확인(검수 권장).")

    filled, skipped, failed, uncertain = [], [], [], []
    for name in listings:
        if args.only and name != args.only:
            continue
        region = listings[name].get("지역", "")
        have = name in coords and coords[name].get("lat") is not None
        if have and not args.force:
            skipped.append(name)
            continue
        rec, tried = geocode(name, region, rest_key, args.sleep)
        if rec:
            coords[name] = rec
            filled.append((name, rec))
            if rec["source"].endswith("?"):
                uncertain.append(name)
        else:
            failed.append((name, region))

    print("=== 채움 (%d) ===" % len(filled))
    for name, rec in filled:
        mark = "  ⚠ 시 토큰 미확인" if rec["source"].endswith("?") else ""
        print("  %s: (%.5f, %.5f) ← '%s' [%s]%s"
              % (name, rec["lat"], rec["lng"], rec["matched"], rec["source"], mark))
    if failed:
        print("=== 좌표 못 찾음 — 빈칸 유지 (%d) ===" % len(failed), file=sys.stderr)
        for name, region in failed:
            print("  %s (지역='%s')" % (name, region), file=sys.stderr)
    if uncertain:
        print("=== 검수 권장(시 토큰 미확인, %d) ===" % len(uncertain), file=sys.stderr)
        for name in uncertain:
            print("  %s" % name, file=sys.stderr)
    print("(증분 skip %d건)" % len(skipped))

    if args.dry_run:
        print("\n[dry-run] coords.json은 변경하지 않았습니다.")
        return
    if not filled:
        print("\n새로 채운 좌표가 없어 coords.json을 그대로 둡니다.")
        return
    with open(os.path.join(DATA_DIR, "coords.json"), "w", encoding="utf-8") as f:
        json.dump(coords, f, ensure_ascii=False, indent=2)
    print("\ncoords.json에 %d개 단지 좌표 기록 완료." % len(filled))


if __name__ == "__main__":
    main()
