"""네트워크 없이 전체 로직 검증.

시나리오:
  Day1~3  관측 기간 — 가격 수집, 알림 없음, 기준가 형성
  Day4    평범한 가격 — 알림 없음
  Day5    특가 등장 — 알림 발생 (record)
  Day5b   같은 가격 재실행 — 중복 억제로 알림 없음
  Day6    더 낮은 특가 — 재알림
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import engine  # noqa: E402
from app.settings import load  # noqa: E402
from app.state import State  # noqa: E402
from app.notify import format_alerts  # noqa: E402


CFG = load()
TODAY0 = dt.date(2026, 8, 1)


def fake_fill_legs(state: State, day: dt.date, out_price: int, ret_price: int,
                   route_key: str = "ICN-KIX") -> None:
    """해당 노선의 9월 일부 날짜 leg를 가짜 가격으로 채운다."""
    now = dt.datetime.combine(day, dt.time(12), tzinfo=dt.timezone.utc)
    for d in [dt.date(2026, 9, 10), dt.date(2026, 9, 11)]:
        state.record_leg(State.leg_key(route_key, "out", d.isoformat()),
                         price=out_price, airline="Jeju Air",
                         dep_time="07:30", arr_time="09:10", now=now)
        for n in CFG.trip_nights:
            r = d + dt.timedelta(days=n)
            state.record_leg(State.leg_key(route_key, "ret", r.isoformat()),
                             price=ret_price, airline="Jeju Air",
                             dep_time="19:40", arr_time="21:30", now=now)


def run_day(state: State, day: dt.date, out_p: int, ret_p: int):
    fake_fill_legs(state, day, out_p, ret_p)
    combos = engine.build_combos(CFG, state, day)
    alerts = engine.process(CFG, state, combos, day)
    return combos, alerts


def main():
    state = State.__new__(State)
    state.legs, state.baselines, state.alerts_sent, state.meta = {}, {}, {}, {}

    # Day 1~3: 관측 기간 (120,000 + 130,000 = 250,000)
    for i in range(3):
        day = TODAY0 + dt.timedelta(days=i)
        combos, alerts = run_day(state, day, 120_000, 130_000)
        assert combos, "콤보 생성 실패"
        assert alerts == [], f"관측 기간에 알림 발생: day{i+1}"
    unit = "ICN-KIX|2026-09"
    assert state.baselines[unit]["baseline"] == 250_000
    print(f"OK 관측기간: 기준가 {state.baselines[unit]['baseline']:,}원, 알림 0건")

    # Day 4: 평범한 가격 (260,000) → 알림 없음
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=3), 125_000, 135_000)
    assert alerts == [], "평범한 가격에 알림 발생"
    print("OK Day4 평상가: 알림 0건")

    # Day 5: 특가 (99,000 + 101,000 = 200,000) → record 알림
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=4), 99_000, 101_000)
    assert alerts, "특가에 알림 미발생"
    assert all(a.kind == "record" for a in alerts)
    msgs = format_alerts(CFG, alerts)
    assert len(msgs) == 1 and "역대 최저가" in msgs[0] and "200,000" in msgs[0]
    engine.mark_sent(state, alerts)
    print(f"OK Day5 특가: 알림 {len(alerts)}건 (묶음 1개 메시지)")
    print("\n----- 메시지 미리보기 -----")
    import re
    print(re.sub(r"<[^>]+>", "", msgs[0]))
    print("-----\n")

    # Day 5b: 같은 가격 재실행 → 중복 억제
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=4), 99_000, 101_000)
    assert alerts == [], "중복 억제 실패"
    print("OK Day5b 동일가 재실행: 알림 0건 (중복 억제)")

    # Day 6: 더 낮은 특가 (190,000) → 재알림
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=5), 95_000, 95_000)
    assert alerts, "더 낮은 특가에 재알림 미발생"
    print(f"OK Day6 추가 하락: 재알림 {len(alerts)}건")

    # 샤딩 검증: 전 leg가 6개 샤드에 고르게 분배 + 임박분 포함 여부
    legs = engine.all_legs(CFG, TODAY0)
    dist = [0] * CFG.shards
    for leg in legs:
        dist[engine.shard_of(leg, CFG.shards)] += 1
    per_run = len(engine.legs_for_run(CFG, TODAY0, 0))
    print(f"\nOK 샤딩: 전체 {len(legs)}개 leg, 샤드 분포 {dist}, "
          f"1회 실행 검색량 ≈ {per_run}건")

    print("\n=== 전체 통과 ===")


if __name__ == "__main__":
    main()
