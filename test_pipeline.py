"""구글 응답 관대 파서 — fast-flights 기본 파서가 죽는 노선을 살린다.

왜 필요한가 (2026-07-26 진단):
  fast-flights의 parse_js는 페이로드를 고정 인덱스로 훑는다. 그중
      (alliances_data, airlines_data) = (payload[7][1][0], payload[7][1][1])
  와 탄소배출량 `flight[22]`는 **이 프로젝트가 전혀 쓰지 않는 값**인데,
  취항사가 많은 노선(오사카·후쿠오카·나리타)에서는 이 부분 구조가 달라
  IndexError가 나고 결과 전체가 버려진다. 실제로 GPROBE에서 그 노선들이
  `list index out of range`로 실패했고, 응답 HTML은 1.8MB로 멀쩡했다.

이 모듈은 **가격·항공사·구간 시각만** 방어적으로 뽑는다. 없거나 모양이
다른 필드는 조용히 건너뛴다. 기본 파서가 성공하면 그대로 쓰고, 실패할 때만
이쪽으로 넘어온다(app/search.py의 `_run_query`).

구글이 진짜로 오류를 반환한 경우(`errorHasStatus: true`)는 여기서도 None을
돌려주어 기존 동작(재시도·폴백)을 유지한다.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


class _Airport:
    __slots__ = ("code", "name")

    def __init__(self, code, name):
        self.code, self.name = code, name


class _Stamp:
    __slots__ = ("date", "time")

    def __init__(self, date, time):
        self.date, self.time = date, time


class _Segment:
    __slots__ = ("from_airport", "to_airport", "departure", "arrival",
                 "duration", "plane_type")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class _Itinerary:
    """fast_flights.Flights 와 같은 속성만 흉내 낸다 (_pick_best가 그대로 동작)."""
    __slots__ = ("type", "price", "airlines", "flights", "carbon")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self) -> str:
        return (f"Tolerant(price={self.price}, airlines={self.airlines}, "
                f"segments={len(self.flights or [])})")


def _get(seq, idx, default=None):
    try:
        return seq[idx]
    except (IndexError, KeyError, TypeError):
        return default


def extract_payload(html: str):
    """HTML에서 ds:1 스크립트의 JSON 페이로드를 꺼낸다.

    Returns:
        payload(list) · None(구글이 오류 반환) · False(스크립트를 못 찾음)
    """
    try:
        from selectolax.lexbor import LexborHTMLParser
    except ImportError:  # pragma: no cover
        from selectolax.parser import HTMLParser as LexborHTMLParser

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        return False
    js = script.text()
    if "data:" not in js:
        return False
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.rstrip().endswith("errorHasStatus: true"):
        return None
    try:
        return json.loads(data)
    except ValueError:
        return False


def parse_tolerant(html: str) -> list | None:
    """가격·항공사·구간 시각만 방어적으로 추출. 실패하면 None."""
    payload = extract_payload(html)
    if payload is None or payload is False:
        return None

    raw = _get(_get(payload, 3, []), 0)
    if not raw:
        return None

    out: list[_Itinerary] = []
    for k in raw:
        try:
            flight = _get(k, 0)
            if flight is None:
                continue
            # 가격: k[1][0][1]
            price = _get(_get(_get(k, 1, []), 0, []), 1)
            if not isinstance(price, (int, float)) or price <= 0:
                continue

            segs = []
            for sf in (_get(flight, 2) or []):
                segs.append(_Segment(
                    from_airport=_Airport(_get(sf, 3), _get(sf, 4)),
                    to_airport=_Airport(_get(sf, 6), _get(sf, 5)),
                    departure=_Stamp(_get(sf, 20), _get(sf, 8)),
                    arrival=_Stamp(_get(sf, 21), _get(sf, 10)),
                    duration=_get(sf, 11),
                    plane_type=_get(sf, 17),
                ))
            if not segs:
                continue

            out.append(_Itinerary(
                type=_get(flight, 0),
                price=int(price),
                airlines=_get(flight, 1) or [],
                flights=segs,
                carbon=None,   # 안 쓰는 값이므로 아예 읽지 않는다
            ))
        except Exception:  # noqa: BLE001 - 항목 하나 실패는 건너뛴다
            continue

    return out or None
