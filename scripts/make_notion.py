# -*- coding: utf-8 -*-
"""Notion 페이지에 '주거 계획' 전치 표(매물=열, 속성=행인 가로 배치)를 생성/갱신한다.

listings.py(데이터 소스 data/listings.json)에서 읽어, 거래유형(매매 / 월세·전세)을
heading_2로, 그 아래 '시'를 heading_3로 두고 시마다 단순 표(simple table) 블록을 만든다.
표는 전치 매트릭스 — 1열=속성 라벨(좌상단은 빈칸), 이후 각 열=매물 1건.
(Notion '데이터베이스'는 항목=행이 고정이라 가로 배치가 불가능해, 정적 표 블록을 쓴다.)

실행:
    python3 make_notion.py   # 부모 페이지에 거래유형>시별 표를 (재)생성. 이전에 만든 표는 교체.

자격 증명(../.notion.json, .gitignore 등록됨):
    {
      "token": "ntn_...",            # Notion integration 토큰 (필수)
      "parent_page_id": "...",       # 표를 넣을 페이지. URL 통째로 넣어도 됨.
      "block_ids": [...],            # 자동 기록 — 이전에 만든 표/헤딩 블록 id (재실행 시 교체용)
      "database_id": "..."           # (구버전) 있으면 실행 시 보관 처리 후 제거
    }
환경변수 NOTION_TOKEN / NOTION_PARENT_PAGE_ID로도 줄 수 있다.
부모 페이지는 Notion에서 '⋯ → 연결(Connections)'로 integration과 공유해야 한다.
"""
import json
import os
import re
import socket
import urllib.error
import urllib.request

from listings import GROUPS, cell_text, validate

# urllib이 IPv6를 먼저 시도하다 ~20초씩 지연되는 환경이 있어 IPv4(AF_INET)만 쓰도록 강제한다.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


socket.getaddrinfo = _ipv4_only
socket.setdefaulttimeout(30)

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(HERE, "..", ".notion.json")

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"

LABEL_COL = ""  # 전치 표 좌상단(속성 라벨 열 머리)은 빈칸으로 둔다

# '목적' 행 셀의 배경색: 값에 따라 한눈에 구분되게 칠한다(Notion 지원 색만 사용).
# 색은 데이터가 아니라 '목적' 값에서 렌더 시점에 파생되므로 JSON에는 저장하지 않는다.
PURPOSE_COLORS = {"실거주": "blue_background", "몸테크": "red_background"}


def load_conf():
    conf = {}
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH) as f:
            conf = json.load(f)
    for env, key in (("NOTION_TOKEN", "token"),
                     ("NOTION_PARENT_PAGE_ID", "parent_page_id")):
        if os.environ.get(env):
            conf[key] = os.environ[env]
    if not conf.get("token"):
        raise SystemExit(
            "Notion 토큰이 없습니다. ../.notion.json에 {\"token\": \"ntn_...\"} 또는 "
            "NOTION_TOKEN 환경변수를 설정하세요.")
    return conf


def save_conf(conf):
    with open(CONF_PATH, "w") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)


def normalize_id(raw):
    """페이지/DB/블록 ID를 URL에서도 뽑아내 32-hex로 정규화한다(하이픈 제거)."""
    if not raw:
        return raw
    ids = re.findall(r"[0-9a-fA-F]{32}", raw.replace("-", ""))
    return ids[-1] if ids else raw


def api(path, body=None, method="POST", token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit("HTTP %s\n%s" % (e.code, e.read().decode()))


def rich_text(value, bold=False, color=None):
    """문자열을 rich_text 배열로. 빈 값은 빈 배열(빈 셀). bold=True면 굵게, color는 글자/배경색."""
    if not value:
        return []
    rt = {"text": {"content": value}}
    ann = {}
    if bold:
        ann["bold"] = True
    if color:
        ann["color"] = color
    if ann:
        rt["annotations"] = ann
    return [rt]


def table_row(values, bold_from=None, colors=None):
    """문자열 리스트 → table_row 블록. bold_from 이상 인덱스 셀은 굵게(헤더의 매물명용).

    colors는 values와 같은 길이의 리스트로 셀별 배경색(없으면 None). '목적' 행 색칠용.
    """
    cells = [rich_text(v, bold=bold_from is not None and i >= bold_from,
                       color=colors[i] if colors else None)
             for i, v in enumerate(values)]
    return {"type": "table_row", "table_row": {"cells": cells}}


def build_table_block(attrs, names):
    """전치 표 블록: 헤더행(빈칸, 매물명…) + 속성행들(라벨, 각 매물 값…).

    헤더의 매물명은 굵게 표시한다(좌상단 빈칸 제외). attrs는 용도별 행 구성.
    """
    rows = [table_row([LABEL_COL] + names, bold_from=1)]
    for attr in attrs:
        values = [cell_text(n, attr) for n in names]
        # '목적' 행은 값(실거주/몸테크)에 따라 셀 배경색을 입힌다. 라벨 칸(0)은 색 없음.
        colors = ([None] + [PURPOSE_COLORS.get(v) for v in values]
                  if attr == "목적" else None)
        rows.append(table_row([attr] + values, colors=colors))
    return {"type": "table",
            "table": {"table_width": len(names) + 1,
                      "has_column_header": True,
                      "has_row_header": True,
                      "children": rows}}


def heading_block(level, text):
    """heading_1(거래유형)·heading_2(시)·heading_3(구) 블록을 만든다. level은 1·2·3."""
    key = "heading_%d" % level
    return {"type": key, key: {"rich_text": [{"text": {"content": text}}]}}


def build_children():
    """거래유형(heading_1) → 시(heading_2) → 구(heading_3) → 표 순으로 이어 붙인 children.

    pull(notion_pull.py)은 이 블록 순서를 거꾸로 읽어 거래유형·시·구 계층을 복원하므로,
    헤딩 레벨(1=거래유형, 2=시, 3=구)과 [헤딩, 표] 배치 순서를 바꾸면 안 된다.
    """
    children = []
    for title, attrs, cities in GROUPS:
        children.append(heading_block(1, title))
        for city, districts in cities:
            children.append(heading_block(2, city))
            for district, names in districts:
                children.append(heading_block(3, district))
                children.append(build_table_block(attrs, names))
    return children


def delete_blocks(token, ids):
    """이전에 만든 블록들을 삭제(보관)한다. 이미 없으면 무시."""
    for bid in ids or []:
        try:
            api("/blocks/%s" % bid, method="DELETE", token=token)
        except SystemExit:
            pass  # 이미 삭제된 블록 등은 건너뛴다


def archive_database(token, db_id):
    api("/databases/%s" % db_id, {"archived": True},
        method="PATCH", token=token)


def main():
    for w in validate():
        print("[경고]", w)
    conf = load_conf()
    token = conf["token"]

    # 구버전 DB가 남아 있으면 보관 처리하고 제거.
    if conf.get("database_id"):
        archive_database(token, normalize_id(conf["database_id"]))
        print("이전 DB 보관(삭제) 처리:", conf.pop("database_id"))

    parent = normalize_id(conf.get("parent_page_id"))
    if not parent:
        raise SystemExit(
            "parent_page_id가 없습니다. ../.notion.json에 표를 넣을 페이지 URL/ID를 넣으세요.")

    # 이전에 만든 표/헤딩 블록을 지워 중복을 막는다.
    delete_blocks(token, conf.get("block_ids"))

    res = api("/blocks/%s/children" % parent,
              {"children": build_children()}, method="PATCH", token=token)
    conf["block_ids"] = [b["id"] for b in res.get("results", [])]
    save_conf(conf)

    tables = sum(len(districts) for _, _, cities in GROUPS for _, districts in cities)
    n = sum(len(names) for _, _, cities in GROUPS
            for _, districts in cities for _, names in districts)
    print("전치 표 생성 완료 (거래유형 %d개, 구별 표 %d개, 매물 %d건)"
          % (len(GROUPS), tables, n))
    print("URL: https://www.notion.so/%s" % parent)


if __name__ == "__main__":
    main()
