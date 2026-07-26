"""네이버 항공권 — 헤드리스 브라우저 탐침 (일회성 진단).

경과:
  requests / primp(크롬 TLS 흉내) 모두 API가 503. IP 차단은 아니었다
  (naver.com·항공권 페이지는 200). 아직 안 써본 수단이 **진짜 브라우저**다.

이 탐침이 답해야 할 두 가지:
  1. 브라우저로는 값을 읽을 수 있는가?           ← 되면 하루 1회 전체 훑기 가능
  2. 구글에 없거나 더 싼 값이 실제로 있는가?      ← 없으면 붙일 이유가 없다

그래서 **이미 구글 가격이 있는 날짜쌍**으로 시험해 나란히 비교한다.
덤으로 페이지가 호출하는 API 주소를 응답 가로채기로 기록한다 — 브라우저가
통과시키는 요청이 무엇인지 알면 나중에 가벼운 방식으로 옮길 여지가 생긴다.

결과는 로그가 아니라 `data/naver_probe.json`에 쓴다 (워크플로우가 data/를
커밋하므로 로그를 옮기지 않고도 확인할 수 있다).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_PRICE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3}){1,3})\s*원")


def _ensure_playwright() -> bool:
    """실행 시점에 설치한다. requirements.txt에 넣으면 평소 실행까지 무거워진다."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        log.info("playwright 설치 중…")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                            "playwright"], capture_output=True, text=True)
        if r.returncode != 0:
            log.info("playwright 설치 실패: %s", (r.stderr or "")[-300:])
            return False
    r = subprocess.run([sys.executable, "-m", "playwright", "install",
                        "--with-deps", "chromium"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # --with-deps 는 sudo가 필요할 수 있다. 브라우저만 받아본다.
        r = subprocess.run([sys.executable, "-m", "playwright", "install",
                            "chromium"], capture_output=True, text=True)
    if r.returncode != 0:
        log.info("chromium 내려받기 실패: %s", (r.stderr or "")[-300:])
        return False
    return True


def _start_display() -> str:
    """가상 화면(Xvfb)을 띄운다. 실패하면 빈 문자열.

    3차까지 headless로 돌렸는데 검색이 끝내 안 걸렸다. headless는 가장
    알아보기 쉬운 형태다. 실제 화면을 띄운 브라우저는 구별이 훨씬 어렵다.
    GitHub 러너는 sudo가 되므로 xvfb를 직접 설치해 쓴다.
    """
    import os
    import shutil
    import time
    if not shutil.which("Xvfb"):
        r = subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "xvfb"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.info("xvfb 설치 실패: %s", (r.stderr or "")[-200:])
            return ""
    try:
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1400x1000x24",
                          "-nolisten", "tcp"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        os.environ["DISPLAY"] = ":99"
        return ":99"
    except Exception as e:  # noqa: BLE001
        log.info("Xvfb 실행 실패: %s", str(e)[:150])
        return ""


# 자동화 흔적을 지우는 초기 스크립트. 페이지 스크립트보다 먼저 실행된다.
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR','ko','en-US']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
const _q = navigator.permissions && navigator.permissions.query;
if (_q) navigator.permissions.query = (p) => (
  p && p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : _q(p));

// 검색 응답 가로채기 (v1.62).
// searchFlights 는 text/event-stream 이라 Playwright의 res.text()로는
// 못 읽는다(스트리밍이라 버려짐). 대신 페이지의 fetch를 감싸 응답을 복제해
// 통째로 모아둔다. clone()은 원본 스트림을 방해하지 않는다.
window.__nv = '';
// 6차에서 clone().text()가 0자였다. SSE는 연결이 계속 열려 있어 text()가
// 스트림 종료까지 기다리다 끝내 값을 못 준다. → **조각이 올 때마다 받는다.**
function _pump(res) {
  try {
    const r = res.clone().body.getReader();
    const dec = new TextDecoder();
    (function step() {
      r.read().then(({done, value}) => {
        if (value) window.__nv += dec.decode(value, {stream: true});
        if (!done) step();
      }).catch(() => {});
    })();
  } catch (e) {}
}
const _of = window.fetch;
window.fetch = async function (...args) {
  const res = await _of.apply(this, args);
  try {
    const u = (args[0] && args[0].url) || String(args[0] || '');
    if (u.indexOf('searchFlights') !== -1) _pump(res);
  } catch (e) {}
  return res;
};
// fetch가 아니라 XHR을 쓸 가능성도 덮는다
const _oo = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function (m, u) {
  this.__nvWatch = String(u || '').indexOf('searchFlights') !== -1;
  return _oo.apply(this, arguments);
};
const _os = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.send = function () {
  if (this.__nvWatch) {
    this.addEventListener('progress', () => {
      try { window.__nv = this.responseText || window.__nv; } catch (e) {}
    });
    this.addEventListener('load', () => {
      try { window.__nv += this.responseText || ''; } catch (e) {}
    });
  }
  return _os.apply(this, arguments);
};
const _OES = window.EventSource;
if (_OES) {
  window.EventSource = function (u, c) {
    const es = new _OES(u, c);
    if (String(u).indexOf('searchFlights') !== -1) {
      es.addEventListener('message', ev => { window.__nv += ev.data + '\\n'; });
    }
    return es;
  };
}
"""


def _url(o: str, d: str, dep: str, ret: str, adults: int, domestic: bool) -> str:
    kind = "domestic" if domestic else "international"
    return (f"https://flight.naver.com/flights/{kind}/"
            f"{o}-{d}-{dep}/{d}-{o}-{ret}?adult={adults}"
            f"&isDirect=true&fareType=Y")


def run(cases: list, adults: int, out_path: Path) -> dict:
    """cases: [{origin,dest,dep,ret,domestic,google_price,label}, ...]"""
    result = {"ok": False, "cases": [], "api_calls": [], "note": ""}
    if not _ensure_playwright():
        result["note"] = "playwright 준비 실패"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        return result

    from playwright.sync_api import sync_playwright

    disp = _start_display()
    result["display"] = disp or "없음(headless로 진행)"
    log.info("가상 화면: %s", result["display"])

    seen_api: dict = {}
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

        bodies: dict = {}

        def on_response(res):
            # 4차에서 검색 API를 특정했다:
            #   flight-api.naver.com/flight/{domestic|international}/searchFlights
            #   → 201, text/event-stream (SSE)
            # 화면 글자를 정규식으로 긁지 말고 **응답 본문을 직접** 받는다.
            if "searchFlights" in res.url and res.url not in bodies:
                try:
                    bodies[res.url] = res.text()
                except Exception as e:  # noqa: BLE001
                    bodies[res.url] = f"__본문 못 읽음: {str(e)[:100]}"
            # 1차 탐침에서 URL에 api/graphql이 든 것만 기록했더니 설정 파일과
            # 에러 수집기만 잡혔다. 걸러내지 말고 naver 요청을 다 본다.
            u = res.url
            if "naver" not in u:
                return
            if any(u.endswith(x) for x in (".js", ".css", ".png", ".jpg",
                                           ".svg", ".woff", ".woff2", ".ico")):
                return
            key = u.split("?")[0]
            if key in seen_api:
                return
            seen_api[key] = {"url": key, "status": res.status,
                             "type": res.headers.get("content-type", "")[:60]}

        ctx.add_init_script(_STEALTH)
        ctx.on("response", on_response)
        page = ctx.new_page()

        for c in cases:
            url = _url(c["origin"], c["dest"], c["dep"], c["ret"],
                       adults, c.get("domestic", False))
            row = {"label": c.get("label", ""), "url": url,
                   "google_price": c.get("google_price")}
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(5000)

                # 2차에서 button:has-text('검색')이 헤더 통합검색을 눌러
                # 페이지가 오류 화면(본문 513자)으로 바뀌었다.
                # → 선택자를 더 추측하지 말고 **실제 요소 목록을 뽑는다.**
                # 3차에서 실물 확인: 검색 버튼 class = searchBox_btn_search__*
                clicked = ""
                for sel in ("[class*=searchBox_btn_search]",
                            "button[class*=btn_search]"):
                    try:
                        el = page.locator(sel).first
                        if el.count():
                            el.click(timeout=8000)
                            clicked = sel
                            break
                    except Exception:  # noqa: BLE001
                        continue
                row["clicked"] = clicked or "검색 버튼 못 찾음"
                page.wait_for_timeout(2000)

                # 결과가 붙을 때까지 기다린다 (검색 10~30초)
                for _ in range(40):
                    page.wait_for_timeout(1500)
                    if _PRICE.search(page.inner_text("body")):
                        break

                # 7차에서 querySelector는 되는데 직후 querySelectorAll이 0이었다.
                # SSE로 결과가 계속 다시 그려져 호출 사이에 비는 순간이 있다.
                # → 호출을 나누지 말고 **한 번에, 재시도까지 JS 안에서** 처리한다.
                # 8차: 행 구조는 잡혔으나 **가격이 없었다** — 고른 컨테이너가
                # 일정 부분만 감싸고 가격은 바깥에 있다.
                # → 부모로 올라가며 '원'이 나오는 조상까지 확장해서 읽는다.
                row["rows"] = page.evaluate("""async () => {
                    const sels = ['[class^=domestic_item]',
                                  '[class*=combination_item]',
                                  '[class*=international_item]'];
                    const sleep = ms => new Promise(r => setTimeout(r, ms));
                    const upToPrice = (e) => {
                      let n = e;
                      for (let i = 0; i < 5 && n; i++) {
                        const t = n.innerText || '';
                        if (/[0-9],[0-9]{3}\\s*원/.test(t)) return n;
                        n = n.parentElement;
                      }
                      return e;
                    };
                    const clean = (e) => (e.innerText || '').split('\\n')
                        .map(x => x.trim()).filter(Boolean).join(' | ').slice(0, 420);
                    for (let a = 0; a < 8; a++) {
                      for (const s of sels) {
                        const els = Array.from(document.querySelectorAll(s))
                          .filter(e => (e.innerText || '').length > 30);
                        if (!els.length) continue;
                        const seen = new Set(); const out = [];
                        for (const e of els) {
                          const p = upToPrice(e);
                          if (seen.has(p)) continue;
                          seen.add(p);
                          out.push(clean(p));
                          if (out.length >= 5) break;
                        }
                        // 첫 행의 조상 사슬도 함께 남긴다 (어느 층에 가격이 있는지)
                        const chain = [];
                        let n = els[0];
                        for (let i = 0; i < 4 && n; i++) {
                          chain.push((n.className || '').toString().slice(0, 40)
                                     + ' >> ' + clean(n).slice(0, 160));
                          n = n.parentElement;
                        }
                        return {sel: s, n: els.length, texts: out, chain: chain};
                      }
                      await sleep(1200);
                    }
                    return {sel: '', n: 0, texts: [], chain: []};
                }""")
                text = page.inner_text("body")
                row["body_len"] = len(text)
                for marker in ("검색 결과", "항공편이 없", "다시 검색",
                               "로그인", "일시적", "오류"):
                    if marker in text:
                        row.setdefault("markers", []).append(marker)
                prices = sorted({int(m.replace(",", ""))
                                 for m in _PRICE.findall(text)})
                prices = [p for p in prices if p >= 30000]   # 잡음 제거
                row["found"] = len(prices)
                row["min_price"] = prices[0] if prices else None
                row["sample"] = prices[:6]
                # 파서를 짜려면 실물 배치를 봐야 한다. 결과가 시작되는 지점부터 넉넉히.
                mark = max(text.find("가는 편 선택"), text.find("가격 낮은 순"),
                           text.find("출발시각 빠른 순"), 0)
                row["body_sample"] = " | ".join(
                    x.strip() for x in text[mark:mark + 3000].splitlines() if x.strip())
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
            # 페이지가 모아둔 응답 본문을 꺼낸다
            try:
                cap = page.evaluate("() => window.__nv || ''")
            except Exception:  # noqa: BLE001
                cap = ""
            row["captured_len"] = len(cap)
            if cap:
                row["captured_head"] = cap[:2000]
                # JSON 최상위 키를 뽑아본다 (구조 파악의 핵심)
                try:
                    obj = json.loads(cap)
                    row["json_keys"] = list(obj)[:20]
                except ValueError:
                    lines_ = [x for x in cap.splitlines() if x.startswith("data:")]
                    row["sse_data_lines"] = len(lines_)
                    if lines_:
                        try:
                            obj = json.loads(lines_[-1][5:].strip())
                            row["json_keys"] = list(obj)[:20]
                        except ValueError:
                            pass
            bodies.clear()

            # 결과 행 하나의 구조도 남긴다 (응답을 못 읽을 때 대비)
            try:
                row["row_html"] = page.evaluate("""() => {
                    const sels = ['[class^=domestic_item]', '[class^=international_item]',
                                  '[class*=_item__]', '[class*=item_wrap]',
                                  '[role=row]'];
                    for (const s of sels) {
                      const el = document.querySelector(s);
                      if (el && el.innerText && el.innerText.length > 30)
                        return s + ' ||| ' + el.outerHTML.slice(0, 1400);
                    }
                    return '';
                }""")
                row["rows_text"] = page.evaluate("""() => {
                    const sels = ['[class^=domestic_item]', '[class*=combination_item]',
                                  '[class*=international_item]', '[class*=_item__]'];
                    for (const s of sels) {
                      const els = [...document.querySelectorAll(s)];
                      if (els.length >= 1) {
                        return {sel: s, n: els.length,
                                texts: els.slice(0, 3).map(
                                  e => (e.innerText || '').replace(/\\s*\\n\\s*/g, ' | ').slice(0, 320))};
                      }
                    }
                    return {sel: '', n: 0, texts: []};
                }""")
            except Exception:  # noqa: BLE001
                row["row_html"] = ""

            result["cases"].append(row)
            log.info("NVB %s → 가격 %s개, 최저 %s (구글 %s)",
                     row["label"], row.get("found"), row.get("min_price"),
                     row.get("google_price"))

        browser.close()

    result["api_calls"] = list(seen_api.values())
    ok = [r for r in result["cases"] if r.get("min_price")]
    result["ok"] = bool(ok)
    result["note"] = (f"{len(ok)}/{len(result['cases'])} 건에서 가격 확보"
                      if result["cases"] else "시험 대상 없음")
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return result
