"""네이버 항공권 API 탐침 3차 — TLS 지문 우회 + 실제 엔드포인트 탐색.

경과:
  1차: 후보 4종 모두 HTTP 503 (nginx 기본 페이지, 592바이트 동일)
  2차: www.naver.com 200 · 항공권 페이지 200 · **API만 503**
       → IP 차단 아님. API 앞단이 이 클라이언트를 골라내고 있다.

남은 유력 원인은 TLS 지문이다. requests는 브라우저와 다른 핸드셰이크 특성을
남기고, 요즘 API 게이트웨이는 이걸로 봇을 거른다. 일반 페이지는 통과시키고
API만 막는 패턴이 정확히 이 증상이다.

마침 primp가 이미 설치돼 있다(fast-flights 의존성). 구글 한국 마켓 폴백에서
쓰고 있는 그 라이브러리로, 크롬 TLS 지문을 흉내 낸다.

3차에서 확인할 것:
  A. primp로 REST 엔드포인트 재시도
  B. primp로 GraphQL 엔드포인트 재시도
  C. 항공권 페이지 HTML에서 실제 API 주소 추출
     (블로그 자료가 낡아 엔드포인트 자체가 바뀌었을 수 있다)
"""
from __future__ import annotations

import datetime as dt
import logging
import re

log = logging.getLogger(__name__)

_TIMEOUT = 25


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _client():
    from primp import Client
    return Client(impersonate="chrome_145", impersonate_os="macos",
                  referer=True, cookie_store=True, timeout=_TIMEOUT)


def _log_res(name: str, res, head: int = 400) -> None:
    body = getattr(res, "text", "") or ""
    log.info("NVPROBE %-12s HTTP %s · %d bytes", name, res.status_code, len(body))
    log.info("NVPROBE %-12s 본문: %s", name, " ".join(body[:head].split()))


def run(adults: int = 2, today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    dep = today + dt.timedelta(days=30)
    ret = dep + dt.timedelta(days=3)
    log.info("=== 네이버 탐침 3차: primp(크롬 TLS) + 엔드포인트 탐색 ===")

    page = (f"https://flight.naver.com/flights/international/"
            f"ICN-KIX-{_ymd(dep)}/KIX-ICN-{_ymd(ret)}?adult={adults}"
            f"&isDirect=true&fareType=Y")

    try:
        c = _client()
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE primp 초기화 실패: %s", str(e)[:200])
        return

    # 먼저 페이지를 열어 세션·쿠키를 만든다 (브라우저와 같은 순서)
    html = ""
    try:
        r = c.get(page)
        html = getattr(r, "text", "") or ""
        log.info("NVPROBE 0.페이지     HTTP %s · %d bytes", r.status_code, len(html))
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE 0.페이지     실패: %s", str(e)[:200])

    headers = {"Content-Type": "application/json", "Referer": page,
               "Origin": "https://flight.naver.com",
               "Accept-Language": "ko-KR,ko;q=0.9"}

    payload = {
        "adultCount": adults, "childCount": 0, "infantCount": 0,
        "device": "pc", "isNonstop": True,
        "itineraries": [
            {"departureLocationCode": "ICN", "departureLocationType": "airport",
             "arrivalLocationCode": "KIX", "arrivalLocationType": "airport",
             "departureDate": _ymd(dep)},
            {"departureLocationCode": "KIX", "departureLocationType": "airport",
             "arrivalLocationCode": "ICN", "arrivalLocationType": "airport",
             "departureDate": _ymd(ret)},
        ],
        "openReturnDays": 0, "seatClass": "Y", "tripType": "RT",
        "flightFilter": {"filter": {}, "limit": 200, "skip": 0,
                         "sort": {"adultMinFare": 1}},
        "initialRequest": True,
    }

    # A. REST (SSE)
    try:
        r = c.post("https://flight-api.naver.com/flight/international/searchFlights",
                   json=payload,
                   headers={**headers, "Accept": "text/event-stream"})
        _log_res("A.primp REST", r)
        body = getattr(r, "text", "") or ""
        lines = [ln for ln in body.splitlines() if ln.startswith("data:")]
        if lines:
            log.info("NVPROBE A.primp REST data 줄 %d개 · 마지막(700자): %s",
                     len(lines), lines[-1][:700])
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE A.primp REST 실패: %s", str(e)[:200])

    # B. GraphQL
    try:
        r = c.post("https://airline-api.naver.com/graphql", json={
            "operationName": "getInternationalList",
            "variables": {"adult": adults, "child": 0, "infant": 0, "where": "pc",
                          "isDirect": True, "fareType": "Y", "trip": "OW",
                          "itinerary": [{"departureAirport": "ICN",
                                         "arrivalAirport": "KIX",
                                         "departureDate": _ymd(dep)}]},
            "query": "query getInternationalList { internationalList { totalResCnt } }",
        }, headers={**headers, "Accept": "application/json"})
        _log_res("B.primp GQL", r)
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE B.primp GQL  실패: %s", str(e)[:200])

    # C. 페이지 HTML에서 실제 API 주소 추출 — 자료가 낡았을 가능성 대비
    if html:
        hosts = sorted(set(re.findall(r"https://([a-z0-9.-]*api[a-z0-9.-]*\.naver\.com)",
                                      html, re.I)))
        paths = sorted(set(re.findall(r"[\"'](/(?:api|graphql)[a-zA-Z0-9/_-]{0,60})[\"']",
                                      html)))[:15]
        log.info("NVPROBE C.발견호스트  %s", hosts or "없음")
        log.info("NVPROBE C.발견경로    %s", paths or "없음")
    else:
        log.info("NVPROBE C.추출        페이지 HTML 없음 → 건너뜀")

    log.info("판별: A나 B가 200/201이면 primp로 뚫린 것 → 네이버 수집 모듈 착수 · "
             "여전히 503이면 C의 발견 목록에서 새 엔드포인트 확인 · "
             "둘 다 없으면 네이버 포기하고 스카이스캐너 비용 검토")
    log.info("=== 네이버 탐침 3차 종료 ===")
