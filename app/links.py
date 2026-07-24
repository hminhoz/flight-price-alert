"""알림용 딥링크 생성.

- Google Flights: q= 자연어 쿼리 방식이 가장 안정적으로 검색 결과에 랜딩한다.
- 네이버항공권: 국제선/국내선 URL 패턴이 다르다. 비공식 패턴이므로
  네이버 개편 시 이 파일만 수정하면 된다.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote_plus

from .settings import Route


def google_flights_url(route: Route, dep: dt.date, ret: dt.date) -> str:
    q = (f"Flights from {route.origin} to {route.destination} "
         f"on {dep.isoformat()} through {ret.isoformat()} nonstop")
    return f"https://www.google.com/travel/flights?q={quote_plus(q)}&curr=KRW&hl=ko"


def naver_url(route: Route, dep: dt.date, ret: dt.date, adults: int) -> str:
    o, d = route.origin, route.destination
    dep_s, ret_s = dep.strftime("%Y%m%d"), ret.strftime("%Y%m%d")
    kind = "domestic" if route.domestic else "international"
    return (f"https://flight.naver.com/flights/{kind}/"
            f"{o}-{d}-{dep_s}/{d}-{o}-{ret_s}?adult={adults}&fareType=Y")
