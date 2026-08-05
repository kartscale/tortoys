#!/usr/bin/env python3
"""
Tortoys Google Ads x Shopify dashboard pipeline.

Two modes:
  --mode seed   Build tortoys_data.json + index.html from scratch out of full-history
                CSV exports (used once to initialize the dashboard).
  --mode merge  Take an existing tortoys_data.json (the dashboard's current persisted
                dataset) plus a set of single-day CSV exports (yesterday's reports) and
                append/replace that one day's rows in every section, then re-render
                index.html. This is what the daily scheduled run uses.

Usage (seed):
  python3 pipeline.py --mode seed \
    --ads-city Kartscale_Report_R1.csv --ads-keyword SearchCampaignKeywordReport_R2.csv \
    --ads-product Kartscale_R3.csv --shopify-sales Kartscale_X_Tortoys.csv \
    --shopify-sessions KS_Sessions_by_landing_page.csv \
    --template dashboard_template.html --chartjs chart.umd.js \
    --out-data tortoys_data.json --out-html index.html --client-name Tortoys

Usage (merge -- yesterday's single-day exports):
  python3 pipeline.py --mode merge \
    --existing-data tortoys_data.json \
    --ads-city ads_city_yesterday.csv --ads-keyword ads_keyword_yesterday.csv \
    --ads-product ads_product_yesterday.csv --shopify-sales shopify_sales_yesterday.csv \
    --shopify-sessions shopify_sessions_yesterday.csv \
    --template dashboard_template.html --chartjs chart.umd.js \
    --out-data tortoys_data.json --out-html index.html --client-name Tortoys

Column matching: exact header names are tried first (matching Google Ads / Shopify's
standard export headers), falling back to a case-insensitive substring search so minor
export wording drift doesn't hard-fail the daily run. If a required column truly can't
be found, the script fails loudly with the headers it saw rather than guessing.

Design discipline:
  - Every ratio metric (ROAS, CTR, Conv. rate, CPC) is computed by the dashboard's own
    JS as sum(numerator)/sum(denominator) over the selected range -- this script only
    stores raw numerators/denominators per day, never pre-computed ratios.
  - All "day" keys are the plain YYYY-MM-DD strings as exported; the dashboard JS treats
    them as UTC dates.
  - Merge mode is idempotent per day: for every section, any existing rows whose "day"
    is present in the new data are dropped before the new rows are appended, so re-running
    the same day's merge twice (e.g. a retried scheduled run) does not double-count.
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def to_f(v):
    if v is None:
        return 0.0
    v = str(v).strip()
    if v in ("", "--", " --", "-"):
        return 0.0
    v = v.replace(",", "").replace("%", "").replace("₹", "")
    try:
        return float(v)
    except ValueError:
        return 0.0


def slugify(title):
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def strip_suffix(slug):
    return re.sub(r"-\d+$", "", slug)


def find_col(headers, name_or_patterns, required=True):
    """Exact match first (case-insensitive), then substring fallback."""
    patterns = name_or_patterns if isinstance(name_or_patterns, list) else [name_or_patterns]
    lowered = [h.strip().lower() if h else "" for h in headers]
    for p in patterns:
        pl = p.lower()
        for i, h in enumerate(lowered):
            if h == pl:
                return i
    for p in patterns:
        pl = p.lower()
        for i, h in enumerate(lowered):
            if pl in h:
                return i
    if required:
        raise ValueError(f"Could not find column matching {patterns} in headers: {headers}")
    return None


def read_google_ads_csv(path):
    """Google Ads exports: 2 title/date-range rows, then the real header row."""
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    # Find the header row: the first row whose first cell is a recognizable column name
    # like "Day" or "Product Title" or "Campaign" (Google always puts 2 meta rows first,
    # but tolerate 0-2 meta rows in case an export is pasted without them).
    header_idx = 0
    for i, r in enumerate(rows[:5]):
        if r and r[0].strip() in ("Day", "Product Title", "Campaign"):
            header_idx = i
            break
    header = rows[header_idx]
    data = rows[header_idx + 1:]
    return header, [r for r in data if any(c.strip() for c in r)]


def read_shopify_csv(path):
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    header = rows[0]
    data = rows[1:]
    return header, [r for r in data if any(c.strip() for c in r)]


# ---------------------------------------------------------------------------
# Loaders -- each returns day-keyed rows ready to merge into RAW's sections
# ---------------------------------------------------------------------------

def load_ads_city(path):
    header, data = read_google_ads_csv(path)
    idx = {
        "day": find_col(header, ["Day"]),
        "campaign": find_col(header, ["Campaign"]),
        "city": find_col(header, ["City (User location)", "City"]),
        "cost": find_col(header, ["Cost"]),
        "impr": find_col(header, ["Impr."]),
        "clicks": find_col(header, ["Clicks"]),
        "conv": find_col(header, ["Conversions"]),
        "conv_value": find_col(header, ["Conv. value"]),
    }
    campaign_day = defaultdict(lambda: {"spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0, "conv_value": 0.0})
    city_day = defaultdict(lambda: {"spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0, "conv_value": 0.0})
    daily_ads = defaultdict(lambda: {"spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0, "conv_value": 0.0})
    for r in data:
        day = r[idx["day"]]
        campaign = r[idx["campaign"]]
        city = r[idx["city"]].strip() or "Unknown"
        cost = to_f(r[idx["cost"]]); impr = to_f(r[idx["impr"]]); clicks = to_f(r[idx["clicks"]])
        conv = to_f(r[idx["conv"]]); convval = to_f(r[idx["conv_value"]])

        d = daily_ads[day]
        d["spend"] += cost; d["impressions"] += impr; d["clicks"] += clicks
        d["conversions"] += conv; d["conv_value"] += convval

        cd = campaign_day[(day, campaign)]
        cd["spend"] += cost; cd["impressions"] += impr; cd["clicks"] += clicks
        cd["conversions"] += conv; cd["conv_value"] += convval

        ct = city_day[(day, city)]
        ct["spend"] += cost; ct["impressions"] += impr; ct["clicks"] += clicks
        ct["conversions"] += conv; ct["conv_value"] += convval

    campaign_out = [{"day": d, "campaign": c, "spend": round(v["spend"], 2), "impressions": int(v["impressions"]),
                      "clicks": int(v["clicks"]), "conversions": round(v["conversions"], 2), "conv_value": round(v["conv_value"], 2)}
                     for (d, c), v in campaign_day.items()]
    city_out = [{"day": d, "city": c, "spend": round(v["spend"], 2), "impressions": int(v["impressions"]),
                 "clicks": int(v["clicks"]), "conversions": round(v["conversions"], 2), "conv_value": round(v["conv_value"], 2)}
                for (d, c), v in city_day.items()]
    return daily_ads, campaign_out, city_out


def load_ads_keyword(path):
    header, data = read_google_ads_csv(path)
    idx = {
        "day": find_col(header, ["Day"]),
        "keyword": find_col(header, ["Search keyword", "Keyword"]),
        "impr": find_col(header, ["Impr."]),
        "cost": find_col(header, ["Cost"]),
        "clicks": find_col(header, ["Clicks"]),
        "conv": find_col(header, ["Conversions"]),
        "conv_value": find_col(header, ["Conv. value"]),
    }
    keyword_day = defaultdict(lambda: {"spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0, "conv_value": 0.0})
    for r in data:
        day = r[idx["day"]]
        kw = r[idx["keyword"]].strip() or "(unknown)"
        k = keyword_day[(day, kw)]
        k["spend"] += to_f(r[idx["cost"]]); k["impressions"] += to_f(r[idx["impr"]])
        k["clicks"] += to_f(r[idx["clicks"]]); k["conversions"] += to_f(r[idx["conv"]])
        k["conv_value"] += to_f(r[idx["conv_value"]])
    return [{"day": d, "keyword": k, "spend": round(v["spend"], 2), "impressions": int(v["impressions"]),
             "clicks": int(v["clicks"]), "conversions": round(v["conversions"], 2), "conv_value": round(v["conv_value"], 2)}
            for (d, k), v in keyword_day.items()]


def load_ads_product(path):
    header, data = read_google_ads_csv(path)
    idx = {
        "day": find_col(header, ["Day"]),
        "title": find_col(header, ["Product Title"]),
        "cost": find_col(header, ["Cost"]),
        "impr": find_col(header, ["Impr."]),
        "clicks": find_col(header, ["Clicks"]),
        "conv": find_col(header, ["Conversions"]),
        "conv_value": find_col(header, ["Conv. value"]),
    }
    product_ads_day = defaultdict(lambda: {"spend": 0.0, "impressions": 0.0, "clicks": 0.0, "conversions": 0.0, "conv_value": 0.0, "title": ""})
    for r in data:
        day = r[idx["day"]]
        title = r[idx["title"]].strip()
        slug = slugify(title)
        p = product_ads_day[(day, slug)]
        p["spend"] += to_f(r[idx["cost"]]); p["impressions"] += to_f(r[idx["impr"]])
        p["clicks"] += to_f(r[idx["clicks"]]); p["conversions"] += to_f(r[idx["conv"]])
        p["conv_value"] += to_f(r[idx["conv_value"]]); p["title"] = title
    return product_ads_day  # keyed by (day, slug)


def load_shopify_sales(path):
    header, data = read_shopify_csv(path)
    idx = {
        "day": find_col(header, ["Day"]),
        "order": find_col(header, ["Order name"]),
        "title": find_col(header, ["Product title at time of sale", "Product title"]),
        "gross": find_col(header, ["Gross sales"]),
        "net": find_col(header, ["Net sales"]),
    }
    daily_shop = defaultdict(lambda: {"gross": 0.0, "net": 0.0, "orders": set()})
    product_shop_day = defaultdict(lambda: {"gross": 0.0, "net": 0.0, "orders": set(), "title": ""})
    for r in data:
        day = r[idx["day"]]
        order = r[idx["order"]]
        title = r[idx["title"]].strip()
        gross = to_f(r[idx["gross"]]); net = to_f(r[idx["net"]])
        ds = daily_shop[day]
        ds["gross"] += gross; ds["net"] += net
        if gross > 0:
            ds["orders"].add(order)
        if title:
            slug = slugify(title)
            p = product_shop_day[(day, slug)]
            p["gross"] += gross; p["net"] += net; p["title"] = title
            if gross > 0:
                p["orders"].add(order)
    return daily_shop, product_shop_day


def load_shopify_sessions(path):
    header, data = read_shopify_csv(path)
    idx = {
        "day": find_col(header, ["Day"]),
        "type": find_col(header, ["Landing page type"]),
        "path": find_col(header, ["Landing page path"]),
        "sessions": find_col(header, ["Sessions"]),
        "cart": find_col(header, ["Sessions with cart additions"]),
        "checkout": find_col(header, ["Sessions that reached checkout"]),
    }
    daily_sessions = defaultdict(lambda: {"sessions": 0.0, "cart": 0.0, "checkout": 0.0})
    landing_day = defaultdict(lambda: {"sessions": 0.0, "cart": 0.0, "checkout": 0.0, "type": ""})
    for r in data:
        day = r[idx["day"]]
        ltype = r[idx["type"]] or "Unknown"
        path_ = r[idx["path"]]
        sessions = to_f(r[idx["sessions"]]); cart = to_f(r[idx["cart"]]); checkout = to_f(r[idx["checkout"]])
        ds = daily_sessions[day]
        ds["sessions"] += sessions; ds["cart"] += cart; ds["checkout"] += checkout
        lp = landing_day[(day, path_)]
        lp["sessions"] += sessions; lp["cart"] += cart; lp["checkout"] += checkout; lp["type"] = ltype
    return daily_sessions, landing_day


# ---------------------------------------------------------------------------
# Combine everything into the RAW data structure the dashboard expects
# ---------------------------------------------------------------------------

def build_raw(daily_ads, campaign_new, city_new, keyword_new, product_ads_day, daily_shop, product_shop_day,
               daily_sessions, landing_day):
    # Landing-page ROAS is matched to that SAME day's product-level ad spend (not a
    # full-period total repeated on every day-row) -- each landing_page row is one
    # (day, path) combination, and renderLandingPanel() in the dashboard sums ad_cost
    # across every day-row in the selected range. If ad_cost were a period-wide total
    # duplicated on every day, summing across N days of sessions would over-count spend
    # by roughly Nx. Matching per-day keeps sum-then-divide correct at any date range.
    landing_out = []
    for (day, path), v in landing_day.items():
        if v["type"] != "Product":
            continue
        slug = path.replace("/products/", "").strip("/")
        ad = product_ads_day.get((day, slug)) or product_ads_day.get((day, strip_suffix(slug)))
        landing_out.append({
            "day": day, "path": path,
            "sessions": int(v["sessions"]), "cart_sessions": int(v["cart"]), "checkout_sessions": int(v["checkout"]),
            "ad_cost": round(ad["spend"], 2) if ad else None,
            "ad_conv_value": round(ad["conv_value"], 2) if ad else None,
        })

    product_out = []
    all_product_keys = set(product_ads_day.keys()) | set(product_shop_day.keys())
    for key in all_product_keys:
        day, slug = key
        ad = product_ads_day.get(key)
        sh = product_shop_day.get(key)
        ad_cost = ad["spend"] if ad else 0.0
        gross = sh["gross"] if sh else 0.0
        if ad_cost <= 0 and gross <= 0:
            continue
        title = sh["title"] if sh else (ad["title"] if ad else None)
        orders = len(sh["orders"]) if sh else 0
        product_out.append({
            "day": day, "product": title,
            "ad_cost": round(ad["spend"], 2) if ad else 0.0,
            "ad_impressions": int(ad["impressions"]) if ad else 0,
            "ad_clicks": int(ad["clicks"]) if ad else 0,
            "ad_conversions": round(ad["conversions"], 2) if ad else 0.0,
            "ad_conv_value": round(ad["conv_value"], 2) if ad else 0.0,
            "gross": round(sh["gross"], 2) if sh else 0.0,
            "net": round(sh["net"], 2) if sh else 0.0,
            "orders": orders,
        })

    all_days = sorted(set(list(daily_ads.keys()) + list(daily_shop.keys()) + list(daily_sessions.keys())))
    daily_out = []
    for d in all_days:
        a = daily_ads.get(d, {"spend": 0, "impressions": 0, "clicks": 0, "conversions": 0, "conv_value": 0})
        s = daily_shop.get(d, {"gross": 0, "net": 0, "orders": set()})
        ses = daily_sessions.get(d, {"sessions": 0, "cart": 0, "checkout": 0})
        daily_out.append({
            "date": d, "spend": round(a["spend"], 2), "impressions": int(a["impressions"]), "clicks": int(a["clicks"]),
            "g_conversions": round(a["conversions"], 2), "g_conv_value": round(a["conv_value"], 2),
            "gross_revenue": round(s["gross"], 2), "net_revenue": round(s["net"], 2), "orders": len(s["orders"]),
            "sessions": int(ses["sessions"]), "cart_sessions": int(ses["cart"]), "checkout_sessions": int(ses["checkout"]),
        })

    return {
        "daily": daily_out, "campaign": campaign_new, "city": city_new, "keyword": keyword_new,
        "product": product_out, "landing_page": landing_out,
    }


TOP_N_CITIES = 150

def prune_cities(city_rows):
    """Cap the persisted city section to the top N cities by all-time cumulative spend.
    Recomputed fresh from the full merged history each run (not a single day's spend),
    so the kept set is stable and reflects real cumulative importance rather than
    whichever city happened to spend the most yesterday. Keeps the growing daily file
    (and the GitHub-hosted index.html, which embeds the same data) from ballooning
    indefinitely -- the long tail of ~1700 near-zero-spend cities isn't shown in the
    dashboard anyway (the panel only ever displays the top 20 by spend)."""
    totals = defaultdict(float)
    for r in city_rows:
        totals[r["city"]] += r["spend"]
    top = set(c for c, _ in sorted(totals.items(), key=lambda x: -x[1])[:TOP_N_CITIES])
    return [r for r in city_rows if r["city"] in top]


def merge_section(existing, new, day_key="day"):
    """Drop any existing rows whose day appears in the new rows, then append new rows.
    Idempotent: re-running the same day's merge twice does not double-count."""
    new_days = set(r[day_key] for r in new)
    kept = [r for r in existing if r[day_key] not in new_days]
    return kept + new


def render_html(data, client_name, template_path, chartjs_path):
    data_json = json.dumps(data, separators=(",", ":"))
    chartjs = Path(chartjs_path).read_text()
    template = Path(template_path).read_text()
    return (template.replace("__CHARTJS__", chartjs)
                     .replace("__DATA_JSON__", data_json)
                     .replace("__CLIENT_NAME__", client_name or "Client"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["seed", "merge"], required=True)
    ap.add_argument("--existing-data", default=None, help="Existing tortoys_data.json (required for --mode merge)")
    ap.add_argument("--ads-city", required=True, help="Kartscale Report R1 CSV (City/Region breakdown)")
    ap.add_argument("--ads-keyword", required=True, help="Search campaign keyword report R2 CSV")
    ap.add_argument("--ads-product", required=True, help="Kartscale R3 CSV (Product-level)")
    ap.add_argument("--shopify-sales", required=True, help="Shopify sales export CSV")
    ap.add_argument("--shopify-sessions", required=True, help="Shopify sessions-by-landing-page export CSV")
    ap.add_argument("--template", required=True)
    ap.add_argument("--chartjs", required=True)
    ap.add_argument("--out-data", required=True)
    ap.add_argument("--out-html", required=True)
    ap.add_argument("--client-name", default="Tortoys")
    args = ap.parse_args()

    daily_ads, campaign_new, city_new = load_ads_city(args.ads_city)
    keyword_new = load_ads_keyword(args.ads_keyword)
    product_ads_day = load_ads_product(args.ads_product)
    daily_shop, product_shop_day = load_shopify_sales(args.shopify_sales)
    daily_sessions, landing_day = load_shopify_sessions(args.shopify_sessions)

    new_raw = build_raw(daily_ads, campaign_new, city_new, keyword_new, product_ads_day,
                         daily_shop, product_shop_day, daily_sessions, landing_day)

    if args.mode == "seed":
        final = new_raw
    else:
        if not args.existing_data:
            print("ERROR: --existing-data is required for --mode merge", file=sys.stderr)
            sys.exit(1)
        existing = json.loads(Path(args.existing_data).read_text())
        final = {
            "daily": merge_section(existing["daily"], new_raw["daily"], "date"),
            "campaign": merge_section(existing["campaign"], new_raw["campaign"], "day"),
            "city": merge_section(existing["city"], new_raw["city"], "day"),
            "keyword": merge_section(existing["keyword"], new_raw["keyword"], "day"),
            "product": merge_section(existing["product"], new_raw["product"], "day"),
            "landing_page": merge_section(existing["landing_page"], new_raw["landing_page"], "day"),
        }
        final["daily"].sort(key=lambda r: r["date"])

    final["city"] = prune_cities(final["city"])

    Path(args.out_data).write_text(json.dumps(final, separators=(",", ":")))
    html = render_html(final, args.client_name, args.template, args.chartjs)
    Path(args.out_html).write_text(html)

    total_spend = sum(d["spend"] for d in final["daily"])
    total_gross = sum(d["gross_revenue"] for d in final["daily"])
    total_orders = sum(d["orders"] for d in final["daily"])
    print(f"Mode: {args.mode}")
    print(f"Wrote {args.out_data} and {args.out_html}")
    print(f"Days covered: {len(final['daily'])} ({final['daily'][0]['date']} to {final['daily'][-1]['date']})")
    print(f"Total spend: {total_spend:,.2f}  Total gross revenue: {total_gross:,.2f}  Total orders: {total_orders}")
    print(f"Blended ROAS (Gross): {(total_gross/total_spend if total_spend else 0):.2f}x")
    print(f"Campaign rows: {len(final['campaign'])}, City rows: {len(final['city'])}, "
          f"Keyword rows: {len(final['keyword'])}, Product rows: {len(final['product'])}, "
          f"Landing page rows: {len(final['landing_page'])}")


if __name__ == "__main__":
    main()
