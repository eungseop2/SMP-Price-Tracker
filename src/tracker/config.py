from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class MatchConfig:
    required_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    product_id: str | None = None
    allowed_product_types: list[int] = field(default_factory=list)


@dataclass
class EmailConfig:
    email_from: str | None = None
    email_password: str | None = None
    email_to: list[str] = field(default_factory=list)


@dataclass
class RequestConfig:
    pages: int = 1
    sort: str = "sim"
    filter: str | None = None


@dataclass
class BrowserConfig:
    wait_until: str = "networkidle"
    click_selectors: list[str] = field(default_factory=list)
    offer_row_selector: str = "li"
    seller_selector: str = "a, span"
    price_selector: str = "strong, em, span"
    take_screenshot_on_failure: bool = True


@dataclass
class TargetConfig:
    name: str
    mode: str
    query: str | None = None
    rank_query: str | None = None
    url: str | None = None
    fallback_url: str | None = None
    category: str = "기타"  # 상품 카테고리 (분류용)
    match: MatchConfig = field(default_factory=MatchConfig)
    request: RequestConfig = field(default_factory=RequestConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)


@dataclass
class AppConfig:
    display: int = 100
    exclude: str = "used:cbshop"
    timeout_seconds: int = 20
    alert_threshold_percent: float = 5.0
    email: EmailConfig = field(default_factory=EmailConfig)
    targets: list[TargetConfig] = field(default_factory=list)


def _to_match(raw: dict[str, Any] | None) -> MatchConfig:
    raw = raw or {}
    return MatchConfig(
        required_keywords=list(raw.get("required_keywords", []) or []),
        exclude_keywords=list(raw.get("exclude_keywords", []) or []),
        product_id=(str(raw["product_id"]) if raw.get("product_id") is not None else None),
        allowed_product_types=[int(x) for x in (raw.get("allowed_product_types", []) or [])],
    )


def _to_request(raw: dict[str, Any] | None) -> RequestConfig:
    raw = raw or {}
    try:
        pages = int(raw.get("pages", 1))
    except (ValueError, TypeError):
        pages = 0  # validate_config will catch this
    return RequestConfig(
        pages=pages,
        sort=str(raw.get("sort", "asc")),
        filter=raw.get("filter"),
    )


def _to_browser(raw: dict[str, Any] | None) -> BrowserConfig:
    raw = raw or {}
    return BrowserConfig(
        wait_until=str(raw.get("wait_until", "networkidle")),
        click_selectors=list(raw.get("click_selectors", []) or []),
        offer_row_selector=str(raw.get("offer_row_selector", "li")),
        seller_selector=str(raw.get("seller_selector", "a, span")),
        price_selector=str(raw.get("price_selector", "strong, em, span")),
        take_screenshot_on_failure=bool(raw.get("take_screenshot_on_failure", True)),
    )


def validate_config(app: AppConfig, extra_errors: list[str] | None = None) -> None:
    """설정 유효성을 검증하고 오류가 있으면 ValueError를 발생시킵니다 (Fail-Fast)."""
    errors: list[str] = list(extra_errors or [])
    names: set[str] = set()

    for t in app.targets:
        # 이름 중복 검사
        if t.name in names:
            errors.append(f"중복된 타겟 이름: {t.name}")
        names.add(t.name)

        # 모드 검사
        if t.mode not in ("api_query", "browser_url"):
            errors.append(f"[{t.name}] 지원하지 않는 mode: {t.mode!r}")

        # 필수 필드 검사
        if t.mode == "api_query" and not t.query:
            errors.append(f"[{t.name}] api_query 모드에는 'query' 필드가 필수입니다.")
        if t.mode == "browser_url" and not t.url:
            errors.append(f"[{t.name}] browser_url 모드에는 'url' 필드가 필수입니다.")

        # 폴백 조건 검사
        if t.fallback_url and t.mode != "api_query":
            errors.append(f"[{t.name}] fallback_url은 api_query 모드에서만 사용할 수 있습니다.")

        # 페이지 범위 검사
        if t.request.pages < 1:
            errors.append(f"[{t.name}] pages는 1 이상이어야 합니다.")

    # 알림 임계값 검사
    if not (0 < app.alert_threshold_percent < 100):
        errors.append(f"alert_threshold_percent 범위가 비정상적입니다 (0~100): {app.alert_threshold_percent}")

    if errors:
        raise ValueError("설정 유효성 검증 실패:\n" + "\n".join(f"- {e}" for e in errors))


def load_config(path: str | Path) -> AppConfig:
    import os
    from dotenv import load_dotenv
    load_dotenv(override=False)
    
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    common = raw.get("common", {}) or {}
    email_raw = common.get("email", {}) or {}

    errors = []
    
    # 1. common 섹션 파싱 (에러 누적)
    try:
        display = min(100, max(1, int(common.get("display", 100))))
    except (ValueError, TypeError):
        errors.append(f"common.display 값이 올바르지 않습니다: {common.get('display')}")
        display = 100

    try:
        timeout_seconds = max(5, int(common.get("timeout_seconds", 20)))
    except (ValueError, TypeError):
        errors.append(f"common.timeout_seconds 값이 올바르지 않습니다: {common.get('timeout_seconds')}")
        timeout_seconds = 20

    try:
        alert_threshold_percent = float(common.get("alert_threshold_percent", 5.0))
    except (ValueError, TypeError):
        errors.append(f"common.alert_threshold_percent 값이 올바르지 않습니다: {common.get('alert_threshold_percent')}")
        alert_threshold_percent = 5.0

    # 1-1. 이메일 설정 파싱 (환경변수 -> YAML 순서로 우선순위)
    email_from = os.getenv("EMAIL_FROM") or email_raw.get("from")
    email_password = os.getenv("EMAIL_APP_PASSWORD") or email_raw.get("password")
    
    to_raw = os.getenv("EMAIL_TO") or email_raw.get("to", "")
    if isinstance(to_raw, list):
        email_to = [str(x).strip() for x in to_raw if x]
    else:
        email_to = [x.strip() for x in str(to_raw).split(",") if x.strip()]

    app = AppConfig(
        display=display,
        exclude=str(common.get("exclude", "used:cbshop")),
        timeout_seconds=timeout_seconds,
        alert_threshold_percent=alert_threshold_percent,
        email=EmailConfig(
            email_from=email_from,
            email_password=email_password,
            email_to=email_to
        ),
        targets=[],
    )

    # 2. targets 섹션 파싱 (에러 누적)
    for i, item in enumerate(raw.get("targets", []) or []):
        try:
            name = item.get("name")
            mode = item.get("mode")
            
            if not name or not mode:
                errors.append(f"targets[{i}]에 'name' 또는 'mode'가 누락되었습니다.")
                continue

            target = TargetConfig(
                name=str(name),
                mode=str(mode),
                query=item.get("query"),
                rank_query=item.get("rank_query") or str(name),
                url=item.get("url"),
                fallback_url=item.get("fallback_url"),
                category=str(item.get("category", "기타")),
                match=_to_match(item.get("match")),
                request=_to_request(item.get("request")),
                browser=_to_browser(item.get("browser")),
            )
            
            app.targets.append(target)
        except Exception as e:
            errors.append(f"targets[{i}] ({item.get('name', 'unknown')}) 처리 중 오류: {e}")

    if errors:
        # validate_config를 호출하기 전에 이미 수집된 에러가 있으면 여기서 던질 수도 있음
        # 하지만 validate_config까지 합쳐서 보여주는 것이 요구 사항
        pass

    try:
        validate_config(app, extra_errors=errors)
    except ValueError as e:
        raise e
    
    return app
