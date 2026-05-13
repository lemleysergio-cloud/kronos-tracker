"""
Kronos daily email sender — Hourly Edition.
Reads scores.json and pending.json, sends a rich HTML report grouped by day.

Env vars required:
  GMAIL_USERNAME     — Gmail address to send from
  GMAIL_APP_PASSWORD — Gmail App Password

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


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def compute_stats(records):
    if not records:
        return {}
    n = len(records)
    dir_correct = sum(1 for r in records if r.get("direction_correct"))
    vol_correct = sum(1 for r in records if r.get("vol_correct"))
    avg_brier = sum(r.get("brier_score", 0.5) for r in records) / n
    avg_vol_brier = sum(r.get("vol_brier_score", 0.5) for r in records) / n
    streak = 0
    for r in reversed(records):
        if r.get("direction_correct"):
            streak += 1
        else:
            break
    return {
        "n": n,
        "dir_correct": dir_correct,
        "direction_accuracy_pct": round(dir_correct / n * 100, 1),
        "vol_accuracy_pct": round(vol_correct / n * 100, 1),
        "avg_brier_score": round(avg_brier, 4),
        "avg_vol_brier_score": round(avg_vol_brier, 4),
        "correct_streak": streak,
    }


def group_by_day(records):
    days = {}
    for r in records:
        date = r.get("prediction_timestamp", "")[:10]
        if date not in days:
            days[date] = []
        days[date].append(r)
    return days


def bottom_line(pct, total):
    if total < 24:
        return f"Only {total} predictions in — need at least a full day of hourly data to draw conclusions."
    if pct >= 65:
        return f"{pct}% over {total} predictions — genuinely impressive, Kronos is beating a coin flip by a real margin. Pay attention."
    if pct >= 60:
        return f"{pct}% is solid over {total} predictions — beating random by a meaningful margin. Something's working."
    if pct >= 55:
        return f"{pct}% — slightly above a coin flip over {total} predictions. Mildly interesting, keep watching."
    if pct >= 45:
        return f"{pct}% across {total} predictions — basically indistinguishable from guessing right now."
    return f"Oof — {pct}% over {total} predictions means Kronos is worse than a coin flip right now. Rough patch."


# ---------------------------------------------------------------------------
# HTML builders
# ---------------------------------------------------------------------------

def build_pending_block(pending_list):
    if not pending_list:
        return ""
    # Show the most recent pending prediction
    pending = sorted(pending_list, key=lambda p: p.get("scrape_timestamp", ""))[-1]
    upside_pct = round(pending["upside_prob"] * 100, 1)
    vol_pct = round(pending["vol_amplification_prob"] * 100, 1)
    ts = pending.get("prediction_timestamp", "")[:16]
    total_pending = len(pending_list)

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
  <p style="margin:0 0 6px; font-weight:bold; color:#1a1a2e;">&#128313; Latest prediction
    <span style="font-size:12px;font-weight:normal;color:#888">({total_pending} pending, scoring in 24h)</span>
  </p>
  <p style="margin:4px 0; font-size:15px; color:{dir_color};"><strong>Direction:</strong> {dir_label}</p>
  <p style="margin:4px 0; font-size:15px; color:#444;"><strong>Volatility:</strong> {vol_label}</p>
  <p style="margin:8px 0 0; font-size:11px; color:#999;">Kronos timestamp: {ts} UTC</p>
</div>"""


def build_day_block(date, records):
    stats = compute_stats(records)
    pct = stats["direction_accuracy_pct"]
    n = stats["n"]
    dir_correct = stats["dir_correct"]

    # Day header color
    if pct >= 65:
        header_bg = "#e8f5e9"; header_color = "#2e7d32"; grade = "&#128293;"
    elif pct >= 55:
        header_bg = "#fff8e1"; header_color = "#f57f17"; grade = "&#128578;"
    elif pct >= 45:
        header_bg = "#f5f5f5"; header_color = "#555"; grade = "&#127922;"
    else:
        header_bg = "#ffebee"; header_color = "#c62828"; grade = "&#128531;"

    # Build hourly rows sorted by hour
    sorted_records = sorted(records, key=lambda r: r.get("prediction_timestamp", ""), reverse=True)
    rows = ""
    for r in sorted_records:
        hour = r.get("prediction_timestamp", "")[11:16]  # HH:MM
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
            up_bg = "#f5f5f5"; up_color = "#555"

        rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0;">
          <td style="padding:6px 10px;font-size:12px;color:#888;white-space:nowrap;">{hour} UTC</td>
          <td style="padding:6px 10px;text-align:center;background:{up_bg};color:{up_color};font-weight:500;font-size:12px;">{upside_pct}%</td>
          <td style="padding:6px 10px;text-align:center;font-size:12px;color:#444;">{vol_pct}%</td>
          <td style="padding:6px 10px;text-align:center;font-size:12px;font-weight:500;color:{change_color};">{change_str}</td>
          <td style="padding:6px 10px;text-align:center;font-size:14px;">{dir_icon}</td>
          <td style="padding:6px 10px;text-align:center;font-size:14px;">{vol_icon}</td>
          <td style="padding:6px 10px;text-align:center;font-size:11px;color:#888;">{brier:.3f} / {vol_brier:.3f}</td>
        </tr>"""

    return f"""
<div style="margin:16px 0; border:1px solid #e0e0e0; border-radius:8px; overflow:hidden;">
  <!-- Day header -->
  <div style="background:{header_bg}; padding:12px 16px; display:flex; justify-content:space-between; align-items:center;">
    <div>
      <span style="font-size:15px; font-weight:600; color:{header_color};">{grade} {date}</span>
      <span style="font-size:13px; color:{header_color}; margin-left:12px;">{dir_correct}/{n} correct &nbsp;·&nbsp; {pct}% &nbsp;·&nbsp; Avg Brier: {stats['avg_brier_score']}</span>
    </div>
    <span style="font-size:12px; color:#888;">Vol acc: {stats['vol_accuracy_pct']}%</span>
  </div>
  <!-- Hourly table -->
  <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;">
      <thead>
        <tr style="background:#fafafa;">
          <th style="padding:7px 10px;text-align:left;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Hour</th>
          <th style="padding:7px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Upside %</th>
          <th style="padding:7px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Vol amp %</th>
          <th style="padding:7px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">BTC &#916;</th>
          <th style="padding:7px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Dir</th>
          <th style="padding:7px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Vol</th>
          <th style="padding:7px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Brier (d/v)</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>"""


def build_overall_scoreboard(records):
    stats = compute_stats(records)
    if not stats:
        return ""
    pct = stats["direction_accuracy_pct"]
    bar_color = "#4caf50" if pct > 55 else ("#ff9800" if pct >= 45 else "#f44336")
    verdict = bottom_line(pct, stats["n"])

    return f"""
<div style="background:#f5f5f5; padding:16px; border-radius:8px; margin:16px 0;">
  <p style="margin:0 0 4px; font-weight:bold; font-size:14px;">&#128202; Overall score — all history</p>
  <p style="font-size:32px; font-weight:bold; margin:0; color:#1a1a2e;">{pct}%
    <span style="font-size:14px; font-weight:normal; color:#666;">({stats['dir_correct']} of {stats['n']} predictions)</span>
  </p>
  <div style="background:#ddd; border-radius:4px; height:10px; margin:10px 0;">
    <div style="background:{bar_color}; width:{min(pct,100):.1f}%; height:10px; border-radius:4px;"></div>
  </div>
  <table style="width:100%;margin-top:8px;">
    <tr>
      <td style="font-size:12px;color:#888;">Vol accuracy</td>
      <td style="font-size:12px;color:#888;">Avg Brier</td>
      <td style="font-size:12px;color:#888;">Avg Vol Brier</td>
      <td style="font-size:12px;color:#888;">Streak</td>
    </tr>
    <tr>
      <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['vol_accuracy_pct']}%</td>
      <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['avg_brier_score']}</td>
      <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['avg_vol_brier_score']}</td>
      <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['correct_streak']} &#9989;</td>
    </tr>
  </table>
  <p style="font-size:12px;color:#999;margin:10px 0 0;">&#127922; Coin flip = 50% &middot; &#128578; Decent = 55% &middot; &#128293; Good = 60%+</p>
</div>
<div style="background:#e3f2fd; padding:16px; border-radius:8px; margin:16px 0;">
  <p style="margin:0 0 4px; font-weight:bold;">&#127919; Bottom line</p>
  <p style="margin:0;">{verdict}</p>
</div>"""


def build_html(records, pending_list, today_str):
    pending_block = build_pending_block(pending_list)

    # Group scored records by day, show most recent 7 days
    days = group_by_day(records)
    sorted_dates = sorted(days.keys(), reverse=True)[:7]

    day_blocks = ""
    for date in sorted_dates:
        day_blocks += build_day_block(date, days[date])

    overall = build_overall_scoreboard(records)

    return f"""<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">

<h2 style="color: #1a1a2e; margin-bottom:4px;">&#128202; Kronos BTC Tracker</h2>
<p style="color: #888; font-size: 13px; margin-top:0;">{today_str} &middot; Hourly predictions</p>

{pending_block}

<p style="font-size:13px;font-weight:600;color:#555;margin:20px 0 8px;text-transform:uppercase;letter-spacing:.05em;">Daily breakdown — last 7 days</p>
{day_blocks}

{overall}

<p style="color: #bbb; font-size: 11px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 12px;">
  Upside % green &#8805;65% (confident bullish) · red &#8804;35% (confident bearish)<br>
  Brier: 0.0 = perfect · 0.25 = random · 1.0 = perfectly wrong<br>
  Kronos Tracker &middot; github.com/lemleysergio-cloud/kronos-tracker
</p>
</div>"""


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_email(html_body, today_str):
    if not GMAIL_USER or not GMAIL_PASS:
        print("ERROR: Set GMAIL_USERNAME and GMAIL_APP_PASSWORD env vars.")
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


def main():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = load_json(SCORES_FILE, [])
    pending_list = load_json(PENDING_FILE, [])

    if not records:
        print("No scored records in scores.json yet — skipping email.")
        return

    html = build_html(records, pending_list, today_str)
    send_email(html, today_str)


if __name__ == "__main__":
    main()
