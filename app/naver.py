"""네이버 항공권 결과행 파서.

탐침(2026-07-26, 10차)으로 확보한 실물 문자열을 기준으로 만들었다.
화면 DOM에서 `[class*=domestic_inner]` / `[class*=combination_inner]` 의
innerText를 ' | ' 로 이어 붙인 형태를 받는다.

국내선 (편도 단위, 1인)
    파라타항공 | 06:00GMP | 07:15CJU | 01시간 15분 | 특가석편도 51,200원~

국제선 (왕복 조합 단위, 1인)
    제주항공 | 07:10ICN | 09:05KIX | 직항, 01시간 55분
           | 09:00KIX | 11:00ICN | 직항, 02시간 00분
           | 성인/하나카드(이용실적 충족시) | 왕복 | 192,300 | 원~ | 로그인 후 특가확인

**실적 조건이 붙은 가격은 버린다** (2026-07-26 사용자 결정).
카드로 결제만 하면 되는 건 괜찮지만, 이용실적을 채워야 하는 값은
대부분의 경우 실제로 살 수 없는 가격이라 알림에 띄우면 안 된다.
"""
from __future__ import annotations

import datetime as dt
import re

# 06:00GMP / 07:15CJU 처럼 시각과 공항이 붙어 나온다
_LEG = re.compile(r"(\d{1,2}):(\d{2})\s*([A-Z]{3})")
_PRICE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+)\s*원")
_PRICE_LOOSE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+)")

# 실적을 채워야 하는 조건 (제외 대상)
_SPEND_COND = ("이용실적", "실적 충족", "실적충족", "전월실적", "실적")
# 좌석 등급
_SEAT = re.compile(r"(특가석|할인석|일반석|비즈니스|프리미엄)")


def has_spend_condition(text: str) -> bool:
    """이용실적을 채워야 하는 조건부 가격인가."""
    return any(k in (text or "") for k in _SPEND_COND)


def _to_time(h: str, m: str) -> dt.time | None:
    try:
        return dt.time(int(h), int(m))
    except ValueError:
        return None


# 목록 전체를 감싼 컨테이너가 같이 잡힐 때가 있다(달력·헤더 포함).
# 그런 덩어리를 한 편으로 오인하면 항공사명이 "가는 편 선택"이 된다.
_JUNK = ("가는 편 선택", "오는 편 선택", "출발시각 빠른 순", "가격 낮은 순",
         "트래블클럽", "왕복 항공편 선택")


def _is_junk(text: str, legs) -> bool:
    return any(k in text for k in _JUNK) or len(legs) > 6


def parse_domestic(text: str) -> dict | None:
    """국내선 편도 한 줄. 실패하면 None."""
    if not text or has_spend_condition(text):
        return None
    legs = _LEG.findall(text)
    if len(legs) < 2 or _is_junk(text, legs):
        return None
    price_m = _PRICE.search(text) or _PRICE_LOOSE.search(text)
    if not price_m:
        return None
    dep_h, dep_m, dep_ap = legs[0]
    arr_h, arr_m, arr_ap = legs[1]
    seat = _SEAT.search(text)
    return {
        "kind": "oneway",
        "airline": text.split("|")[0].strip(),
        "from": dep_ap, "to": arr_ap,
        "dep": _to_time(dep_h, dep_m), "arr": _to_time(arr_h, arr_m),
        "seat": seat.group(1) if seat else "",
        "price": int(price_m.group(1).replace(",", "")),   # 1인 편도
        "raw": text[:300],
    }


def parse_intl(text: str) -> dict | None:
    """국제선 왕복 조합 한 줄. 실패하거나 실적 조건이면 None."""
    if not text or has_spend_condition(text):
        return None
    legs = _LEG.findall(text)
    if len(legs) < 4 or _is_junk(text, legs):   # 가는 편 2개 + 오는 편 2개
        return None
    price_m = _PRICE.search(text) or _PRICE_LOOSE.search(text)
    if not price_m:
        return None
    o_dep, o_arr, r_dep, r_arr = legs[:4]
    return {
        "kind": "roundtrip",
        "airline": text.split("|")[0].strip(),
        "out_from": o_dep[2], "out_to": o_arr[2],
        "out_dep": _to_time(o_dep[0], o_dep[1]),
        "ret_from": r_dep[2], "ret_to": r_arr[2],
        "ret_dep": _to_time(r_dep[0], r_dep[1]),
        "direct": text.count("직항") >= 2,
        "price": int(price_m.group(1).replace(",", "")),   # 1인 왕복
        "raw": text[:300],
    }


def pick_best(rows: list, *, domestic: bool,
              out_window: tuple | None = None,
              ret_window: tuple | None = None,
              direct_only: bool = True) -> dict | None:
    """조건에 맞는 것 중 최저가 하나.

    국내선은 편도라 오는 편 조건을 걸 수 없다(호출부가 따로 조합한다).
    국제선은 한 줄에 양방향이 들어 있어 두 창을 모두 적용한다.
    """
    best = None
    for text in rows or []:
        r = parse_domestic(text) if domestic else parse_intl(text)
        if not r:
            continue
        if domestic:
            t = r["dep"]
            if out_window and (t is None or not (out_window[0] <= t <= out_window[1])):
                continue
        else:
            if direct_only and not r["direct"]:
                continue
            o, rt = r["out_dep"], r["ret_dep"]
            if out_window and (o is None or not (out_window[0] <= o <= out_window[1])):
                continue
            if ret_window and (rt is None or not (ret_window[0] <= rt <= ret_window[1])):
                continue
        if best is None or r["price"] < best["price"]:
            best = r
    return best
