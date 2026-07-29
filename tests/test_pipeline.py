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
import pathlib
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
    unit = "NGO|2026-09"      # v2.07: 기준가 단위는 도시×월
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
    assert len(msgs) == 1 and "싸짐" in msgs[0] and "100,000" in msgs[0]
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
    test_widened_window_marks_off_preference()
    test_off_window_mark_is_short()
    test_time_hist_persisted()
    test_roundtrip_verification()
    test_display_selection()

    test_tolerant_parser()
    test_preview_alerts()
    test_per_person_and_link()
    test_price_ordering()
    test_cross_airport_combos()
    test_weak_alert_suppressed()
    test_header_matches_cheapest_shown()
    test_digest()
    test_live_board()
    test_board_ids_compact()
    test_exclude_airlines()
    test_poll_commands()
    test_month_filter()
    test_run_mode_parsing()
    test_naver_parser()
    test_naver_leg_merge()
    test_naver_schedule()
    test_naver_job_order()
    test_call_signatures()
    test_mixed_source_links()
    test_unit_is_city()
    test_verify_targets_catch_skew()
    test_baseline_unstick()
    test_new_vs_drop_badge()
    test_near_dates_linked()
    test_time_histogram()
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
    assert "원</b>/인" in msg
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
    assert "<a href=" in msg, msg              # 링크

    # 항공사 코드가 들어가면 링크가 달라져야 한다
    with_code = google_flights_url(route, combo.dep, combo.ret, cfg.adults, ["7C"])
    without = google_flights_url(route, combo.dep, combo.ret, cfg.adults, [])
    assert with_code != without and "tfs=" in with_code
    # 코드를 모르면 필터 없는 링크 + 다른 라벨
    combo.out_leg["carrier"] = combo.ret_leg["carrier"] = ""
    msg2 = format_alerts(cfg, [a])[0]
    assert "구글</a>" in msg2, msg2
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
    assert engine.city_label(cfg, r2) in msgs[0], "더 싼 도시의 메시지가 먼저 와야 한다"

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
    assert "지난 알림보다 50,000원 더 내림" in msg, msg  # 100,000 / 2명
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
    assert msg.split("다른 날짜")[0].count("3박") == 1, msg

    # 10% 차이면 둘 다 보여야 한다
    msg = format_alerts(cfg, [mk(10, 800_000), mk(11, 880_000)])[0]
    assert msg.split("다른 날짜")[0].count("3박") == 2, msg
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

    # 샤드가 여러 개면, 같은 샤드가 반복돼도 완주로 오인하지 않는다
    # (샤드가 1이면 매 실행이 곧 완주이므로 이 검사는 해당 없음)
    if cfg.shards > 1:
        st.meta["cycle_shards"] = []
        for _ in range(cfg.shards + 2):
            _, _, done = engine.note_shard(cfg, st, 0)
            assert not done, "같은 샤드 반복은 완주가 아니다"
    else:
        st.meta["cycle_shards"] = []
        _, _, done = engine.note_shard(cfg, st, 0)
        assert done, "샤드가 1이면 한 번 실행이 곧 완주여야 한다"

    # 보고 정책 (v1.47부터 기본은 off — 고정판이 그 역할을 대신한다)
    import copy
    d = copy.copy(cfg)
    d.cycle_report = "daily"
    d.digest_hour = 0                     # 시각 조건은 여기서 검증하지 않는다
    first = engine.cycle_report(d, st, today, 1017)
    assert first and "1,017" in first, first
    assert engine.cycle_report(d, st, today, 1017) is None, "하루 두 번 보고됨"

    # off면 아예 보내지 않는다. YAML이 따옴표 없는 off를 False로 읽어
    # 두 분기를 모두 비껴가 매번 발송되던 버그가 있었다 (v1.47)
    from app.settings import _cycle_policy
    assert _cycle_policy(False) == "off" and _cycle_policy("off") == "off"
    o = copy.copy(cfg)
    o.cycle_report = "off"
    st.meta.pop("last_cycle_report", None)
    assert engine.cycle_report(o, st, today, 1017) is None, "off인데 발송됨"
    print("OK 한 바퀴: 완주 판정·중복 방지·하루 1회 보고")


def test_near_dates_linked():
    """근처 날짜 목록의 각 줄이 눌러서 갈 수 있는 링크인지 (v1.31)."""
    cfg = load()
    r = cfg.routes[0]

    def mk(day, price):
        return engine.Combo(
            route=r, dep=dt.date(2026, 9, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    top = mk(10, 800_000)
    a = engine.Alert(kind="baseline", combo=top, baseline=850_000, prev_min=None)
    near = [mk(12, 820_000), mk(14, 840_000)]
    msg = format_alerts(cfg, [a], [top] + near, today=dt.date(2026, 8, 1))[0]

    # v1.38부터 근처 날짜는 별도 구역이 아니라 본문에 오름차순으로 섞인다.
    body = msg.split("다른 날짜")[1] if "다른 날짜" in msg else ""
    assert body.count("<a href=") == len(near), msg
    assert "9/12" in body, body                         # 날짜·요일
    assert "410,000" in body, body                      # 820,000 / 2명
    assert "D-" not in body, "D-day는 빼기로 했다"

    # 메시지 전체가 오름차순이어야 한다 (13만 → 16만 → 13만 사태 방지)
    import re as _re
    shown = [int(x.replace(",", "")) for x in
             _re.findall(r"([\d,]+)원/인", _re.sub(r"</?b>", "", msg))]
    assert shown == sorted(shown), f"금액이 오름차순이 아니다: {shown}"
    print("OK 근처 날짜: 본문에 오름차순 통합 · 링크·시각·항공사 포함")


def test_time_histogram():
    """시간창에 걸려 탈락한 편까지 포함해 출발 시각이 집계되는지 (v1.32)."""
    import datetime as _dt
    from app import search as S

    class _T:
        def __init__(self, h, m=0):
            self.time = [h, m]

    class _Seg:
        def __init__(self, h):
            self.departure, self.arrival = _T(h), _T(h + 2)
            self.from_airport = type("A", (), {"code": "ICN", "name": "I"})()
            self.to_airport = type("A", (), {"code": "NGO", "name": "N"})()

    class _It:
        def __init__(self, h, price):
            self.flights, self.price, self.airlines = [_Seg(h)], price, ["Jeju Air"]
            self.type = "7C"

    S._time_hist.clear()
    window = (_dt.time(6, 0), _dt.time(13, 0))
    # 07시(창 안) · 15시·19시(창 밖) → 셋 다 집계되어야 한다
    S._pick_best([_It(7, 300_000), _It(15, 200_000), _It(19, 100_000)],
                 window, True, "2026-08-01", "ICN", "NGO")
    hours = S.time_histogram()[("ICN", "NGO")]
    assert hours == {7: 1, 15: 1, 19: 1}, hours

    # 창 안 편만 채택돼야 한다 (더 싼 15시·19시를 고르면 안 됨)
    best = S._pick_best([_It(7, 300_000), _It(15, 200_000)], window, True,
                        "2026-08-01", "ICN", "NGO")
    assert best is not None and best.price == 300_000, best
    print("OK 시간 분포: 탈락편 포함 집계 · 채택은 창 안에서만")


def test_off_window_mark_is_short():
    """선호 시간 밖 표시는 시각 옆 ⚠ 한 글자로 (v1.35)."""
    cfg = load()
    r = [x for x in cfg.routes if x.key == "ICN-KOJ"][0]
    c = engine.Combo(route=r, dep=dt.date(2026, 8, 12), nights=3, price=900_000,
                     out_leg={"price": 1, "airline": "대한항공",
                              "dep_time": "16:20", "carrier": "KE",
                              "off_window": True},
                     ret_leg={"price": 1, "airline": "대한항공",
                              "dep_time": "18:55", "carrier": "KE"})
    a = engine.Alert(kind="baseline", combo=c, baseline=950_000, prev_min=None)
    msg = format_alerts(cfg, [a])[0]
    assert "16:20⚠ → 18:55" in msg, msg       # 조건 밖인 쪽에만 ⚠
    assert "선호 시간대 밖입니다" not in msg      # 긴 설명 줄은 삭제
    print("OK 선호시간 밖 표시: 해당 시각 옆 ⚠ 한 글자")


def test_time_hist_persisted():
    """시간 분포가 실행마다 누적 저장되는지 (v1.36).

    이 값은 원래 로그에만 있어 옮겨 보기가 번번이 실패했다.
    data/time_hist.json 에 쌓아두면 저장소에서 바로 확인할 수 있다.
    """
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}

    st.merge_time_hist({("ICN", "KOJ"): {8: 1, 16: 13}})
    st.merge_time_hist({("ICN", "KOJ"): {16: 5}, ("GMP", "CJU"): {6: 4}})

    koj = st.time_hist["ICN-KOJ"]
    assert koj == {"8": 1, "16": 18}, koj          # 실행 간 누적
    assert st.time_hist["GMP-CJU"] == {"6": 4}
    assert st.time_hist["_runs"] == 2, st.time_hist
    assert "_updated" in st.time_hist
    print("OK 시간 분포 저장: 실행 간 누적 · 노선별 분리")


def test_cross_airport_combos():
    """인천/김포 교차 조합이 만들어지고, 어느 공항인지 반드시 표시되는지 (v1.41).

    실측 근거: `김포→나고야 / 나고야→인천`이 같은 공항 왕복보다 1인 최대
    184,600원 쌌다. 김포발 피치가 싼데 나고야→김포는 18시 이후 편이 없어
    같은 공항끼리만 묶으면 그 가는 편이 버려지기 때문. 추가 검색은 0건이다.
    """
    cfg = load()
    # 특정 노선을 박아두면 노선 구성이 바뀔 때 테스트가 깨진다.
    # 같은 도시에 서울발 노선이 둘 이상인 곳을 찾아 쓴다.
    groups: dict = {}
    for r in cfg.routes:
        groups.setdefault(engine._seoul_group(cfg, r), []).append(r)
    pair = next((v for v in groups.values() if len(v) >= 2), None)
    assert pair, "교차 가능한 도시가 없다 (노선 구성 확인 필요)"
    gmp, icn = pair[0], pair[1]

    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    today = dt.date(2026, 8, 1)
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    # 김포 출발은 싸고, 김포로 돌아오는 편은 없음 → 인천 귀국만 가능
    st.record_leg(State.leg_key(gmp.key, "out", dep.isoformat()),
                  price=200_000, airline="Jin Air", dep_time="11:20",
                  carrier="LJ", now=now)
    st.record_leg(State.leg_key(icn.key, "out", dep.isoformat()),
                  price=400_000, airline="Jin Air", dep_time="07:30",
                  carrier="LJ", now=now)
    st.record_leg(State.leg_key(icn.key, "ret", ret.isoformat()),
                  price=300_000, airline="Jeju Air", dep_time="19:00",
                  carrier="7C", now=now)

    combos = engine.build_combos(cfg, st, today)
    pairs = {(c.route.key, c.back.key): c for c in combos
             if c.dep == dep and c.nights == 3}
    assert (gmp.key, icn.key) in pairs, "교차 조합이 안 만들어졌다"
    assert (icn.key, icn.key) in pairs, "같은 공항 조합도 있어야 한다"

    cross = pairs[(gmp.key, icn.key)]
    same = pairs[(icn.key, icn.key)]
    assert cross.is_cross and not same.is_cross
    assert cross.price == 500_000 < same.price == 700_000, (cross.price, same.price)
    # 중복 억제용 key는 달라야 하지만, **기준가 단위는 같아야 한다**
    # (v2.07: 같은 도시·같은 달이면 하나의 잣대로 비교한다)
    assert cross.key != same.key, "중복 억제 키가 겹치면 안 된다"
    assert cross.unit == same.unit, "같은 도시·달인데 기준가 단위가 갈렸다"

    # 메시지에 공항이 반드시 드러나야 한다 (엉뚱한 공항으로 가면 비행기를 놓친다)
    a = engine.Alert(kind="baseline", combo=cross, baseline=600_000, prev_min=None)
    msg = format_alerts(cfg, [a], combos)[0]
    from app.notify import _ko
    assert f"{_ko(gmp.origin)} 출발" in msg and f"{_ko(icn.origin)} 귀국" in msg, msg
    assert engine.city_label(cfg, gmp) in msg.splitlines()[0], msg.splitlines()[0]
    print("OK 교차 조합: 생성·키 분리·공항 명시")


def test_weak_alert_suppressed():
    """기준가와 사실상 같은 가격은 알리지 않는다 (v1.42).

    실측에서 알림 90건 중 55건이 기준가 대비 1% 미만이었다. 특가가 아닌데
    알림이 가면 진짜 특가가 묻힌다. 역대 최저 갱신은 이 조건과 무관하게 알린다.
    """
    cfg = load()
    assert cfg.min_below_baseline_pct > 0, "임계가 0이면 이 보호가 무력하다"
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    st.meta["first_run"] = "2026-07-01"          # 관측 기간 종료 상태
    st.meta["baseline_metric"] = engine._METRIC
    today = dt.date(2026, 8, 1)
    route = cfg.routes[0]

    def combo(price):
        return engine.Combo(route=route, dep=dt.date(2026, 9, 10), nights=3,
                            price=price,
                            out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                            ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})

    unit = combo(1_000_000).unit
    # 기준가는 최근 daily_min의 중앙값이므로, 여러 날치를 채워야 현실적이다.
    # 하루치만 두면 새 최저가가 곧바로 기준가가 돼 비교 자체가 성립하지 않는다.
    hist = {(today - dt.timedelta(days=i)).isoformat(): 1_000_000
            for i in range(1, 8)}

    def reset():
        st.baselines[unit] = {"daily_min": dict(hist), "baseline": 1_000_000,
                              "alltime_min": 900_000,
                              "alltime_min_at": "2026-07-01T00:00:00"}
        st.alerts_sent = {}

    reset()   # 기준가와 같은 값 → 알림 없음
    assert engine.process(cfg, st, [combo(1_000_000)], today) == []
    reset()   # 1% 낮음 → 임계(2%) 미달이라 알림 없음
    assert engine.process(cfg, st, [combo(990_000)], today) == []
    reset()   # 5% 낮음 → 알림
    al = engine.process(cfg, st, [combo(950_000)], today)
    assert len(al) == 1, al
    print("OK 약한 알림 차단: 기준가 대비 임계 미만은 발송하지 않음")


def test_header_matches_cheapest_shown():
    """제목의 'N원부터'가 메시지에 실린 것 중 최저가와 일치하는지 (v1.44).

    알림 항목만 보고 제목을 정하면, 더 싼 근처 날짜가 바로 아래 있는데도
    제목이 비싼 값을 말한다. 실측 나고야에서 제목 368,688원 / 본문 282,139원.
    """
    import re as _re
    cfg = load()
    r = cfg.routes[0]

    def mk(day, price, nights=3):
        return engine.Combo(
            route=r, dep=dt.date(2026, 9, day), nights=nights, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    alert_combo = mk(10, 800_000)
    a = engine.Alert(kind="baseline", combo=alert_combo, baseline=900_000,
                     prev_min=None)
    cheaper = mk(14, 600_000)          # 알림은 아니지만 더 싸다
    msg = format_alerts(cfg, [a], [alert_combo, cheaper])[0]

    head = msg.splitlines()[0]
    shown = [int(x.replace(",", "")) for x in
             _re.findall(r"([\d,]+)원", _re.sub(r"</?b>", "", msg))]
    assert f"{300_000:,}원</b>/인" in head, head   # 600,000 / 2명
    assert min(shown) == 300_000, (head, shown)

    # 같은 날 같은 값이면 박 수가 긴 쪽만 (3박·4박 중복 제거)
    dup3, dup4 = mk(16, 620_000, 3), mk(16, 620_000, 4)
    msg2 = format_alerts(cfg, [a], [alert_combo, dup3, dup4])[0]
    assert msg2.count("9/16") == 1, msg2
    assert "4박" in [l for l in msg2.splitlines() if "9/16" in l][0], msg2
    # 제목 금액은 본문 최저와 같아야 한다 (근처 날짜가 더 쌀 수 있다)
    print("OK 제목 금액: 실제 최저와 일치 · 같은 날 같은 값은 긴 박 수만")


def test_digest():
    """조용한 날 볼 수 있는 도시별 최저가 요약 (v1.45).

    한 번 알린 조합은 더 싸지기 전엔 다시 알리지 않으므로, 알림이 없는 날에
    현재 시세를 확인할 창구가 필요하다.
    """
    from app.notify import format_digest
    cfg = load()
    r1, r2 = cfg.routes[0], cfg.routes[1]

    def mk(route, day, price):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    combos = [mk(r1, 10, 900_000), mk(r1, 12, 700_000), mk(r2, 11, 500_000)]
    msgs = format_digest(cfg, combos, "테스트", dt.date(2026, 8, 1))
    msg = "\n".join(msgs)

    # 도시가 제목, 그 밑에 날짜 여러 줄. 싼 도시부터.
    assert msg.count("원~") == 2, msg
    i2 = msg.index(engine.city_label(cfg, r2))
    i1 = msg.index(engine.city_label(cfg, r1))
    assert i2 < i1, "더 싼 도시가 먼저 와야 한다"
    assert "250,000원~" in msg, msg      # 500,000 / 2명
    assert "350,000원~" in msg, msg      # 700,000 / 2명
    assert "450,000" in msg, "도시 안에서는 여러 날짜를 보여준다"
    assert msg.count("<a href=") == 3, "날짜마다 링크"

    # 도시별 표시 개수는 설정을 따른다
    many = [mk(r1, d, 900_000 - d * 1000) for d in range(1, 9)]
    msg3 = "\n".join(format_digest(cfg, many, "", dt.date(2026, 8, 1)))
    assert msg3.count("<a href=") == cfg.digest_top_n, msg3

    # 텔레그램 4096자 제한을 넘으면 나눠 보낸다 (실측 8도시×3날짜 = 6,400자)
    from app.notify import TELEGRAM_LIMIT
    big = [mk(r, d, 500_000 + i * 1000)
           for i, r in enumerate(cfg.routes) for d in range(1, 6)]
    parts = format_digest(cfg, big, "긴 경우", dt.date(2026, 8, 1))
    assert len(parts) > 1, "안 나뉘었다"
    for pmsg in parts:
        assert len(pmsg) < TELEGRAM_LIMIT, len(pmsg)
    # 도시가 통째로 한 통 안에 있어야 한다 (블록이 쪼개지면 안 됨)
    joined = "\n".join(parts)
    for r in cfg.routes:
        assert engine.city_label(cfg, r) in joined

    # 콤보가 없어도 죽지 않는다
    empty = format_digest(cfg, [], "", dt.date(2026, 8, 1))
    assert len(empty) == 1 and "아직 비교할 조합이 없습니다" in empty[0]
    print(f"OK 다이제스트: 도시 제목 + 날짜 {cfg.digest_top_n}개·싼 순·링크·빈 데이터 방어")


def test_live_board():
    """고정판은 반드시 한 통에 들어가야 한다 (v1.47).

    수정(editMessageText)으로 갱신하는 구조라 여러 통으로 나눌 수 없다.
    노선이 늘어도 4096자를 넘지 않는지 지킨다.
    """
    from app.notify import format_board, TELEGRAM_LIMIT
    cfg = load()

    def mk(route, day, price):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    # 전 노선 × 넉넉한 날짜로 최악을 가정해도 한 통에 들어가야 한다
    big = [mk(r, d, 300_000 + i * 7000 + d * 100)
           for i, r in enumerate(cfg.routes) for d in range(1, 15)]
    parts = format_board(cfg, big, "07/26 14:07", dt.date(2026, 8, 1))
    for pm in parts:
        assert len(pm) < TELEGRAM_LIMIT, f"한 통이 {len(pm)}자로 제한을 넘었다"
    board = "\n".join(parts)

    # v1.98: **모든 날짜에 링크**. 그래서 여러 통으로 나눈다.
    cities = {engine._seoul_group(cfg, r) for r in cfg.routes}
    assert board.count("<a href=") == len(cities) * cfg.board_top_n, board.count("<a href=")
    assert "📌" in board.splitlines()[0]

    # v1.96부터 도시당 개수는 board_top_n으로 고정하고, 길이는 통을 나눠 맞춘다.
    # (예전엔 한 통에 우겨넣느라 개수를 줄였다)
    small = [mk(r, d, 300_000 + d * 100) for r in cfg.routes[:2] for d in range(1, 15)]
    parts_s = format_board(cfg, small, "07/26 14:07", dt.date(2026, 8, 1))
    for pm in parts_s:
        assert len(pm) < TELEGRAM_LIMIT, len(pm)
    b_small = "\n".join(parts_s)
    assert b_small.count("<a href=") == 2 * cfg.board_top_n, b_small.count("<a href=")

    # 빈 데이터에도 죽지 않는다
    assert "아직" in format_board(cfg, [], "07/26 14:07", dt.date(2026, 8, 1))[0]
    print(f"OK 고정판: {len(parts)}통 · 모든 날짜 링크 · 도시당 {cfg.board_top_n}개")


def test_board_ids_compact():
    """고정판 상태는 해시만 남긴다 (v1.49).

    전문을 meta.json에 넣으면 실행마다 4KB가 커밋에 쌓인다. 실제로 그렇게
    돌고 있었고, 수정 성공 시 그 값을 갱신하지도 않아 비교가 무용지물이었다.
    """
    import os
    from app import notify as N

    calls = []

    def fake_post(token, method, payload):
        calls.append(method)
        return {"message_id": 112} if method == "sendMessage" else {"ok": True}

    old_post, old_env = N._post, dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "999"
    N._post = fake_post
    try:
        ids = N.upsert_board("첫 내용", {})
        assert ids["999"] == [112] and calls == ["sendMessage"], (ids, calls)

        calls.clear()                      # 내용 동일 → 호출 없음
        ids = N.upsert_board("첫 내용", ids)
        assert calls == [], calls

        calls.clear()                      # 내용 변경 → 수정
        ids = N.upsert_board("바뀐 내용", ids)
        assert calls == ["editMessageText"], calls

        calls.clear()                      # 통이 늘면 새 통만 발송
        ids = N.upsert_board(["바뀐 내용", "둘째 통"], ids)
        assert calls == ["sendMessage"] and len(ids["999"]) == 2, (calls, ids)

        # 전문은 저장하지 않는다 (해시만)
        assert not any(str(k).endswith(":text") for k in ids), ids
        assert all(len(str(v)) < 60 for v in ids.values()), ids
        # 예전 형식(:text)이 남아 있어도 정리된다
        ids2 = N.upsert_board("또 바뀜", {**ids, "999:text": "x" * 4000})
        assert not any(str(k).endswith(":text") for k in ids2)
    finally:
        N._post = old_post
        os.environ.clear()
        os.environ.update(old_env)
    print("OK 고정판 상태: 해시만 저장 · 동일 내용은 호출 생략")


def test_exclude_airlines():
    """제외 항공사는 수집·조합 양쪽에서 빠져야 한다 (v1.50).

    수집에서만 막으면 이미 저장된 다리가 leg_freshness_days 동안 남아
    설정을 바꿔도 며칠간 계속 노출된다. 그래서 조합 단계에서도 거른다.
    """
    from app import search as S
    cfg = load()
    assert "MM" in cfg.exclude_airlines, cfg.exclude_airlines

    S.set_excluded_airlines(cfg.exclude_airlines)
    try:
        assert S.is_excluded("MM", "Peach Aviation")
        assert S.is_excluded("", "Peach")          # 코드 없어도 이름으로
        assert not S.is_excluded("7C", "Jeju Air")
        assert not S.is_excluded("TW", "Trinity Airways")
    finally:
        S.set_excluded_airlines([])

    # 저장된 다리도 조합에서 빠진다
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    route = cfg.routes[0]
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    st.record_leg(State.leg_key(route.key, "out", dep.isoformat()),
                  price=100_000, airline="Peach", dep_time="07:30",
                  carrier="MM", now=now)
    st.record_leg(State.leg_key(route.key, "ret", ret.isoformat()),
                  price=100_000, airline="Jeju Air", dep_time="19:00",
                  carrier="7C", now=now)
    assert engine.build_combos(cfg, st, dt.date(2026, 8, 1)) == [], "제외 항공사가 조합에 남았다"

    # 제외 항공사가 아니면 정상 생성
    st.record_leg(State.leg_key(route.key, "out", dep.isoformat()),
                  price=100_000, airline="Jin Air", dep_time="07:30",
                  carrier="LJ", now=now)
    assert engine.build_combos(cfg, st, dt.date(2026, 8, 1)), "정상 조합까지 막혔다"
    print("OK 제외 항공사: 수집·조합 양쪽에서 차단, 코드·이름 모두 인식")


def test_poll_commands():
    """텔레그램 명령은 허용된 방에서 온 것만, offset은 전진해야 한다 (v1.52)."""
    import os
    from app import notify as N

    payload = {"result": [
        {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/digest"}},
        {"update_id": 11, "message": {"chat": {"id": 999}, "text": "안녕"}},
        {"update_id": 12, "message": {"chat": {"id": 555}, "text": "/digest"}},
        {"update_id": 13, "message": {"chat": {"id": 999}, "text": "/Help@mybot"}},
    ]}

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return payload

    old_get, old_env = N.requests.get, dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "999"
    N.requests.get = lambda *a, **k: _R()
    try:
        cmds, nxt = N.poll_commands(0)
        assert nxt == 14, nxt                       # 마지막 update_id + 1
        assert cmds == [("999", "digest", ""), ("999", "help", "")], cmds
        # 남의 방(555)에서 온 명령은 무시 — 아무나 조회를 돌리게 두면 안 된다
        assert all(c == "999" for c, _, _ in cmds)

    finally:
        N.requests.get = old_get
        os.environ.clear()
        os.environ.update(old_env)

    # 월 인자 해석
    assert N.parse_month("8월", "") == 8
    assert N.parse_month("digest", "8") == 8
    assert N.parse_month("digest", "8월") == 8
    assert N.parse_month("digest", "") is None
    assert N.parse_month("13", "") is None      # 없는 달은 무시
    print("OK 텔레그램 명령: 허용 방만 · @봇이름 처리 · offset 전진 · 월 인자")


def test_month_filter():
    """월 요약은 그 달 출발만, 한 통에 (v1.53)."""
    from app.notify import format_board, format_digest, TELEGRAM_LIMIT
    cfg = load()
    r = cfg.routes[0]

    def mk(month, day, price):
        return engine.Combo(
            route=r, dep=dt.date(2026, month, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    combos = [mk(8, 12, 500_000), mk(9, 12, 400_000), mk(10, 12, 300_000)]
    b8 = "\n".join(format_board(cfg, combos, "07/26 15:40", dt.date(2026, 8, 1), month=8))
    assert "8월 출발" in b8 and "8/12" in b8, b8
    assert "9/12" not in b8 and "10/12" not in b8, b8
    pass

    # 해당 월 조합이 없으면 그렇게 알려준다
    b12 = format_board(cfg, combos, "07/26 15:40", dt.date(2026, 8, 1), month=12)[0]
    assert "12월 출발 조합이 아직 없습니다" in b12, b12
    d12 = format_digest(cfg, combos, "", dt.date(2026, 8, 1), month=12)
    assert len(d12) == 1 and "12월 출발 조합이 아직 없습니다" in d12[0]
    print("OK 월 요약: 해당 월만 · 한 통 · 없으면 안내")


def test_run_mode_parsing():
    """깃허브 dry_run 입력과 텔레그램 명령이 같은 규칙으로 해석되는지 (v1.55).

    텔레그램에만 월 지정을 붙이고 깃허브 입력을 안 맞춰서, 같은 기능인데
    한쪽에서만 되던 상태였다.
    """
    from app.notify import parse_month

    def interpret(raw: str):
        parts = raw.strip().lower().split()
        mode = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        month = parse_month(mode, arg)
        return {
            "dry": mode == "1",
            "preview": mode == "preview",
            "digest": mode == "digest",
            "month": month,
            "brief": bool(month) and mode != "digest",
        }

    assert interpret("") == {"dry": False, "preview": False, "digest": False,
                             "month": None, "brief": False}
    assert interpret("1")["dry"] is True
    assert interpret("preview")["preview"] is True
    assert interpret("digest") == {"dry": False, "preview": False, "digest": True,
                                   "month": None, "brief": False}
    assert interpret("digest 8")["digest"] and interpret("digest 8")["month"] == 8
    assert interpret("digest 8")["brief"] is False
    assert interpret("8")["brief"] and interpret("8")["month"] == 8
    assert interpret("8월")["brief"] and interpret("8월")["month"] == 8
    assert interpret("13")["month"] is None      # 없는 달은 실전으로 떨어진다
    # 보기 전용 모드는 검색도 저장도 하지 않아야 한다 (v1.56).
    # main.py에서 이 순서가 지켜지는지 소스로 확인한다 — 순서가 뒤집히면
    # 조회 한 번에 고정판이 덮어써지거나 수집분이 날아간다.
    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text()
    i_skip = src.index("보기 전용 모드 → 검색 건너뜀")
    i_search = src.index("검색 완료: %d 시도")
    i_brief = src.index("월 요약 전송")
    i_board = src.index("고정판 갱신 완료")
    # naver-run 모드가 자체 저장을 하므로 '마지막' 저장 지점을 본다
    i_save = src.rindex("state.save()")
    assert i_skip < i_search, "검색 건너뛰기가 검색보다 뒤에 있다"
    assert i_brief < i_board < i_save, "월 요약이 고정판·저장보다 뒤에 있다"
    print("OK 실행 모드 해석: 깃허브·텔레그램 동일 규칙 · 보기 전용은 검색·저장 없음")


def test_naver_parser():
    """네이버 결과행 파서 — 탐침으로 확보한 실물 문자열 기준 (v1.67).

    실적 조건이 붙은 가격은 버린다: 카드 이용실적을 채워야 살 수 있는 값이라
    대부분 실제로는 그 가격에 못 산다. 알림에 띄우면 "가서 보니 더 비싸다"가 된다.
    """
    import datetime as _dt
    from app import naver as NV

    # ── 국내선 (실물) ──
    dom = "파라타항공 | 06:00GMP | 07:15CJU | 01시간 15분 | 특가석편도 51,200원~"
    r = NV.parse_domestic(dom)
    assert r and r["airline"] == "파라타항공", r
    assert r["from"] == "GMP" and r["to"] == "CJU"
    assert r["dep"] == _dt.time(6, 0) and r["arr"] == _dt.time(7, 15)
    assert r["seat"] == "특가석" and r["price"] == 51_200

    # ── 국제선 (실물) ──
    intl = ("제주항공 | 07:10ICN | 09:05KIX | 직항, 01시간 55분 | 09:00KIX | "
            "11:00ICN | 직항, 02시간 00분 | 성인 | 왕복 | 192,300 | 원~")
    r = NV.parse_intl(intl)
    assert r and r["airline"] == "제주항공", r
    assert r["out_from"] == "ICN" and r["out_dep"] == _dt.time(7, 10)
    assert r["ret_from"] == "KIX" and r["ret_dep"] == _dt.time(9, 0)
    assert r["direct"] is True and r["price"] == 192_300

    # ── 실적 조건은 버린다 (실물 문구) ──
    cond = ("제주항공 | 07:10ICN | 09:05KIX | 직항, 01시간 55분 | 09:00KIX | "
            "11:00ICN | 직항, 02시간 00분 | 성인/하나카드(이용실적 충족시) | "
            "왕복 | 192,300 | 원~ | 로그인 후 특가확인")
    assert NV.has_spend_condition(cond)
    # v2.08: 조건부도 받되 표시를 남긴다. 끄면 예전처럼 버린다.
    NV.set_allow_card_condition(False)
    assert NV.parse_intl(cond) is None, "끈 상태인데 조건부가 통과됐다"
    NV.set_allow_card_condition(True)
    r = NV.parse_intl(cond)
    assert r and r["card_cond"] is True, "조건부 표시가 안 붙었다"
    # 카드로 결제만 하면 되는 건 조건이 아니다
    ok = cond.replace("(이용실적 충족시)", "")
    r2 = NV.parse_intl(ok)
    assert r2 and r2["card_cond"] is False, r2

    # ── 시간창·직항 필터 ──
    NV.set_allow_card_condition(False)
    rows = [
        intl,                                             # 07:10 출발 / 09:00 귀국
        intl.replace("07:10ICN", "15:20ICN").replace("192,300", "150,000"),
        cond.replace("192,300", "100,000"),               # 조건부 — 끈 상태이므로 제외
    ]
    best = NV.pick_best(rows, domestic=False,
                        out_window=(_dt.time(6, 0), _dt.time(13, 0)),
                        ret_window=(_dt.time(8, 0), _dt.time(23, 59)))
    assert best and best["price"] == 192_300, best   # 15:20편·조건부는 탈락

    # 국내선 시간창
    rows_d = [dom, dom.replace("06:00GMP", "15:00GMP").replace("51,200", "40,000")]
    bd = NV.pick_best(rows_d, domestic=True,
                      out_window=(_dt.time(6, 0), _dt.time(13, 0)))
    assert bd and bd["price"] == 51_200, bd
    NV.set_allow_card_condition(load().naver_card_condition)   # 설정값으로 복구
    print("OK 네이버 파서: 실물 파싱·카드조건 표시/제외 전환·시간창 필터")


def test_naver_leg_merge():
    """네이버 다리는 더 쌀 때만 쓰이고, 구글 값을 덮어쓰지 않는다 (v1.69)."""
    cfg = load()
    route = [r for r in cfg.routes if r.key == "GMP-CJU"][0]
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    today = dt.date(2026, 8, 1)
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    ok = engine.is_excluded_departure(cfg, dep)
    assert not ok, "테스트 날짜가 제외 요일이면 전제가 깨진다"

    st.record_leg(State.leg_key("GMP-CJU", "out", dep.isoformat()),
                  price=300_000, airline="구글편", dep_time="07:00",
                  carrier="7C", now=now)
    st.record_leg(State.leg_key("GMP-CJU", "ret", ret.isoformat()),
                  price=300_000, airline="구글편", dep_time="19:00",
                  carrier="7C", now=now)

    fresh_at = now.isoformat(timespec="seconds")

    # 네이버가 더 비싸면 무시
    st.naver_legs = {State.leg_key("GMP-CJU", "out", dep.isoformat()):
                     {"price": 400_000, "airline": "네이버편",
                      "dep_time": "07:30", "source": "naver", "at": fresh_at}}
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "구글편", c.out_leg

    # 네이버가 더 싸면 채택하고 출처를 남긴다
    st.naver_legs = {State.leg_key("GMP-CJU", "out", dep.isoformat()):
                     {"price": 200_000, "airline": "네이버편",
                      "dep_time": "07:30", "source": "naver", "at": fresh_at}}
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "네이버편" and c.out_leg["source"] == "naver"
    assert c.price == 200_000 + 300_000, c.price
    # 원본 구글 데이터는 그대로 남아 있어야 한다
    g = st.legs[State.leg_key("GMP-CJU", "out", dep.isoformat())]
    assert g["price"] == 300_000 and "source" not in g, g

    # 오래된 네이버 값은 쓰지 않는다 (v1.91).
    # 구글 leg는 만료되는데 네이버만 영원히 살아 있으면, 수집이 며칠 실패했을 때
    # 사라진 가격으로 알림이 나간다.
    old = (now - dt.timedelta(days=cfg.naver_freshness_days + 1)
           ).isoformat(timespec="seconds")
    st.naver_legs = {State.leg_key("GMP-CJU", "out", dep.isoformat()):
                     {"price": 100_000, "airline": "묵은네이버",
                      "dep_time": "07:30", "source": "naver", "at": old}}
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "구글편", "만료된 네이버 값이 쓰였다"
    # 수집 시각이 아예 없는 값도 신뢰하지 않는다
    st.naver_legs[State.leg_key("GMP-CJU", "out", dep.isoformat())].pop("at")
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "구글편", "시각 없는 네이버 값이 쓰였다"

    # 지난 날짜는 네이버 쪽도 정리된다
    st.naver_legs["GMP-CJU|out|2026-01-01"] = {"price": 1, "at": fresh_at}
    st.prune_past_legs(today)
    assert "GMP-CJU|out|2026-01-01" not in st.naver_legs, "과거 네이버 데이터가 남았다"
    print("OK 네이버 병합: 더 쌀 때만 채택 · 만료·과거 정리 · 구글 원본 보존")


def test_naver_schedule():
    """네이버 수집이 하루 여러 번 자동으로 이어받는지 (v1.74).

    '하루 1회'로 두었더니 145건을 한 번에 못 채워 뒷부분이 영영 밀렸고,
    수동 트리거를 매번 입력해야 해 실제로는 거의 안 돌았다.
    """
    from app.naver_collect import due_now
    cfg = load()
    today = dt.date(2026, 7, 26)
    n = cfg.naver_runs_per_day
    assert n >= 2, "1회면 한 바퀴를 못 채운다"

    # 시작 시각 전에는 쉰다
    assert not due_now({}, today, cfg.naver_hour - 1, cfg.naver_hour, n)
    # 오늘 첫 실행은 항상 수집
    assert due_now({}, today, cfg.naver_hour, cfg.naver_hour, n)
    # 한도 안이면 계속 이어받는다
    assert due_now({"naver_day": today.isoformat(), "naver_runs": n - 1},
                   today, 12, cfg.naver_hour, n)
    # 한도를 채우면 멈춘다
    assert not due_now({"naver_day": today.isoformat(), "naver_runs": n},
                       today, 12, cfg.naver_hour, n)
    # 날짜가 바뀌면 초기화
    assert due_now({"naver_day": "2026-07-25", "naver_runs": n},
                   today, 12, cfg.naver_hour, n)
    print(f"OK 네이버 일정: 하루 {n}회까지 자동 이어받기 · 날짜 바뀌면 초기화")


def test_naver_job_order():
    """네이버 수집은 **못 모은 것부터** 돈다 (v1.77).

    순번 커서를 쓰던 방식은 앞쪽(가는 편 53건)만 반복하고 뒤쪽(오는 편 92건)은
    예산이 모자라 영영 못 채웠다. 실제로 오는 편이 92건 중 1건이었다.
    """
    import datetime as _dt

    def order(jobs, known):
        js = list(jobs)
        js.sort(key=lambda j: known.get(
            f"{j[3]}|{j[2]}|{j[4].isoformat()}", {}).get("at", ""))
        return [f"{j[2]}:{j[4].day}" for j in js]

    d = _dt.date(2026, 9, 1)
    jobs = [("GMP", "CJU", "out", "GMP-CJU", d),
            ("GMP", "CJU", "out", "GMP-CJU", d + _dt.timedelta(1)),
            ("CJU", "GMP", "ret", "GMP-CJU", d),
            ("CJU", "GMP", "ret", "GMP-CJU", d + _dt.timedelta(1))]
    known = {
        f"GMP-CJU|out|{d.isoformat()}": {"at": "2026-07-26T10:00:00"},
        f"GMP-CJU|out|{(d + _dt.timedelta(1)).isoformat()}": {"at": "2026-07-26T09:00:00"},
    }
    got = order(jobs, known)
    # 못 모은 오는 편 둘이 먼저, 그다음 오래된 out(09시)부터
    assert got[:2] == ["ret:1", "ret:2"], got
    assert got[2] == "out:2" and got[3] == "out:1", got
    print("OK 네이버 순서: 미수집 우선 · 그다음 오래된 것부터")


def test_call_signatures():
    """main.py가 넘기는 인자를 함수가 실제로 받는지 (v1.89).

    2026-07-27 CI가 12초 만에 죽었다:
      · `collect() got an unexpected keyword argument 'delay'` — 시그니처 수정이
        한쪽에만 반영됐다
      · `UnboundLocalError: msg` — 변수 정의보다 앞에 코드를 넣었다
    둘 다 문법은 통과하므로 ast.parse로는 못 잡는다. 호출부와 정의를 대조한다.
    """
    import ast as _ast
    import inspect
    from app import naver_collect as NVC

    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text()
    tree = _ast.parse(src)

    # main.py 안의 NVC.collect(...) 호출에서 쓰는 키워드를 모은다
    used = set()
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        f = node.func
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name != "collect":
            continue
        used |= {kw.arg for kw in node.keywords if kw.arg}
    assert used, "main.py에서 collect 호출을 찾지 못했다 (테스트 전제 붕괴)"

    have = set(inspect.signature(NVC.collect).parameters)
    missing = used - have
    assert not missing, f"collect가 안 받는 인자를 넘기고 있다: {sorted(missing)}"

    # 반환값 개수도 맞아야 한다 (tuple 언패킹 실패는 런타임에야 터진다)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Call)
                and getattr(node.value.func, "attr", "") == "collect"
                and isinstance(node.targets[0], _ast.Tuple)):
            n = len(node.targets[0].elts)
            doc = inspect.getsource(NVC.collect)
            assert "-> tuple[dict, int, int, dict]" in doc and n == 4, (
                f"collect 반환값 {n}개로 받는데 정의와 다르다")
    print(f"OK 호출 시그니처: collect 인자 {len(used)}개·반환 4개 일치")


def test_baseline_unstick():
    """도달 불가능해진 기준가는 다시 올라간다 (v1.92).

    네이버가 만든 낮은 값에 기준가가 박힌 뒤 수집이 끊기면, 그 노선·월은
    영영 알림이 안 나간다. 최근 창 전체가 기준가를 못 건드렸으면 풀어준다.
    """
    cfg = load()
    n = cfg.baseline_unstick_days
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    st.meta["first_run"] = "2026-07-01"
    st.meta["baseline_metric"] = engine._METRIC   # 지표 초기화 방지
    today = dt.date(2026, 8, 20)
    route = cfg.routes[0]

    def combo(price, day=10):
        return engine.Combo(route=route, dep=dt.date(2026, 9, day), nights=3,
                            price=price,
                            out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                            ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})

    unit = combo(1).unit
    # 기준가는 250,000인데 최근 N일 내내 300,000대만 나왔다
    hist = {(today - dt.timedelta(days=i)).isoformat(): 300_000 + i * 100
            for i in range(1, n + 1)}
    st.baselines[unit] = {"daily_min": dict(hist), "baseline": 250_000,
                          "alltime_min": 250_000,
                          "alltime_min_at": "2026-07-01T00:00:00"}
    engine.process(cfg, st, [combo(305_000)], today)
    b = st.baselines[unit]
    assert b["baseline"] > 250_000, f"갇힌 기준가가 안 풀렸다: {b['baseline']}"
    assert "unstuck_at" in b

    # 창 안에서 한 번이라도 닿았으면 올리지 않는다
    st.baselines[unit] = {"daily_min": {**hist,
                                        today.isoformat(): 240_000},
                          "baseline": 250_000, "alltime_min": 240_000,
                          "alltime_min_at": "2026-07-01T00:00:00"}
    engine.process(cfg, st, [combo(260_000)], today)
    assert st.baselines[unit]["baseline"] <= 250_000, "닿았는데도 올렸다"
    print(f"OK 기준가 잠금 해제: 최근 {n}일 미달성 시 상향 · 닿으면 유지")


def test_mixed_source_links():
    """출처가 섞이면 **두 사이트 링크를 다 준다** (v2.02).

    가는 편과 오는 편을 다른 사이트에서 따로 사야 하는데 한쪽만 걸면
    나머지 편 가격이 그곳에 없다. 실측 조합 796개 중 52개가 혼합이다.
    """
    from app.notify import _blink, _sites
    cfg = load()
    route = [r for r in cfg.routes if r.key == "GMP-CJU"][0]

    def mk(src_out, src_ret):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, 10), nights=3, price=200_000,
            out_leg={"price": 100_000, "dep_time": "06:00", "airline": "A",
                     "carrier": "7C", **({"source": src_out} if src_out else {})},
            ret_leg={"price": 100_000, "dep_time": "21:00", "airline": "B",
                     "carrier": "LJ", **({"source": src_ret} if src_ret else {})})

    # 둘 다 네이버 → 네이버 하나 (주소가 짧다)
    c = mk("naver", "naver")
    assert "flight.naver" in _blink(cfg, c) and _sites(cfg, c) == ""
    # 둘 다 구글 → 구글 하나
    c = mk(None, None)
    assert "google.com" in _blink(cfg, c) and _sites(cfg, c) == ""
    # 섞이면 → 날짜는 글자, 줄 끝에 두 링크
    c = mk("naver", None)
    assert "<a href" not in _blink(cfg, c), _blink(cfg, c)
    s = _sites(cfg, c)
    assert "flight.naver" in s and "google.com" in s, s
    assert s.count("<a href") == 2, s
    # 알림·고정판·digest **세 곳 모두** 같은 규칙을 써야 한다
    import datetime as _dt
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    mixed = mk("naver", None)
    a = engine.Alert(kind="baseline", combo=mixed, baseline=250_000, prev_min=None)
    from app.notify import format_board, format_digest
    texts = (format_alerts(cfg, [a], [mixed])
             + format_board(cfg, [mixed], "07/27 23:59", _dt.date(2026, 8, 1))
             + format_digest(cfg, [mixed], "", _dt.date(2026, 8, 1)))
    for m in texts:
        assert "flight.naver" in m and "google.com" in m, m[:200]
    print("OK 혼합 출처: 알림·고정판·digest 모두 두 사이트 링크")


def test_unit_is_city():
    """기준가 단위는 **도시 × 월** 하나여야 한다 (v2.07).

    예전엔 오사카 9월 하나에 단위가 4개였다(인천왕복·김포왕복·교차 2종).
    각자 기준가를 가지니 **비싼 조합이 "그 단위 기준 13% 싸다"며 알림**이
    나갔고 더 싼 조합은 조용했다. 사용자는 '오사카'로 보는데 시스템은
    넷으로 쪼개 봤다.
    """
    cfg = load()
    gmp = [r for r in cfg.routes if r.key == "GMP-KIX"][0]
    icn = [r for r in cfg.routes if r.key == "ICN-KIX"][0]

    def mk(route, back=None, price=500_000):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, 10), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"},
            ret_route=back,
            city=engine._seoul_group(cfg, route))

    units = {mk(icn).unit, mk(gmp).unit, mk(icn, gmp).unit, mk(gmp, icn).unit}
    assert len(units) == 1, f"오사카 9월인데 단위가 {len(units)}개: {units}"
    assert units.pop() == "KIX|2026-09"

    # 도시가 다르면 단위도 다르다
    ngo = [r for r in cfg.routes if r.key == "ICN-NGO"][0]
    assert mk(ngo).unit != mk(icn).unit

    # 새로 생긴 단위는 그날 알리지 않는다 (심기만 한다)
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    st.meta["first_run"] = "2026-07-01"
    st.meta["baseline_metric"] = engine._METRIC
    today = dt.date(2026, 8, 20)
    assert engine.process(cfg, st, [mk(icn, price=100_000)], today) == [], "새 단위가 알렸다"
    assert st.baselines[mk(icn).unit].get("_seeded") == today.isoformat()
    print("OK 기준가 단위: 도시×월 하나 · 새 단위는 첫날 알리지 않음")


def test_verify_targets_catch_skew():
    """왕복 검증 대상에 **배율이 큰 조합**이 들어가야 한다 (v2.09).

    편도 합산 순위만 보면, 오는 편 편도가 폭등한 조합은 뒤로 밀려 후보에도
    못 오른다. 실측: 나고야 8/8~8/12는 편도합산 1인 52만이라 60개 중 한참
    뒤였지만 구글 왕복으로는 18만이었다.
    """
    cfg = load()
    route = cfg.routes[0]

    def mk(day, out_p, ret_p):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, day), nights=3,
            price=out_p + ret_p,
            out_leg={"price": out_p, "dep_time": "07:30", "airline": "X"},
            ret_leg={"price": ret_p, "dep_time": "19:40", "airline": "X"},
            city=engine._seoul_group(cfg, route))

    cheap = [mk(d, 100_000, 120_000) for d in range(1, 6)]      # 합산 220,000
    skew = mk(10, 100_000, 900_000)                              # 합산 1,000,000
    targets = engine.verify_targets(cfg, cheap + [skew])
    assert skew in targets, "배율 큰 조합이 검증 대상에서 빠졌다"
    assert any(c in targets for c in cheap), "싼 조합도 함께 봐야 한다"

    # 왕복 실가가 붙으면 '실제 낼 금액'이 그걸 따른다
    assert skew.pay == 1_000_000
    skew.rt_price = 300_000
    assert skew.pay == 300_000, "왕복이 싼데 편도 합산을 쓰고 있다"
    print("OK 왕복 검증 대상: 싼 것 + 배율 큰 것 · pay는 싼 쪽을 따른다")


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
    """노선별 값은 '넓힌 창'이고, 선호 창은 전역값 그대로여야 한다 (v1.34).

    넓힌 창은 검색 범위로 쓰고, 선호 창은 '이 편이 선호 시간 밖인가'를
    판정해 알림에 표시하는 기준으로 쓴다.
    """
    cfg = load()
    # 선호 창은 노선과 무관하게 항상 전역값이어야 한다
    for r in cfg.routes:
        assert cfg.window_for(r.key, "out") == cfg.outbound_window, r.key
        assert cfg.window_for(r.key, "ret") == cfg.return_window, r.key

    relaxed = [r.key for r in cfg.routes if cfg.has_window_override(r.key)]
    assert relaxed, "넓힌 창이 설정된 노선이 없다 (테스트 전제 붕괴)"
    for key in relaxed:
        for d, glob in (("out", cfg.outbound_window), ("ret", cfg.return_window)):
            fb = cfg.fallback_window_for(key, d)
            if fb is None:
                continue
            # 양보 창은 선호 창을 포함하면서 더 넓어야 의미가 있다
            assert fb[0] <= glob[0] and fb[1] >= glob[1], (key, d, fb, glob)
            assert fb != glob, f"{key} {d}: 넓힌 창이 전역값과 같아 무의미"

    koj = cfg.fallback_window_for("ICN-KOJ", "out")
    cts = cfg.fallback_window_for("ICN-CTS", "ret")
    assert koj and koj[1] > cfg.outbound_window[1], "가고시마 넓힌 창 미설정"
    assert cts and cts[0] < cfg.return_window[0], "삿포로 넓힌 창 미설정"
    print(f"OK 넓힌 창 설정: ICN-KOJ 가는편 ~{koj[1]:%H:%M}, "
          f"ICN-CTS 오는편 {cts[0]:%H:%M}~ (선호 창은 전역 유지)")


def test_widened_window_marks_off_preference():
    """넓힌 창 노선은 그 안에서 최저가를 고르되, 선호 시간 밖이면 표시한다.

    가고시마 out은 실측 1/14(7%), 삿포로 ret은 2/18(11%)만 선호 시간 안이다.
    이런 노선에 '오전 우선'을 강제하면 오버라이드를 준 이유와 모순되고,
    드물게 있는 비싼 오전 편을 '특가'라며 알리게 된다.
    대신 선호 시간 밖이면 알림에 반드시 표시해 사용자가 판단하게 한다.
    """
    import datetime as _dt
    from app import search as S

    class _T:
        def __init__(self, h):
            self.time = [h, 0]

    class _Seg:
        def __init__(self, h):
            self.departure, self.arrival = _T(h), _T(h + 2)
            self.from_airport = type("A", (), {"code": "ICN", "name": "I"})()
            self.to_airport = type("A", (), {"code": "KOJ", "name": "K"})()

    class _It:
        def __init__(self, h, price):
            self.flights, self.price, self.airlines = [_Seg(h)], price, ["KE"]
            self.type = "KE"

    pref = (_dt.time(6, 0), _dt.time(13, 0))
    wide = (_dt.time(6, 0), _dt.time(17, 0))
    orig = S._do_fetch
    try:
        # 넓힌 창 안 최저가를 고르고, 선호 밖이므로 표시
        S._do_fetch = lambda *a, **k: [_It(9, 500_000), _It(16, 400_000)]
        r = S.search_leg("ICN", "KOJ", "2026-08-01", adults=2, window=wide,
                         preferred_window=pref, retries=1)
        assert r and r.price == 400_000 and r.off_window, r

        # 최저가가 선호 시간 안이면 표시하지 않는다
        S._do_fetch = lambda *a, **k: [_It(9, 300_000), _It(16, 400_000)]
        r = S.search_leg("ICN", "KOJ", "2026-08-01", adults=2, window=wide,
                         preferred_window=pref, retries=1)
        assert r and r.price == 300_000 and not r.off_window, r

        # 넓힌 창이 없는 일반 노선은 선호 창이 절대 기준 (16시 편은 아예 후보 밖)
        S._do_fetch = lambda *a, **k: [_It(16, 100_000)]
        r = S.search_leg("ICN", "NGO", "2026-08-01", adults=2, window=pref,
                         preferred_window=None, retries=1)
        assert r is None, r
    finally:
        S._do_fetch = orig
    print("OK 넓힌 창: 그 안 최저가 채택 · 선호 밖이면 표시 · 일반 노선 불변")


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
    assert "원</b>/인" in msg
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
    assert "<a href=" in msg, msg              # 링크

    # 항공사 코드가 들어가면 링크가 달라져야 한다
    with_code = google_flights_url(route, combo.dep, combo.ret, cfg.adults, ["7C"])
    without = google_flights_url(route, combo.dep, combo.ret, cfg.adults, [])
    assert with_code != without and "tfs=" in with_code
    # 코드를 모르면 필터 없는 링크 + 다른 라벨
    combo.out_leg["carrier"] = combo.ret_leg["carrier"] = ""
    msg2 = format_alerts(cfg, [a])[0]
    assert "구글</a>" in msg2, msg2
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
    assert engine.city_label(cfg, r2) in msgs[0], "더 싼 도시의 메시지가 먼저 와야 한다"

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
    assert "지난 알림보다 50,000원 더 내림" in msg, msg  # 100,000 / 2명
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
    assert msg.split("다른 날짜")[0].count("3박") == 1, msg

    # 10% 차이면 둘 다 보여야 한다
    msg = format_alerts(cfg, [mk(10, 800_000), mk(11, 880_000)])[0]
    assert msg.split("다른 날짜")[0].count("3박") == 2, msg
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

    # 샤드가 여러 개면, 같은 샤드가 반복돼도 완주로 오인하지 않는다
    # (샤드가 1이면 매 실행이 곧 완주이므로 이 검사는 해당 없음)
    if cfg.shards > 1:
        st.meta["cycle_shards"] = []
        for _ in range(cfg.shards + 2):
            _, _, done = engine.note_shard(cfg, st, 0)
            assert not done, "같은 샤드 반복은 완주가 아니다"
    else:
        st.meta["cycle_shards"] = []
        _, _, done = engine.note_shard(cfg, st, 0)
        assert done, "샤드가 1이면 한 번 실행이 곧 완주여야 한다"

    # 보고 정책 (v1.47부터 기본은 off — 고정판이 그 역할을 대신한다)
    import copy
    d = copy.copy(cfg)
    d.cycle_report = "daily"
    d.digest_hour = 0                     # 시각 조건은 여기서 검증하지 않는다
    first = engine.cycle_report(d, st, today, 1017)
    assert first and "1,017" in first, first
    assert engine.cycle_report(d, st, today, 1017) is None, "하루 두 번 보고됨"

    # off면 아예 보내지 않는다. YAML이 따옴표 없는 off를 False로 읽어
    # 두 분기를 모두 비껴가 매번 발송되던 버그가 있었다 (v1.47)
    from app.settings import _cycle_policy
    assert _cycle_policy(False) == "off" and _cycle_policy("off") == "off"
    o = copy.copy(cfg)
    o.cycle_report = "off"
    st.meta.pop("last_cycle_report", None)
    assert engine.cycle_report(o, st, today, 1017) is None, "off인데 발송됨"
    print("OK 한 바퀴: 완주 판정·중복 방지·하루 1회 보고")


def test_near_dates_linked():
    """근처 날짜 목록의 각 줄이 눌러서 갈 수 있는 링크인지 (v1.31)."""
    cfg = load()
    r = cfg.routes[0]

    def mk(day, price):
        return engine.Combo(
            route=r, dep=dt.date(2026, 9, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    top = mk(10, 800_000)
    a = engine.Alert(kind="baseline", combo=top, baseline=850_000, prev_min=None)
    near = [mk(12, 820_000), mk(14, 840_000)]
    msg = format_alerts(cfg, [a], [top] + near, today=dt.date(2026, 8, 1))[0]

    # v1.38부터 근처 날짜는 별도 구역이 아니라 본문에 오름차순으로 섞인다.
    body = msg.split("다른 날짜")[1] if "다른 날짜" in msg else ""
    assert body.count("<a href=") == len(near), msg
    assert "9/12" in body, body                         # 날짜·요일
    assert "410,000" in body, body                      # 820,000 / 2명
    assert "D-" not in body, "D-day는 빼기로 했다"

    # 메시지 전체가 오름차순이어야 한다 (13만 → 16만 → 13만 사태 방지)
    import re as _re
    shown = [int(x.replace(",", "")) for x in
             _re.findall(r"([\d,]+)원/인", _re.sub(r"</?b>", "", msg))]
    assert shown == sorted(shown), f"금액이 오름차순이 아니다: {shown}"
    print("OK 근처 날짜: 본문에 오름차순 통합 · 링크·시각·항공사 포함")


def test_time_histogram():
    """시간창에 걸려 탈락한 편까지 포함해 출발 시각이 집계되는지 (v1.32)."""
    import datetime as _dt
    from app import search as S

    class _T:
        def __init__(self, h, m=0):
            self.time = [h, m]

    class _Seg:
        def __init__(self, h):
            self.departure, self.arrival = _T(h), _T(h + 2)
            self.from_airport = type("A", (), {"code": "ICN", "name": "I"})()
            self.to_airport = type("A", (), {"code": "NGO", "name": "N"})()

    class _It:
        def __init__(self, h, price):
            self.flights, self.price, self.airlines = [_Seg(h)], price, ["Jeju Air"]
            self.type = "7C"

    S._time_hist.clear()
    window = (_dt.time(6, 0), _dt.time(13, 0))
    # 07시(창 안) · 15시·19시(창 밖) → 셋 다 집계되어야 한다
    S._pick_best([_It(7, 300_000), _It(15, 200_000), _It(19, 100_000)],
                 window, True, "2026-08-01", "ICN", "NGO")
    hours = S.time_histogram()[("ICN", "NGO")]
    assert hours == {7: 1, 15: 1, 19: 1}, hours

    # 창 안 편만 채택돼야 한다 (더 싼 15시·19시를 고르면 안 됨)
    best = S._pick_best([_It(7, 300_000), _It(15, 200_000)], window, True,
                        "2026-08-01", "ICN", "NGO")
    assert best is not None and best.price == 300_000, best
    print("OK 시간 분포: 탈락편 포함 집계 · 채택은 창 안에서만")


def test_off_window_mark_is_short():
    """선호 시간 밖 표시는 시각 옆 ⚠ 한 글자로 (v1.35)."""
    cfg = load()
    r = [x for x in cfg.routes if x.key == "ICN-KOJ"][0]
    c = engine.Combo(route=r, dep=dt.date(2026, 8, 12), nights=3, price=900_000,
                     out_leg={"price": 1, "airline": "대한항공",
                              "dep_time": "16:20", "carrier": "KE",
                              "off_window": True},
                     ret_leg={"price": 1, "airline": "대한항공",
                              "dep_time": "18:55", "carrier": "KE"})
    a = engine.Alert(kind="baseline", combo=c, baseline=950_000, prev_min=None)
    msg = format_alerts(cfg, [a])[0]
    assert "16:20⚠ → 18:55" in msg, msg       # 조건 밖인 쪽에만 ⚠
    assert "선호 시간대 밖입니다" not in msg      # 긴 설명 줄은 삭제
    print("OK 선호시간 밖 표시: 해당 시각 옆 ⚠ 한 글자")


def test_time_hist_persisted():
    """시간 분포가 실행마다 누적 저장되는지 (v1.36).

    이 값은 원래 로그에만 있어 옮겨 보기가 번번이 실패했다.
    data/time_hist.json 에 쌓아두면 저장소에서 바로 확인할 수 있다.
    """
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}

    st.merge_time_hist({("ICN", "KOJ"): {8: 1, 16: 13}})
    st.merge_time_hist({("ICN", "KOJ"): {16: 5}, ("GMP", "CJU"): {6: 4}})

    koj = st.time_hist["ICN-KOJ"]
    assert koj == {"8": 1, "16": 18}, koj          # 실행 간 누적
    assert st.time_hist["GMP-CJU"] == {"6": 4}
    assert st.time_hist["_runs"] == 2, st.time_hist
    assert "_updated" in st.time_hist
    print("OK 시간 분포 저장: 실행 간 누적 · 노선별 분리")


def test_cross_airport_combos():
    """인천/김포 교차 조합이 만들어지고, 어느 공항인지 반드시 표시되는지 (v1.41).

    실측 근거: `김포→나고야 / 나고야→인천`이 같은 공항 왕복보다 1인 최대
    184,600원 쌌다. 김포발 피치가 싼데 나고야→김포는 18시 이후 편이 없어
    같은 공항끼리만 묶으면 그 가는 편이 버려지기 때문. 추가 검색은 0건이다.
    """
    cfg = load()
    # 특정 노선을 박아두면 노선 구성이 바뀔 때 테스트가 깨진다.
    # 같은 도시에 서울발 노선이 둘 이상인 곳을 찾아 쓴다.
    groups: dict = {}
    for r in cfg.routes:
        groups.setdefault(engine._seoul_group(cfg, r), []).append(r)
    pair = next((v for v in groups.values() if len(v) >= 2), None)
    assert pair, "교차 가능한 도시가 없다 (노선 구성 확인 필요)"
    gmp, icn = pair[0], pair[1]

    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    today = dt.date(2026, 8, 1)
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    # 김포 출발은 싸고, 김포로 돌아오는 편은 없음 → 인천 귀국만 가능
    st.record_leg(State.leg_key(gmp.key, "out", dep.isoformat()),
                  price=200_000, airline="Jin Air", dep_time="11:20",
                  carrier="LJ", now=now)
    st.record_leg(State.leg_key(icn.key, "out", dep.isoformat()),
                  price=400_000, airline="Jin Air", dep_time="07:30",
                  carrier="LJ", now=now)
    st.record_leg(State.leg_key(icn.key, "ret", ret.isoformat()),
                  price=300_000, airline="Jeju Air", dep_time="19:00",
                  carrier="7C", now=now)

    combos = engine.build_combos(cfg, st, today)
    pairs = {(c.route.key, c.back.key): c for c in combos
             if c.dep == dep and c.nights == 3}
    assert (gmp.key, icn.key) in pairs, "교차 조합이 안 만들어졌다"
    assert (icn.key, icn.key) in pairs, "같은 공항 조합도 있어야 한다"

    cross = pairs[(gmp.key, icn.key)]
    same = pairs[(icn.key, icn.key)]
    assert cross.is_cross and not same.is_cross
    assert cross.price == 500_000 < same.price == 700_000, (cross.price, same.price)
    # 중복 억제용 key는 달라야 하지만, **기준가 단위는 같아야 한다**
    # (v2.07: 같은 도시·같은 달이면 하나의 잣대로 비교한다)
    assert cross.key != same.key, "중복 억제 키가 겹치면 안 된다"
    assert cross.unit == same.unit, "같은 도시·달인데 기준가 단위가 갈렸다"

    # 메시지에 공항이 반드시 드러나야 한다 (엉뚱한 공항으로 가면 비행기를 놓친다)
    a = engine.Alert(kind="baseline", combo=cross, baseline=600_000, prev_min=None)
    msg = format_alerts(cfg, [a], combos)[0]
    from app.notify import _ko
    assert f"{_ko(gmp.origin)} 출발" in msg and f"{_ko(icn.origin)} 귀국" in msg, msg
    assert engine.city_label(cfg, gmp) in msg.splitlines()[0], msg.splitlines()[0]
    print("OK 교차 조합: 생성·키 분리·공항 명시")


def test_weak_alert_suppressed():
    """기준가와 사실상 같은 가격은 알리지 않는다 (v1.42).

    실측에서 알림 90건 중 55건이 기준가 대비 1% 미만이었다. 특가가 아닌데
    알림이 가면 진짜 특가가 묻힌다. 역대 최저 갱신은 이 조건과 무관하게 알린다.
    """
    cfg = load()
    assert cfg.min_below_baseline_pct > 0, "임계가 0이면 이 보호가 무력하다"
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    st.meta["first_run"] = "2026-07-01"          # 관측 기간 종료 상태
    st.meta["baseline_metric"] = engine._METRIC
    today = dt.date(2026, 8, 1)
    route = cfg.routes[0]

    def combo(price):
        return engine.Combo(route=route, dep=dt.date(2026, 9, 10), nights=3,
                            price=price,
                            out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                            ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})

    unit = combo(1_000_000).unit
    # 기준가는 최근 daily_min의 중앙값이므로, 여러 날치를 채워야 현실적이다.
    # 하루치만 두면 새 최저가가 곧바로 기준가가 돼 비교 자체가 성립하지 않는다.
    hist = {(today - dt.timedelta(days=i)).isoformat(): 1_000_000
            for i in range(1, 8)}

    def reset():
        st.baselines[unit] = {"daily_min": dict(hist), "baseline": 1_000_000,
                              "alltime_min": 900_000,
                              "alltime_min_at": "2026-07-01T00:00:00"}
        st.alerts_sent = {}

    reset()   # 기준가와 같은 값 → 알림 없음
    assert engine.process(cfg, st, [combo(1_000_000)], today) == []
    reset()   # 1% 낮음 → 임계(2%) 미달이라 알림 없음
    assert engine.process(cfg, st, [combo(990_000)], today) == []
    reset()   # 5% 낮음 → 알림
    al = engine.process(cfg, st, [combo(950_000)], today)
    assert len(al) == 1, al
    print("OK 약한 알림 차단: 기준가 대비 임계 미만은 발송하지 않음")


def test_header_matches_cheapest_shown():
    """제목의 'N원부터'가 메시지에 실린 것 중 최저가와 일치하는지 (v1.44).

    알림 항목만 보고 제목을 정하면, 더 싼 근처 날짜가 바로 아래 있는데도
    제목이 비싼 값을 말한다. 실측 나고야에서 제목 368,688원 / 본문 282,139원.
    """
    import re as _re
    cfg = load()
    r = cfg.routes[0]

    def mk(day, price, nights=3):
        return engine.Combo(
            route=r, dep=dt.date(2026, 9, day), nights=nights, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    alert_combo = mk(10, 800_000)
    a = engine.Alert(kind="baseline", combo=alert_combo, baseline=900_000,
                     prev_min=None)
    cheaper = mk(14, 600_000)          # 알림은 아니지만 더 싸다
    msg = format_alerts(cfg, [a], [alert_combo, cheaper])[0]

    head = msg.splitlines()[0]
    shown = [int(x.replace(",", "")) for x in
             _re.findall(r"([\d,]+)원", _re.sub(r"</?b>", "", msg))]
    assert f"{300_000:,}원</b>/인" in head, head   # 600,000 / 2명
    assert min(shown) == 300_000, (head, shown)

    # 같은 날 같은 값이면 박 수가 긴 쪽만 (3박·4박 중복 제거)
    dup3, dup4 = mk(16, 620_000, 3), mk(16, 620_000, 4)
    msg2 = format_alerts(cfg, [a], [alert_combo, dup3, dup4])[0]
    assert msg2.count("9/16") == 1, msg2
    assert "4박" in [l for l in msg2.splitlines() if "9/16" in l][0], msg2
    # 제목 금액은 본문 최저와 같아야 한다 (근처 날짜가 더 쌀 수 있다)
    print("OK 제목 금액: 실제 최저와 일치 · 같은 날 같은 값은 긴 박 수만")


def test_digest():
    """조용한 날 볼 수 있는 도시별 최저가 요약 (v1.45).

    한 번 알린 조합은 더 싸지기 전엔 다시 알리지 않으므로, 알림이 없는 날에
    현재 시세를 확인할 창구가 필요하다.
    """
    from app.notify import format_digest
    cfg = load()
    r1, r2 = cfg.routes[0], cfg.routes[1]

    def mk(route, day, price):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    combos = [mk(r1, 10, 900_000), mk(r1, 12, 700_000), mk(r2, 11, 500_000)]
    msgs = format_digest(cfg, combos, "테스트", dt.date(2026, 8, 1))
    msg = "\n".join(msgs)

    # 도시가 제목, 그 밑에 날짜 여러 줄. 싼 도시부터.
    assert msg.count("원~") == 2, msg
    i2 = msg.index(engine.city_label(cfg, r2))
    i1 = msg.index(engine.city_label(cfg, r1))
    assert i2 < i1, "더 싼 도시가 먼저 와야 한다"
    assert "250,000원~" in msg, msg      # 500,000 / 2명
    assert "350,000원~" in msg, msg      # 700,000 / 2명
    assert "450,000" in msg, "도시 안에서는 여러 날짜를 보여준다"
    assert msg.count("<a href=") == 3, "날짜마다 링크"

    # 도시별 표시 개수는 설정을 따른다
    many = [mk(r1, d, 900_000 - d * 1000) for d in range(1, 9)]
    msg3 = "\n".join(format_digest(cfg, many, "", dt.date(2026, 8, 1)))
    assert msg3.count("<a href=") == cfg.digest_top_n, msg3

    # 텔레그램 4096자 제한을 넘으면 나눠 보낸다 (실측 8도시×3날짜 = 6,400자)
    from app.notify import TELEGRAM_LIMIT
    big = [mk(r, d, 500_000 + i * 1000)
           for i, r in enumerate(cfg.routes) for d in range(1, 6)]
    parts = format_digest(cfg, big, "긴 경우", dt.date(2026, 8, 1))
    assert len(parts) > 1, "안 나뉘었다"
    for pmsg in parts:
        assert len(pmsg) < TELEGRAM_LIMIT, len(pmsg)
    # 도시가 통째로 한 통 안에 있어야 한다 (블록이 쪼개지면 안 됨)
    joined = "\n".join(parts)
    for r in cfg.routes:
        assert engine.city_label(cfg, r) in joined

    # 콤보가 없어도 죽지 않는다
    empty = format_digest(cfg, [], "", dt.date(2026, 8, 1))
    assert len(empty) == 1 and "아직 비교할 조합이 없습니다" in empty[0]
    print(f"OK 다이제스트: 도시 제목 + 날짜 {cfg.digest_top_n}개·싼 순·링크·빈 데이터 방어")


def test_live_board():
    """고정판은 반드시 한 통에 들어가야 한다 (v1.47).

    수정(editMessageText)으로 갱신하는 구조라 여러 통으로 나눌 수 없다.
    노선이 늘어도 4096자를 넘지 않는지 지킨다.
    """
    from app.notify import format_board, TELEGRAM_LIMIT
    cfg = load()

    def mk(route, day, price):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    # 전 노선 × 넉넉한 날짜로 최악을 가정해도 한 통에 들어가야 한다
    big = [mk(r, d, 300_000 + i * 7000 + d * 100)
           for i, r in enumerate(cfg.routes) for d in range(1, 15)]
    parts = format_board(cfg, big, "07/26 14:07", dt.date(2026, 8, 1))
    for pm in parts:
        assert len(pm) < TELEGRAM_LIMIT, f"한 통이 {len(pm)}자로 제한을 넘었다"
    board = "\n".join(parts)

    # v1.98: **모든 날짜에 링크**. 그래서 여러 통으로 나눈다.
    cities = {engine._seoul_group(cfg, r) for r in cfg.routes}
    assert board.count("<a href=") == len(cities) * cfg.board_top_n, board.count("<a href=")
    assert "📌" in board.splitlines()[0]

    # 자동 맞춤: 여유가 있으면 많이, 없으면 줄인다 (v1.48)
    small = [mk(r, d, 300_000 + d * 100) for r in cfg.routes[:2] for d in range(1, 15)]
    b_small = "\n".join(format_board(cfg, small, "07/26 14:07", dt.date(2026, 8, 1)))

    # 빈 데이터에도 죽지 않는다
    assert "아직" in format_board(cfg, [], "07/26 14:07", dt.date(2026, 8, 1))[0]
    print(f"OK 고정판: {len(parts)}통 · 모든 날짜 링크 · 도시당 {cfg.board_top_n}개")


def test_board_ids_compact():
    """고정판 상태는 해시만 남긴다 (v1.49).

    전문을 meta.json에 넣으면 실행마다 4KB가 커밋에 쌓인다. 실제로 그렇게
    돌고 있었고, 수정 성공 시 그 값을 갱신하지도 않아 비교가 무용지물이었다.
    """
    import os
    from app import notify as N

    calls = []

    def fake_post(token, method, payload):
        calls.append(method)
        return {"message_id": 112} if method == "sendMessage" else {"ok": True}

    old_post, old_env = N._post, dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "999"
    N._post = fake_post
    try:
        ids = N.upsert_board("첫 내용", {})
        assert ids["999"] == [112] and calls == ["sendMessage"], (ids, calls)

        calls.clear()                      # 내용 동일 → 호출 없음
        ids = N.upsert_board("첫 내용", ids)
        assert calls == [], calls

        calls.clear()                      # 내용 변경 → 수정
        ids = N.upsert_board("바뀐 내용", ids)
        assert calls == ["editMessageText"], calls

        calls.clear()                      # 통이 늘면 새 통만 발송
        ids = N.upsert_board(["바뀐 내용", "둘째 통"], ids)
        assert calls == ["sendMessage"] and len(ids["999"]) == 2, (calls, ids)

        # 전문은 저장하지 않는다 (해시만)
        assert not any(str(k).endswith(":text") for k in ids), ids
        assert all(len(str(v)) < 60 for v in ids.values()), ids
        # 예전 형식(:text)이 남아 있어도 정리된다
        ids2 = N.upsert_board("또 바뀜", {**ids, "999:text": "x" * 4000})
        assert not any(str(k).endswith(":text") for k in ids2)
    finally:
        N._post = old_post
        os.environ.clear()
        os.environ.update(old_env)
    print("OK 고정판 상태: 해시만 저장 · 동일 내용은 호출 생략")


def test_exclude_airlines():
    """제외 항공사는 수집·조합 양쪽에서 빠져야 한다 (v1.50).

    수집에서만 막으면 이미 저장된 다리가 leg_freshness_days 동안 남아
    설정을 바꿔도 며칠간 계속 노출된다. 그래서 조합 단계에서도 거른다.
    """
    from app import search as S
    cfg = load()
    assert "MM" in cfg.exclude_airlines, cfg.exclude_airlines

    S.set_excluded_airlines(cfg.exclude_airlines)
    try:
        assert S.is_excluded("MM", "Peach Aviation")
        assert S.is_excluded("", "Peach")          # 코드 없어도 이름으로
        assert not S.is_excluded("7C", "Jeju Air")
        assert not S.is_excluded("TW", "Trinity Airways")
    finally:
        S.set_excluded_airlines([])

    # 저장된 다리도 조합에서 빠진다
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    route = cfg.routes[0]
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    st.record_leg(State.leg_key(route.key, "out", dep.isoformat()),
                  price=100_000, airline="Peach", dep_time="07:30",
                  carrier="MM", now=now)
    st.record_leg(State.leg_key(route.key, "ret", ret.isoformat()),
                  price=100_000, airline="Jeju Air", dep_time="19:00",
                  carrier="7C", now=now)
    assert engine.build_combos(cfg, st, dt.date(2026, 8, 1)) == [], "제외 항공사가 조합에 남았다"

    # 제외 항공사가 아니면 정상 생성
    st.record_leg(State.leg_key(route.key, "out", dep.isoformat()),
                  price=100_000, airline="Jin Air", dep_time="07:30",
                  carrier="LJ", now=now)
    assert engine.build_combos(cfg, st, dt.date(2026, 8, 1)), "정상 조합까지 막혔다"
    print("OK 제외 항공사: 수집·조합 양쪽에서 차단, 코드·이름 모두 인식")


def test_poll_commands():
    """텔레그램 명령은 허용된 방에서 온 것만, offset은 전진해야 한다 (v1.52)."""
    import os
    from app import notify as N

    payload = {"result": [
        {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/digest"}},
        {"update_id": 11, "message": {"chat": {"id": 999}, "text": "안녕"}},
        {"update_id": 12, "message": {"chat": {"id": 555}, "text": "/digest"}},
        {"update_id": 13, "message": {"chat": {"id": 999}, "text": "/Help@mybot"}},
    ]}

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return payload

    old_get, old_env = N.requests.get, dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "999"
    N.requests.get = lambda *a, **k: _R()
    try:
        cmds, nxt = N.poll_commands(0)
        assert nxt == 14, nxt                       # 마지막 update_id + 1
        assert cmds == [("999", "digest", ""), ("999", "help", "")], cmds
        # 남의 방(555)에서 온 명령은 무시 — 아무나 조회를 돌리게 두면 안 된다
        assert all(c == "999" for c, _, _ in cmds)

    finally:
        N.requests.get = old_get
        os.environ.clear()
        os.environ.update(old_env)

    # 월 인자 해석
    assert N.parse_month("8월", "") == 8
    assert N.parse_month("digest", "8") == 8
    assert N.parse_month("digest", "8월") == 8
    assert N.parse_month("digest", "") is None
    assert N.parse_month("13", "") is None      # 없는 달은 무시
    print("OK 텔레그램 명령: 허용 방만 · @봇이름 처리 · offset 전진 · 월 인자")


def test_month_filter():
    """월 요약은 그 달 출발만, 한 통에 (v1.53)."""
    from app.notify import format_board, format_digest, TELEGRAM_LIMIT
    cfg = load()
    r = cfg.routes[0]

    def mk(month, day, price):
        return engine.Combo(
            route=r, dep=dt.date(2026, month, day), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "제주항공",
                     "carrier": "7C"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "제주항공",
                     "carrier": "7C"})

    combos = [mk(8, 12, 500_000), mk(9, 12, 400_000), mk(10, 12, 300_000)]
    b8 = "\n".join(format_board(cfg, combos, "07/26 15:40", dt.date(2026, 8, 1), month=8))
    assert "8월 출발" in b8 and "8/12" in b8, b8
    assert "9/12" not in b8 and "10/12" not in b8, b8
    pass

    # 해당 월 조합이 없으면 그렇게 알려준다
    b12 = format_board(cfg, combos, "07/26 15:40", dt.date(2026, 8, 1), month=12)[0]
    assert "12월 출발 조합이 아직 없습니다" in b12, b12
    d12 = format_digest(cfg, combos, "", dt.date(2026, 8, 1), month=12)
    assert len(d12) == 1 and "12월 출발 조합이 아직 없습니다" in d12[0]
    print("OK 월 요약: 해당 월만 · 한 통 · 없으면 안내")


def test_run_mode_parsing():
    """깃허브 dry_run 입력과 텔레그램 명령이 같은 규칙으로 해석되는지 (v1.55).

    텔레그램에만 월 지정을 붙이고 깃허브 입력을 안 맞춰서, 같은 기능인데
    한쪽에서만 되던 상태였다.
    """
    from app.notify import parse_month

    def interpret(raw: str):
        parts = raw.strip().lower().split()
        mode = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        month = parse_month(mode, arg)
        return {
            "dry": mode == "1",
            "preview": mode == "preview",
            "digest": mode == "digest",
            "month": month,
            "brief": bool(month) and mode != "digest",
        }

    assert interpret("") == {"dry": False, "preview": False, "digest": False,
                             "month": None, "brief": False}
    assert interpret("1")["dry"] is True
    assert interpret("preview")["preview"] is True
    assert interpret("digest") == {"dry": False, "preview": False, "digest": True,
                                   "month": None, "brief": False}
    assert interpret("digest 8")["digest"] and interpret("digest 8")["month"] == 8
    assert interpret("digest 8")["brief"] is False
    assert interpret("8")["brief"] and interpret("8")["month"] == 8
    assert interpret("8월")["brief"] and interpret("8월")["month"] == 8
    assert interpret("13")["month"] is None      # 없는 달은 실전으로 떨어진다
    # 보기 전용 모드는 검색도 저장도 하지 않아야 한다 (v1.56).
    # main.py에서 이 순서가 지켜지는지 소스로 확인한다 — 순서가 뒤집히면
    # 조회 한 번에 고정판이 덮어써지거나 수집분이 날아간다.
    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text()
    i_skip = src.index("보기 전용 모드 → 검색 건너뜀")
    i_search = src.index("검색 완료: %d 시도")
    i_brief = src.index("월 요약 전송")
    i_board = src.index("고정판 갱신 완료")
    # naver-run 모드가 자체 저장을 하므로 '마지막' 저장 지점을 본다
    i_save = src.rindex("state.save()")
    assert i_skip < i_search, "검색 건너뛰기가 검색보다 뒤에 있다"
    assert i_brief < i_board < i_save, "월 요약이 고정판·저장보다 뒤에 있다"
    print("OK 실행 모드 해석: 깃허브·텔레그램 동일 규칙 · 보기 전용은 검색·저장 없음")


def test_naver_parser():
    """네이버 결과행 파서 — 탐침으로 확보한 실물 문자열 기준 (v1.67).

    실적 조건이 붙은 가격은 버린다: 카드 이용실적을 채워야 살 수 있는 값이라
    대부분 실제로는 그 가격에 못 산다. 알림에 띄우면 "가서 보니 더 비싸다"가 된다.
    """
    import datetime as _dt
    from app import naver as NV

    # ── 국내선 (실물) ──
    dom = "파라타항공 | 06:00GMP | 07:15CJU | 01시간 15분 | 특가석편도 51,200원~"
    r = NV.parse_domestic(dom)
    assert r and r["airline"] == "파라타항공", r
    assert r["from"] == "GMP" and r["to"] == "CJU"
    assert r["dep"] == _dt.time(6, 0) and r["arr"] == _dt.time(7, 15)
    assert r["seat"] == "특가석" and r["price"] == 51_200

    # ── 국제선 (실물) ──
    intl = ("제주항공 | 07:10ICN | 09:05KIX | 직항, 01시간 55분 | 09:00KIX | "
            "11:00ICN | 직항, 02시간 00분 | 성인 | 왕복 | 192,300 | 원~")
    r = NV.parse_intl(intl)
    assert r and r["airline"] == "제주항공", r
    assert r["out_from"] == "ICN" and r["out_dep"] == _dt.time(7, 10)
    assert r["ret_from"] == "KIX" and r["ret_dep"] == _dt.time(9, 0)
    assert r["direct"] is True and r["price"] == 192_300

    # ── 실적 조건은 버린다 (실물 문구) ──
    cond = ("제주항공 | 07:10ICN | 09:05KIX | 직항, 01시간 55분 | 09:00KIX | "
            "11:00ICN | 직항, 02시간 00분 | 성인/하나카드(이용실적 충족시) | "
            "왕복 | 192,300 | 원~ | 로그인 후 특가확인")
    assert NV.has_spend_condition(cond)
    # v2.08: 조건부도 받되 표시를 남긴다. 끄면 예전처럼 버린다.
    NV.set_allow_card_condition(False)
    assert NV.parse_intl(cond) is None, "끈 상태인데 조건부가 통과됐다"
    NV.set_allow_card_condition(True)
    r = NV.parse_intl(cond)
    assert r and r["card_cond"] is True, "조건부 표시가 안 붙었다"
    # 카드로 결제만 하면 되는 건 조건이 아니다
    ok = cond.replace("(이용실적 충족시)", "")
    r2 = NV.parse_intl(ok)
    assert r2 and r2["card_cond"] is False, r2

    # ── 시간창·직항 필터 ──
    NV.set_allow_card_condition(False)
    rows = [
        intl,                                             # 07:10 출발 / 09:00 귀국
        intl.replace("07:10ICN", "15:20ICN").replace("192,300", "150,000"),
        cond.replace("192,300", "100,000"),               # 조건부 — 끈 상태이므로 제외
    ]
    best = NV.pick_best(rows, domestic=False,
                        out_window=(_dt.time(6, 0), _dt.time(13, 0)),
                        ret_window=(_dt.time(8, 0), _dt.time(23, 59)))
    assert best and best["price"] == 192_300, best   # 15:20편·조건부는 탈락

    # 국내선 시간창
    rows_d = [dom, dom.replace("06:00GMP", "15:00GMP").replace("51,200", "40,000")]
    bd = NV.pick_best(rows_d, domestic=True,
                      out_window=(_dt.time(6, 0), _dt.time(13, 0)))
    assert bd and bd["price"] == 51_200, bd
    NV.set_allow_card_condition(load().naver_card_condition)   # 설정값으로 복구
    print("OK 네이버 파서: 실물 파싱·카드조건 표시/제외 전환·시간창 필터")


def test_naver_leg_merge():
    """네이버 다리는 더 쌀 때만 쓰이고, 구글 값을 덮어쓰지 않는다 (v1.69)."""
    cfg = load()
    route = [r for r in cfg.routes if r.key == "GMP-CJU"][0]
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    today = dt.date(2026, 8, 1)
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    ok = engine.is_excluded_departure(cfg, dep)
    assert not ok, "테스트 날짜가 제외 요일이면 전제가 깨진다"

    st.record_leg(State.leg_key("GMP-CJU", "out", dep.isoformat()),
                  price=300_000, airline="구글편", dep_time="07:00",
                  carrier="7C", now=now)
    st.record_leg(State.leg_key("GMP-CJU", "ret", ret.isoformat()),
                  price=300_000, airline="구글편", dep_time="19:00",
                  carrier="7C", now=now)

    fresh_at = now.isoformat(timespec="seconds")

    # 네이버가 더 비싸면 무시
    st.naver_legs = {State.leg_key("GMP-CJU", "out", dep.isoformat()):
                     {"price": 400_000, "airline": "네이버편",
                      "dep_time": "07:30", "source": "naver", "at": fresh_at}}
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "구글편", c.out_leg

    # 네이버가 더 싸면 채택하고 출처를 남긴다
    st.naver_legs = {State.leg_key("GMP-CJU", "out", dep.isoformat()):
                     {"price": 200_000, "airline": "네이버편",
                      "dep_time": "07:30", "source": "naver", "at": fresh_at}}
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "네이버편" and c.out_leg["source"] == "naver"
    assert c.price == 200_000 + 300_000, c.price
    # 원본 구글 데이터는 그대로 남아 있어야 한다
    g = st.legs[State.leg_key("GMP-CJU", "out", dep.isoformat())]
    assert g["price"] == 300_000 and "source" not in g, g

    # 오래된 네이버 값은 쓰지 않는다 (v1.91).
    # 구글 leg는 만료되는데 네이버만 영원히 살아 있으면, 수집이 며칠 실패했을 때
    # 사라진 가격으로 알림이 나간다.
    old = (now - dt.timedelta(days=cfg.naver_freshness_days + 1)
           ).isoformat(timespec="seconds")
    st.naver_legs = {State.leg_key("GMP-CJU", "out", dep.isoformat()):
                     {"price": 100_000, "airline": "묵은네이버",
                      "dep_time": "07:30", "source": "naver", "at": old}}
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "구글편", "만료된 네이버 값이 쓰였다"
    # 수집 시각이 아예 없는 값도 신뢰하지 않는다
    st.naver_legs[State.leg_key("GMP-CJU", "out", dep.isoformat())].pop("at")
    c = [x for x in engine.build_combos(cfg, st, today)
         if x.dep == dep and x.nights == 3][0]
    assert c.out_leg["airline"] == "구글편", "시각 없는 네이버 값이 쓰였다"

    # 지난 날짜는 네이버 쪽도 정리된다
    st.naver_legs["GMP-CJU|out|2026-01-01"] = {"price": 1, "at": fresh_at}
    st.prune_past_legs(today)
    assert "GMP-CJU|out|2026-01-01" not in st.naver_legs, "과거 네이버 데이터가 남았다"
    print("OK 네이버 병합: 더 쌀 때만 채택 · 만료·과거 정리 · 구글 원본 보존")


def test_naver_schedule():
    """네이버 수집이 하루 여러 번 자동으로 이어받는지 (v1.74).

    '하루 1회'로 두었더니 145건을 한 번에 못 채워 뒷부분이 영영 밀렸고,
    수동 트리거를 매번 입력해야 해 실제로는 거의 안 돌았다.
    """
    from app.naver_collect import due_now
    cfg = load()
    today = dt.date(2026, 7, 26)
    n = cfg.naver_runs_per_day
    assert n >= 2, "1회면 한 바퀴를 못 채운다"

    # 시작 시각 전에는 쉰다
    assert not due_now({}, today, cfg.naver_hour - 1, cfg.naver_hour, n)
    # 오늘 첫 실행은 항상 수집
    assert due_now({}, today, cfg.naver_hour, cfg.naver_hour, n)
    # 한도 안이면 계속 이어받는다
    assert due_now({"naver_day": today.isoformat(), "naver_runs": n - 1},
                   today, 12, cfg.naver_hour, n)
    # 한도를 채우면 멈춘다
    assert not due_now({"naver_day": today.isoformat(), "naver_runs": n},
                       today, 12, cfg.naver_hour, n)
    # 날짜가 바뀌면 초기화
    assert due_now({"naver_day": "2026-07-25", "naver_runs": n},
                   today, 12, cfg.naver_hour, n)
    print(f"OK 네이버 일정: 하루 {n}회까지 자동 이어받기 · 날짜 바뀌면 초기화")


def test_naver_job_order():
    """네이버 수집은 **못 모은 것부터** 돈다 (v1.77).

    순번 커서를 쓰던 방식은 앞쪽(가는 편 53건)만 반복하고 뒤쪽(오는 편 92건)은
    예산이 모자라 영영 못 채웠다. 실제로 오는 편이 92건 중 1건이었다.
    """
    import datetime as _dt

    def order(jobs, known):
        js = list(jobs)
        js.sort(key=lambda j: known.get(
            f"{j[3]}|{j[2]}|{j[4].isoformat()}", {}).get("at", ""))
        return [f"{j[2]}:{j[4].day}" for j in js]

    d = _dt.date(2026, 9, 1)
    jobs = [("GMP", "CJU", "out", "GMP-CJU", d),
            ("GMP", "CJU", "out", "GMP-CJU", d + _dt.timedelta(1)),
            ("CJU", "GMP", "ret", "GMP-CJU", d),
            ("CJU", "GMP", "ret", "GMP-CJU", d + _dt.timedelta(1))]
    known = {
        f"GMP-CJU|out|{d.isoformat()}": {"at": "2026-07-26T10:00:00"},
        f"GMP-CJU|out|{(d + _dt.timedelta(1)).isoformat()}": {"at": "2026-07-26T09:00:00"},
    }
    got = order(jobs, known)
    # 못 모은 오는 편 둘이 먼저, 그다음 오래된 out(09시)부터
    assert got[:2] == ["ret:1", "ret:2"], got
    assert got[2] == "out:2" and got[3] == "out:1", got
    print("OK 네이버 순서: 미수집 우선 · 그다음 오래된 것부터")


def test_call_signatures():
    """main.py가 넘기는 인자를 함수가 실제로 받는지 (v1.89).

    2026-07-27 CI가 12초 만에 죽었다:
      · `collect() got an unexpected keyword argument 'delay'` — 시그니처 수정이
        한쪽에만 반영됐다
      · `UnboundLocalError: msg` — 변수 정의보다 앞에 코드를 넣었다
    둘 다 문법은 통과하므로 ast.parse로는 못 잡는다. 호출부와 정의를 대조한다.
    """
    import ast as _ast
    import inspect
    from app import naver_collect as NVC

    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text()
    tree = _ast.parse(src)

    # main.py 안의 NVC.collect(...) 호출에서 쓰는 키워드를 모은다
    used = set()
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        f = node.func
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name != "collect":
            continue
        used |= {kw.arg for kw in node.keywords if kw.arg}
    assert used, "main.py에서 collect 호출을 찾지 못했다 (테스트 전제 붕괴)"

    have = set(inspect.signature(NVC.collect).parameters)
    missing = used - have
    assert not missing, f"collect가 안 받는 인자를 넘기고 있다: {sorted(missing)}"

    # 반환값 개수도 맞아야 한다 (tuple 언패킹 실패는 런타임에야 터진다)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Call)
                and getattr(node.value.func, "attr", "") == "collect"
                and isinstance(node.targets[0], _ast.Tuple)):
            n = len(node.targets[0].elts)
            doc = inspect.getsource(NVC.collect)
            assert "-> tuple[dict, int, int, dict]" in doc and n == 4, (
                f"collect 반환값 {n}개로 받는데 정의와 다르다")
    print(f"OK 호출 시그니처: collect 인자 {len(used)}개·반환 4개 일치")


def test_baseline_unstick():
    """도달 불가능해진 기준가는 다시 올라간다 (v1.92).

    네이버가 만든 낮은 값에 기준가가 박힌 뒤 수집이 끊기면, 그 노선·월은
    영영 알림이 안 나간다. 최근 창 전체가 기준가를 못 건드렸으면 풀어준다.
    """
    cfg = load()
    n = cfg.baseline_unstick_days
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    st.meta["first_run"] = "2026-07-01"
    st.meta["baseline_metric"] = engine._METRIC   # 지표 초기화 방지
    today = dt.date(2026, 8, 20)
    route = cfg.routes[0]

    def combo(price, day=10):
        return engine.Combo(route=route, dep=dt.date(2026, 9, day), nights=3,
                            price=price,
                            out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
                            ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"})

    unit = combo(1).unit
    # 기준가는 250,000인데 최근 N일 내내 300,000대만 나왔다
    hist = {(today - dt.timedelta(days=i)).isoformat(): 300_000 + i * 100
            for i in range(1, n + 1)}
    st.baselines[unit] = {"daily_min": dict(hist), "baseline": 250_000,
                          "alltime_min": 250_000,
                          "alltime_min_at": "2026-07-01T00:00:00"}
    engine.process(cfg, st, [combo(305_000)], today)
    b = st.baselines[unit]
    assert b["baseline"] > 250_000, f"갇힌 기준가가 안 풀렸다: {b['baseline']}"
    assert "unstuck_at" in b

    # 창 안에서 한 번이라도 닿았으면 올리지 않는다
    st.baselines[unit] = {"daily_min": {**hist,
                                        today.isoformat(): 240_000},
                          "baseline": 250_000, "alltime_min": 240_000,
                          "alltime_min_at": "2026-07-01T00:00:00"}
    engine.process(cfg, st, [combo(260_000)], today)
    assert st.baselines[unit]["baseline"] <= 250_000, "닿았는데도 올렸다"
    print(f"OK 기준가 잠금 해제: 최근 {n}일 미달성 시 상향 · 닿으면 유지")


def test_mixed_source_links():
    """출처가 섞이면 **두 사이트 링크를 다 준다** (v2.02).

    가는 편과 오는 편을 다른 사이트에서 따로 사야 하는데 한쪽만 걸면
    나머지 편 가격이 그곳에 없다. 실측 조합 796개 중 52개가 혼합이다.
    """
    from app.notify import _blink, _sites
    cfg = load()
    route = [r for r in cfg.routes if r.key == "GMP-CJU"][0]

    def mk(src_out, src_ret):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, 10), nights=3, price=200_000,
            out_leg={"price": 100_000, "dep_time": "06:00", "airline": "A",
                     "carrier": "7C", **({"source": src_out} if src_out else {})},
            ret_leg={"price": 100_000, "dep_time": "21:00", "airline": "B",
                     "carrier": "LJ", **({"source": src_ret} if src_ret else {})})

    # 둘 다 네이버 → 네이버 하나 (주소가 짧다)
    c = mk("naver", "naver")
    assert "flight.naver" in _blink(cfg, c) and _sites(cfg, c) == ""
    # 둘 다 구글 → 구글 하나
    c = mk(None, None)
    assert "google.com" in _blink(cfg, c) and _sites(cfg, c) == ""
    # 섞이면 → 날짜는 글자, 줄 끝에 두 링크
    c = mk("naver", None)
    assert "<a href" not in _blink(cfg, c), _blink(cfg, c)
    s = _sites(cfg, c)
    assert "flight.naver" in s and "google.com" in s, s
    assert s.count("<a href") == 2, s
    # 알림·고정판·digest **세 곳 모두** 같은 규칙을 써야 한다
    import datetime as _dt
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    mixed = mk("naver", None)
    a = engine.Alert(kind="baseline", combo=mixed, baseline=250_000, prev_min=None)
    from app.notify import format_board, format_digest
    texts = (format_alerts(cfg, [a], [mixed])
             + format_board(cfg, [mixed], "07/27 23:59", _dt.date(2026, 8, 1))
             + format_digest(cfg, [mixed], "", _dt.date(2026, 8, 1)))
    for m in texts:
        assert "flight.naver" in m and "google.com" in m, m[:200]
    print("OK 혼합 출처: 알림·고정판·digest 모두 두 사이트 링크")


def test_unit_is_city():
    """기준가 단위는 **도시 × 월** 하나여야 한다 (v2.07).

    예전엔 오사카 9월 하나에 단위가 4개였다(인천왕복·김포왕복·교차 2종).
    각자 기준가를 가지니 **비싼 조합이 "그 단위 기준 13% 싸다"며 알림**이
    나갔고 더 싼 조합은 조용했다. 사용자는 '오사카'로 보는데 시스템은
    넷으로 쪼개 봤다.
    """
    cfg = load()
    gmp = [r for r in cfg.routes if r.key == "GMP-KIX"][0]
    icn = [r for r in cfg.routes if r.key == "ICN-KIX"][0]

    def mk(route, back=None, price=500_000):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, 10), nights=3, price=price,
            out_leg={"price": 1, "dep_time": "07:30", "airline": "X"},
            ret_leg={"price": 1, "dep_time": "19:40", "airline": "X"},
            ret_route=back,
            city=engine._seoul_group(cfg, route))

    units = {mk(icn).unit, mk(gmp).unit, mk(icn, gmp).unit, mk(gmp, icn).unit}
    assert len(units) == 1, f"오사카 9월인데 단위가 {len(units)}개: {units}"
    assert units.pop() == "KIX|2026-09"

    # 도시가 다르면 단위도 다르다
    ngo = [r for r in cfg.routes if r.key == "ICN-NGO"][0]
    assert mk(ngo).unit != mk(icn).unit

    # 새로 생긴 단위는 그날 알리지 않는다 (심기만 한다)
    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta = {}, {}, {}, {}
    st.time_hist, st.naver_legs = {}, {}
    st.meta["first_run"] = "2026-07-01"
    st.meta["baseline_metric"] = engine._METRIC
    today = dt.date(2026, 8, 20)
    assert engine.process(cfg, st, [mk(icn, price=100_000)], today) == [], "새 단위가 알렸다"
    assert st.baselines[mk(icn).unit].get("_seeded") == today.isoformat()
    print("OK 기준가 단위: 도시×월 하나 · 새 단위는 첫날 알리지 않음")


def test_verify_targets_catch_skew():
    """왕복 검증 대상에 **배율이 큰 조합**이 들어가야 한다 (v2.09).

    편도 합산 순위만 보면, 오는 편 편도가 폭등한 조합은 뒤로 밀려 후보에도
    못 오른다. 실측: 나고야 8/8~8/12는 편도합산 1인 52만이라 60개 중 한참
    뒤였지만 구글 왕복으로는 18만이었다.
    """
    cfg = load()
    route = cfg.routes[0]

    def mk(day, out_p, ret_p):
        return engine.Combo(
            route=route, dep=dt.date(2026, 9, day), nights=3,
            price=out_p + ret_p,
            out_leg={"price": out_p, "dep_time": "07:30", "airline": "X"},
            ret_leg={"price": ret_p, "dep_time": "19:40", "airline": "X"},
            city=engine._seoul_group(cfg, route))

    cheap = [mk(d, 100_000, 120_000) for d in range(1, 6)]      # 합산 220,000
    skew = mk(10, 100_000, 900_000)                              # 합산 1,000,000
    targets = engine.verify_targets(cfg, cheap + [skew])
    assert skew in targets, "배율 큰 조합이 검증 대상에서 빠졌다"
    assert any(c in targets for c in cheap), "싼 조합도 함께 봐야 한다"

    # 왕복 실가가 붙으면 '실제 낼 금액'이 그걸 따른다
    assert skew.pay == 1_000_000
    skew.rt_price = 300_000
    assert skew.pay == 300_000, "왕복이 싼데 편도 합산을 쓰고 있다"
    print("OK 왕복 검증 대상: 싼 것 + 배율 큰 것 · pay는 싼 쪽을 따른다")


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
    """노선별 값은 '넓힌 창'이고, 선호 창은 전역값 그대로여야 한다 (v1.34).

    넓힌 창은 검색 범위로 쓰고, 선호 창은 '이 편이 선호 시간 밖인가'를
    판정해 알림에 표시하는 기준으로 쓴다.
    """
    cfg = load()
    # 선호 창은 노선과 무관하게 항상 전역값이어야 한다
    for r in cfg.routes:
        assert cfg.window_for(r.key, "out") == cfg.outbound_window, r.key
        assert cfg.window_for(r.key, "ret") == cfg.return_window, r.key

    relaxed = [r.key for r in cfg.routes if cfg.has_window_override(r.key)]
    assert relaxed, "넓힌 창이 설정된 노선이 없다 (테스트 전제 붕괴)"
    for key in relaxed:
        for d, glob in (("out", cfg.outbound_window), ("ret", cfg.return_window)):
            fb = cfg.fallback_window_for(key, d)
            if fb is None:
                continue
            # 양보 창은 선호 창을 포함하면서 더 넓어야 의미가 있다
            assert fb[0] <= glob[0] and fb[1] >= glob[1], (key, d, fb, glob)
            assert fb != glob, f"{key} {d}: 넓힌 창이 전역값과 같아 무의미"

    koj = cfg.fallback_window_for("ICN-KOJ", "out")
    cts = cfg.fallback_window_for("ICN-CTS", "ret")
    assert koj and koj[1] > cfg.outbound_window[1], "가고시마 넓힌 창 미설정"
    assert cts and cts[0] < cfg.return_window[0], "삿포로 넓힌 창 미설정"
    print(f"OK 넓힌 창 설정: ICN-KOJ 가는편 ~{koj[1]:%H:%M}, "
          f"ICN-CTS 오는편 {cts[0]:%H:%M}~ (선호 창은 전역 유지)")


def test_fallback_window_is_last_resort():
    """양보 시간창은 선호 시간대가 비었을 때만 쓰인다 (v1.33)."""
    import datetime as _dt
    from app import search as S

    class _T:
        def __init__(self, h):
            self.time = [h, 0]

    class _Seg:
        def __init__(self, h):
            self.departure, self.arrival = _T(h), _T(h + 2)
            self.from_airport = type("A", (), {"code": "ICN", "name": "I"})()
            self.to_airport = type("A", (), {"code": "KOJ", "name": "K"})()

    class _It:
        def __init__(self, h, price):
            self.flights, self.price, self.airlines = [_Seg(h)], price, ["KE"]
            self.type = "KE"

    pref = (_dt.time(6, 0), _dt.time(13, 0))
    fb = (_dt.time(6, 0), _dt.time(17, 0))
    orig = S._do_fetch
    try:
        # 오전 편이 있으면 더 싼 오후 편이 있어도 오전을 고른다
        S._do_fetch = lambda *a, **k: [_It(9, 500_000), _It(16, 400_000)]
        r = S.search_leg("ICN", "KOJ", "2026-08-01", adults=2, window=pref,
                         fallback_window=fb, retries=1)
        assert r and r.price == 500_000 and not r.off_window, r

        # 오전 편이 없을 때만 양보하고, 양보했음을 표시한다
        S._do_fetch = lambda *a, **k: [_It(16, 400_000)]
        r = S.search_leg("ICN", "KOJ", "2026-08-01", adults=2, window=pref,
                         fallback_window=fb, retries=1)
        assert r and r.price == 400_000 and r.off_window, r

        # 양보 창이 없는 노선은 기존과 동일하게 '없음' 처리
        r = S.search_leg("ICN", "KOJ", "2026-08-01", adults=2, window=pref,
                         fallback_window=None, retries=1)
        assert r is None, r
    finally:
        S._do_fetch = orig
    print("OK 양보 동작: 선호 우선 · 비었을 때만 양보 · 양보 시 표시")


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
    assert "왕복권" not in msg, msg

    # 왕복이 더 비싼 경우 → 편도 2장이 대표 금액, 왕복은 비교용 한 줄
    a.rt_price = 1_200_000
    msg = format_alerts(cfg, [a])[0]
    assert "400,000원</b>/인" in msg, msg              # 더 싼 쪽(편도 2장)
    assert "편도 2장이 유리" in msg, msg

    # 왕복이 더 싼 경우 → 왕복이 대표 금액 (노선마다 갈리므로 양방향 필요)
    a.rt_price = 600_000
    msg = format_alerts(cfg, [a])[0]
    assert "300,000원</b>/인" in msg, msg
    assert "왕복권이 유리" in msg, msg
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
