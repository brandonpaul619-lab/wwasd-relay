# scan_endpoint.py — compact read-only TV Desk scan view
# Imports the existing relay app and adds one projection-only endpoint.
# No ingest, storage, Pine payload, or existing route behavior is changed.

from typing import Any, Dict, List, Set

from fastapi.responses import JSONResponse

from app import app, snap, FRESH_CUTOFF_SECS


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _compact_item(it: Dict[str, Any], memberships: Set[str]) -> Dict[str, Any]:
    trend = it.get("trend") or {}
    htf = it.get("htf") or {}
    ltf = it.get("ltf") or {}
    ind = ((ltf.get("comp") or {}).get("ind") or {})
    qv = ind.get("mpvwap") or {}
    ema12 = ind.get("ema12_1d") or {}
    vwap = ind.get("vwap_1d") or {}

    htf_rating = _num(htf.get("rating"))
    ltf_rating = _num(ltf.get("rating"))
    rank = max(htf_rating, ltf_rating)

    out: Dict[str, Any] = {
        "symbol": it.get("symbol"),
        "cmp": it.get("cmp"),
        "fresh": bool(it.get("is_fresh")),
        "lists": sorted(memberships),
        "rank": rank,
        "trend": {
            "sig": trend.get("sig"),
            "rating": trend.get("rating"),
        },
        "htf": {
            "sig": htf.get("sig"),
            "rating": htf.get("rating"),
        },
        "ltf": {
            "sig": ltf.get("sig"),
            "rating": ltf.get("rating"),
            "trigger": ltf.get("trigger"),
        },
        "qvwap": {
            "state": qv.get("state"),
            "reclaim": qv.get("reclaim"),
            "loss": qv.get("loss"),
        },
        "ema12_1d": {
            "above": ema12.get("above"),
            "reclaim": ema12.get("reclaim"),
            "loss": ema12.get("loss"),
        },
    }

    # vwap_1d is optional in the Pine payload; omit it when absent.
    if vwap:
        out["vwap_1d"] = {
            "reclaim": vwap.get("reclaim"),
            "loss": vwap.get("loss"),
        }

    return out


@app.get("/scan")
def scan_view(
    lists: str = "green,macro,full",
    fresh_only: int = 1,
    max_age_secs: int = FRESH_CUTOFF_SECS,
    limit: int = 0,
):
    """Compact, read-only projection of the existing TV snapshot for TV Desk scans."""
    raw = snap(lists=lists, fresh_only=fresh_only, max_age_secs=max_age_secs)
    by_symbol: Dict[str, Dict[str, Any]] = {}
    memberships: Dict[str, Set[str]] = {}

    for list_name, pack in (raw.get("lists") or {}).items():
        for it in (pack or {}).get("items", []) or []:
            sym = str(it.get("symbol") or "").upper()
            if not sym:
                continue
            by_symbol[sym] = it
            memberships.setdefault(sym, set()).add(list_name)

    items: List[Dict[str, Any]] = [
        _compact_item(it, memberships.get(sym, set()))
        for sym, it in by_symbol.items()
    ]

    # Strongest current HTF/LTF rating first; symbol is deterministic tie-breaker.
    items.sort(key=lambda x: (-_num(x.get("rank")), str(x.get("symbol") or "")))

    if limit > 0:
        items = items[:limit]

    return JSONResponse(
        {"ts": raw.get("ts"), "count": len(items), "items": items},
        headers={"Cache-Control": "no-store"},
    )
