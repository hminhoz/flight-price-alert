"""Google Flights(fast-flights) 편도 검색 래퍼.

설계 메모:
- 왕복 검색은 '가는 편' 목록만 반환하므로 오는 편 시간 조건을 걸 수 없다.
  → 방향별 편도 검색 후 합산하는 구조 (SPEC §6 참고).
- fast-flights 3.x 는 currency=KRW, FlightQuery(max_stops=0) 를 지원한다.
- 비공식 스크래핑이므로 모든 파싱은 방어적으로: 실패한 항목은 버리고 진행.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class LegResult:
    price: int              # 지정 통화 총액 (성인 N명)
    airline: str
    dep_time: str           # "06:30" (24h)
    arr_time: str
    date: str               # YYYY-MM-DD


_AMPM = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.IGNORECASE)


def parse_time(raw: str) -> dt.time | None:
    """'6:30 AM' / '18:05' / '6:30\u202fPM' 등 다양한 표기를 24h time으로."""
    if raw is None:
        return None
    m = _AMPM.search(str(raw).replace("\u202f", " ").replace("\u00a0", " "))
    if not m:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ampm == "PM" and h != 12:
        h += 12
    if ampm == "AM" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return dt.time(h, mi)


def parse_price(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else None


def search_leg(
    origin: str,
    dest: str,
    date: str,
    *,
    adults: int,
    window: tuple[dt.time, dt.time],
    currency: str = "KRW",
    direct_only: bool = True,
    retries: int = 3,
) -> LegResult | None:
    """해당 날짜·방향의 조건 만족 최저가 1건 반환. 조건 만족 편이 없으면 None.

    반환 None 과 예외를 구분한다:
      - None: 검색은 성공했으나 조건(시간대/직항) 만족 항공편 없음
      - raises SearchError: 검색 자체가 실패 (재시도 소진)
    """
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    q = create_query(
        flights=[FlightQuery(
            date=date, from_airport=origin, to_airport=dest,
            max_stops=0 if direct_only else None,
        )],
        trip="one-way",
        passengers=Passengers(adults=adults),
        language="en-US",   # 시간 표기 파싱 안정성을 위해 고정
        currency=currency,
    )

    last_err: Exception | None = None
    nodata_hits = 0
    for attempt in range(retries):
        try:
            results = get_flights(q)
            return _pick_best(results, window, direct_only, date)
        except IndexError:
            # HTTP 200이지만 파서가 못 넘기는 페이지. 일시적일 수 있어 1회만 재시도,
            # 반복되면 no-data (국내선 등 구글에 가격이 없는 노선에서 빈발).
            nodata_hits += 1
            if nodata_hits >= 2:
                raise NoFlightData(f"{origin}-{dest} {date}")
            time.sleep(2 + random.uniform(0, 2))
        except Exception as e:  # noqa: BLE001 - 비공식 라이브러리, 광범위 방어
            last_err = e
            wait = (2 ** attempt) + random.uniform(0, 1)
            log.warning("search fail %s-%s %s (try %d/%d): %s",
                        origin, dest, date, attempt + 1, retries, e)
            time.sleep(wait)
    if nodata_hits and last_err is None:
        raise NoFlightData(f"{origin}-{dest} {date}")
    raise SearchError(f"{origin}-{dest} {date}: {last_err}") from last_err


class SearchError(RuntimeError):
    pass


class NoFlightData(RuntimeError):
    """검색은 됐으나 해당 날짜/노선에 파싱 가능한 가격 데이터가 없음."""


def _pick_best(results, window, direct_only, date) -> LegResult | None:
    lo, hi = window
    best: LegResult | None = None
    for item in results or []:
        try:
            legs = getattr(item, "flights", None) or []
            if direct_only and len(legs) != 1:
                continue
            price = parse_price(getattr(item, "price", None))
            if not price or price <= 0:
                continue
            first = legs[0]
            dep = getattr(getattr(first, "departure", None), "time", None)
            arr = getattr(getattr(first, "arrival", None), "time", None)
            t = parse_time(dep)
            if t is None or not (lo <= t <= hi):
                continue
            airlines = getattr(item, "airlines", None) or []
            name = getattr(airlines[0], "name", "?") if airlines else "?"
            cand = LegResult(
                price=price, airline=str(name),
                dep_time=t.strftime("%H:%M"),
                arr_time=(parse_time(arr).strftime("%H:%M") if parse_time(arr) else "?"),
                date=date,
            )
            if best is None or cand.price < best.price:
                best = cand
        except Exception:  # 항목 하나 파싱 실패는 무시하고 계속
            continue
    return best


def polite_delay(rng: tuple[float, float]) -> None:
    time.sleep(random.uniform(*rng))
