import os
import asyncio
import logging
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .main import run_once
from .gcs_sync import upload_db, download_db

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("tracker.cloud")

app = FastAPI(title="Naver Price Tracker Cloud")

# Configuration from ENV
CONFIG_PATH = os.getenv("CONFIG_PATH", "targets.yaml")
DB_PATH = os.getenv("DB_PATH", "data/smp_price_tracker.sqlite3")
ARTIFACTS_DIR = os.getenv("ARTIFACTS_DIR", "data/artifacts")
GCS_BUCKET = os.getenv("GCS_BUCKET") # GCS 버킷 이름 (필수)
INTERVAL = int(os.getenv("COLLECT_INTERVAL", "3600"))
ENABLE_BACKGROUND_COLLECTION = os.getenv("ENABLE_BACKGROUND_COLLECTION", "false").lower() == "true"
ENABLE_MANUAL_COLLECT = os.getenv("ENABLE_MANUAL_COLLECT", "false").lower() == "true"
ENABLE_GCS_SYNC = os.getenv("ENABLE_GCS_SYNC", "false").lower() == "true"

# 정적 파일 서빙 (dashboard_data.json 등)
app.mount("/static", StaticFiles(directory="."), name="static")

@app.on_event("startup")
async def startup_event():
    """앱 시작 시 GCS에서 DB 다운로드 및 추적 루프 시작"""
    logger.info("Cloud App starting up (Read-Only Mode Default)...")
    
    # 1. GCS에서 최신 DB 다운로드
    if GCS_BUCKET and ENABLE_GCS_SYNC:
        download_db(GCS_BUCKET, DB_PATH)
    
    # 2. 백그라운드 추적 루프 시작
    if ENABLE_BACKGROUND_COLLECTION:
        asyncio.create_task(tracker_loop())
    else:
        logger.info("Background collection is disabled by default.")

async def update_tracker_data():
    """수집 수행 및 UI/GCS 갱신 로직"""
    try:
        logger.info("Starting collection and sync...")
        ok, fail = await run_once(CONFIG_PATH, DB_PATH, ARTIFACTS_DIR)
        logger.info(f"Collection finished: ok={ok}, fail={fail}")
        
        # export-ui 로직 내장: JSON 파일만 생성하도록 변경 (HTML 주입 제거)
        from .config import load_config
        app_config = load_config(CONFIG_PATH)
        categories = {t.name: t.category for t in app_config.targets}

        store = ObservationStore(DB_PATH)
        data = store.get_dashboard_data(categories=categories)
        store.close()
        
        # dashboard_data.json 원자적 기록 (Race Condition 방지)
        json_path = Path("dashboard_data.json")
        tmp_path = json_path.with_name(json_path.name + ".tmp")
        tmp_path.write_text(dump_json(data), encoding="utf-8")
        tmp_path.replace(json_path)
        
        logger.info("dashboard_data.json updated.")
        
        # 3. GCS로 결과 업로드
        if GCS_BUCKET and ENABLE_GCS_SYNC:
            upload_db(GCS_BUCKET, DB_PATH)
        return ok, fail
    except Exception as e:
        logger.error(f"Update error: {e}")
        raise e

async def tracker_loop():
    """배경에서 주기적으로 수집 수행"""
    error_count = 0
    while True:
        try:
            await update_tracker_data()
            error_count = 0
        except Exception as e:
            error_count += 1
            logger.error(f"Tracker loop execution error: {e}")
            
        penalty = min(600, error_count * 60) if error_count > 0 else 0
        sleep_for = INTERVAL + penalty
        if error_count > 0:
            logger.warning(f"연속 에러 {error_count}회. 대기 {sleep_for}초 (백오프 {penalty}초)")
            
        await asyncio.sleep(sleep_for)

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """대시보드 보기"""
    html_path = Path("dashboard.html")
    if html_path.exists():
        # dashboard.html 내부에서 fetch('dashboard_data.json') 또는 fetch('/dashboard_data.json') 호출 대응 필요
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("Dashboard not found. Run collection first or check if dashboard.html exists.", status_code=404)

@app.get("/dashboard_data.json")
async def get_dashboard_data():
    """대시보드 데이터 JSON 서빙"""
    json_path = Path("dashboard_data.json")
    if json_path.exists():
        return FileResponse(json_path)
    raise HTTPException(status_code=404, detail="Data not found")

@app.post("/collect")
async def manual_collect():
    """수동 수집 트리거"""
    if not ENABLE_MANUAL_COLLECT:
        return {"status": "error", "message": "Manual collection is disabled. Set ENABLE_MANUAL_COLLECT=true"}
    ok, fail = await update_tracker_data()
    return {"status": "ok", "ok": ok, "fail": fail}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
