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

_ONEWAY = "https://flight.naver.com/flights/domestic/{o}-{d}-{ymd}?adult={n}"


def collect(route_pairs: list, dates_by_pair: dict, adults: int,
            windows: dict, budget_sec: int = 1500,
            start_at: int = 0, probe_mod=None) -> tuple[dict, int, int]:
    """편도 단위로 훑어 {leg_key: {...}} 를 돌려준다.

    route_pairs: [("GMP","CJU","out","GMP-CJU"), ("CJU","GMP","ret","GMP-CJU"), ...]
    dates_by_pair: {(o,d,direction): [date, ...]}
    windows: {(route_key, direction): (lo, hi)}
    budget_sec: 이 시간을 넘기면 **거기서 멈추고 다음 실행이 이어받는다**.
                버리면 뒷부분 날짜가 영영 갱신되지 않는다 (v1.70).
    start_at:   이어받을 위치. 전체를 한 바퀴 돌면 0으로 되돌아간다.

    Returns: (수집분, 다음 시작 위치, 전체 작업 수)
    """
    out: dict = {}
    jobs = [(o, d, direction, rk, day)
            for (o, d, direction, rk) in route_pairs
            for day in dates_by_pair.get((o, d, direction), [])]
    total = len(jobs)
    if not total:
        return out, 0, 0
    start_at = start_at % total
    if probe_mod is None:
        from . import naver_browser_probe as probe_mod
    if not probe_mod._ensure_playwright():
        log.info("네이버 수집: playwright 준비 실패 → 건너뜀")
        return out, start_at, total

    from playwright.sync_api import sync_playwright

    disp = probe_mod._start_display()
    log.info("네이버 수집 시작 (화면 %s, 예산 %d분)", disp or "없음", budget_sec // 60)
    started = time.time()
    done = skipped = 0

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

        idx = start_at
        visited = 0
        while visited < total and time.time() - started < budget_sec:
            o, d, direction, route_key, day = jobs[idx % total]
            idx += 1
            visited += 1
            url = _ONEWAY.format(o=o, d=d, ymd=day.strftime("%Y%m%d"), n=adults)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(2500)
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
                log.info("네이버 %s-%s %s 실패: %s", o, d, day, str(e)[:90])
                continue

            best = NV.pick_best(rows, domestic=True,
                                out_window=windows.get((route_key, direction)))
            done += 1
            if not best:
                continue
            out[f"{route_key}|{direction}|{day.isoformat()}"] = {
                "price": best["price"] * adults,   # 화면값은 1인 → 총액으로
                "airline": best["airline"],
                "dep_time": best["dep"].strftime("%H:%M") if best["dep"] else "",
                "seat": best.get("seat", ""),
                "source": "naver",
            }
        skipped = total - visited
        browser.close()

    log.info("네이버 수집: %d/%d건 조회 · %d건 확보 · 남은 %d건은 다음 실행이 이어받음 "
             "(%.0f분, 다음 시작 %d)",
             done, total, len(out), skipped, (time.time() - started) / 60,
             idx % total)
    return out, idx % total, total


def _read_rows(page) -> list:
    """결과가 붙을 때까지 기다렸다가 가격 포함 행들을 읽는다.

    앞 60개만 읽었더니 오는 편(18시 이후 조건)이 92건 중 1건만 잡혔다.
    목록이 '출발시각 빠른 순'이라 저녁 편은 뒤쪽에 있는데 잘려나간 것.
    → 넉넉히 읽고 중복을 제거한다 (v1.72).
    """
    js = (
        'async () => {'
        '  const NL = String.fromCharCode(10);'
        '  const sleep = ms => new Promise(r => setTimeout(r, ms));'
        '  for (let a = 0; a < 14; a++) {'
        '    const els = Array.from(document.querySelectorAll("[class*=domestic_inner]"))'
        '      .filter(e => (e.innerText || "").indexOf("원") !== -1);'
        '    if (els.length) {'
        '      const seen = new Set(); const out = [];'
        '      for (const e of els) {'
        '        const s = (e.innerText || "").split(NL).map(x => x.trim())'
        '                   .filter(Boolean).join(" | ").slice(0, 400);'
        '        if (s && !seen.has(s)) { seen.add(s); out.push(s); }'
        '        if (out.length >= 250) break;'
        '      }'
        '      return out;'
        '    }'
        '    await sleep(1500);'
        '  }'
        '  return [];'
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
