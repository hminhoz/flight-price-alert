"""텔레그램 알림 전송 + 메시지 포매팅."""
from __future__ import annotations

import logging
import os
import datetime as dt
from collections import defaultdict

import requests

from .engine import Alert
from .links import (google_flights_url, google_oneway_url,
                    google_roundtrip_url, naver_url, naver_leg_url)
from .settings import Settings

log = logging.getLogger(__name__)

_WEEKDAY = "월화수목금토일"


def _d(date) -> str:
    return f"{date.month}/{date.day}({_WEEKDAY[date.weekday()]})"




_AIRPORT_KO = {"ICN": "인천", "GMP": "김포", "HND": "하네다", "NRT": "나리타",
               "KIX": "간사이", "NGO": "나고야", "CJU": "제주", "CTS": "삿포로",
               "FUK": "후쿠오카", "OKA": "오키나와", "KOJ": "가고시마",
               "HKG": "홍콩", "MFM": "마카오", "HAN": "하노이", "DAD": "다낭",
               "SGN": "호치민", "BKK": "수완나품", "DMK": "돈므앙"}


def _ko(code: str) -> str:
    return _AIRPORT_KO.get(code, code)


def _airport_note(c) -> str:
    """어느 공항에서 뜨고 어디로 돌아오는지. 공항을 헷갈리면 비행기를 놓친다."""
    b = c.back
    if not c.is_cross:
        return f" · {_ko(c.route.origin)} 왕복"
    dest_note = ("" if b.destination == c.route.destination
                 else f" ({_ko(c.route.destination)} 입 / {_ko(b.destination)} 출)")
    if b.origin == c.route.origin:
        # 서울 쪽은 같고 현지 공항만 다른 교차 (방콕 수완나품↔돈므앙, v2.45).
        # "인천 출발 / 인천 귀국"은 같은 말을 두 번 하는 것이라 왕복으로 적는다.
        return f" · {_ko(c.route.origin)} 왕복{dest_note}"
    return f" · ⇄ {_ko(c.route.origin)} 출발 / {_ko(b.origin)} 귀국{dest_note}"


def _sources(c) -> tuple:
    """(가는 편 출처, 오는 편 출처). 'naver' 또는 'google'."""
    return (c.out_leg.get("source") or "google",
            c.ret_leg.get("source") or "google")




def _leg_time(leg: dict, mark_src: bool = False) -> str:
    """출발 시각. 선호 시간대 밖이면 ⚠.

    출처는 **두 편이 서로 다를 때만** 시각 옆에 붙인다. 같으면 항목 끝에
    한 번만 적는 게 짧고 읽기 쉽다 — `06:00(네이버)/21:15(네이버)`는
    같은 말을 두 번 하는 셈이었다 (v1.91).
    """
    # 출처는 여기 쓰지 않는다. 줄 끝 링크가 `가는편 네이버 / 오는편 구글`로
    # 어느 편을 어디서 사는지 알려주므로 시각 옆 표기는 중복이다 (v2.05).
    # ⚠는 시각 **앞**에 붙인다 (v2.44). 뒤에 붙이면 `16:20⚠/19:05`가 되는데,
    # 텔레그램은 이모지를 단어 경계로 보아 그 뒤의 `/19`를 **봇 명령어로
    # 자동 인식**한다 — 파랗게 되고 누르면 채팅창에 입력된다(실사용 보고,
    # 가고시마 16:20⚠/19:05). 앞에 붙이면 슬래시 앞뒤가 항상 숫자라 안전하다.
    mark = "⚠" if leg.get("off_window") else ""
    return f"{mark}{leg.get('dep_time', '?')}"


def entry_lines(cfg, c, *, pay: int | None = None, airport: bool = True,
                rt_cheaper: bool | None = None, note: str = "") -> list[str]:
    """**알림·고정판·digest가 공유하는 항목 두 줄** (v2.25).

        123,400  [8/8(토)]~[8/12(수)] 4박 · 인천 왕복
                 07:30/18:00 제주항공 · 카드조건 · 네이버(오는편)

    1줄 = 가격 · 날짜(링크) · 박수 · 공항
    2줄 = 시각 · 항공사 · 부가(카드조건/네이버)
    날짜가 곧 링크다 — 왕복이면 범위 하나, 편도면 각 날짜가 그 편 검색.
    """
    n = max(cfg.adults, 1)
    price = pay if pay is not None else c.pay
    if rt_cheaper is None:
        rt_cheaper = bool(c.rt_price and c.rt_price < c.price)
    ow = cfg.window_for(c.route.key, "out")
    rw = cfg.window_for(c.route.key, "ret")

    when = _date_links(cfg, c, rt_cheaper)

    head = (f"<b>{round(price / n):,}</b>  {when} {c.nights}박"
            + (_airport_note(c) if airport else ""))
    body = (f"    {_leg_time(c.out_leg)}/{_leg_time(c.ret_leg)} "
            f"{_airlines(c)}{_cond(c)}{_buy_note(cfg, c)}{note}")
    return [head, body]


def _date_links(cfg, c, rt_cheaper: bool) -> str:
    """**날짜 = 그 편을 사는 곳으로 가는 링크.** 이 한 문장이 규칙 전부다.

    예전엔 날짜는 늘 구글로 보내고, 값이 네이버에서 온 경우에만 줄 끝에
    `네이버(가는편)` 같은 꼬리표를 따로 달았다. 그러면 "날짜가 링크"라는
    규칙과 어긋나고(눌러도 그 가격이 없다), 꼬리표가 무슨 뜻인지도 알 수 없다.
    이제 링크 자체가 예약처를 가리키므로 어느 편인지 적을 이유가 없다.
    """
    ow = cfg.window_for(c.route.key, "out")
    rw = cfg.window_for(c.route.key, "ret")
    so, sr = _sources(c)

    def rng(u: str) -> str:
        return f'<a href="{u}">{_d(c.dep)}~{_d(c.ret)}</a>'

    if c.is_cross:      # 교차 조합은 다구간 검색 하나로
        codes = [c.out_leg.get("carrier", ""), c.ret_leg.get("carrier", "")]
        return rng(google_flights_url(c.route, c.dep, c.ret, cfg.adults,
                                      codes, back=c.back))
    if so == sr == "naver":
        return rng(naver_url(c.route, c.dep, c.ret, cfg.adults))
    if rt_cheaper and so == sr == "google":
        return rng(google_roundtrip_url(c.route, c.dep, c.ret, cfg.adults,
                                        out_window=ow, ret_window=rw))
    # 편도 2장 — 편마다 파는 곳이 다를 수 있으므로 날짜마다 따로 건다
    def one(src, o, d, day, win, leg) -> str:
        if src == "naver":
            # 그 편이 앞 구간인 왕복 검색으로 (편도 URL은 안 된다, v1.84)
            return naver_leg_url(c.route, c.dep, c.ret, cfg.adults, leg)
        return google_oneway_url(o, d, day, cfg.adults, window=win)
    u1 = one(so, c.route.origin, c.route.destination, c.dep, ow, "out")
    u2 = one(sr, c.route.destination, c.route.origin, c.ret, rw, "ret")
    # 구분자가 곧 발권 형태다: `~`는 왕복권 1장, `+`는 편도 2장 (v2.43).
    # HTML로는 링크 1개/2개가 다르지만 **텔레그램 화면에선 밑줄이 이어져
    # 보여 구분이 안 된다**(실사용 보고). 티켓을 더하듯 +로 잇는다.
    return f'<a href="{u1}">{_d(c.dep)}</a> + <a href="{u2}">{_d(c.ret)}</a>'


def _buy_note(cfg, c) -> str:
    """구글이 아닌 곳에서 사야 할 때만.

    혼합 발권이면 **`네이버`·`구글` 글자 자체가 각자의 예약처 링크다** (v2.40).
    전엔 글자가 안 눌려서, 날짜 두 개 중 어느 쪽이 네이버인지 이 줄만 보고는
    알 수 없었다 — "따로 발권"이라고 말만 하고 어디서를 안 알려준 셈.
    날짜 링크와 같은 URL을 쓰므로 새 규칙이 생기는 게 아니라
    같은 링크가 글자에도 붙는 것뿐이다.
    """
    if c.is_cross:
        return ""
    so, sr = _sources(c)
    if so == sr == "naver":
        return " · 네이버"
    if "naver" in (so, sr):
        # **네이버 글자는 네이버가 파는 그 편이 앞 구간인 검색으로** 보낸다.
        # 네이버는 편도 URL이 안 되므로(실측 0행, v1.84) 왕복 검색의 앞 구간을
        # 이용한다. 그냥 왕복으로 보내면 오는 편이 네이버여도 가는 편부터
        # 보여서 "둘 다 가는 편만 나온다"가 된다 (v2.42 버그 수정).
        nv = naver_leg_url(c.route, c.dep, c.ret, cfg.adults,
                           "out" if so == "naver" else "ret")
        if so == "naver":       # 가는 편이 네이버 → 오는 편이 구글
            gg = google_oneway_url(c.route.destination, c.route.origin, c.ret,
                                   cfg.adults,
                                   window=cfg.window_for(c.route.key, "ret"))
        else:                    # 오는 편이 네이버 → 가는 편이 구글
            gg = google_oneway_url(c.route.origin, c.route.destination, c.dep,
                                   cfg.adults,
                                   window=cfg.window_for(c.route.key, "out"))
        return (f' · <a href="{nv}">네이버</a>·<a href="{gg}">구글</a>'
                f" 따로 발권")
    return ""


# ============================================================ 화면 조립 (공용)
#
# 알림 · 고정판 · 전체시세는 **같은 부품으로 조립된다** (v2.26).
#
#   제목 1줄   {아이콘} <b>{이름}</b> · {범위} · {i/총}
#   도시 블록  도시 줄 + 항목 2줄(entry_lines) × N
#   꼬리 1줄   해당 메시지에 실제로 등장한 기호만 설명
#
# 화면마다 다른 것은 **아이콘·이름·도시당 개수** 셋뿐이다. 나머지를 각자
# 들고 있으면 한쪽만 고쳐져 어긋난다 — 실제로 꼬리에 "편도 2장 합산 기준"이
# 남아 있었다(이제 편도·왕복 중 싼 값을 쓰는데도).

TELEGRAM_LIMIT = 4096     # 텔레그램 한 통 한도
_SAFE_LEN = 3900          # 조립 시 여유를 둔 상한


def pick_dates(combos: list, top_n: int) -> list:
    """도시 안에서 보여줄 날짜를 고른다. **세 화면이 같은 규칙을 쓴다.**

    같은 출발일·같은 가격이면 박 수가 긴 쪽만 남긴다 (3박·4박이 같은 값이라
    두 줄씩 뜨던 문제). 그다음 싼 순 → 이른 날짜 순.

    기준은 **화면에 찍히는 값(`c.pay` = 편도합산·왕복실가 중 싼 쪽)**이다.
    편도합산으로 정렬하면 왕복이 싼 항목이 엉뚱한 자리에 끼어 세로로 읽을 때
    243,000 → 235,000 → 260,000처럼 보인다. 알림은 v2.18에 고쳤는데
    고정판·전체시세는 그대로였다.
    """
    pick: dict = {}
    for c in combos:
        k = (c.dep, c.pay)
        if k not in pick or c.nights > pick[k].nights:
            pick[k] = c
    return sorted(pick.values(), key=lambda c: (c.pay, c.dep))[:top_n]


def city_block(cfg, picked: list, label: str) -> str:
    """도시 줄 + 항목들. 공항이 블록 안에서 모두 같으면 도시 줄로 올린다."""
    notes = {_airport_note(c) for c in picked}
    head_air = notes.pop() if len(notes) == 1 else ""
    rows = [f"<b>{label}</b>{head_air}"]
    for c in picked:
        rows += entry_lines(cfg, c, airport=not head_air)
    return "\n".join(rows)


def _foot(cfg, body: str) -> str:
    """꼬리는 **이 메시지에 실제로 나온 것만** 설명한다.

    안 쓴 기호를 매번 나열하면 그게 소음이다.
    """
    parts = [f"성인 {cfg.adults}명 · 1인 기준"]
    if "⚠" in body:
        parts.append("⚠는 선호 시간대 밖")
    if "<a href" in body:
        parts.append("날짜를 누르면 예약처로")
    # ~와 +가 같이 나온 메시지에만 구분을 설명한다 (한쪽뿐이면 비교 대상이
    # 없어 설명이 소음이다)
    import re as _re
    has_plus = bool(_re.search(r"</a> \+ <a", body))
    has_rng = bool(_re.search(r">[^<]*~[^<]*</a>", body))
    if has_plus and has_rng:
        parts.append("~는 왕복권 1장, +는 편도 2장")
    return " · ".join(parts)


def pack(cfg, title: str, blocks: list[str], lead: str = "") -> list[str]:
    """도시 블록들을 텔레그램 한 통 한도에 맞게 나눈다. **분할 규칙도 공용.**

    통이 여러 개면 **모든 통의 제목에** `i/총`을 붙인다 (첫 통만 번호가 없어
    "1/3은 왜 없지?"가 됐던 적이 있다). 번호가 이어짐을 말해주므로
    "아래로 계속" 같은 안내 줄은 두지 않는다.
    """
    msgs: list[list[str]] = []
    cur: list[str] = []
    for b in blocks:
        cand = cur + ["", b] if cur else [b]
        if cur and len("\n".join(cand)) + len(title) + 90 > _SAFE_LEN:
            msgs.append(cur)
            cur = [b]
        else:
            cur = cand
    msgs.append(cur)

    total = len(msgs)
    # 꼬리는 **마지막 통에만**. 번호가 이어짐을 이미 말해주므로 매 통마다
    # 같은 안내를 반복할 이유가 없다. 대신 기호 설명은 전체 내용을 보고 정한다.
    whole = "\n".join("\n".join(r) for r in msgs)
    out = []
    for i, rows in enumerate(msgs, 1):
        body = "\n".join(rows)
        head = title if total == 1 else f"{title} · <b>{i}/{total}</b>"
        # 실행 요약은 **첫 통에만.** 고정해두고 보는 게 1통이라 여기 있어야 한다.
        if lead and i == 1:
            head = f"{head}\n{lead}"
        tail = f"\n\n{_foot(cfg, whole)}" if i == total else ""
        out.append(f"{head}\n\n{body}{tail}")
    return out










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








def _cond(c) -> str:
    """카드 이용실적 등 조건부 가격이면 표시. 눌러보기 전에 알아야 한다."""
    if c.out_leg.get("card_cond") or c.ret_leg.get("card_cond"):
        return " · 카드조건"
    return ""


def _airlines(c) -> str:
    """가는 편·오는 편 항공사. 같으면 한 번만. 이름은 한글로."""
    a = _ko_air(c.out_leg.get("airline", ""), c.out_leg.get("carrier", ""))
    b = _ko_air(c.ret_leg.get("airline", ""), c.ret_leg.get("carrier", ""))
    return a if a == b else f"{a}/{b}"




def format_alerts(cfg: Settings, alerts: list[Alert],
                  all_combos: list | None = None,
                  today: "dt.date | None" = None,
                  used: list | None = None) -> list[str]:
    """노선별로 묶어 메시지 생성. 노선당 1개 메시지, 최저 top N 요약.

    all_combos를 주면 알림 조건 미충족이어도 '최저가 +N% 이내'인
    다른 날짜 조합을 함께 보여준다 (추가 검색 비용 0 — 이미 수집된 데이터).
    """
    today = today or (dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(hours=9)).date()
    # 노선이 아니라 **도시** 단위로 묶는다 (v1.41). 같은 나고야인데 인천발·김포발·
    # 교차 조합이 따로 메시지로 나가면 어느 게 싼지 비교가 안 된다.
    from .engine import _seoul_group, city_label, alert_selection
    by_route = alert_selection(cfg, alerts)   # 선별 규칙은 engine 한 곳에만
    combos_by_route: dict[str, list] = defaultdict(list)
    for c in all_combos or []:
        combos_by_route[_seoul_group(cfg, c.route)].append(c)

    def best_price(a) -> int:
        """실제로 낼 금액 = 편도 2장과 왕복권 중 싼 쪽.

        제목·정렬 모두 이 값이어야 한다. 편도 합산으로 정렬하면 왕복이 더 싼
        항목이 뒤로 밀려 눈에 보이는 숫자가 뒤죽박죽이 된다.
        왕복 실가는 조합(`combo.rt_price`)에 붙는다 — 알림 객체만 보면
        제목이 편도 합산으로 나온다 (v2.18에서 겪음).
        """
        return a.combo.pay if a.combo.rt_price else min(
            x for x in (a.combo.price, a.rt_price) if x)

    messages: list[tuple[int, str]] = []
    for _key, top in by_route.items():
        # 선별은 engine.alert_selection이 이미 pay 기준으로 마쳤다.
        # 여기서는 표시 순서만 알림에 붙은 왕복 실가까지 반영해 다시 맞춘다.
        top = sorted(top, key=best_price)

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
        lines = ["", ""]   # 제목은 표시 항목을 다 정한 뒤 채운다 (head_idx)
        head_idx = 0

        # 근처 날짜 후보 (알림 조건은 아니지만 값이 비슷한 조합)
        shown_deps = {(a.combo.dep, a.combo.nights) for a in top}
        # 고르는 잣대도 **화면에 찍히는 값(pay)**이어야 한다. 편도합산으로
        # 재면 왕복이 싼 조합이 상한에 걸려 빠지거나, 엉뚱하게 끼어든다.
        base = min(a.combo.pay for a in top)
        limit = base * (1 + cfg.similar_margin_pct / 100)
        cands = [c for c in combos_by_route.get(_key, [])
                 if (c.dep, c.nights) not in shown_deps and c.pay <= limit]
        # 추리는 규칙은 세 화면 공용 (pick_dates)
        near = pick_dates(cands, cfg.similar_top_n)
        has_context = bool(near)     # 참고 행이 섞이면 특가 쪽에 표시가 필요하다

        # 주 항목과 근처 날짜를 **하나의 오름차순 스트림**으로 합친다 (v1.38).
        # 예전엔 두 구역을 따로 정렬해, 근처 날짜 상한이 '가장 싼 주 항목 +10%'인
        # 탓에 두 번째 주 항목보다 근처 날짜가 싸게 나왔다. 실측 김포-제주에서
        # 13만 → 16만 → 13만 순으로 보여 정렬이 깨진 것처럼 읽혔다.
        stream = [(best_price(a), 0, a) for a in top]
        stream += [(c.pay, 1, c) for c in near]   # ← price로 두면 순서가 무너진다
        stream.sort(key=lambda x: (x[0], x[1]))

        # 제목의 "N원부터"는 **이 메시지에 실제로 실리는 것 중 최저가**여야 한다.
        # 알림 항목만 보고 정하면, 더 싼 근처 날짜가 바로 아래 있는데도 제목이
        # 비싼 값을 말하는 모순이 생긴다 (v1.44).
        head_price = round(min(x[0] for x in stream) / max(n, 1))
        # 제목엔 도시·금액·공항·왜 싼지만. 잠금화면에서 이 한 줄로 판단한다.
        why = ""
        t0 = top[0]
        if t0.kind == "record" and t0.prev_min:
            # prev_min은 pay 기준으로 저장된 값이다. price와 비교하면 잣대가
            # 섞여 % 가 틀린다.
            why = f" · {(t0.prev_min - best_price(t0)) / t0.prev_min * 100:.0f}% 싸짐"
        elif t0.baseline:
            cut = (t0.baseline - best_price(t0)) / max(t0.baseline, 1) * 100
            if cut >= 1:
                why = f" · 평소보다 {cut:.0f}% 싸짐"
        # 공항 표기는 고정판·전체시세와 **같은 규칙**: 실리는 항목이 모두 같은
        # 공항이면 제목(=도시 줄)에 한 번, 섞이면 항목마다. 예전엔 주 항목은
        # 항상 붙고 '다른 날짜'는 절대 안 붙어 같은 메시지 안에서도 어긋났다.
        notes = {_airport_note(a.combo) for a in top} | {
            _airport_note(c) for c in near}
        head_air = notes.pop() if len(notes) == 1 else ""
        lines[head_idx] = (f"{'🏆' if record else '✈️'} "
                           f"<b>{city_label(cfg, route)} {head_price:,}원</b>/인"
                           f"{head_air}{why}")

        # **한 메시지 = 하나의 오름차순 목록.** 예전엔 알림 조건을 충족한 것과
        # 값이 비슷한 '다른 날짜'를 두 구역으로 나눠 아래에 몰아넣었다. 그래서
        # 정렬을 해놔도 금액이 중간에서 처음부터 다시 시작했다(오사카 실측).
        # 읽는 쪽에 그 구분은 쓸모가 없다 — 어차피 싼 순으로 고르기 때문이다.
        # 구역을 없애니 알림도 고정판·전체시세와 같은 모양이 된다.
        for _price, kind, obj in stream:
            if kind == 1:                      # 값이 비슷한 다른 날짜
                lines += entry_lines(cfg, obj, airport=not head_air)
                continue

            a, c = obj, obj.combo
            if used is not None:
                used.append(a)      # 실제로 표시된 것만 '보냄'으로 기록해야 한다
            one, rt = c.price, (c.rt_price or a.rt_price)
            pay = min(one, rt) if rt else one
            # 지난 알림 대비 하락은 **화살표와 금액만.** 줄을 따로 쓰지 않고
            # 2줄 끝 부가 자리에 붙인다 (카드조건·네이버와 같은 자리).
            # **이번에 조건을 넘은 건지 표시한다.** 한 목록으로 합치면서
            # 어느 게 '새로 뜬 특가'이고 어느 게 참고용 다른 날짜인지 알 수
            # 없어졌다 (v2.27의 과잉 삭제). 다만 목록에 참고 행이 하나도
            # 없으면 전부 특가라 표시가 소음이 되므로 **섞였을 때만** 붙인다.
            note = " · 특가" if has_context else ""
            if a.prev_sent and a.prev_sent > c.pay:
                note += f" · 🔻{round((a.prev_sent - c.pay) / n):,}"
            lines += entry_lines(cfg, c, pay=pay, airport=not head_air, note=note)

        body = "\n".join(lines)
        messages.append((best_price(top[0]),
                         f"{body}\n\n{_foot(cfg, body)}"))

    # 노선 간에도 싼 순서로: 가장 저렴한 노선의 메시지가 먼저 간다
    messages.sort(key=lambda m: m[0])
    return [m for _, m in messages]


def format_board(cfg: Settings, combos: list, stamp: str,
                 today: "dt.date | None" = None,
                 month: int | None = None, status: str = "") -> list[str]:
    """📌 고정판 — 실행마다 조용히 수정되는, 방에 고정해두는 현황판.

    수정 방식이라 알림이 울리지 않는다. 통이 여러 개여도 연달아 붙어 있어
    스크롤로 이어 읽힌다. 조립은 pack()이 하고 여기서는 **무엇을 담을지만**
    정한다.
    """
    return _screen(cfg, combos, month,
                   icon="📌", name="최저가 현황",
                   extra=f"{stamp} 기준", top_n=cfg.board_top_n, status=status)


def format_digest(cfg: Settings, combos: list, subtitle: str = "",
                  today: "dt.date | None" = None,
                  month: int | None = None) -> list[str]:
    """🔄 전체 시세 — 요청했을 때만(`/digest`) 나가는 넓은 조회.

    왜 필요한가: 한 번 알린 조합은 더 싸지기 전엔 다시 알리지 않는다.
    조용한 날에 "지금 뭐가 제일 싸지?"를 확인할 방법이 이것뿐이다.
    고정판보다 도시당 날짜를 더 많이 싣는 것 말고는 차이가 없다.
    """
    return _screen(cfg, combos, month,
                   icon="🔄", name="전체 시세",
                   extra=subtitle, top_n=cfg.digest_top_n)


def _screen(cfg: Settings, combos: list, month: int | None, *,
            icon: str, name: str, extra: str, top_n: int,
            status: str = "") -> list[str]:
    """고정판·전체시세의 **공통 본체**. 다른 건 아이콘·이름·개수뿐이다."""
    from .engine import _seoul_group, city_label

    if month:
        combos = [c for c in (combos or []) if c.dep.month == month]
    by_city: dict = {}
    for c in combos or []:
        by_city.setdefault(_seoul_group(cfg, c.route), []).append(c)

    title = (f"{icon} <b>{name}</b>"
             + (f" · {month}월 출발" if month else "")
             + (f" · {extra}" if extra else ""))
    if not by_city:
        miss = (f"{month}월 출발 조합이 아직 없습니다." if month
                else "아직 비교할 조합이 없습니다.")
        return [f"{title}\n\n{miss}"]

    # 싼 도시부터 (역시 실제 지불액 기준)
    blocks = []
    for city_combos in sorted(by_city.values(),
                              key=lambda v: min(x.pay for x in v)):
        picked = pick_dates(city_combos, top_n)
        blocks.append(city_block(cfg, picked,
                                 city_label(cfg, picked[0].route)))
    return pack(cfg, title, blocks, lead=status)



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
        # **통 수가 바뀌면 전부 지우고 다시 보낸다.**
        # 예전엔 남는 통을 그냥 버렸는데, 버려진 메시지는 대화방에 옛 내용
        # 그대로 남았다. 반대로 통이 늘면 새 통만 뒤늦게 발송돼 1/3은 위쪽에,
        # 2/3·3/3은 한참 아래에 떨어졌다(실측 message_id 112 vs 166·167).
        # "연달아 붙어 있다"는 전제가 그때 깨진다.
        resent = len(mids) != len(texts)
        if resent:
            for mid in mids:
                r = _post(token, "deleteMessage",
                          {"chat_id": chat_id, "message_id": mid})
                if r is None:
                    # 텔레그램은 **보낸 지 48시간 지난 메시지를 봇이 못 지운다.**
                    # 고정판은 몇 주 전에 보낸 걸 계속 수정해 쓰므로 거의 항상
                    # 여기에 걸린다 (v2.45, 6통→12통 때 발견). 수정은 기한이
                    # 없으니 옛 통을 짧은 안내로 바꿔 옛 시세가 남지 않게 한다.
                    _post(token, "editMessageText", {
                        "chat_id": chat_id, "message_id": mid,
                        "text": "(옛 고정판 — 새 고정판은 상단 📌를 누르세요)",
                        "disable_web_page_preview": True})
            mids, hs = [], []
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
            # 고정판은 **무음 발송**. 푸시는 진짜 특가에만 쓴다.
            r = _post(token, "sendMessage", {
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": True})
            if r and r.get("message_id"):
                new_ids.append(r["message_id"]); new_hs.append(dg)
        # 새로 보냈으면 **첫 통을 봇이 직접 고정한다.**
        # 통 수가 바뀔 때마다 사람이 다시 고정하게 둘 수는 없다. 나머지 통은
        # 바로 아래 붙어 있으므로 고정은 1통이면 충분하다.
        if resent and new_ids:
            _post(token, "pinChatMessage",
                  {"chat_id": chat_id, "message_id": new_ids[0],
                   "disable_notification": True})
        out[chat_id], out[f"{chat_id}:h"] = new_ids, new_hs
    return out
