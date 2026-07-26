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

    # daily 정책: 하루 한 번만 보고. (v1.45부터 본문은 notify.format_digest가
    # 만들고, cycle_report는 '몇 편 확인했는지' 부제만 돌려준다)
    first = engine.cycle_report(cfg, st, today, 1017)
    assert first and "1,017" in first, first
    assert engine.cycle_report(cfg, st, today, 1017) is None, "하루 두 번 보고됨"
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
    one_liners = [l for l in msg.split("\n") if l.startswith("· <a href=")]
    assert len(one_liners) == len(near), msg
    body = "\n".join(one_liners)
    assert "9/12" in body, body                         # 날짜·요일
    assert "07:30/19:40" in body, body                  # 출발/귀국 시각
    assert "제주항공" in body, body                       # 항공사
    assert "410,000원/인" in body, body                  # 820,000 / 2명
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
    assert "16:20⚠ 출발" in msg, msg
    assert "18:55 귀국" in msg, msg            # 조건 맞는 쪽엔 표시 없음
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
    gmp = [r for r in cfg.routes if r.key == "GMP-NGO"][0]
    icn = [r for r in cfg.routes if r.key == "ICN-NGO"][0]

    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    today = dt.date(2026, 8, 1)
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    # 김포 출발은 싸고, 김포로 돌아오는 편은 없음 → 인천 귀국만 가능
    st.record_leg(State.leg_key("GMP-NGO", "out", dep.isoformat()),
                  price=200_000, airline="Peach", dep_time="11:20", carrier="MM", now=now)
    st.record_leg(State.leg_key("ICN-NGO", "out", dep.isoformat()),
                  price=400_000, airline="Jin Air", dep_time="07:30", carrier="LJ", now=now)
    st.record_leg(State.leg_key("ICN-NGO", "ret", ret.isoformat()),
                  price=300_000, airline="Jeju Air", dep_time="19:00", carrier="7C", now=now)

    combos = engine.build_combos(cfg, st, today)
    pairs = {(c.route.key, c.back.key): c for c in combos
             if c.dep == dep and c.nights == 3}
    assert ("GMP-NGO", "ICN-NGO") in pairs, "교차 조합이 안 만들어졌다"
    assert ("ICN-NGO", "ICN-NGO") in pairs, "같은 공항 조합도 있어야 한다"

    cross = pairs[("GMP-NGO", "ICN-NGO")]
    same = pairs[("ICN-NGO", "ICN-NGO")]
    assert cross.is_cross and not same.is_cross
    assert cross.price == 500_000 < same.price == 700_000, (cross.price, same.price)
    assert cross.key != same.key and cross.unit != same.unit, "키가 겹치면 안 된다"

    # 메시지에 공항이 반드시 드러나야 한다 (엉뚱한 공항으로 가면 비행기를 놓친다)
    a = engine.Alert(kind="baseline", combo=cross, baseline=600_000, prev_min=None)
    msg = format_alerts(cfg, [a], combos)[0]
    assert "김포 출발" in msg and "인천 귀국" in msg, msg
    assert "나고야" in msg.splitlines()[0], msg.splitlines()[0]  # 제목은 도시명
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
    assert f"{300_000:,}원부터" in head, head      # 600,000 / 2명
    assert min(shown) == 300_000, (head, shown)

    # 같은 날 같은 값이면 박 수가 긴 쪽만 (3박·4박 중복 제거)
    dup3, dup4 = mk(16, 620_000, 3), mk(16, 620_000, 4)
    msg2 = format_alerts(cfg, [a], [alert_combo, dup3, dup4])[0]
    assert msg2.count("9/16") == 1, msg2
    assert "4박" in [l for l in msg2.splitlines() if "9/16" in l][0], msg2
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
    assert msg.count("원</b>/인부터") == 2, msg
    i2 = msg.index(engine.city_label(cfg, r2))
    i1 = msg.index(engine.city_label(cfg, r1))
    assert i2 < i1, "더 싼 도시가 먼저 와야 한다"
    assert "250,000원</b>/인부터" in msg, msg      # 500,000 / 2명
    assert "350,000원</b>/인부터" in msg, msg      # 700,000 / 2명
    assert "450,000원" in msg, "도시 안에서는 여러 날짜를 보여준다"
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
        assert joined.count(f"{engine.city_label(cfg, r)} ") >= 1

    # 콤보가 없어도 죽지 않는다
    empty = format_digest(cfg, [], "", dt.date(2026, 8, 1))
    assert len(empty) == 1 and "아직 비교할 조합이 없습니다" in empty[0]
    print(f"OK 다이제스트: 도시 제목 + 날짜 {cfg.digest_top_n}개·싼 순·링크·빈 데이터 방어")


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

    # daily 정책: 하루 한 번만 보고. (v1.45부터 본문은 notify.format_digest가
    # 만들고, cycle_report는 '몇 편 확인했는지' 부제만 돌려준다)
    first = engine.cycle_report(cfg, st, today, 1017)
    assert first and "1,017" in first, first
    assert engine.cycle_report(cfg, st, today, 1017) is None, "하루 두 번 보고됨"
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
    one_liners = [l for l in msg.split("\n") if l.startswith("· <a href=")]
    assert len(one_liners) == len(near), msg
    body = "\n".join(one_liners)
    assert "9/12" in body, body                         # 날짜·요일
    assert "07:30/19:40" in body, body                  # 출발/귀국 시각
    assert "제주항공" in body, body                       # 항공사
    assert "410,000원/인" in body, body                  # 820,000 / 2명
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
    assert "16:20⚠ 출발" in msg, msg
    assert "18:55 귀국" in msg, msg            # 조건 맞는 쪽엔 표시 없음
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
    gmp = [r for r in cfg.routes if r.key == "GMP-NGO"][0]
    icn = [r for r in cfg.routes if r.key == "ICN-NGO"][0]

    st = State.__new__(State)
    st.legs, st.baselines, st.alerts_sent, st.meta, st.time_hist = {}, {}, {}, {}, {}
    today = dt.date(2026, 8, 1)
    dep, ret = dt.date(2026, 9, 10), dt.date(2026, 9, 13)
    now = dt.datetime.now(dt.timezone.utc)
    # 김포 출발은 싸고, 김포로 돌아오는 편은 없음 → 인천 귀국만 가능
    st.record_leg(State.leg_key("GMP-NGO", "out", dep.isoformat()),
                  price=200_000, airline="Peach", dep_time="11:20", carrier="MM", now=now)
    st.record_leg(State.leg_key("ICN-NGO", "out", dep.isoformat()),
                  price=400_000, airline="Jin Air", dep_time="07:30", carrier="LJ", now=now)
    st.record_leg(State.leg_key("ICN-NGO", "ret", ret.isoformat()),
                  price=300_000, airline="Jeju Air", dep_time="19:00", carrier="7C", now=now)

    combos = engine.build_combos(cfg, st, today)
    pairs = {(c.route.key, c.back.key): c for c in combos
             if c.dep == dep and c.nights == 3}
    assert ("GMP-NGO", "ICN-NGO") in pairs, "교차 조합이 안 만들어졌다"
    assert ("ICN-NGO", "ICN-NGO") in pairs, "같은 공항 조합도 있어야 한다"

    cross = pairs[("GMP-NGO", "ICN-NGO")]
    same = pairs[("ICN-NGO", "ICN-NGO")]
    assert cross.is_cross and not same.is_cross
    assert cross.price == 500_000 < same.price == 700_000, (cross.price, same.price)
    assert cross.key != same.key and cross.unit != same.unit, "키가 겹치면 안 된다"

    # 메시지에 공항이 반드시 드러나야 한다 (엉뚱한 공항으로 가면 비행기를 놓친다)
    a = engine.Alert(kind="baseline", combo=cross, baseline=600_000, prev_min=None)
    msg = format_alerts(cfg, [a], combos)[0]
    assert "김포 출발" in msg and "인천 귀국" in msg, msg
    assert "나고야" in msg.splitlines()[0], msg.splitlines()[0]  # 제목은 도시명
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
    assert f"{300_000:,}원부터" in head, head      # 600,000 / 2명
    assert min(shown) == 300_000, (head, shown)

    # 같은 날 같은 값이면 박 수가 긴 쪽만 (3박·4박 중복 제거)
    dup3, dup4 = mk(16, 620_000, 3), mk(16, 620_000, 4)
    msg2 = format_alerts(cfg, [a], [alert_combo, dup3, dup4])[0]
    assert msg2.count("9/16") == 1, msg2
    assert "4박" in [l for l in msg2.splitlines() if "9/16" in l][0], msg2
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
    assert msg.count("원</b>/인부터") == 2, msg
    i2 = msg.index(engine.city_label(cfg, r2))
    i1 = msg.index(engine.city_label(cfg, r1))
    assert i2 < i1, "더 싼 도시가 먼저 와야 한다"
    assert "250,000원</b>/인부터" in msg, msg      # 500,000 / 2명
    assert "350,000원</b>/인부터" in msg, msg      # 700,000 / 2명
    assert "450,000원" in msg, "도시 안에서는 여러 날짜를 보여준다"
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
        assert joined.count(f"{engine.city_label(cfg, r)} ") >= 1

    # 콤보가 없어도 죽지 않는다
    empty = format_digest(cfg, [], "", dt.date(2026, 8, 1))
    assert len(empty) == 1 and "아직 비교할 조합이 없습니다" in empty[0]
    print(f"OK 다이제스트: 도시 제목 + 날짜 {cfg.digest_top_n}개·싼 순·링크·빈 데이터 방어")


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
