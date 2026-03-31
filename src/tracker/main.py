from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from .alert import check_and_alert
from .config import TargetConfig, load_config
from .db import ObservationStore, RankingStore
from .gcs_sync import download_db, upload_db
from .naver_api import (
    NaverShoppingSearchClient,
    collect_lowest_offer_via_api,
    _normalized_item,
)
from .notifier import send_price_alert
from .report import send_daily_report
from .util import calc_change_metrics, dump_json, utc_now_iso, is_night_time_kst

logger = logging.getLogger("naver_price_tracker")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


async def _collect_one(client: NaverShoppingSearchClient, target: TargetConfig, app_config, artifacts_dir: str) -> dict:
    """단일 타겟 수집 (API 전용)"""
    if target.mode == "api_query":
        try:
            return collect_lowest_offer_via_api(client, app_config, target)
        except Exception as e:
            raise e
    else:
        raise ValueError(f"지원하지 않는 수집 모드: {target.mode}")


async def run_once(app_config, artifacts_dir: str, db_path: str, summary_json: str | None = None) -> None:
    ok = 0
    fail = 0
    fallback_used_count = 0
    alerts_triggered_count = 0
    changed_items = []
    
    store = ObservationStore(db_path)
    client = NaverShoppingSearchClient(timeout_seconds=app_config.timeout_seconds)

    for target in app_config.targets:
        logger.info("수집 시작 | %s | mode=%s", target.name, target.mode)
        try:
            # 직전 성공 기록 조회 (가격 변동 체크용)
            prev_success = store.get_latest_success(target.name)
            prev_price = prev_success["price"] if prev_success else None

            result = await _collect_one(client, target, app_config, artifacts_dir)
            result["collected_at"] = utc_now_iso()
            result["config_mode"] = target.mode

            # 가격 변동 및 상태 계산
            current_price = result.get("price")
            if current_price is not None:
                if prev_price is None:
                    result["price_change_status"] = "FIRST_SEEN"
                else:
                    delta, pct = calc_change_metrics(current_price, prev_price)
                    result["prev_price"] = prev_price
                    result["price_delta"] = delta
                    result["price_delta_pct"] = pct
                    
                    if delta is not None:
                        if delta < 0: result["price_change_status"] = "PRICE_DOWN"
                        elif delta > 0: result["price_change_status"] = "PRICE_UP"
                        else: result["price_change_status"] = "PRICE_SAME"
                    else:
                        result["price_change_status"] = "PRICE_SAME"
            else:
                result["price_change_status"] = None

            # 필수 필드 보장
            result.setdefault("fallback_used", 0)
            result["alert_triggered"] = 0
            
            # 알림 체크
            # 알림 체크 (가격 변동 기반)
            if result.get("success"):
                ok += 1
                status = result.get("price_change_status")

                # 가격이 변동된 경우 (상승 or 하락)
                if status in ["PRICE_DOWN", "PRICE_UP"]:
                    result["alert_triggered"] = 1
                    alerts_triggered_count += 1
                    changed_items.append(result)
                else:
                    result["alert_triggered"] = 0
                
                if result.get("fallback_used"):
                    fallback_used_count += 1
                
                logger.info("수집 완료 | %s | %s", target.name, result.get("price_change_status"))
                store.insert(result)
            else:
                fail += 1
                logger.warning("수집 미일치 | %s | %s", target.name, result.get("status"))
                store.insert(result)

        except Exception as exc:
            fail += 1
            logger.exception("수집 실패 | %s", target.name)
            store.insert({
                "target_name": target.name,
                "config_mode": target.mode,
                "source_mode": target.mode,
                "fallback_used": 0,
                "collected_at": utc_now_iso(),
                "success": 0,
                "status": type(exc).__name__,
                "title": None,
                "price": None,
                "seller_name": None,
                "product_url": target.url,
                "error_message": str(exc),
                "price_change_status": None,
                "prev_price": None,
                "alert_triggered": 0
            })

    # ---------- [랭킹 수집 최적화 루틴] ----------
    unique_rank_queries = {t.rank_query for t in app_config.targets if t.rank_query}
    # "갤럭시"가 포함된 경우 제외된 버전도 수집 목록에 추가
    expanded_queries = set()
    for q in unique_rank_queries:
        expanded_queries.add(q)
        if "갤럭시" in q:
            short_q = q.replace("갤럭시", "").strip()
            if short_q:
                expanded_queries.add(short_q)
    
    logger.info("고유 랭킹 키워드 수집 시작 (%d개 -> 확장 %d개)", len(unique_rank_queries), len(expanded_queries))
    
    r_store = RankingStore(db_path)
    rank_collected_at = utc_now_iso()
    
    for r_query in expanded_queries:
        try:
            logger.info(f"랭킹 수집 중 (API): {r_query}")
            # Naver API 검색 (sim=네이버 랭킹순)
            rank_payload = client.search(query=r_query, display=15, sort="sim")
            rank_items = rank_payload.get("items", [])
            
            rows_to_insert = []
            for rank, item in enumerate(rank_items, start=1):
                norm = _normalized_item(item)
                rows_to_insert.append({
                    "query": r_query,
                    "rank": rank,
                    "collected_at": rank_collected_at,
                    "title": norm.get("title"),
                    "price": norm.get("price"),
                    "seller_name": norm.get("seller_name"),
                    "product_id": norm.get("product_id"),
                    "product_type": norm.get("product_type"),
                    "product_url": norm.get("product_url"),
                    "image_url": norm.get("image_url"),
                    "is_ad": 0
                })
            
            if rows_to_insert:
                r_store.insert_ranking_batch(rows_to_insert)
                logger.info(f"랭킹 수집 완료: {r_query} ({len(rows_to_insert)}개)")
            else:
                logger.warning(f"랭킹 수집 결과 없음: {r_query}")
        except Exception as e:
            logger.error(f"랭킹 수집 실패 ({r_query}): {e}")
    
    r_store.close()
    store.close()

    # 이메일 알림 (KST 기준 야간 시간대 21:00~08:00 제외)
    if changed_items:
        if not is_night_time_kst():
            send_price_alert(
                changed_items,
                app_config.email.email_from,
                app_config.email.email_password,
                app_config.email.email_to
            )
        else:
            logger.info("야간 시간(21:00-08:00 KST)이므로 이메일 알림을 건너뜁니다.")

    logger.info("최종 결과 | OK: %d, FAIL: %d, Fallback: %d, Alert: %d", ok, fail, fallback_used_count, alerts_triggered_count)

    if summary_json:
        summary_data = {
            "ok": ok,
            "fail": fail,
            "fallback_used": fallback_used_count,
            "alerts": alerts_triggered_count,
            "collected_at": utc_now_iso()
        }
        Path(summary_json).write_text(dump_json(summary_data), encoding="utf-8")
        logger.info(f"수집 요약 저장 완료: {summary_json}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Naver Shopping Price Tracker")
    parser.add_argument("command", choices=["once", "monitor", "export-ui", "serve", "sync-from-gcs", "sync-to-gcs", "daily-report"], help="실행할 커맨드")
    parser.add_argument("--config", default="targets.yaml", help="설정 파일 경로")
    parser.add_argument("--db", default="smp_price_tracker.sqlite3", help="DB 파일 경로")
    parser.add_argument("--interval", type=int, default=3600, help="모니터링 주기 (초)")
    parser.add_argument("--summary-json", help="수집 결과 요약을 저장할 JSON 경로")
    parser.add_argument("--verbose", action="store_true", help="상세 로그 출력")
    args = parser.parse_args()

    setup_logging(args.verbose)
    load_dotenv()

    try:
        app_config = load_config(args.config)
    except Exception as e:
        logger.error("설정 로드 실패: %s", e)
        return

    if args.command == "once":
        asyncio.run(run_once(app_config, "artifacts", args.db, summary_json=args.summary_json))

    elif args.command == "monitor":
        logger.info("%d초 간격으로 모니터링을 시작합니다...", args.interval)
        while True:
            try:
                asyncio.run(run_once(app_config, "artifacts", args.db, summary_json=args.summary_json))
            except Exception as e:
                logger.exception("모니터링 루프 중 오류 발생")
            time.sleep(args.interval)

    elif args.command == "export-ui":
        store = ObservationStore(args.db)
        r_store = RankingStore(args.db)
        try:
            dashboard_raw = store.get_dashboard_data(app_config.targets)
            
            # 고유 랭킹 키워드별 최신 데이터 수집 (확장 버전 포함)
            rankings = {}
            unique_rank_queries = {t.rank_query for t in app_config.targets if t.rank_query}
            
            expanded_queries = set()
            for q in unique_rank_queries:
                expanded_queries.add(q)
                if "갤럭시" in q:
                    short_q = q.replace("갤럭시", "").strip()
                    if short_q:
                        expanded_queries.add(short_q)

            for rq in expanded_queries:
                latest = r_store.get_latest_rankings(rq)
                if latest:
                    rankings[rq] = latest
            
            data = {
                "products": dashboard_raw["products"],
                "rankings": rankings,
                "updated_at": dashboard_raw["generated_at"]
            }
            Path("dashboard_data.json").write_text(dump_json(data), encoding="utf-8")
            logger.info("UI 데이터 내보내기 완료: dashboard_data.json")
        finally:
            store.close()
            r_store.close()

    elif args.command == "sync-from-gcs":
        bucket = os.getenv("GCS_BUCKET")
        if not bucket:
            logger.error("GCS_BUCKET 환경변수가 설정되지 않았습니다.")
            return
        download_db(bucket, args.db)

    elif args.command == "sync-to-gcs":
        bucket = os.getenv("GCS_BUCKET")
        if not bucket:
            logger.error("GCS_BUCKET 환경변수가 설정되지 않았습니다.")
            return
        upload_db(bucket, args.db)

    elif args.command == "serve":
        # 간단한 HTTP 서버 실행 (대시보드 확인용)
        import http.server
        import socketserver
        PORT = 8000
        Handler = http.server.SimpleHTTPRequestHandler
        with socketserver.TCPServer(("", PORT), Handler) as httpd:
            logger.info("http://localhost:%d 에서 대시보드 서비스를 시작합니다.", PORT)
            httpd.serve_forever()

    elif args.command == "daily-report":
        send_daily_report(args.db, app_config.email.email_from, app_config.email.email_password, app_config.email.email_to, app_config.targets)


if __name__ == "__main__":
    main()
