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
from app.search import SearchError, polite_delay, search_leg
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

    shard = int(os.environ.get("SHARD_OVERRIDE",
                engine.current_shard_from_hour(now.hour, cfg.shards)))
    legs = engine.legs_for_run(cfg, today, shard)
    max_legs = os.environ.get("MAX_LEGS")
    if max_legs:
        legs = legs[: int(max_legs)]
    log.info("shard=%d 검색 대상 %d개 leg", shard, len(legs))

    # ---- 검색 ----
    attempted = failed = 0
    for leg in legs:
        attempted += 1
        window = cfg.outbound_window if leg.direction == "out" else cfg.return_window
        try:
            res = search_leg(
                leg.origin, leg.dest, leg.date.isoformat(),
                adults=cfg.adults, window=window, currency=cfg.currency,
                direct_only=cfg.direct_only, retries=cfg.retry,
            )
            if res:
                state.record_leg(leg.key, price=res.price, airline=res.airline,
                                 dep_time=res.dep_time, arr_time=res.arr_time)
            else:
                state.record_leg(leg.key, price=None)
        except SearchError:
            failed += 1
        polite_delay(cfg.request_delay_sec)

    state.record_run_stats(attempted=attempted, failed=failed)
    log.info("검색 완료: %d 시도 / %d 실패", attempted, failed)

    # ---- 판정 & 알림 ----
    combos = engine.build_combos(cfg, state, today)
    alerts = engine.process(cfg, state, combos, today)
    log.info("콤보 %d개, 알림 후보 %d건", len(combos), len(alerts))

    dry = os.environ.get("DRY_RUN") == "1"
    if alerts:
        sent_ok = True
        for msg in notify.format_alerts(cfg, alerts):
            if dry:
                print("\n----- DRY RUN 메시지 -----\n" + msg)
            else:
                sent_ok = notify.send(msg) and sent_ok
        if sent_ok or dry:
            engine.mark_sent(state, alerts)  # 전송 실패 시 다음 실행에서 재시도

    err = engine.failure_alert_needed(cfg, state)
    if err:
        print(err) if dry else notify.send(err)

    state.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
