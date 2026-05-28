"""
Kronos daily email — v3
Mobile-first responsive HTML email.
Uses hybrid approach: table structure + media queries + fluid widths.
Tested patterns: Gmail Android, Gmail iOS, Apple Mail iOS, Apple Mail macOS.
"""

import json, os, smtplib, sys, urllib.request
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

# ─── helpers ──────────────────────────────────────────────────────────────────
def load_json(path, default):
    if path.exists():
        with open(path) as f: return json.load(f)
    return default

def compute_stats(recs):
    if not recs: return {}
    n = len(recs)
    dc = sum(1 for r in recs if r.get("direction_correct"))
    vc = sum(1 for r in recs if r.get("vol_correct"))
    ab = sum(r.get("brier_score", 0.5) for r in recs) / n
    avb = sum(r.get("vol_brier_score", 0.5) for r in recs) / n
    streak = 0
    for r in reversed(recs):
        if r.get("direction_correct"): streak += 1
        else: break
    return {"n":n,"dir_correct":dc,
            "direction_accuracy_pct":round(dc/n*100,1),
            "vol_accuracy_pct":round(vc/n*100,1),
            "avg_brier_score":round(ab,4),
            "avg_vol_brier_score":round(avb,4),
            "correct_streak":streak}

def group_by_day(recs):
    d = {}
    for r in recs:
        k = r.get("prediction_timestamp","")[:10]
        d.setdefault(k,[]).append(r)
    return d

def hours_remaining(ts):
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
        rem = (t + timedelta(hours=24)) - datetime.now(timezone.utc)
        return max(0, rem.total_seconds()/3600)
    except: return 24.0

def utc_to_et(h):
    try:
        hh,mm = int(h[:2]),int(h[3:5])
        et = (hh-4)%24
        return f"{et%12 or 12}:{mm:02d} {'AM' if et<12 else 'PM'} ET"
    except: return ""

def prob_color(p):
    return "#2e7d32" if p>=0.65 else "#c62828" if p<=0.35 else "#555"

def score_color(s):
    return "#00695c" if s>=8 else "#2e7d32" if s>=6 else "#e65100" if s>=4 else "#c62828"

def score_bg(s):
    return "#e0f2f1" if s>=8 else "#e8f5e9" if s>=6 else "#fff8e1" if s>=4 else "#ffebee"

def fear_color(v):
    return "#c62828" if v<=25 else "#e65100" if v<=45 else "#555" if v<=55 else "#2e7d32" if v<=75 else "#1b5e20"

def get_signal(p): return "bearish" if p<=0.30 else "bullish" if p>=0.70 else "neutral"

def calc_conviction(prob, vol, sig, align="unknown"):
    s = 0
    d = abs(prob-0.5)
    if d>=0.4: s+=3
    elif d>=0.3: s+=2
    elif d>=0.2: s+=1
    if vol is not None:
        if sig=="bearish": s += 2 if vol>=0.7 else 1 if vol>=0.5 else -1
        else: s += 2 if vol>=0.7 else 1 if vol>=0.5 else -1
    if align=="aligned": s+=2
    elif align=="counter": s-=2
    if prob<=0.1 or prob>=0.9: s+=1
    return max(1,min(10,s))

def get_size(sc): return 50 if sc<=3 else 75 if sc<=5 else 100 if sc<=7 else 150

def bottom_line(pct, total):
    if total<24: return f"Only {total} predictions scored — keep collecting data."
    if pct>=65: return f"{pct}% over {total} predictions — genuinely beating a coin flip. Pay attention."
    if pct>=60: return f"{pct}% is solid over {total} predictions — something's working."
    if pct>=55: return f"{pct}% — slightly above a coin flip. Mildly interesting, keep watching."
    if pct>=45: return f"{pct}% across {total} predictions — basically indistinguishable from guessing."
    return f"{pct}% over {total} predictions — Kronos is worse than a coin flip right now."

def fetch_fear_greed():
    try:
        with urllib.request.urlopen("https://api.alternative.me/fng/?limit=1", timeout=8) as r:
            data = json.loads(r.read())
        v = int(data["data"][0]["value"])
        return v, data["data"][0]["value_classification"]
    except: return None, None

# ─── HEAD (responsive CSS lives here — Gmail strips <style> in body) ──────────
HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>Kronos BTC Tracker</title>
<style type="text/css">
/* Reset */
body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}
table,td{mso-table-lspace:0pt;mso-table-rspace:0pt}
img{-ms-interpolation-mode:bicubic;border:0;outline:none;text-decoration:none}

/* Base */
body{margin:0!important;padding:0!important;background:#e8e8e8!important}
.email-wrapper{width:100%;background:#e8e8e8}
.email-container{max-width:620px;margin:0 auto;background:#ffffff}

/* Mobile - anything under 620px */
@media screen and (max-width:620px){
  .email-container{width:100%!important}
  .mob-pad{padding:12px!important}
  .mob-full{width:100%!important;display:block!important}
  .mob-stack{display:block!important;width:100%!important;padding:0 0 8px 0!important}
  .mob-hide{display:none!important}
  .mob-center{text-align:center!important}
  .mob-font-lg{font-size:26px!important}
  .mob-font-sm{font-size:11px!important}
  .mob-metric-row td{display:block!important;width:100%!important;padding-bottom:8px!important}

  /* Shrink table columns on mobile */
  .score-col{display:none!important}
  .vol-col{display:none!important}
  .brier-col{display:none!important}
  .et-col{display:none!important}

  /* Paper trade table mobile */
  .pt-score-col{display:none!important}
  .pt-vol-col{display:none!important}
}
</style>
</head>
<body style="margin:0;padding:0;background:#e8e8e8;">
<div class="email-wrapper">
<table class="email-container" width="620" cellpadding="0" cellspacing="0" border="0" align="center" style="max-width:620px;background:#ffffff;">
"""

FOOT = """</table>
</div>
</body></html>"""

# ─── section helpers ──────────────────────────────────────────────────────────

def row(content, bg="#ffffff", pad="16px 20px"):
    return f'<tr><td style="background:{bg};padding:{pad};">{content}</td></tr>\n'

def divider(color="#e5e5e5", style="solid"):
    border = f"border-top:1px {style} {color}"
    return f'<tr><td style="{border};font-size:0;line-height:0;">&nbsp;</td></tr>\n'

def section_label(text, color="#555"):
    return f'<p style="font-size:11px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:.07em;margin:0 0 10px;">{text}</p>'

def badge(text, bg, col):
    return f'<span style="background:{bg};color:{col};font-size:10px;font-weight:700;padding:3px 8px;border-radius:4px;white-space:nowrap;">{text}</span>'

# ─── header ──────────────────────────────────────────────────────────────────

def build_header(today_str):
    return row(f"""
<table width="100%" cellpadding="0" cellspacing="0">
<tr>
  <td>
    <p style="margin:0;font-size:22px;font-weight:800;color:#1a1a2e;letter-spacing:-.3px;">📊 Kronos BTC</p>
    <p style="margin:3px 0 0;font-size:12px;color:#aaa;">{today_str} &nbsp;·&nbsp; Hourly predictions &nbsp;·&nbsp; Conviction-scored</p>
  </td>
</tr>
</table>""", bg="#ffffff", pad="20px 20px 12px")

# ─── fear & greed ─────────────────────────────────────────────────────────────

def build_fg(fv, fl):
    if fv is None: return ""
    col = fear_color(fv)
    emoji = "😱" if fv<=25 else "😟" if fv<=45 else "😐" if fv<=55 else "🙂" if fv<=75 else "🤑"
    note = "Extreme fear = historically good buy zone" if fv<=25 else "Fear = cautious environment" if fv<=45 else "Neutral market sentiment" if fv<=55 else "Greed = be careful chasing" if fv<=75 else "Extreme greed = danger zone"
    return row(f"""
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9f9f9;border-radius:8px;border:1px solid #e5e5e5;">
<tr><td style="padding:12px 14px;">
  <table width="100%" cellpadding="0" cellspacing="0">
  <tr>
    <td style="font-size:28px;width:36px;vertical-align:middle;">{emoji}</td>
    <td style="padding-left:10px;vertical-align:middle;">
      <p style="margin:0;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.06em;">Fear &amp; Greed Index</p>
      <p style="margin:2px 0 0;font-size:22px;font-weight:800;color:{col};line-height:1;">{fv} <span style="font-size:13px;font-weight:600;">{fl}</span></p>
    </td>
    <td align="right" style="vertical-align:middle;">
      <p style="margin:0;font-size:11px;color:#aaa;max-width:140px;text-align:right;">{note}</p>
    </td>
  </tr>
  <tr><td colspan="3" style="padding-top:8px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="width:{fv}%;background:{col};height:5px;border-radius:3px 0 0 3px;"></td>
      <td style="background:#e5e5e5;height:5px;border-radius:0 3px 3px 0;"></td>
    </tr></table>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:3px;"><tr>
      <td style="font-size:9px;color:#ccc;">0 — extreme fear</td>
      <td align="right" style="font-size:9px;color:#ccc;">100 — extreme greed</td>
    </tr></table>
  </td></tr>
  </table>
</td></tr></table>""", bg="#ffffff", pad="0 20px 12px")

# ─── pending ─────────────────────────────────────────────────────────────────

def build_pending(pending_list):
    if not pending_list: return ""
    # sort newest first, group by day
    days = {}
    for r in pending_list:
        d = r.get("prediction_timestamp","")[:10]
        days.setdefault(d,[]).append(r)

    blocks = section_label("🕐 Pending — awaiting scoring", "#1565c0")
    for date in sorted(days.keys(), reverse=True):
        recs = sorted(days[date], key=lambda x: x.get("prediction_timestamp",""), reverse=True)
        rows_html = ""
        for r in recs:
            hour = r.get("prediction_timestamp","")[11:16]
            up = round(r["upside_prob"]*100,1)
            vp = round(r["vol_amplification_prob"]*100,1)
            sig = get_signal(r["upside_prob"])
            sc = calc_conviction(r["upside_prob"], r["vol_amplification_prob"], sig)
            sz = get_size(sc)
            hrs = hours_remaining(r.get("scrape_timestamp",""))
            cd_col = "#c62828" if hrs<=2 else "#e65100" if hrs<=6 else "#888"
            up_col = prob_color(r["upside_prob"])
            dw = "▼ Bear" if sig=="bearish" else "▲ Bull" if sig=="bullish" else "— Neutral"
            sc_b = badge(f"{sc}/10 · ${sz}", score_bg(sc), score_color(sc))
            rows_html += f"""
<tr style="border-bottom:1px solid #f0f0f0;">
  <td style="padding:7px 10px;font-size:11px;color:#aaa;">{hour}<br>
    <span class="et-col" style="font-size:10px;color:#ccc;">{utc_to_et(hour)}</span>
  </td>
  <td style="padding:7px 10px;font-size:13px;font-weight:700;color:{up_col};">{up}%
    <br><span style="font-size:10px;font-weight:500;">{dw}</span>
  </td>
  <td class="vol-col" style="padding:7px 10px;font-size:11px;color:#777;text-align:center;">{vp}%</td>
  <td class="score-col" style="padding:7px 10px;text-align:center;">{sc_b}</td>
  <td style="padding:7px 10px;font-size:11px;font-weight:700;color:{cd_col};text-align:right;">~{int(hrs)}h</td>
</tr>"""

        blocks += f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px;border-radius:8px;overflow:hidden;border:1px solid #dbeafe;">
  <tr style="background:#dbeafe;">
    <td style="padding:9px 12px;font-size:12px;font-weight:700;color:#1565c0;">{date} &nbsp;<span style="font-weight:500;font-size:11px;">({len(recs)} prediction{'s' if len(recs)!=1 else ''})</span></td>
  </tr>
  <tr><td>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr style="background:#f0f6ff;">
      <th style="padding:5px 10px;font-size:9px;color:#999;font-weight:700;text-transform:uppercase;text-align:left;">Hour</th>
      <th style="padding:5px 10px;font-size:9px;color:#999;font-weight:700;text-transform:uppercase;text-align:left;">Upside</th>
      <th class="vol-col" style="padding:5px 10px;font-size:9px;color:#999;font-weight:700;text-transform:uppercase;text-align:center;">Vol</th>
      <th class="score-col" style="padding:5px 10px;font-size:9px;color:#999;font-weight:700;text-transform:uppercase;text-align:center;">Score / Size</th>
      <th style="padding:5px 10px;font-size:9px;color:#999;font-weight:700;text-transform:uppercase;text-align:right;">In</th>
    </tr>
    {rows_html}
    </table>
  </td></tr>
</table>"""

    return row(blocks, pad="12px 20px")

# ─── scored ──────────────────────────────────────────────────────────────────

def build_scored(records):
    if not records: return ""
    days = group_by_day(records)
    blocks = section_label("Scored history — last 7 days")

    for date in sorted(days.keys(), reverse=True)[:7]:
        dr = days[date]
        st = compute_stats(dr)
        pct = st["direction_accuracy_pct"]
        hbg,hcol,grade = ("#e8f5e9","#2e7d32","🔥") if pct>=65 else ("#fff8e1","#e65100","🙂") if pct>=55 else ("#f5f5f5","#555","🎲") if pct>=45 else ("#ffebee","#c62828","😬")
        rows_html = ""
        for r in sorted(dr, key=lambda x: x.get("prediction_timestamp",""), reverse=True):
            hour = r.get("prediction_timestamp","")[11:16]
            up = round(r["upside_prob"]*100,1)
            vp = round(r.get("vol_amplification_prob",0)*100,1)
            sig = get_signal(r["upside_prob"])
            sc = calc_conviction(r["upside_prob"], r.get("vol_amplification_prob"), sig)
            sz = get_size(sc)
            chg = r.get("price_change_pct",0)
            dok = r.get("direction_correct",False)
            vok = r.get("vol_correct",False)
            br = r.get("brier_score",0)
            cc = "#2e7d32" if chg>0 else "#c62828" if chg<0 else "#777"
            sc_b = badge(f"{sc}/10", score_bg(sc), score_color(sc))
            rows_html += f"""
<tr style="border-bottom:1px solid #f5f5f5;">
  <td style="padding:6px 10px;font-size:11px;color:#aaa;">{hour}
    <br><span class="et-col" style="font-size:10px;color:#ccc;">{utc_to_et(hour)}</span>
  </td>
  <td style="padding:6px 10px;font-size:12px;font-weight:700;color:{prob_color(r['upside_prob'])};">{up}%</td>
  <td class="vol-col" style="padding:6px 10px;font-size:11px;color:#777;text-align:center;">{vp}%</td>
  <td class="score-col" style="padding:6px 10px;text-align:center;">{sc_b}<br><span style="font-size:9px;color:#bbb;">${sz}</span></td>
  <td style="padding:6px 10px;font-size:12px;font-weight:700;color:{cc};text-align:center;">{chg:+.1f}%</td>
  <td style="padding:6px 10px;font-size:14px;text-align:center;">{"✅" if dok else "❌"}</td>
  <td class="vol-col" style="padding:6px 10px;font-size:14px;text-align:center;">{"✅" if vok else "❌"}</td>
  <td class="brier-col" style="padding:6px 10px;font-size:10px;color:#ccc;text-align:center;">{br:.3f}</td>
</tr>"""

        blocks += f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px;border-radius:8px;overflow:hidden;border:1px solid #e5e5e5;">
  <tr style="background:{hbg};">
    <td style="padding:9px 12px;">
      <span style="font-size:13px;font-weight:700;color:{hcol};">{grade} {date}</span>
      <span style="font-size:11px;color:{hcol};margin-left:8px;">{st['dir_correct']}/{st['n']} correct &nbsp;·&nbsp; {pct}% &nbsp;·&nbsp; Brier: {st['avg_brier_score']}</span>
    </td>
  </tr>
  <tr><td>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr style="background:#fafafa;">
      <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Hour</th>
      <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Upside</th>
      <th class="vol-col" style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Vol</th>
      <th class="score-col" style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Score/$</th>
      <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">BTC Δ</th>
      <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Dir</th>
      <th class="vol-col" style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Vol</th>
      <th class="brier-col" style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Brier</th>
    </tr>
    {rows_html}
    </table>
  </td></tr>
</table>"""

    return row(blocks, pad="12px 20px")

# ─── scoreboard ───────────────────────────────────────────────────────────────

def build_scoreboard(records, fv, fl):
    st = compute_stats(records)
    if not st: return ""
    pct = st["direction_accuracy_pct"]
    bc = "#4caf50" if pct>55 else "#ff9800" if pct>=45 else "#f44336"
    bh = [r for r in records if r["upside_prob"]<=0.30]
    bu = [r for r in records if r["upside_prob"]>=0.80]
    bha = round(sum(1 for r in bh if r.get("direction_correct"))/len(bh)*100) if bh else None
    bua = round(sum(1 for r in bu if r.get("direction_correct"))/len(bu)*100) if bu else None

    # emoji history — last 20 scored
    recent = records[-20:] if len(records)>20 else records
    emoji_chain = " ".join("✅" if r.get("direction_correct") else "❌" for r in recent)

    hc_rows = ""
    if bha is not None:
        hc_rows += f"""<tr>
<td style="padding:5px 0;font-size:12px;color:#888;">Bearish signals (0–30%)</td>
<td align="right" style="padding:5px 0;font-size:13px;font-weight:700;color:#c62828;">{bha}% <span style="font-size:11px;font-weight:400;color:#bbb;">({len(bh)} calls)</span></td></tr>"""
    if bua is not None:
        hc_rows += f"""<tr>
<td style="padding:5px 0;font-size:12px;color:#888;">Bullish signals (80%+)</td>
<td align="right" style="padding:5px 0;font-size:13px;font-weight:700;color:#2e7d32;">{bua}% <span style="font-size:11px;font-weight:400;color:#bbb;">({len(bu)} calls)</span></td></tr>"""
    if fv:
        fc = fear_color(fv)
        hc_rows += f"""<tr style="border-top:1px solid #f0f0f0;">
<td style="padding:8px 0 4px;font-size:12px;color:#888;">Fear &amp; Greed today</td>
<td align="right" style="padding:8px 0 4px;font-size:13px;font-weight:700;color:{fc};">{fv} — {fl}</td></tr>"""

    return row(f"""
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9f9f9;border-radius:8px;border:1px solid #e5e5e5;">
<tr><td style="padding:16px;">

  <p style="margin:0 0 2px;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.06em;">Overall accuracy — all history</p>
  <p class="mob-font-lg" style="font-size:32px;font-weight:800;color:#1a1a2e;margin:4px 0;">{pct}%
    <span style="font-size:13px;font-weight:400;color:#aaa;">({st['dir_correct']} of {st['n']})</span>
  </p>

  <table width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0;">
    <tr>
      <td style="width:{min(pct,100):.1f}%;background:{bc};height:8px;border-radius:4px 0 0 4px;"></td>
      <td style="background:#ddd;height:8px;border-radius:0 4px 4px 0;"></td>
    </tr>
  </table>
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:3px;">
    <tr>
      <td style="font-size:9px;color:#ccc;">🎲 50% = coin flip</td>
      <td align="center" style="font-size:9px;color:#ccc;">🙂 55% = decent</td>
      <td align="right" style="font-size:9px;color:#ccc;">🔥 60%+ = good</td>
    </tr>
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" style="margin:12px 0;border-top:1px solid #eeeeee;border-bottom:1px solid #eeeeee;padding:10px 0;">
    <tr>
      <td style="text-align:center;padding:4px 8px;border-right:1px solid #eee;">
        <p style="margin:0;font-size:10px;color:#aaa;">Vol accuracy</p>
        <p style="margin:2px 0 0;font-size:18px;font-weight:700;color:#1a1a2e;">{st['vol_accuracy_pct']}%</p>
      </td>
      <td style="text-align:center;padding:4px 8px;border-right:1px solid #eee;">
        <p style="margin:0;font-size:10px;color:#aaa;">Avg Brier</p>
        <p style="margin:2px 0 0;font-size:18px;font-weight:700;color:#1a1a2e;">{st['avg_brier_score']}</p>
      </td>
      <td style="text-align:center;padding:4px 8px;">
        <p style="margin:0;font-size:10px;color:#aaa;">Streak</p>
        <p style="margin:2px 0 0;font-size:18px;font-weight:700;color:#1a1a2e;">{st['correct_streak']} ✅</p>
      </td>
    </tr>
  </table>

  <p style="margin:0 0 4px;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.05em;">Recent call history</p>
  <p style="margin:0 0 12px;font-size:16px;letter-spacing:3px;line-height:1.8;">{emoji_chain}</p>

  <table width="100%" cellpadding="0" cellspacing="0">
    {hc_rows}
  </table>

</td></tr>
</table>

<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;background:#e3f2fd;border-radius:8px;border:1px solid #bbdefb;">
<tr><td style="padding:14px 16px;">
  <p style="margin:0 0 4px;font-weight:700;font-size:13px;color:#1a1a2e;">🎯 Bottom line</p>
  <p style="margin:0;font-size:13px;color:#333;line-height:1.5;">{bottom_line(pct, st['n'])}</p>
</td></tr>
</table>""", pad="12px 20px")

# ─── paper trading ────────────────────────────────────────────────────────────

def build_paper_trading(trades, balance, pnl, exposure=0):
    if not trades: return ""

    closed_trades = [t for t in trades if t.get("status") == "closed"]
    open_trades   = [t for t in trades if t.get("status") == "open"]
    wins   = sum(1 for t in closed_trades if t["outcome"] in ("win","take-profit"))
    losses = sum(1 for t in closed_trades if t["outcome"] == "loss")
    sls    = sum(1 for t in closed_trades if t["outcome"] == "stop-loss")
    tps    = sum(1 for t in closed_trades if t["outcome"] == "take-profit")
    total_closed = len(closed_trades)
    total_fees = sum(t.get("fees",0) or 0 for t in closed_trades)
    wr     = round(wins/total_closed*100) if total_closed else 0
    bal_col = "#2e7d32" if pnl>=0 else "#c62828"
    pnl_str = ("+$" if pnl>=0 else "-$") + f"{abs(pnl):.2f}"
    pnl_pct = round(pnl/1000*100,1)
    avail   = round(1000 - exposure, 2)

    # Open trades alert
    open_alert = ""
    if open_trades:
        open_rows = ""
        for t in open_trades:
            sig_col = "#c62828" if t["signal"]=="bearish" else "#2e7d32"
            sl = t.get("sl") or 0
            tp = t.get("tp") or 0
            open_rows += f"""<tr style="border-bottom:1px solid #f0f0f0;">
  <td style="padding:6px 10px;font-size:11px;color:#aaa;">{t['date']}</td>
  <td style="padding:6px 10px;"><span style="color:{sig_col};font-size:12px;font-weight:700;">{'▼' if t['signal']=='bearish' else '▲'} {t['prob']}%</span></td>
  <td style="padding:6px 10px;font-size:11px;color:#888;text-align:center;">${t['size']}</td>
  <td style="padding:6px 10px;font-size:11px;color:#555;text-align:center;">${t.get('entry',0):,.0f}</td>
  <td style="padding:6px 10px;font-size:10px;color:#e65100;text-align:center;">${sl:,.0f}</td>
  <td style="padding:6px 10px;font-size:10px;color:#2e7d32;text-align:center;">${tp:,.0f}</td>
  <td style="padding:6px 10px;font-size:10px;color:#888;text-align:right;">{t.get('trend','—')} · F&G:{t.get('fg','—')}</td>
</tr>"""
        open_alert = f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;border-radius:8px;overflow:hidden;border:1px solid #fff3cd;">
<tr style="background:#fff3cd;"><td style="padding:9px 12px;font-size:12px;font-weight:700;color:#856404;">
  ⏳ {len(open_trades)} open trade{'s' if len(open_trades)!=1 else ''} — awaiting 24h close
</td></tr>
<tr><td>
<table width="100%" cellpadding="0" cellspacing="0">
<tr style="background:#fffdf0;">
  <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Date</th>
  <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Signal</th>
  <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Size</th>
  <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Entry</th>
  <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Stop</th>
  <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Target</th>
  <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:right;">Context</th>
</tr>
{open_rows}
</table>
</td></tr></table>"""

    # Prominent balance hero card
    metric_cards = f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;border-radius:10px;overflow:hidden;border:2px solid {bal_col};">
<tr style="background:{'#f0faf0' if pnl>=0 else '#fff5f5'};">
  <td style="padding:18px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="vertical-align:top;">
        <p style="margin:0;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.07em;">Paper trading balance</p>
        <p style="margin:4px 0 0;font-size:38px;font-weight:900;color:{bal_col};letter-spacing:-1px;line-height:1;">${balance:.2f}</p>
        <p style="margin:4px 0 0;font-size:14px;font-weight:600;color:{bal_col};">{pnl_str} &nbsp;<span style="font-size:12px;opacity:.8;">({pnl_pct:+.1f}% return)</span></p>
        <p style="margin:6px 0 0;font-size:10px;color:#aaa;">Started $1,000.00 · Fees paid: ${total_fees:.2f}</p>
      </td>
      <td style="vertical-align:top;text-align:right;padding-left:16px;">
        <p style="margin:0;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.06em;">Available</p>
        <p style="margin:4px 0 0;font-size:22px;font-weight:800;color:#1a1a2e;">${avail:.2f}</p>
        <p style="margin:2px 0 0;font-size:10px;color:#aaa;">${exposure:.2f} in {len(open_trades)} open trade{'s' if len(open_trades)!=1 else ''}</p>
        <br>
        <p style="margin:0;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.06em;">Win rate</p>
        <p style="margin:4px 0 0;font-size:22px;font-weight:800;color:#1a1a2e;">{wr}%</p>
        <p style="margin:2px 0 0;font-size:10px;color:#aaa;">{wins}W · {losses}L · {sls}SL · {total_closed} total</p>
      </td>
    </tr>
    </table>
  </td>
</tr>
</table>"""

    rows_html = ""
    for t in reversed(closed_trades):
        oc = t["outcome"]
        sig_bg  = "#ffebee" if t["signal"]=="bearish" else "#e8f5e9"
        sig_col = "#c62828" if t["signal"]=="bearish" else "#2e7d32"
        sig_lbl = f"▼ {t['prob']}%" if t["signal"]=="bearish" else f"▲ {t['prob']}%"
        out_bg  = "#e8f5e9" if oc in("win","take-profit") else "#fff8e1" if oc=="stop-loss" else "#ffebee"
        out_col = "#2e7d32" if oc in("win","take-profit") else "#e65100" if oc=="stop-loss" else "#c62828"
        out_lbl = "✅ Win" if oc=="win" else "🎯 TP" if oc=="take-profit" else "🛡️ SL" if oc=="stop-loss" else "❌ Loss"
        pnl_val = t.get("pnl",0) or 0
        pc  = "#2e7d32" if pnl_val>=0 else "#c62828"
        ps  = ("+$" if pnl_val>=0 else "-$")+f"{abs(pnl_val):.2f}"
        fees_str = f"${t.get('fees',0) or 0:.2f}"
        sc_b = badge(f"{t['score']}/10", score_bg(t['score']), score_color(t['score']))
        rows_html += f"""
<tr style="border-bottom:1px solid #f5f5f5;">
  <td style="padding:7px 10px;font-size:11px;color:#aaa;">{t['date']}<br><span style="font-size:9px;color:#ddd;">ID: {t.get('id','—')}</span></td>
  <td style="padding:7px 10px;">
    <span style="background:{sig_bg};color:{sig_col};font-size:11px;font-weight:700;padding:3px 7px;border-radius:4px;">{sig_lbl}</span>
  </td>
  <td class="pt-score-col" style="padding:7px 10px;text-align:center;">{sc_b}</td>
  <td style="padding:7px 10px;font-size:11px;color:#888;text-align:center;">${t['size']}</td>
  <td class="pt-vol-col" style="padding:7px 10px;font-size:11px;color:#777;text-align:center;">{t.get('vol',0) or 0}%</td>
  <td style="padding:7px 10px;font-size:13px;font-weight:700;color:{pc};text-align:right;">{ps}<br><span style="font-size:9px;color:#ccc;font-weight:400;">fee {fees_str}</span></td>
  <td style="padding:7px 10px;">
    <span style="background:{out_bg};color:{out_col};font-size:10px;font-weight:700;padding:3px 7px;border-radius:4px;white-space:nowrap;">{out_lbl}</span>
  </td>
</tr>"""

    return (
        divider(color="#e5e5e5", style="dashed") +
        row(f"""
{section_label("📋 Paper trading — auto-executed · $1,000 simulated")}
<p style="margin:-6px 0 12px;font-size:11px;color:#aaa;">Trades auto-executed when all 3 filters align · Separate from Kronos accuracy tracker</p>
{metric_cards}
{open_alert}
<table width="100%" cellpadding="0" cellspacing="0" style="border-radius:8px;overflow:hidden;border:1px solid #e5e5e5;">
<tr style="background:#f5f5f5;">
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Date</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Signal</th>
  <th class="pt-score-col" style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Score</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Size</th>
  <th class="pt-vol-col" style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Vol%</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:right;">P&amp;L</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Result</th>
</tr>
{rows_html}
</table>
<p style="margin:8px 0 0;font-size:10px;color:#ccc;">Simulated only. Not financial advice.</p>""", pad="12px 20px")
    )

# ─── footer ───────────────────────────────────────────────────────────────────

def build_footer():
    return row("""
<p style="margin:0;font-size:10px;color:#ccc;line-height:1.6;">
Upside % green ≥65% bullish · red ≤35% bearish &nbsp;·&nbsp;
Score = conviction 1–10 · $ = suggested position &nbsp;·&nbsp;
Brier: 0.0 perfect, 0.25 random &nbsp;·&nbsp;
<a href="https://github.com/lemleysergio-cloud/kronos-tracker" style="color:#aaa;text-decoration:none;">kronos-tracker on GitHub</a>
</p>""", bg="#f9f9f9", pad="14px 20px")

# ─── assemble ─────────────────────────────────────────────────────────────────

def build_html(records, pending_list, paper_trades, paper_balance,
               paper_pnl, paper_exposure, today_str, fg_val, fg_label):
    return (
        HEAD
        + build_header(today_str)
        + divider()
        + build_fg(fg_val, fg_label)
        + build_pending(pending_list)
        + build_scored(records)
        + build_scoreboard(records, fg_val, fg_label)
        + build_paper_trading(paper_trades, paper_balance, paper_pnl, paper_exposure)
        + divider()
        + build_footer()
        + FOOT
    )

# ─── send ─────────────────────────────────────────────────────────────────────

def send_email(html_body, today_str):
    if not GMAIL_USER or not GMAIL_PASS:
        print("ERROR: Set GMAIL_USERNAME and GMAIL_APP_PASSWORD secrets.")
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
    print(f"✓ Email sent → {RECIPIENT} [{subject}]")

# ─── main ─────────────────────────────────────────────────────────────────────

def load_paper_trades():
    """Load paper_trades.json and compute balance from auto_trader format."""
    path = REPO_ROOT / "paper_trades.json"
    trades = load_json(path, [])
    balance  = 1000.0
    exposure = 0.0
    for t in trades:
        if t.get("status") == "closed":
            balance += t.get("net_pnl", 0) or 0
        elif t.get("status") == "open":
            exposure += t.get("size", 0) or 0
    return trades, round(balance, 2), round(exposure, 2)


def fmt_paper_trade(t):
    """Normalise auto_trader trade dict for email rendering."""
    status = t.get("status","closed")
    outcome = t.get("outcome") or ("open" if status=="open" else "unknown")
    pnl = t.get("net_pnl") or 0
    return {
        "id":      t.get("id","—"),
        "date":    (t.get("entry_timestamp") or t.get("date",""))[:10],
        "signal":  t.get("signal","—"),
        "prob":    t.get("prob", 0),
        "vol":     t.get("vol_prob") or 0,
        "score":   t.get("conviction_score", 0),
        "size":    t.get("size", 0),
        "entry":   t.get("entry_price"),
        "exit":    t.get("exit_price"),
        "sl":      t.get("sl_price"),
        "tp":      t.get("tp_price"),
        "fg":      t.get("fear_greed"),
        "trend":   t.get("btc_trend_7d","—"),
        "fees":    t.get("fees") or t.get("entry_fee") or 0,
        "pnl":     pnl,
        "outcome": outcome,
        "status":  status,
        "reason":  t.get("filter_reason",""),
    }


def main():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = load_json(SCORES_FILE, [])
    pending_list = load_json(PENDING_FILE, [])

    raw_trades, paper_balance, paper_exposure = load_paper_trades()
    paper_trades = [fmt_paper_trade(t) for t in raw_trades]
    paper_pnl = round(paper_balance - 1000, 2)

    if not records and not pending_list:
        print("No data yet — skipping email.")
        return

    print("Fetching Fear & Greed index...")
    fg_val, fg_label = fetch_fear_greed()
    print(f"  {fg_val} ({fg_label})" if fg_val else "  Could not fetch.")

    html = build_html(records, pending_list, paper_trades, paper_balance,
                      paper_pnl, paper_exposure, today_str, fg_val, fg_label)
    send_email(html, today_str)

if __name__ == "__main__":
    main()
