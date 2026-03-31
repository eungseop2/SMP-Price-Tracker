import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("tracker.notifier")


def send_price_alert(
    changes: list[dict],
    email_from: str,
    email_password: str,
    email_to: str | list[str],
) -> bool:
    """가격 변동 항목 리스트를 이메일로 알립니다. 성공 시 True 반환."""
    if not all([email_from, email_password, email_to]):
        logger.info("이메일 설정이 없어 알림을 건너뜁니다.")
        return False

    downs = [c for c in changes if c.get("price_change_status") == "PRICE_DOWN"]
    ups = [c for c in changes if c.get("price_change_status") == "PRICE_UP"]

    if not downs and not ups:
        logger.info("가격 변동 없음 - 이메일 발송 생략")
        return False

    subject = _build_subject(downs, ups)
    html_body = _build_html(downs, ups)

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

        logger.info("가격 변동 이메일 발송 완료 → %s", ", ".join(recipients))
        return True
    except Exception as e:
        logger.error("이메일 발송 실패: %s", e)
        return False


def _build_subject(downs: list, ups: list) -> str:
    parts = []
    if downs:
        parts.append(f"📉 가격 하락 {len(downs)}건")
    if ups:
        parts.append(f"📈 가격 상승 {len(ups)}건")
    return f"[Price Insight Pro] {' / '.join(parts)}"


def _build_html(downs: list, ups: list) -> str:
    rows = ""

    def make_rows(items, color, icon):
        result = ""
        for item in items:
            name = item.get("target_name", "")
            price = item.get("price", 0)
            prev = item.get("prev_price") or 0
            url = item.get("product_url", "#")
            pct = item.get("price_delta_pct")
            pct_str = f"{pct:+.1f}%" if pct is not None else ""
            result += f"""
            <tr>
                <td style="padding:8px;border-bottom:1px solid #eee">
                    <a href="{url}" style="text-decoration:none;color:#1e293b">{icon} {name}</a>
                </td>
                <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">
                    <s style="color:#999">{prev:,}원</s> →
                    <b style="color:{color}">{price:,}원</b>
                </td>
                <td style="padding:8px;border-bottom:1px solid #eee;text-align:right;color:{color}">
                    <b>{pct_str}</b>
                </td>
            </tr>"""
        return result

    rows += make_rows(downs, "#16a34a", "📉")
    rows += make_rows(ups, "#dc2626", "📈")

    return f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:auto;padding:20px">
    <h2 style="color:#1e293b">💡 Price Insight Pro 가격 변동 알림</h2>
    <table width="100%" style="border-collapse:collapse;margin-top:16px">
        <thead>
            <tr style="background:#f1f5f9">
                <th style="padding:8px;text-align:left">상품명</th>
                <th style="padding:8px;text-align:right">가격</th>
                <th style="padding:8px;text-align:right">변동률</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    <div style="margin-top:24px;text-align:center">
        <a href="https://eungseop2.github.io/Lowest-Price-Tracker/" 
           style="background-color:#2563eb;color:white;padding:12px 24px;text-decoration:none;border-radius:6px;font-weight:bold;display:inline-block">
           📊 대시보드 바로가기
        </a>
    </div>
    <p style="color:#94a3b8;font-size:12px;margin-top:24px">
        Price Insight Pro · 자동 발송 이메일입니다.
    </p>
    </body></html>
    """
