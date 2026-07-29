"""구글 플라이트 tfs(protobuf)에 **시간 필터**를 직접 심는다.

fast_flights 라이브러리의 스키마에는 시간 필드가 없다. 그러나 구글 UI에서
시간 필터를 걸면 `FlightData`(필드 3) 안에 다음이 추가된다 — 2026-07-29에
실제 URL을 해부해 확인:

    3.2   "2026-08-08"   날짜
    3.8   0              ┐
    3.9   11             ├ 시간 관련 4칸
    3.10  18             │
    3.11  23             ┘
    3.13  {1:1, 2:"ICN"} 출발 공항
    3.14  {1:3, 2:"/m/0dqyw"} 도착지

네 칸의 의미(출발 범위 / 도착 범위)는 **쏴 보고 확인**한다. 편도 응답에는
항공편 시각이 모두 들어 있으므로, 필터를 걸고 돌아온 편들의 시각을 보면
어느 필드가 무엇인지 확정된다. `probe_semantics()`가 그 일을 한다.
"""
from __future__ import annotations

import base64
import logging

log = logging.getLogger(__name__)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _str_field(field: int, s: str) -> bytes:
    b = s.encode()
    return _key(field, 2) + _varint(len(b)) + b


def _int_field(field: int, v: int) -> bytes:
    return _key(field, 0) + _varint(v)


def _msg_field(field: int, body: bytes) -> bytes:
    return _key(field, 2) + _varint(len(body)) + body


def _airport(code: str, kind: int = 1) -> bytes:
    """구글은 공항 코드(kind=1)와 지역 ID(kind=3)를 구분한다."""
    return _int_field(1, kind) + _str_field(2, code)


def flight_data(date: str, frm: str, to: str, *, max_stops: int | None = 0,
                times: tuple | None = None) -> bytes:
    """FlightData 하나. times=(f8, f9, f10, f11) 중 None인 칸은 생략."""
    body = _str_field(2, date)
    if max_stops is not None:
        body += _int_field(5, max_stops)
    if times:
        for idx, val in zip((8, 9, 10, 11), times):
            if val is not None:
                body += _int_field(idx, int(val))
    body += _msg_field(13, _airport(frm))
    body += _msg_field(14, _airport(to))
    return body


def build_tfs(legs: list, adults: int = 1, trip: int = 2,
              seat: int = 1) -> str:
    """legs: [flight_data(...) 바이트]. trip 1=왕복 2=편도 3=다구간."""
    body = b"".join(_msg_field(3, x) for x in legs)
    body += _int_field(8, 1) * adults      # 승객: ADULT 반복
    body += _int_field(9, seat)
    body += _int_field(19, trip)
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def url(tfs: str, currency: str = "KRW", lang: str = "ko") -> str:
    return (f"https://www.google.com/travel/flights?tfs={tfs}"
            f"&curr={currency}&hl={lang}&gl=KR")
