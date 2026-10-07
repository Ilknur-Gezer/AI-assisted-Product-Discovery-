"""Lightweight first-party analytics persistence for the Shiny app."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.settings import DATABASE_PATH


_OUTBOUND_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_clicks (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL
        REFERENCES products(id) ON DELETE CASCADE,
    influencer_id INTEGER
        REFERENCES influencers(id) ON DELETE SET NULL,
    influencer_slug TEXT NOT NULL,
    merchant_slug TEXT NOT NULL,
    destination_url TEXT NOT NULL,
    clicked_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_product
    ON outbound_clicks(product_id);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_influencer
    ON outbound_clicks(influencer_id);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_merchant
    ON outbound_clicks(merchant_slug);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_clicked_at
    ON outbound_clicks(clicked_at);
"""


def _utc_iso(value: Any) -> str:
    """Normalize a browser ISO timestamp to UTC; fall back to server time."""
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _track_outbound_click(database: Path, payload: dict[str, Any]) -> None:
    try:
        product_id = int(payload["product_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("outbound_click requires a valid product_id") from exc

    influencer_slug = str(payload.get("influencer_slug") or "").strip()
    merchant_slug = str(payload.get("merchant") or "").strip()
    if not influencer_slug or not merchant_slug:
        raise ValueError("outbound_click requires influencer_slug and merchant")

    clicked_at = _utc_iso(payload.get("timestamp"))

    connection = sqlite3.connect(database, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            connection.executescript(_OUTBOUND_SCHEMA)

            product = connection.execute(
                "SELECT id FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()
            if product is None:
                raise ValueError(f"Unknown product_id: {product_id}")

            influencer = connection.execute(
                "SELECT id FROM influencers WHERE slug = ?",
                (influencer_slug,),
            ).fetchone()
            influencer_id = int(influencer[0]) if influencer else None

            purchase = connection.execute(
                """
                SELECT url
                FROM purchase_links
                WHERE product_id = ?
                  AND merchant_slug = ?
                  AND verification_status = 'verified'
                ORDER BY COALESCE(match_confidence, 0) DESC, id
                LIMIT 1
                """,
                (product_id, merchant_slug),
            ).fetchone()
            if purchase is None:
                raise ValueError(
                    "Outbound target is not a verified purchase link for this product"
                )

            connection.execute(
                """
                INSERT INTO outbound_clicks (
                    product_id,
                    influencer_id,
                    influencer_slug,
                    merchant_slug,
                    destination_url,
                    clicked_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    influencer_id,
                    influencer_slug,
                    merchant_slug,
                    str(purchase[0]),
                    clicked_at,
                ),
            )
    finally:
        connection.close()


def track_event(
    event_name: str,
    payload: dict[str, Any] | None = None,
    *,
    database: Path = DATABASE_PATH,
) -> None:
    """Persist supported first-party analytics events.

    Unknown event names are ignored deliberately so adding future UI events does
    not break the application. Client-provided IDs/URLs are never trusted as the
    source of truth; the outbound destination is resolved again from SQLite.
    """
    if event_name != "outbound_click":
        return
    if not isinstance(payload, dict):
        raise ValueError("outbound_click payload must be a dictionary")
    _track_outbound_click(Path(database), payload)
