# AI Gateway receipt

A till-style receipt of your [Vercel AI Gateway](https://vercel.com/docs/ai-gateway) spend for the last 24 hours and the last 30 days, side by side, drawn in a terminal pane with the kitty graphics protocol. The `24h` and `30d` modes add clock, gauge, calendar, punchcard, bubble, and bar views.

This is a personal side project, not an official Vercel tool.

## Requirements

- macOS with Google Chrome at `/Applications/Google Chrome.app` (each view is rendered to an image by headless Chrome)
- Python 3, standard library only
- A terminal that supports the kitty graphics protocol, such as kitty, Ghostty, or WezTerm
- `AI_GATEWAY_API_KEY` set in your shell

Optional:

- `GATEWAY_RECEIPT_LABEL` sets the receipt's second header line (default "AI Gateway key").
- `GATEWAY_RECEIPT_CELL=WxH` sets the cell size in device pixels, for terminals that do not report it (default `14x28`).

## Run it

```sh
python3 gateway-usage.py receipt   # the two-column receipt
python3 gateway-usage.py 24h       # last-24-hours views
python3 gateway-usage.py 30d       # last-30-days views
```

Keys: left/right (or `v`) change view, `r` refreshes, `q` quits.

## Cost

Data comes from the [Custom Reporting API](https://vercel.com/docs/ai-gateway/capabilities/usage) (`/v1/report?api_key_id=self`), which charges $5 per 1,000 queries. The receipt left open all day costs about $0.40:

- 24-hour column: one query per refresh (every 30 minutes). Every hour of the current UTC day falls inside the rolling window, so one `group_by=model` query covers today.
- The rest of the window is yesterday's later hours. A finished day stops changing, so its per-model hourly rows (about 10 queries) are fetched once and cached in `~/.cache/gateway-usage/`.
- 30-day column: one query an hour.

The report covers only the API key that makes the request, so it shows your own spend, not your team's.
