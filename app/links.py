"""알림용 딥링크 생성.

목표: 링크를 눌렀을 때 사용자가 알림에 적힌 그 항공편을 바로 찾을 수 있어야 한다.

Google Flights의 tfs 파라미터에는 출발 시각 필터를 넣을 수 없다. 대신
**날짜 + 직항만 + 해당 항공사 + 인원수**까지 걸어두면 결과가 보통 한두 편으로
좁혀져서 사실상 그 편이 바로 보인다. 항공사 코드는 검색 때 수집한
IATA 코드(`carrier`)를 쓴다. 코드를 모르면 필터 없이 링크만 만든다.

네이버항공권은 공개된 필터 파라미터가 없어 날짜·인원까지만 건다.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote_plus, urlencode

from .settings import Route


def google_oneway_url(origin: str, dest: str, day: dt.date, adults: int,
                      carrier: str = "", window=None) -> str:
    """편도 검색 링크. 출발 시각 조건을 심는다 (v2.22).

    편도 2장이 더 쌀 때 왕복 링크를 주면 그 가격이 화면에 없다.
    실제로 사야 하는 방식과 링크를 맞춘다.
    """
    from . import tfs as TFS
    leg = TFS.flight_data(
        day.isoformat(), origin, dest, max_stops=0,
        dep_window=(window[0].hour, window[1].hour) if window else None)
    return TFS.url(TFS.build_tfs([leg], adults=adults, trip=2))


def google_roundtrip_url(route: Route, dep: dt.date, ret: dt.date, adults: int,
                         out_window=None, ret_window=None) -> str:
    """왕복 검색 링크 + 양쪽 출발 시각 조건 (v2.22).

    알림이 "이 가격이 싸다"고 말했으면 눌렀을 때 그 가격이 보여야 한다.
    """
    from . import tfs as TFS
    legs = [
        TFS.flight_data(dep.isoformat(), route.origin, route.destination,
                        max_stops=0,
                        dep_window=(out_window[0].hour, out_window[1].hour)
                        if out_window else None),
        TFS.flight_data(ret.isoformat(), route.destination, route.origin,
                        max_stops=0,
                        dep_window=(ret_window[0].hour, ret_window[1].hour)
                        if ret_window else None),
    ]
    return TFS.url(TFS.build_tfs(legs, adults=adults, trip=1))


def google_flights_url(route: Route, dep: dt.date, ret: dt.date,
                       adults: int = 1, carriers: list[str] | None = None,
                       back: Route | None = None, short: bool = False) -> str:
    """왕복(또는 교차 시 다구간) 검색 결과로 랜딩.

    back을 주면 오는 편 노선이 다른 교차 조합으로 보고 multi-city 쿼리를 만든다
    (예: 김포→나고야 / 나고야→인천). 항공사 코드를 알면 그 항공사만 남긴다.
    """
    codes = sorted({c for c in (carriers or []) if c and c != "multi"})
    cross = back is not None and back.key != route.key
    ret_from = (back or route).destination
    ret_to = (back or route).origin
    if short:
        # tfs 프로토버프는 169자라 여러 개 걸면 텔레그램 4096자를 넘긴다.
        # 자연어 질의는 119자 — 항공사 필터는 못 걸지만 노선·날짜·직항은
        # 그대로 전달된다. 고정판처럼 링크를 많이 넣는 곳에서 쓴다 (v1.96).
        q = (f"Flights from {route.origin} to {route.destination} "
             f"on {dep.isoformat()} through {ret.isoformat()} nonstop")
        return (f"https://www.google.com/travel/flights?q={quote_plus(q)}"
                f"&curr=KRW&hl=ko")
    try:
        from fast_flights import FlightQuery, Passengers, create_query
        q = create_query(
            flights=[
                FlightQuery(date=dep.isoformat(), from_airport=route.origin,
                            to_airport=route.destination, max_stops=0,
                            airlines=codes or None),
                FlightQuery(date=ret.isoformat(), from_airport=ret_from,
                            to_airport=ret_to, max_stops=0,
                            airlines=codes or None),
            ],
            trip="multi-city" if cross else "round-trip",
            passengers=Passengers(adults=adults),
            language="ko", currency="KRW",
        )
        params = {**q.params(), "gl": "KR"}
        return "https://www.google.com/travel/flights?" + urlencode(params)
    except Exception:  # noqa: BLE001 - 링크 실패가 알림을 막으면 안 된다
        q = (f"Flights from {route.origin} to {route.destination} "
             f"on {dep.isoformat()}, then {ret_from} to {ret_to} "
             f"on {ret.isoformat()} nonstop")
        return (f"https://www.google.com/travel/flights?q={quote_plus(q)}"
                f"&curr=KRW&hl=ko")


def naver_leg_url(route: Route, dep: dt.date, ret: dt.date, adults: int,
                  leg: str) -> str:
    """특정 편(out/ret)을 **앞 구간에 둔** 네이버 왕복 검색.

    네이버는 편도 페이지 URL이 실측에서 항상 0행이었다(v1.84) — 보낼 수 없다.
    대신 왕복 검색은 앞 구간부터 보여주므로, 오는 편을 사야 하면 오는 구간을
    앞에 둔다. 이게 없으면 '네이버' 글자가 항상 가는 편부터 보여줘서
    "둘 다 눌러도 가는 편만 나온다"가 된다 (v2.42).
    """
    o, d = route.origin, route.destination
    dep_s, ret_s = dep.strftime("%Y%m%d"), ret.strftime("%Y%m%d")
    kind = "domestic" if route.domestic else "international"
    if leg == "ret":
        first, second = f"{d}-{o}-{ret_s}", f"{o}-{d}-{dep_s}"
    else:
        first, second = f"{o}-{d}-{dep_s}", f"{d}-{o}-{ret_s}"
    return (f"https://flight.naver.com/flights/{kind}/"
            f"{first}/{second}?adult={adults}&fareType=Y")


def naver_url(route: Route, dep: dt.date, ret: dt.date, adults: int,
              back: Route | None = None) -> str:
    # 네이버는 다구간 URL 규격이 공개돼 있지 않아, 교차 조합이면 가는 편 기준
    # 왕복 검색으로 보낸다 (참고용).
    o, d = route.origin, route.destination
    dep_s, ret_s = dep.strftime("%Y%m%d"), ret.strftime("%Y%m%d")
    kind = "domestic" if route.domestic else "international"
    return (f"https://flight.naver.com/flights/{kind}/"
            f"{o}-{d}-{dep_s}/{d}-{o}-{ret_s}?adult={adults}&fareType=Y")
