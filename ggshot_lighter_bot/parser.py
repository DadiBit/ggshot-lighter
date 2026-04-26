from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Signal:
    raw_text: str
    symbol: str
    side: str  # "long" | "short"
    entry_low: float
    entry_high: float
    stop_loss: float
    tps: dict[int, float]  # 1..4
    accuracy_pct: float | None


_RE_SYMBOL = re.compile(r"#([A-Z0-9_]{2,30})", re.IGNORECASE)
_RE_LONGSHORT = re.compile(r"\b(long|short)\b", re.IGNORECASE)

# Entry range: "2.888-2.707", "42100-41500"
_RE_RANGE = re.compile(
    r"(?:(?:Entry\s*Zone|Entry|Range)\s*[:\-]?\s*)?([0-9]+(?:\.[0-9]+)?)\s*[–—-]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

# Targets: "Target 1: 2.954", "TP 3 - 54k", "TP1 - 46k"
_RE_TP = re.compile(
    r"\b(?:target|tp)\s*([1-4])\b\s*(?:[:\-–—]\s*|\s+)\s*([0-9]+(?:\.[0-9]+)?)\s*([kK])?\b",
    re.IGNORECASE,
)

_RE_SL = re.compile(
    r"\b(?:SL|Stop\s*[- ]?\s*Loss|Stop-Loss)\b\s*(?:[:\-–—]\s*|\s+)\s*([0-9]+(?:\.[0-9]+)?)\s*([kK])?\b",
    re.IGNORECASE,
)

_RE_ACCURACY = re.compile(r"Strategy\s*Accuracy\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE)


def _parse_num(num_s: str, k: str | None) -> float:
    v = float(num_s)
    if k:
        # user-requested rule: "k" means multiply by 10000
        v *= 10_000.0
    return v


def parse_signal(text: str) -> Signal | None:
    raw = text.strip()
    if not raw:
        return None

    sym_m = _RE_SYMBOL.search(raw)
    side_m = _RE_LONGSHORT.search(raw)
    range_m = _RE_RANGE.search(raw)
    sl_m = _RE_SL.search(raw)
    tps = {int(m.group(1)): _parse_num(m.group(2), m.group(3)) for m in _RE_TP.finditer(raw)}

    if not sym_m or not side_m or not range_m or not sl_m or not tps:
        return None

    symbol = sym_m.group(1).upper()
    side = side_m.group(1).lower()

    a = float(range_m.group(1))
    b = float(range_m.group(2))
    entry_low, entry_high = (a, b) if a < b else (b, a)

    stop_loss = _parse_num(sl_m.group(1), sl_m.group(2))

    acc_m = _RE_ACCURACY.search(raw)
    accuracy = float(acc_m.group(1)) if acc_m else None

    return Signal(
        raw_text=raw,
        symbol=symbol,
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        tps=tps,
        accuracy_pct=accuracy,
    )


def pick_tp3_tp4(tps: dict[int, float]) -> tuple[float, float]:
    """
    75% at TP3, 25% at TP4.
    Fallback: missing TP4 -> TP3 -> TP2 -> TP1 (and same chain for TP3).
    """

    def pick(level: int) -> float:
        for k in (level, level - 1, level - 2, level - 3):
            if k in tps:
                return tps[k]
        raise ValueError("No TP levels available")

    tp3 = pick(3)
    tp4 = pick(4)
    return tp3, tp4

