#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-extract HIGH/MEDIUM social caption-context audit posts."
    )
    parser.add_argument(
        "--audit",
        default="data/audits/social_context_audit.json",
        help="Audit JSON produced by audit_social_contexts.py",
    )
    parser.add_argument(
        "--model",
        default="gpt-4.1-mini",
    )
    parser.add_argument(
        "--severity",
        choices=["high", "high+medium"],
        default="high+medium",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list the unique posts that would be re-extracted.",
    )
    args = parser.parse_args()

    audit_path = Path(args.audit)
    if not audit_path.exists():
        raise SystemExit(f"Audit raporu yok: {audit_path}")

    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    allowed = {"HIGH"}
    if args.severity == "high+medium":
        allowed.add("MEDIUM")

    targets = sorted({
        (str(item["influencer_slug"]), str(item["external_post_id"]))
        for item in payload.get("findings", [])
        if item.get("severity") in allowed
        and item.get("influencer_slug")
        and item.get("external_post_id")
    })

    print(f"Unique flagged posts: {len(targets)}")
    for influencer, video_id in targets:
        print(f"  {influencer:20s} {video_id}")

    if args.dry_run:
        return

    failures = []
    for index, (influencer, video_id) in enumerate(targets, start=1):
        print(f"\n[{index}/{len(targets)}] {influencer} · {video_id}", flush=True)
        command = [
            sys.executable,
            "-m",
            "skinfluencer.extraction.caption_context_extractor",
            "--influencer",
            influencer,
            "--video-id",
            video_id,
            "--model",
            args.model,
            "--force",
        ]
        result = subprocess.run(command)
        if result.returncode != 0:
            failures.append({
                "influencer_slug": influencer,
                "external_post_id": video_id,
                "returncode": result.returncode,
            })

    print("\n=== RE-EXTRACTION SUMMARY ===")
    print(f"Targets : {len(targets)}")
    print(f"Success : {len(targets) - len(failures)}")
    print(f"Failure : {len(failures)}")

    if failures:
        failure_path = audit_path.with_name("social_context_reextract_failures.json")
        failure_path.write_text(
            json.dumps(failures, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Failures: {failure_path}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
