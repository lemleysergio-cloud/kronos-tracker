"""
Kronos daily email sender — Hourly Edition, Apple Mail compatible.
Day headers bold + colored, hourly rows muted underneath.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCORES_FILE = REPO_ROOT / "scores.json"
PENDING_FILE = REPO_ROOT / "pending.json"

RECIPIENT = os.environ.get("RECIPIENT_EMAIL", "lemleysergio@gmail.com")
GMAIL_USER = os.environ.get("GMAIL_USERNAME")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")


def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


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


def hours_remaining(scrape_timestamp_str):
    try:
        scrape_ts = datetime.fromisoformat(scrape_timestamp_str)
        if scrape_ts.tzinfo is None:
            scrape_ts = scrape_ts.replace(tzinfo=timezone.utc)
        scores_at = scrape_ts + timedelta(hours=24)
        remaining = scores_at - datetime.now(timezone.utc)
        hours = max(0, remaining.total_seconds() / 3600)
        return hours
    except Exception:
        return 24.0


def bottom_line(pct, total):
    if total < 24:
        return f"Only {total} predictions scored so far — need at least a full day of hourly data to draw conclusions."
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
# Pending section
# ---------------------------------------------------------------------------

def build_pending_section(pending_list):
    if not pending_list:
        return ""

    days = {}
    for p in pending_list:
        date = p.get("prediction_timestamp", "")[:10]
        if date not in days:
            days[date] = []
        days[date].append(p)

    blocks = ""
    for date in sorted(days.keys(), reverse=True):
        records = sorted(days[date], key=lambda r: r.get("prediction_timestamp", ""), reverse=True)
        n = len(records)

        # Day header row
        blocks += f"""
<table style="width:100%;border-collapse:collapse;margin-bottom:2px;">
  <tr style="background:#dbeafe;">
    <td style="padding:10px 14px;font-size:14px;font-weight:600;color:#1565c0;border-radius:6px 6px 0 0;">
      &#128313; {date}
      <span style="font-size:12px;font-weight:normal;color:#1976d2;margin-left:10px;">{n} prediction{'s' if n != 1 else ''} pending — scoring in progress</span>
    </td>
  </tr>
</table>
<table style="width:100%;border-collapse:collapse;margin-bottom:16px;border:1px solid #dbeafe;border-top:none;border-radius:0 0 6px 6px;">
  <tr style="background:#f0f6ff;">
    <th style="padding:5px 10px;text-align:left;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Hour</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Upside %</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Vol amp %</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Result</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#999;font-weight:600;text-transform:uppercase;">Scores in</th>
  </tr>"""

        for r in records:
            hour = r.get("prediction_timestamp", "")[11:16]
            upside_pct = round(r["upside_prob"] * 100, 1)
            vol_pct = round(r["vol_amplification_prob"] * 100, 1)
            hrs_left = hours_remaining(r.get("scrape_timestamp", ""))
            hrs_int = int(hrs_left)
            mins_int = int((hrs_left - hrs_int) * 60)

            if hrs_left <= 2:
                cd_color = "#c62828"
                countdown = f"~{hrs_int}h {mins_int}m"
            elif hrs_left <= 6:
                cd_color = "#e65100"
                countdown = f"~{hrs_int}h remaining"
            else:
                cd_color = "#888"
                countdown = f"~{hrs_int}h remaining"

            if upside_pct >= 65:
                up_color = "#2e7d32"
            elif upside_pct <= 35:
                up_color = "#c62828"
            else:
                up_color = "#555"

            dir_word = "&#9650; Bullish" if r["upside_prob"] > 0.5 else "&#9660; Bearish" if r["upside_prob"] < 0.5 else "&#8212; Neutral"
            vol_word = "&#128308; Choppy" if r["vol_amplification_prob"] > 0.5 else "&#128994; Calm"

            blocks += f"""
  <tr style="border-bottom:1px solid #eef3ff;">
    <td style="padding:5px 10px;font-size:11px;color:#777;">{hour} UTC</td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;font-weight:500;color:{up_color};">{upside_pct}% <span style="font-weight:normal;font-size:10px;">{dir_word}</span></td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;color:#555;">{vol_pct}% <span style="font-size:10px;color:#888;">{vol_word}</span></td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;color:#aaa;font-style:italic;">pending</td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;font-weight:500;color:{cd_color};">{countdown}</td>
  </tr>"""

        blocks += "</table>"

    return f"""
<p style="font-size:12px;font-weight:600;color:#1565c0;margin:20px 0 8px;text-transform:uppercase;letter-spacing:.06em;">&#128313; Pending predictions</p>
{blocks}"""


# ---------------------------------------------------------------------------
# Scored section
# ---------------------------------------------------------------------------

def build_scored_section(records):
    if not records:
        return ""

    days = group_by_day(records)
    sorted_dates = sorted(days.keys(), reverse=True)[:7]

    blocks = ""
    for date in sorted_dates:
        day_records = days[date]
        stats = compute_stats(day_records)
        pct = stats["direction_accuracy_pct"]
        n = stats["n"]
        dir_correct = stats["dir_correct"]

        if pct >= 65:
            hdr_bg = "#e8f5e9"; hdr_color = "#2e7d32"; grade = "&#128293;"
        elif pct >= 55:
            hdr_bg = "#fff8e1"; hdr_color = "#f57f17"; grade = "&#128578;"
        elif pct >= 45:
            hdr_bg = "#f5f5f5"; hdr_color = "#555"; grade = "&#127922;"
        else:
            hdr_bg = "#ffebee"; hdr_color = "#c62828"; grade = "&#128531;"

        # Day summary header
        blocks += f"""
<table style="width:100%;border-collapse:collapse;margin-bottom:2px;">
  <tr style="background:{hdr_bg};">
    <td style="padding:10px 14px;border-radius:6px 6px 0 0;">
      <span style="font-size:14px;font-weight:600;color:{hdr_color};">{grade} {date}</span>
      <span style="font-size:12px;color:{hdr_color};margin-left:10px;">{dir_correct}/{n} correct &nbsp;·&nbsp; {pct}% &nbsp;·&nbsp; Brier: {stats['avg_brier_score']} &nbsp;·&nbsp; Vol: {stats['vol_accuracy_pct']}%</span>
    </td>
  </tr>
</table>
<table style="width:100%;border-collapse:collapse;margin-bottom:16px;border:1px solid #e8e8e8;border-top:none;border-radius:0 0 6px 6px;">
  <tr style="background:#fafafa;">
    <th style="padding:5px 10px;text-align:left;font-size:10px;color:#bbb;font-weight:600;text-transform:uppercase;">Hour</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#bbb;font-weight:600;text-transform:uppercase;">Upside %</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#bbb;font-weight:600;text-transform:uppercase;">Vol amp %</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#bbb;font-weight:600;text-transform:uppercase;">BTC &#916;</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#bbb;font-weight:600;text-transform:uppercase;">Dir</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#bbb;font-weight:600;text-transform:uppercase;">Vol</th>
    <th style="padding:5px 10px;text-align:center;font-size:10px;color:#bbb;font-weight:600;text-transform:uppercase;">Brier d/v</th>
  </tr>"""

        sorted_records = sorted(day_records, key=lambda r: r.get("prediction_timestamp", ""), reverse=True)
        for r in sorted_records:
            hour = r.get("prediction_timestamp", "")[11:16]
            upside_pct = round(r["upside_prob"] * 100, 1)
            vol_pct = round(r["vol_amplification_prob"] * 100, 1)
            price_change = r.get("price_change_pct", 0.0)
            brier = r.get("brier_score", 0.0)
            vol_brier = r.get("vol_brier_score", 0.0)
            dir_ok = r.get("direction_correct", False)
            vol_ok = r.get("vol_correct", False)

            dir_icon = "&#9989;" if dir_ok else "&#10060;"
            vol_icon = "&#9989;" if vol_ok else "&#10060;"
            change_color = "#2e7d32" if price_change > 0 else "#c62828" if price_change < 0 else "#777"
            change_str = f"{price_change:+.2f}%"

            if upside_pct >= 65:
                up_color = "#2e7d32"
            elif upside_pct <= 35:
                up_color = "#c62828"
            else:
                up_color = "#777"

            blocks += f"""
  <tr style="border-bottom:1px solid #f2f2f2;">
    <td style="padding:5px 10px;font-size:11px;color:#aaa;">{hour} UTC</td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;font-weight:500;color:{up_color};">{upside_pct}%</td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;color:#888;">{vol_pct}%</td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;font-weight:500;color:{change_color};">{change_str}</td>
    <td style="padding:5px 10px;text-align:center;font-size:13px;">{dir_icon}</td>
    <td style="padding:5px 10px;text-align:center;font-size:13px;">{vol_icon}</td>
    <td style="padding:5px 10px;text-align:center;font-size:11px;color:#bbb;">{brier:.3f}/{vol_brier:.3f}</td>
  </tr>"""

        blocks += "</table>"

    return f"""
<p style="font-size:12px;font-weight:600;color:#555;margin:24px 0 8px;text-transform:uppercase;letter-spacing:.06em;">Scored history — last 7 days</p>
{blocks}"""


# ---------------------------------------------------------------------------
# Overall scoreboard
# ---------------------------------------------------------------------------

def build_overall_scoreboard(records):
    stats = compute_stats(records)
    if not stats:
        return ""
    pct = stats["direction_accuracy_pct"]
    bar_color = "#4caf50" if pct > 55 else ("#ff9800" if pct >= 45 else "#f44336")
    verdict = bottom_line(pct, stats["n"])

    return f"""
<table style="width:100%;border-collapse:collapse;background:#f5f5f5;border-radius:8px;margin:8px 0 0;">
  <tr><td style="padding:16px;">
    <p style="margin:0 0 4px;font-weight:bold;font-size:14px;color:#1a1a2e;">&#128202; Overall score — all history</p>
    <p style="font-size:30px;font-weight:bold;margin:4px 0;color:#1a1a2e;">{pct}%
      <span style="font-size:13px;font-weight:normal;color:#888;">({stats['dir_correct']} of {stats['n']} predictions)</span>
    </p>
    <table style="width:100%;border-collapse:collapse;margin:6px 0;">
      <tr>
        <td style="width:{min(pct,100):.1f}%;background:{bar_color};height:8px;border-radius:4px 0 0 4px;"></td>
        <td style="background:#ddd;height:8px;border-radius:0 4px 4px 0;"></td>
      </tr>
    </table>
    <table style="width:100%;border-collapse:collapse;margin-top:10px;">
      <tr>
        <td style="font-size:11px;color:#aaa;">Vol accuracy</td>
        <td style="font-size:11px;color:#aaa;">Avg Brier</td>
        <td style="font-size:11px;color:#aaa;">Avg Vol Brier</td>
        <td style="font-size:11px;color:#aaa;">Streak</td>
      </tr>
      <tr>
        <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['vol_accuracy_pct']}%</td>
        <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['avg_brier_score']}</td>
        <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['avg_vol_brier_score']}</td>
        <td style="font-size:14px;font-weight:500;color:#1a1a2e;">{stats['correct_streak']} &#9989;</td>
      </tr>
    </table>
    <p style="font-size:11px;color:#bbb;margin:8px 0 0;">&#127922; Coin flip = 50% &middot; &#128578; Decent = 55% &middot; &#128293; Good = 60%+</p>
  </td></tr>
</table>
<table style="width:100%;border-collapse:collapse;background:#e3f2fd;border-radius:8px;margin:10px 0 0;">
  <tr><td style="padding:14px 16px;">
    <p style="margin:0 0 4px;font-weight:bold;color:#1a1a2e;">&#127919; Bottom line</p>
    <p style="margin:0;color:#333;">{verdict}</p>
  </td></tr>
</table>"""


# ---------------------------------------------------------------------------
# Main builder + send
# ---------------------------------------------------------------------------

def build_html(records, pending_list, today_str):
    pending_section = build_pending_section(pending_list)
    scored_section = build_scored_section(records)
    overall = build_overall_scoreboard(records)

    return f"""<div style="font-family:-apple-system,Arial,sans-serif;max-width:620px;margin:0 auto;padding:20px;">

<h2 style="color:#1a1a2e;margin-bottom:4px;">&#128202; Kronos BTC Tracker</h2>
<p style="color:#888;font-size:13px;margin-top:0;">{today_str} &middot; Hourly predictions</p>

{pending_section}
{scored_section}
{overall}

<p style="color:#ccc;font-size:11px;margin-top:20px;border-top:1px solid #eee;padding-top:12px;">
  Upside % green &#8805;65% bullish · red &#8804;35% bearish &middot;
  Brier: 0.0 perfect · 0.25 random &middot;
  Kronos Tracker · github.com/lemleysergio-cloud/kronos-tracker
</p>
</div>"""


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

    if not records and not pending_list:
        print("No data yet — skipping email.")
        return

    html = build_html(records, pending_list, today_str)
    send_email(html, today_str)


if __name__ == "__main__":
    main()
