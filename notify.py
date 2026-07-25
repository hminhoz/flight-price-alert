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


def google_flights_url(route: Route, dep: dt.date, ret: dt.date,
                       adults: int = 1, carriers: list[str] | None = None) -> str:
    """왕복 검색 결과로 랜딩. 항공사 코드를 알면 그 항공사만 남긴다."""
    codes = sorted({c for c in (carriers or []) if c and c != "multi"})
    try:
        from fast_flights import FlightQuery, Passengers, create_query
        q = create_query(
            flights=[
                FlightQuery(date=dep.isoformat(), from_airport=route.origin,
                            to_airport=route.destination, max_stops=0,
                            airlines=codes or None),
                FlightQuery(date=ret.isoformat(), from_airport=route.destination,
                            to_airport=route.origin, max_stops=0,
                            airlines=codes or None),
            ],
            trip="round-trip", passengers=Passengers(adults=adults),
            language="ko", currency="KRW",
        )
        params = {**q.params(), "gl": "KR"}
        return "https://www.google.com/travel/flights?" + urlencode(params)
    except Exception:  # noqa: BLE001 - 링크 실패가 알림을 막으면 안 된다
        q = (f"Flights from {route.origin} to {route.destination} "
             f"on {dep.isoformat()} through {ret.isoformat()} nonstop")
        return (f"https://www.google.com/travel/flights?q={quote_plus(q)}"
                f"&curr=KRW&hl=ko")


def naver_url(route: Route, dep: dt.date, ret: dt.date, adults: int) -> str:
    o, d = route.origin, route.destination
    dep_s, ret_s = dep.strftime("%Y%m%d"), ret.strftime("%Y%m%d")
    kind = "domestic" if route.domestic else "international"
    return (f"https://flight.naver.com/flights/{kind}/"
            f"{o}-{d}-{dep_s}/{d}-{o}-{ret_s}?adult={adults}&fareType=Y")
