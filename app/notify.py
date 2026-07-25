"""텔레그램 알림 전송 + 메시지 포매팅."""
from __future__ import annotations

import logging
import os
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


def format_alerts(cfg: Settings, alerts: list[Alert],
                  all_combos: list | None = None) -> list[str]:
    """노선별로 묶어 메시지 생성. 노선당 1개 메시지, 최저 top N 요약.

    all_combos를 주면 알림 조건 미충족이어도 '최저가 +N% 이내'인
    다른 날짜 조합을 함께 보여준다 (추가 검색 비용 0 — 이미 수집된 데이터).
    """
    by_route: dict[str, list[Alert]] = defaultdict(list)
    for a in alerts:
        by_route[a.combo.route.key].append(a)
    combos_by_route: dict[str, list] = defaultdict(list)
    for c in all_combos or []:
        combos_by_route[c.route.key].append(c)

    messages = []
    for _key, items in by_route.items():
        items.sort(key=lambda a: a.combo.price)
        top = items[: cfg.bundle_top_n]
        route = top[0].combo.route
        month = top[0].combo.dep.month
        record = any(a.kind == "record" for a in top)
        head = f"{'🏆' if record else '✈️'} <b>{route.label}</b> " \
               f"{'역대 최저가 갱신!' if record else f'{month}월 기준가 이하 특가'}"

        lines = [head]
        for a in top:
            c = a.combo
            lines.append("")
            # 표시 금액: 왕복 실가가 확보되면 그것을 앞세운다. 편도 합산가는
            # 오는 편이 일본 시장 요금이라 실구매가보다 크게 높다 (v1.12).
            if a.rt_price:
                lines.append(
                    f"<b>{_d(c.dep)} → {_d(c.ret)}</b> · {c.nights}박 · "
                    f"<b>{_won(a.rt_price)}</b> (성인 {cfg.adults}명 왕복 총액)"
                )
            else:
                lines.append(
                    f"<b>{_d(c.dep)} → {_d(c.ret)}</b> · {c.nights}박 · "
                    f"<b>{_won(c.price)}</b> (편도합산 참고치 · 왕복 실가 확인 필요)"
                )
            lines.append(
                f"가는 편 {c.out_leg.get('dep_time','?')} {c.out_leg.get('airline','')} / "
                f"오는 편 {c.ret_leg.get('dep_time','?')} {c.ret_leg.get('airline','')}"
            )
            if a.kind == "record" and a.prev_min:
                drop = (a.prev_min - c.price) / a.prev_min * 100
                lines.append(f"이전 최저 대비 <b>-{drop:.1f}%</b> "
                             f"(감지지표 {_won(c.price)} · 이전 {_won(a.prev_min)})")
            else:
                lines.append(f"감지지표 {_won(c.price)} · 기준가 {_won(a.baseline)}")
            g = google_flights_url(route, c.dep, c.ret)
            n = naver_url(route, c.dep, c.ret, cfg.adults)
            lines.append(f'<a href="{g}">Google Flights</a> · <a href="{n}">네이버항공권</a>')
        if len(items) > len(top):
            lines.append(f"\n…외 {len(items) - len(top)}건 더 조건 충족")

        # 유사 가격대 다른 날짜 (알림 조건 미충족 포함)
        shown = {a.combo.key for a in top}
        floor = top[0].combo.price
        limit = floor * (1 + cfg.similar_margin_pct / 100)
        similar = sorted(
            (c for c in combos_by_route.get(_key, [])
             if c.key not in shown and c.price <= limit),
            key=lambda c: c.price,
        )[: cfg.similar_top_n]
        if similar:
            lines.append(f"\n📅 <b>비슷한 가격대 다른 날짜</b> "
                         f"(+{cfg.similar_margin_pct:.0f}% 이내 · 편도합산 기준)")
            for c in similar:
                lines.append(f"· {_d(c.dep)}~{_d(c.ret)} {c.nights}박 {_won(c.price)}")

        messages.append("\n".join(lines))
    return messages


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
