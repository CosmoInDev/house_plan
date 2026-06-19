# -*- coding: utf-8 -*-
"""Notion 표 → data/*.json 역방향 동기화(pull).

make_notion.py(push)가 그린 Notion 표를 다시 읽어, 사용자가 Notion에서 직접
수정한 내용을 data/listings.json·data/config.json으로 회수한다. push의 거울상이다.

merge 원칙:
- Notion 표에 나타나는 필드(ATTRS)만 갱신한다.
- Notion에 없는 로컬 필드(id, estimate, 향후 지도 좌표 등)는 보존한다.
- 표에서 빠진 매물은 자동 삭제하지 않고 경고만 한다(데이터 유실 방지).
- 표에 새로 생긴 매물은 id 없이 추가하고, id 수동 부여를 경고한다.
- 거래유형>시>구 분류(groups)는 Notion의 헤딩·표 배치를 그대로 반영한다.

실행:
    python3 notion_pull.py   # .notion.json의 block_ids가 가리키는 표를 읽어 JSON 갱신

자격 증명은 make_notion.py와 동일(../.notion.json 또는 NOTION_TOKEN).
표 위치는 make_notion.py가 저장해 둔 block_ids로 찾으므로, 먼저 한 번 push되어 있어야 한다.
"""
import json
import os

from make_notion import api, load_conf, normalize_id

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")


def cell_plain(cell):
    """table_row의 셀(rich_text 배열) → 평문 문자열.

    Notion GET 응답(plain_text 보유)과 push body(text.content) 양쪽 형식을 모두 지원해,
    실제 API 없이도 build_table_block 출력으로 라운드트립 검증이 가능하다.
    """
    return "".join(
        rt.get("plain_text", rt.get("text", {}).get("content", ""))
        for rt in cell)


def parse_value(text):
    """push의 cell_text를 역변환한다. '· '로 시작하면 글머리 리스트, 아니면 문자열.

    push는 리스트를 항상 모든 줄에 '· '를 붙여 그리므로, 첫 줄만 보고 리스트인지 판단한다.
    (사용자가 Notion에서 일부 줄의 '· '를 지운 혼합 편집은 접두어 없는 줄을 원문대로 합류시킨다.)
    """
    if text.startswith("· "):
        return [line[2:] if line.startswith("· ") else line
                for line in text.split("\n") if line.strip()]
    return text


def parse_table_rows(rows):
    """table_row 블록 리스트 → (매물명 순서, 속성 순서, {매물명: {attr: value}}).

    rows[0]은 헤더([빈칸, 매물명...]), 이후 각 행은 [속성명, 값...]이다.
    속성 순서(attrs)는 표의 행 라벨을 그대로 따르므로 용도별 컬럼 구성을 반영한다.
    빈 셀(미설정 속성)은 결과에 넣지 않는다.
    """
    if not rows:  # 표가 비었거나 block_ids가 표 아닌 블록을 가리키는 경우
        return [], [], {}
    header = [cell_plain(c) for c in rows[0]["table_row"]["cells"]]
    names = header[1:]  # 첫 칸은 라벨 열(좌상단 빈칸)
    attrs = []
    data = {n: {} for n in names}
    for row in rows[1:]:
        cells = row["table_row"]["cells"]
        attr = cell_plain(cells[0])
        attrs.append(attr)
        for name, cell in zip(names, cells[1:]):
            text = cell_plain(cell)
            if text != "":
                data[name][attr] = parse_value(text)
    return names, attrs, data


def fetch_children(token, block_id):
    # block_id는 Notion 응답에서 받은 유효 UUID라 normalize 없이 그대로 쓴다.
    res = api("/blocks/%s/children?page_size=100" % block_id,
              method="GET", token=token)
    # 표 행은 속성+헤더(십수 행)뿐이라 100을 넘을 수 없다. 넘으면 부분 데이터로
    # 덮어쓰는 사고를 막기 위해 중단한다(조용히 잘라 반환하지 않음).
    if res.get("has_more"):
        raise SystemExit(
            "[오류] 표 행이 100개를 넘습니다(block_id=%s). 지원하지 않는 크기입니다." % block_id)
    return res.get("results", [])


def fetch_groups(conf, token):
    """block_ids를 따라가며 거래유형>시>구 3단계 구조를 복원한다.

    block_ids는 push가 [heading_1(거래유형), heading_2(시), heading_3(구), table,
    heading_3, table, ..., heading_2(시), ..., heading_1(거래유형), ...] 순으로 저장한다.
    블록 타입을 직접 확인해, table 직전 heading_3을 '구', 그 위 heading_2를 '시',
    그 위 heading_1을 거래유형으로 묶는다.

    반환: [{"title", "attrs", "tables": [{"city", "district", "names", "data"}]}].
    tables는 문서 순서대로의 (시,구)별 표 리스트다(중첩은 apply_pull에서 복원).
    같은 거래유형의 모든 표는 attrs가 같아 마지막 표 기준으로 group.attrs를 둔다.
    """
    groups = []
    cur_group = None
    cur_city = None
    cur_district = None
    for bid in conf.get("block_ids", []):
        block = api("/blocks/%s" % normalize_id(bid), method="GET", token=token)
        btype = block.get("type")
        if btype == "heading_1":
            cur_group = {"title": cell_plain(block["heading_1"]["rich_text"]),
                         "attrs": [], "tables": []}
            groups.append(cur_group)
            cur_city = None
            cur_district = None
        elif btype == "heading_2":
            cur_city = cell_plain(block["heading_2"]["rich_text"])
            cur_district = None
        elif btype == "heading_3":
            cur_district = cell_plain(block["heading_3"]["rich_text"])
        elif btype == "table" and cur_group is not None:
            names, attrs, data = parse_table_rows(fetch_children(token, block["id"]))
            cur_group["attrs"] = attrs
            cur_group["tables"].append(
                {"city": cur_city or "", "district": cur_district or "",
                 "names": names, "data": data})
    return groups


def _load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def _dump_json(name, obj):
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def merge_listing(old, new_attrs, attrs):
    """기존 레코드에 Notion 값(new_attrs)을 덮어쓰되 로컬 필드는 보존한다.

    attrs는 그 매물이 속한 용도 표의 속성 순서다.
    결과 키 순서: id → attrs(설정된 것만) → 나머지 로컬 키(estimate, 좌표 등).
    old가 None이면 신규 매물이라 id가 없다(호출부에서 경고).
    """
    rec = {}
    if old and "id" in old:
        rec["id"] = old["id"]
    for attr in attrs:
        if attr in new_attrs:
            rec[attr] = new_attrs[attr]
    if old:
        for key, value in old.items():
            if key != "id" and key not in attrs and key not in rec:
                rec[key] = value  # Notion에 없는 로컬 메타(estimate, 좌표 등) 보존
    return rec


def _nest_cities(tables):
    """문서 순서의 (시,구)별 표 리스트를 시 아래 구를 중첩한 config 형태로 묶는다.

    같은 시는 등장 순서를 보존해 하나의 city로 합치고, 그 안에 districts를 순서대로 쌓는다.
    반환: [{"city", "districts": [{"district", "names"}]}].
    """
    by_city = {}
    order = []
    for t in tables:
        if t["city"] not in by_city:
            by_city[t["city"]] = []
            order.append(t["city"])
        by_city[t["city"]].append({"district": t["district"], "names": t["names"]})
    return [{"city": c, "districts": by_city[c]} for c in order]


def apply_pull(groups, listings, config):
    """Notion에서 읽은 groups를 listings/config에 merge한다. 변경 요약을 반환."""
    notion_names, updated, created = [], [], []
    for group in groups:
        attrs = group["attrs"]
        for table in group["tables"]:
            for name in table["names"]:
                notion_names.append(name)
                old = listings.get(name)
                (created if old is None else updated).append(name)
                listings[name] = merge_listing(old, table["data"][name], attrs)

    config["groups"] = [
        {"title": g["title"], "attrs": g["attrs"],
         "cities": _nest_cities(g["tables"])}
        for g in groups]
    orphans = [n for n in listings if n not in notion_names]
    return updated, created, orphans


def main():
    conf = load_conf()
    token = conf["token"]
    if not conf.get("block_ids"):
        raise SystemExit(
            "block_ids가 없습니다. 먼저 make_notion.py로 표를 한 번 배포하세요.")

    groups = fetch_groups(conf, token)
    listings = _load_json("listings.json")
    config = _load_json("config.json")
    updated, created, orphans = apply_pull(groups, listings, config)
    _dump_json("listings.json", listings)
    _dump_json("config.json", config)

    print("pull 완료: 갱신 %d건, 신규 %d건" % (len(updated), len(created)))
    for name in created:
        print("  [신규] %s — id가 없습니다. listings.json에 id를 수동 부여하세요." % name)
    for name in orphans:
        print("  [경고] '%s'는 Notion 표에 없지만 JSON에 유지됩니다(자동 삭제 안 함)." % name)


if __name__ == "__main__":
    main()
