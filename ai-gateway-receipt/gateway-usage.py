#!/usr/bin/env python3
"""Live AI Gateway spend views for this machine's key, drawn with kitty graphics.

  gateway-usage.py 24h   views of the last 24 hours, refreshed every 15 minutes
  gateway-usage.py 30d   views of the last 30 days, refreshed every hour
  gateway-usage.py receipt   one till receipt with a last-24-hours and a last-30-days column
                             (24h column every 30 minutes, 30d column hourly).
                             Small panes get a compact receipt so the text stays readable

Keys: left/right (or v) change view, r refreshes now, q quits. The last view used is
remembered per pane. Resizing the pane redraws without refetching.

Views
  24h  clock      24-hour clock face; each wedge's length is that hour's spend
       gauge      speedometer of the last 24 hours against your 30-day daily average
       bars       hourly stacked bars by model
  30d  calendar   GitHub-style heatmap of daily spend
       punchcard  weekday x hour grid of when the money goes (local time)
       bubbles    one bubble per model, area = spend
       receipt    itemized till receipt with tokens and requests
       bars       daily stacked bars by model
  receipt till    the two-column receipt; its 24h column costs 1 query per refresh, plus
                  about 10 once a day for yesterday's hourly rows (cached on disk), so about
                  $0.40 a day if left open

Data: https://ai-gateway.vercel.sh/v1/report?api_key_id=self, which costs $0.005 per query.
A view fetches only what it needs (1 to 5 queries) when it is shown and then on each refresh,
so a pane left open all day costs between $0.12 and $2.
"""
import base64, colorsys, datetime as dt, fcntl, hashlib, html, json, math, os, select, signal
import struct, subprocess, sys, tempfile, termios, time, tty, urllib.parse, urllib.request

MODE = sys.argv[1] if len(sys.argv) > 1 else "24h"
REFRESH_S = {"24h": 15 * 60, "30d": 60 * 60, "receipt": 30 * 60}[MODE]
VIEWS = {"24h": ["clock", "gauge", "bars"], "30d": ["calendar", "punchcard", "bubbles", "receipt", "bars"],
         "receipt": ["till"]}[MODE]
STATE = os.path.expanduser(f"~/.cache/gateway-usage/{MODE}.view")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REPORT = "https://ai-gateway.vercel.sh/v1/report"
LABEL = os.environ.get("GATEWAY_RECEIPT_LABEL", "AI Gateway key")  # second line of the receipt header
SCALE = 2  # render at 2x so text stays sharp on Retina; the terminal scales the image to the cells
BG, FG, DIM, FAINT, GRID = "#0b0b0f", "#fafafa", "#a1a1aa", "#71717a", "#27272a"
# Stacked separately in the bar views; everything else is "Other models". 24h stacks the first two.
MODELS = [
    ("anthropic/claude-opus-5.5", "Claude Opus 5.5", "#f97316"),
    ("openai/gpt-6-sol", "GPT-6 Sol", "#3b82f6"),
    ("openai/gpt-5.5", "GPT-5.5", "#22c55e"),
    ("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5", "#eab308"),
]
OTHER = ("Other models", "#a855f7")
PROVIDER_COLORS = {"anthropic": "#f97316", "openai": "#3b82f6", "google": "#22c55e",
                   "deepseek": "#a855f7", "xai": "#e4e4e7"}


# ---------------------------------------------------------------- data

def report(**params):
    params.update(api_key_id="self")
    req = urllib.request.Request(f"{REPORT}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["results"]


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def hour_key_to_local(k):
    return dt.datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).astimezone()


def range_30d():
    end = utc_now().date()
    start = end - dt.timedelta(days=29)
    return start, end


def fetch_daily30():
    start, end = range_30d()
    rows = {r["day"]: r for r in report(start_date=start.isoformat(), end_date=end.isoformat(), group_by="day")}
    days = [start + dt.timedelta(days=d) for d in range(30)]
    return [(d, rows.get(d.isoformat(), {}).get("total_cost", 0), rows.get(d.isoformat(), {}).get("request_count", 0))
            for d in days]


def fetch_hourly30():
    start, end = range_30d()
    rows = report(start_date=start.isoformat(), end_date=end.isoformat(), group_by="day", date_part="hour")
    return [(hour_key_to_local(r["hour"]), r["total_cost"]) for r in rows]


def fetch_models30():
    start, end = range_30d()
    rows = report(start_date=start.isoformat(), end_date=end.isoformat(), group_by="model")
    return sorted((r for r in rows if r["total_cost"] > 0 or r["request_count"] > 0), key=lambda r: -r["total_cost"])


def stacked(field, keys, q, stack):
    rows = report(**q)
    total = {r[field]: r["total_cost"] for r in rows}
    reqs = sum(r["request_count"] for r in rows if r[field] in keys)
    series = []
    for model, name, color in stack:
        m = {r[field]: r["total_cost"] for r in report(model=model, **q)}
        series.append((name, color, [m.get(k, 0) for k in keys]))
    other = [max(0, total.get(k, 0) - sum(s[2][i] for s in series)) for i, k in enumerate(keys)]
    series.append((*OTHER, other))
    return series, reqs


def fetch_hourly24():
    now = utc_now()
    start = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=23)
    keys = [(start + dt.timedelta(hours=h)).strftime("%Y-%m-%d %H:00:00") for h in range(24)]
    q = dict(start_date=start.date().isoformat(), end_date=now.date().isoformat(), group_by="day", date_part="hour")
    series, reqs = stacked("hour", keys, q, MODELS[:2])
    return [hour_key_to_local(k) for k in keys], series, reqs


def fetch_bars30():
    start, end = range_30d()
    keys = [(start + dt.timedelta(days=d)).isoformat() for d in range(30)]
    q = dict(start_date=start.isoformat(), end_date=end.isoformat(), group_by="day")
    series, reqs = stacked("day", keys, q, MODELS)
    return [dt.date.fromisoformat(k) for k in keys], series, reqs


FIELDS24 = ("total_cost", "request_count", "input_tokens", "output_tokens", "cached_input_tokens",
            "cache_creation_input_tokens", "reasoning_tokens")


def fetch_yesterday_hourly(day):
    """Hourly rows for one closed UTC day: the total and each of its top 8 models.

    The report API has no per-model hourly query (date_part=hour needs group_by=day), so this
    costs 2 + 1 per model. A closed day stops changing, so the result is cached on disk and
    refetched only while it may still be settling (until 2 hours after the day ends)."""
    path = os.path.expanduser(f"~/.cache/gateway-usage/hourly-{day.isoformat()}.json")
    settled_at = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(2), dt.timezone.utc).timestamp()
    try:
        cached = json.load(open(path))
        if cached["fetched_at"] >= settled_at or time.time() - cached["fetched_at"] < REFRESH_S * 2:
            return cached["hours"]
    except (OSError, ValueError, KeyError):
        pass
    q = dict(start_date=day.isoformat(), end_date=day.isoformat(), group_by="day", date_part="hour")
    models = sorted((r for r in report(group_by="model", start_date=day.isoformat(), end_date=day.isoformat())
                     if r["total_cost"] > 0), key=lambda r: -r["total_cost"])[:8]
    hours = {"": report(**q)}
    for r in models:
        hours[r["model"]] = report(model=r["model"], **q)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump({"fetched_at": time.time(), "hours": hours}, open(path, "w"))
    return hours


def fetch_models24():
    """Exact last-24-hour totals per model, overall under "total".

    Every hour of the current UTC day falls inside the window, so today needs one group_by=model
    query. The rest of the window is yesterday's later hours, read from fetch_yesterday_hourly.
    One query per refresh instead of 2 + 1 per model."""
    now = utc_now()
    start = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=23)
    keys = {(start + dt.timedelta(hours=h)).strftime("%Y-%m-%d %H:00:00") for h in range(24)}
    today = now.date()

    def add(out, r):
        for k in FIELDS24:
            out[k] += r.get(k) or 0

    total, models = dict.fromkeys(FIELDS24, 0), {}
    for r in report(group_by="model", start_date=today.isoformat(), end_date=today.isoformat()):
        add(total, r)
        add(models.setdefault(r["model"], dict.fromkeys(FIELDS24, 0)), r)
    if start.date() < today:
        for model, rows in fetch_yesterday_hourly(start.date()).items():
            out = total if model == "" else models.setdefault(model, dict.fromkeys(FIELDS24, 0))
            for r in rows:
                if r["hour"] in keys:
                    add(out, r)
    models = {m: v for m, v in models.items() if v["total_cost"] >= 0.01 or v["request_count"] > 0}
    return {"total": total, "models": models}


FETCHERS = {"daily30": fetch_daily30, "hourly30": fetch_hourly30, "models30": fetch_models30,
            "hourly24": fetch_hourly24, "bars30": fetch_bars30,
            "models24": fetch_models24}
MAX_AGE = {"models30": 60 * 60, "daily30": 60 * 60}  # seconds; others use REFRESH_S
NEEDS = {"clock": ["hourly24"], "gauge": ["hourly24", "daily30"], "calendar": ["daily30"],
         "punchcard": ["hourly30"], "bubbles": ["models30"], "receipt": ["models30"],
         "bars": ["hourly24" if MODE == "24h" else "bars30"], "till": ["models24", "models30"]}


# ---------------------------------------------------------------- drawing helpers

def esc(t):
    return html.escape(str(t), quote=True)


def money(v, cents=None):
    if cents is None:
        cents = abs(v) < 100
    return f"${v:,.2f}" if cents else f"${v:,.0f}"


def text(x, y, t, size, fill=FG, anchor="start", weight=None, family=None, extra=""):
    w = f' font-weight="{weight}"' if weight else ""
    f = f' font-family="{family}"' if family else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size:.1f}" text-anchor="{anchor}"'
            f'{w}{f}{extra}>{esc(t)}</text>')


def heat(f):
    """0..1 to a dark-ember-to-gold color."""
    f = max(0.0, min(1.0, f))
    stops = [(0.0, (0x2a, 0x12, 0x08)), (0.35, (0x9a, 0x34, 0x12)), (0.7, (0xf9, 0x73, 0x16)), (1.0, (0xfd, 0xe6, 0x8a))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if f <= b:
            t = (f - a) / (b - a)
            return "#%02x%02x%02x" % tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    return "#fde68a"


def frame(W, H, s, title, subtitle, view, fetched):
    when = fetched.astimezone().strftime("%-I:%M %p")
    o = [f'<rect width="{W}" height="{H}" fill="{BG}"/>',
         text(40 * s, 46 * s, title, 28 * s, weight=700),
         text(40 * s, 78 * s, f"{subtitle} · updated {when}", 17 * s, DIM)]
    x = 40 * s
    for v in VIEWS:
        label = v.capitalize()
        on = v == view
        o.append(text(x, H - 18 * s, label, 13 * s, FG if on else "#52525b", weight=600 if on else None))
        x += (len(label) * 7.6 + 20) * s
    o.append(text(W - 40 * s, H - 18 * s, "← → views · r refresh · q quit", 13 * s, "#52525b", "end"))
    return o


def legend(x, y, s, items, grand):
    o = [text(x, y, "By model", 17 * s, weight=600)]
    for j, (name, color, tot) in enumerate(items):
        yy = y + (38 + j * 50) * s
        o.append(f'<rect x="{x}" y="{yy-14*s}" width="{15*s}" height="{15*s}" rx="3" fill="{color}"/>')
        o.append(text(x + 24 * s, yy, name, 15 * s, "#e4e4e7"))
        o.append(text(x + 24 * s, yy + 21 * s, f"{money(tot, True)} · {tot/grand*100 if grand else 0:.0f}%", 14 * s, DIM))
    return o


def nice_step(top, ticks=6):
    return next((st for st in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000) if top / st <= ticks), 5000)


# ---------------------------------------------------------------- views

def view_bars(W, H, s, d):
    if MODE == "24h":
        times, series, reqs = d["hourly24"]
        labels, every, per = [t.strftime("%-I%p").lower() for t in times], 3, "hour"
        title = "AI Gateway spend · last 24 hours"
    else:
        days, series, reqs = d["bars30"]
        labels, every, per = [x.strftime("%b %-d") for x in days], 7, "day"
        title = "AI Gateway spend · last 30 days"
    n = len(labels)
    totals = [sum(x[2][i] for x in series) for i in range(n)]
    grand = sum(totals)
    L, R, T, B = 80 * s, 300 * s, 105 * s, 70 * s
    pw, ph = W - L - R, H - T - B
    step = nice_step(max(totals) or 1)
    ymax = step * (int((max(totals) or 1) / step) + 1)
    bw = pw / n
    o = [f"{money(grand, True)} · {reqs:,} requests · {money(grand/n, True)}/{per} avg"]
    body = []
    for v in range(0, int(ymax) + 1, step):
        y = T + ph - v / ymax * ph
        body.append(f'<line x1="{L}" x2="{L+pw}" y1="{y}" y2="{y}" stroke="{GRID}"/>')
        body.append(text(L - 10 * s, y + 5 * s, f"${v:,}", 13 * s, FAINT, "end"))
    peak = totals.index(max(totals))
    for i in range(n):
        x, y = L + i * bw + bw * 0.15, T + ph
        for _, color, vals in series:
            h = vals[i] / ymax * ph
            if h > 0:
                body.append(f'<rect x="{x:.1f}" y="{y-h:.1f}" width="{bw*0.7:.1f}" height="{h:.1f}" fill="{color}"/>')
                y -= h
        if i % every == 0 or i == n - 1:
            body.append(text(x + bw * 0.35, T + ph + 22 * s, labels[i], 13 * s, DIM, "middle"))
        if i == peak and totals[i] > 0:
            body.append(text(x + bw * 0.35, y - 8 * s, f"${totals[i]:,.0f}", 13 * s, FG, "middle"))
    body += legend(L + pw + 36 * s, T + 10 * s, s, [(nm, c, sum(v)) for nm, c, v in series], grand)
    return title, o[0], body


def wedge(cx, cy, r0, r1, a0, a1):
    p = lambda r, a: (cx + r * math.cos(a), cy + r * math.sin(a))
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p(r0, a0), p(r1, a0), p(r1, a1), p(r0, a1)
    return (f"M{x0:.1f},{y0:.1f} L{x1:.1f},{y1:.1f} A{r1:.1f},{r1:.1f} 0 0 1 {x2:.1f},{y2:.1f} "
            f"L{x3:.1f},{y3:.1f} A{r0:.1f},{r0:.1f} 0 0 0 {x0:.1f},{y0:.1f} Z")


def view_clock(W, H, s, d):
    times, series, reqs = d["hourly24"]
    totals = [sum(x[2][i] for x in series) for i in range(24)]
    grand, top = sum(totals), max(totals) or 1
    cx, cy = (W - 300 * s) / 2 + 20 * s, (H + 40 * s) / 2
    R = min((W - 300 * s) / 2 - 60 * s, (H - 190 * s) / 2)
    r0 = R * 0.34
    ang = lambda h: h / 24 * 2 * math.pi - math.pi / 2  # midnight at the top
    body = []
    step = nice_step(top, 4)
    for v in range(step, int(top) + step, step):
        rr = r0 + v / top * (R - r0)
        if rr <= R * 1.02:
            body.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rr:.1f}" fill="none" stroke="{GRID}" stroke-dasharray="3 5"/>')
            body.append(text(cx + 4 * s, cy - rr - 3 * s, f"${v}", 11 * s, "#52525b"))
    for i, t in enumerate(times):
        h = t.hour
        a0, a1 = ang(h) + 0.012, ang(h + 1) - 0.012
        body.append(f'<path d="{wedge(cx, cy, r0, R, a0, a1)}" fill="#141418"/>')
        rr = r0
        for _, color, vals in series:
            if vals[i] > 0:
                nr = rr + vals[i] / top * (R - r0)
                body.append(f'<path d="{wedge(cx, cy, rr, nr, a0, a1)}" fill="{color}"/>')
                rr = nr
    for h in range(0, 24, 3):
        a = ang(h)
        lbl = {0: "midnight", 12: "noon"}.get(h, dt.time(h).strftime("%-I%p").lower())
        body.append(text(cx + (R + 26 * s) * math.cos(a), cy + (R + 26 * s) * math.sin(a) + 5 * s, lbl, 14 * s, DIM, "middle"))
    now = dt.datetime.now().astimezone()
    a = ang(now.hour + now.minute / 60)
    body.append(f'<line x1="{cx + r0*0.9*math.cos(a):.1f}" y1="{cy + r0*0.9*math.sin(a):.1f}" '
                f'x2="{cx + (R+10*s)*math.cos(a):.1f}" y2="{cy + (R+10*s)*math.sin(a):.1f}" '
                f'stroke="{FG}" stroke-width="{2.5*s}" stroke-linecap="round"/>')
    body.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r0*0.92:.1f}" fill="{BG}" stroke="{GRID}"/>')
    body.append(text(cx, cy + 4 * s, money(grand, True), 30 * s, FG, "middle", 700))
    body.append(text(cx, cy + 28 * s, f"{reqs:,} requests", 13 * s, DIM, "middle"))
    body.append(text(cx, cy - 26 * s, "now ↗", 12 * s, FAINT, "middle"))
    peak = max(range(24), key=lambda i: totals[i])
    body += legend(W - 300 * s + 20 * s, 130 * s, s, [(nm, c, sum(v)) for nm, c, v in series], grand)
    body.append(text(W - 280 * s, H - 70 * s, f"Peak hour: {times[peak].strftime('%-I%p').lower()} · {money(totals[peak], True)}", 14 * s, DIM))
    return "Spend clock · last 24 hours", "each wedge is one hour; longer = more spend", body


def arc_path(cx, cy, r, f0, f1):
    p = lambda f: (cx + r * math.cos(math.pi * (1 - f)), cy - r * math.sin(math.pi * (1 - f)))
    (x0, y0), (x1, y1) = p(f0), p(f1)
    return f"M{x0:.1f},{y0:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1:.1f},{y1:.1f}"


def view_gauge(W, H, s, d):
    times, series, reqs = d["hourly24"]
    daily = d["daily30"]
    last24 = sum(sum(x[2]) for x in series)
    complete = [c for day, c, _ in daily[:-1]]  # today (UTC) is still filling in
    avg = sum(complete) / len(complete) if complete else 0
    best_day, best = max(((day, c) for day, c, _ in daily), key=lambda x: x[1])
    ratio = last24 / avg if avg else 0
    top = max(avg * 2.5, last24 * 1.15, best * 1.05, 1)
    hourly = [sum(x[2][i] for x in series) for i in range(24)]
    last_hour = hourly[-2] if len(hourly) > 1 else 0  # the newest hour is partial
    mood = ("Sipping 🧃" if ratio < 0.5 else "Cruising 🚲" if ratio < 0.9 else "Right on pace 🚗" if ratio < 1.2
            else "Spicy 🌶️" if ratio < 1.8 else "On fire 🔥")
    cx, cy = W / 2, H * 0.68
    R = max(60 * s, min(W * 0.36, cy - 150 * s))  # keep the arc and its labels clear of the title
    sw = 34 * s
    body = []
    zones = [(0, avg * 0.9 / top, "#16a34a"), (avg * 0.9 / top, avg * 1.2 / top, "#ca8a04"),
             (avg * 1.2 / top, avg * 1.8 / top, "#ea580c"), (avg * 1.8 / top, 1, "#dc2626")]
    for f0, f1, c in zones:
        f0, f1 = min(f0, 1), min(f1, 1)
        if f1 > f0:
            body.append(f'<path d="{arc_path(cx, cy, R, f0, f1)}" stroke="{c}" stroke-width="{sw}" fill="none" opacity="0.85"/>')
    step = nice_step(top, 6)
    for v in range(0, int(top) + 1, step):
        f = v / top
        a = math.pi * (1 - f)
        x0, y0 = cx + (R - sw / 2 - 4 * s) * math.cos(a), cy - (R - sw / 2 - 4 * s) * math.sin(a)
        x1, y1 = cx + (R - sw / 2 - 16 * s) * math.cos(a), cy - (R - sw / 2 - 16 * s) * math.sin(a)
        body.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="{DIM}" stroke-width="{1.5*s}"/>')
        tx, ty = cx + (R + sw / 2 + 22 * s) * math.cos(a), cy - (R + sw / 2 + 22 * s) * math.sin(a)
        body.append(text(tx, ty + 5 * s, f"${v:,}", 13 * s, FAINT, "middle"))
    for val, lbl in ((avg, "your avg day"), (best, f"best day ({best_day.strftime('%b %-d')})")):
        a = math.pi * (1 - min(val / top, 1))
        x0, y0 = cx + (R - sw / 2) * math.cos(a), cy - (R - sw / 2) * math.sin(a)
        x1, y1 = cx + (R + sw / 2) * math.cos(a), cy - (R + sw / 2) * math.sin(a)
        body.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="{FG}" stroke-width="{2*s}"/>')
        body.append(text(x1 + 6 * s * math.cos(a), y1 - 8 * s, lbl, 12 * s, FG, "middle"))
    a = math.pi * (1 - min(last24 / top, 1))
    body.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{cx + (R - sw)*math.cos(a):.1f}" y2="{cy - (R - sw)*math.sin(a):.1f}" '
                f'stroke="{FG}" stroke-width="{6*s}" stroke-linecap="round"/>')
    body.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{14*s}" fill="{FG}"/>')
    body.append(text(cx, cy + 58 * s, money(last24, True), 40 * s, FG, "middle", 700))
    body.append(text(cx, cy + 88 * s, f"{mood} · {ratio:.1f}× your average day", 18 * s, DIM, "middle"))
    body.append(text(cx, cy + 114 * s, f"Last full hour {money(last_hour, True)} · that pace is {money(last_hour*24)}/day",
                     14 * s, FAINT, "middle"))
    return "Burn meter · last 24 hours", f"{reqs:,} requests against a {money(avg)} average day", body


def view_calendar(W, H, s, d):
    daily = d["daily30"]
    first = daily[0][0]
    monday = first - dt.timedelta(days=first.weekday())
    weeks = (daily[-1][0] - monday).days // 7 + 1
    top = max(c for _, c, _ in daily) or 1
    L, T, panel = 90 * s, 130 * s, 300 * s
    cell = min((W - L - panel - 40 * s) / weeks, (H - T - 60 * s) / 7)
    gap = cell * 0.08
    body = []
    for r, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        body.append(text(L - 14 * s, T + r * cell + cell / 2 + 5 * s, name, 14 * s, DIM, "end"))
    for w in range(weeks):
        body.append(text(L + w * cell + gap, T - 12 * s, (monday + dt.timedelta(weeks=w)).strftime("%b %-d"), 13 * s, FAINT))
    today = utc_now().date()
    for day, c, n in daily:
        w, r = (day - monday).days // 7, day.weekday()
        x, y, sz = L + w * cell + gap, T + r * cell + gap, cell - 2 * gap
        fill = "#18181b" if c <= 0 else heat(math.sqrt(c / top))
        stroke = f' stroke="{FG}" stroke-width="{2*s}"' if day == today else ""
        body.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{sz:.1f}" height="{sz:.1f}" rx="{6*s}" fill="{fill}"{stroke}/>')
        dark = c > 0 and math.sqrt(c / top) > 0.75
        body.append(text(x + 7 * s, y + 17 * s, day.day, 12 * s, "#1c1917" if dark else FAINT))
        if c > 0 and sz > 50 * s:
            body.append(text(x + sz / 2, y + sz / 2 + 8 * s, f"${c:,.0f}", min(20 * s, sz / 3.6), "#1c1917" if dark else FG,
                             "middle", 700))
    px = W - panel + 10 * s
    total = sum(c for _, c, _ in daily)
    active = sum(1 for _, c, _ in daily if c > 0.5)
    streak = best_streak = 0
    for _, c, _ in daily:
        streak = streak + 1 if c > 0.5 else 0
        best_streak = max(best_streak, streak)
    bd, bc, _ = max(daily, key=lambda x: x[1])
    wk = [0.0] * 7
    for day, c, _ in daily:
        wk[day.weekday()] += c
    stats = [("Total", money(total)), ("Busiest day", f"{bd.strftime('%b %-d')} · {money(bc)}"),
             ("Average day", money(total / 30)), ("Days with spend", f"{active} of 30"),
             ("Longest streak", f"{best_streak} days"),
             ("Biggest weekday", ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"][wk.index(max(wk))])]
    for j, (k, v) in enumerate(stats):
        body.append(text(px, T + j * 54 * s, k, 13 * s, FAINT))
        body.append(text(px, T + j * 54 * s + 22 * s, v, 18 * s, FG, weight=600))
    gy = T + len(stats) * 54 * s + 10 * s
    for i in range(10):
        body.append(f'<rect x="{px + i*20*s:.1f}" y="{gy:.1f}" width="{18*s}" height="{12*s}" rx="2" fill="{heat((i+0.5)/10)}"/>')
    body.append(text(px, gy + 30 * s, "less", 12 * s, FAINT))
    body.append(text(px + 198 * s, gy + 30 * s, "more", 12 * s, FAINT, "end"))
    return "Spend calendar · last 30 days", "one square per day (UTC); today is outlined", body


def view_punchcard(W, H, s, d):
    grid = [[0.0] * 24 for _ in range(7)]
    for t, c in d["hourly30"]:
        grid[t.weekday()][t.hour] += c
    top = max(max(r) for r in grid) or 1
    L, T, panel = 90 * s, 130 * s, 290 * s
    cw = (W - L - panel - 20 * s) / 24
    ch = (H - T - 90 * s) / 7
    body = []
    for r, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        y = T + r * ch + ch / 2
        body.append(f'<line x1="{L}" x2="{L + 24*cw:.1f}" y1="{y:.1f}" y2="{y:.1f}" stroke="#18181b"/>')
        body.append(text(L - 14 * s, y + 5 * s, name, 14 * s, DIM, "end"))
        for h in range(24):
            v = grid[r][h]
            if v > 0:
                rad = max(1.5 * s, math.sqrt(v / top) * min(cw, ch) * 0.48)
                body.append(f'<circle cx="{L + h*cw + cw/2:.1f}" cy="{y:.1f}" r="{rad:.1f}" fill="{heat(0.25 + 0.75*math.sqrt(v/top))}"/>')
    for h in range(0, 24, 3):
        body.append(text(L + h * cw + cw / 2, T + 7 * ch + 24 * s, dt.time(h).strftime("%-I%p").lower(), 13 * s, DIM, "middle"))
    pr, ph_ = max(((r, h) for r in range(7) for h in range(24)), key=lambda x: grid[x[0]][x[1]])
    total = sum(map(sum, grid)) or 1
    parts = [("Night (12–6am)", 0, 6), ("Morning (6am–12)", 6, 12), ("Afternoon (12–6pm)", 12, 18), ("Evening (6pm–12)", 18, 24)]
    px = W - panel + 10 * s
    day_names = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
    body.append(text(px, T, "Hottest slot", 13 * s, FAINT))
    body.append(text(px, T + 22 * s, f"{day_names[pr]} at {dt.time(ph_).strftime('%-I%p').lower()}", 18 * s, FG, weight=600))
    body.append(text(px, T + 44 * s, f"{money(grid[pr][ph_])} over 30 days", 13 * s, DIM))
    for j, (name, a, b) in enumerate(parts):
        share = sum(grid[r][h] for r in range(7) for h in range(a, b)) / total
        y = T + 90 * s + j * 50 * s
        body.append(text(px, y, name, 13 * s, FAINT))
        body.append(f'<rect x="{px}" y="{y + 8*s:.1f}" width="{200*s}" height="{12*s}" rx="3" fill="#18181b"/>')
        body.append(f'<rect x="{px}" y="{y + 8*s:.1f}" width="{200*s*share:.1f}" height="{12*s}" rx="3" fill="{heat(0.7)}"/>')
        body.append(text(px + 210 * s, y + 19 * s, f"{share*100:.0f}%", 13 * s, FG))
    return "When you spend · last 30 days", "bubble size is total spend in that hour of the week, in local time", body


def pack(radii):
    """Greedy circle packing: each circle goes to the free spot nearest the center."""
    placed = []
    for r in radii:
        if not placed:
            placed.append((0.0, 0.0, r))
            continue
        cands = []
        for (x, y, rr) in placed:
            for k in range(24):
                a = k / 24 * 2 * math.pi
                cands.append((x + (rr + r) * math.cos(a), y + (rr + r) * math.sin(a)))
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                (x1, y1, r1), (x2, y2, r2) = placed[i], placed[j]
                a, b = r1 + r, r2 + r
                dx, dy = x2 - x1, y2 - y1
                dd = math.hypot(dx, dy)
                if dd == 0 or dd > a + b or dd < abs(a - b):
                    continue
                l = (a * a - b * b + dd * dd) / (2 * dd)
                h = math.sqrt(max(0, a * a - l * l))
                mx, my = x1 + l * dx / dd, y1 + l * dy / dd
                cands += [(mx + h * dy / dd, my - h * dx / dd), (mx - h * dy / dd, my + h * dx / dd)]
        ok = [(math.hypot(x, y), x, y) for x, y in cands
              if all(math.hypot(x - px, y - py) >= pr + r - 1e-6 for px, py, pr in placed)]
        _, x, y = min(ok)
        placed.append((x, y, r))
    return placed


def view_bubbles(W, H, s, d):
    rows = [r for r in d["models30"] if r["total_cost"] >= 0.5]
    grand = sum(r["total_cost"] for r in d["models30"])
    circles = pack([math.sqrt(r["total_cost"]) for r in rows])
    x0 = min(x - r for x, y, r in circles); x1 = max(x + r for x, y, r in circles)
    y0 = min(y - r for x, y, r in circles); y1 = max(y + r for x, y, r in circles)
    aw, ah = W - 80 * s, H - 170 * s
    k = min(aw / (x1 - x0), ah / (y1 - y0))
    ox, oy = 40 * s + (aw - (x1 - x0) * k) / 2 - x0 * k, 110 * s + (ah - (y1 - y0) * k) / 2 - y0 * k
    body = []
    for row, (x, y, r) in zip(rows, circles):
        provider, _, name = row["model"].partition("/")
        color = PROVIDER_COLORS.get(provider, "#71717a")
        cx, cy, rr = ox + x * k, oy + y * k, r * k - 2 * s
        body.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rr:.1f}" fill="{color}" fill-opacity="0.22" stroke="{color}" stroke-width="{2*s}"/>')
        if rr > 34 * s:
            fs = max(11 * s, min(26 * s, rr / 5))
            body.append(text(cx, cy - fs * 0.2, name, fs, FG, "middle", 600))
            body.append(text(cx, cy + fs * 1.15, f"{money(row['total_cost'])} · {row['total_cost']/grand*100:.0f}%", fs * 0.8, DIM, "middle"))
        elif rr > 16 * s:
            body.append(text(cx, cy + 4 * s, money(row["total_cost"]), 11 * s, FG, "middle"))
    lx = 40 * s
    for p, c in PROVIDER_COLORS.items():
        if any(r["model"].startswith(p + "/") for r in rows):
            body.append(f'<circle cx="{lx + 6*s:.1f}" cy="{H - 48*s:.1f}" r="{6*s}" fill="{c}"/>')
            body.append(text(lx + 18 * s, H - 43 * s, p, 13 * s, DIM))
            lx += (len(p) * 8 + 40) * s
    return "Model bubbles · last 30 days", f"{len(rows)} models · bubble area = spend", body


def tokens(n):
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n/size:.1f}{unit}"
    return f"{n:.0f}"


def view_receipt(W, H, s, d):
    rows = d["models30"]
    start, end = range_30d()
    total = sum(r["total_cost"] for r in rows)
    market = sum(r.get("market_cost", r["total_cost"]) for r in rows)
    reqs = sum(r["request_count"] for r in rows)
    tin = sum(r["input_tokens"] + r["cached_input_tokens"] + r["cache_creation_input_tokens"] for r in rows)
    tout = sum(r["output_tokens"] for r in rows)
    cached = sum(r["cached_input_tokens"] for r in rows)
    reasoning = sum(r["reasoning_tokens"] for r in rows)
    Wc = 38
    line = lambda l, r="": (l[: Wc - len(r) - 1] + " " * max(1, Wc - len(l[: Wc - len(r) - 1]) - len(r)) + r)
    shown = rows[:9]
    rest = rows[9:]
    lines = [("center", "AI GATEWAY MART", True), ("center", LABEL, False),
             ("center", f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}", False), ("rule", "", False)]
    for r in shown:
        lines.append(("left", line(r["model"].split("/", 1)[1], money(r["total_cost"], True)), False))
    if rest:
        lines.append(("left", line(f"+ {len(rest)} more models", money(sum(r['total_cost'] for r in rest), True)), False))
    lines.append(("rule", "", False))
    if market - total > 0.005:  # only itemize a discount when list price is above what you paid
        lines += [("left", line("LIST PRICE", money(market, True)), False),
                  ("left", line("GATEWAY DISCOUNT", "-" + money(market - total, True)), False)]
    lines += [("left", line("TOTAL", money(total, True)), True), ("blank", "", False),
              ("left", line("REQUESTS", f"{reqs:,}"), False), ("left", line("TOKENS IN", tokens(tin)), False),
              ("left", line("  read from cache", f"{cached/tin*100 if tin else 0:.0f}%"), False),
              ("left", line("TOKENS OUT", tokens(tout)), False), ("left", line("  of which thinking", tokens(reasoning)), False),
              ("left", line("COST PER REQUEST", money(total / reqs if reqs else 0, True)), False), ("blank", "", False),
              ("center", "*** THANK YOU · COME AGAIN ***", False), ("barcode", "", False)]
    n = len(lines) + 1.5
    fs = min((H - 150 * s) / (n * 1.32), (W - 120 * s) / (Wc * 0.62))
    lh = fs * 1.32
    pw, phh = Wc * 0.602 * fs + 2.2 * fs * 2, lh * n + fs * 1.5
    px, py = (W - pw) / 2, 100 * s + max(0, (H - 160 * s - phh) / 2)
    teeth = int(pw / (fs * 0.9))
    zig = " ".join(f"L{px + (i + 0.5) * pw / teeth:.1f},{py + phh + fs*0.45:.1f} L{px + (i + 1) * pw / teeth:.1f},{py + phh:.1f}"
                   for i in range(teeth))
    body = [f'<path d="M{px:.1f},{py:.1f} L{px+pw:.1f},{py:.1f} L{px+pw:.1f},{py+phh:.1f} {zig} L{px:.1f},{py+phh:.1f} Z" '
            f'fill="#f5f0e6" transform="rotate(-1.2 {W/2:.0f} {H/2:.0f})"/>']
    g = [f'<g transform="rotate(-1.2 {W/2:.0f} {H/2:.0f})">']
    ink, mono = "#27231d", "Menlo, monospace"
    y = py + fs * 1.9
    lx = px + 2.2 * fs
    for kind, t, bold in lines:
        if kind == "center":
            g.append(text(px + pw / 2, y, t, fs * (1.25 if bold else 1), ink, "middle", 700 if bold else None, mono))
        elif kind == "left":
            g.append(text(lx, y, t, fs, ink, "start", 700 if bold else None, mono, ' xml:space="preserve"'))
        elif kind == "rule":
            g.append(text(lx, y, "-" * Wc, fs, "#8a8378", "start", None, mono))
        elif kind == "barcode":
            bx = px + pw * 0.18
            for i, ch_ in enumerate(hashlib.sha256(f"{total:.2f}{reqs}".encode()).hexdigest()[:40]):
                bw_ = (int(ch_, 16) % 3 + 1) * fs * 0.09
                if i % 2 == 0:
                    g.append(f'<rect x="{bx:.1f}" y="{y - fs:.1f}" width="{bw_:.1f}" height="{fs*1.6:.1f}" fill="{ink}"/>')
                bx += bw_ + fs * 0.08
        y += lh
    g.append("</g>")
    return "Your receipt · last 30 days", "itemized by model, list price and tokens included", body + g


def till_lines(d, compact):
    """Receipt lines. Full: 42 columns, ten models, tokens. Compact: 28 columns, five models,
    whole dollars, for small panes."""
    m30, day = d["models30"], d["models24"]
    m24, t24 = day["models"], day["total"]
    tot30 = sum(r["total_cost"] for r in m30)
    req30 = sum(r["request_count"] for r in m30)
    in30 = sum(r["input_tokens"] + r["cached_input_tokens"] + r["cache_creation_input_tokens"] for r in m30)
    in24 = t24["input_tokens"] + t24["cached_input_tokens"] + t24["cache_creation_input_tokens"]
    cache24 = t24["cached_input_tokens"] / in24 * 100 if in24 else 0
    cache30 = sum(r["cached_input_tokens"] for r in m30) / in30 * 100 if in30 else 0
    avg = tot30 / 30
    ratio = t24["total_cost"] / avg if avg else 0
    mood = ("SIPPING" if ratio < 0.5 else "CRUISING" if ratio < 0.9 else "RIGHT ON PACE" if ratio < 1.2
            else "SPICY" if ratio < 1.8 else "ON FIRE")
    Wc, cw, top = (28, 7, 5) if compact else (42, 11, 10)
    lw = Wc - 2 * cw

    def row(label, a="", b=""):
        return label[:lw - 1].ljust(lw) + a.rjust(cw) + b.rjust(cw)

    def m(v):
        if not v:
            return "-"
        return f"${v:,.0f}" if compact and v >= 100 else money(v, True)

    names = [r["model"] for r in m30]
    names += [k for k in m24 if k not in names]
    by30 = {r["model"]: r["total_cost"] for r in m30}
    shown = [n for n in names if by30.get(n, 0) >= 1 or m24.get(n, {}).get("total_cost", 0) >= 0.5][:top]
    rest = [n for n in names if n not in shown]
    now = dt.datetime.now().astimezone()
    L = [("c", "AI GATEWAY MART", 1.3, True)]
    if not compact:
        L.append(("c", LABEL, 1, False))
    L += [("c", f"PRINTED {now.strftime('%b %-d %-I:%M %p')}", 1, False), ("rule",),
          ("l", row("ITEM", "24 HRS" if not compact else "24H", "30 DAYS" if not compact else "30D"), 1, True), ("rule",)]
    for n in shown:
        name = n.split("/", 1)[-1]
        if compact:
            name = name.replace("claude-", "").replace("deepseek-", "ds-")
        L.append(("l", row(name, m(m24.get(n, {}).get("total_cost", 0)), m(by30.get(n, 0))), 1, False))
    if rest:
        o24 = max(0, t24["total_cost"] - sum(m24.get(n, {}).get("total_cost", 0) for n in shown))
        L.append(("l", row(f"+ {len(rest)} more", m(o24), m(sum(by30.get(n, 0) for n in rest))), 1, False))
    L += [("rule",), ("l", row("TOTAL", m(t24["total_cost"]), m(tot30)), 1, True)]
    if compact:
        L += [("l", row("VS AVG DAY", f"{ratio:.1f}x", m(avg)), 1, False), ("blank",),
              ("l", row("REQUESTS", f"{t24['request_count']:,}", f"{req30/1000:.1f}K"), 1, False),
              ("l", row("CACHE HIT", f"{cache24:.0f}%", f"{cache30:.0f}%"), 1, False),
              ("l", row("PER REQ", money(t24["total_cost"] / t24["request_count"] if t24["request_count"] else 0, True),
                        money(tot30 / req30 if req30 else 0, True)), 1, False), ("blank",),
              ("c", f"VIBE: {mood}", 1.05, True), ("barcode",)]
    else:
        L += [("l", row("A TYPICAL DAY", "", money(avg, True)), 1, False),
              ("l", row("VS A TYPICAL DAY", f"{ratio:.1f}x", ""), 1, False), ("blank",),
              ("l", row("REQUESTS", f"{t24['request_count']:,}", f"{req30:,}"), 1, False),
              ("l", row("TOKENS IN", tokens(in24), tokens(in30)), 1, False),
              ("l", row("  from cache", f"{cache24:.0f}%", f"{cache30:.0f}%"), 1, False),
              ("l", row("TOKENS OUT", tokens(t24["output_tokens"]), tokens(sum(r["output_tokens"] for r in m30))), 1, False),
              ("l", row("  thinking", tokens(t24["reasoning_tokens"]), tokens(sum(r["reasoning_tokens"] for r in m30))), 1, False),
              ("l", row("PER REQUEST", money(t24["total_cost"] / t24["request_count"] if t24["request_count"] else 0, True),
                        money(tot30 / req30 if req30 else 0, True)), 1, False), ("blank",),
              ("c", f"24-HOUR VIBE: {mood}", 1.1, True), ("c", "*** THANK YOU · COME AGAIN ***", 1, False), ("barcode",)]
    return L, Wc, f"{tot30:.2f}{req30}"


TILL_MIN_FONT = 9  # points; below this the full receipt gives way to the compact one


def view_till(W, H, s, d):
    """A receipt with a last-24-hours and a last-30-days column, sized to fill the pane. No title frame."""
    margin = max(4, min(W, H) * 0.025)

    def fit(L, Wc):
        n = len(L) + 2
        return min((H - 2 * margin) / (n * 1.3 + 1.6), (W - 2 * margin) / (Wc * 0.602 + 4.4))

    L, Wc, seed = till_lines(d, compact=False)
    fs = fit(L, Wc)
    if fs < TILL_MIN_FONT:
        L, Wc, seed = till_lines(d, compact=True)
        fs = fit(L, Wc)
    n = len(L) + 2
    lh = fs * 1.3
    pw, ph = Wc * 0.602 * fs + 4.4 * fs, lh * n + fs * 0.6
    px, py = (W - pw) / 2, (H - ph) / 2
    teeth = max(8, int(pw / (fs * 0.9)))
    zig = " ".join(f"L{px + pw - (i + 0.5) * pw / teeth:.1f},{py + ph + fs*0.45:.1f} L{px + pw - (i + 1) * pw / teeth:.1f},{py + ph:.1f}"
                   for i in range(teeth))
    ink, mono = "#27231d", "Menlo, monospace"
    o = [f'<path d="M{px:.1f},{py:.1f} L{px+pw:.1f},{py:.1f} L{px+pw:.1f},{py+ph:.1f} {zig} Z" fill="#f5f0e6"/>']
    y, lx = py + fs * 1.9, px + 2.2 * fs
    for item in L:
        kind = item[0]
        if kind == "c":
            o.append(text(px + pw / 2, y, item[1], fs * item[2], ink, "middle", 700 if item[3] else None, mono))
        elif kind == "l":
            o.append(text(lx, y, item[1], fs * item[2], ink, "start", 700 if item[3] else None, mono, ' xml:space="preserve"'))
        elif kind == "rule":
            o.append(text(lx, y, "-" * Wc, fs, "#8a8378", "start", None, mono))
        elif kind == "barcode":
            digits = hashlib.sha256(seed.encode()).hexdigest()[:44]
            unit = pw * 0.6 / sum(int(c, 16) % 3 + 1 + 0.9 for c in digits)
            bx = px + pw * 0.2
            for i, c in enumerate(digits):
                bw_ = (int(c, 16) % 3 + 1) * unit
                if i % 2 == 0:
                    o.append(f'<rect x="{bx:.1f}" y="{y - fs:.1f}" width="{bw_:.1f}" height="{fs*1.6:.1f}" fill="{ink}"/>')
                bx += bw_ + 0.9 * unit
        y += lh
    return None, None, o


VIEW_FUNCS = {"bars": view_bars, "clock": view_clock, "gauge": view_gauge, "calendar": view_calendar,
              "punchcard": view_punchcard, "bubbles": view_bubbles, "receipt": view_receipt, "till": view_till}


# ---------------------------------------------------------------- terminal

def cell_size():
    """Cell size in device pixels, for terminals that report 0 pixels through TIOCGWINSZ.
    Set GATEWAY_RECEIPT_CELL=WxH to override; the default fits a 7x14-point cell on a 2x display."""
    try:
        w, h = (int(x) for x in os.environ["GATEWAY_RECEIPT_CELL"].lower().split("x"))
        return w, h
    except (KeyError, ValueError):
        return 14, 28


def pane_size():
    """Columns, rows, and the image's size in points (one row is left for the cursor)."""
    rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
    cw, ch = (xp / cols, yp / rows) if xp and yp else cell_size()
    cw, ch = cw / SCALE, ch / SCALE  # device pixels to points; Chrome renders back at SCALE
    return cols, rows, int(cols * cw), int((rows - 1) * ch)


def render_svg(view, data, fetched, W, H):
    s = max(0.5, min(W / 1400, H / 720))
    if MODE == "receipt":  # a portrait pane; scale type to its width
        s = max(0.5, min(W / 600, H / 1000))
    title, subtitle, body = VIEW_FUNCS[view](W, H, s, data)
    # The till receipt floats on the pane with no backdrop; other views keep theirs.
    head = frame(W, H, s, title, subtitle, view, fetched) if title else ([] if MODE == "receipt" else [f'<rect width="{W}" height="{H}" fill="{BG}"/>'])
    parts = head + body
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'font-family="-apple-system,Helvetica,sans-serif">{"".join(parts)}</svg>')


def draw(view, data, fetched):
    cols, rows, W, H = pane_size()
    with tempfile.TemporaryDirectory() as d:
        page, png = os.path.join(d, "c.html"), os.path.join(d, "c.png")
        bg = "transparent" if MODE == "receipt" else BG
        open(page, "w").write(f'<html><body style="margin:0;background:{bg}">{render_svg(view, data, fetched, W, H)}</body></html>')
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--force-device-scale-factor={SCALE}",
                        *(["--default-background-color=00000000"] if MODE == "receipt" else []),
                        f"--window-size={W},{H}", f"--screenshot={png}", f"file://{page}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        b64 = base64.b64encode(open(png, "rb").read()).decode()
    out = ["\x1b_Ga=d,d=A,q=2\x1b\\\x1b[2J\x1b[H"]  # delete old images, clear, home
    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]
    for i, c in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        head = f"a=T,f=100,c={cols},r={rows-1},C=1,q=2,m={more}" if i == 0 else f"m={more}"
        out.append(f"\x1b_G{head};{c}\x1b\\")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def status(msg):
    sys.stdout.write(f"\x1b_Ga=d,d=A,q=2\x1b\\\x1b[2J\x1b[H{msg}\r\n")
    sys.stdout.flush()


def load_view():
    try:
        v = open(STATE).read().strip()
        return v if v in VIEWS else VIEWS[0]
    except OSError:
        return VIEWS[0]


def save_view(v):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    open(STATE, "w").write(v)


def main():
    if not os.environ.get("AI_GATEWAY_API_KEY"):
        print("AI_GATEWAY_API_KEY is not set in this shell.")
        return 1
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l")  # hide cursor
    resized = []
    signal.signal(signal.SIGWINCH, lambda *_: resized.append(1))
    view = load_view()
    cache = {}  # fetcher name -> (fetched_at, value)
    next_fetch, shown, force_all = 0.0, False, False

    def ensure(force=False):
        """Fetch what the current view needs, if missing, stale, or forced."""
        now = time.time()
        for name in NEEDS[view]:
            if force == "all" or name not in cache or now - cache[name][0] >= MAX_AGE.get(name, REFRESH_S) - 60:
                cache[name] = (now, FETCHERS[name]())

    def show():
        oldest = min(cache[n][0] for n in NEEDS[view])
        draw(view, {n: cache[n][1] for n in NEEDS[view]}, dt.datetime.fromtimestamp(oldest, dt.timezone.utc))

    try:
        while True:
            if time.time() >= next_fetch:
                if not shown:
                    status(f"Loading {view}…")
                try:
                    ensure(force=force_all)
                    force_all = False
                    show()
                    shown = True
                except Exception as e:  # keep the last chart; say why the refresh failed
                    if not shown:
                        status(f"Could not load usage: {e}\r\nRetrying in 60s. r retries now, q quits.")
                    next_fetch = time.time() + 60
                else:
                    next_fetch = time.time() + REFRESH_S
            try:
                ready, _, _ = select.select([fd], [], [], max(0.1, next_fetch - time.time()))
            except InterruptedError:
                ready = []
            if resized:
                resized.clear()
                time.sleep(0.3)  # let a drag-resize settle
                if shown:
                    show()
            if not ready:
                continue
            key = os.read(fd, 32)
            if key[:1] in (b"q", b"Q", b"\x03"):
                return 0
            if key[:1] in (b"r", b"R"):
                next_fetch, force_all = 0, "all"
                continue
            step = 1 if key in (b"\x1b[C", b"\x1bOC", b"v", b"l", b" ") else -1 if key in (b"\x1b[D", b"\x1bOD", b"h") else 0
            if step:
                view = VIEWS[(VIEWS.index(view) + step) % len(VIEWS)]
                save_view(view)
                try:
                    ensure()
                    show()
                except Exception as e:
                    status(f"Could not load {view}: {e}\r\n← → to switch view, r to retry.")
                    shown = False
    finally:
        sys.stdout.write("\x1b_Ga=d,d=A,q=2\x1b\\\x1b[?25h")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    sys.exit(main())
