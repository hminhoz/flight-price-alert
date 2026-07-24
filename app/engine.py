"""가격 감시 핵심 로직 (네트워크 없음 → 단위 테스트 가능).

용어:
  leg   = (노선, 방향 out/ret, 날짜) 편도 검색 단위
  combo = (노선, 출발일, N박) 실제 여행 조합. 가격 = out leg + ret leg 합산
  unit  = 기준가 단위 = 노선 × 출발월 ("ICN-KIX|2026-09")
"""
from __future__ import annotations

import datetime as dt
import hashlib
import statistics
from dataclasses import dataclass

from .settings import Route, Settings
from .state import State


# ---------------------------------------------------------------- legs & 샤딩

@dataclass(frozen=True)
class Leg:
    route: Route
    direction: str  # "out" | "ret"
    date: dt.date

    @property
    def key(self) -> str:
        return f"{self.route.key}|{self.direction}|{self.date.isoformat()}"

    @property
    def origin(self) -> str:
        return self.route.origin if self.direction == "out" else self.route.destination

    @property
    def dest(self) -> str:
        return self.route.destination if self.direction == "out" else self.route.origin


def all_legs(cfg: Settings, today: dt.date) -> list[Leg]:
    """오늘 이후, 기간 내 유효한 모든 leg."""
    legs: list[Leg] = []
    start = max(cfg.period_start, today)
    for route in cfg.routes:
        out_dates: set[dt.date] = set()
        ret_dates: set[dt.date] = set()
        d = start
        while d <= cfg.period_end:
            for n in cfg.trip_nights:
                r = d + dt.timedelta(days=n)
                if r <= cfg.period_end:
                    out_dates.add(d)
                    ret_dates.add(r)
            d += dt.timedelta(days=1)
        legs += [Leg(route, "out", d) for d in sorted(out_dates)]
        legs += [Leg(route, "ret", d) for d in sorted(ret_dates)]
    return legs


def shard_of(leg: Leg, shards: int) -> int:
    h = hashlib.sha256(leg.key.encode()).digest()
    return h[0] % shards


def legs_for_run(cfg: Settings, today: dt.date, current_shard: int) -> list[Leg]:
    """이번 실행이 검색할 leg: 담당 샤드 + 출발 임박분(항상)."""
    imminent_until = today + dt.timedelta(days=cfg.imminent_days)
    picked = []
    for leg in all_legs(cfg, today):
        if leg.date <= imminent_until or shard_of(leg, cfg.shards) == current_shard:
            picked.append(leg)
    return picked


def current_shard_from_hour(hour_utc: int, shards: int) -> int:
    return (hour_utc // 2) % shards


# ---------------------------------------------------------------- combos

@dataclass
class Combo:
    route: Route
    dep: dt.date
    nights: int
    price: int
    out_leg: dict
    ret_leg: dict

    @property
    def key(self) -> str:
        return f"{self.route.key}|{self.dep.isoformat()}|{self.nights}n"

    @property
    def unit(self) -> str:
        return f"{self.route.key}|{self.dep.strftime('%Y-%m')}"

    @property
    def ret(self) -> dt.date:
        return self.dep + dt.timedelta(days=self.nights)


def build_combos(cfg: Settings, state: State, today: dt.date) -> list[Combo]:
    combos: list[Combo] = []
    for route in cfg.routes:
        d = max(cfg.period_start, today)
        while d <= cfg.period_end:
            out = state.fresh_leg_price(
                State.leg_key(route.key, "out", d.isoformat()), cfg.leg_freshness_days)
            if out:
                for n in cfg.trip_nights:
                    r = d + dt.timedelta(days=n)
                    if r > cfg.period_end:
                        continue
                    ret = state.fresh_leg_price(
                        State.leg_key(route.key, "ret", r.isoformat()), cfg.leg_freshness_days)
                    if ret:
                        combos.append(Combo(route, d, n, out["price"] + ret["price"], out, ret))
            d += dt.timedelta(days=1)
    return combos


# ---------------------------------------------------------------- 기준가 & 알림

@dataclass
class Alert:
    kind: str          # "baseline" | "record"
    combo: Combo
    baseline: int
    prev_min: int | None


def process(cfg: Settings, state: State, combos: list[Combo],
            today: dt.date) -> list[Alert]:
    """기준가 갱신 + 알림 판정. 관측 기간에는 수집만 하고 빈 리스트 반환."""
    first_run = state.first_run_date(today)
    observing = today < first_run + dt.timedelta(days=cfg.observation_days)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # 1) unit별 오늘의 최저 콤보가격 기록
    todays_min: dict[str, int] = {}
    for c in combos:
        todays_min[c.unit] = min(todays_min.get(c.unit, c.price), c.price)

    for unit, price in todays_min.items():
        b = state.baselines.setdefault(unit, {"daily_min": {}})
        dm = b["daily_min"]
        dm[today.isoformat()] = min(dm.get(today.isoformat(), price), price)
        cutoff = (today - dt.timedelta(days=cfg.baseline_recalc_window)).isoformat()
        b["daily_min"] = {d: p for d, p in dm.items() if d >= cutoff}

        # 역대 최저 (unit 단위)
        if "alltime_min" not in b or price < b["alltime_min"]:
            b["alltime_min_prev"] = b.get("alltime_min")
            b["alltime_min"] = price
            b["alltime_min_at"] = now_iso

        # 기준가: 관측기간엔 관측 최저로 수렴, 이후엔
        # 최근 14일 daily_min의 중앙값이 더 낮을 때만 하향 (상향 없음)
        if observing or "baseline" not in b:
            b["baseline"] = min(b.get("baseline", price), price)
        else:
            recent = list(b["daily_min"].values())
            if recent:
                candidate = int(statistics.median(recent))
                if candidate < b["baseline"]:
                    b["baseline"] = candidate

    if observing:
        return []

    # 2) 알림 판정
    alerts: list[Alert] = []
    for c in combos:
        b = state.baselines.get(c.unit)
        if not b or "baseline" not in b:
            continue
        is_record = c.price <= b["alltime_min"] and b.get("alltime_min_at") == now_iso \
            and todays_min.get(c.unit) == c.price
        is_below_baseline = c.price <= b["baseline"]
        if not (is_record or is_below_baseline):
            continue
        # 중복 억제: 직전 알림가보다 낮을 때만
        sent = state.alerts_sent.get(c.key)
        if sent and c.price >= sent["price"]:
            continue
        alerts.append(Alert(
            kind="record" if is_record else "baseline",
            combo=c,
            baseline=b["baseline"],
            prev_min=b.get("alltime_min_prev"),
        ))

    # 3) 전송 확정분 기록 (실 전송은 notify 성공 후 caller가 확정해도 되지만
    #    실패 시 다음 실행에서 재시도되는 편이 안전해 여기서는 기록만 준비)
    return alerts


def mark_sent(state: State, alerts: list[Alert]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for a in alerts:
        state.alerts_sent[a.combo.key] = {"price": a.combo.price, "at": now}


# ---------------------------------------------------------------- 오류 감시

def failure_alert_needed(cfg: Settings, state: State) -> str | None:
    """연속 N회 실행에서 실패율 초과 시 메시지 반환 (쿨다운 적용)."""
    runs = state.meta.get("recent_runs", [])[-cfg.failure_alert_streak:]
    if len(runs) < cfg.failure_alert_streak:
        return None
    bad = all(
        r["attempted"] > 0 and r["failed"] / r["attempted"] > cfg.failure_alert_threshold
        for r in runs
    )
    if not bad:
        return None
    last = state.meta.get("last_failure_alert")
    if last:
        since = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)
        if since < dt.timedelta(hours=cfg.failure_alert_cooldown_hours):
            return None
    state.meta["last_failure_alert"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rate = runs[-1]["failed"] / runs[-1]["attempted"] * 100
    return (f"⚠️ 가격 조회가 계속 실패하고 있어요 (최근 실패율 {rate:.0f}%).\n"
            f"fast-flights 라이브러리 파손 또는 일시 차단 가능성이 있습니다. "
            f"GitHub Actions 로그를 확인해주세요.")
