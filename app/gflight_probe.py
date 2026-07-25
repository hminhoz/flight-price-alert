"""구글 불가 노선(오사카·후쿠오카·나리타) 우회 탐침 — v2 무료 재도전.

배경:
  v1.10에서 이 세 노선을 "구글 소스 불가"로 판정하고 휴면 처리했다. 그런데
  근거가 부실했다. 3단 폴백이 모두 같은 요청 방식이었고, 실패 원인을 끝까지
  파고들지 않았다.

2026-07-26 재조사로 밝혀진 것:
  1. 파서 소스를 읽어보니 실패 지점은 `errorHasStatus: true`.
     파싱 실패가 아니라 **구글이 그 쿼리에 오류 상태를 반환**하는 것이다.
  2. 로그의 경유편 샘플에 ICN→KIX(06:55 아시아나), ICN→FUK(13:55 아시아나)가
     들어 있다. 즉 **구글에 데이터는 있다.** 직접 검색만 실패한다.
  3. fast-flights 3.0.2의 FlightQuery에 여태 안 쓴 `airlines` 필터가 있다.

가설: 죽은 세 노선은 취항 항공사가 10곳 이상인 초대형 노선이라 응답이 크다.
      잘 되는 노선(나고야·삿포로·가고시마)은 3~4곳뿐이다.
      → 결과가 너무 커서 구글이 오류를 내는 것일 수 있다. 줄이면 통과할지 모른다.

시험 6종 (모두 무료, 요청 12건·1분 내):
  A 기본        현재 방식 그대로 (대조군, 실패 예상)
  B 항공사 지정  결과 크기를 줄인다 ← 본 가설
  C 도시 코드    KIX 대신 OSA
  D 성인 1명     인원수가 변수인지
  E 왕복         편도 대신 왕복 쿼리
  F 문자열 쿼리  tfs 프로토버프 대신 자연어 q= 경로
  G 관대 파서    안 쓰는 필드를 건너뛰고 가격·시각만 추출 ← 2차 본가설
  + 실패 시 원본 HTML 상태를 함께 기록
"""
from __future__ import annotations

import datetime as dt
import logging

log = logging.getLogger(__name__)

# 한국-일본 주요 노선 취항 LCC·FSC (결과 축소용 필터 후보)
_KR_CARRIERS = ["7C", "TW", "LJ", "BX", "ZE", "KE", "OZ", "RS"]


def _ymd(d: dt.date) -> str:
    return d.isoformat()


def _try(name: str, fn) -> bool:
    """시험 1건. 성공하면 최저가를 남기고 True."""
    try:
        res = fn()
    except Exception as e:  # noqa: BLE001
        log.info("GPROBE %-22s 실패: %s", name, str(e)[:110])
        return False
    items = list(res or [])
    if not items:
        log.info("GPROBE %-22s 응답은 왔으나 항목 0개", name)
        return False
    prices = []
    for it in items[:8]:
        p = getattr(it, "price", None)
        legs = getattr(it, "flights", None) or []
        prices.append((len(legs), p))
    log.info("GPROBE %-22s ✅ 성공 · 항목 %d개 · (구간수,가격) %s",
             name, len(items), prices[:6])
    return True


def _probe_route(origin: str, dest: str, city: str | None, dep: dt.date,
                 ret: dt.date, adults: int, currency: str) -> None:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    from fast_flights import fetch_flights_html

    log.info("--- %s-%s 우회 시험 (%s) ---", origin, dest, dep)

    def q(**kw):
        fq = kw.pop("fq", None) or [FlightQuery(date=_ymd(dep), from_airport=origin,
                                                to_airport=dest, max_stops=0)]
        return create_query(flights=fq, trip=kw.pop("trip", "one-way"),
                            passengers=Passengers(adults=kw.pop("adults", adults)),
                            language="en-US", currency=currency, **kw)

    # A. 대조군
    ok_a = _try(f"A.기본 {origin}-{dest}", lambda: get_flights(q()))

    # 실패했다면 구글이 실제로 뭘 줬는지 본다
    if not ok_a:
        try:
            html = fetch_flights_html(q())
            has_err = "errorHasStatus" in html
            log.info("GPROBE A.원본HTML         %d bytes · errorHasStatus=%s · "
                     "script.ds:1 존재=%s", len(html), has_err,
                     "script" in html and "ds:1" in html)
        except Exception as e:  # noqa: BLE001
            log.info("GPROBE A.원본HTML         실패: %s", str(e)[:110])

    # B. 항공사 지정 (본 가설)
    _try(f"B.항공사지정 {dest}", lambda: get_flights(q(fq=[
        FlightQuery(date=_ymd(dep), from_airport=origin, to_airport=dest,
                    max_stops=0, airlines=_KR_CARRIERS)])))

    # C. 도시 코드
    if city:
        _try(f"C.도시코드 {city}", lambda: get_flights(q(fq=[
            FlightQuery(date=_ymd(dep), from_airport=origin, to_airport=city,
                        max_stops=0)])))

    # D. 성인 1명
    _try(f"D.성인1명 {dest}", lambda: get_flights(q(adults=1)))

    # E. 왕복
    _try(f"E.왕복 {dest}", lambda: get_flights(q(trip="round-trip", fq=[
        FlightQuery(date=_ymd(dep), from_airport=origin, to_airport=dest, max_stops=0),
        FlightQuery(date=_ymd(ret), from_airport=dest, to_airport=origin, max_stops=0)])))

    # F. 문자열(자연어) 쿼리 — tfs 프로토버프를 우회하는 다른 경로
    _try(f"F.문자열 {dest}",
         lambda: get_flights(f"Flights from {origin} to {dest} on {_ymd(dep)}"))

    # G. 관대 파서 (v1.20 본가설) — 기본 파서가 IndexError로 버리는 응답을 직접 판다
    from .gparse import parse_tolerant
    for label, query in ((f"G.관대 {dest}", q()),
                         (f"G2.관대+항공사 {dest}", q(fq=[
                             FlightQuery(date=_ymd(dep), from_airport=origin,
                                         to_airport=dest, max_stops=0,
                                         airlines=_KR_CARRIERS)]))):
        try:
            html = fetch_flights_html(query)
            items = parse_tolerant(html)
        except Exception as e:  # noqa: BLE001
            log.info("GPROBE %-22s 실패: %s", label, str(e)[:110])
            continue
        if not items:
            log.info("GPROBE %-22s 추출 0건 (구글이 실제 오류 응답)", label)
            continue
        direct = [i for i in items if len(i.flights) == 1]
        log.info("GPROBE %-22s ✅ 성공 · 총 %d개(직항 %d개) · 직항 최저 %s",
                 label, len(items), len(direct),
                 min((i.price for i in direct), default="-"))
        for i in direct[:3]:
            s = i.flights[0]
            log.info("GPROBE %-22s    %s %s→%s %s %s원", label, i.airlines,
                     s.from_airport.code, s.to_airport.code,
                     s.departure.time, f"{i.price:,}")


def run(adults: int = 2, currency: str = "KRW", today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    dep = today + dt.timedelta(days=30)
    ret = dep + dt.timedelta(days=3)
    log.info("=== 구글 불가 노선 우회 탐침 (%s) ===", dep)
    for origin, dest, city in (("ICN", "KIX", "OSA"), ("ICN", "FUK", None)):
        try:
            _probe_route(origin, dest, city, dep, ret, adults, currency)
        except Exception as e:  # noqa: BLE001
            log.info("GPROBE %s-%s 전체 실패: %s", origin, dest, str(e)[:150])
    log.info("판정: B가 성공하면 항공사 필터로 휴면 노선 부활 가능 · "
             "C·E·F 중 성공이 있으면 그 경로로 우회 · "
             "전부 실패면 원본HTML 줄의 errorHasStatus 값으로 원인 재판단")
    log.info("=== 구글 우회 탐침 종료 ===")
