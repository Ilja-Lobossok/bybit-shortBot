"""Bounded complete-generation Loki projection; IDs only inside JSON bodies."""
import hashlib
import json
from performance.storage import encoded

LIMITS = {"position": 200, "trade": 1000, "strategy": 200}
MAX_BODY_BYTES = 2 * 1024**2


def generation(store, view, now):
    # Monotonic across restarts/corrections; never a metric/index label.
    ident = max(now, int(store.meta("last_generation", 0)) + 1)
    groups = {"position": view["positions"],
              "trade": sorted(view["trades"] + view["unmatched_closes"],
                              key=lambda x: x.get("closed_at_ms") or x.get("source_updated_ms", 0), reverse=True),
              "strategy": [dict(json.loads(x["payload"]), entity_key=x["entity_key"])
                           for x in store.db.execute("SELECT * FROM strategy_current ORDER BY entity_key")]}
    rows, expected, truncated = [], {}, {}
    for kind, values in groups.items():
        truncated[kind] = len(values) > LIMITS[kind]
        selected = values[:LIMITS[kind]]
        expected[kind] = len(selected)
        for row in selected:
            # Narrow allowlist below intentionally excludes financial/API dumps,
            # order links, free text, headers, wallet addresses and credentials.
            allowed = ("entity_key symbol positionIdx side size avgPrice markPrice positionValue unrealisedPnl "
                       "curRealisedPnl takeProfit stopLoss as_of_ms attribution_status opened_at_ms closed_at_ms "
                       "entry exit qty net_pnl money_status reason risk_reason unit fees funding fee_detail_status source_updated_ms "
                       "pump_pct last near_high local_high stall updated_ts candle_ms scheduled_bar blocked ready watching candidate decision_ms").split()
            clean = {key: row[key] for key in allowed if key in row}
            clean.update(kind=kind, generation=ident, published_at_ms=now)
            # Content revision supports canonical replay and offline validation.
            clean["revision"] = hashlib.sha256(encoded(clean).encode()).hexdigest()[:16]
            rows.append(clean)
    manifest = {"kind": "manifest", "generation": ident, "published_at_ms": now,
                "expected": expected, "truncated": truncated, "history_status": view["history_status"],
                "coverage_start_ms": view["coverage_start_ms"], "coverage_end_ms": view["coverage_end_ms"],
                "pending": view["pending"], "unattributed": view["unattributed"]}
    rows.append(manifest)
    batch = {"generation": ident, "created_ms": now, "rows": rows, "expected": expected, "truncated": truncated}
    if len(encoded(batch).encode()) > MAX_BODY_BYTES:
        raise ValueError("Projection too large; do not publish a partial generation")
    return batch


def validate_projection(logs, manifest, *, kind, generation_id, now, max_age_ms=900_000):
    """Synthetic presentation oracle: a missing row is incomplete, a new empty view is valid."""
    if (manifest.get("generation") != generation_id or now - manifest["published_at_ms"] > max_age_ms or
            manifest.get("truncated", {}).get(kind, False)):
        return "incomplete", []
    selected = {}
    for row in logs:
        if row.get("generation") == generation_id and row.get("kind") == kind:
            previous = selected.get(row["entity_key"])
            if previous and previous != row:
                return "incomplete", []
            selected[row["entity_key"]] = row
    if len(selected) != manifest["expected"][kind]:
        return "incomplete", []
    return "complete", list(selected.values())
