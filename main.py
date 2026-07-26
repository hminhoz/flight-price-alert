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

    # DRY_RUN 값으로 세 가지 모드를 가른다 (워크플로우 수정 없이 쓰기 위함):
    #   빈값     → 실전
    #   "1"      → 테스트 (전송·저장 없음)
    #   "preview"→ 미리보기 (기준가 무시하고 현재 최저가를 실제로 1회 전송, 저장 없음)
    _mode = (os.environ.get("DRY_RUN") or "").strip().lower()
    dry = _mode == "1"
    preview = _mode == "preview"

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
    done_n, total_n, cycle_done = engine.note_shard(cfg, state, shard)
    log.info("shard=%d 검색 대상 %d개 leg · 이번 바퀴 진행 %d/%d%s",
             shard, len(legs), done_n, total_n, " (완주)" if cycle_done else "")
    for r in cfg.routes:
        if cfg.has_window_override(r.key):
            for d, ko in (("out", "가는 편"), ("ret", "오는 편")):
                fb = cfg.fallback_window_for(r.key, d)
                if fb:
                    pref = cfg.window_for(r.key, d)
                    log.info("  넓힌 시간창 %s %s: %s~%s (선호 %s~%s 밖이면 표시)",
                             r.key, ko, fb[0].strftime("%H:%M"),
                             fb[1].strftime("%H:%M"),
                             pref[0].strftime("%H:%M"), pref[1].strftime("%H:%M"))

    # ---- 검색 ----
    from collections import defaultdict
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # [가격확보, 조건불일치, 실패, 데이터없음]
    last_errors: dict[str, str] = {}
    attempted = failed = 0

    def work(leg):
        """네트워크 작업만 담당. 상태 기록은 메인 스레드에서 한다."""
        polite_delay(cfg.request_delay_sec)
        preferred = cfg.window_for(leg.route.key, leg.direction)
        widened = cfg.fallback_window_for(leg.route.key, leg.direction)
        try:
            return leg, search_leg(
                leg.origin, leg.dest, leg.date.isoformat(),
                adults=cfg.adults, window=widened or preferred,
                preferred_window=preferred if widened else None,
                currency=cfg.currency,
                direct_only=cfg.direct_only, retries=cfg.retry,
            ), None
        except Exception as e:  # noqa: BLE001 - 분류는 아래에서
            return leg, None, e

    def absorb(leg, res, err):
        nonlocal failed
        if err is None:
            if res:
                state.record_leg(leg.key, price=res.price, airline=res.airline,
                                 dep_time=res.dep_time, arr_time=res.arr_time,
                                 carrier=res.carrier, off_window=res.off_window)
                stats[leg.route.key][0] += 1
            else:
                state.record_leg(leg.key, price=None)
                stats[leg.route.key][1] += 1
        elif isinstance(err, NoFlightData):
            state.record_leg(leg.key, price=None)
            stats[leg.route.key][3] += 1
        elif isinstance(err, SearchError):
            failed += 1
            stats[leg.route.key][2] += 1
            last_errors[leg.route.key] = str(err)[:140]
        else:
            failed += 1
            stats[leg.route.key][2] += 1
            last_errors[leg.route.key] = f"{type(err).__name__}: {str(err)[:120]}"

    if cfg.concurrency > 1 and len(legs) > 1:
        # 상태 기록은 메인 스레드가 순차로 하므로 자료구조 경합이 없다.
        from concurrent.futures import ThreadPoolExecutor
        log.info("동시 검색 %d개로 진행", cfg.concurrency)
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            for leg, res, err in pool.map(work, legs):
                attempted += 1
                absorb(leg, res, err)
    else:
        for leg in legs:
            attempted += 1
            absorb(*work(leg))

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

    # 시간창 점검용: 실제 직항편이 몇 시에 몰려 있고, 지금 창이 몇 %를 담는지.
    # override를 감이 아니라 데이터로 정하기 위한 근거 (v1.32).
    from app.search import time_histogram
    hist = time_histogram()
    if hist:
        log.info("시간 분포 (직항 출발 시각 · 현재 시간창이 담는 비율)")
        for (o, d), hours in sorted(hist.items()):
            route = next((r for r in cfg.routes
                          if (r.origin, r.destination) == (o, d)), None)
            direction = "out"
            if route is None:
                route = next((r for r in cfg.routes
                              if (r.destination, r.origin) == (o, d)), None)
                direction = "ret"
            if route is None:
                continue
            lo, hi = cfg.window_for(route.key, direction)
            total = sum(hours.values())
            inside = sum(c for h, c in hours.items() if lo.hour <= h <= hi.hour)
            bar = " ".join(f"{h:02d}시:{hours[h]}" for h in sorted(hours))
            log.info("  %s-%s(%s) 창 %02d~%02d시 · %d/%d편(%.0f%%) · %s",
                     o, d, "가는편" if direction == "out" else "오는편",
                     lo.hour, hi.hour, inside, total,
                     inside / total * 100 if total else 0, bar)

    # ---- 판정 & 알림 ----
    combos = engine.build_combos(cfg, state, today)
    alerts = engine.process(cfg, state, combos, today)
    log.info("콤보 %d개, 알림 후보 %d건", len(combos), len(alerts))

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
    def verify_roundtrips(alerts_):
        if not (alerts_ and cfg.verify_roundtrip) or dry_skip_network():
            return
        targets = engine.display_selection(cfg, alerts_)[: cfg.verify_max_queries]
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
                # 부호 주의: 왕복이 비싸면 +, 싸면 -. 예전엔 반대로 계산해놓고
                # "낮음"이라고 찍어 -59%가 '싸다'로 읽혔다 (v1.31 수정).
                gap = (a.rt_price - c.price) / max(c.price, 1) * 100
                log.info("RTVERIFY %s %s %d박: 편도2장 %d / 왕복티켓 %d (왕복이 %+.0f%%)",
                         c.route.key, c.dep, c.nights, c.price, a.rt_price, gap)
            polite_delay(cfg.request_delay_sec)
        log.info("왕복 검증: %d건 중 %d건 확보", len(targets), ok)

    # ---- 미리보기 모드: 기준가와 무관하게 지금 최저가를 실제로 보내본다 ----
    if preview:
        pv = engine.preview_alerts(cfg, combos)
        log.info("미리보기 모드: 노선별 최저 %d건 전송 예정", len(pv))
        if not pv:
            log.info("콤보가 아직 없어 보낼 것이 없음")
            return 0
        verify_roundtrips(pv)
        notify.send("🔎 <b>미리보기</b>\n"
                    "기준가와 무관하게 <b>지금 최저가</b>를 보여드립니다.\n"
                    "실제 특가 알림이 아니고, 기록도 남기지 않습니다.")
        for msg in notify.format_alerts(cfg, pv, combos):
            notify.send(msg)
        log.info("미리보기 전송 완료 · 상태 저장하지 않음")
        return 0

    verify_roundtrips(alerts)

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
        # 노선별 최저가 1건씩. 전체 최저 3건만 뽑으면 한 노선에 몰려서
        # (2026-07-25 1차 점검이 전부 나고야였음) 노선 간 차이를 못 본다.
        cheapest_per_route: dict[str, object] = {}
        for c in combos:
            cur = cheapest_per_route.get(c.route.key)
            if cur is None or c.price < cur.price:
                cheapest_per_route[c.route.key] = c
        ok = 0
        for c in sorted(cheapest_per_route.values(), key=lambda x: x.route.key):
            rt = search_roundtrip(
                c.route.origin, c.route.destination,
                c.dep.isoformat(), c.ret.isoformat(),
                adults=cfg.adults,
                out_window=cfg.window_for(c.route.key, "out"),
                currency=cfg.currency, direct_only=cfg.direct_only,
                diag=True,
            )
            if rt:
                ok += 1
                gap = (rt - c.price) / c.price * 100
                log.info("  %s %s~%s %d박: 편도2장 %d / 왕복티켓 %d (왕복이 %+.0f%%)",
                         c.route.key, c.dep, c.ret, c.nights, c.price, rt, gap)
            else:
                log.info("  %s %s~%s %d박: 왕복 조회 실패 (편도합산 %d)",
                         c.route.key, c.dep, c.ret, c.nights, c.price)
            polite_delay(cfg.request_delay_sec)
        log.info("=== 사전 점검 결과: %d건 중 %d건 확보 ===",
                 len(cheapest_per_route), ok)

    # 한 바퀴(전 조합 1회 훑기) 완료 보고
    if cycle_done:
        msg = engine.cycle_report(cfg, state, today, len(engine.all_legs(cfg, today)))
        if msg:
            deliver(msg)
            log.info("한 바퀴 완료 보고 전송")
        else:
            log.info("한 바퀴 완료 (보고는 정책상 생략)")

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
