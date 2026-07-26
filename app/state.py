"""저장소 내 JSON 상태 관리.

data/
  legs.json         편도(다리) 최신 가격: "ICN-KIX|out|2026-09-12" -> {...}
  baselines.json    노선×월 기준가: "ICN-KIX|2026-09" -> {...}
  alerts_sent.json  중복 억제: 콤보키 -> {price, at}
  meta.json         first_run, 실패 통계 등
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .settings import ROOT

DATA = ROOT / "data"

LEG_HISTORY_DAYS = 30


def _load(name: str) -> dict:
    p = DATA / name
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _save(name: str, obj: dict) -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )


class State:
    def __init__(self) -> None:
        self.legs: dict = _load("legs.json")
        self.baselines: dict = _load("baselines.json")
        self.alerts_sent: dict = _load("alerts_sent.json")
        self.meta: dict = _load("meta.json")
        # 노선·방향별 직항 출발 시각 분포 (시간창 판단 근거, v1.36)
        self.time_hist: dict = _load("time_hist.json")

    # ---------- legs ----------
    @staticmethod
    def leg_key(route_key: str, direction: str, date: str) -> str:
        return f"{route_key}|{direction}|{date}"

    def record_leg(self, key: str, *, price: int | None, airline: str = "",
                   dep_time: str = "", arr_time: str = "", carrier: str = "",
                   off_window: bool = False,
                   now: dt.datetime | None = None) -> None:
        """price=None 이면 '조건 만족 편 없음'으로 기록."""
        now = now or dt.datetime.now(dt.timezone.utc)
        today = now.date().isoformat()
        entry = self.legs.get(key, {"history": {}})
        entry.update({
            "price": price, "airline": airline, "carrier": carrier,
            "off_window": off_window,
            "dep_time": dep_time, "arr_time": arr_time,
            "checked_at": now.isoformat(timespec="seconds"),
        })
        if price is not None:
            hist = entry.setdefault("history", {})
            prev = hist.get(today)
            hist[today] = price if prev is None else min(prev, price)
            cutoff = (now.date() - dt.timedelta(days=LEG_HISTORY_DAYS)).isoformat()
            entry["history"] = {d: p for d, p in hist.items() if d >= cutoff}
        self.legs[key] = entry

    def fresh_leg_price(self, key: str, max_age_days: int) -> dict | None:
        e = self.legs.get(key)
        if not e or e.get("price") is None:
            return None
        checked = dt.datetime.fromisoformat(e["checked_at"])
        age = dt.datetime.now(dt.timezone.utc) - checked
        if age > dt.timedelta(days=max_age_days):
            return None
        return e

    def prune_past_legs(self, today: dt.date) -> None:
        self.legs = {k: v for k, v in self.legs.items()
                     if k.split("|")[2] >= today.isoformat()}

    # ---------- meta ----------
    def first_run_date(self, today: dt.date) -> dt.date:
        if "first_run" not in self.meta:
            self.meta["first_run"] = today.isoformat()
        return dt.date.fromisoformat(self.meta["first_run"])

    def board_ids(self) -> dict:
        return self.meta.setdefault("board_ids", {})

    def merge_time_hist(self, hist: dict) -> None:
        """실행별 시간 분포를 data/time_hist.json 에 누적한다.

        왜 파일로 남기나: 이 값은 원래 로그에만 있었는데, 로그를 옮겨오는
        과정이 번번이 실패했다. 저장소에 파일로 두면 언제든 바로 확인할 수 있고
        여러 실행이 쌓여 표본도 커진다. 초기화하려면 파일을 지우면 된다.

        hist: {(origin, dest): {hour: count}}
        """
        cur = self.time_hist
        for (o, d), hours in hist.items():
            slot = cur.setdefault(f"{o}-{d}", {})
            for h, c in hours.items():
                slot[str(h)] = slot.get(str(h), 0) + c
        cur["_runs"] = cur.get("_runs", 0) + 1
        cur["_updated"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")

    def record_run_stats(self, *, attempted: int, failed: int) -> None:
        runs = self.meta.setdefault("recent_runs", [])
        runs.append({
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "attempted": attempted, "failed": failed,
        })
        self.meta["recent_runs"] = runs[-10:]

    def save(self) -> None:
        _save("legs.json", self.legs)
        _save("baselines.json", self.baselines)
        _save("alerts_sent.json", self.alerts_sent)
        _save("meta.json", self.meta)
        _save("time_hist.json", self.time_hist)
