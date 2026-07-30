"""가격 감시 핵심 로직 (네트워크 없음 → 단위 테스트 가능).

용어:
  leg   = (노선, 방향 out/ret, 날짜) 편도 검색 단위
  combo = (노선, 출발일, N박) 실제 여행 조합. 가격 = out leg + ret leg 합산
  unit  = 기준가 단위 = 노선 × 출발월 ("ICN-KIX|2026-09")
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import statistics
from dataclasses import dataclass

from .settings import Route, Settings

log = logging.getLogger(__name__)
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
    route: Route                 # 가는 편 노선
    dep: dt.date
    nights: int
    price: int
    out_leg: dict
    ret_leg: dict
    ret_route: Route | None = None   # 오는 편 노선. None이면 route와 동일(왕복)
    city: str = ""                   # 기준가 단위용 도시 키 (build_combos가 채운다)
    rt_price: int | None = None      # 왕복 실가 (확인된 조합만)

    @property
    def pay(self) -> int:
        """실제로 낼 금액 = 편도 2장과 왕복권 중 싼 쪽.

        일본발 편도는 현지 시장가라 왕복보다 훨씬 비싸다(실측 중앙값 1.4배,
        최대 13배). 편도 합산만 보면 표시가가 실구매가와 크게 벌어지고,
        **왕복이 싼 조합은 순위에서 밀려 후보에도 못 오른다** (v2.09).
        """
        return min(self.price, self.rt_price) if self.rt_price else self.price

    @property
    def back(self) -> Route:
        return self.ret_route or self.route

    @property
    def is_cross(self) -> bool:
        """오는 편 노선이 가는 편과 다른 교차 조합인가.

        주의: ret_leg는 노선 키 기준으로 저장돼 실제 비행 방향이 반대다.
        (GMP-NGO의 ret leg = NGO→GMP). 그래서 방향을 비교하지 말고
        ret_route를 채웠는지로만 판정한다 — 같은 노선이면 None이다.
        """
        return self.ret_route is not None

    @property
    def label(self) -> str:
        if not self.is_cross:
            return self.route.label
        b = self.back
        # 실제 비행 방향으로 적는다: 가는 편 origin→dest, 오는 편 dest→origin
        return (f"{self.route.origin}→{self.route.destination} / "
                f"{b.destination}→{b.origin}")

    @property
    def key(self) -> str:
        b = self.back
        rk = self.route.key if not self.is_cross else f"{self.route.key}>{b.key}"
        return f"{rk}|{self.dep.isoformat()}|{self.nights}n"

    @property
    def unit(self) -> str:
        """기준가 단위 = **도시 × 월**.

        예전엔 노선·교차조합마다 따로 잡아 오사카 9월 하나에 단위가 4개였다
        (인천왕복·김포왕복·교차 2종). 각자 기준가를 가지니 비싼 조합이
        "그 단위 기준 13% 싸다"며 알림이 나갔고, 정작 더 싼 조합은 조용했다.
        사용자는 '오사카'로 보는데 시스템이 넷으로 쪼개 본 것이다 (v2.07).
        """
        city = self.city or self.route.destination
        return f"{city}|{self.dep.strftime('%Y-%m')}"

    @property
    def ret(self) -> dt.date:
        return self.dep + dt.timedelta(days=self.nights)


def _seoul_group(cfg: Settings, route: Route) -> str:
    """목적지 도시 키. config의 city_groups로 하네다·나리타 같은 복수 공항을 묶는다."""
    for city, airports in (cfg.city_groups or {}).items():
        if route.destination in airports:
            return city
    return route.destination


def city_label(cfg: Settings, route: Route) -> str:
    """메시지 제목용 도시 이름. '인천-나고야' → '나고야'."""
    for city, airports in (cfg.city_groups or {}).items():
        if route.destination in airports:
            return city
    lb = route.label or route.destination
    return lb.split("-")[-1] if "-" in lb else lb


def build_combos(cfg: Settings, state: State, today: dt.date) -> list[Combo]:
    """왕복 조합 + (허용 시) 출발/도착 공항 교차 조합.

    교차 조합이 왜 필요한가 (v1.41):
      서울 사람에게 인천·김포는 바꿔 쓸 수 있는 공항이다. 실측(2026-07-26)에서
      `김포→나고야 / 나고야→인천` 조합이 같은 공항 왕복보다 1인 최대 184,600원
      쌌다. 김포발 피치가 저렴한데 나고야→김포는 18시 이후 편이 없어, 같은
      공항끼리만 묶으면 그 싼 가는 편이 통째로 버려지기 때문이다.
      교차하면 가격도 싸고 귀국 시각도 선호대로(18시 이후) 맞출 수 있다.
      **추가 검색은 0건** — 네 방향 다리를 이미 모두 수집하고 있다.
    """
    fresh = cfg.leg_freshness_days
    combos: list[Combo] = []
    excluded = set(cfg.exclude_airlines or ())
    nv = getattr(state, "naver_legs", None) or {}
    # 구글 leg는 leg_freshness_days가 지나면 무시되는데 네이버는 검사가 없어
    # 한 번 모으면 영원히 쓰였다. 수집이 며칠 실패하면 사라진 가격으로 알림이
    # 나간다 → 같은 방식으로 만료시킨다 (v1.91).
    _nv_cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=cfg.naver_freshness_days)).isoformat()

    def cheaper(key: str, leg: dict | None) -> dict | None:
        """같은 다리에 구글·네이버가 다 있으면 싼 쪽을 쓰되, **진 쪽 가격도
        남긴다**. 네이버 특가석은 환불·변경 제약이 있어 더 비싸도 구글 쪽을
        고르고 싶을 수 있다. 판단 재료를 없애지 않는다 (v1.70).
        """
        n = nv.get(key)
        if not n or not n.get("price"):
            return leg
        if (n.get("at") or "") < _nv_cutoff:
            return leg          # 오래된 네이버 값은 쓰지 않는다
        if not leg or not leg.get("price"):
            out = dict(n)
            out.setdefault("source", "naver")
            return out
        win, lose = (leg, n) if leg["price"] <= n["price"] else (n, leg)
        out = dict(win)
        out.setdefault("source", "naver" if win is n else "google")
        out["alt_price"] = lose["price"]
        out["alt_source"] = "naver" if lose is n else "google"
        out["alt_seat"] = lose.get("seat", "")
        return out

    def blocked(leg: dict) -> bool:
        """설정 변경 직후에도 즉시 반영되도록, 저장된 다리도 여기서 거른다.
        (수집 단계에서 이미 빼지만 기존 데이터가 최대 leg_freshness_days 남는다)"""
        if not excluded:
            return False
        return (leg.get("carrier", "") or "").strip().upper() in excluded

    # (도시, 출발일) -> [(route, leg)] / (도시, 귀국일) -> [(route, leg)]
    outs: dict[tuple, list] = {}
    rets: dict[tuple, list] = {}

    for route in cfg.routes:
        city = _seoul_group(cfg, route)
        d = max(cfg.period_start, today)
        while d <= cfg.period_end:
            if not is_excluded_departure(cfg, d):
                o = state.fresh_leg_price(
                    State.leg_key(route.key, "out", d.isoformat()), fresh)
                o = cheaper(State.leg_key(route.key, "out", d.isoformat()), o)
                if o and not blocked(o):
                    outs.setdefault((city, d), []).append((route, o))
            r = state.fresh_leg_price(
                State.leg_key(route.key, "ret", d.isoformat()), fresh)
            r = cheaper(State.leg_key(route.key, "ret", d.isoformat()), r)
            if r and not blocked(r):
                rets.setdefault((city, d), []).append((route, r))
            d += dt.timedelta(days=1)

    for (city, d), out_list in outs.items():
        for n in cfg.trip_nights:
            r_date = d + dt.timedelta(days=n)
            if r_date > cfg.period_end:
                continue
            for out_route, out_leg in out_list:
                for ret_route, ret_leg in rets.get((city, r_date), []):
                    same = (ret_route.key == out_route.key)
                    if not same and not cfg.cross_airports:
                        continue
                    combos.append(Combo(
                        route=out_route, dep=d, nights=n,
                        price=out_leg["price"] + ret_leg["price"],
                        out_leg=out_leg, ret_leg=ret_leg,
                        ret_route=None if same else ret_route,
                        city=city,
                    ))
    return combos


# ---------------------------------------------------------------- 기준가 & 알림

@dataclass
class Alert:
    kind: str          # "baseline" | "record"
    combo: Combo
    baseline: int
    prev_min: int | None
    # v1.26: 이 조합으로 직전에 알림 보냈던 가격. 없으면 이번이 첫 알림.
    prev_sent: int | None = None
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


def verify_targets(cfg: Settings, combos: list[Combo]) -> list[Combo]:
    """왕복 실가를 확인할 조합.

    편도 합산 순위만 보면 안 된다 — 오는 편 편도가 폭등한 조합은 합산이 커서
    뒤로 밀리지만 **왕복으로는 가장 쌀 수 있다**(실측: 나고야 8/8 편도합산
    1인 52만 vs 구글 왕복 18만). 두 갈래로 뽑는다:
      · **배율(오는편/가는편)이 임계 이상인 것 전부** — 왜곡이 실재하는 조합
      · 도시별 편도합산 최저 몇 개 — 이미 후보인 조합의 실가 확인
    배율 분포 실측: 1.5배↑ 45% · 2배↑ 21% · 3배↑ 7% (전체 833건 기준)
    """
    from collections import defaultdict

    def ratio(c: Combo) -> float:
        o = c.out_leg.get("price") or 1
        return (c.ret_leg.get("price") or 0) / o

    pool = [c for c in combos if not c.is_cross]
    picked: dict[int, Combo] = {}

    for c in pool:
        if ratio(c) >= cfg.verify_skew_ratio:
            picked[id(c)] = c

    by_city: dict[str, list[Combo]] = defaultdict(list)
    for c in pool:
        by_city[c.unit.split("|")[0]].append(c)
    for items in by_city.values():
        for c in sorted(items, key=lambda x: x.price)[: cfg.verify_per_city]:
            picked[id(c)] = c

    out = sorted(picked.values(), key=lambda c: -ratio(c))
    return out[: cfg.verify_max_queries]


def display_selection(cfg: Settings, alerts: list[Alert]) -> list[Alert]:
    """알림 메시지 본문에 실제로 노출될 알림들 (노선별 저가 top N).

    notify.format_alerts 와 동일한 선별 규칙 — 왕복 검증 쿼리를 표시될 건에만
    쓰기 위해 분리했다.
    """
    from collections import defaultdict
    by_route: dict[str, list[Alert]] = defaultdict(list)
    for a in alerts:
        by_route[_seoul_group(cfg, a.combo.route)].append(a)
    picked: list[Alert] = []
    for items in by_route.values():
        items.sort(key=lambda a: a.combo.price)
        picked += items[: cfg.bundle_top_n]
    return picked


_METRIC = "pay-v2"   # 기준가가 어떤 금액 기준인지. 바뀌면 전부 다시 심는다.

# 발송 기록이 오염된 시점을 표시한다. 이 값이 다르면 한 번 비운다.
# v2.19 이전에는 **표시되지도 않은 알림 후보 전부**를 '보냄'으로 기록해,
# 메시지에 안 나간 조합이 재알림 문턱에 걸려 조용히 억제됐다 (누적 216건 중
# 대부분이 그런 기록이었다).
_SENT_EPOCH = "v2.20-shown-only"


def process(cfg: Settings, state: State, combos: list[Combo],
            today: dt.date) -> list[Alert]:
    """기준가 갱신 + 알림 판정. 관측 기간에는 수집만 하고 빈 리스트 반환."""
    first_run = state.first_run_date(today)
    observing = today < first_run + dt.timedelta(days=cfg.observation_days)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # 1) unit별 오늘의 최저 콤보가격 기록
    # 지표가 바뀌면(편도합산 → 실제 낼 금액) 기존 기준가는 잣대가 다르다.
    # 그대로 두면 갑자기 전부 '싸졌다'가 되어 알림이 쏟아진다 (v2.09).
    if state.meta.get("sent_epoch") != _SENT_EPOCH:
        n = len(state.alerts_sent)
        state.alerts_sent.clear()
        state.meta["sent_epoch"] = _SENT_EPOCH
        log.info("발송 기록 초기화: 표시되지 않은 기록 %d건 폐기 (%s)",
                 n, _SENT_EPOCH)

    if state.meta.get("baseline_metric") != _METRIC:
        state.baselines.clear()
        state.meta["baseline_metric"] = _METRIC
        log.info("기준가 지표 변경(%s) → 전부 새로 심는다", _METRIC)

    todays_min: dict[str, int] = {}
    for c in combos:
        todays_min[c.unit] = min(todays_min.get(c.unit, c.pay), c.pay)

    for unit, price in todays_min.items():
        # 단위가 바뀌면(v2.07 도시 통합) 기준가가 전부 새로 생긴다. 그때 전부
        # "새로 찾은 조합"으로 알리면 수십 통이 쏟아지므로 이번 실행은 심기만 한다.
        fresh_unit = unit not in state.baselines
        b = state.baselines.setdefault(unit, {"daily_min": {}})
        if fresh_unit:
            b["_seeded"] = today.isoformat()
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
        # 기준가는 최근 daily_min 중앙값으로 내려간다. 다만 **영영 도달 불가능한
        # 값에 갇히면 알림이 조용해진다** — 예: 네이버가 만든 낮은 값에 기준가가
        # 박힌 뒤 네이버 수집이 끊기면 그 단위는 다시 울리지 않는다.
        # → 최근 창 전체가 기준가를 한 번도 못 건드렸으면 중앙값까지 올린다 (v1.92).
        if observing or "baseline" not in b:
            b["baseline"] = min(b.get("baseline", price), price)
        else:
            recent = list(b["daily_min"].values())
            if recent:
                candidate = int(statistics.median(recent))
                if candidate < b["baseline"]:
                    b["baseline"] = candidate
                elif (len(recent) >= cfg.baseline_unstick_days
                      and min(recent) > b["baseline"]):
                    # 창 안의 어느 날도 기준가에 못 닿았다 → 시세가 올라간 것
                    b["baseline"] = candidate
                    b["unstuck_at"] = dt.datetime.now(
                        dt.timezone.utc).isoformat(timespec="seconds")

    if observing:
        return []

    # 2) 알림 판정
    alerts: list[Alert] = []
    for c in combos:
        b = state.baselines.get(c.unit)
        if not b or "baseline" not in b:
            continue
        if b.get("_seeded") == today.isoformat():
            continue          # 오늘 처음 생긴 단위 — 비교 대상이 없다
        is_record = c.pay <= b["alltime_min"] and b.get("alltime_min_at") == now_iso \
            and todays_min.get(c.unit) == c.pay
        # 기준가와 같기만 해도 알리면 '특가 아닌 알림'이 대부분이 된다 (v1.42).
        # 새로 생긴 unit은 기준가 = 자기 가격이라 특히 그렇다.
        is_below_baseline = c.pay <= b["baseline"] * (
            1 - cfg.min_below_baseline_pct / 100)
        if not (is_record or is_below_baseline):
            continue
        # 중복 억제: 직전 알림가보다 min_redrop_pct 이상 더 싸졌을 때만 재알림.
        # (예전엔 1원만 내려도 다시 보내서 같은 조합이 반복 노출됐다)
        sent = state.alerts_sent.get(c.key)
        if sent:
            need = sent["price"] * (1 - cfg.min_redrop_pct / 100)
            if c.pay > need:
                continue
        alerts.append(Alert(
            kind="record" if is_record else "baseline",
            combo=c,
            baseline=b["baseline"],
            prev_min=b.get("alltime_min_prev"),
            prev_sent=sent["price"] if sent else None,
        ))

    # 3) 전송 확정분 기록 (실 전송은 notify 성공 후 caller가 확정해도 되지만
    #    실패 시 다음 실행에서 재시도되는 편이 안전해 여기서는 기록만 준비)
    return alerts


def mark_sent(state: State, alerts: list[Alert]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for a in alerts:
        state.alerts_sent[a.combo.key] = {"price": a.combo.pay, "at": now}


# ---------------------------------------------------------------- 관측기간 리포트

def is_observing(cfg: Settings, state: State, today: dt.date) -> bool:
    first_run = state.first_run_date(today)
    return today < first_run + dt.timedelta(days=cfg.observation_days)


# ---------------------------------------------------------------- 한 바퀴 진행도

def note_shard(cfg: Settings, state: State, shard: int) -> tuple[int, int, bool]:
    """이번 실행의 샤드를 진행도에 기록.

    Returns: (이번 바퀴에 훑은 샤드 수, 전체 샤드 수, 방금 한 바퀴를 마쳤는가)

    샤드 커서는 매 실행 +1 되므로 shards번이면 전 조합을 한 번씩 본다. 다만
    실행이 생략·중복될 수 있어 단순 카운트 대신 '본 샤드 집합'으로 판정한다.
    """
    seen = set(state.meta.get("cycle_shards") or [])
    if not seen:
        state.meta["cycle_started_at"] = dt.datetime.now(
            dt.timezone.utc).isoformat(timespec="seconds")
    seen.add(shard)
    done = len(seen) >= cfg.shards
    state.meta["cycle_shards"] = [] if done else sorted(seen)
    if done:
        state.meta["last_cycle_done_at"] = dt.datetime.now(
            dt.timezone.utc).isoformat(timespec="seconds")
    return (cfg.shards if done else len(seen)), cfg.shards, done


def cycle_report(cfg: Settings, state: State, today: dt.date,
                 total_legs: int) -> str | None:
    """한 바퀴 완료 시각 헤더. 본문(도시별 최저 조합)은 notify가 붙인다.

    None이면 정책상 보내지 않는다는 뜻.
    """
    policy = cfg.cycle_report
    if policy == "off":
        return None
    if policy == "daily":
        if state.meta.get("last_cycle_report") == today.isoformat():
            return None
        # 한국 시각 기준 digest_hour 이후 첫 완주에만 보낸다.
        # 예전엔 '날짜가 바뀐 뒤 첫 완주'라 새벽 1시쯤 울렸다 (v1.46).
        kst_hour = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).hour
        if kst_hour < cfg.digest_hour:
            return None
    state.meta["last_cycle_report"] = today.isoformat()

    started = state.meta.get("cycle_started_at")
    took = ""
    if started:
        try:
            d = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(started)
            mins = int(d.total_seconds() // 60)
            took = (f" · {mins // 60}시간 {mins % 60}분 걸림" if mins >= 60
                    else f" · {mins}분 걸림")
        except ValueError:
            pass
    return f"{total_legs:,}개 편도를 모두 확인했습니다{took}"


def _baseline_lines(cfg: Settings, state: State) -> list[str]:
    """노선·월별 현재 최저가 목록 (1인당)."""
    labels = {r.key: r.label for r in cfg.routes}
    out = []
    for unit in sorted(state.baselines):
        b = state.baselines[unit]
        if "baseline" not in b:
            continue
        route_key, month = unit.split("|")
        m = int(month.split("-")[1])
        per = round(b["baseline"] / max(cfg.adults, 1))
        out.append(f"· {labels.get(route_key, route_key)} {m}월: ₩{per:,}/인")
    return out


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
