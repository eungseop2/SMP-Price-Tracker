from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import AppConfig, TargetConfig
from .util import all_keywords_present, any_keyword_present, clean_text, parse_int


SHOP_API_URL = "https://openapi.naver.com/v1/search/shop.json"


class NaverShoppingSearchClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.client_id = os.getenv("NAVER_CLIENT_ID", "")
        self.client_secret = os.getenv("NAVER_CLIENT_SECRET", "")
        self.user_agent = os.getenv("USER_AGENT", "NaverPriceTracker/1.0")
        self.timeout_seconds = int(os.getenv("REQUEST_TIMEOUT", str(timeout_seconds)))
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _headers(self) -> dict[str, str]:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 설정되지 않았습니다.")
        return {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

    def search(self, *, query: str, display: int = 100, start: int = 1, sort: str = "asc", filter_: str | None = None, exclude: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": query,
            "display": display,
            "start": start,
            "sort": sort,
        }
        if filter_:
            params["filter"] = filter_
        if exclude:
            params["exclude"] = exclude

        response = self.session.get(
            SHOP_API_URL,
            headers=self._headers(),
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()



def _item_matches(target: TargetConfig, item: dict[str, Any]) -> bool:
    title = clean_text(item.get("title"))
    product_id = str(item.get("productId", "") or "").strip()
    target_id = str(target.match.product_id or "").strip()
    product_type = int(item.get("productType", 0) or 0)

    # 1. 타입 체크 우선 (카탈로그 요청 시 카탈로그만, 혹은 사용자 지정 타입)
    if target.match.allowed_product_types and product_type not in target.match.allowed_product_types:
        return False

    # 2. product_id가 지정된 경우 ID가 일치하면 최우선 매칭 (로그상 확인 가능하도록 별도 처리 가능)
    id_matched = False
    if target_id and product_id == target_id:
        id_matched = True

    # 3. 키워드 기반 매칭 (ID 미지정 시 필수, 지정 시 보조 수단)
    kw_matched = True
    if target.match.required_keywords and not all_keywords_present(title, target.match.required_keywords):
        kw_matched = False
    if target.match.exclude_keywords and any_keyword_present(title, target.match.exclude_keywords):
        kw_matched = False

    # 결론: ID가 일치하거나, (ID 매칭 실패 시) 키워드라도 완벽히 맞으면 매칭 성공으로 간주
    if id_matched:
        return True
    
    return kw_matched



def _normalized_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": clean_text(item.get("title")),
        "price": parse_int(item.get("lprice"), default=0),
        "seller_name": clean_text(item.get("mallName")),
        "product_id": str(item.get("productId", "") or "") or None,
        "product_type": int(item.get("productType", 0) or 0),
        "product_url": item.get("link"),
        "image_url": item.get("image"),
        "search_rank": item.get("_search_rank"),
        "raw_payload": item,
    }



def collect_lowest_offer_via_api(client: NaverShoppingSearchClient, app_config: AppConfig, target: TargetConfig) -> dict[str, Any]:
    if not target.query:
        raise ValueError(f"target '{target.name}' 에 query 가 없습니다.")

    pages = max(1, target.request.pages)
    items: list[dict[str, Any]] = []

    for page_index in range(pages):
        start = page_index * app_config.display + 1
        payload = client.search(
            query=target.query,
            display=app_config.display,
            start=start,
            sort=target.request.sort,
            filter_=target.request.filter,
            exclude=app_config.exclude,
        )
        page_items = payload.get("items", []) or []
        for i, itm in enumerate(page_items, start=len(items) + 1):
            itm["_search_rank"] = i
        items.extend(page_items)

    candidates: list[dict[str, Any]] = []
    for item in items:
        # ID 매칭 여부와 키워드 매칭 여부를 동시에 확인
        title = clean_text(item.get("title"))
        product_id = str(item.get("productId", "") or "").strip()
        target_id = str(target.match.product_id or "").strip()
        product_type = int(item.get("productType", 0) or 0)

        # 1. 타입 체크 (기본적으로 1: 카탈로그, 2: 일반, 3: 쇼핑몰상품, 11: 가격비교 등 유입 허용)
        allowed_types = target.match.allowed_product_types or [1, 2, 3, 11]
        type_ok = product_type in allowed_types

        # 2. ID 매칭
        id_matched = (target_id and product_id == target_id)

        # 3. 키워드 매칭
        kw_matched = True
        if target.match.required_keywords and not all_keywords_present(title, target.match.required_keywords):
            kw_matched = False
        if target.match.exclude_keywords and any_keyword_present(title, target.match.exclude_keywords):
            kw_matched = False

        if type_ok and (id_matched or kw_matched):
            norm = _normalized_item(item)
            norm["_id_matched"] = id_matched
            if norm["price"] > 0:
                candidates.append(norm)

    if not candidates:
        return {
            "target_name": target.name,
            "source_mode": target.mode,
            "success": 0,
            "status": "NO_MATCH",
            "title": None,
            "price": None,
            "seller_name": None,
            "product_id": target.match.product_id,
            "product_type": None,
            "product_url": None,
            "raw_payload": {
                "query": target.query,
                "request": asdict(target.request),
                "match": asdict(target.match),
                "items_examined": len(items),
            },
            "error_message": "조건에 맞는 상품을 찾지 못했습니다. (검색된 상품 수: {})".format(len(items)),
        }

    # 정렬: 1순위 ID 매칭 상품, 2순위 최저가 순
    best = min(candidates, key=lambda x: (not x["_id_matched"], x["price"], x["seller_name"] or "zzzz"))
    return {
        "target_name": target.name,
        "source_mode": target.mode,
        "success": 1,
        "status": "OK",
        **best,
        "error_message": None,
    }
