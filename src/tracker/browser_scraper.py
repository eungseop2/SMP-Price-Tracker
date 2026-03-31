from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import TargetConfig
from .util import clean_text, dump_json, ensure_dir, parse_int

JSON_SCRIPT_SELECTOR = 'script[type="application/ld+json"]'
PRICE_KEYS = {"price", "lowPrice", "lowestPrice", "salePrice"}
SELLER_KEYS = {"seller", "sellerName", "mallName", "vendor", "merchantName"}
URL_KEYS = {"url", "productUrl", "link"}
TITLE_KEYS = {"name", "title", "productName"}


class BrowserScrapeError(RuntimeError):
    pass


def _flatten_ld_json_payloads(values: list[Any]) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            normalized: dict[str, Any] = {}
            for key, value in node.items():
                if key in PRICE_KEYS:
                    normalized["price"] = parse_int(value, default=0)
                elif key in SELLER_KEYS:
                    if isinstance(value, dict):
                        normalized["seller_name"] = clean_text(value.get("name") or value.get("sellerName") or value)
                    else:
                        normalized["seller_name"] = clean_text(value)
                elif key in URL_KEYS:
                    normalized["product_url"] = value
                elif key in TITLE_KEYS:
                    normalized["title"] = clean_text(value)

            if normalized.get("price"):
                normalized["search_rank"] = len(offers) + 1
                offers.append(normalized)

            for value in node.values():
                walk(value)

    walk(values)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for offer in offers:
        key = (offer.get("title"), offer.get("seller_name"), offer.get("price"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(offer)
    return deduped


async def _extract_from_ld_json(page) -> list[dict[str, Any]]:
    handles = await page.locator(JSON_SCRIPT_SELECTOR).all()
    raw_values: list[Any] = []
    for h in handles:
        text = await h.text_content()
        if not text:
            continue
        try:
            raw_values.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return _flatten_ld_json_payloads(raw_values)


async def _extract_from_dom(page, target: TargetConfig) -> list[dict[str, Any]]:
    rows = page.locator(target.browser.offer_row_selector)
    count = await rows.count()
    offers: list[dict[str, Any]] = []
    for i in range(count):
        row = rows.nth(i)
        row_text = clean_text(await row.text_content() or "")
        price = 0
        seller = ""

        price_nodes = row.locator(target.browser.price_selector)
        for j in range(await price_nodes.count()):
            text = clean_text(await price_nodes.nth(j).text_content() or "")
            value = parse_int(text, default=0)
            if value > 0:
                price = value
                break

        seller_nodes = row.locator(target.browser.seller_selector)
        for j in range(await seller_nodes.count()):
            text = clean_text(await seller_nodes.nth(j).text_content() or "")
            if len(text) >= 2 and not re.fullmatch(r"[0-9,원\s]+", text):
                seller = text
                break

        if price > 0:
            offers.append(
                {
                    "title": row_text[:200],
                    "price": price,
                    "seller_name": seller or None,
                    "product_url": page.url,
                    "search_rank": i + 1,
                }
            )
    return offers


async def collect_lowest_offer_via_browser(target: TargetConfig, artifacts_dir: str = "./artifacts") -> dict[str, Any]:
    """브라우저를 이용해 최저가를 수집합니다. (collect_current_offer_via_browser와 동일)"""
    return await collect_current_offer_via_browser(target, artifacts_dir)


async def collect_current_offer_via_browser(target: TargetConfig, artifacts_dir: str = "./artifacts") -> dict[str, Any]:
    if not target.url:
        raise ValueError(f"target '{target.name}' 에 url 이 없습니다.")

    from playwright.async_api import async_playwright

    artifacts = ensure_dir(artifacts_dir)
    screenshot_dir = ensure_dir(artifacts / "screenshots")
    html_dir = ensure_dir(artifacts / "html")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # 뷰포트를 넓게 설정하여 반응형 웹 대응
        page = await browser.new_page(viewport={"width": 1440, "height": 2400})
        try:
            await page.goto(target.url, wait_until=target.browser.wait_until, timeout=45000)
            
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(3000)

            for selector in target.browser.click_selectors:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    await locator.first.click(timeout=5000)

            offers = await _extract_from_ld_json(page)
            if not offers:
                offers = await _extract_from_dom(page, target)

            offers = [o for o in offers if parse_int(o.get("price"), 0) > 0]
            if not offers:
                html_path = html_dir / f"{target.name.replace('/', '_')}.html"
                html_path.write_text(await page.content(), encoding="utf-8")
                if target.browser.take_screenshot_on_failure:
                    await page.screenshot(path=str(screenshot_dir / f"{target.name.replace('/', '_')}.png"), full_page=True)
                raise BrowserScrapeError("가격 추출 실패")

            best = min(offers, key=lambda x: (parse_int(x.get("price"), 0), clean_text(x.get("seller_name"))))
            
            return {
                "target_name": target.name,
                "source_mode": target.mode,
                "success": 1,
                "status": "OK",
                "title": clean_text(best.get("title")),
                "price": parse_int(best.get("price"), 0),
                "seller_name": clean_text(best.get("seller_name")) or None,
                "product_id": None,
                "product_type": None,
                "product_url": best.get("product_url") or page.url,
                "search_rank": best.get("search_rank"),
                "raw_payload": {
                    "url": page.url,
                    "browser": asdict(target.browser),
                    "offers_found": offers,
                },
                "error_message": None,
            }
        finally:
            await browser.close()
