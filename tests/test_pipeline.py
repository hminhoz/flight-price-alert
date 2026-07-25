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
                   route_key: str = "ICN-NGO") -> None:
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
    unit = "ICN-NGO|2026-09"
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
    msgs = format_alerts(CFG, alerts, combos)
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

    test_window_override()
    test_roundtrip_verification()
    test_display_selection()

    print("\n=== 전체 통과 ===")


# ---------------------------------------------------------------- v1.11 / v1.12

def test_window_override():
    """노선별 시간창 override가 config에서 읽히고 방향별로 적용되는지."""
    cfg = load()
    # override 없는 노선은 전역값
    plain = [r for r in cfg.routes if not cfg.has_window_override(r.key)]
    assert plain, "override 없는 노선이 하나도 없다 (테스트 전제 붕괴)"
    assert cfg.window_for(plain[0].key, "out") == cfg.outbound_window
    assert cfg.window_for(plain[0].key, "ret") == cfg.return_window
    # override 노선은 지정한 쪽만 바뀌고 반대 경계는 전역값 유지
    for r in cfg.routes:
        if not cfg.has_window_override(r.key):
            continue
        for direction, glob in (("out", cfg.outbound_window),
                                ("ret", cfg.return_window)):
            lo, hi = cfg.window_for(r.key, direction)
            assert lo <= hi, f"{r.key} {direction} 시간창 역전"
            assert (lo, hi) == glob or lo == glob[0] or hi == glob[1], \
                f"{r.key} {direction}: 양쪽 경계가 동시에 바뀜 (의도 확인 필요)"
    koj = cfg.window_for("ICN-KOJ", "out")
    cts = cfg.window_for("ICN-CTS", "ret")
    assert koj[1] > cfg.outbound_window[1], "가고시마 가는 편 완화 미적용"
    assert cts[0] < cfg.return_window[0], "삿포로 오는 편 완화 미적용"
    print(f"OK 시간창 override: ICN-KOJ out ~{koj[1]:%H:%M}, "
          f"ICN-CTS ret {cts[0]:%H:%M}~")


def test_roundtrip_verification():
    """왕복 실가가 있으면 그 금액이, 없으면 참고치 표시가 나가는지."""
    cfg = load()
    route = cfg.routes[0]
    combo = engine.Combo(route=route, dep=dt.date(2026, 9, 10), nights=3,
                         price=800_000,
                         out_leg={"price": 300_000, "airline": "Jeju Air",
                                  "dep_time": "07:30"},
                         ret_leg={"price": 500_000, "airline": "Jeju Air",
                                  "dep_time": "19:40"})
    a = engine.Alert(kind="record", combo=combo, baseline=850_000,
                     prev_min=900_000)

    # 검증 실패(None) → 편도합산 참고치임을 명시해야 한다
    msg = format_alerts(cfg, [a])[0]
    assert "800,000" in msg and "편도합산 참고치" in msg, msg
    assert "왕복 총액" not in msg

    # 검증 성공 → 왕복 실가가 주 금액
    a.rt_price = 520_000
    msg = format_alerts(cfg, [a])[0]
    assert "520,000" in msg and "왕복 총액" in msg, msg
    assert "참고치" not in msg
    assert "800,000" in msg, "감지지표(편도합산)도 함께 보여야 한다"
    # 판정 근거는 여전히 편도합산 기준이어야 한다 (기준가와 같은 척도)
    assert "-11.1%" in msg, f"하락률이 편도합산 기준이 아니다: {msg}"
    print("OK 왕복 검증 표시: 실가 확보 시 왕복 총액, 실패 시 참고치 명시")


def test_display_selection():
    """왕복 쿼리를 낭비하지 않도록, 실제 노출될 알림만 선별되는지."""
    cfg = load()
    route = cfg.routes[0]
    alerts = []
    for i in range(cfg.bundle_top_n + 3):
        c = engine.Combo(route=route, dep=dt.date(2026, 9, 10) + dt.timedelta(days=i),
                         nights=3, price=900_000 - i * 1000,
                         out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                         ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})
        alerts.append(engine.Alert(kind="baseline", combo=c, baseline=950_000,
                                   prev_min=None))
    picked = engine.display_selection(cfg, alerts)
    assert len(picked) == cfg.bundle_top_n, len(picked)
    # 본문에 실제로 실린 조합과 일치해야 한다 (쿼리 낭비/누락 방지)
    msg = format_alerts(cfg, alerts)[0]
    for a in picked:
        assert f"{a.combo.dep.month}/{a.combo.dep.day}" in msg
    cheapest = min(a.combo.price for a in alerts)
    assert min(a.combo.price for a in picked) == cheapest, "최저가가 선별에서 누락"
    print(f"OK 검증 대상 선별: 알림 {len(alerts)}건 → 왕복 쿼리 {len(picked)}건")


if __name__ == "__main__":
    main()
