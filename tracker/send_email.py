"""
Kronos daily email sender.
Reads scores.json and sends an HTML summary email via Gmail SMTP.

Usage:
  python tracker/send_email.py

Env vars required:
  GMAIL_USERNAME     — Gmail address to send from (must have an App Password enabled)
  GMAIL_APP_PASSWORD — Gmail App Password (Settings → Security → App passwords)

Optional:
  RECIPIENT_EMAIL    — defaults to lemleysergio@gmail.com
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCORES_FILE = REPO_ROOT / "scores.json"

RECIPIENT = os.environ.get("RECIPIENT_EMAIL", "lemleysergio@gmail.com")
GMAIL_USER = os.environ.get("GMAIL_USERNAME")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")


def load_scores() -> list[dict]:
    if not SCORES_FILE.exists():
        return []
    with open(SCORES_FILE) as f:
        return json.load(f)


def compute_streak(records: list[dict]) -> tuple[int, bool]:
    """Return (length, is_correct_streak) for the current streak."""
    if not records:
        return 0, True
    last_val = records[-1].get("direction_correct", False)
    streak = 1
    for r in reversed(records[:-1]):
        if r.get("direction_correct") == last_val:
            streak += 1
        else:
            break
    return streak, last_val


def bottom_line(pct: float, total: int, streak: int, streak_correct: bool) -> str:
    if total < 5:
        return (
            f"Only {total} day{'s' if total != 1 else ''} in — way too early to call "
            f"anything. Come back in a week."
        )
    if pct >= 65:
        return (
            f"{pct}% over {total} days — ok that's actually impressive, Kronos is "
            f"genuinely beating a coin flip. I'd pay attention."
        )
    if pct >= 60:
        return (
            f"{pct}% is solid — beating random by a real margin over {total} days. "
            f"Not financial advice but it's doing something right."
        )
    if pct >= 55:
        return (
            f"{pct}% — slightly above a coin flip, could still be noise at {total} "
            f"days. Mildly interesting, keep watching."
        )
    if pct >= 45:
        return (
            f"{pct}% across {total} days — basically indistinguishable from guessing "
            f"right now. Don't read too much into it yet."
        )
    return (
        f"Oof — {pct}% means Kronos is actually worse than flipping a coin right now "
        f"over {total} days. Hopefully just a rough patch."
    )


def build_html(records: list[dict], today_str: str) -> str:
    yesterday = records[-1]
    direction_correct = yesterday.get("direction_correct", False)
    went_up = yesterday.get("went_up", False)
    kronos_said = "UP" if yesterday.get("upside_prob", 0.5) > 0.5 else "DOWN"
    btc_went = "UP" if went_up else "DOWN"
    price_change = yesterday.get("price_change_pct", 0.0)

    total = len(records)
    correct = sum(1 for r in records if r.get("direction_correct"))
    pct = round(correct / total * 100, 1)

    streak_count, streak_correct = compute_streak(records)

    history_emojis = "".join(
        "✅" if r.get("direction_correct") else "❌" for r in records
    )

    yesterday_bg = "#e8f5e9" if direction_correct else "#ffebee"
    yesterday_border = "#4caf50" if direction_correct else "#f44336"
    yesterday_label = "✅ RIGHT" if direction_correct else "❌ WRONG"

    bar_color = (
        "#4caf50" if pct > 55
        else ("#ff9800" if pct >= 45 else "#f44336")
    )

    streak_label = "correct ✅" if streak_correct else "wrong ❌"
    streak_text = f"{streak_count} day{'s' if streak_count != 1 else ''} in a row {streak_label}"

    verdict = bottom_line(pct, total, streak_count, streak_correct)

    return f"""<div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">

<h2 style="color: #1a1a2e;">&#128202; Kronos BTC Tracker</h2>
<p style="color: #888; font-size: 13px;">{today_str} &middot; Every day counts</p>

<!-- YESTERDAY -->
<div style="background: {yesterday_bg}; border-left: 4px solid {yesterday_border}; padding: 16px; border-radius: 8px; margin: 16px 0;">
<p style="margin:0; font-size: 20px;">{yesterday_label} yesterday</p>
<p style="margin: 8px 0 0; color: #444;">Kronos said {kronos_said}. BTC went {btc_went} {abs(price_change):.1f}%.</p>
</div>

<!-- FULL HISTORY -->
<div style="background: #f9f9f9; padding: 16px; border-radius: 8px; margin: 16px 0;">
<p style="margin: 0 0 10px; font-weight: bold;">Full history</p>
<p style="font-family: monospace; font-size: 22px; letter-spacing: 3px; margin: 0; line-height: 1.8;">{history_emojis}</p>
<p style="margin: 10px 0 0; font-size: 12px; color: #999;">&#10003; = Kronos called it &middot; &#10007; = Kronos missed &middot; newest &rarr;</p>
</div>

<!-- SCOREBOARD -->
<div style="background: #f5f5f5; padding: 16px; border-radius: 8px; margin: 16px 0;">
<p style="margin: 0 0 4px; font-weight: bold;">Overall score</p>
<p style="font-size: 32px; font-weight: bold; margin: 0; color: #1a1a2e;">{pct}% <span style="font-size: 14px; font-weight: normal; color: #666;">({correct} of {total} days)</span></p>
<div style="background: #ddd; border-radius: 4px; height: 10px; margin: 10px 0;">
<div style="background: {bar_color}; width: {min(pct, 100):.1f}%; height: 10px; border-radius: 4px;"></div>
</div>
<p style="font-size: 12px; color: #999; margin: 0;">&#127922; Coin flip = 50% &middot; &#128578; Decent = 55% &middot; &#128293; Good = 60%+</p>
</div>

<!-- STREAK -->
<div style="padding: 0 0 16px;">
<p style="margin: 0;"><strong>Current streak:</strong> {streak_text}</p>
</div>

<!-- VERDICT -->
<div style="background: #e3f2fd; padding: 16px; border-radius: 8px;">
<p style="margin: 0 0 4px; font-weight: bold;">&#127919; Bottom line</p>
<p style="margin: 0;">{verdict}</p>
</div>

<p style="color: #bbb; font-size: 11px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 12px;">Kronos Tracker &middot; github.com/lemleysergio-cloud/kronos-tracker</p>
</div>"""


def send_email(html_body: str, today_str: str) -> None:
    if not GMAIL_USER or not GMAIL_PASS:
        print(
            "ERROR: Set GMAIL_USERNAME and GMAIL_APP_PASSWORD env vars.\n"
            "  Generate an App Password at: myaccount.google.com/apppasswords"
        )
        sys.exit(1)

    subject = f"📊 Kronos — {today_str}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())

    print(f"✓ Email sent → {RECIPIENT}  [{subject}]")


def main() -> None:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = load_scores()

    if not records:
        print("No scored records in scores.json yet — skipping email.")
        return

    html = build_html(records, today_str)
    send_email(html, today_str)


if __name__ == "__main__":
    main()
