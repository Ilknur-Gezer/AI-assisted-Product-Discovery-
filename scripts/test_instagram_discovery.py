from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

USERNAME = "yagmurvardar_"

PROFILE_DIR = Path(".secrets/instagram_browser")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

found: dict[str, dict[str, Any]] = {}
interesting_responses: list[str] = []


def caption_text(value: Any) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text

    return ""


def username_from(obj: dict) -> str | None:
    for key in ("owner", "user"):
        value = obj.get(key)

        if isinstance(value, dict):
            username = value.get("username")

            if isinstance(username, str):
                return username

    username = obj.get("username")

    if isinstance(username, str):
        return username

    return None


def walk(obj: Any) -> None:
    if isinstance(obj, dict):

        code = obj.get("code") or obj.get("shortcode")

        # Media benzeri bir nesne olduğundan emin olmaya çalış.
        looks_like_media = any(
            key in obj
            for key in (
                "taken_at",
                "media_type",
                "product_type",
                "caption",
                "pk",
                "id",
            )
        )

        if isinstance(code, str) and code and looks_like_media:

            owner = username_from(obj)

            # Owner bilgisi varsa yanlış hesaba ait media'yı alma.
            if owner is None or owner.casefold() == USERNAME.casefold():

                product_type = str(
                    obj.get("product_type") or ""
                ).casefold()

                is_reel = (
                    product_type in {"clips", "reels", "reel"}
                )

                url_type = "reel" if is_reel else "p"

                found[code] = {
                    "shortcode": code,
                    "owner": owner,
                    "taken_at": obj.get("taken_at"),
                    "media_type": obj.get("media_type"),
                    "product_type": obj.get("product_type"),
                    "caption": caption_text(obj.get("caption")),
                    "url": f"https://www.instagram.com/{url_type}/{code}/",
                }

        for value in obj.values():
            walk(value)

    elif isinstance(obj, list):
        for value in obj:
            walk(value)


def on_response(response) -> None:
    url = response.url

    if not any(
        token in url
        for token in (
            "graphql",
            "/api/v1/",
            "clips",
            "profile",
        )
    ):
        return

    try:
        content_type = response.headers.get(
            "content-type",
            ""
        )

        if "json" not in content_type:
            return

        payload = response.json()

    except Exception:
        return

    interesting_responses.append(url)

    try:
        walk(payload)
    except Exception:
        pass


with sync_playwright() as p:

    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        viewport={
            "width": 1280,
            "height": 900,
        },
    )

    page = (
        context.pages[0]
        if context.pages
        else context.new_page()
    )

    page.on("response", on_response)

    url = f"https://www.instagram.com/{USERNAME}/"

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    page.wait_for_timeout(5000)

    print("\nCURRENT URL:", page.url)
    print("TITLE      :", page.title())

    # Birkaç kez scroll ederek profile feed pagination'ı tetikle.
    for _ in range(8):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(1500)

    # DOM fallback
    hrefs = page.locator("a").evaluate_all(
        """
        els => els
            .map(a => a.href)
            .filter(Boolean)
        """
    )

    dom_codes = set()

    for href in hrefs:
        match = re.search(
            r"instagram\\.com/(?:reel|reels|p)/([^/?#]+)",
            href,
        )

        if match:
            dom_codes.add(match.group(1))

    print("\nDOM MEDIA CODES:", len(dom_codes))
    for code in sorted(dom_codes):
        print("DOM:", code)

    print("\nNETWORK RESPONSES:", len(interesting_responses))
    for u in interesting_responses[-15:]:
        print("NET:", u[:180])

    print("\nNETWORK MEDIA:", len(found))

    for item in found.values():
        print("-" * 80)
        print("shortcode :", item["shortcode"])
        print("owner     :", item["owner"])
        print("taken_at  :", item["taken_at"])
        print("type      :", item["product_type"])
        print("url       :", item["url"])
        print(
            "caption   :",
            item["caption"][:180].replace("\n", " "),
        )

    output = Path("/tmp/instagram_discovery.json")

    output.write_text(
        json.dumps(
            list(found.values()),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    page.screenshot(
        path="/tmp/instagram_profile.png",
        full_page=False,
    )

    print("\nJSON      :", output)
    print("SCREENSHOT: /tmp/instagram_profile.png")

    context.close()
    