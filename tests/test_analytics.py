from __future__ import annotations

import sqlite3
from pathlib import Path

from skinfluencer.web.analytics import track_event


def test_outbound_click_is_persisted(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE influencers (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, display_name TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, brand TEXT, product_name TEXT);
        CREATE TABLE purchase_links (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            merchant_slug TEXT NOT NULL,
            url TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            match_confidence REAL
        );
        INSERT INTO influencers(id, slug, display_name) VALUES (7, 'naturally_serein', 'Çisem Çakır');
        INSERT INTO products(id, brand, product_name) VALUES (42, 'Brand', 'Product');
        INSERT INTO purchase_links(id, product_id, merchant_slug, url, verification_status, match_confidence)
        VALUES (9, 42, 'sephora', 'https://example.com/product', 'verified', 0.99);
        """
    )
    con.commit()
    con.close()

    track_event(
        "outbound_click",
        {
            "product_id": 42,
            "influencer_id": 999,
            "influencer_slug": "naturally_serein",
            "merchant": "sephora",
            "timestamp": "2026-10-07T09:00:00.000Z",
        },
        database=db,
    )

    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT product_id, influencer_id, influencer_slug, merchant_slug, destination_url, clicked_at FROM outbound_clicks"
    ).fetchone()
    con.close()

    assert row == (
        42,
        7,
        "naturally_serein",
        "sephora",
        "https://example.com/product",
        "2026-10-07T09:00:00Z",
    )
