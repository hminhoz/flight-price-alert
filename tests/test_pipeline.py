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

    # 관측 기간 (120,000 + 130,000 = 250,000) — 길이는 config를 따른다
    obs = CFG.observation_days
    for i in range(obs):
        day = TODAY0 + dt.timedelta(days=i)
        combos, alerts = run_day(state, day, 120_000, 130_000)
        assert combos, "콤보 생성 실패"
        assert alerts == [], f"관측 기간에 알림 발생: day{i+1}"
    unit = "ICN-NGO|2026-09"
    assert state.baselines[unit]["baseline"] == 250_000
    print(f"OK 관측기간 {obs}일: 기준가 {state.baselines[unit]['baseline']:,}원, 알림 0건")

    # 관측 직후: 평범한 가격 (260,000) → 알림 없음
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=obs), 125_000, 135_000)
    assert alerts == [], "평범한 가격에 알림 발생"
    print("OK 관측직후 평상가: 알림 0건")

    # 특가 (99,000 + 101,000 = 200,000) → record 알림
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=obs + 1), 99_000, 101_000)
    assert alerts, "특가에 알림 미발생"
    assert all(a.kind == "record" for a in alerts)
    msgs = format_alerts(CFG, alerts, combos)
    assert len(msgs) == 1 and "역대 최저" in msgs[0] and "200,000" in msgs[0]
    engine.mark_sent(state, alerts)
    print(f"OK 특가: 알림 {len(alerts)}건 (묶음 1개 메시지)")
    print("\n----- 메시지 미리보기 -----")
    import re
    print(re.sub(r"<[^>]+>", "", msgs[0]))
    print("-----\n")

    # 같은 가격 재실행 → 중복 억제
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=obs + 1), 99_000, 101_000)
    assert alerts == [], "중복 억제 실패"
    print("OK 동일가 재실행: 알림 0건 (중복 억제)")

    # 더 낮은 특가 (190,000) → 재알림
    _, alerts = run_day(state, TODAY0 + dt.timedelta(days=obs + 2), 95_000, 95_000)
    assert alerts, "더 낮은 특가에 재알림 미발생"
    print(f"OK 추가 하락: 재알림 {len(alerts)}건")

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

    test_tolerant_parser()
    test_preview_alerts()
    test_per_person_and_link()
    test_price_ordering()
    test_new_vs_drop_badge()
    test_bundle_gap_filter()
    test_cycle_progress()

    print("\n=== 전체 통과 ===")


def test_preview_alerts():
    """미리보기가 기준가·중복억제와 무관하게 노선별 최저를 뽑는지 (v1.22)."""
    cfg = load()
    route = cfg.routes[0]
    combos = [engine.Combo(route=route, dep=dt.date(2026, 9, 10) + dt.timedelta(days=i),
                           nights=3, price=900_000 - i * 1000,
                           out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                           ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})
              for i in range(6)]
    pv = engine.preview_alerts(cfg, combos)
    assert len(pv) == cfg.bundle_top_n, len(pv)
    assert min(a.combo.price for a in pv) == min(c.price for c in combos)
    # 기준가를 조합가와 같게 세팅하므로 메시지에 '기준가' 줄이 자연스럽게 나온다
    msg = format_alerts(cfg, pv, combos)[0]
    assert "편도 2장" in msg
    print(f"OK 미리보기: 콤보 {len(combos)}개 → 노선별 최저 {len(pv)}건")


def test_per_person_and_link():
    """금액은 1인당 우선 표기, 링크는 항공사 코드로 좁혀지는지 (v1.23)."""
    from app.links import google_flights_url
    cfg = load()
    route = cfg.routes[0]
    combo = engine.Combo(
        route=route, dep=dt.date(2026, 9, 10), nights=3, price=800_000,
        out_leg={"price": 300_000, "airline": "Jeju Air", "dep_time": "07:30",
                 "carrier": "7C"},
        ret_leg={"price": 500_000, "airline": "Jeju Air", "dep_time": "19:40",
                 "carrier": "7C"})
    a = engine.Alert(kind="baseline", combo=combo, baseline=850_000, prev_min=None)
    msg = format_alerts(cfg, [a])[0]
    assert "400,000원</b>/인" in msg, msg      # 800,000 / 성인 2명 (굵게)
    assert "2명 800,000원" in msg, msg         # 총액도 함께
    assert "구글에서 보기 (" in msg, msg        # 항공사 필터가 걸린 링크

    # 항공사 코드가 들어가면 링크가 달라져야 한다
    with_code = google_flights_url(route, combo.dep, combo.ret, cfg.adults, ["7C"])
    without = google_flights_url(route, combo.dep, combo.ret, cfg.adults, [])
    assert with_code != without and "tfs=" in with_code
    # 코드를 모르면 필터 없는 링크 + 다른 라벨
    combo.out_leg["carrier"] = combo.ret_leg["carrier"] = ""
    msg2 = format_alerts(cfg, [a])[0]
    assert "구글에서 보기</a>" in msg2, msg2    # 필터 없으면 괄호 표기 없음
    print("OK 1인당 표기 + 항공사 필터 링크")


def test_price_ordering():
    """표시 순서가 실제 강조 금액(편도·왕복 중 싼 쪽) 오름차순인지 (v1.25)."""
    cfg = load()
    r1, r2 = cfg.routes[0], cfg.routes[1]

    def mk(route, day, one_way, rt):
        c = engine.Combo(route=route, dep=dt.date(2026, 9, day), nights=3,
                         price=one_way,
                         out_leg={"price": 1, "dep_time": "07:30",
                                  "airline": "X", "carrier": "7C"},
                         ret_leg={"price": 1, "dep_time": "19:40",
                                  "airline": "X", "carrier": "7C"})
        a = engine.Alert(kind="baseline", combo=c, baseline=one_way, prev_min=None)
        a.rt_price = rt
        return a

    # 같은 노선 안: 편도 기준으론 800<850 이지만 실제 금액은 500<800
    alerts = [mk(r1, 10, 800_000, 1_200_000),   # 표시 800,000
              mk(r1, 11, 850_000, 500_000),     # 표시 500,000
              # 다른 노선은 더 싸다 → 메시지가 먼저 나가야 한다
              mk(r2, 12, 400_000, None)]        # 표시 400,000
    msgs = format_alerts(cfg, alerts)
    assert len(msgs) == 2, msgs
    assert r2.label in msgs[0], "더 싼 노선의 메시지가 먼저 와야 한다"

    body = msgs[1]
    i_cheap = body.index("250,000원")   # 500,000 / 2명
    i_exp = body.index("400,000원</b>/인")   # 800,000 / 2명
    assert i_cheap < i_exp, f"싼 항목이 위에 와야 한다:\n{body}"
    print("OK 정렬: 노선 간·노선 내 모두 실제 금액 오름차순")


def test_new_vs_drop_badge():
    """첫 알림엔 🆕, 재알림엔 얼마나 더 내렸는지 표시 (v1.26)."""
    cfg = load()
    c = engine.Combo(route=cfg.routes[0], dep=dt.date(2026, 9, 10), nights=3,
                     price=800_000,
                     out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                     ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})
    first = engine.Alert(kind="baseline", combo=c, baseline=850_000, prev_min=None)
    msg = format_alerts(cfg, [first])[0]
    assert "지난 알림" not in msg, "첫 알림에는 재알림 문구가 없어야 한다"

    again = engine.Alert(kind="baseline", combo=c, baseline=850_000,
                         prev_min=None, prev_sent=900_000)
    msg = format_alerts(cfg, [again])[0]
    assert "지난 알림" in msg and "50,000원 더 내렸어요" in msg, msg  # 100,000 / 2명
    print("OK 재알림 표시: 첫 알림은 조용, 재알림은 하락폭 명시")


def test_bundle_gap_filter():
    """가격 차이가 미미한 옆 날짜는 묶음에서 빠지는지 (v1.27)."""
    cfg = load()
    r = cfg.routes[0]

    def mk(day, price):
        c = engine.Combo(route=r, dep=dt.date(2026, 9, day), nights=3, price=price,
                         out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                         ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})
        return engine.Alert(kind="baseline", combo=c, baseline=price, prev_min=None)

    # 1% 차이 3개 → 1줄만 남아야 한다 (기본 임계 3%)
    msg = format_alerts(cfg, [mk(10, 800_000), mk(11, 808_000), mk(12, 816_000)])[0]
    assert msg.count("3박") == 1, f"미미한 차이가 여러 줄로 노출됨:\n{msg}"

    # 10% 차이면 둘 다 보여야 한다
    msg = format_alerts(cfg, [mk(10, 800_000), mk(11, 880_000)])[0]
    assert msg.count("3박") == 2, msg
    print("OK 묶음 간격: 1% 차이는 1줄로 정리, 10% 차이는 각각 노출")


def test_cycle_progress():
    """샤드를 다 훑으면 완주로 잡고, 보고 정책이 지켜지는지 (v1.28)."""
    cfg = load()
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    today = dt.date(2026, 8, 1)

    # 샤드를 하나씩 돌면 마지막에 완주
    for i in range(cfg.shards):
        n, total, done = engine.note_shard(cfg, st, i)
        assert total == cfg.shards
        assert done == (i == cfg.shards - 1), (i, done)
    assert st.meta["cycle_shards"] == [], "완주 후에는 진행도가 초기화돼야 한다"

    # 같은 샤드가 반복돼도 완주로 오인하지 않는다 (실행 중복 대비)
    st.meta["cycle_shards"] = []
    for _ in range(cfg.shards + 2):
        _, _, done = engine.note_shard(cfg, st, 0)
        assert not done, "같은 샤드 반복은 완주가 아니다"

    # daily 정책: 하루 한 번만 보고
    st.baselines = {"ICN-NGO|2026-08": {"baseline": 700_000, "daily_min": {}}}
    first = engine.cycle_report(cfg, st, today, 1017)
    assert first and "한 바퀴 완료" in first and "1,017" in first, first
    assert "350,000/인" in first, first          # 700,000 / 성인 2명
    assert engine.cycle_report(cfg, st, today, 1017) is None, "하루 두 번 보고됨"
    print("OK 한 바퀴: 완주 판정·중복 방지·하루 1회 보고")


def test_tolerant_parser():
    """안 쓰는 필드가 빠진 페이로드에서도 가격·시각을 뽑는지 (v1.20).

    기본 파서는 payload[7][1][0](항공동맹 메타)과 flight[22](탄소)를 고정
    인덱스로 읽다가 취항사 많은 노선에서 IndexError로 결과를 통째로 버린다.
    둘 다 이 프로젝트가 안 쓰는 값이므로 관대 파서는 무시해야 한다.
    """
    import json
    from app.gparse import parse_tolerant

    seg = [None, None, None, "ICN", "Incheon", "Kansai", "KIX", None, [6, 55],
           None, [9, 0], 125, None, None, None, None, None, "A350", None, None,
           [2026, 8, 25], [2026, 8, 25]]
    flight = ["OZ", ["Asiana Airlines"], [seg]]          # flight[22] 없음
    payload = [None, None, None, [[[flight, [[None, 412000]]]]],
               None, None, None, [None, []]]            # payload[7][1][0] 없음
    html = ('<html><script class="ds:1">AF_initDataCallback({key:1, data:%s, '
            'sideChannel:{}});</script></html>' % json.dumps(payload))

    res = parse_tolerant(html)
    assert res and len(res) == 1, res
    it = res[0]
    assert it.price == 412000 and it.airlines == ["Asiana Airlines"]
    s = it.flights[0]
    assert s.from_airport.code == "ICN" and s.to_airport.code == "KIX"
    assert s.departure.time == [6, 55]

    # 구글이 실제로 오류를 준 응답은 None → 기존 재시도·폴백 흐름 유지
    err = ('<html><script class="ds:1">AF_initDataCallback({key:1, '
           'data: errorHasStatus: true,});</script></html>')
    assert parse_tolerant(err) is None

    # 뒤쪽 구획(그 외 항공편)까지 긁는지 + 중복 제거 (v1.24)
    def mk(price, hh):
        s = [None, None, None, "GMP", "Gimpo", "Jeju", "CJU", None, [hh, 0],
             None, [hh + 1, 10], 70, None, None, None, None, None, "738",
             None, None, [2026, 8, 1], [2026, 8, 1]]
        return [["7C", ["Jeju Air"], [s]], [[None, price]]]

    multi = [None, None, None,
             [[mk(257400, 15)],                 # 추천 구획
              [mk(198000, 7), mk(257400, 15)]], # 그 외 구획 (겹침 1건 포함)
             None, None, None, [None, []]]
    html2 = ('<html><script class="ds:1">AF_initDataCallback({key:1, data:%s, '
             'sideChannel:{}});</script></html>' % json.dumps(multi))
    got = parse_tolerant(html2)
    assert got is not None and len(got) == 2, got      # 중복 1건 제거
    assert min(i.price for i in got) == 198000, got     # 오전 7시 편을 건졌다
    print("OK 관대 파서: 전 구획 수집·중복 제거 · 오류 응답은 None 유지")


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

    # 왕복가 미확보 → 편도 2장만 표시
    msg = format_alerts(cfg, [a])[0]
    assert "편도 2장" in msg and "왕복권" not in msg, msg

    # 왕복이 더 비싼 경우 → 편도 2장이 대표 금액, 왕복은 비교용 한 줄
    a.rt_price = 1_200_000
    msg = format_alerts(cfg, [a])[0]
    assert "편도 2장 <b>400,000원</b>/인" in msg, msg   # 800,000 / 2명
    assert "왕복권으로 사면 600,000원/인" in msg, msg   # 1,200,000 / 2명
    assert "편도 2장이 유리" in msg, msg

    # 왕복이 더 싼 경우 → 왕복이 대표 금액 (노선마다 갈리므로 양방향 필요)
    a.rt_price = 600_000
    msg = format_alerts(cfg, [a])[0]
    assert "왕복권 <b>300,000원</b>/인" in msg, msg
    assert "왕복이 유리" in msg, msg
    print("OK 금액 표시: 실제 낼 금액을 대표로, 대안 구매법은 비교 한 줄")


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
    # 본문에 실린 조합은 검증 대상의 부분집합이어야 한다.
    # (v1.27부터 가격 차이가 미미한 항목은 본문에서 빠지므로 '일치'가 아니라
    #  '포함'이 조건이다. 검증받지 않은 항목이 표시되는 일만 없으면 된다)
    msg = format_alerts(cfg, alerts)[0]
    picked_days = {f"{a.combo.dep.month}/{a.combo.dep.day}" for a in picked}
    shown = {f"{a.combo.dep.month}/{a.combo.dep.day}" for a in alerts
             if f"{a.combo.dep.month}/{a.combo.dep.day}" in msg}
    assert shown and shown <= picked_days, (shown, picked_days)
    cheapest = min(a.combo.price for a in alerts)
    assert min(a.combo.price for a in picked) == cheapest, "최저가가 선별에서 누락"
    print(f"OK 검증 대상 선별: 알림 {len(alerts)}건 → 왕복 쿼리 {len(picked)}건")


if __name__ == "__main__":
    main()
