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

    seen_api: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            locale="ko-KR", timezone_id="Asia/Seoul",
            viewport={"width": 1400, "height": 1000},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/139.0.0.0 Safari/537.36"))

        def on_response(res):
            u = res.url
            if "naver.com" not in u:
                return
            if not any(k in u for k in ("api", "graphql", "searchFlights")):
                return
            key = u.split("?")[0]
            if key in seen_api:
                return
            seen_api[key] = {"url": key, "status": res.status,
                             "type": res.headers.get("content-type", "")[:60]}

        ctx.on("response", on_response)
        page = ctx.new_page()

        for c in cases:
            url = _url(c["origin"], c["dest"], c["dep"], c["ret"],
                       adults, c.get("domestic", False))
            row = {"label": c.get("label", ""), "url": url,
                   "google_price": c.get("google_price")}
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # SPA라 결과가 늦게 붙는다. 가격 문자열이 보일 때까지 기다린다.
                for _ in range(20):
                    page.wait_for_timeout(1500)
                    text = page.inner_text("body")
                    if _PRICE.search(text):
                        break
                text = page.inner_text("body")
                prices = sorted({int(m.replace(",", ""))
                                 for m in _PRICE.findall(text)})
                prices = [p for p in prices if p >= 30000]   # 잡음 제거
                row["found"] = len(prices)
                row["min_price"] = prices[0] if prices else None
                row["sample"] = prices[:6]
                row["body_head"] = " ".join(text[:250].split())
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
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
