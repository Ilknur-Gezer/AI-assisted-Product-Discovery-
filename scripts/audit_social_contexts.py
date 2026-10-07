#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path


CURRENT_SAFE_PROMPT_PREFIXES = (
    "social-caption-context-v4-product-specific-opinions",
    "social-caption-context-v5",
    "social-caption-context-v6-one-product-one-context",
)


def norm(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )
    text = re.sub(r"[^a-z0-9%+]+", " ", text)
    return " ".join(text.split())


def meaningful_tokens(text: str | None) -> set[str]:
    stop = {
        "the","and","for","with","cream","serum","gel","spf","plus",
        "urun","urunu","krem","serum","jel","losyon","mask","maske",
        "beauty","skin","skin care","skincare"
    }
    return {
        t for t in norm(text).split()
        if len(t) >= 4 and t not in stop
    }


def safe_prompt(prompt_version: str | None) -> bool:
    value = str(prompt_version or "")
    return any(value.startswith(prefix) for prefix in CURRENT_SAFE_PROMPT_PREFIXES)


def load_rows(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT
            sp.id AS social_post_id,
            sp.external_post_id,
            sp.platform,
            i.slug AS influencer_slug,
            i.display_name AS influencer_name,

            spp.id AS social_post_product_id,
            spp.product_id,
            spp.candidate_key,
            spp.evidence_text AS product_evidence,

            p.brand,
            p.product_name,

            spc.relationship,
            spc.summary,
            spc.opinion_summary,
            spc.evidence_texts_json,
            spc.confidence,
            spc.status,
            spc.prompt_version,
            spc.source_file
        FROM social_product_contexts spc
        JOIN social_post_products spp
          ON spp.id = spc.social_post_product_id
        JOIN social_posts sp
          ON sp.id = spp.social_post_id
        JOIN influencers i
          ON i.id = sp.influencer_id
        LEFT JOIN products p
          ON p.id = spp.product_id
        WHERE spc.status = 'approved'
          AND spp.status = 'approved'
          AND spp.product_id IS NOT NULL
        ORDER BY sp.id, spp.id
        """
    ).fetchall()


def main():
    parser = argparse.ArgumentParser(
        description="Audit TikTok social caption contexts for cross-product opinion leakage."
    )
    parser.add_argument(
        "--database",
        default="data/database/skinfluencer.sqlite",
    )
    parser.add_argument(
        "--json-out",
        default="data/audits/social_context_audit.json",
    )
    args = parser.parse_args()

    db = Path(args.database)
    if not db.exists():
        raise SystemExit(f"Database yok: {db}")

    conn = sqlite3.connect(db)
    try:
        rows = load_rows(conn)
    finally:
        conn.close()

    by_post = defaultdict(list)
    for row in rows:
        by_post[row["social_post_id"]].append(dict(row))

    findings = []

    for social_post_id, items in by_post.items():
        if len(items) < 2:
            continue

        # -------------------------------------------------------------
        # 1) Same non-empty opinion copied to multiple distinct products
        # -------------------------------------------------------------
        opinion_map = defaultdict(list)
        for item in items:
            opinion = norm(item["opinion_summary"])
            if opinion:
                opinion_map[opinion].append(item)

        for opinion, matched in opinion_map.items():
            product_ids = {x["product_id"] for x in matched}
            if len(product_ids) > 1:
                findings.append({
                    "severity": "HIGH",
                    "reason": "same_opinion_multiple_products",
                    "social_post_id": social_post_id,
                    "external_post_id": matched[0]["external_post_id"],
                    "influencer_slug": matched[0]["influencer_slug"],
                    "opinion_summary": matched[0]["opinion_summary"],
                    "products": [
                        {
                            "product_id": x["product_id"],
                            "brand": x["brand"],
                            "product_name": x["product_name"],
                            "prompt_version": x["prompt_version"],
                            "source_file": x["source_file"],
                        }
                        for x in matched
                    ],
                })

        # -------------------------------------------------------------
        # 2) Opinion mentions another brand/product from the same post
        # -------------------------------------------------------------
        for item in items:
            opinion = norm(item["opinion_summary"])
            if not opinion:
                continue

            own_brand = norm(item["brand"])
            own_tokens = meaningful_tokens(item["product_name"])

            for other in items:
                if other["product_id"] == item["product_id"]:
                    continue

                other_brand = norm(other["brand"])
                other_tokens = meaningful_tokens(other["product_name"])

                foreign_brand_hit = bool(
                    other_brand
                    and len(other_brand) >= 4
                    and other_brand in opinion
                    and other_brand != own_brand
                )

                foreign_product_hits = {
                    t for t in other_tokens
                    if t in opinion and t not in own_tokens
                }

                # Product-name evidence requires at least 2 distinguishing tokens
                # to reduce false positives such as "cream" / "serum".
                foreign_name_hit = len(foreign_product_hits) >= 2

                if foreign_brand_hit or foreign_name_hit:
                    findings.append({
                        "severity": "HIGH",
                        "reason": "foreign_product_mentioned_in_opinion",
                        "social_post_id": social_post_id,
                        "external_post_id": item["external_post_id"],
                        "influencer_slug": item["influencer_slug"],
                        "product_id": item["product_id"],
                        "brand": item["brand"],
                        "product_name": item["product_name"],
                        "opinion_summary": item["opinion_summary"],
                        "foreign_product_id": other["product_id"],
                        "foreign_brand": other["brand"],
                        "foreign_product_name": other["product_name"],
                        "prompt_version": item["prompt_version"],
                        "source_file": item["source_file"],
                    })
                    break

        # -------------------------------------------------------------
        # 3) Old extractor + multi-product post + any opinion
        # This is a candidate for targeted re-extraction, not proof of error.
        # -------------------------------------------------------------
        if any((x["opinion_summary"] or "").strip() for x in items):
            old = [x for x in items if not safe_prompt(x["prompt_version"])]
            if old:
                findings.append({
                    "severity": "MEDIUM",
                    "reason": "old_prompt_multi_product_post_with_opinion",
                    "social_post_id": social_post_id,
                    "external_post_id": items[0]["external_post_id"],
                    "influencer_slug": items[0]["influencer_slug"],
                    "product_count": len({x["product_id"] for x in items}),
                    "prompt_versions": sorted({
                        str(x["prompt_version"] or "") for x in items
                    }),
                    "products": [
                        {
                            "product_id": x["product_id"],
                            "brand": x["brand"],
                            "product_name": x["product_name"],
                            "opinion_summary": x["opinion_summary"],
                        }
                        for x in items
                    ],
                })

    # Deduplicate by reason/post/product tuple
    seen = set()
    deduped = []
    for f in findings:
        key = (
            f["reason"],
            f["external_post_id"],
            f.get("product_id"),
            f.get("foreign_product_id"),
            f.get("opinion_summary"),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    deduped.sort(
        key=lambda x: (
            severity_order.get(x["severity"], 9),
            x["influencer_slug"],
            x["external_post_id"],
        )
    )

    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "database": str(db),
                "approved_context_rows_scanned": len(rows),
                "multi_product_posts_scanned": sum(
                    1 for v in by_post.values() if len(v) >= 2
                ),
                "high_findings": sum(
                    1 for x in deduped if x["severity"] == "HIGH"
                ),
                "medium_findings": sum(
                    1 for x in deduped if x["severity"] == "MEDIUM"
                ),
                "findings": deduped,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    high_posts = sorted({
        (x["influencer_slug"], x["external_post_id"])
        for x in deduped
        if x["severity"] == "HIGH"
    })
    medium_posts = sorted({
        (x["influencer_slug"], x["external_post_id"])
        for x in deduped
        if x["severity"] == "MEDIUM"
    })

    print(f"Approved context satırı: {len(rows)}")
    print(
        "Multi-product post:",
        sum(1 for v in by_post.values() if len(v) >= 2),
    )
    print(f"HIGH finding: {sum(x['severity']=='HIGH' for x in deduped)}")
    print(f"MEDIUM finding: {sum(x['severity']=='MEDIUM' for x in deduped)}")
    print(f"Rapor: {out}")

    if high_posts:
        print("\nHIGH — önce bunları kontrol / yeniden extract et:")
        for influencer, video_id in high_posts:
            print(f"  {influencer:20s} {video_id}")

    if medium_posts:
        print("\nMEDIUM — eski prompt kullanılan multi-product+opinion postları:")
        for influencer, video_id in medium_posts[:50]:
            print(f"  {influencer:20s} {video_id}")
        if len(medium_posts) > 50:
            print(f"  ... +{len(medium_posts)-50} post")


if __name__ == "__main__":
    main()
