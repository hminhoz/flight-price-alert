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


def _sources(c) -> tuple:
    """(가는 편 출처, 오는 편 출처). 'naver' 또는 'google'."""
    return (c.out_leg.get("source") or "google",
            c.ret_leg.get("source") or "google")


def _mixed(c) -> bool:
    a, b = _sources(c)
    return a != b


def _leg_time(leg: dict, mark_src: bool = False) -> str:
    """출발 시각. 선호 시간대 밖이면 ⚠.

    출처는 **두 편이 서로 다를 때만** 시각 옆에 붙인다. 같으면 항목 끝에
    한 번만 적는 게 짧고 읽기 쉽다 — `06:00(네이버)/21:15(네이버)`는
    같은 말을 두 번 하는 셈이었다 (v1.91).
    """
    mark = "⚠" if leg.get("off_window") else ""
    src = ""
    if mark_src:
        src = "네이버" if leg.get("source") == "naver" else "구글"
        src = f"({src})"
    return f"{leg.get('dep_time', '?')}{mark}{src}"


def _times(c) -> str:
    """`06:00/21:15` + 출처. 알림·고정판·근처날짜·digest가 같은 규칙을 쓴다.

    고정판과 근처 날짜에는 출처 표시가 아예 빠져 있었다(v1.71에서 알림만
    고치고 나머지를 놓쳤다). 어디서 사야 하는지 모르면 가격만 보여주는 셈이다.
    """
    a, b = _sources(c)
    mx = a != b
    s = f"{_leg_time(c.out_leg, mx)}/{_leg_time(c.ret_leg, mx)}"
    return s + (" 네이버" if a == b == "naver" else "")


def _src_suffix(c) -> str:
    """두 편이 같은 출처일 때 항목 끝에 붙일 표시."""
    a, b = _sources(c)
    return " · 네이버" if a == b == "naver" else ""


def _mixed_note(c) -> str:
    """출처가 섞이면 **각각 다른 곳에서 발권**해야 한다는 걸 반드시 알린다.
    링크만 둘 다 걸어두면 한 곳에서 다 살 수 있다고 오해한다 (v1.91)."""
    if not _mixed(c):
        return ""
    a, b = _sources(c)
    ko = {"naver": "네이버", "google": "구글"}
    return (f"⚠️ 가는 편은 {ko[a]}, 오는 편은 {ko[b]}에서 "
            f"<b>각각 따로</b> 발권해야 해요")


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
        near_lines: list = []
        stream = [(best_price(a), 0, a) for a in top]
        stream += [(c.price, 1, c) for c in near]
        stream.sort(key=lambda x: (x[0], x[1]))

        # 제목의 "N원부터"는 **이 메시지에 실제로 실리는 것 중 최저가**여야 한다.
        # 알림 항목만 보고 정하면, 더 싼 근처 날짜가 바로 아래 있는데도 제목이
        # 비싼 값을 말하는 모순이 생긴다 (v1.44).
        head_price = round(min(x[0] for x in stream) / max(n, 1))
        # 제목엔 도시·금액·왜 싼지만. 잠금화면에서 이 한 줄로 판단한다.
        why = ""
        t0 = top[0]
        if t0.kind == "record" and t0.prev_min:
            why = f" · {(t0.prev_min - t0.combo.price) / t0.prev_min * 100:.0f}% 싸짐"
        elif t0.baseline:
            cut = (t0.baseline - best_price(t0)) / max(t0.baseline, 1) * 100
            if cut >= 1:
                why = f" · 평소보다 {cut:.0f}% 싸짐"
        lines[head_idx] = (f"{'🏆' if record else '✈️'} "
                           f"<b>{city_label(cfg, route)} {head_price:,}원</b>/인{why}")

        for _price, kind, obj in stream:
            if kind == 1:                      # 다른 날짜 — 날짜와 값만
                c = obj
                near_lines.append(
                    f'{_blink(cfg, c)} {c.nights}박 '
                    f'{_times(c)} {_airlines(c)} {round(c.price / n):,}'
                    f'{_sites(cfg, c)}')
                continue

            a, c = obj, obj.combo
            one, rt = c.price, a.rt_price
            pay = min(one, rt) if rt else one
            lines.append("")
            lines.append(f"<b>{_d(c.dep)}~{_d(c.ret)}</b> {c.nights}박"
                         f"{_airport_note(c)} · <b>{round(pay / n):,}원</b>/인"
                         f" · {cfg.adults}명 {pay:,}원")
            # 지난 알림보다 더 내렸으면 그것부터 알린다 — 재알림의 존재 이유다.
            if a.prev_sent and a.prev_sent > c.price:
                lines.append(f"🔻 지난 알림보다 "
                             f"{round((a.prev_sent - c.price) / n):,}원 더 내림")
            if rt and abs(rt - one) / max(one, 1) >= 0.05:
                lines.append("왕복권이 유리" if rt < one else "편도 2장이 유리")

            # 어떤 편인지 + 어디서 사는지를 한 줄로
            a_, b_ = _sources(c)
            where = ("네이버에서 구매" if a_ == b_ == "naver"
                     else "" if a_ == b_ != "naver"
                     else f"가는편 {_ko_src(a_)} / 오는편 {_ko_src(b_)} 따로 구매")
            lines.append(f"{_leg_time(c.out_leg)} → {_leg_time(c.ret_leg)} "
                         f"{_airlines(c)}" + (f" · {where}" if where else ""))

            codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]
            g = google_flights_url(c.route, c.dep, c.ret, cfg.adults, codes,
                                   back=c.back if c.is_cross else None)
            if c.is_cross:
                lines.append(f'<a href="{g}">구글에서 보기</a>')
            else:
                nv = naver_url(c.route, c.dep, c.ret, cfg.adults)
                gl, nl = f'<a href="{g}">구글</a>', f'<a href="{nv}">네이버</a>'
                lines.append(f"{nl} · {gl}" if "naver" in (a_, b_)
                             else f"{gl} · {nl}")

        if near_lines:
            lines.append("")
            lines.append("<b>다른 날짜</b> (1인)")
            for x in near_lines:
                lines.append(f"· {x}")

        messages.append((best_price(top[0]), "\n".join(lines)))

    # 노선 간에도 싼 순서로: 가장 저렴한 노선의 메시지가 먼저 간다
    messages.sort(key=lambda m: m[0])
    return [m for _, m in messages]


def format_board(cfg: Settings, combos: list, stamp: str,
                 today: "dt.date | None" = None,
                 month: int | None = None) -> list[str]:
    """고정판 — 모든 날짜에 링크를 걸기 위해 여러 통으로 나눈다 (v1.98).

    통이 여러 개여도 **연달아 발송돼 대화창에서 붙어 있다** — 상단 띠를 눌러
    첫 통으로 점프한 뒤 스크롤하면 이어서 읽힌다. 띠를 여러 번 누를 필요 없다.
    링크는 **그 가격이 실제로 있는 곳**으로 건다(네이버 값이면 네이버).
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
        return [f"{title}\n\n{miss}"]

    ranked: list[list] = []
    for city_combos in sorted(by_city.values(), key=lambda v: min(x.price for x in v)):
        pick: dict = {}
        for c in city_combos:
            k = (c.dep, c.price)
            if k not in pick or c.nights > pick[k].nights:
                pick[k] = c
        ranked.append(sorted(pick.values(), key=lambda c: (c.price, c.dep)))

    def city_block(picked):
        top = picked[0]
        rows = [f'<b>{city_label(cfg, top.route)} {round(top.price / n):,}원</b>/인 · '
                f'{_blink(cfg, top)} {top.nights}박{_airport_note(top)}'
                f'{_sites(cfg, top)}',
                f'   {_times(top)} {_airlines(top)}']
        for c in picked[1:]:
            rows.append(f'   {_blink(cfg, c)} {c.nights}박 {_times(c)} '
                        f'{_airlines(c)} · {round(c.price / n):,}원'
                        f'{_sites(cfg, c)}')
        return "\n".join(rows)

    # 모든 날짜에 링크를 건다 → 한 통엔 안 들어가므로 도시 단위로 나눠 담는다.
    # 통이 여러 개여도 연달아 발송돼 붙어 있으므로 스크롤로 이어 읽힌다.
    blocks = [city_block(pk[: cfg.board_top_n]) for pk in ranked]
    foot = f"성인 {cfg.adults}명 · ⚠는 선호 시간대 밖 · 날짜를 누르면 예약처로"

    # 통을 나눈 뒤 **모든 통에** 같은 형식으로 번호를 붙인다.
    # 첫 통만 번호가 없어 "1/3은 왜 없지?"가 됐다 (v1.99).
    msgs, cur = [], []
    for b in blocks:
        cand = cur + ["", b] if cur else [b]
        if len("\n".join(cand)) + len(title) + len(foot) + 8 > _BOARD_SAFE_LEN and cur:
            msgs.append("\n".join(cur))
            cur = [b]
        else:
            cur = cand
    msgs.append("\n".join(cur))

    total = len(msgs)
    out = []
    for i, body in enumerate(msgs, 1):
        head = title if total == 1 else f"{title} · <b>{i}/{total}</b>"
        tail = foot if i == total else f"({i}/{total} — 아래로 계속)"
        out.append(f"{head}\n\n{body}\n\n{tail}")
    return out


def _blink(cfg, c) -> str:
    """날짜 → 그 가격이 실제로 있는 곳으로.

    · 둘 다 네이버면 네이버 하나 (주소가 84자로 구글 169자의 절반이다)
    · 둘 다 구글이면 구글 하나
    · **섞이면 둘 다** — 가는 편과 오는 편을 다른 사이트에서 따로 사야 하므로
      한쪽만 걸면 나머지 편 가격이 그곳에 없다 (v2.02).
    """
    a, b = _sources(c)
    label = f"{_d(c.dep)}~{_d(c.ret)}"
    g_codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]

    def g():
        return google_flights_url(c.route, c.dep, c.ret, cfg.adults, g_codes,
                                  back=c.back if c.is_cross else None)

    if c.is_cross:                       # 교차 조합은 네이버 다구간 URL이 없다
        return f'<a href="{g()}">{label}</a>'
    if a == b == "naver":
        return f'<a href="{naver_url(c.route, c.dep, c.ret, cfg.adults)}">{label}</a>'
    if a == b:
        return f'<a href="{g()}">{label}</a>'
    # 혼합: 날짜는 글자로 두고, 사이트 링크는 줄 끝에 붙인다(_sites)
    return label


def _sites(cfg, c) -> str:
    """혼합 조합일 때 줄 끝에 붙일 두 사이트 링크. 아니면 빈 문자열."""
    a, b = _sources(c)
    if a == b or c.is_cross:
        return ""
    nv = naver_url(c.route, c.dep, c.ret, cfg.adults)
    codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]
    g = google_flights_url(c.route, c.dep, c.ret, cfg.adults, codes)
    return f' <a href="{nv}">네이버</a>·<a href="{g}">구글</a>'


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
            rows.append(
                f'· {_blink(cfg, c)} {c.nights}박'
                f'{_airport_note(c)} · {_times(c)} '
                f'{_airlines(c)} · {round(c.price / n):,}원{_sites(cfg, c)}')
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


def upsert_board(texts, ids: dict) -> dict:
    """고정판(여러 통)을 만들거나 갱신한다.

    수정은 알림이 울리지 않고 고정도 유지된다. 내용이 같은 통은 호출도 생략.
    ids: {chat_id: [message_id...], "chat_id:h": [해시...]}
    """
    import hashlib

    if isinstance(texts, str):
        texts = [texts]
    token, targets = _targets()
    if not token or not targets:
        return ids
    digests = [hashlib.sha1(x.encode("utf-8")).hexdigest()[:12] for x in texts]
    out = {k: v for k, v in (ids or {}).items() if not str(k).endswith(":text")}

    for chat_id in targets:
        mids = out.get(chat_id) or []
        mids = [mids] if isinstance(mids, int) else list(mids)
        hs = out.get(f"{chat_id}:h") or []
        hs = [hs] if isinstance(hs, str) else list(hs)
        new_ids, new_hs = [], []
        for i, (text, dg) in enumerate(zip(texts, digests)):
            mid = mids[i] if i < len(mids) else None
            if mid and i < len(hs) and hs[i] == dg:
                new_ids.append(mid); new_hs.append(dg); continue
            if mid:
                r = _post(token, "editMessageText", {
                    "chat_id": chat_id, "message_id": mid, "text": text,
                    "parse_mode": "HTML", "disable_web_page_preview": True})
                if r is not None:
                    new_ids.append(mid); new_hs.append(dg); continue
                log.info("고정판 수정 실패(chat %s, %d번째) → 새로 발송", chat_id, i + 1)
            r = _post(token, "sendMessage", {
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True})
            if r and r.get("message_id"):
                new_ids.append(r["message_id"]); new_hs.append(dg)
        out[chat_id], out[f"{chat_id}:h"] = new_ids, new_hs
    return out
