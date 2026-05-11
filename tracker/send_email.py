"""
Kronos daily email sender.
Reads scores.json and pending.json, sends an HTML summary email via Gmail SMTP.

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
PENDING_FILE = REPO_ROOT / "pending.json"

RECIPIENT = os.environ.get("RECIPIENT_EMAIL", "lemleysergio@gmail.com")
GMAIL_USER = os.environ.get("GMAIL_USERNAME")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")


def load_scores() -> list[dict]:
    if not SCORES_FILE.exists():
        return []
    with open(SCORES_FILE) as f:
        return json.load(f)


def load_pending() -> dict | None:
    if not PENDING_FILE.exists():
        return None
    with open(PENDING_FILE) as f:
        return json.load(f)


def compute_streak(records: list[dict]) -> tuple[int, bool]:
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


def build_pending_block(pending: dict) -> str:
    upside_pct = round(pending["upside_prob"] * 100, 1)
    vol_pct = round(pending["vol_amplification_prob"] * 100, 1)
    ts = pending.get("prediction_timestamp", "")[:16]

    if pending["upside_prob"] > 0.5:
        dir_label = f"&#9650; BULLISH ({upside_pct}%)"
        dir_color = "#2e7d32"
    elif pending["upside_prob"] < 0.5:
        dir_label = f"&#9660; BEARISH ({upside_pct}%)"
        dir_color = "#c62828"
    else:
        dir_label = f"&#8212; NEUTRAL ({upside_pct}%)"
        dir_color = "#666"

    vol_label = (
        f"&#128308; Choppy expected ({vol_pct}%)"
        if pending["vol_amplification_prob"] > 0.5
        else f"&#128994; Calm expected ({vol_pct}%)"
    )

    return f"""
<div style="background:#e3f2fd; border-left:4px solid #1976d2; padding:16px; border-radius:8px; margin:16px 0;">
  <p style="margin:0 0 6px; font-weight:bold; color:#1a1a2e;">&#128313; Today's prediction <span style="font-size:12px;font-weight:normal;color:#888">(results in tomorrow's report)</span></p>
  <p style="margin:4px 0; font-size:15px; color:{dir_color};"><strong>Direction:</strong> {dir_label}</p>
  <p style="margin:4px 0; font-size:15px; color:#444;"><strong>Volatility:</strong> {vol_label}</p>
  <p style="margin:8px 0 0; font-size:11px; color:#999;">Kronos timestamp: {ts} UTC</p>
</div>"""


def build_history_table(records: list[dict]) -> str:
    rows = ""
    for r in reversed(records):
        date = r.get("prediction_timestamp", "")[:10]
        upside_pct = round(r["upside_prob"] * 100, 1)
        vol_pct = round(r["vol_amplification_prob"] * 100, 1)
        price_change = r.get("price_change_pct", 0.0)
        brier = r.get("brier_score", 0.0)
        vol_brier = r.get("vol_brier_score", 0.0)
        dir_ok = r.get("direction_correct", False)
        vol_ok = r.get("vol_correct", False)

        dir_icon = "&#9989;" if dir_ok else "&#10060;"
        vol_icon = "&#9989;" if vol_ok else "&#10060;"
        change_color = "#2e7d32" if price_change > 0 else "#c62828" if price_change < 0 else "#666"
        change_str = f"{price_change:+.2f}%"

        if upside_pct >= 65:
            up_bg = "#e8f5e9"; up_color = "#2e7d32"
        elif upside_pct <= 35:
            up_bg = "#ffebee"; up_color = "#c62828"
        else:
            up_bg = "#f5f5f5"; up_color = "#444"

        rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0;">
          <td style="padding:8px 10px;font-size:12px;color:#888;white-space:nowrap;">{date}</td>
          <td style="padding:8px 10px;text-align:center;background:{up_bg};color:{up_color};font-weight:500;font-size:13px;">{upside_pct}%</td>
          <td style="padding:8px 10px;text-align:center;font-size:13px;color:#444;">{vol_pct}%</td>
          <td style="padding:8px 10px;text-align:center;font-size:13px;font-weight:500;color:{change_color};">{change_str}</td>
          <td style="padding:8px 10px;text-align:center;font-size:15px;">{dir_icon}</td>
          <td style="padding:8px 10px;text-align:center;font-size:15px;">{vol_icon}</td>
          <td style="padding:8px 10px;text-align:center;font-size:12px;color:#888;">{brier:.3f} / {vol_brier:.3f}</td>
        </tr>"""

    return f"""
<div style="background:#f9f9f9; padding:16px; border-radius:8px; margin:16px 0;">
  <p style="margin:0 0 10px; font-weight:bold;">Full prediction history</p>
  <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;">
      <thead>
        <tr style="background:#eeeeee;">
          <th style="padding:8px 10px;text-align:left;font-size:11px;color:#666;font-weight:600;">Date</th>
          <th style="padding:8px 10px;text-align:center;font-size:11px;color:#666;font-weight:600;">Upside %</th>
          <th style="padding:8px 10px;text-align:center;font-size:11px;color:#666;font-weight:600;">Vol amp %</th>
          <th style="padding:8px 10px;text-align:center;font-size:11px;color:#666;font-weight:600;">BTC &#916;</th>
          <th style="padding:8px 10px;text-align:center;font-size:11px;color:#666;font-weight:600;">Dir</th>
          <th style="padding:8px 10px;text-align:center;font-size:11px;color:#666;font-weight:600;">Vol</th>
          <th style="padding:8px 10px;text-align:center;font-size:11px;color:#666;font-weight:600;">Brier (dir/vol)</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <p style="margin:10px 0 0;font-size:11px;color:#999;">Upside % green &#8805;65% (confident bullish) · red &#8804;35% (confident bearish) · newest first</p>
</div>"""


def build_html(records: list[dict], pending: dict | None, today_str: str) -> str:
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

    bar_color = "#4caf50" if pct > 55 else ("#ff9800" if pct >= 45 else "#f44336")

    streak_label = "correct ✅" if streak_correct else "wrong ❌"
    streak_text = f"{streak_count} day{'s' if streak_count != 1 else ''} in a row {streak_label}"

    verdict = bottom_line(pct, total, streak_count, streak_correct)

    pending_block = build_pending_block(pending) if pending else ""
    history_table = build_history_table(records)

    return f"""<div style="font-family: Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 20px;">

<h2 style="color: #1a1a2e;">&#128202; Kronos BTC Tracker</h2>
<p style="color: #888; font-size: 13px;">{today_str} &middot; Every day counts</p>

{pending_block}

<!-- YESTERDAY -->
<div style="background: {yesterday_bg}; border-left: 4px solid {yesterday_border}; padding: 16px; border-radius: 8px; margin: 16px 0;">
  <p style="margin:0; font-size: 20px;">{yesterday_label} yesterday</p>
  <p style="margin: 8px 0 0; color: #444;">Kronos said {kronos_said}. BTC went {btc_went} {abs(price_change):.1f}%.</p>
</div>

{history_table}

<!-- EMOJI STRIP -->
<div style="background: #f9f9f9; padding: 12px 16px; border-radius: 8px; margin: 16px 0;">
  <p style="font-family: monospace; font-size: 22px; letter-spacing: 3px; margin: 0; line-height: 1.8;">{history_emojis}</p>
  <p style="margin: 6px 0 0; font-size: 12px; color: #999;">&#10003; = correct &middot; &#10007; = missed &middot; left = oldest, right = newest</p>
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
    pending = load_pending()

    if not records:
        print("No scored records in scores.json yet — skipping email.")
        return

    html = build_html(records, pending, today_str)
    send_email(html, today_str)


if __name__ == "__main__":
    main()
