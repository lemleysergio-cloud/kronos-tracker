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

# Cloudflare Worker endpoint that triggers the GitHub Actions workflow.
# Clicking this in the email fires kronos_tracker.py --all + auto_trader.py
# immediately, same as clicking "Run workflow" in GitHub.
TRIGGER_URL = "https://kronos-trigger.sergio-b69.workers.dev/trigger?key=kronos2026"
LIVE_CHART_URL = "https://www.tradingview.com/symbols/BTCUSD/"

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

# ─── run-now button ────────────────────────────────────────────────────────────

def build_run_now_button():
    """Two action buttons side by side:
    - Run Latest Prediction Now: triggers GitHub Actions via Cloudflare Worker
    - View Live BTC Price: opens TradingView's real-time chart in browser
      (a link, not an embed — email can't run the JS a live widget needs,
      so this always shows the true current price no matter when you click it)."""
    return row(f"""
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:4px 0 8px;">
  <table cellpadding="0" cellspacing="0"><tr>
    <td style="padding-right:6px;">
      <a href="{TRIGGER_URL}"
         style="display:inline-block;background:#1a1a2e;color:#ffffff;text-decoration:none;
                font-size:12px;font-weight:700;padding:12px 18px;border-radius:8px;
                letter-spacing:.02em;white-space:nowrap;">
        ⚡ Run Latest Prediction
      </a>
    </td>
    <td style="padding-left:6px;">
      <a href="{LIVE_CHART_URL}"
         style="display:inline-block;background:#f7931a;color:#ffffff;text-decoration:none;
                font-size:12px;font-weight:700;padding:12px 18px;border-radius:8px;
                letter-spacing:.02em;white-space:nowrap;">
        📈 View Live BTC Price
      </a>
    </td>
  </tr></table>
  <p style="margin:8px 0 0;font-size:11px;color:#bbb;">
    Run: scrapes current hour + scores + trades (2-4 min) &nbsp;·&nbsp; Live price: opens TradingView, always current
  </p>
</td></tr>
</table>""", bg="#ffffff", pad="0px 20px 16px")

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
            ref_price = r.get("current_price")
            ref_price_str = f"${ref_price:,.0f}" if ref_price else "—"
            hz = r.get("horizon", "24h")
            hz_b = badge(hz, "#eeedfe" if hz=="1h" else "#e6f1fb",
                             "#3c3489" if hz=="1h" else "#0c447c")
            tgt = r.get("mean_forecast_close")
            tgt_str = f"${tgt:,.0f}" if tgt else "—"
            rows_html += f"""
<tr style="border-bottom:1px solid #f0f0f0;">
  <td style="padding:7px 10px;font-size:11px;color:#aaa;">{hour}<br>
    <span class="et-col" style="font-size:10px;color:#ccc;">{utc_to_et(hour)}</span>
  </td>
  <td style="padding:7px 10px;font-size:13px;font-weight:700;color:{up_col};">{up}%
    <br><span style="font-size:10px;font-weight:500;">{dw}</span>
  </td>
  <td style="padding:7px 10px;text-align:center;">{hz_b}</td>
  <td style="padding:7px 10px;font-size:11px;font-weight:600;color:#555;text-align:center;">{ref_price_str}<br><span style="font-size:10px;color:#aaa;">→ {tgt_str}</span></td>
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
      <th style="padding:5px 10px;font-size:9px;color:#999;font-weight:700;text-transform:uppercase;text-align:center;">Hz</th>
      <th style="padding:5px 10px;font-size:9px;color:#999;font-weight:700;text-transform:uppercase;text-align:center;">BTC → target</th>
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
            p0 = r.get("price_t0") or r.get("current_price")
            p24 = r.get("price_t24")
            ref_str = f"${p0:,.0f}" if p0 else "—"
            exit_str = f"→ ${p24:,.0f}" if p24 else ""
            rows_html += f"""
<tr style="border-bottom:1px solid #f5f5f5;">
  <td style="padding:6px 10px;font-size:11px;color:#aaa;">{hour}
    <br><span class="et-col" style="font-size:10px;color:#ccc;">{utc_to_et(hour)}</span>
  </td>
  <td style="padding:6px 10px;font-size:12px;font-weight:700;color:{prob_color(r['upside_prob'])};">{up}%</td>
  <td style="padding:6px 10px;font-size:10px;color:#666;text-align:center;">{ref_str}<br><span style="font-size:9px;color:#ccc;">{exit_str}</span></td>
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
      <th style="padding:5px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">BTC @ call → 24h</th>
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

    # Horizon split — which timeframe is Kronos actually good at
    by_h = {"1h": [], "24h": []}
    for r in records:
        by_h.setdefault(r.get("horizon","24h"), []).append(r)

    horizon_block = ""
    h_cells = ""
    for hz in ["1h", "24h"]:
        recs = by_h.get(hz, [])
        if not recs: continue
        st_h = compute_stats(recs)
        acc  = st_h["direction_accuracy_pct"]
        col  = "#2e7d32" if acc > 55 else "#e65100" if acc >= 45 else "#c62828"
        errs = [r.get("target_error_pct") for r in recs if r.get("target_error_pct") is not None]
        avg_err = f"{sum(errs)/len(errs):.2f}% avg target error" if errs else "—"
        h_cells += f"""
      <td style="text-align:center;padding:8px;border-right:1px solid #eee;">
        <p style="margin:0;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;">{hz} horizon</p>
        <p style="margin:3px 0 0;font-size:22px;font-weight:800;color:{col};">{acc}%</p>
        <p style="margin:1px 0 0;font-size:10px;color:#bbb;">{st_h['dir_correct']}/{st_h['n']} · brier {st_h['avg_brier_score']}</p>
        <p style="margin:1px 0 0;font-size:9px;color:#ccc;">{avg_err}</p>
      </td>"""
    if h_cells:
        horizon_block = f"""
  <p style="margin:12px 0 4px;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.05em;">Accuracy by horizon</p>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #eee;border-bottom:1px solid #eee;">
    <tr>{h_cells}</tr>
  </table>"""

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

  {horizon_block}

  <p style="margin:12px 0 4px;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.05em;">Recent call history</p>
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

def build_barbell(suggestions, trades, summary):
    """Kalshi barbell strike suggestions + trade history.
    Strikes are computed from Kronos's dollar target; sizing is deliberately
    NOT shown because live Kalshi odds move constantly — you size in the moment."""

    if not suggestions and not trades:
        return (divider(color="#e5e5e5", style="dashed") + row(f"""
{section_label("🎯 Kalshi barbell — strike suggestions")}
<table width="100%" cellpadding="0" cellspacing="0" style="border-radius:8px;border:1px solid #e5e5e5;">
<tr><td style="padding:20px;text-align:center;">
  <p style="margin:0;font-size:24px;">🔍</p>
  <p style="margin:8px 0 4px;font-size:13px;font-weight:600;color:#1a1a2e;">No qualifying signals right now</p>
  <p style="margin:0;font-size:11px;color:#aaa;">Predicted move too small for a meaningful barbell.</p>
</td></tr></table>""", pad="12px 20px"))

    # ── suggestion cards ──
    cards = ""
    for horizon in ["1h", "24h"]:
        s = suggestions.get(horizon)
        if not s: continue
        is_up   = s["direction"] == "up"
        accent  = "#2e7d32" if is_up else "#c62828"
        bg      = "#e8f5e9" if is_up else "#ffebee"
        arrow   = "▲" if is_up else "▼"
        side    = s["side"].upper()
        rng = ""
        if s.get("forecast_low") and s.get("forecast_high"):
            rng = (f'<p style="margin:6px 0 0;font-size:10px;color:#bbb;">'
                   f'Monte Carlo range: ${s["forecast_low"]:,.0f} – ${s["forecast_high"]:,.0f}</p>')

        vol_lbl = s.get("vol_label", "moderate")
        vol_col = "#c62828" if vol_lbl=="high" else "#2e7d32" if vol_lbl=="low" else "#e65100"
        vol_note = {"high":"widened for expected chop","low":"tightened, calm move expected","moderate":"baseline width"}[vol_lbl]
        vol_badge = badge(f"{vol_lbl} vol {s.get('vol_prob',0)}%", "#fff", vol_col)

        kalshi_block = ""
        kalshi = s.get("kalshi")
        if kalshi:
            kalshi_rows = ""
            for leg_name, leg_label in [("conservative","🛡️ Cons"), ("aggressive","🎲 Aggr")]:
                k = kalshi.get(leg_name)
                if not k: continue
                edge = k["edge_pts"]
                edge_col = "#2e7d32" if edge > 3 else "#c62828" if edge < -3 else "#888"
                edge_str = f"{'+' if edge>=0 else ''}{edge} pts"
                kalshi_rows += (f'<span style="display:inline-block;margin-right:10px;">'
                                f'{leg_label}: {k["yes_price_cents"]}¢ '
                                f'<span style="color:{edge_col};font-weight:700;">({edge_str})</span></span>')
            if kalshi_rows:
                kalshi_block = f"""
      <tr><td colspan="2" style="padding:8px 14px;background:#f5f7ff;border-top:1px solid #e5e5e5;">
        <p style="margin:0 0 3px;font-size:9px;font-weight:700;color:#5a5fc7;text-transform:uppercase;letter-spacing:.05em;">Live Kalshi snapshot vs Kronos</p>
        <p style="margin:0;font-size:11px;">{kalshi_rows}</p>
        <p style="margin:3px 0 0;font-size:9px;color:#aaa;">Edge = Kronos % − Kalshi ¢. Positive = Kronos more bullish than the market. Snapshot only — check live price before acting.</p>
      </td></tr>"""

        cards += f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;border-radius:8px;overflow:hidden;border:1px solid {accent};">
  <tr style="background:{bg};">
    <td style="padding:10px 14px;">
      <span style="font-size:13px;font-weight:800;color:{accent};">{arrow} {horizon.upper()} · {s['kronos_prob']}% upside</span>
      <span style="font-size:11px;color:{accent};margin-left:8px;">
        ${s['entry_price']:,.0f} → ${s['forecast_price']:,.0f} ({'+' if is_up else '-'}{s['move_pct']:.2f}%)
      </span>
      <br>{vol_badge}<span style="font-size:9px;color:#999;margin-left:6px;">{vol_note}</span>
    </td>
  </tr>
  <tr><td style="padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr class="mob-metric-row">
        <td style="width:50%;padding:12px 14px;border-right:1px solid #f0f0f0;vertical-align:top;">
          <p style="margin:0;font-size:9px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.06em;">🛡️ Conservative leg</p>
          <p style="margin:4px 0 0;font-size:17px;font-weight:800;color:#1a1a2e;">{side} ${s['conservative_strike']:,}</p>
          <p style="margin:2px 0 0;font-size:11px;color:#888;">{s['cushion_pct']:.2f}% cushion · bigger stake</p>
        </td>
        <td style="width:50%;padding:12px 14px;vertical-align:top;">
          <p style="margin:0;font-size:9px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.06em;">🎲 Aggressive leg</p>
          <p style="margin:4px 0 0;font-size:17px;font-weight:800;color:{accent};">{side} ${s['aggressive_strike']:,}</p>
          <p style="margin:2px 0 0;font-size:11px;color:#888;">{s['stretch_pct']:.2f}% stretch · small stake</p>
        </td>
      </tr>
    </table>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #f0f0f0;">
      <tr><td style="padding:8px 14px;background:#fafafa;">
        <p style="margin:0;font-size:10px;color:#aaa;">Called at {s['kronos_timestamp']} UTC</p>
        {rng}
      </td></tr>
      {kalshi_block}
    </table>
  </td></tr>
</table>"""

    # ── sizing reminder ──
    sizing_note = f"""
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff8e1;border:1px solid #ffb300;border-radius:8px;margin-bottom:12px;">
<tr><td style="padding:10px 14px;">
  <p style="margin:0;font-size:11px;font-weight:700;color:#e65100;">⚠️ Size live — Kalshi/Robinhood prices move constantly</p>
</td></tr></table>"""

    # ── performance summary ──
    pnl      = summary.get("total_pnl", 0) or 0
    pnl_col  = "#2e7d32" if pnl >= 0 else "#c62828"
    pnl_str  = ("+$" if pnl >= 0 else "-$") + f"{abs(pnl):.2f}"
    cwp      = summary.get("conservative_win_pct")
    awp      = summary.get("aggressive_win_pct")

    perf = f"""
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9f9f9;border:1px solid #e5e5e5;border-radius:8px;margin-bottom:12px;">
<tr><td style="padding:14px;">
  <p style="margin:0 0 8px;font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.06em;">Barbell track record</p>
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="text-align:center;padding:4px;border-right:1px solid #eee;">
        <p style="margin:0;font-size:10px;color:#aaa;">Realized P&amp;L</p>
        <p style="margin:2px 0 0;font-size:20px;font-weight:800;color:{pnl_col};">{pnl_str}</p>
      </td>
      <td style="text-align:center;padding:4px;border-right:1px solid #eee;">
        <p style="margin:0;font-size:10px;color:#aaa;">🛡️ Conservative</p>
        <p style="margin:2px 0 0;font-size:20px;font-weight:800;color:#1a1a2e;">{str(cwp)+'%' if cwp is not None else '—'}</p>
      </td>
      <td style="text-align:center;padding:4px;border-right:1px solid #eee;">
        <p style="margin:0;font-size:10px;color:#aaa;">🎲 Aggressive</p>
        <p style="margin:2px 0 0;font-size:20px;font-weight:800;color:#1a1a2e;">{str(awp)+'%' if awp is not None else '—'}</p>
      </td>
      <td style="text-align:center;padding:4px;">
        <p style="margin:0;font-size:10px;color:#aaa;">Trades</p>
        <p style="margin:2px 0 0;font-size:20px;font-weight:800;color:#1a1a2e;">{summary.get('closed',0)}</p>
        <p style="margin:0;font-size:9px;color:#ccc;">{summary.get('open',0)} open</p>
      </td>
    </tr>
  </table>
</td></tr></table>"""

    # ── closed trade log ──
    log = ""
    closed = [t for t in trades if t.get("status") == "closed"]
    if closed:
        rows = ""
        for t in reversed(closed[-10:]):
            cw = t.get("conservative_won")
            aw = t.get("aggressive_won")
            tp = t.get("total_pnl", 0) or 0
            tc = "#2e7d32" if tp >= 0 else "#c62828"
            cons = t.get("conservative", {})
            aggr = t.get("aggressive", {})
            rows += f"""
<tr style="border-bottom:1px solid #f5f5f5;">
  <td style="padding:6px 10px;font-size:11px;color:#aaa;">{t.get('kronos_timestamp','')[:10]}<br>
    <span style="font-size:9px;color:#ddd;">{t.get('id','')}</span></td>
  <td style="padding:6px 10px;">{badge(t.get('horizon','—'), '#e6f1fb', '#0c447c')}</td>
  <td style="padding:6px 10px;font-size:11px;color:#666;text-align:center;">${cons.get('strike',0):,}<br>
    <span style="font-size:13px;">{'✅' if cw else '❌'}</span></td>
  <td style="padding:6px 10px;font-size:11px;color:#666;text-align:center;">${aggr.get('strike',0):,}<br>
    <span style="font-size:13px;">{'✅' if aw else '❌'}</span></td>
  <td style="padding:6px 10px;font-size:11px;color:#888;text-align:center;">${t.get('settle_price',0):,.0f}</td>
  <td style="padding:6px 10px;font-size:13px;font-weight:700;color:{tc};text-align:right;">
    {'+$' if tp>=0 else '-$'}{abs(tp):.2f}</td>
</tr>"""
        log = f"""
<table width="100%" cellpadding="0" cellspacing="0" style="border-radius:8px;overflow:hidden;border:1px solid #e5e5e5;">
<tr style="background:#f5f5f5;">
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">Date</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:left;">H</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">🛡️ Cons</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">🎲 Aggr</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:center;">Settled</th>
  <th style="padding:6px 10px;font-size:9px;color:#bbb;font-weight:700;text-transform:uppercase;text-align:right;">P&amp;L</th>
</tr>{rows}</table>"""

    return (divider(color="#e5e5e5", style="dashed") + row(f"""
{section_label("🎯 Kalshi barbell — strike suggestions")}

{cards}
{sizing_note}
{perf}
{log}""", pad="12px 20px"))


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

def build_html(records, pending_list, barbell_suggestions, barbell_trades,
               barbell_summary, today_str, fg_val, fg_label):
    return (
        HEAD
        + build_header(today_str)
        + build_run_now_button()
        + divider()
        + build_fg(fg_val, fg_label)
        + build_pending(pending_list)
        + build_scored(records)
        + build_scoreboard(records, fg_val, fg_label)
        + build_barbell(barbell_suggestions, barbell_trades, barbell_summary)
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

def load_barbell_data():
    """Load barbell trades + compute suggestions using barbell_tracker logic."""
    trades_path = REPO_ROOT / "barbell_trades.json"
    trades = load_json(trades_path, [])

    suggestions = {}
    summary = {"closed": 0, "open": 0, "open_stake": 0.0, "total_pnl": 0.0,
               "conservative_win_pct": None, "aggressive_win_pct": None}

    try:
        import importlib.util
        bt_path = Path(__file__).parent / "barbell_tracker.py"
        if bt_path.exists():
            spec = importlib.util.spec_from_file_location("barbell_tracker", bt_path)
            bt   = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bt)
            suggestions = bt.build_suggestions()
            summary     = bt.portfolio_summary(trades)
    except Exception as e:
        print(f"  Barbell module load failed: {e}")

    return suggestions, trades, summary


def main():
    today_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records      = load_json(SCORES_FILE, [])
    pending_list = load_json(PENDING_FILE, [])

    suggestions, barbell_trades, summary = load_barbell_data()

    if not records and not pending_list:
        print("No data yet — skipping email.")
        return

    print("Fetching Fear & Greed index...")
    fg_val, fg_label = fetch_fear_greed()
    print(f"  {fg_val} ({fg_label})" if fg_val else "  Could not fetch.")

    html = build_html(records, pending_list, suggestions, barbell_trades,
                      summary, today_str, fg_val, fg_label)
    send_email(html, today_str)


if __name__ == "__main__":
    main()
