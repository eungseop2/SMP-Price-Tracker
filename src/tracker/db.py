from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .util import dump_json, ensure_dir, format_price

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_name TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    status TEXT NOT NULL,
    config_mode TEXT,
    fallback_used INTEGER DEFAULT 0,
    title TEXT,
    price INTEGER,
    seller_name TEXT,
    product_id TEXT,
    product_type INTEGER,
    product_url TEXT,
    raw_payload TEXT,
    error_message TEXT,
    price_change_status TEXT,
    prev_price INTEGER,
    price_delta INTEGER,
    price_delta_pct REAL,
    alert_triggered INTEGER DEFAULT 0,
    image_url TEXT,
    search_rank INTEGER
);

CREATE INDEX IF NOT EXISTS idx_observations_target_time
ON observations(target_name, collected_at DESC);

CREATE TABLE IF NOT EXISTS ranking_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    rank INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    title TEXT,
    price INTEGER,
    seller_name TEXT,
    product_id TEXT,
    product_type INTEGER,
    product_url TEXT,
    image_url TEXT,
    is_ad INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ranking_query_time
ON ranking_history(query, collected_at DESC);
"""

# 인증점 관련 컬럼들은 스키마 정의에서 제거하지만, 
# 마이그레이션 로직에서는 DB 호환성을 위해 남겨두거나 필요한 것만 유지합니다.
_MIGRATION_COLUMNS = [
    ("config_mode", "TEXT"),
    ("fallback_used", "INTEGER DEFAULT 0"),
    ("price_change_status", "TEXT"),
    ("prev_price", "INTEGER"),
    ("price_delta", "INTEGER"),
    ("price_delta_pct", "REAL"),
    ("alert_triggered", "INTEGER DEFAULT 0"),
    ("product_id", "TEXT"),
    ("image_url", "TEXT"),
    ("search_rank", "INTEGER"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB에 신규 컬럼이 없으면 ALTER TABLE로 추가합니다."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(observations)").fetchall()}
    for col_name, col_type in _MIGRATION_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {col_name} {col_type}")
    conn.commit()


class ObservationStore:
    def __init__(self, db_path: str) -> None:
        db_path = str(Path(db_path).resolve())
        ensure_dir(Path(db_path).parent)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        _migrate(self.conn)

    def insert(self, row: dict[str, Any]) -> None:
        payload = dict(row)
        if "raw_payload" in payload and not isinstance(payload["raw_payload"], str):
            payload["raw_payload"] = dump_json(payload["raw_payload"])

        columns = [
            "target_name",
            "source_mode",
            "collected_at",
            "success",
            "status",
            "config_mode",
            "fallback_used",
            "title",
            "price",
            "seller_name",
            "product_id",
            "product_type",
            "product_url",
            "raw_payload",
            "error_message",
            "price_change_status",
            "prev_price",
            "price_delta",
            "price_delta_pct",
            "alert_triggered",
            "image_url",
            "search_rank",
        ]
        values = [payload.get(col) for col in columns]
        self.conn.execute(
            f"INSERT INTO observations ({','.join(columns)}) VALUES ({','.join(['?']*len(columns))})",
            values,
        )
        self.conn.commit()

    def get_latest_success(self, target_name: str) -> dict[str, Any] | None:
        """특정 상품의 가장 최근 성공 수집 기록(success=1)을 반환합니다."""
        row = self.conn.execute(
            """
            SELECT * FROM observations
            WHERE target_name = ? AND success = 1 AND price IS NOT NULL
            ORDER BY collected_at DESC, id DESC
            LIMIT 1
            """,
            (target_name,),
        ).fetchone()
        return dict(row) if row else None

    def get_price_history(self, target_name: str, limit: int = 20) -> list[dict[str, Any]]:
        """특정 상품의 최근 수집 이력을 반환합니다."""
        rows = self.conn.execute(
            """
            SELECT * FROM observations
            WHERE target_name = ?
            ORDER BY collected_at DESC, id DESC
            LIMIT ?
            """,
            (target_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_dashboard_data(self, targets: list[TargetConfig]) -> dict[str, Any]:
        """대시보드 시각화용 통합 데이터를 반환합니다 (7일/30일/90일 분석 포함)."""
        target_map = {t.name: t for t in targets}
        target_names = list(target_map.keys())

        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "products": []
        }

        for name in target_names:
            t_config = target_map[name]

            # 1. 최신 정보 가져오기
            latest = self.get_latest_success(name)
            if not latest:
                continue

            # 2. 분석용 데이터 추출 (전체 히스토리 분석 가능하도록 필터 제거)
            hist_all = self.conn.execute(
                """
                SELECT collected_at, price 
                FROM observations 
                WHERE target_name = ? AND success = 1 AND price IS NOT NULL
                ORDER BY collected_at ASC
                """, (name,)
            ).fetchall()

            # 3. 역대 최저/최고가 계산 (전체 히스토리 대상)
            stats_all = self.conn.execute(
                """
                SELECT MIN(price) as min_p, MAX(price) as max_p
                FROM observations
                WHERE target_name = ? AND success = 1 AND price IS NOT NULL
                """, (name,)
            ).fetchone()
            all_time_low = stats_all["min_p"]
            all_time_high = stats_all["max_p"]

            if not hist_all:
                continue
            
            # 기간별 평균 계산 함수 (데이터가 부족하면 None 반환)
            def calc_avg(days):
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                prices = [r["price"] for r in hist_all if datetime.fromisoformat(r["collected_at"].replace('Z', '+00:00')) >= cutoff]
                return round(sum(prices) / len(prices)) if prices else None

            product_data = {
                "name": name,
                "category": t_config.category,
                "rank_query": t_config.rank_query,
                "current_price": latest["price"],
                "seller": latest["seller_name"] or "네이버",
                "status": latest["price_change_status"],
                "change_pct": latest["price_delta_pct"],
                "product_id": latest["product_id"],
                "avg_7d": calc_avg(7),
                "avg_30d": calc_avg(30),
                "avg_90d": calc_avg(90),
                "all_time_low": all_time_low,
                "all_time_high": all_time_high,
                "image_url": latest["image_url"],
                "search_rank": latest.get("search_rank"),
                "history": [
                    {"t": r["collected_at"], "p": r["price"]} for r in hist_all[-500:] # 최근 500개 데이터 포인트로 확장
                ]
            }
            data["products"].append(product_data)

        return data

    def export_dashboard_json(self, out_path: str, categories: dict[str, str] | None = None) -> str:
        """대시보드 데이터를 JSON 파일로 저장합니다."""
        data = self.get_dashboard_data(categories=categories)
        out = Path(out_path).resolve()
        ensure_dir(out.parent)
        out.write_text(dump_json(data), encoding="utf-8")
        return str(out)

    def export_latest_csv(self, out_path: str) -> str:
        query = """
        WITH ranked AS (
          SELECT
            *,
            ROW_NUMBER() OVER (
              PARTITION BY target_name
              ORDER BY collected_at DESC, id DESC
            ) AS rn
          FROM observations
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY target_name;
        """
        rows = self.conn.execute(query).fetchall()
        out = Path(out_path).resolve()
        ensure_dir(out.parent)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "target_name", "collected_at", "config_mode", "source_mode", "fallback_used", "success", "status",
                "title", "price", "seller_name", "price_change_status", "prev_price",
                "price_delta", "price_delta_pct", "product_url", "error_message", "image_url", "search_rank"
            ])
            for r_raw in rows:
                r = dict(r_raw)
                writer.writerow([
                    r["target_name"], r["collected_at"], r.get("config_mode"), r["source_mode"], r.get("fallback_used", 0), r["success"], r["status"],
                    r["title"], r["price"], r["seller_name"], r["price_change_status"], r["prev_price"],
                    r["price_delta"], r["price_delta_pct"], r["product_url"], r["error_message"],
                    r.get("image_url"), r.get("search_rank")
                ])
        return str(out)

    def export_html_report(self, out_path: str, limit: int = 20) -> str:
        """HTML 리포트 생성 (이스케이프 및 폴백 정보 추가)"""
        import html as py_html
        target_names = [
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT target_name FROM observations ORDER BY target_name"
            ).fetchall()
        ]

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sections = []
        for name in target_names:
            history = self.get_price_history(name, limit=limit)
            rows_html = []
            for rec in history:
                status_cls = rec.get("price_change_status") or "UNKNOWN"
                status_color = "#94a3b8"  # 기본 회색
                if status_cls == "PRICE_DOWN": status_color = "#22c55e"  # 초록
                elif status_cls == "PRICE_UP": status_color = "#ef4444"  # 빨강
                elif status_cls == "PRICE_SAME": status_color = "#6b7280" # 중립 회색
                
                row_style = ""
                if not rec.get("success"):
                    row_style = 'style="background: #2d0a0a"'  # 더 어두운 빨강 배경 (success=0)

                delta_pct = rec.get("price_delta_pct")
                pct_str = f"{delta_pct:+.1f}%" if delta_pct is not None else "-"
                delta_str = f"{rec.get('price_delta'):+,}" if rec.get("price_delta") is not None else "-"

                cfg_m = py_html.escape(str(rec.get("config_mode") or "-"))
                src_m = py_html.escape(str(rec.get("source_mode") or "-"))
                route_html = f"{cfg_m} &rarr; {src_m}"
                if rec.get("fallback_used"):
                    route_html += ' <span style="background:#f59e0b; color:#fff; font-size:10px; padding:1px 4px; border-radius:4px; font-weight:700">FALLBACK</span>'
                
                rows_html.append(f"""
        <tr {row_style}>
          <td style="font-size:11px">{py_html.escape((rec.get('collected_at') or '')[:19].replace('T', ' '))}</td>
          <td style="font-size:12px; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap" title="{py_html.escape(rec.get('title') or '')}">{py_html.escape(rec.get('title') or '-')}</td>
          <td>{py_html.escape(rec.get('seller_name') or '-')}</td>
          <td style="font-size:11px; color:#94a3b8">{route_html}</td>
          <td style="font-weight:700">{format_price(rec.get('price'))}</td>
          <td style="color:#94a3b8">{format_price(rec.get('prev_price'))}</td>
          <td style="color:{status_color}">{delta_str}</td>
          <td style="color:{status_color}; font-weight:700">{pct_str}</td>
          <td style="font-size:12px; color:#94a3b8">{py_html.escape(str(rec.get('search_rank') or '-'))}</td>
          <td style="color:{status_color}; font-size:12px; font-weight:700">{py_html.escape(str(status_cls or ""))}</td>
          <td style="font-size:11px; color:#cbd5e1">{py_html.escape(str(rec.get('status') or ''))}</td>
        </tr>""")

            section = f"""
  <section style="margin-bottom:40px">
    <h2 style="color:#f1f5f9; border-bottom:1px solid #334155; padding-bottom:8px; margin-bottom:12px">{name}</h2>
    <div style="overflow-x:auto">
      <table style="width:100%; border-collapse:collapse; font-size:14px">
        <thead>
            <tr style="background:#1e293b; color:#94a3b8">
            <th style="padding:10px; text-align:left">수집시각</th>
            <th style="padding:10px; text-align:left">상품명</th>
            <th style="padding:10px; text-align:left">판매자</th>
            <th style="padding:10px; text-align:left">수집경로</th>
            <th style="padding:10px; text-align:left">현재가</th>
            <th style="padding:10px; text-align:left">이전가</th>
            <th style="padding:10px; text-align:left">변동액</th>
            <th style="padding:10px; text-align:left">변동률</th>
            <th style="padding:10px; text-align:left">순위</th>
            <th style="padding:10px; text-align:left">변동상태</th>
            <th style="padding:10px; text-align:left">수집상태</th>
          </tr>
        </thead>
        <tbody style="color:#e2e8f0">
          {''.join(rows_html)}
        </tbody>
      </table>
    </div>
  </section>"""
            sections.append(section)

        html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>네이버 쇼핑 가격 추적 리포트</title>
<style>
  body {{ background: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; padding: 30px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 12px; border-bottom: 1px solid #1e293b; }}
  tr:hover {{ background: #1e293b; }}
</style>
</head>
<body>
  <h1 style="margin-bottom:8px">📊 가격 추적 리포트</h1>
  <p style="color:#64748b; margin-bottom:30px">생성: {now_str}</p>
  {''.join(sections)}
</body>
</html>"""
        out = Path(out_path).resolve()
        ensure_dir(out.parent)
        out.write_text(html_content, encoding="utf-8")
        return str(out)

    def close(self) -> None:
        self.conn.close()


class RankingStore:
    def __init__(self, db_path: str) -> None:
        db_path = str(Path(db_path).resolve())
        ensure_dir(Path(db_path).parent)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        
        # [Migration] is_ad 컬럼 추가 (기존 DB 대응)
        try:
            self.conn.execute("ALTER TABLE ranking_history ADD COLUMN is_ad INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass # 이미 컬럼이 존재함
            
        self.conn.commit()

    def insert_ranking_batch(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        columns = [
            "query", "rank", "collected_at", "title", "price",
            "seller_name", "product_id", "product_type", "product_url", "image_url", "is_ad"
        ]
        
        values = []
        for row in rows:
            values.append([row.get(col) for col in columns])
            
        self.conn.executemany(
            f"INSERT INTO ranking_history ({','.join(columns)}) VALUES ({','.join(['?'] * len(columns))})",
            values
        )
        self.conn.commit()

    def get_latest_rankings(self, query: str, limit: int = 15) -> list[dict[str, Any]]:
        # 가장 최근 수집된 시간을 찾음
        recent_time_row = self.conn.execute(
            "SELECT collected_at FROM ranking_history WHERE query = ? ORDER BY collected_at DESC LIMIT 1", 
            (query,)
        ).fetchone()
        
        if not recent_time_row:
            return []
            
        recent_time = recent_time_row["collected_at"]
        
        rows = self.conn.execute(
            """
            SELECT * FROM ranking_history 
            WHERE query = ? AND collected_at = ?
            ORDER BY rank ASC
            LIMIT ?
            """,
            (query, recent_time, limit)
        ).fetchall()
        
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()
