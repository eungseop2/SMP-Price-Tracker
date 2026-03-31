from __future__ import annotations

import logging
import os
from pathlib import Path

from .util import format_price, utc_now_iso

logger = logging.getLogger("naver_price_tracker.alert")

_ALERT_LOG_PATH = "./price_alerts.log"


def check_and_alert(result: dict, prev_price: int | None, threshold: float) -> bool:
    """직전 성공가 대비 현재 가격이 임계값 이상 하락했는지 확인하고 알림을 발생시킵니다.
    
    Returns:
        bool: 알림이 발생했는지 여부 (alert_triggered)
    """
    if not result.get("success"):
        return False
        
    current_price = result.get("price")
    if current_price is None or prev_price is None or prev_price == 0:
        return False

    # 계산식: ((prev_price - current_price) / prev_price) * 100 >= threshold
    drop_pct = ((prev_price - current_price) / prev_price) * 100
    
    if drop_pct < threshold:
        return False

    target_name = result.get("target_name", "Unknown")
    seller = result.get("seller_name") or "-"
    message = (
        f"[가격하락 경고] {target_name} | "
        f"{format_price(prev_price)} → {format_price(current_price)} "
        f"({drop_pct:+.1f}% 하락!) | 판매처: {seller}"
    )

    logger.warning(message)
    _write_alert_log(message)
    return True


def _write_alert_log(message: str) -> None:
    """알림을 price_alerts.log 파일에 추가합니다."""
    try:
        log_path = Path(_ALERT_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = utc_now_iso()
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{timestamp} | {message}\n")
    except OSError as exc:
        logger.debug("알림 로그 기록 실패: %s", exc)
