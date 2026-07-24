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


def _leg_hash(leg: Leg) -> int:
    return hashlib.sha256(leg.key.encode()).digest()[0] * 256 + \
        hashlib.sha256(leg.key.encode()).digest()[1]


def shard_of(leg: Leg, shards: int) -> int:
    return _leg_hash(leg) % shards


def legs_for_run(cfg: Settings, today: dt.date, current_shard: int) -> list[Leg]:
    """이번 실행이 검색할 leg — 3단계 계층 (SPEC §5):

      임박(≤imminent_days)   : 매 실행 체크 (하루 12회)
      주시(≤attention_days)  : 하루 2회 체크
      원거리(그 외)          : 하루 1회 체크

    `offset = today.toordinal()` 을 해시에 더해 배정 슬롯이 매일 회전한다.
    → 특정 조합이 항상 같은 시각에만 체크되어 생기는 사각(항공사별
    고정 갱신 시각과의 어긋남)을 제거.
    """
    imminent_until = today + dt.timedelta(days=cfg.imminent_days)
    attention_until = today + dt.timedelta(days=cfg.attention_days)
    offset = today.toordinal()
    half = max(1, cfg.shards // 2)  # shards=12 → 6: 하루 12슬롯 중 2회 적중

    picked = []
    for leg in all_legs(cfg, today):
        if leg.date <= imminent_until:
            picked.append(leg)
        elif leg.date <= attention_until:
            if (_leg_hash(leg) + offset) % half == current_shard % half:
                picked.append(leg)
        else:
            if (_leg_hash(leg) + offset) % cfg.shards == current_shard:
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


# ---------------------------------------------------------------- 관측기간 리포트

def is_observing(cfg: Settings, state: State, today: dt.date) -> bool:
    first_run = state.first_run_date(today)
    return today < first_run + dt.timedelta(days=cfg.observation_days)


def observation_report(cfg: Settings, state: State, today: dt.date) -> str | None:
    """관측 기간 중 하루 1회, 형성 중인 기준가 요약 메시지. 이미 보냈으면 None."""
    if not is_observing(cfg, state, today):
        return None
    if state.meta.get("last_obs_report") == today.isoformat():
        return None
    if not state.baselines:
        return None
    state.meta["last_obs_report"] = today.isoformat()

    labels = {r.key: r.label for r in cfg.routes}
    day_n = (today - state.first_run_date(today)).days + 1
    lines = [f"📡 <b>관측 {day_n}일차</b> — 기준가 형성 중 "
             f"(총 {cfg.observation_days}일, 이후 특가 알림 시작)"]
    for unit in sorted(state.baselines):
        b = state.baselines[unit]
        if "baseline" not in b:
            continue
        route_key, month = unit.split("|")
        m = int(month.split("-")[1])
        lines.append(f"· {labels.get(route_key, route_key)} {m}월: "
                     f"현재 최저 ₩{b['baseline']:,}")
    return "\n".join(lines) if len(lines) > 1 else None


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
