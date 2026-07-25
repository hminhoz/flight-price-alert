"""항공권 최저가 알림 - 실행 진입점.

환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (필수, GitHub Secrets)
  SHARD_OVERRIDE  이번 실행이 담당할 샤드 강제 지정 (테스트용)
  DRY_RUN=1       텔레그램 전송 대신 콘솔 출력
  MAX_LEGS        이번 실행 검색 상한 (테스트용)
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys

from app import engine, notify
from app.search import (NoFlightData, SearchError, polite_delay, search_leg,
                        search_roundtrip)
from app.settings import load
from app.state import State

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")


def main() -> int:
    cfg = load()
    state = State()
    now = dt.datetime.now(dt.timezone.utc)
    # 한국 기준 날짜로 동작 (출발 임박 판정 등)
    today = (now + dt.timedelta(hours=9)).date()

    if today > cfg.period_end:
        log.info("여행 가능 기간 종료. 할 일 없음.")
        return 0

    state.prune_past_legs(today)
    state.first_run_date(today)

    # 샤드는 실행 시각이 아닌 저장된 커서로 순환: GitHub이 예약 슬롯을 건너뛰거나
    # 지연시켜도 "실행할 때마다 다음 샤드"라서 검색 누락이 생기지 않는다. (v1.6)
    if "SHARD_OVERRIDE" in os.environ:
        shard = int(os.environ["SHARD_OVERRIDE"]) % cfg.shards
    else:
        shard = (int(state.meta.get("shard_cursor", -1)) + 1) % cfg.shards
    state.meta["shard_cursor"] = shard
    legs = engine.legs_for_run(cfg, today, shard)
    max_legs = os.environ.get("MAX_LEGS")
    if max_legs:
        legs = legs[: int(max_legs)]
    log.info("shard=%d 검색 대상 %d개 leg", shard, len(legs))
    for r in cfg.routes:
        if cfg.has_window_override(r.key):
            o, rt = cfg.window_for(r.key, "out"), cfg.window_for(r.key, "ret")
            log.info("  시간창 override %s: 가는 편 %s~%s / 오는 편 %s~%s", r.key,
                     o[0].strftime("%H:%M"), o[1].strftime("%H:%M"),
                     rt[0].strftime("%H:%M"), rt[1].strftime("%H:%M"))

    # ---- 검색 ----
    from collections import defaultdict
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # [가격확보, 조건불일치, 실패, 데이터없음]
    last_errors: dict[str, str] = {}
    attempted = failed = 0
    for leg in legs:
        attempted += 1
        window = cfg.window_for(leg.route.key, leg.direction)
        try:
            res = search_leg(
                leg.origin, leg.dest, leg.date.isoformat(),
                adults=cfg.adults, window=window, currency=cfg.currency,
                direct_only=cfg.direct_only, retries=cfg.retry,
            )
            if res:
                state.record_leg(leg.key, price=res.price, airline=res.airline,
                                 dep_time=res.dep_time, arr_time=res.arr_time)
                stats[leg.route.key][0] += 1
            else:
                state.record_leg(leg.key, price=None)
                stats[leg.route.key][1] += 1
        except NoFlightData:
            state.record_leg(leg.key, price=None)
            stats[leg.route.key][3] += 1
        except SearchError as e:
            failed += 1
            stats[leg.route.key][2] += 1
            last_errors[leg.route.key] = str(e)[:140]
        polite_delay(cfg.request_delay_sec)

    state.record_run_stats(attempted=attempted, failed=failed)
    log.info("검색 완료: %d 시도 → 가격확보 %d / 조건불일치 %d / 실패 %d / 데이터없음 %d",
             attempted, sum(v[0] for v in stats.values()), sum(v[1] for v in stats.values()),
             failed, sum(v[3] for v in stats.values()))
    for rk in sorted(stats):
        got, miss, ng, nd = stats[rk]
        log.info("  %-8s 가격확보 %3d · 조건불일치 %3d · 실패 %3d · 데이터없음 %3d",
                 rk, got, miss, ng, nd)
    for rk, msg in sorted(last_errors.items()):
        log.info("  ⤷ %s 마지막 에러: %s", rk, msg)

    # ---- 판정 & 알림 ----
    combos = engine.build_combos(cfg, state, today)
    alerts = engine.process(cfg, state, combos, today)
    log.info("콤보 %d개, 알림 후보 %d건", len(combos), len(alerts))

    dry = os.environ.get("DRY_RUN") == "1"

    def dry_skip_network() -> bool:
        """DRY_RUN 중에도 왕복 검증은 실제로 돌려봐야 진단이 되므로 기본은 실행.
        NO_VERIFY=1 이면 건너뛴다 (오프라인 테스트용)."""
        return os.environ.get("NO_VERIFY") == "1"

    def deliver(msg: str) -> bool:
        if dry:
            print("\n----- DRY RUN 메시지 -----\n" + msg)
            return True
        return notify.send(msg)

    # 최초 가동 시 시작 메시지 1회 (텔레그램 연결 즉시 검증 목적)
    if not state.meta.get("startup_sent"):
        n_legs = len(engine.all_legs(cfg, today))
        if deliver(
            "🚀 <b>항공권 최저가 알림 가동 시작</b>\n"
            f"노선 {len(cfg.routes)}개 · 감시 조합 {n_legs}개 leg\n"
            f"첫 {cfg.observation_days}일은 관측 기간이라 매일 요약만 보내고, "
            "이후 특가 알림이 시작됩니다."
        ):
            state.meta["startup_sent"] = True

    # ---- 왕복 실가 검증 (v1.12) ----
    # 판정은 편도 합산 기준 그대로. 메시지에 노출될 조합만 왕복으로 재조회해
    # 표시 금액을 실구매가에 가깝게 만든다. 실패해도 알림은 그대로 나간다.
    if alerts and cfg.verify_roundtrip and not dry_skip_network():
        targets = engine.display_selection(cfg, alerts)[: cfg.verify_max_queries]
        ok = 0
        for a in targets:
            c = a.combo
            a.rt_price = search_roundtrip(
                c.route.origin, c.route.destination,
                c.dep.isoformat(), c.ret.isoformat(),
                adults=cfg.adults,
                out_window=cfg.window_for(c.route.key, "out"),
                currency=cfg.currency, direct_only=cfg.direct_only,
            )
            if a.rt_price:
                ok += 1
                gap = (c.price - a.rt_price) / c.price * 100
                log.info("RTVERIFY %s %s %d박: 편도합산 %d → 왕복실가 %d (%.0f%% 낮음)",
                         c.route.key, c.dep, c.nights, c.price, a.rt_price, gap)
            polite_delay(cfg.request_delay_sec)
        log.info("왕복 검증: %d건 중 %d건 확보", len(targets), ok)

    if alerts:
        sent_ok = True
        for msg in notify.format_alerts(cfg, alerts, combos):
            sent_ok = deliver(msg) and sent_ok
        if sent_ok:
            engine.mark_sent(state, alerts)  # 전송 실패 시 다음 실행에서 재시도

    # 관측 기간 중에는 하루 1회 형성 중인 기준가 요약 전송
    obs = engine.observation_report(cfg, state, today)
    if obs:
        deliver(obs)

    # 관측 기간이라 알림이 아직 없을 때, DRY_RUN 실행이면 왕복 조회가 실제로
    # 되는지 미리 확인한다. 7/28 첫 알림까지 기다리지 않고 검증하기 위함 (v1.12.1).
    # 전송·저장 없이 로그만 남긴다.
    if dry and not alerts and combos and cfg.verify_roundtrip:
        log.info("=== 왕복 검증 사전 점검 (DRY_RUN) ===")
        ok = 0
        for c in sorted(combos, key=lambda x: x.price)[:3]:
            rt = search_roundtrip(
                c.route.origin, c.route.destination,
                c.dep.isoformat(), c.ret.isoformat(),
                adults=cfg.adults,
                out_window=cfg.window_for(c.route.key, "out"),
                currency=cfg.currency, direct_only=cfg.direct_only,
            )
            if rt:
                ok += 1
                log.info("  %s %s~%s %d박: 편도합산 %d → 왕복실가 %d (%.0f%% 낮음)",
                         c.route.key, c.dep, c.ret, c.nights, c.price, rt,
                         (c.price - rt) / c.price * 100)
            else:
                log.info("  %s %s~%s %d박: 왕복 조회 실패 (편도합산 %d)",
                         c.route.key, c.dep, c.ret, c.nights, c.price)
            polite_delay(cfg.request_delay_sec)
        log.info("=== 사전 점검 결과: 3건 중 %d건 확보 ===", ok)

    err = engine.failure_alert_needed(cfg, state)
    if err:
        deliver(err)

    # DRY_RUN은 아무것도 바꾸지 않는다 (v1.12.2 버그 수정).
    # 이전에는 테스트 실행이 상태를 저장해서 두 가지 사고를 냈다:
    #   1. 관측 리포트를 "보냈음"으로 기록 → 그날 실제 텔레그램 리포트가 누락
    #   2. 알림을 "전송했음"으로 기록 → 진짜 실행이 중복으로 보고 건너뜀
    if dry:
        log.info("DRY_RUN: 상태를 저장하지 않고 종료 (수집분 폐기, 다음 실행에서 재수집)")
        return 0

    state.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
