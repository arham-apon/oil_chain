"""Post-validation of LLM text (spec §12.2): every number must exist in FACTS and every entity id must exist."""

from __future__ import annotations

import json
import re
from typing import Any

from fsp_shared import world_constants as wc

NUM_RE = re.compile(r"(?<![\w.-])-?\d[\d,]*(?:\.\d+)?%?")
ID_RE = re.compile(r"\b(?:route|depot|station|region)-[a-z]+(?:-[a-z]+)?\b")
KNOWN_IDS = set(wc.ROUTES) | set(wc.DEPOTS) | set(wc.STATIONS) | set(wc.REGIONS)
FREE_NUMBERS = {0.0, 1.0, 2.0, 3.0, 100.0}  # counting words / percent base; never quantities that matter


def _collect(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect(v, out)
    elif isinstance(obj, str):
        for m in NUM_RE.findall(obj):
            try:
                out.add(float(m.rstrip("%").replace(",", "")))
            except ValueError:
                pass


def allowed_numbers(*sources: Any) -> set[float]:
    base: set[float] = set()
    for s in sources:
        _collect(s, base)
    allowed = set(FREE_NUMBERS)
    for v in base:
        allowed |= {v, round(v), round(v, 1), round(v, 2), round(v * 100, 1), round(v * 100), round(v / 100, 3)}
    return allowed


def check(text: str, *sources: Any) -> list[str]:
    """Return a list of violations (empty = grounded)."""
    allowed = allowed_numbers(*sources)
    problems = []
    for tok in NUM_RE.findall(text):
        raw = tok.rstrip("%").replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        candidates = {v, round(v), round(v, 1), round(v, 2)}
        if tok.endswith("%"):
            candidates |= {round(v / 100, 3), round(v / 100, 2), round(v / 100, 1)}
        if not any(c in allowed or any(abs(c - a) <= max(0.0051, 0.005 * abs(a)) for a in allowed) for c in candidates):
            problems.append(f"ungrounded number {tok}")
    known = KNOWN_IDS | {m for m in ID_RE.findall(json.dumps(sources, default=str))}
    for ident in ID_RE.findall(text):
        if ident not in known:
            problems.append(f"unknown entity {ident}")
    return problems


def narrative_text(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    return json.dumps(obj, ensure_ascii=False)
