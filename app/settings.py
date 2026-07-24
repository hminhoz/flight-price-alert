"""config.yaml 로더. 설정 구조가 바뀌면 이 파일만 손보면 된다."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Route:
    origin: str
    destination: str
    label: str
    domestic: bool = False

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}"


@dataclass
class Settings:
    period_start: dt.date
    period_end: dt.date
    trip_nights: list[int]
    outbound_window: tuple[dt.time, dt.time]
    return_window: tuple[dt.time, dt.time]
    direct_only: bool
    exclude_departures: list
    exclude_weekdays: list
    adults: int
    currency: str
    routes: list[Route]
    shards: int
    imminent_days: int
    attention_days: int
    request_delay_sec: tuple[float, float]
    observation_days: int
    baseline_recalc_window: int
    bundle_top_n: int
    similar_margin_pct: float
    similar_top_n: int
    retry: int
    failure_alert_threshold: float
    failure_alert_streak: int
    failure_alert_cooldown_hours: int
    leg_freshness_days: int = 3  # 콤보 계산 시 다리(leg) 가격의 최대 허용 나이

    raw: dict = field(default_factory=dict, repr=False)


def _parse_excludes(items: list) -> list:
    """["2026-09-24", "2026-10-01~2026-10-05"] → [(시작일, 종료일), ...]"""
    out = []
    for it in items:
        s = str(it)
        if "~" in s:
            a, b = s.split("~")
            out.append((dt.date.fromisoformat(a.strip()), dt.date.fromisoformat(b.strip())))
        else:
            d = dt.date.fromisoformat(s.strip())
            out.append((d, d))
    return out


def _parse_time(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def load(path: Path | None = None) -> Settings:
    path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    s, sch, al, er = raw["search"], raw["schedule"], raw["alerts"], raw["errors"]
    return Settings(
        period_start=dt.date.fromisoformat(s["period"]["start"]),
        period_end=dt.date.fromisoformat(s["period"]["end"]),
        trip_nights=list(s["trip_nights"]),
        outbound_window=(
            _parse_time(s["outbound_departure"]["earliest"]),
            _parse_time(s["outbound_departure"]["latest"]),
        ),
        return_window=(
            _parse_time(s["return_departure"]["earliest"]),
            _parse_time(s["return_departure"]["latest"]),
        ),
        direct_only=bool(s.get("direct_only", True)),
        exclude_departures=_parse_excludes(s.get("exclude_departures") or []),
        exclude_weekdays=["월화수목금토일".index(str(w)[0]) for w in (s.get("exclude_weekdays") or [])],
        adults=int(s["adults"]),
        currency=s.get("currency", "KRW"),
        routes=[Route(**r) for r in raw["routes"]],
        shards=int(sch["shards"]),
        imminent_days=int(sch["imminent_days"]),
        attention_days=int(sch.get("attention_days", 21)),
        request_delay_sec=tuple(sch["request_delay_sec"]),
        observation_days=int(al["observation_days"]),
        baseline_recalc_window=int(al["baseline_recalc_window"]),
        bundle_top_n=int(al["bundle_top_n"]),
        similar_margin_pct=float(al.get("similar_margin_pct", 10)),
        similar_top_n=int(al.get("similar_top_n", 4)),
        retry=int(er["retry"]),
        failure_alert_threshold=float(er["failure_alert_threshold"]),
        failure_alert_streak=int(er["failure_alert_streak"]),
        failure_alert_cooldown_hours=int(er["failure_alert_cooldown_hours"]),
        raw=raw,
    )
