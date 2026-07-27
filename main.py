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

# HTTP 클라이언트 소음 차단 (v1.37).
# primp/httpx 등이 요청마다 수백 글자짜리 구글 tfs URL을 통째로 찍는다.
# 432 leg 실행이면 이런 줄만 600개 넘어 로그가 수 MB가 되고, 정작 필요한
# 통계·진단 줄이 묻힌다. 로그를 통째로 복사하기도 어려워진다.
# 오류는 WARNING 이상이라 그대로 보인다. 되살리려면 아래 줄을 지울 것.
for _noisy in ("primp", "httpx", "httpcore", "urllib3", "requests",
               "urllib3.connectionpool"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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
    #   "digest" → 도시별 지금 최저가 한 통만 전송 (저장 없음). 조용한 날 확인용
    _raw = (os.environ.get("DRY_RUN") or "").strip().lower()
    _parts = _raw.split()
    _mode = _parts[0] if _parts else ""
    _arg = _parts[1] if len(_parts) > 1 else ""
    dry = _mode == "1"
    preview = _mode == "preview"
    digest = _mode == "digest"
    naver_probe = _mode == "naver"
    # 하이픈·공백·대소문자 어떻게 넣어도 받는다 (입력 실수로 조용히 평범한
    # 실행이 돼버리는 일이 있었다)
    naver_run = _mode.replace("_", "-") in ("naver-run", "naverrun", "naverrun")        or _raw.replace(" ", "-").replace("_", "-") == "naver-run"
    # 깃허브 입력에서도 월을 받는다. 텔레그램 /digest 8 과 같은 동작.
    #   "digest 8" → 8월만 자세히 · "8" 또는 "8월" → 8월만 가볍게 한 통
    manual_month = notify.parse_month(_mode, _arg)
    brief = bool(manual_month) and not digest

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
    # ---- 텔레그램 명령 확인 (지난 실행 이후 밀린 것) ----
    wants_digest = False       # /digest → 자세히(여러 통, 날짜마다 링크)
    wants_brief = False        # /8월 → 가볍게(한 통, 요약)
    digest_month = None
    if not dry:
        try:
            cmds, new_offset = notify.poll_commands(
                int(state.meta.get("tg_offset", 0)))
            state.meta["tg_offset"] = new_offset
            for _chat, cmd, arg in cmds:
                m = notify.parse_month(cmd, arg)
                if cmd in ("digest", "시세", "now", "board"):
                    wants_digest = True
                    digest_month = m or digest_month
                elif m:
                    # 월만 던진 건 훑어보려는 것 → 가벼운 한 통으로
                    wants_brief = True
                    digest_month = m
                elif cmd in ("help", "start", "도움말"):
                    notify.send(
                        "🤖 <b>쓸 수 있는 명령</b>\n"
                        "/8 또는 /8월 — 그 달 출발만 가볍게 한 통\n"
                        "/digest — 도시별로 자세히 (날짜마다 링크, 여러 통)\n"
                        "/digest 8 — 8월만 자세히\n"
                        "/help — 이 안내\n\n"
                        "명령은 <b>다음 실행 때</b> 처리됩니다 (최대 1시간). "
                        "바로 보고 싶으면 고정해둔 📌 메시지를 확인하세요 — "
                        "실행마다 조용히 갱신됩니다.")
            if cmds:
                log.info("텔레그램 명령 %d건 수신: %s",
                         len(cmds), [c for _, c in cmds])
        except Exception as e:  # noqa: BLE001 - 명령 처리 실패가 본 작업을 막지 않는다
            log.info("명령 확인 실패: %s", str(e)[:150])

    from app.search import set_excluded_airlines
    set_excluded_airlines(cfg.exclude_airlines)
    if cfg.exclude_airlines:
        log.info("제외 항공사: %s", ", ".join(cfg.exclude_airlines))

    # 보기 전용 모드(digest / 월 요약)는 **검색을 건너뛴다** (v1.56).
    # 이미 저장된 데이터를 다르게 그려줄 뿐이라 새로 뒤질 이유가 없다.
    # 예전엔 663건을 8분간 검색하고 그 결과를 저장도 안 한 채 버렸다.
    if brief or digest or naver_probe or naver_run:
        legs = []
        log.info("보기 전용 모드 → 검색 건너뜀 (저장된 데이터로 즉시 응답)")

    done_n, total_n, cycle_done = engine.note_shard(cfg, state, shard)
    # 어떤 모드로 도는지 로그 맨 앞에 남긴다. 입력이 먹혔는지 추측하지 않도록.
    log.info("실행 모드: 입력=%r → %s", _raw,
             "테스트(1)" if dry else "미리보기" if preview else
             "다이제스트" if digest else "네이버탐침" if naver_probe else
             "네이버수집" if naver_run else
             (f"{manual_month}월 요약" if brief else "실전"))

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
        # 로그로만 두면 옮겨 보기가 번거로워 파일로도 누적한다 (v1.36).
        # data/time_hist.json 을 열면 언제든 확인 가능. 지우면 초기화.
        state.merge_time_hist(hist)
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

    # ---- 네이버 보조 수집 (하루 1회, 예산 안에서) ----
    # 국내선에서만 이득이 확인돼 제주만 대상. 브라우저를 띄워야 해 무겁다.
    # 실패해도 구글 파이프라인은 그대로 간다.
    if cfg.naver_routes and not dry and not (brief or digest or naver_probe):
        from app import naver_collect as NVC
        kst_hour = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).hour
        if naver_run or NVC.due_now(state.meta, today, kst_hour, cfg.naver_hour,
                                    cfg.naver_runs_per_day):
            try:
                pairs, dates_by, windows = [], {}, {}
                for rk in cfg.naver_routes:
                    route = next((r for r in cfg.routes if r.key == rk), None)
                    if route is None:
                        continue
                    o, d = route.origin, route.destination
                    if "out" in cfg.naver_directions:
                        pairs.append((o, d, "out", rk))
                    if "ret" in cfg.naver_directions:
                        pairs.append((d, o, "ret", rk))
                    windows[(rk, "out")] = cfg.window_for(rk, "out")
                    windows[(rk, "ret")] = cfg.window_for(rk, "ret")
                    outs, rets = [], []
                    day = max(cfg.period_start, today)
                    while day <= cfg.period_end:
                        if not engine.is_excluded_departure(cfg, day):
                            outs.append(day)
                        rets.append(day)
                        day += dt.timedelta(days=1)
                    dates_by[(o, d, "out")] = outs
                    dates_by[(d, o, "ret")] = rets
                got, remain, total, dstat = NVC.collect(
                    pairs, dates_by, cfg.adults, windows,
                    budget_sec=cfg.naver_budget_min * 60,
                    known=state.naver_legs,
                    delay=cfg.naver_delay_sec,
                    reset_every=cfg.naver_reset_every,
                    stop_after_fail=cfg.naver_stop_after_fail)
                if got:
                    state.naver_legs.update(got)
                state.meta["naver_remain"] = remain
                state.meta["naver_dstat"] = {k: list(v) for k, v in dstat.items()}
                state.meta["naver_total"] = total
                if state.meta.get("naver_day") != today.isoformat():
                    state.meta["naver_day"] = today.isoformat()
                    state.meta["naver_runs"] = 0
                state.meta["naver_runs"] = int(state.meta.get("naver_runs", 0)) + 1
                state.meta["naver_last_run"] = today.isoformat()
                log.info("네이버 반영 %d건 (누적 %d/%d) · 미수집 %d건 · 오늘 %d/%d회",
                         len(got), len(state.naver_legs), total, remain,
                         state.meta["naver_runs"], cfg.naver_runs_per_day)
            except Exception as e:  # noqa: BLE001 - 보조 소스 실패가 본 작업을 막지 않는다
                log.info("네이버 수집 오류: %s", str(e)[:200])

    if naver_run:
        # 수동 수집은 아무 메시지도 안 보내 "됐는지 안 됐는지" 알 수 없었다.
        # 무엇을 얼마나 모았고 구글 대비 어떤지 요약해 보낸다 (v1.73).
        import collections as _c
        nvl = state.naver_legs
        by_dir = _c.Counter(k.split("|")[1] for k in nvl)
        win = lose = 0
        gaps = []
        for k, v in nvl.items():
            g = state.legs.get(k)
            if not (g and g.get("price") and v.get("price")):
                continue
            if v["price"] < g["price"]:
                win += 1
                gaps.append(g["price"] - v["price"])
            else:
                lose += 1
        rem = int(state.meta.get("naver_remain", 0))
        tot = int(state.meta.get("naver_total", 0)) or 145
        msg = ["🧪 <b>네이버 수집 완료</b>",
               f"가는 편 {by_dir.get('out', 0)}건 · 오는 편 {by_dir.get('ret', 0)}건 "
               f"(누적 {len(nvl)}건)"]
        if rem:
            msg.append(f"{tot}건 중 {rem}건 미수집 — 다음 실행이 이어받아요")
        else:
            msg.append(f"전체 {tot}건 수집 완료")
        # 방향별 성적을 메시지에 싣는다. 로그에만 두면 어느 쪽이 왜 실패하는지
        # 확인할 방법이 없다 (오는 편이 91건 내내 0이던 것을 못 잡았다, v1.78).
        ds = state.meta.get("naver_dstat") or {}
        for d_, v in sorted(ds.items()):
            q, rws, ok_ = (list(v) + [0, 0, 0])[:3]
            if d_.startswith("세션"):
                msg.append(f"{d_}: {q}건 중 {ok_}건 성공")
            elif "·" in d_:        # URL 형식별 성적 (어느 형식이 통하는지)
                msg.append(f"[{d_.replace('out', '가는편').replace('ret', '오는편')}] "
                           f"시도 {q} · 읽은 행 평균 {rws / max(q, 1):.0f}")
            else:
                msg.append(f"{'가는 편' if d_ == 'out' else '오는 편'}: 시도 {q} · "
                           f"읽은 행 평균 {rws / max(q, 1):.0f} · 조건 통과 {ok_}")
        if win + lose:
            avg = round(sum(gaps) / len(gaps) / max(cfg.adults, 1)) if gaps else 0
            msg.append(f"구글 대비 승 {win} · 패 {lose}"
                       + (f" · 이겼을 때 평균 {avg:,}원/인 절감" if avg else ""))
        notify.send("\n".join(msg))
        state.save()
        log.info("네이버 수동 수집 종료 · 저장 완료")
        return 0

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
        # 교차 조합(김포 출발/인천 귀국 등)은 왕복 상품 자체가 없다.
        # 가는 편 노선으로 왕복을 조회하면 실제 여정과 다른 가격이 붙는다 (v1.42).
        targets = [a for a in engine.display_selection(cfg, alerts_)
                   if not a.combo.is_cross][: cfg.verify_max_queries]
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

    # ---- 네이버 브라우저 탐침 (DRY_RUN=naver) ----
    # 구글 가격이 이미 있는 날짜쌍으로 시험해야 "뚫리나"와 "다른 게 있나"를
    # 한 번에 알 수 있다. 결과는 data/naver_probe.json 에 남긴다.
    if naver_probe:
        from pathlib import Path as _P
        from app import naver_browser_probe as nvb
        best: dict = {}
        for c in combos:
            k = c.route.key
            if k not in best or c.price < best[k].price:
                best[k] = c
        # 국제선을 한 노선(오사카)만 보고 판단하면 성급하다.
        # 국내선 1 + 서로 다른 국제선 도시 4곳으로 교차검증한다 (v1.68).
        picked, seen_city = [], set()
        dom = [c for c in best.values() if getattr(c.route, "domestic", False)]
        if dom:
            picked.append(min(dom, key=lambda x: x.price))
        for c in sorted(best.values(), key=lambda x: x.price):
            if getattr(c.route, "domestic", False):
                continue
            city = engine._seoul_group(cfg, c.route)
            if city in seen_city:
                continue
            seen_city.add(city)
            picked.append(c)
            if len(picked) >= 5:
                break
        # 오는 편(CJU→GMP)이 왜 0행인지 **페이지를 직접 본 적이 없다.**
        # 숫자만 보고 다섯 번을 고쳤다. 실패하는 방향을 탐침에 명시적으로 넣는다.
        # 오는 편이 8월만 되고 9월부터 전멸한다. 원인 불명 → **추측하지 말고
        # 성공 날짜와 실패 날짜를 나란히 열어 차이를 본다** (v1.85).
        cases = []
        dom_route = next((r for r in cfg.routes
                          if getattr(r, "domestic", False)), None)
        if dom_route:
            o, d = dom_route.origin, dom_route.destination
            for day, tag in ((dt.date(2026, 8, 12), "8월(성공하던 날짜)"),
                             (dt.date(2026, 9, 16), "9월(실패하는 날짜)"),
                             (dt.date(2026, 10, 14), "10월(실패하는 날짜)")):
                cases.append({
                    "origin": d, "dest": o,          # 오는 편 방향 (CJU→GMP)
                    "dep": day.strftime("%Y%m%d"),
                    "ret": (day + dt.timedelta(days=3)).strftime("%Y%m%d"),
                    "domestic": True, "google_price": 0,
                    "label": f"{d}→{o} {tag}",
                })
        for c in []:

            cases.append({
                "origin": c.route.origin, "dest": c.route.destination,
                "dep": c.dep.strftime("%Y%m%d"), "ret": c.ret.strftime("%Y%m%d"),
                "domestic": bool(getattr(c.route, "domestic", False)),
                "google_price": c.price, "label": f"{c.route.label} {c.dep}",
            })
        log.info("네이버 탐침 %d건 시작", len(cases))
        res = nvb.run(cases, cfg.adults, _P("data/naver_probe.json"))
        # 수집한 행을 파서에 태워 **구글과 같은 조건으로** 비교한다.
        from app import naver as NV
        lines = ["🧪 <b>네이버 검증</b>", res.get("note", "")]
        for r, c in zip(res.get("cases", []), cases):
            rows = (r.get("rows") or {}).get("texts") or []
            dom = c["domestic"]
            route_key = f"{c['origin']}-{c['dest']}"
            best = NV.pick_best(
                rows, domestic=dom,
                out_window=cfg.window_for(route_key, "out"),
                ret_window=None if dom else cfg.window_for(route_key, "ret"),
                direct_only=cfg.direct_only)
            g_per = round(c["google_price"] / max(cfg.adults, 1))
            n_raw = len(rows)
            drop = sum(1 for x in rows if NV.has_spend_condition(x))
            lines.append("")
            lines.append(f"<b>{r['label']}</b> · 행 {n_raw}개(실적조건 {drop}개 제외)")
            if not best:
                lines.append(f"조건 맞는 값 없음 · 구글 {g_per:,}원/인")
                continue
            if dom:
                nv_per = best["price"] * 2      # 편도 → 왕복 환산
                lines.append(f"네이버 {nv_per:,}원/인 (편도 {best['price']:,}×2, "
                             f"{best['seat']}) / 구글 {g_per:,}원/인")
            else:
                nv_per = best["price"]
                lines.append(f"네이버 {nv_per:,}원/인 / 구글 {g_per:,}원/인")
            gap = (nv_per - g_per) / max(g_per, 1) * 100
            lines.append(f"→ 네이버가 {abs(gap):.0f}% {'싸다' if gap < 0 else '비싸다'}"
                         f" · {best['airline']}")
        notify.send("\n".join(lines))
        log.info("네이버 탐침 종료 · data/naver_probe.json 기록")
        return 0

    # ---- 깃허브 입력이 월만 준 경우: 가벼운 한 통 ----
    if brief:
        stamp = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(hours=9)).strftime("%m/%d %H:%M")
        notify.send(notify.format_board(cfg, combos, stamp, today, manual_month))
        log.info("월 요약 전송 (%s월) · 상태 저장하지 않음", manual_month)
        return 0

    # ---- 다이제스트: 수동 모드이거나 텔레그램 /digest 요청이 있을 때 ----
    if wants_brief and not digest:
        stamp = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(hours=9)).strftime("%m/%d %H:%M")
        notify.send(notify.format_board(cfg, combos, stamp, today, digest_month))
        log.info("텔레그램 월 요약 처리 완료 (%s월)", digest_month)

    if wants_digest and not digest:
        sub = ("요청하신 현재 시세입니다" if not digest_month
               else f"{digest_month}월 출발만 추렸습니다")
        for _m in notify.format_digest(cfg, combos, sub, today, digest_month):
            notify.send(_m)
        log.info("텔레그램 요청 처리 완료 (월=%s)", digest_month or "전체")

    if digest:
        sub = (f"{manual_month}월 출발만 추렸습니다" if manual_month
               else "요청하신 현재 시세입니다")
        for _m in notify.format_digest(cfg, combos, sub, today, manual_month):
            notify.send(_m)
        log.info("다이제스트 전송 완료 (월=%s) · 상태 저장하지 않음",
                 manual_month or "전체")
        return 0

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
        for c in [x for x in sorted(cheapest_per_route.values(),
                                    key=lambda x: x.route.key) if not x.is_cross]:
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

    # ---- 고정판 갱신 (알림 없이 조용히) ----
    if cfg.live_board and not dry:
        stamp = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(hours=9)).strftime("%m/%d %H:%M")
        try:
            state.meta["board_ids"] = notify.upsert_board(
                notify.format_board(cfg, combos, stamp, today), state.board_ids())
            log.info("고정판 갱신 완료")
        except Exception as e:  # noqa: BLE001 - 고정판 실패가 알림을 막지 않는다
            log.info("고정판 갱신 실패: %s", str(e)[:150])

    # 한 바퀴(전 조합 1회 훑기) 완료 보고
    if cycle_done:
        sub = engine.cycle_report(cfg, state, today, len(engine.all_legs(cfg, today)))
        if sub:
            for _m in notify.format_digest(cfg, combos, sub, today):
                deliver(_m)
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
