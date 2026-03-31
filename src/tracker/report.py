import sqlite3
from datetime import datetime, timedelta, timezone
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib

from .config import TargetConfig
from .util import format_price

logger = logging.getLogger("tracker.report")

def send_daily_report(db_path: str, email_from: str, email_password: str, email_to: str | list[str], targets: list[TargetConfig]) -> bool:
    if not all([email_from, email_password, email_to]):
        logger.info("이메일 설정이 없어 데일리 리포트 알림을 건너뜁니다.")
        return False
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 10일치 날짜 계산 (KST 기준)
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc + timedelta(hours=9)
    # 오늘 포함 گذشته 10일
    dates_kst = [(now_kst - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(9, -1, -1)]
    
    cutoff_utc = now_utc - timedelta(days=12)
    
    rows = conn.execute(
        """
        SELECT target_name, collected_at, price 
        FROM observations 
        WHERE success = 1 AND price IS NOT NULL AND collected_at >= ?
        """,
        (cutoff_utc.isoformat(),)
    ).fetchall()
    
    daily_min = {}
    for r in rows:
        t_name = r["target_name"]
        t_utc = datetime.fromisoformat(r["collected_at"].replace('Z', '+00:00'))
        t_kst = t_utc + timedelta(hours=9)
        d_str = t_kst.strftime("%Y-%m-%d")
        
        if d_str not in daily_min:
            daily_min[d_str] = {}
        if t_name not in daily_min[d_str]:
            daily_min[d_str][t_name] = r["price"]
        else:
            daily_min[d_str][t_name] = min(daily_min[d_str][t_name], r["price"])
            
    conn.close()
    
    # HTML 빌드
    target_names = [t.name for t in targets]
    
    # 테이블 헤더
    header_html = ''.join(f'<th style="padding:10px;text-align:right;border:1px solid #ddd;background:#f8f9fa;">{d[5:]}</th>' for d in dates_kst)
    
    rows_html = ""
    for name in target_names:
        row_cells = []
        prev_price = None
        for i, d in enumerate(dates_kst):
            price_val = daily_min.get(d, {}).get(name)
            if price_val is not None:
                color = "#000"
                if prev_price is not None:
                    if price_val < prev_price: color = "#2563eb" # 하락(파란색)
                    elif price_val > prev_price: color = "#dc2626" # 상승(빨간색)
                row_cells.append(f'<td style="padding:10px;text-align:right;border:1px solid #ddd;color:{color};">{format_price(price_val)}</td>')
                prev_price = price_val
            else:
                row_cells.append('<td style="padding:10px;text-align:right;border:1px solid #ddd;color:#aaa;">-</td>')
        
        rows_html += f"<tr><td style='padding:10px;border:1px solid #ddd;font-weight:bold;'>{name}</td>{''.join(row_cells)}</tr>"

    html_body = f"""
    <html><body style="font-family:sans-serif;max-width:900px;margin:auto;padding:20px">
    <h2 style="color:#1e293b">📊 최근 10일 모델별 최저가 일일 리포트</h2>
    <p style="color:#64748b;font-size:14px;margin-bottom:20px">KST 기준, 매일 수집된 가격 중 최저가를 보여줍니다.</p>
    <div style="overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse; font-size:13px; min-width:800px;">
            <thead>
                <tr>
                    <th style="padding:10px;text-align:left;border:1px solid #ddd;background:#f8f9fa;width:180px;">모델명</th>
                    {header_html}
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
    <div style="margin-top:32px;text-align:center">
        <a href="https://eungseop2.github.io/Lowest-Price-Tracker/" 
           style="background-color:#2563eb;color:white;padding:12px 24px;text-decoration:none;border-radius:6px;font-weight:bold;display:inline-block">
           📊 대시보드 바로가기
        </a>
    </div>
    </body></html>
    """
    
    subject = f"[Price Insight Pro] 최근 10일 가격 동향 리포트 ({dates_kst[-1]})"
    
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = email_from
        
        if isinstance(email_to, list):
            recipients = [e.strip() for e in email_to if e.strip()]
        else:
            recipients = [e.strip() for e in email_to.split(",") if e.strip()]
            
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(email_from, email_password)
            server.sendmail(email_from, recipients, msg.as_string())

        logger.info("데일리 리포트 이메일 발송 완료 → %s", ", ".join(recipients))
        return True
    except Exception as e:
        logger.error("데일리 리포트 이메일 발송 실패: %s", e)
        return False
