"""네이버 국내선(제주) 수집기.

**왜 제주만인가** — 2026-07-26 교차검증 결과:
    김포-제주    네이버 92,400원/인 vs 구글 130,200원  → -29%
    인천-오사카  네이버 245,800    vs 구글 223,200    → +10% (오히려 비쌈)
    인천-후쿠오카 네이버 249,900    vs 구글 255,877    → -2%
국제선은 구글과 같은 재고를 본다. 이득이 있는 건 한국 국내선 LCC의 특가
운임 등급(특가석 편도 46,200원 등)뿐이고, 그건 구글이 잘 못 보여준다.

**왜 브라우저인가** — HTTP(requests·primp)로는 API가 503. IP 차단은 아니다.
headless 브라우저도 검색이 안 걸린다. **실제 화면(Xvfb) + 스텔스**여야 페이지가
스스로 검색 API를 호출한다. 자세한 경위는 STATUS.md 참조.

무겁기 때문에 하루 1회만, 시간 예산 상한을 두고 돌린다.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from . import naver as NV

log = logging.getLogger(__name__)

# URL 형식을 하나로 못 박지 않는다 (v1.80).
#   실측: 편도 URL이 가는 편은 68행, 오는 편은 2행. 왜 방향에 따라 갈리는지
#   아직 모른다. "기본 검색이 김포→제주라 우연히 맞았다"는 설명은 데이터와
#   맞지 않는다 — 그렇다면 오는 편도 68행이 나왔어야 한다.
#   왕복 형식은 탐침에서 101행이 나왔지만 그건 **가는 편을 앞 구간에 둔 경우**뿐,
#   오는 편을 앞에 둔 형식은 시험한 적이 없다.
# → 추론으로 하나를 고르지 말고 **둘 다 시도하고 어느 쪽이 통했는지 기록**한다.
# 탐침이 성공했던 URL에는 `&isDirect=true&fareType=Y`가 붙어 있었는데
# 수집기는 그걸 빠뜨리고 있었다. 가는 편은 그래도 됐지만 오는 편은 다를 수
# 있어 동일하게 맞춘다 (v1.82).
_Q = "adult={n}&isDirect=true&fareType=Y"
_RT = ("https://flight.naver.com/flights/domestic/"
       "{o}-{d}-{ymd}/{d}-{o}-{ymd2}?" + _Q)
# 편도형은 실측에서 **항상 0행**이었다(out 1회·ret 23회 모두). 빈손일 때마다
# 그쪽으로 재시도하느라 페이지 로딩만 버렸다. 대신 **같은 왕복형을 한 번 더,
# 더 오래 기다려서** 시도한다 — 왕복형은 31회 중 8회만 성공해 실패가 대체로
# 로딩 타이밍 문제로 보인다 (v1.84).
_FORMS = (("왕복형", _RT), ("왕복형(재시도)", _RT))
_MIN_ROWS = 8      # 이보다 적게 읽히면 실패로 보고 다른 형식을 시도


def _urls(o: str, d: str, day, adults: int):
    import datetime as _dt
    back = day + _dt.timedelta(days=3)
    for name, tpl in _FORMS:
        yield name, tpl.format(o=o, d=d, ymd=day.strftime("%Y%m%d"),
                               ymd2=back.strftime("%Y%m%d"), n=adults)


def collect(route_pairs: list, dates_by_pair: dict, adults: int,
            windows: dict, budget_sec: int = 1500,
            known: dict | None = None, probe_mod=None,
            delay: tuple = (8, 16), reset_every: int = 12,
            stop_after_fail: int = 6) -> tuple[dict, int, int, dict]:
    """편도 단위로 훑어 {leg_key: {...}} 를 돌려준다.

    route_pairs: [("GMP","CJU","out","GMP-CJU"), ("CJU","GMP","ret","GMP-CJU"), ...]
    dates_by_pair: {(o,d,direction): [date, ...]}
    windows: {(route_key, direction): (lo, hi)}
    budget_sec: 이 시간을 넘기면 거기서 멈춘다.
    known:      이미 모아둔 것 {leg_key: {... "at": iso}}.
                **아직 못 모은 것 → 오래된 것 순으로 돈다** (v1.77).
                순번 커서를 쓰던 방식은 가는 편(앞쪽 53건)만 반복하고
                오는 편(뒤쪽 92건)은 예산이 모자라 영영 못 채웠다.

    Returns: (수집분, 남은 미수집 수, 전체 작업 수, 방향별 통계)
    """
    out: dict = {}
    known = known or {}
    jobs = [(o, d, direction, rk, day)
            for (o, d, direction, rk) in route_pairs
            for day in dates_by_pair.get((o, d, direction), [])]
    total = len(jobs)
    if not total:
        return out, 0, 0, {}
    # 못 모은 것 먼저, 그다음 오래된 것 순
    jobs.sort(key=lambda j: known.get(
        f"{j[3]}|{j[2]}|{j[4].isoformat()}", {}).get("at", ""))
    missing = sum(1 for j in jobs
                  if f"{j[3]}|{j[2]}|{j[4].isoformat()}" not in known)
    if probe_mod is None:
        from . import naver_browser_probe as probe_mod
    if not probe_mod._ensure_playwright():
        log.info("네이버 수집: playwright 준비 실패 → 건너뜀")
        return out, missing, total, {}

    from playwright.sync_api import sync_playwright

    disp = probe_mod._start_display()
    log.info("네이버 수집 시작 (화면 %s, 예산 %d분)", disp or "없음", budget_sec // 60)
    started = time.time()
    done = skipped = 0
    # 방향별 성적. 한쪽만 실패하면(오는 편 92건 중 1건 같은) 즉시 보이도록.
    seen_stat: dict = {}
    form_stat: dict = {}   # (방향, 형식) -> [시도, 읽은 행] — 어느 URL이 통하는지

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not disp,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--window-size=1400,1000"])
        ctx = browser.new_context(
            locale="ko-KR", timezone_id="Asia/Seoul",
            viewport={"width": 1400, "height": 1000},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/139.0.0.0 Safari/537.36"))
        ctx.add_init_script(probe_mod._STEALTH)
        page = ctx.new_page()

        def fresh_page():
            """세션을 새로 연다. 연속 검색이 막히면 이걸로 푼다."""
            nonlocal ctx, page
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            ctx = browser.new_context(
                locale="ko-KR", timezone_id="Asia/Seoul",
                viewport={"width": 1400, "height": 1000},
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/139.0.0.0 Safari/537.36"))
            ctx.add_init_script(probe_mod._STEALTH)
            page = ctx.new_page()

        visited = 0
        fails = 0
        # 세션 구간별 성공률을 잰다. 재시작 직후에 되살아나면 '세션 상태' 문제,
        # 재시작해도 안 되면 '서버가 막는' 것 — 원인을 구분하기 위한 계측.
        seg: list = [[0, 0]]     # [(시도, 성공)] 세션 구간마다 하나
        import random as _rnd
        for o, d, direction, route_key, day in jobs:
            if time.time() - started >= budget_sec:
                break
            if fails >= stop_after_fail:
                log.info("연속 실패 %d회 → 이번 실행은 여기서 중단 "
                         "(차단으로 보임, 다음 실행이 이어받음)", fails)
                break
            if visited and visited % reset_every == 0:
                fresh_page()
                seg.append([0, 0])
                log.info("세션 재시작 (%d건마다)", reset_every)
            if visited:
                time.sleep(_rnd.uniform(delay[0], delay[1]))
            visited += 1
            rows, used = [], ""
            for form, url in _urls(o, d, day, adults):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=40000)
                    page.wait_for_timeout(5000)   # 탐침과 동일하게
                    for sel in ("[class*=searchBox_btn_search]",
                                "button[class*=btn_search]"):
                        try:
                            el = page.locator(sel).first
                            if el.count():
                                el.click(timeout=5000)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    rows = _read_rows(page)
                except Exception as e:  # noqa: BLE001
                    log.info("네이버 %s-%s %s %s 실패: %s",
                             o, d, day, form, str(e)[:80])
                    rows = []
                used = form
                # 마지막 형식만 기록하면 중간 결과가 사라진다. 매번 남긴다.
                form_stat.setdefault((direction, form), [0, 0])
                form_stat[(direction, form)][0] += 1
                form_stat[(direction, form)][1] += len(rows)
                if len(rows) >= _MIN_ROWS:
                    break     # 충분히 읽혔으면 다른 형식은 시도하지 않는다
            best = NV.pick_best(rows, domestic=True,
                                out_window=windows.get((route_key, direction)))
            done += 1
            seg[-1][0] += 1
            if rows:
                seg[-1][1] += 1
            fails = 0 if rows else fails + 1
            stat = seen_stat.setdefault(direction, [0, 0, 0])
            stat[0] += 1                 # 조회
            stat[1] += len(rows)         # 읽은 행
            if best:
                stat[2] += 1             # 조건 통과
            if not best:
                continue
            out[f"{route_key}|{direction}|{day.isoformat()}"] = {
                "price": best["price"] * adults,   # 화면값은 1인 → 총액으로
                "airline": best["airline"],
                "dep_time": best["dep"].strftime("%H:%M") if best["dep"] else "",
                "seat": best.get("seat", ""),
                "card_cond": bool(best.get("card_cond")),
                "source": "naver",
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            }
        skipped = total - visited
        browser.close()

    for (d_, f_), (q, rws) in sorted(form_stat.items()):
        log.info("  URL %s %s: 시도 %d · 읽은 행 평균 %.0f", d_, f_, q, rws / max(q, 1))
    for d_, (q, rws, ok_) in sorted(seen_stat.items()):
        log.info("  방향 %s: 조회 %d · 읽은 행 평균 %.0f · 조건 통과 %d",
                 d_, q, rws / max(q, 1), ok_)
    # 미수집 수는 '이번에 새로 채운 것'만 빼야 한다. out에는 이미 있던 걸
    # 다시 모은 것도 섞여 있어 예전 계산은 실제보다 적게 나왔다 (v1.78).
    newly = sum(1 for k in out if k not in known)
    still = max(0, missing - newly)
    log.info("네이버 수집: %d/%d건 조회 · %d건 확보 · 미수집 %d건 남음 (%.0f분)",
             done, total, len(out), still, (time.time() - started) / 60)
    # 세션 구간별 성공률 (원인 구분용)
    for i, (q, ok_) in enumerate(seg):
        if q:
            merged_key = f"세션{i + 1}"
            seen_stat[merged_key] = [q, ok_ * 100, ok_]   # 두번째 칸은 표시용
    merged = dict(seen_stat)
    for (d_, f_), (q, rws) in form_stat.items():
        merged[f"{d_}·{f_}"] = [q, rws, 0]
    return out, still, total, merged


def _read_rows(page) -> list:
    """결과가 붙을 때까지 기다렸다가 가격 포함 행들을 읽는다.

    앞 60개만 읽었더니 오는 편(18시 이후 조건)이 92건 중 1건만 잡혔다.
    목록이 '출발시각 빠른 순'이라 저녁 편은 뒤쪽에 있는데 잘려나간 것.
    → 넉넉히 읽고 중복을 제거한다 (v1.72).
    """
    # 목록이 스크롤해야 더 그려진다. 오는 편(18시 이후)은 한참 아래에 있어
    # 첫 화면만 읽으면 92건 중 1건밖에 안 잡혔다 (v1.76).
    js = (
        'async () => {'
        '  const NL = String.fromCharCode(10);'
        '  const sleep = ms => new Promise(r => setTimeout(r, ms));'
        '  const pick = () => Array.from('
        '      document.querySelectorAll("[class*=domestic_inner]"))'
        '    .filter(e => (e.innerText || "").indexOf("원") !== -1);'
        '  for (let a = 0; a < 14; a++) {'
        '    if (pick().length) break;'
        '    await sleep(1500);'
        '  }'
        '  let prev = 0;'
        '  for (let s = 0; s < 25; s++) {'
        '    window.scrollTo(0, document.body.scrollHeight);'
        '    await sleep(700);'
        '    const now = pick().length;'
        '    if (now === prev && s > 3) break;'
        '    prev = now;'
        '  }'
        '  const seen = new Set(); const out = [];'
        '  for (const e of pick()) {'
        '    const s2 = (e.innerText || "").split(NL).map(x => x.trim())'
        '               .filter(Boolean).join(" | ").slice(0, 400);'
        '    if (s2 && !seen.has(s2)) { seen.add(s2); out.push(s2); }'
        '    if (out.length >= 400) break;'
        '  }'
        '  return out;'
        '}'
    )
    return page.evaluate(js) or []


def due_now(meta: dict, today: dt.date, hour_kst: int, run_hour: int,
            max_per_day: int) -> bool:
    """지금 실행에서 네이버를 이어받을지.

    '하루 1회'로 두었더니 145건을 한 번에 못 채워 **뒷부분이 영영 밀렸다**.
    게다가 수동 트리거(`naver-run`)를 매번 입력해야 해서 실제로는 거의
    안 돌았다. → 하루 max_per_day 번까지 자동으로 이어받는다 (v1.74).
    """
    if hour_kst < run_hour:
        return False
    if meta.get("naver_day") != today.isoformat():
        return True                      # 오늘 첫 실행
    return int(meta.get("naver_runs", 0)) < max_per_day
