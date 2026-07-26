"""텔레그램 알림 전송 + 메시지 포매팅."""
from __future__ import annotations

import logging
import os
import datetime as dt
from collections import defaultdict

import requests

from .engine import Alert
from .links import google_flights_url, naver_url
from .settings import Settings

log = logging.getLogger(__name__)

_WEEKDAY = "월화수목금토일"


def _d(date) -> str:
    return f"{date.month}/{date.day}({_WEEKDAY[date.weekday()]})"


def _won(n: int) -> str:
    return f"₩{n:,}"


def _per(total: int, adults: int) -> str:
    """1인당 금액을 앞세우고 총액을 괄호에 (사용자 요청, v1.23)."""
    if adults <= 1:
        return _won(total)
    return f"{_won(round(total / adults))}/인 (총 {_won(total)})"


def format_alerts(cfg: Settings, alerts: list[Alert],
                  all_combos: list | None = None,
                  today: "dt.date | None" = None) -> list[str]:
    """노선별로 묶어 메시지 생성. 노선당 1개 메시지, 최저 top N 요약.

    all_combos를 주면 알림 조건 미충족이어도 '최저가 +N% 이내'인
    다른 날짜 조합을 함께 보여준다 (추가 검색 비용 0 — 이미 수집된 데이터).
    """
    today = today or (dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(hours=9)).date()
    by_route: dict[str, list[Alert]] = defaultdict(list)
    for a in alerts:
        by_route[a.combo.route.key].append(a)
    combos_by_route: dict[str, list] = defaultdict(list)
    for c in all_combos or []:
        combos_by_route[c.route.key].append(c)

    def best_price(a) -> int:
        """실제로 화면에 강조되는 금액 = 편도 2장과 왕복 티켓 중 싼 쪽.

        정렬은 이 값으로 해야 한다. 감지 지표(편도 2장)로 정렬하면 왕복이 더
        싼 항목이 뒤로 밀려서 눈에 보이는 숫자가 뒤죽박죽이 된다 (v1.25).
        """
        cands = [a.combo.price]
        if a.rt_price:
            cands.append(a.rt_price)
        return min(cands)

    messages: list[tuple[int, str]] = []
    for _key, items in by_route.items():
        # 노출 대상 선별은 감지 지표 기준 (engine.display_selection과 동일해야
        # 왕복 검증을 받은 항목과 실제 표시 항목이 어긋나지 않는다)
        items.sort(key=lambda a: a.combo.price)
        top = items[: cfg.bundle_top_n]
        # 선별이 끝난 뒤 표시 순서만 실제 금액 기준으로 다시 정렬
        top.sort(key=best_price)

        # 가격 차이가 미미한 항목은 빼서 줄 수를 줄인다 (v1.27).
        # 옆 날짜가 1~2% 차이로 줄줄이 뜨는 건 정보가 아니라 소음이고,
        # 그런 날짜들은 어차피 아래 '유사 가격대' 목록에 나온다.
        pruned = []
        for a in top:
            if pruned and best_price(a) <= best_price(pruned[-1]) * (
                    1 + cfg.bundle_min_gap_pct / 100):
                continue
            pruned.append(a)
        top = pruned
        route = top[0].combo.route
        month = top[0].combo.dep.month
        record = any(a.kind == "record" for a in top)
        n = cfg.adults
        head_price = round(best_price(top[0]) / max(n, 1))

        # 제목에 금액을 넣는다: 잠금화면 미리보기에서 노선만 보이고 얼마인지
        # 안 보이면 열어봐야 알 수 있다 (v1.29).
        lines = [f"{'🏆' if record else '✈️'} <b>{route.label} 1인 {head_price:,}원</b>"]

        # 왜 싼지 한 줄로. '기준가' 같은 내부 용어 대신 체감되는 표현으로.
        base_per = round(top[0].baseline / max(n, 1))
        if record and top[0].prev_min:
            prev_per = round(top[0].prev_min / max(n, 1))
            cut = (top[0].prev_min - top[0].combo.price) / top[0].prev_min * 100
            lines.append(f"{month}월 역대 최저 · 직전 최저 {prev_per:,}원보다 "
                         f"{cut:.0f}% 쌉니다")
        else:
            cut = (top[0].baseline - best_price(top[0])) / max(top[0].baseline, 1) * 100
            if cut >= 1:
                lines.append(f"{month}월 요즘 최저가({base_per:,}원)보다 {cut:.0f}% 쌉니다")
            else:
                lines.append(f"{month}월 요즘 최저가({base_per:,}원) 수준입니다")

        for a in top:
            c = a.combo
            lines.append("")
            dday = (c.dep - today).days
            when = f"D-{dday}" if dday > 0 else ("오늘 출발" if dday == 0 else "")
            lines.append(f"<b>{_d(c.dep)} → {_d(c.ret)}</b> · {c.nights}박"
                         + (f" · {when}" if when else ""))

            # 실제로 낼 돈만 굵게. 대안 구매법은 한 줄 아래에 이유와 함께.
            # (편도 2장이 쌀 때도 왕복이 쌀 때도 있어 한쪽 고정이 불가 — v1.14 실측)
            one, rt = c.price, a.rt_price
            if rt and rt < one:
                lines.append(f"왕복권 <b>{round(rt / n):,}원</b>/인 · "
                             f"{n}명 {rt:,}원")
                lines.append(f"편도 2장으로 사면 {round(one / n):,}원/인 → 왕복이 유리")
            elif rt:
                lines.append(f"편도 2장 <b>{round(one / n):,}원</b>/인 · "
                             f"{n}명 {one:,}원")
                lines.append(f"왕복권으로 사면 {round(rt / n):,}원/인 → 편도 2장이 유리")
            else:
                lines.append(f"편도 2장 <b>{round(one / n):,}원</b>/인 · "
                             f"{n}명 {one:,}원")

            out_air = c.out_leg.get("airline", "")
            ret_air = c.ret_leg.get("airline", "")
            air = out_air if out_air == ret_air else f"{out_air} / {ret_air}"
            lines.append(f"{c.out_leg.get('dep_time','?')} 출발 · "
                         f"{c.ret_leg.get('dep_time','?')} 귀국 · {air}")

            # 재알림일 때만 표시. 첫 알림은 대부분 첫 알림이라 배지가 의미 없다.
            if a.prev_sent:
                gap = a.prev_sent - c.price
                lines.append(f"🔻 지난 알림 {round(a.prev_sent / n):,}원/인에서 "
                             f"<b>{round(gap / n):,}원 더 내렸어요</b>")

            codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]
            g = google_flights_url(route, c.dep, c.ret, cfg.adults, codes)
            nv = naver_url(route, c.dep, c.ret, cfg.adults)
            tag = f" ({out_air}만)" if any(x for x in codes if x) and out_air else ""
            lines.append(f'<a href="{g}">구글에서 보기{tag}</a> · '
                         f'<a href="{nv}">네이버</a>')

        # 근처 날짜 목록 — 이미 수집된 데이터라 추가 검색 비용 0.
        # 기준은 반드시 '편도 2장'끼리여야 한다. 위 대표 금액이 왕복일 때
        # 그걸 기준으로 잡으면 임계가 낮아져 목록이 거의 안 뜬다 (v1.29).
        shown_deps = {(a.combo.dep, a.combo.nights) for a in top}
        base = min(a.combo.price for a in top)
        limit = base * (1 + cfg.similar_margin_pct / 100)
        near = sorted(
            (c for c in combos_by_route.get(_key, [])
             if (c.dep, c.nights) not in shown_deps and c.price <= limit),
            key=lambda c: c.price)[: cfg.similar_top_n]
        if near:
            lines.append("")
            lines.append("📅 <b>근처 날짜도 비슷한 값</b> — 편도 2장 기준이라 "
                         "왕복은 더 쌀 수 있어요")
            for c in near:
                lines.append(f"· {_d(c.dep)}~{_d(c.ret)} {c.nights}박 "
                             f"{round(c.price / n):,}원/인")

        messages.append((best_price(top[0]), "\n".join(lines)))

    # 노선 간에도 싼 순서로: 가장 저렴한 노선의 메시지가 먼저 간다
    messages.sort(key=lambda m: m[0])
    return [m for _, m in messages]


def send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.error("telegram %s: %s", r.status_code, r.text[:200])
        return r.status_code == 200
    except requests.RequestException as e:
        log.error("telegram send failed: %s", e)
        return False
