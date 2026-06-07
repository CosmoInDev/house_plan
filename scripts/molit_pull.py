# -*- coding: utf-8 -*-
"""국토부 아파트 매매 실거래가를 받아 listings.json의 `최근 실거래가(억)`을 채운다(pull).

매매 단지마다 data/molit_map.json의 매칭 정보(시군구코드·국토부 단지명 패턴·전용면적)를
참고해, 국토부 오픈API에서 최근 N개월 거래를 받아 **단지명 + 동일 전용면적**으로 골라
가장 최근 체결가를 기록한다. 매칭이 안 되거나 'skip'인 단지는 빈칸으로 둔다(추정·보간 금지).

실행 (★ 사용자 본인 로컬 터미널에서 실행할 것):
    python3 molit_pull.py              # 조회→매칭→listings.json 기록
    python3 molit_pull.py --dry-run    # 적용 없이 제안만 출력
    python3 molit_pull.py --months 8   # 조회 개월 수(기본 6, 최근 달부터 거꾸로)

[중요] Claude Code의 Bash/curl은 이 API가 WAF에 막힌다("Request Blocked"/"Forbidden").
       세션 안에서 채울 땐 WebFetch 도구를 쓰고, 이 스크립트는 사용자 로컬에서 돌린다.

인증키(../.molit.json, .gitignore 등록됨):
    {"service_key": "발급받은_일반_인증키(Decoding)"}
환경변수 MOLIT_SERVICE_KEY로도 줄 수 있다. 키 401이면 Encoding/Decoding 키를 서로 바꿔 시도.
"""
import argparse
import collections
import datetime
import json
import os
import socket
import statistics
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# urllib이 IPv6를 먼저 시도하다 지연되는 환경이 있어 IPv4만 강제(make_notion.py와 동일).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, *a, **k):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *a, **k)


socket.getaddrinfo = _ipv4_only
socket.setdefaulttimeout(30)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")
KEY_PATH = os.path.join(HERE, "..", ".molit.json")
ENDPOINT = ("https://apis.data.go.kr/1613000/"
            "RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade")
AREA_TOL = 1.0          # 전용면적 매칭 허용 오차(㎡): 같은 평형의 미세 변형(84.96 vs 84.99)을 흡수
OUTLIER_LO, OUTLIER_HI = 0.7, 1.6   # 같은 평형 거래의 중앙값 대비 이 배수를 벗어나면 직거래성 이상치로 제외


def load_key():
    key = os.environ.get("MOLIT_SERVICE_KEY")
    if not key and os.path.exists(KEY_PATH):
        with open(KEY_PATH, encoding="utf-8") as f:
            key = json.load(f).get("service_key")
    if not key:
        raise SystemExit(
            "국토부 인증키가 없습니다. ../.molit.json에 {\"service_key\": \"...\"} 또는 "
            "MOLIT_SERVICE_KEY 환경변수를 설정하세요.")
    return key


def load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=collections.OrderedDict)


def recent_ymds(n):
    """오늘 기준 최근 n개월의 YYYYMM 문자열을 최신→과거 순으로."""
    y, m = datetime.date.today().year, datetime.date.today().month
    out = []
    for _ in range(n):
        out.append("%04d%02d" % (y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def parse_items(xml_text):
    """응답 XML → [{aptNm, area, amount(만원), floor, date=(y,m,d), umd}]. 오류면 SystemExit."""
    head = xml_text.strip()[:80].replace("\n", " ")
    blocked = ("→ Claude Code Bash는 이 API가 WAF에 막힙니다. 로컬 터미널에서 실행하세요.")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise SystemExit("응답이 XML이 아닙니다(차단 가능성): %r\n%s" % (head, blocked))
    tag = root.tag.lower().rsplit("}", 1)[-1]   # 네임스페이스 제거
    if tag == "html":
        raise SystemExit("HTML 응답(차단 가능성): %r\n%s" % (head, blocked))
    code = root.findtext(".//resultCode")
    auth = root.findtext(".//returnAuthMsg")
    if code is None and auth is None:
        raise SystemExit("국토부 API 응답이 아닙니다(차단 가능성): root=<%s>, %r\n%s"
                         % (root.tag, head, blocked))
    if auth and "NORMAL" not in auth.upper():
        raise SystemExit("국토부 API 인증 오류: %s (returnReasonCode=%s) — 키/활용신청 확인"
                         % (auth, root.findtext(".//returnReasonCode")))
    if code is not None and code not in ("000", "00"):
        raise SystemExit("국토부 API 오류 resultCode=%s, resultMsg=%s"
                         % (code, root.findtext(".//resultMsg")))
    items = []
    for it in root.findall(".//item"):
        try:
            area = float(it.findtext("excluUseAr"))
            amount = int(it.findtext("dealAmount").replace(",", "").strip())
            date = (int(it.findtext("dealYear")), int(it.findtext("dealMonth")),
                    int(it.findtext("dealDay")))
        except (TypeError, ValueError, AttributeError):
            continue   # 필드 누락/형식 이상 행은 건너뜀
        items.append({"aptNm": (it.findtext("aptNm") or "").strip(),
                      "area": area, "amount": amount, "floor": it.findtext("floor"),
                      "date": date, "umd": (it.findtext("umdNm") or "").strip()})
    return items


def fetch_month(key, lawd, ymd, cache):
    """(lawd, ymd)의 모든 거래 item을 받아온다(페이지네이션). cache로 중복 호출 방지."""
    if (lawd, ymd) in cache:
        return cache[(lawd, ymd)]
    items, page = [], 1
    while True:
        qs = urllib.parse.urlencode({"serviceKey": key, "LAWD_CD": lawd,
                                     "DEAL_YMD": ymd, "numOfRows": 1000, "pageNo": page})
        try:
            with urllib.request.urlopen(ENDPOINT + "?" + qs) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace").replace("\n", " ")
            hint = ("\n→ Claude Code Bash는 이 API가 WAF에 막힙니다(이 오류). 사용자 로컬 터미널에서 실행하세요."
                    if e.code in (400, 401, 403) else "")
            raise SystemExit("HTTP %s (%s %s): %s%s" % (e.code, lawd, ymd, detail, hint))
        batch = parse_items(body)
        items += batch
        total = ET.fromstring(body).findtext(".//totalCount")
        if not batch or (total and len(items) >= int(total)) or len(batch) < 1000:
            break
        page += 1
    cache[(lawd, ymd)] = items
    return items


def fmt_eok(amount_manwon):
    s = "%.2f" % (amount_manwon / 10000.0)
    s = s.rstrip("0").rstrip(".")
    return s if "." in s else s + ".0"


def pick_recent(cands):
    """같은 평형 후보 거래들에서 이상치를 거르고 가장 최근 1건을 고른다. 없으면 None."""
    if not cands:
        return None
    if len(cands) >= 4:                       # 표본이 충분하면 중앙값 기준 이상치 제외
        med = statistics.median(c["amount"] for c in cands)
        kept = [c for c in cands if OUTLIER_LO * med <= c["amount"] <= OUTLIER_HI * med]
        cands = kept or cands
    return max(cands, key=lambda c: c["date"])


def resolve(entry, key, cache, ymds):
    """단지 한 곳의 평형별 최근 실거래를 매칭해 (값 문자열 or None, 경고 리스트) 반환."""
    patterns = entry["aptNm"]
    pool = []
    for ymd in ymds:
        for it in fetch_month(key, entry["lawd_cd"], ymd, cache):
            if any(p in it["aptNm"] for p in patterns):
                pool.append(it)
    lines, warns = [], []
    for label, target in entry["areas"].items():
        cands = [it for it in pool if abs(it["area"] - target) <= AREA_TOL]
        best = pick_recent(cands)
        if best:
            y, m, _ = best["date"]
            lines.append("%s (%s, %02d.%02d)" % (fmt_eok(best["amount"]), label, y % 100, m))
        else:
            warns.append("%s 매칭 없음(전용 %.2f㎡ ±%.1f)" % (label, target, AREA_TOL))
    return ("\n".join(lines) if lines else None), warns


def set_recent(listing, value):
    """최근 실거래가(억)을 호가(억) 뒤(없으면 지역 뒤)에 넣어 순서를 보기 좋게 유지."""
    anchor = "호가(억)" if "호가(억)" in listing else "지역"
    new = collections.OrderedDict()
    placed = False
    for k, v in listing.items():
        if k == "최근 실거래가(억)":
            continue
        new[k] = v
        if k == anchor and not placed:
            new["최근 실거래가(억)"] = value
            placed = True
    if not placed:
        new["최근 실거래가(억)"] = value
    return new


def main():
    ap = argparse.ArgumentParser(description="국토부 실거래가를 listings.json에 채운다")
    ap.add_argument("--months", type=int, default=6, help="조회할 최근 개월 수(기본 6)")
    ap.add_argument("--dry-run", action="store_true", help="적용 없이 제안만 출력")
    args = ap.parse_args()

    key = load_key()
    listings = load_json("listings.json")
    mapping = load_json("molit_map.json")
    ymds = recent_ymds(args.months)
    cache = {}

    filled, skipped, nomatch = [], [], []
    for name, entry in mapping.items():
        if name.startswith("_") or name not in listings:
            continue
        if entry.get("skip"):
            skipped.append((name, entry["skip"]))
            continue
        value, warns = resolve(entry, key, cache, ymds)
        if value:
            listings[name] = set_recent(listings[name], value)
            filled.append((name, value, warns))
        else:
            nomatch.append((name, warns))

    print("=== 채움 (%d) ===" % len(filled))
    for name, value, warns in filled:
        print("  %s: %s%s" % (name, value.replace("\n", " / "),
                              ("  ⚠ " + "; ".join(warns)) if warns else ""))
    print("=== 매칭 없음 — 빈칸 유지 (%d) ===" % len(nomatch))
    for name, warns in nomatch:
        print("  %s: %s" % (name, "; ".join(warns)))
    print("=== 건너뜀 skip — 빈칸 유지 (%d) ===" % len(skipped))
    for name, reason in skipped:
        print("  %s: %s" % (name, reason))

    if args.dry_run:
        print("\n[dry-run] listings.json은 변경하지 않았습니다.")
        return
    with open(os.path.join(DATA_DIR, "listings.json"), "w", encoding="utf-8") as f:
        json.dump(listings, f, ensure_ascii=False, indent=2)
    print("\nlistings.json에 %d개 단지 기록 완료. 이어서 `python3 make_notion.py`로 배포하세요." % len(filled))


if __name__ == "__main__":
    main()
