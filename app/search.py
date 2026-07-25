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


def parse_time(raw) -> dt.time | None:
    """구글 원본 [시, 분] 배열, 정수(시), '6:30 AM'/'18:05' 문자열을 모두 24h time으로.

    fast-flights는 구글 내부 payload의 시각을 가공 없이 넘긴다: [6, 30] 또는
    분이 0이면 [6] 형태 (v1.7에서 소스 확인). 문자열 케이스도 방어적으로 유지.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        if not raw or raw[0] is None:
            return None
        try:
            h = int(raw[0])
            m = int(raw[1]) if len(raw) > 1 and raw[1] is not None else 0
        except (TypeError, ValueError):
            return None
        return dt.time(h, m) if (0 <= h <= 23 and 0 <= m <= 59) else None
    if isinstance(raw, (int, float)):
        h = int(raw)
        return dt.time(h, 0) if 0 <= h <= 23 else None
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


_diag_logged: set[tuple[str, str]] = set()


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
    from fast_flights import FlightsNotFound

    last_err: Exception | None = None
    nodata_hits = 0
    for attempt in range(retries):
        try:
            try:
                results = _do_fetch(origin, dest, date, adults, currency,
                                    max_stops=0 if direct_only else None)
            except (FlightsNotFound, IndexError, TypeError):
                if not direct_only:
                    raise
                # 대형 노선에서 직항 필터 쿼리가 구글 오류 응답/파서 붕괴를 유발하는
                # 사례 관찰(KIX·FUK·NRT 전량 no-data, 2026-07-25) → 무필터로 재조회하고
                # 직항 선별은 _pick_best에서 직접 수행 (v1.8)
                if (origin, dest) not in _fallback_logged:
                    _fallback_logged.add((origin, dest))
                    log.info("FALLBACK %s-%s: 직항필터 쿼리 실패 → 무필터 재조회", origin, dest)
                results = _do_fetch(origin, dest, date, adults, currency, max_stops=None)
            return _pick_best(results, window, direct_only, date, origin, dest)
        except FlightsNotFound:
            raise NoFlightData(f"{origin}-{dest} {date}")
        except (IndexError, TypeError):
            # HTTP 200이지만 파서가 못 넘기는 페이지 구조. 일시적일 수 있어 1회 재시도,
            # 반복되면 no-data.
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


_fallback_logged: set[tuple[str, str]] = set()


def _do_fetch(origin, dest, date, adults, currency, max_stops):
    """실제 조회 1회. 테스트에서 이 함수를 바꿔치기해 시나리오를 주입한다."""
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    q = create_query(
        flights=[FlightQuery(date=date, from_airport=origin, to_airport=dest,
                             max_stops=max_stops)],
        trip="one-way",
        passengers=Passengers(adults=adults),
        language="en-US",   # 시간 표기 파싱 안정성을 위해 고정
        currency=currency,
    )
    return get_flights(q)


class SearchError(RuntimeError):
    pass


class NoFlightData(RuntimeError):
    """검색은 됐으나 해당 날짜/노선에 파싱 가능한 가격 데이터가 없음."""


def _pick_best(results, window, direct_only, date, origin="?", dest="?") -> LegResult | None:
    lo, hi = window
    best: LegResult | None = None
    n_items = n_direct = n_price = n_time = 0
    for item in results or []:
        try:
            n_items += 1
            legs = getattr(item, "flights", None) or []
            if direct_only and len(legs) != 1:
                n_direct += 1
                continue
            price = parse_price(getattr(item, "price", None))
            if not price or price <= 0:
                n_price += 1
                continue
            first = legs[0]
            dep = getattr(getattr(first, "departure", None), "time", None)
            arr = getattr(getattr(first, "arrival", None), "time", None)
            t = parse_time(dep)
            if t is None or not (lo <= t <= hi):
                n_time += 1
                continue
            airlines = getattr(item, "airlines", None) or []
            a0 = airlines[0] if airlines else None
            name = getattr(a0, "name", None) or (str(a0) if a0 is not None else "?")
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
    # 진단: 결과는 있는데 조건 통과가 0건이면, 왜 탈락했는지 + 원본 샘플을 노선당 1회 기록
    if best is None and n_items and (origin, dest) not in _diag_logged:
        _diag_logged.add((origin, dest))
        sample = repr(results[0])[:400]
        log.info("DIAG %s-%s %s: 항목 %d개 전원 탈락 (직항아님 %d · 가격파싱 %d · 시간창밖 %d, "
                 "시간창 %s~%s) 샘플=%s",
                 origin, dest, date, n_items, n_direct, n_price, n_time,
                 lo.strftime("%H:%M"), hi.strftime("%H:%M"), sample)
    return best


def polite_delay(rng: tuple[float, float]) -> None:
    time.sleep(random.uniform(*rng))
