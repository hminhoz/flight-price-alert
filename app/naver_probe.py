"""네이버 항공권 API 탐침 — v2 준비용 일회성 진단 코드.

왜 탐침부터인가:
  개발 환경에서 네이버 서버에 접속할 수 없어 실제 응답 구조를 확인할 수 없다.
  공개 자료에 두 가지 상충하는 구조가 있어(구형 GraphQL / 신형 SSE REST) 어느
  쪽이 현재 동작하는지 확정해야 수집 모듈을 제대로 짤 수 있다.
  → GitHub Actions(열린 인터넷)에서 후보를 두드려 응답을 로그로 남긴다.

확인이 끝나 app/naver.py 를 만들고 나면 이 파일은 삭제할 것.

후보:
  A. REST + SSE  POST https://flight-api.naver.com/flight/international/searchFlights
  B. REST + SSE  POST https://flight-api.naver.com/flight/domestic/searchFlights
  C. GraphQL     POST https://airline-api.naver.com/graphql (getInternationalList)
  D. GraphQL     POST https://airline-api.naver.com/graphql (getDomesticList)
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import requests

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
_TIMEOUT = 25


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _rest_payload(origin: str, dest: str, dep: dt.date, ret: dt.date,
                  adults: int) -> dict:
    return {
        "adultCount": adults,
        "childCount": 0,
        "infantCount": 0,
        "device": "pc",
        "isNonstop": True,
        "itineraries": [
            {"departureLocationCode": origin, "departureLocationType": "airport",
             "arrivalLocationCode": dest, "arrivalLocationType": "airport",
             "departureDate": _ymd(dep)},
            {"departureLocationCode": dest, "departureLocationType": "airport",
             "arrivalLocationCode": origin, "arrivalLocationType": "airport",
             "departureDate": _ymd(ret)},
        ],
        "openReturnDays": 0,
        "seatClass": "Y",
        "tripType": "RT",
        "flightFilter": {
            "filter": {
                "airlines": [], "departureAirports": [[origin], []],
                "arrivalAirports": [[], [origin]], "departureTime": [],
                "fareTypes": [], "flightDurationSeconds": [],
                "hasCardBenefit": True, "isIndividual": False,
                "isLowCarbonEmission": False, "isSameAirlines": False,
                "isSameDepArrAirport": True, "isTravelClub": False,
                "minFare": {}, "viaCount": [], "selectedItineraries": [],
            },
            "limit": 200, "skip": 0, "sort": {"adultMinFare": 1},
        },
        "initialRequest": True,
    }


_GQL_INTL = (
    "query getInternationalList($trip: String!, "
    "$itinerary: [InternationalList_itinerary]!, $adult: Int = 1, "
    "$fareType: String!, $where: String = \"pc\", $isDirect: Boolean = false) {"
    " internationalList(input: {trip: $trip, itinerary: $itinerary, "
    "person: {adult: $adult, child: 0, infant: 0}, fareType: $fareType, "
    "where: $where, isDirect: $isDirect}) { totalResCnt resCnt } }"
)


def _probe(name: str, url: str, payload: dict, referer: str,
           sse: bool = False) -> None:
    """후보 하나를 호출하고 결과를 로그로 남긴다. 예외는 삼킨다."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _UA,
        "Referer": referer,
        "Origin": "https://flight.naver.com",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    if sse:
        headers["Accept"] = "text/event-stream"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE %-14s 요청 실패: %s", name, str(e)[:200])
        return

    body = r.text or ""
    ctype = r.headers.get("Content-Type", "?")
    log.info("NVPROBE %-14s HTTP %s · %s · %d bytes",
             name, r.status_code, ctype, len(body))

    # 응답이 JSON이면 최상위 키를, 아니면 앞부분 원문을 남긴다
    try:
        data = json.loads(body)
        log.info("NVPROBE %-14s JSON 최상위 키: %s", name,
                 list(data)[:12] if isinstance(data, dict) else type(data).__name__)
        log.info("NVPROBE %-14s 본문 앞부분: %s", name, body[:700])
    except ValueError:
        # SSE는 "data: {...}" 줄이 이어진다. 마지막 data 줄이 가장 완전한 스냅샷.
        lines = [ln for ln in body.splitlines() if ln.startswith("data:")]
        log.info("NVPROBE %-14s 비JSON · data 줄 %d개", name, len(lines))
        log.info("NVPROBE %-14s 앞부분: %s", name, body[:500])
        if lines:
            log.info("NVPROBE %-14s 마지막 data 줄(앞 700자): %s",
                     name, lines[-1][:700])


def run(adults: int = 2, today: dt.date | None = None) -> None:
    """후보 4종을 순서대로 두드린다. 총 4요청, 1분 이내."""
    today = today or dt.date.today()
    dep = today + dt.timedelta(days=30)
    ret = dep + dt.timedelta(days=3)
    log.info("=== 네이버 API 탐침 시작 (%s~%s, 성인 %d명) ===", dep, ret, adults)

    # 구글로 불가 확정된 노선(오사카)과 국내선(김포-제주)이 실제 목표
    _probe("A:REST국제", "https://flight-api.naver.com/flight/international/searchFlights",
           _rest_payload("ICN", "KIX", dep, ret, adults),
           f"https://flight.naver.com/flights/international/ICN-KIX-{_ymd(dep)}/"
           f"KIX-ICN-{_ymd(ret)}?adult={adults}&isDirect=true&fareType=Y",
           sse=True)

    _probe("B:REST국내", "https://flight-api.naver.com/flight/domestic/searchFlights",
           _rest_payload("GMP", "CJU", dep, ret, adults),
           f"https://flight.naver.com/flights/domestic/GMP-CJU-{_ymd(dep)}/"
           f"CJU-GMP-{_ymd(ret)}?adult={adults}",
           sse=True)

    _probe("C:GQL국제", "https://airline-api.naver.com/graphql", {
        "operationName": "getInternationalList",
        "variables": {
            "adult": adults, "child": 0, "infant": 0, "where": "pc",
            "isDirect": True, "fareType": "Y", "trip": "OW",
            "itinerary": [{"departureAirport": "ICN", "arrivalAirport": "KIX",
                           "departureDate": _ymd(dep)}],
        },
        "query": _GQL_INTL,
    }, f"https://m-flight.naver.com/flights/international/ICN-KIX-{_ymd(dep)}"
       f"?adult={adults}&isDirect=true&fareType=Y")

    _probe("D:GQL국내", "https://airline-api.naver.com/graphql", {
        "operationName": "getDomesticList",
        "variables": {
            "adult": adults, "child": 0, "infant": 0, "where": "pc",
            "trip": "OW", "fareType": "Y",
            "itinerary": [{"departureAirport": "GMP", "arrivalAirport": "CJU",
                           "departureDate": _ymd(dep)}],
        },
        "query": _GQL_INTL.replace("International", "Domestic")
                          .replace("internationalList", "domesticList"),
    }, f"https://m-flight.naver.com/flights/domestic/GMP-CJU-{_ymd(dep)}"
       f"?adult={adults}&fareType=Y")

    log.info("=== 네이버 API 탐침 종료 ===")
