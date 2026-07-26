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


_AIRPORT_KO = {"ICN": "인천", "GMP": "김포", "HND": "하네다", "NRT": "나리타",
               "KIX": "간사이", "NGO": "나고야", "CJU": "제주", "CTS": "삿포로",
               "FUK": "후쿠오카", "OKA": "오키나와", "KOJ": "가고시마"}


def _ko(code: str) -> str:
    return _AIRPORT_KO.get(code, code)


def _airport_note(c) -> str:
    """어느 공항에서 뜨고 어디로 돌아오는지. 공항을 헷갈리면 비행기를 놓친다."""
    b = c.back
    if not c.is_cross:
        return f" · {_ko(c.route.origin)} 왕복"
    return (f" · ⇄ {_ko(c.route.origin)} 출발 / {_ko(b.origin)} 귀국"
            f"{'' if b.destination == c.route.destination else f' ({_ko(c.route.destination)} 입 / {_ko(b.destination)} 출)'}")


def _leg_time(leg: dict) -> str:
    """출발 시각. 선호 시간대 밖이면 ⚠, 네이버 값이면 (네이버).

    출처를 항목 끝에 한 번만 적으면 어느 편이 네이버인지 알 수 없다.
    공항을 헷갈리면 비행기를 놓치듯, 사이트를 헷갈리면 그 가격이 없다 (v1.71).
    """
    mark = "⚠" if leg.get("off_window") else ""
    src = "(네이버)" if leg.get("source") == "naver" else ""
    return f"{leg.get('dep_time', '?')}{mark}{src}"


# 항공사명은 **IATA 코드 기준**으로 매핑한다. 이름은 사명 변경에 흔들린다 —
# 실제로 티웨이항공이 '트리니티항공(Trinity Airways)'으로 바꾸는 중이라
# 구글이 이미 새 이름을 쓰고 있었다. 코드(TW)와 편명은 그대로다.
_AIRLINE_BY_CODE = {
    "KE": "대한항공", "OZ": "아시아나", "7C": "제주항공", "LJ": "진에어",
    "TW": "티웨이", "BX": "에어부산", "RS": "에어서울", "ZE": "이스타",
    "YP": "에어프레미아", "4V": "파라타",
    "MM": "피치", "NH": "ANA", "JL": "JAL", "ZG": "집에어", "IJ": "스프링재팬",
}
# 코드를 못 얻은 경우를 위한 이름 보조 매핑
_AIRLINE_BY_NAME = {
    "Korean Air": "대한항공", "Asiana Airlines": "아시아나", "Jeju Air": "제주항공",
    "Jin Air": "진에어", "T'way Air": "티웨이", "Trinity Airways": "티웨이",
    "Air Busan": "에어부산", "Air Seoul": "에어서울", "Eastar Jet": "이스타",
    "Air Premia": "에어프레미아", "Peach Aviation": "피치", "Peach": "피치",
    "ZIPAIR Tokyo": "집에어", "ZIPAIR": "집에어",
    "All Nippon Airways": "ANA", "Japan Airlines": "JAL",
}


def _ko_air(name: str, code: str = "") -> str:
    return (_AIRLINE_BY_CODE.get((code or "").strip().upper())
            or _AIRLINE_BY_NAME.get((name or "").strip())
            or name)


def _src(c) -> str:
    """네이버에서 온 값이면 표시. 어디서 사야 하는지 알아야 한다."""
    s = {c.out_leg.get("source"), c.ret_leg.get("source")}
    return " · 네이버" if "naver" in s else ""


def _alt_line(c, adults: int) -> str:
    """구글·네이버가 겹칠 때 진 쪽도 알려준다.

    네이버 특가석은 환불·변경 제약이 있어 더 비싸도 구글 쪽을 고르고 싶을 수
    있다. 5% 넘게 차이날 때만 (그 아래는 소음).
    """
    parts = []
    for leg, ko in ((c.out_leg, "가는 편"), (c.ret_leg, "오는 편")):
        alt = leg.get("alt_price")
        cur = leg.get("price")
        if not alt or not cur:
            continue
        gap = (alt - cur) / cur * 100
        if gap < 5:
            continue
        seat = leg.get("seat") or ""
        parts.append(f"{ko} {_ko_src(leg.get('source'))}{(' ' + seat) if seat else ''} "
                     f"{round(cur / adults):,} vs "
                     f"{_ko_src(leg.get('alt_source'))} {round(alt / adults):,}")
    # 근처 날짜 목록이 '·'로 시작하므로 마커를 달리 한다 (혼동 방지)
    return "" if not parts else "↳ " + " / ".join(parts) + " (1인)"


def _ko_src(s: str | None) -> str:
    return {"naver": "네이버", "google": "구글"}.get(s or "", "구글")


def _airlines(c) -> str:
    """가는 편·오는 편 항공사. 같으면 한 번만. 이름은 한글로."""
    a = _ko_air(c.out_leg.get("airline", ""), c.out_leg.get("carrier", ""))
    b = _ko_air(c.ret_leg.get("airline", ""), c.ret_leg.get("carrier", ""))
    return a if a == b else f"{a}/{b}"


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
    # 노선이 아니라 **도시** 단위로 묶는다 (v1.41). 같은 나고야인데 인천발·김포발·
    # 교차 조합이 따로 메시지로 나가면 어느 게 싼지 비교가 안 된다.
    from .engine import _seoul_group, city_label
    by_route: dict[str, list[Alert]] = defaultdict(list)
    for a in alerts:
        by_route[_seoul_group(cfg, a.combo.route)].append(a)
    combos_by_route: dict[str, list] = defaultdict(list)
    for c in all_combos or []:
        combos_by_route[_seoul_group(cfg, c.route)].append(c)

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
        n = cfg.adults
        # 🏆는 '깰 이전 기록이 있을 때'만. 새로 잡힌 조합은 비교 대상이 없어
        # 역대 최저라고 해봐야 의미가 없다 (v1.42).
        record = any(a.kind == "record" and a.prev_min for a in top)
        lines = [""]   # 제목은 표시 항목을 다 정한 뒤 채운다 (아래 head_idx)
        head_idx = 0
        # 이 도시에 서울발 공항이 여러 개면 항목마다 공항을 밝힌다.
        multi_air = len({(c.route.origin, c.route.destination)
                         for c in combos_by_route.get(_key, [])
                         } | {(a.combo.route.origin, a.combo.route.destination)
                              for a in top}) > 1

        # 근처 날짜 후보 (알림 조건은 아니지만 값이 비슷한 조합)
        shown_deps = {(a.combo.dep, a.combo.nights) for a in top}
        base = min(a.combo.price for a in top)
        limit = base * (1 + cfg.similar_margin_pct / 100)
        cands = [c for c in combos_by_route.get(_key, [])
                 if (c.dep, c.nights) not in shown_deps and c.price <= limit]
        # 같은 날 출발·같은 가격이면 박 수가 긴 쪽이 이득이라 그것만 남긴다.
        # (3박과 4박이 같은 값이라 두 줄씩 뜨던 문제)
        pick: dict = {}
        for c in cands:
            k = (c.dep, c.price, c.out_leg.get("dep_time"), c.ret_leg.get("dep_time"))
            if k not in pick or c.nights > pick[k].nights:
                pick[k] = c
        near = sorted(pick.values(), key=lambda c: (c.price, c.dep))[: cfg.similar_top_n]

        # 주 항목과 근처 날짜를 **하나의 오름차순 스트림**으로 합친다 (v1.38).
        # 예전엔 두 구역을 따로 정렬해, 근처 날짜 상한이 '가장 싼 주 항목 +10%'인
        # 탓에 두 번째 주 항목보다 근처 날짜가 싸게 나왔다. 실측 김포-제주에서
        # 13만 → 16만 → 13만 순으로 보여 정렬이 깨진 것처럼 읽혔다.
        stream = [(best_price(a), 0, a) for a in top]
        stream += [(c.price, 1, c) for c in near]
        stream.sort(key=lambda x: (x[0], x[1]))

        # 제목의 "N원부터"는 **이 메시지에 실제로 실리는 것 중 최저가**여야 한다.
        # 알림 항목만 보고 정하면, 더 싼 근처 날짜가 바로 아래 있는데도 제목이
        # 비싼 값을 말하는 모순이 생긴다 (v1.44).
        head_price = round(min(x[0] for x in stream) / max(n, 1))
        lines[head_idx] = (f"{'🏆' if record else '✈️'} "
                           f"<b>{city_label(cfg, route)} 1인 {head_price:,}원부터</b>")

        for _price, kind, obj in stream:
            if kind == 1:  # 근처 날짜 — 한 줄
                c = obj
                codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]
                url = google_flights_url(c.route, c.dep, c.ret, cfg.adults, codes,
                                         back=c.back if c.is_cross else None)
                air = _airport_note(c) if (multi_air or c.is_cross) else ""
                lines.append(
                    f'· <a href="{url}">{_d(c.dep)}~{_d(c.ret)}</a> {c.nights}박{air} · '
                    f'{_leg_time(c.out_leg)}/{_leg_time(c.ret_leg)} {_airlines(c)} · '
                    f'{round(c.price / n):,}원/인')
                continue

            a, c = obj, obj.combo
            lines.append("")
            air = _airport_note(c) if (multi_air or c.is_cross) else ""
            lines.append(f"<b>{_d(c.dep)} → {_d(c.ret)}</b> · {c.nights}박{air}")

            one, rt = c.price, a.rt_price
            if rt and rt < one:
                lines.append(f"왕복권 <b>{round(rt / n):,}원</b>/인 · {n}명 {rt:,}원")
                lines.append(f"편도 2장으로 사면 {round(one / n):,}원/인 → 왕복이 유리")
            elif rt:
                lines.append(f"편도 2장 <b>{round(one / n):,}원</b>/인 · {n}명 {one:,}원")
                lines.append(f"왕복권으로 사면 {round(rt / n):,}원/인 → 편도 2장이 유리")
            else:
                lines.append(f"편도 2장 <b>{round(one / n):,}원</b>/인 · {n}명 {one:,}원")

            # 왜 싼지를 먼저, 그다음 어떤 편인지, 마지막에 대안 비교.
            m = c.dep.month
            if a.kind == "record" and a.prev_min:
                prev_per = round(a.prev_min / max(n, 1))
                cut = (a.prev_min - c.price) / a.prev_min * 100
                lines.append(f"{m}월 역대 최저 · 직전 최저 {prev_per:,}원보다 "
                             f"{cut:.0f}% 쌉니다")
            elif a.kind == "record":
                lines.append(f"🆕 이번에 새로 찾은 조합 · {m}월 현재 최저가")
            else:
                base_per = round(a.baseline / max(n, 1))
                cut = (a.baseline - best_price(a)) / max(a.baseline, 1) * 100
                lines.append(f"{m}월 요즘 최저가({base_per:,}원)보다 {cut:.0f}% 쌉니다")

            if a.prev_sent:
                gap = a.prev_sent - c.price
                lines.append(f"🔻 지난 알림 {round(a.prev_sent / n):,}원/인에서 "
                             f"<b>{round(gap / n):,}원 더 내렸어요</b>")

            lines.append(f"{_leg_time(c.out_leg)} 출발 · "
                         f"{_leg_time(c.ret_leg)} 귀국 · {_airlines(c)}")
            alt = _alt_line(c, n)
            if alt:
                lines.append(alt)

            codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]
            g = google_flights_url(c.route, c.dep, c.ret, cfg.adults, codes,
                                   back=c.back if c.is_cross else None)
            out_air = _ko_air(c.out_leg.get("airline", ""),
                              c.out_leg.get("carrier", ""))
            tag = f" ({out_air}만)" if any(x for x in codes if x) and out_air else ""
            # 네이버 값으로 알림이 나갔는데 구글 링크를 앞세우면, 눌러도 그
            # 가격이 없다. **가격 출처를 먼저** 보여준다 (v1.71).
            from_naver = "naver" in {c.out_leg.get("source"),
                                     c.ret_leg.get("source")}
            g_link = f'<a href="{g}">구글에서 보기{tag}</a>'
            if c.is_cross:
                lines.append(g_link)   # 교차 조합은 네이버 다구간 URL이 없다
            else:
                nv = naver_url(c.route, c.dep, c.ret, cfg.adults)
                n_link = f'<a href="{nv}">네이버에서 보기</a>'
                lines.append(f"{n_link} · {g_link}" if from_naver
                             else f"{g_link} · {n_link}")

        if near or any("↳" in x for x in lines):
            lines.append("")
            foot = []
            if near:
                foot.append("· 로 시작하는 줄은 알림 조건은 아니지만 값이 비슷한 날짜")
            if any("↳" in x for x in lines):
                foot.append("↳ 는 같은 편의 다른 사이트 가격")
            lines.append(" · ".join(foot) + "예요" if len(foot) == 1
                         else " / ".join(foot))

        messages.append((best_price(top[0]), "\n".join(lines)))

    # 노선 간에도 싼 순서로: 가장 저렴한 노선의 메시지가 먼저 간다
    messages.sort(key=lambda m: m[0])
    return [m for _, m in messages]


def format_board(cfg: Settings, combos: list, stamp: str,
                 today: "dt.date | None" = None,
                 month: int | None = None) -> str:
    """고정판용 압축 요약 — 반드시 한 통(4096자)에 들어가야 한다.

    수정(editMessageText)으로 갱신하는 구조라 여러 통으로 나눌 수 없다.
    도시별 최저 1건만 링크를 걸고(URL 하나가 180자쯤 된다) 나머지 날짜는
    링크 없이 25자짜리 텍스트로 붙인다. 그래서 날짜는 많이 실을 수 있다.
    **개수를 고정하지 않고 한 통에 들어가는 만큼 자동으로 줄인다** (v1.48) —
    노선이 늘어도 길이 초과로 발송이 실패하지 않는다.
    """
    from .engine import _seoul_group, city_label
    today = today or (dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(hours=9)).date()
    n = max(cfg.adults, 1)

    if month:
        combos = [c for c in (combos or []) if c.dep.month == month]
    by_city: dict = {}
    for c in combos or []:
        by_city.setdefault(_seoul_group(cfg, c.route), []).append(c)
    title = ("📌 <b>항공권 최저가</b>"
             + (f" · <b>{month}월 출발</b>" if month else "")
             + f" · {stamp} 기준")
    if not by_city:
        miss = (f"{month}월 출발 조합이 아직 없습니다." if month
                else "아직 비교할 조합이 없습니다.")
        return f"{title}\n\n{miss}"

    # 도시별 후보 정리 (같은 출발일·같은 가격이면 박 수가 긴 쪽만)
    ranked: list[list] = []
    for city_combos in sorted(by_city.values(), key=lambda v: min(x.price for x in v)):
        pick: dict = {}
        for c in city_combos:
            k = (c.dep, c.price)
            if k not in pick or c.nights > pick[k].nights:
                pick[k] = c
        ranked.append(sorted(pick.values(), key=lambda c: (c.price, c.dep)))

    def build(per_city: int) -> str:
        lines = [title]
        for picked in ranked:
            sel = picked[:per_city]
            top = sel[0]
            codes = [top.out_leg.get("carrier", ""), top.ret_leg.get("carrier", "")]
            url = google_flights_url(top.route, top.dep, top.ret, cfg.adults, codes,
                                     back=top.back if top.is_cross else None)
            lines.append("")
            lines.append(
                f'<b>{city_label(cfg, top.route)} {round(top.price / n):,}원</b>/인 · '
                f'<a href="{url}">{_d(top.dep)}~{_d(top.ret)}</a> {top.nights}박'
                f'{_airport_note(top)}')
            lines.append(f'   {_leg_time(top.out_leg)}/{_leg_time(top.ret_leg)} '
                         f'{_airlines(top)}')
            for c in sel[1:]:
                lines.append(
                    f'   {_d(c.dep)}~{_d(c.ret)} {c.nights}박 '
                    f'{_leg_time(c.out_leg)}/{_leg_time(c.ret_leg)} '
                    f'{_airlines(c)} · {round(c.price / n):,}원')
        lines.append("")
        lines.append(f"성인 {cfg.adults}명 · 편도 2장 합산 · ⚠는 선호 시간대 밖")
        # 위 시각이 갱신됐다면 그 실행에서 명령도 확인된 것이다.
        # 기능이 있는 줄 모르면 안 쓰게 되므로 여기에 안내를 남긴다.
        lines.append("💬 <b>/help</b> 를 보내면 쓸 수 있는 명령을 알려드려요")
        return "\n".join(lines)

    for per_city in range(cfg.board_top_n, 0, -1):
        text = build(per_city)
        if len(text) <= _BOARD_SAFE_LEN:
            return text
    return build(1)[:_BOARD_SAFE_LEN]


_BOARD_SAFE_LEN = 3900   # 4096 제한에 여유를 둔다


TELEGRAM_LIMIT = 4096
_SAFE_LEN = 3600      # 여유를 두고 자른다 (링크 URL이 하나에 180자쯤 된다)


def format_digest(cfg: Settings, combos: list, subtitle: str = "",
                  today: "dt.date | None" = None,
                  month: int | None = None) -> list[str]:
    """도시별 '지금 최저가' 한 줄 요약 (v1.45).

    왜 필요한가: 한 번 알린 조합은 더 싸지기 전엔 다시 알리지 않는다. 조용한
    날에 "지금 뭐가 제일 싸지?"를 확인할 방법이 없었다. 예전 한 바퀴 완료
    메시지는 기준가 숫자만 나열해 날짜도 링크도 없어 쓸 수가 없었다.
    → 도시마다 실제 최저 조합을 날짜·시각·항공사·링크까지 한 줄로 싣는다.
    """
    from .engine import _seoul_group, city_label
    today = today or (dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(hours=9)).date()
    n = max(cfg.adults, 1)

    if month:
        combos = [c for c in (combos or []) if c.dep.month == month]
    by_city: dict = {}
    for c in combos or []:
        by_city.setdefault(_seoul_group(cfg, c.route), []).append(c)

    head = [f"🔄 <b>지금 최저가</b>" + (f" · {month}월 출발" if month else "")]
    if subtitle:
        head.append(subtitle)
    if not by_city:
        miss = (f"{month}월 출발 조합이 아직 없습니다." if month
                else "아직 비교할 조합이 없습니다.")
        return ["\n".join(head + ["", miss])]

    # 도시를 제목으로 올리고 날짜를 밑에 붙인다. 도시별 1개만 보여주면 "어느
    # 도시가 싼지"는 알아도 "언제 가야 싼지"를 모른다. 반대로 도시명을 매 줄
    # 반복하면 지저분해진다 (v1.46).
    blocks: list[str] = []
    for city_combos in sorted(by_city.values(), key=lambda v: min(x.price for x in v)):
        # 같은 출발일·같은 가격이면 박 수가 긴 쪽만
        pick: dict = {}
        for c in city_combos:
            k = (c.dep, c.price)
            if k not in pick or c.nights > pick[k].nights:
                pick[k] = c
        picked = sorted(pick.values(), key=lambda c: (c.price, c.dep))[: cfg.digest_top_n]
        top = picked[0]
        rows = [f"<b>{city_label(cfg, top.route)} "
                f"{round(top.price / n):,}원</b>/인부터"]
        for c in picked:
            codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]
            url = google_flights_url(c.route, c.dep, c.ret, cfg.adults, codes,
                                     back=c.back if c.is_cross else None)
            rows.append(
                f'· <a href="{url}">{_d(c.dep)}~{_d(c.ret)}</a> {c.nights}박'
                f'{_airport_note(c)} · {_leg_time(c.out_leg)}/{_leg_time(c.ret_leg)} '
                f'{_airlines(c)} · {round(c.price / n):,}원')
        blocks.append("\n".join(rows))

    # 텔레그램 한 통은 4096자 제한이다. 도시 블록 단위로 나눠 담는다
    # (실측 8개 도시 × 3날짜 = 6,400자로 한 통에 안 들어갔다, v1.46).
    foot = f"성인 {cfg.adults}명 · 편도 2장 합산 기준 · ⚠는 선호 시간대 밖"
    msgs: list[str] = []
    cur: list[str] = list(head)
    for b in blocks:
        candidate = cur + ["", b]
        if len("\n".join(candidate)) + len(foot) + 2 > _SAFE_LEN and len(cur) > len(head):
            msgs.append("\n".join(cur))
            cur = [f"🔄 <b>지금 최저가</b> (이어서 {len(msgs) + 1})", "", b]
        else:
            cur = candidate
    cur += ["", foot]
    msgs.append("\n".join(cur))
    return msgs


def _targets() -> tuple[str, list[str]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    raw = os.environ.get("TELEGRAM_CHAT_ID") or ""
    return token, [c.strip() for c in raw.split(",") if c.strip()]


def _post(token: str, method: str, payload: dict):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=payload, timeout=15)
        if r.status_code != 200:
            log.info("telegram %s %s: %s", method, r.status_code, r.text[:180])
            return None
        return r.json().get("result")
    except requests.RequestException as e:
        log.error("telegram %s failed: %s", method, e)
        return None


def send(text: str) -> bool:
    """TELEGRAM_CHAT_ID의 모든 대상에게 전송. 하나라도 성공하면 True.

    여러 명에게 보내려면 Secret에 콤마로 나열한다:
        123456789,-1001234567890
    그룹은 chat_id가 음수다. 그룹 방에 봇을 초대한 뒤
    https://api.telegram.org/bot<TOKEN>/getUpdates 로 확인할 수 있다.
    """
    token, targets = _targets()
    if not token or not targets:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")
        return False
    ok = False
    for chat_id in targets:
        if _post(token, "sendMessage", {
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True}) is not None:
            ok = True
    return ok


def poll_commands(offset: int) -> tuple[list, int]:
    """지난 실행 이후 방에 들어온 봇 명령을 읽는다 (v1.52).

    텔레그램 명령을 받으려면 봇이 항상 켜져 있어야 하는데, 무료 구성에는
    상시 서버가 없다. 대신 **실행할 때마다 밀린 메시지를 한 번 훑는다.**
    즉시 응답은 아니고 다음 실행까지(최대 1시간쯤) 기다려야 한다.

    허용된 chat_id에서 온 것만 받는다. 아무나 봇을 부려서 조회를 돌리게
    두면 안 되기 때문.

    Returns: ([(chat_id, 명령어, 인자)], 다음 offset)
    """
    token, targets = _targets()
    if not token or not targets:
        return [], offset
    allowed = set(targets)
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": 0, "limit": 20},
                         timeout=15)
        if r.status_code != 200:
            log.info("getUpdates %s: %s", r.status_code, r.text[:150])
            return [], offset
        updates = r.json().get("result") or []
    except (requests.RequestException, ValueError) as e:
        log.info("getUpdates 실패: %s", str(e)[:120])
        return [], offset

    cmds = []
    nxt = offset
    for u in updates:
        nxt = max(nxt, int(u.get("update_id", 0)) + 1)
        msg = u.get("message") or u.get("channel_post") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/") or chat_id not in allowed:
            continue
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        cmds.append((chat_id, cmd, arg))
    return cmds, nxt


def parse_month(cmd: str, arg: str = "") -> int | None:
    """'/8월', '/8', '/digest 8', '/digest 8월' 에서 월을 뽑는다."""
    import re as _re
    for s in (arg, cmd):
        m = _re.fullmatch(r"(\d{1,2})\s*월?", (s or "").strip())
        if m and 1 <= int(m.group(1)) <= 12:
            return int(m.group(1))
    return None


def upsert_board(text: str, ids: dict) -> dict:
    """항상 최신 시세를 담는 '고정판' 한 통을 만들거나 갱신한다.

    왜 수정인가: 시세 확인은 푸시가 잘못된 도구다. 내가 원하는 시점이 아니라
    시스템이 정한 시점에 오고, 하루만 지나도 낡는다. 텔레그램은 봇이 자기
    메시지를 나중에 수정할 수 있고 **수정은 알림이 울리지 않는다.**
    메시지를 수정해도 고정(핀)은 유지된다.

    내용이 그대로면 API를 아예 호출하지 않는다. 비교는 전문이 아니라 **해시**로
    한다 — 전문을 meta.json에 넣으면 실행마다 4KB가 커밋에 쌓인다 (v1.49).

    ids: {chat_id: message_id, "chat_id:h": 내용해시}. 갱신된 사전을 돌려준다.
    """
    import hashlib

    token, targets = _targets()
    if not token or not targets:
        return ids
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    out = {k: v for k, v in (ids or {}).items() if not str(k).endswith(":text")}

    for chat_id in targets:
        mid = out.get(chat_id)
        if mid and out.get(f"{chat_id}:h") == digest:
            continue                      # 내용 동일 → 호출 생략
        if mid:
            r = _post(token, "editMessageText", {
                "chat_id": chat_id, "message_id": mid, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True})
            if r is not None:
                out[f"{chat_id}:h"] = digest
                continue
            log.info("고정판 수정 실패(chat %s) → 새로 발송", chat_id)
        r = _post(token, "sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True})
        if r and r.get("message_id"):
            out[chat_id] = r["message_id"]
            out[f"{chat_id}:h"] = digest
    return out
