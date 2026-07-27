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


# 노선별 시간창 override 지원 이유 (v1.11):
#   전역 시간창(가는 편 06~13시 / 오는 편 18시 이후)은 노선마다 운항 스케줄
#   구조가 달라서 일부 노선을 통째로 죽인다. 실측 확인:
#     - ICN→KOJ 는 출발편이 12:47·15:45 등 오후에 몰려 있어 out 콤보 0개
#     - CTS→ICN 은 저녁 출발편이 없어 ret 가격 확보 1/58
#   → config의 routes 항목에 outbound_departure / return_departure 를 부분
#     지정하면 그 노선만 덮어쓴다 (지정하지 않은 쪽 경계는 전역값 유지).
_WINDOW_KEYS = ("outbound_departure", "return_departure")


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
    naver_routes: tuple = ()
    naver_directions: tuple = ("out",)
    naver_hour: int = 5
    naver_runs_per_day: int = 3
    naver_budget_min: int = 20
    exclude_airlines: tuple = ()
    cross_airports: bool = True
    city_groups: dict = field(default_factory=dict, repr=False)
    concurrency: int = 3
    leg_freshness_days: int = 3  # 콤보 계산 시 다리(leg) 가격의 최대 허용 나이

    # 노선별 시간창 override: {route_key: {"out": (lo, hi), "ret": (lo, hi)}}
    route_windows: dict = field(default_factory=dict, repr=False)
    live_board: bool = True
    board_top_n: int = 8
    digest_top_n: int = 3
    digest_hour: int = 9
    cycle_report: str = "daily"     # 한 바퀴 완료 보고: daily | every | off
    min_below_baseline_pct: float = 2.0
    min_redrop_pct: float = 2.0     # 재알림 최소 하락폭 (%)
    bundle_min_gap_pct: float = 3.0 # 묶음 항목 간 최소 가격 차이 (%)
    verify_roundtrip: bool = True   # 알림 직전 왕복 실가 조회 (v1.12)
    verify_max_queries: int = 6     # 실행당 왕복 검증 쿼리 상한

    raw: dict = field(default_factory=dict, repr=False)

    def window_for(self, route_key: str, direction: str) -> tuple[dt.time, dt.time]:
        """**선호** 시간창. 노선과 무관하게 항상 전역값이다.

        v1.33 이전에는 여기서 노선별 override를 돌려줘, 넓힌 창 안에서 최저가를
        고르는 바람에 오전 편이 있는 날에도 더 싼 오후 편을 집어왔다.
        사용자 의도는 '선호 시간대에 아무것도 없을 때만 양보'였다.
        """
        return self.outbound_window if direction == "out" else self.return_window

    def fallback_window_for(self, route_key: str,
                            direction: str) -> tuple[dt.time, dt.time] | None:
        """선호 시간창에 편이 없을 때만 쓰는 **양보** 시간창. 없으면 None."""
        return self.route_windows.get(route_key, {}).get(direction)

    def has_window_override(self, route_key: str) -> bool:
        return bool(self.route_windows.get(route_key))


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


def _cycle_policy(v) -> str:
    """한 바퀴 보고 정책 정규화.

    YAML 1.1은 따옴표 없는 off/on/yes/no 를 불리언으로 읽는다. 그래서
    `cycle_report: off` 가 False → "false" 가 되어 "off" 와도 "daily" 와도
    맞지 않았고, 두 분기 모두 비껴가 **매번 발송**되는 상태가 됐다 (v1.47).
    알 수 없는 값은 조용한 쪽(off)으로 떨어뜨려 스팸을 막는다.
    """
    s = str(v).strip().lower()
    if s in ("off", "false", "no", "none", "0"):
        return "off"
    if s in ("every", "always", "true", "yes"):
        return "every"
    if s == "daily":
        return "daily"
    return "off"


def _parse_time(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def load(path: Path | None = None) -> Settings:
    path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    s, sch, al, er = raw["search"], raw["schedule"], raw["alerts"], raw["errors"]

    global_out = (_parse_time(s["outbound_departure"]["earliest"]),
                  _parse_time(s["outbound_departure"]["latest"]))
    global_ret = (_parse_time(s["return_departure"]["earliest"]),
                  _parse_time(s["return_departure"]["latest"]))

    # 노선 정의에서 시간창 override 키를 분리한 뒤 Route를 만든다
    routes: list[Route] = []
    route_windows: dict[str, dict[str, tuple[dt.time, dt.time]]] = {}
    for item in raw["routes"]:
        spec = dict(item)
        overrides = {k: spec.pop(k) for k in _WINDOW_KEYS if k in spec}
        route = Route(**spec)
        routes.append(route)
        for key, direction, base in (("outbound_departure", "out", global_out),
                                     ("return_departure", "ret", global_ret)):
            if key not in overrides:
                continue
            o = overrides[key] or {}
            lo = _parse_time(o["earliest"]) if "earliest" in o else base[0]
            hi = _parse_time(o["latest"]) if "latest" in o else base[1]
            route_windows.setdefault(route.key, {})[direction] = (lo, hi)

    return Settings(
        period_start=dt.date.fromisoformat(s["period"]["start"]),
        period_end=dt.date.fromisoformat(s["period"]["end"]),
        trip_nights=list(s["trip_nights"]),
        outbound_window=global_out,
        return_window=global_ret,
        direct_only=bool(s.get("direct_only", True)),
        exclude_departures=_parse_excludes(s.get("exclude_departures") or []),
        exclude_weekdays=["월화수목금토일".index(str(w)[0]) for w in (s.get("exclude_weekdays") or [])],
        adults=int(s["adults"]),
        currency=s.get("currency", "KRW"),
        naver_routes=tuple(s.get("naver_routes") or []),
        naver_directions=tuple(s.get("naver_directions") or ["out"]),
        naver_hour=int(s.get("naver_hour", 5)),
        naver_runs_per_day=max(1, int(s.get("naver_runs_per_day", 3))),
        naver_budget_min=int(s.get("naver_budget_min", 20)),
        exclude_airlines=tuple(
            str(x).strip().upper() for x in (s.get("exclude_airlines") or [])),
        cross_airports=bool(s.get("cross_airports", True)),
        city_groups={k: list(v) for k, v in (s.get("city_groups") or {}).items()},
        concurrency=max(1, int(sch.get("concurrency", 3))),
        routes=routes,
        route_windows=route_windows,
        live_board=bool(al.get("live_board", True)),
        board_top_n=max(1, int(al.get("board_top_n", 8))),
        digest_top_n=max(1, int(al.get("digest_top_n", 3))),
        digest_hour=int(al.get("digest_hour", 9)),
        cycle_report=_cycle_policy(al.get("cycle_report", "daily")),
        min_below_baseline_pct=float(al.get("min_below_baseline_pct", 2)),
        min_redrop_pct=float(al.get("min_redrop_pct", 2)),
        bundle_min_gap_pct=float(al.get("bundle_min_gap_pct", 3)),
        verify_roundtrip=bool(al.get("verify_roundtrip", True)),
        verify_max_queries=int(al.get("verify_max_queries", 6)),
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
