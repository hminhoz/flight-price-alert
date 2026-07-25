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


def is_excluded_departure(cfg: Settings, d: dt.date) -> bool:
    if d.weekday() in cfg.exclude_weekdays:
        return True
    return any(a <= d <= b for a, b in cfg.exclude_departures)


def all_legs(cfg: Settings, today: dt.date) -> list[Leg]:
    """오늘 이후, 기간 내 유효한 모든 leg. 제외 출발일은 검색에서 뺀다."""
    legs: list[Leg] = []
    start = max(cfg.period_start, today)
    for route in cfg.routes:
        out_dates: set[dt.date] = set()
        ret_dates: set[dt.date] = set()
        d = start
        while d <= cfg.period_end:
            if not is_excluded_departure(cfg, d):
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
            if is_excluded_departure(cfg, d):  # 과거 수집분이 남아있어도 콤보 제외
                d += dt.timedelta(days=1)
                continue
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
    # v1.12: 알림 확정 후 왕복 재조회로 얻은 실제 왕복 총액. 조회 실패 시 None.
    # 판정(기준가/역대최저)은 여전히 combo.price(편도 합산) 기준 — 기준가와 같은
    # 척도라야 비교가 성립하므로 이 값은 표시 전용이다.
    rt_price: int | None = None


def preview_alerts(cfg: Settings, combos: list[Combo]) -> list[Alert]:
    """미리보기용: 기준가·중복억제를 무시하고 노선별 최저 조합만 뽑는다.

    "알림이 실제로 어떻게 오는지 지금 보고 싶다"는 용도. 판정 로직을 타지 않으므로
    상태를 바꾸지 않고, 호출부에서 저장 없이 전송만 한다.
    """
    from collections import defaultdict
    by_route: dict[str, list[Combo]] = defaultdict(list)
    for c in combos:
        by_route[c.route.key].append(c)
    out: list[Alert] = []
    for items in by_route.values():
        items.sort(key=lambda c: c.price)
        for c in items[: cfg.bundle_top_n]:
            out.append(Alert(kind="baseline", combo=c, baseline=c.price, prev_min=None))
    return out


def display_selection(cfg: Settings, alerts: list[Alert]) -> list[Alert]:
    """알림 메시지 본문에 실제로 노출될 알림들 (노선별 저가 top N).

    notify.format_alerts 와 동일한 선별 규칙 — 왕복 검증 쿼리를 표시될 건에만
    쓰기 위해 분리했다.
    """
    from collections import defaultdict
    by_route: dict[str, list[Alert]] = defaultdict(list)
    for a in alerts:
        by_route[a.combo.route.key].append(a)
    picked: list[Alert] = []
    for items in by_route.values():
        items.sort(key=lambda a: a.combo.price)
        picked += items[: cfg.bundle_top_n]
    return picked


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
    """관측 기간 중 하루 1회 상황 메시지. 콤보(기준가)가 있으면 기준가 요약,
    아직 없으면 수집 현황을 보낸다 — 침묵으로 시스템 생사를 알 수 없는 상태 방지."""
    if not is_observing(cfg, state, today):
        return None
    if state.meta.get("last_obs_report") == today.isoformat():
        return None
    state.meta["last_obs_report"] = today.isoformat()

    labels = {r.key: r.label for r in cfg.routes}
    day_n = (today - state.first_run_date(today)).days + 1
    head = (f"📡 <b>관측 {day_n}일차</b> "
            f"(총 {cfg.observation_days}일, 이후 특가 알림 시작)")

    if state.baselines:  # 콤보가 생겨 기준가 형성 중
        lines = [head + " — 기준가 형성 중"]
        for unit in sorted(state.baselines):
            b = state.baselines[unit]
            if "baseline" not in b:
                continue
            route_key, month = unit.split("|")
            m = int(month.split("-")[1])
            lines.append(f"· {labels.get(route_key, route_key)} {m}월: "
                         f"현재 최저 ₩{b['baseline']:,}")
        if len(lines) > 1:
            return "\n".join(lines)

    # 콤보가 아직 없음 → 편도 수집 현황으로 대체
    per_route: dict[str, list[int]] = {r.key: [0, 0] for r in cfg.routes}  # [가격확보, 조회완료]
    for key, e in state.legs.items():
        rk = key.split("|")[0]
        if rk not in per_route:
            continue
        per_route[rk][1] += 1
        if e.get("price") is not None:
            per_route[rk][0] += 1
    total = sum(v[0] for v in per_route.values())
    lines = [head + " — 편도 가격 수집 중",
             f"수집된 편도 최저가 {total}건 · 왕복 조합은 짝이 모이면 자동 생성"]
    for rk in sorted(per_route, key=lambda k: -per_route[k][0]):
        got, seen = per_route[rk]
        note = " ⚠️ 데이터 없음" if seen >= 5 and got == 0 else ""
        lines.append(f"· {labels[rk]}: {got}건{note}")
    return "\n".join(lines)


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
