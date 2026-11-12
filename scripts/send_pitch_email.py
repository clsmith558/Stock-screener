#!/usr/bin/env python3
"""
Daily Stock Pitch Ledger Logger & Email Dispatcher

1. Logs a complete point-in-time snapshot of the pitched stock (valuation, short interest,
   analyst revisions, Piotroski F-Score, SBC-adjusted FCF yield, share count delta,
   Capex/D&A, Sloan accruals, interest coverage, liquidity & ML scores) to `pitch_ledger`
   in `asx_announcements_cache.db` and updates realized forward returns on prior pitches.
2. Dispatches the Markdown brief via SMTP if credentials are provided in `.env`.
"""

import os
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv

# Ensure screener root is on sys.path
SCREENER_ROOT = Path(__file__).resolve().parent.parent
if str(SCREENER_ROOT) not in sys.path:
    sys.path.insert(0, str(SCREENER_ROOT))

load_dotenv(SCREENER_ROOT / ".env")

RECIPIENT = (
    os.environ.get("PITCH_EMAIL_RECIPIENT")
    or os.environ.get("SEC_EDGAR_IDENTITY")
    or os.environ.get("SCREENER_CONTACT_EMAIL", "clsmith558@gmail.com")
)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")


def send_pitch(file_path: str):
    p = Path(file_path)
    if not p.exists():
        print(f"[ERROR] Brief file not found: {file_path}")
        return False

    # 1. Record point-in-time pitch snapshot & backfill prior pitch outcomes
    try:
        from data.pitch_ledger import record_pitch_from_markdown

        rec = record_pitch_from_markdown(p, fetch_live_yf=True)
        print(
            f"[LEDGER] Logged point-in-time snapshot for {rec.get('ticker')} ({rec.get('pitch_date')}): "
            f"price={rec.get('price_at_pitch')}, FwdPE={rec.get('forward_pe')}, "
            f"SBC_Adj_FCFYield={rec.get('sbc_adjusted_fcf_yield')}%, "
            f"ShortFloat={rec.get('short_pct_float')}%, "
            f"PiotroskiF={rec.get('piotroski_f_score')}/9, "
            f"Capex/DA={rec.get('capex_to_da_ratio')}x"
        )
    except Exception as ex:
        print(f"[WARN] Pitch ledger logging warning: {ex}")

    with open(p, "r", encoding="utf-8") as f:
        md_content = f.read()

    subject = f"Daily Stock Pitch: {p.stem}"

    if not SMTP_USER or not SMTP_PASS:
        print("[INFO] SMTP credentials (SMTP_USER / SMTP_PASS) not configured in .env.")
        print(f"[INFO] Brief prepared for: {RECIPIENT}")
        print(
            "[INFO] To enable live email dispatch, add SMTP_USER and SMTP_PASS "
            "(e.g. Gmail App Password) to C:\\Grok\\Stock screener\\.env"
        )
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = RECIPIENT

        part1 = MIMEText(md_content, "plain")
        msg.attach(part1)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, RECIPIENT, msg.as_string())

        print(f"[SUCCESS] Pitch successfully emailed to {RECIPIENT}")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send email: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python send_pitch_email.py <path_to_markdown_pitch>")
        sys.exit(1)
    send_pitch(sys.argv[1])
