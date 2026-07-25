"""네이버 항공권 API 탐침 2차 — 차단 원인 판별용.

1차 결과(2026-07-25): 후보 4종이 모두 HTTP 503 nginx 기본 페이지(592바이트 동일).
엔드포인트 오류(404)도 인증 오류(401/403)도 아닌 획일적 거절 → API 앞단 차단.

두 가설이 남았다:
  H1. IP 차단 — GitHub Actions 러너가 미국 애저 IP라 네이버가 통째로 막는다.
      → 일반 페이지(www.naver.com)도 503이면 이 쪽. 해결 불가에 가깝다.
  H2. 세션/헤더 부족 — 쿠키 없이 API를 직접 POST해서 거절당한다.
      → 일반 페이지는 200인데 API만 503이면 이 쪽. 세션 확보로 뚫릴 수 있다.

그래서 순서대로 확인한다:
  1) www.naver.com GET      — IP 차단 여부 (대조군)
  2) flight.naver.com 페이지 GET — 항공권 서비스 접근 + 쿠키 확보
  3) 같은 세션으로 API POST  — 쿠키를 달면 달라지는지

판별이 끝나면 이 파일은 삭제할 것.
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


def _log_result(name: str, r, body_head: int = 300) -> None:
    body = r.text or ""
    log.info("NVPROBE %-16s HTTP %s · %s · %d bytes",
             name, r.status_code, r.headers.get("Content-Type", "?"), len(body))
    head = " ".join(body[:body_head].split())
    log.info("NVPROBE %-16s 본문: %s", name, head)


def run(adults: int = 2, today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    dep = today + dt.timedelta(days=30)
    ret = dep + dt.timedelta(days=3)
    log.info("=== 네이버 탐침 2차: 차단 원인 판별 (%s~%s) ===", dep, ret)

    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    # 1) 대조군 — 네이버 메인. 여기가 503이면 IP 차단(H1) 확정.
    try:
        r = s.get("https://www.naver.com", timeout=_TIMEOUT)
        _log_result("1.대조군메인", r, 200)
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE 1.대조군메인     요청 실패: %s", str(e)[:200])

    # 2) 항공권 페이지 — 서비스 접근 확인 + 쿠키 확보
    page = (f"https://flight.naver.com/flights/international/"
            f"ICN-KIX-{_ymd(dep)}/KIX-ICN-{_ymd(ret)}?adult={adults}"
            f"&isDirect=true&fareType=Y")
    try:
        r = s.get(page, timeout=_TIMEOUT)
        _log_result("2.항공권페이지", r, 200)
        log.info("NVPROBE 2.항공권페이지     확보 쿠키: %s",
                 list(s.cookies.get_dict())[:10] or "없음")
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE 2.항공권페이지   요청 실패: %s", str(e)[:200])

    # 3) 같은 세션으로 API POST — 쿠키가 붙으면 달라지는지
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
    try:
        r = s.post("https://flight-api.naver.com/flight/international/searchFlights",
                   json=payload, timeout=_TIMEOUT,
                   headers={"Accept": "text/event-stream",
                            "Content-Type": "application/json",
                            "Referer": page,
                            "Origin": "https://flight.naver.com"})
        _log_result("3.세션API", r, 400)
        if r.status_code < 400:
            lines = [ln for ln in (r.text or "").splitlines() if ln.startswith("data:")]
            log.info("NVPROBE 3.세션API        data 줄 %d개", len(lines))
            if lines:
                log.info("NVPROBE 3.세션API        마지막 data(700자): %s", lines[-1][:700])
    except Exception as e:  # noqa: BLE001
        log.info("NVPROBE 3.세션API        요청 실패: %s", str(e)[:200])

    log.info("판별: 1번이 503이면 IP 차단(해결 어려움) · "
             "1·2번 200인데 3번만 503이면 세션/헤더 문제(추가 시도 가치 있음)")
    log.info("=== 네이버 탐침 2차 종료 ===")
