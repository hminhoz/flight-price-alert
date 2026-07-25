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
    carrier: str = ""       # 항공사 IATA 코드 (7C, OZ, KE...). 알림 링크 필터용


_AMPM = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.IGNORECASE)


def parse_time(raw) -> dt.time | None:
    """구글 원본 [시, 분] 배열, 정수(시), '6:30 AM'/'18:05' 문자열을 모두 24h time으로.

    fast-flights는 구글 내부 payload의 시각을 가공 없이 넘긴다: [6, 30] 또는
    분이 0이면 [6] 형태 (v1.7에서 소스 확인). 문자열 케이스도 방어적으로 유지.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        if not raw:
            return None
        try:
            h = int(raw[0]) if raw[0] is not None else 0  # 구글은 0시를 null로 보냄
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
    _soft = (FlightsNotFound, IndexError, TypeError)  # 구글이 오류/이형 페이지를 준 경우

    last_err: Exception | None = None
    soft_fails = 0
    stops = 0 if direct_only else None
    for attempt in range(retries):
        try:
            used_fallback = False
            try:
                results = _do_fetch(origin, dest, date, adults, currency, max_stops=stops)
            except _soft:
                # (v1.8) 직항 필터 쿼리 실패 → 무필터 재조회, 직항 선별은 우리가 직접
                try:
                    used_fallback = True
                    results = _do_fetch(origin, dest, date, adults, currency, max_stops=None)
                except _soft:
                    # (v1.9) 무필터마저 실패 → 한국 마켓(gl=KR) 파라미터로 재시도.
                    # 실행 서버가 미국이라 구글이 미국 시장 응답을 주는 것이
                    # 대형 노선 오류의 원인일 수 있음 (2026-07-25 진단)
                    used_fallback = False
                    if (origin, dest) not in _fallback_logged:
                        _fallback_logged.add((origin, dest))
                        log.info("KRFALLBACK %s-%s: 무필터도 실패 → 한국 마켓 재시도",
                                 origin, dest)
                    results = _do_fetch(origin, dest, date, adults, currency,
                                        max_stops=stops, korea_market=True)
            best = _pick_best(results, window, direct_only, date, origin, dest)
            if best is not None:
                return best
            if not used_fallback:
                return None  # 필터 쿼리가 정상 응답 + 조건 맞는 편 없음 → 신뢰
            # 폴백 응답이 경유편 위주(직항 누락)였을 수 있음(v1.9 진단:
            # ICN-HND 등에서 폴백이 경유만 반환) → 직항 쿼리에 기회를 더 준다
            time.sleep(1.5 + random.uniform(0, 1.5))
        except _soft:
            soft_fails += 1
            if soft_fails >= 2:
                raise NoFlightData(f"{origin}-{dest} {date}")
            time.sleep(2 + random.uniform(0, 2))
        except Exception as e:  # noqa: BLE001 - 비공식 라이브러리, 광범위 방어
            last_err = e
            wait = (2 ** attempt) + random.uniform(0, 1)
            log.warning("search fail %s-%s %s (try %d/%d): %s",
                        origin, dest, date, attempt + 1, retries, e)
            time.sleep(wait)
    if last_err is not None:
        raise SearchError(f"{origin}-{dest} {date}: {last_err}") from last_err
    if soft_fails:
        raise NoFlightData(f"{origin}-{dest} {date}")
    return None  # 폴백 경유편만 반복 → 조건불일치로 기록


_fallback_logged: set[tuple[str, str]] = set()


def _do_fetch(origin, dest, date, adults, currency, max_stops, korea_market=False):
    """편도 조회 1회. 테스트에서 이 함수를 바꿔치기해 시나리오를 주입한다."""
    from fast_flights import FlightQuery, Passengers, create_query
    q = create_query(
        flights=[FlightQuery(date=date, from_airport=origin, to_airport=dest,
                             max_stops=max_stops)],
        trip="one-way",
        passengers=Passengers(adults=adults),
        language="ko" if korea_market else "en-US",
        currency=currency,
    )
    return _run_query(q, korea_market)


def _run_query(q, korea_market=False):
    """쿼리 1건 실행. 기본 파서가 죽으면 관대 파서로 한 번 더 시도한다.

    korea_market=True면 gl=KR 파라미터를 붙여 한국 시장 기준 응답을 요청한다
    (시각·가격은 숫자 payload라 언어와 무관하게 파싱됨).

    관대 파서 폴백 (v1.20): fast-flights 기본 파서는 안 쓰는 메타데이터를
    고정 인덱스로 읽다가 취항사 많은 노선에서 IndexError로 죽는다. 그때
    app/gparse.py 가 가격·시각만 다시 뽑는다. 구글이 진짜 오류를 준
    경우에는 관대 파서도 None을 주므로 기존 재시도 흐름이 그대로 살아 있다.
    """
    if not korea_market:
        from fast_flights import get_flights
        try:
            return get_flights(q)
        except Exception as e:  # noqa: BLE001
            return _tolerant_retry(q, None, e)

    from primp import Client
    from fast_flights.parser import parse
    client = Client(impersonate="chrome_145", impersonate_os="macos",
                    referer=True, cookie_store=True)
    params = {**q.params(), "gl": "KR"}
    res = client.get("https://www.google.com/travel/flights", params=params)
    try:
        return parse(res.text)
    except Exception as e:  # noqa: BLE001
        return _tolerant_retry(q, res.text, e)


_tolerant_hits: set[str] = set()


def _tolerant_retry(q, html, original_exc):
    """기본 파서 실패분을 관대 파서로 재시도. 실패하면 원래 예외를 다시 던진다."""
    from .gparse import parse_tolerant
    try:
        if html is None:
            from fast_flights import fetch_flights_html
            html = fetch_flights_html(q)
        items = parse_tolerant(html)
    except Exception:  # noqa: BLE001
        raise original_exc from None
    if not items:
        raise original_exc from None
    key = type(original_exc).__name__
    if key not in _tolerant_hits:
        _tolerant_hits.add(key)
        log.info("TOLERANT 기본 파서 실패(%s: %s)를 관대 파서가 복구 · 항목 %d개",
                 key, str(original_exc)[:60], len(items))
    return items


def _do_fetch_rt(origin, dest, dep_date, ret_date, adults, currency,
                 max_stops, korea_market=False):
    """왕복 조회 1회. 반환 항목의 price는 왕복 총액이다."""
    from fast_flights import FlightQuery, Passengers, create_query
    q = create_query(
        flights=[
            FlightQuery(date=dep_date, from_airport=origin, to_airport=dest,
                        max_stops=max_stops),
            FlightQuery(date=ret_date, from_airport=dest, to_airport=origin,
                        max_stops=max_stops),
        ],
        trip="round-trip",
        passengers=Passengers(adults=adults),
        language="ko" if korea_market else "en-US",
        currency=currency,
    )
    return _run_query(q, korea_market)


def search_roundtrip(
    origin: str,
    dest: str,
    dep_date: str,
    ret_date: str,
    *,
    adults: int,
    out_window: tuple[dt.time, dt.time] | None = None,
    currency: str = "KRW",
    direct_only: bool = True,
    diag: bool = False,
) -> int | None:
    """해당 날짜쌍의 실제 왕복 총액 최저가. 실패하면 None (예외 안 던짐).

    왜 필요한가 (v1.12):
      편도 합산 방식은 '오는 편'을 일본 시장 편도 요금으로 잡는다. 구글이 이를
      환산해 주는데, 실측상 한국 발권 왕복 총액보다 크게 비싸다(노선별 1.5~2배).
      따라서 편도 합산가는 '변동 감지 신호'로는 쓸 수 있어도 알림에 표시할
      금액으로는 부적절하다. 알림이 확정된 조합만 왕복으로 재조회해 실구매가에
      가까운 금액을 얻는다. 실행당 몇 건뿐이라 부하 영향은 없다.

    한계: 왕복 쿼리는 '가는 편' 목록만 반환하므로 오는 편 출발 시각은 검증할 수
    없다. 시간 조건 충족 여부는 편도 검색 결과가 이미 보장하고, 이 값은 금액
    참조용이다.
    """
    stops = 0 if direct_only else None
    for korea_market in (False, True):
        try:
            results = _do_fetch_rt(origin, dest, dep_date, ret_date,
                                   adults, currency, stops, korea_market)
        except Exception as e:  # noqa: BLE001 - 검증 실패는 알림을 막지 않는다
            log.info("RTVERIFY %s-%s %s/%s 실패(korea=%s): %s",
                     origin, dest, dep_date, ret_date, korea_market, str(e)[:120])
            continue
        if diag:
            # 왕복 응답의 항목 구조를 확인한다. 왕복은 항목 하나에 가는 편+오는 편이
            # 함께 담길 수 있어(legs=2), 편도용 직항 판정(legs==1)이 오작동할 소지가
            # 있다. 2026-07-25 왕복가가 편도합산보다 비싸게 나온 원인 후보.
            shapes = [(len(getattr(i, "flights", None) or []),
                       parse_price(getattr(i, "price", None)))
                      for i in (results or [])[:8]]
            log.info("RTDIAG %s-%s %s: 항목 %d개, (구간수,가격) %s",
                     origin, dest, dep_date, len(results or []), shapes)
            if results:
                log.info("RTDIAG 샘플=%s", repr(results[0])[:500])
        best = None
        for item in results or []:
            try:
                legs = getattr(item, "flights", None) or []
                # 왕복 응답은 legs가 1(가는 편만) 또는 2(가는 편+오는 편)일 수 있다.
                # 각 방향이 직항이면 되므로 2까지 허용한다.
                if direct_only and len(legs) not in (1, 2):
                    continue
                price = parse_price(getattr(item, "price", None))
                if not price or price <= 0:
                    continue
                if out_window is not None:
                    t = parse_time(getattr(getattr(legs[0], "departure", None), "time", None))
                    if t is None or not (out_window[0] <= t <= out_window[1]):
                        continue
                if best is None or price < best:
                    best = price
            except Exception:
                continue
        if best is not None:
            return best
    return None


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
            # item.type 이 IATA 코드다 (경유편은 'multi'). 링크 필터에 쓴다.
            code = getattr(item, "type", "") or ""
            cand = LegResult(
                price=price, airline=str(name),
                carrier="" if code == "multi" else str(code),
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
