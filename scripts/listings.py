# -*- coding: utf-8 -*-
"""매물 데이터 로더 + 표 렌더링/검증 로직.

데이터 단일 소스는 ../data/listings.json (매물)과 ../data/config.json (열 순서·분류).
이 모듈은 그 JSON을 읽어 LISTINGS/CATEGORIES와 헬퍼(cell_text, validate)를
make_notion.py 등에 제공한다. 데이터 자체는 코드가 아니라 JSON에서 편집한다.

향후 Notion → JSON pull 모듈과 지도 SPA가 같은 JSON을 공유한다.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")


def _load(filename):
    with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


# 매물 데이터. id/estimate 같은 메타 필드도 들어 있으나 표에는 ATTRS만 렌더링된다.
LISTINGS = _load("listings.json")

_config = _load("config.json")
# 거래유형(매매 / 월세·전세)별로 (제목, 행 속성 순서, [(시, 매물명들)]).
# 거래유형 아래를 다시 '시' 단위 표로 나눈 2단계 구조다(매매 > 경기도 용인시 > 표 …).
# attrs는 거래유형마다 다를 수 있다(예: 월세/전세 표는 '가격'을 '월세 가격'·'전세 가격'으로
# 분리하고 '토허제 해당'이 없다). 시 분류는 config의 배치로만 표현하며, 매물 레코드 자체에는
# 시 필드를 두지 않는다(pull이 헤딩에서 시를 복원하므로 라운드트립이 일관).
# 시 표 노출 순서: 아래 우선순위를 먼저, 나머지는 가나다순(한글 음절은 유니코드 순=가나다순).
CITY_PRIORITY = ["서울시", "경기도 성남시", "경기도 용인시", "경기도 수원시"]


def _city_sort_key(city_name):
    """우선순위에 있으면 (0, 지정순서), 없으면 (1, 시이름)으로 가나다 정렬."""
    if city_name in CITY_PRIORITY:
        return (0, CITY_PRIORITY.index(city_name))
    return (1, city_name)


GROUPS = [
    (
        g["title"],
        g["attrs"],
        sorted(
            [(c["city"], c["names"]) for c in g["cities"]],
            key=lambda cn: _city_sort_key(cn[0]),
        ),
    )
    for g in _config["groups"]
]


def cell_text(name, attr):
    """장점/단점 리스트는 글머리 붙여 줄바꿈 문자열로, 나머지는 그대로 반환.

    정의되지 않은 속성은 빈 문자열로 처리한다('AI 추천'/'검토 여부'처럼 기본값 없는 컬럼).
    """
    v = LISTINGS[name].get(attr, "")
    if isinstance(v, list):
        return "\n".join("· " + x for x in v)
    return v


def validate():
    """config 배치와 listings.json의 구조 무결성 위반을 경고 리스트로 반환한다.

    - config에 배치됐지만 listings.json에 정의가 없는 매물(오타·미작성)
    - 둘 이상의 시 표에 중복 배치된 매물
    - 어느 시 표에도 배치되지 않아 Notion에 안 그려지는 매물

    (과거 '실거주' 25평 기준 검증은 '실거주'+'재건축 투자'가 '매매'로 통합되며
    기준의 근거가 사라져 제거했다. 평수 조건은 매물_관리.md 문서 기준으로만 관리한다.)
    """
    warnings = []
    placed = []
    for _, _, cities in GROUPS:
        for city, names in cities:
            for name in names:
                placed.append(name)
                if name not in LISTINGS:
                    warnings.append("[%s] config에 배치됐으나 listings.json에 정의 없음: %s"
                                    % (city, name))
    for name in sorted({n for n in placed if placed.count(n) > 1}):
        warnings.append("둘 이상의 시 표에 중복 배치됨: %s" % name)
    for name in LISTINGS:
        if name not in placed:
            warnings.append("어느 시 표에도 배치되지 않음(Notion에 안 그려짐): %s" % name)
    return warnings


if __name__ == "__main__":
    _w = validate()
    if _w:
        print("분류 기준 경고:")
        for _m in _w:
            print(" -", _m)
    else:
        print("분류 기준 위반 없음.")
