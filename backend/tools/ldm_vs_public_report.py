"""
Detailed report: private LDM API vs NOAA's public MADIS HFMETAR files ("ASOS"), from one day of live logs.

Run the backend with, in backend/.env (both are written by the running server):

    ASOS_SOURCE=ldm
    ASOS_SHADOW_COMPARE=true
    ASOS_ARRIVAL_LOG_PATH=logs/asos_arrivals.jsonl      # one line per observation each source delivered
    ASOS_SHADOW_LOG_PATH=logs/asos_shadow.jsonl         # one line per side-by-side comparison

then, from the backend folder:

    python tools/ldm_vs_public_report.py                 # today (Miami date)
    python tools/ldm_vs_public_report.py --date 2026-10-29
    python tools/ldm_vs_public_report.py --all           # everything in the logs

Writes logs/report_<date>.html (open it in a browser) and logs/report_<date>.json, and prints a short summary.

What is measured (all times UTC unless stated):
  * "age at receipt"   = when this backend first saw an observation  -  the observation's own time.
  * "snapshot lag"     = the snapshot's own stamp (LDM modified_at / NOAA Last-Modified)  -  observation time:
                         how long the SOURCE took. It does not depend on our polling.
  * "pickup delay"     = when we received it  -  the snapshot stamp: how long WE took (polling cadence + download).
                         Negative values mean the clocks of the two machines disagree.
  * "lead"             = public receipt time  -  LDM receipt time, per observation. Positive = LDM was first.
Arrival times have about one polling interval (~1 minute) of resolution. Observations delivered in the first
batch after a server start are excluded from latency statistics (they may have been available long before).
Only standard-library modules are used.
"""
import argparse
import html
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MIAMI = ZoneInfo("America/New_York")
BACKEND = Path(__file__).resolve().parent.parent
FRESH_SECONDS = 15 * 60          # same thresholds the app uses
MAX_SECONDS = 25 * 60
QUARTER_WINDOW_SECONDS = 90      # "just after a :00/:15/:30/:45 boundary"
TEMP_TOLERANCE_F = 0.05
SOURCES = ("ldm", "public")
LABEL = {"ldm": "LDM (private API)", "public": "Public NOAA (ASOS/MADIS)"}
AGE_BUCKETS = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, None)]    # minutes


# ── reading ──────────────────────────────────────────────────────────────────

def load_jsonl(path):
    rows = []
    if not path or not Path(path).exists():
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue                    # a half-written last line is not an error
    return rows


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def stamp_of(source, version):
    """The snapshot's own timestamp from the logged version string, or None."""
    if not version:
        return None
    try:
        if source == "ldm":                                  # "<size>|<modified_at iso>"
            return parse_time(str(version).split("|", 1)[1])
        dt = parsedate_to_datetime(str(version))             # public: HTTP Last-Modified
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (IndexError, TypeError, ValueError):
        return None


# ── statistics ───────────────────────────────────────────────────────────────

def percentile(values, q):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q / 100.0
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def describe(values):
    values = [x for x in values if x is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": min(values), "p10": percentile(values, 10), "median": statistics.median(values),
            "mean": statistics.fmean(values), "p90": percentile(values, 90), "max": max(values)}


def histogram(ages_seconds):
    counts = []
    for lo, hi in AGE_BUCKETS:
        n = sum(1 for a in ages_seconds if a >= lo * 60 and (hi is None or a < hi * 60))
        counts.append({"label": f"{lo}–{hi} min" if hi else f"{lo}+ min", "count": n})
    return counts


def quarter_clustering(stamps):
    """How many snapshot stamps fall just after a quarter-hour boundary (random would be ~10%)."""
    if not stamps:
        return None
    bins = {"0–90 s": 0, "90 s–5 min": 0, "5–10 min": 0, "10–15 min": 0}
    for s in stamps:
        sec = (s.minute % 15) * 60 + s.second
        key = ("0–90 s" if sec < QUARTER_WINDOW_SECONDS else "90 s–5 min" if sec < 300
               else "5–10 min" if sec < 600 else "10–15 min")
        bins[key] += 1
    n = len(stamps)
    return {"n": n, "bins": bins, "fraction_within_window": bins["0–90 s"] / n,
            "expected_if_random": QUARTER_WINDOW_SECONDS / 900.0}


# ── building the report ──────────────────────────────────────────────────────

def first_arrivals(rows):
    """{source: {obs_time: row}} keeping, per observation, the earliest non-startup, non-correction arrival."""
    out = {s: {} for s in SOURCES}
    for r in rows:
        src, obs, recv = r.get("source"), parse_time(r.get("observation_time_utc")), parse_time(r.get("backend_received_at_utc"))
        if src not in out or obs is None or recv is None or r.get("corrected"):
            continue
        key = (bool(r.get("startup")), recv)                 # real arrivals sort before startup ones, then by time
        cur = out[src].get(obs)
        if cur is None or key < cur["_key"]:
            out[src][obs] = {**r, "_key": key, "obs": obs, "recv": recv}
    return out


def freshness_simulation(rows, start, end):
    """Minute by minute: how old was the newest observation this source had delivered?"""
    result = {}
    for src in SOURCES:
        arrivals = sorted(
            (parse_time(r["backend_received_at_utc"]), parse_time(r["observation_time_utc"]))
            for r in rows if r.get("source") == src and parse_time(r.get("backend_received_at_utc"))
            and parse_time(r.get("observation_time_utc")))
        ages, i, newest = [], 0, None
        t = start
        while t <= end:
            while i < len(arrivals) and arrivals[i][0] <= t:
                newest = arrivals[i][1] if newest is None or arrivals[i][1] > newest else newest
                i += 1
            if newest is not None:
                ages.append((t, (t - newest).total_seconds()))
            t += timedelta(minutes=1)
        seconds = [a for _, a in ages]
        n = len(seconds)
        result[src] = {
            "minutes": n,
            "healthy_pct": 100.0 * sum(1 for a in seconds if a <= FRESH_SECONDS) / n if n else None,
            "delayed_pct": 100.0 * sum(1 for a in seconds if FRESH_SECONDS < a <= MAX_SECONDS) / n if n else None,
            "unavailable_pct": 100.0 * sum(1 for a in seconds if a > MAX_SECONDS) / n if n else None,
            "age": describe(seconds),
            "worst_at": max(ages, key=lambda x: x[1])[0].isoformat() if ages else None,
        }
    return result


def error_category(text):
    m = re.search(r"\[(\w+)\]", str(text))
    if m:
        return m.group(1)
    return "configuration" if "configuration" in str(text) else "other"


def analyse_shadow(rows):
    if not rows:
        return None
    diffs = [r["newest_time_diff_seconds"] for r in rows if r.get("newest_time_diff_seconds") is not None]
    cats = {"primary": {}, "shadow": {}}
    polls = {"primary": 0, "shadow": 0}
    examples = []
    for r in rows:
        for side in ("primary", "shadow"):
            errs = r.get(f"{side}_errors") or []
            if errs:
                polls[side] += 1
            for e in errs:
                c = error_category(e)
                cats[side][c] = cats[side].get(c, 0) + 1
                if len(examples) < 6 and str(e)[:160] not in examples:
                    examples.append(str(e)[:160])
    return {
        "comparisons": len(rows),
        "primary_source": rows[-1].get("primary_source"), "shadow_source": rows[-1].get("shadow_source"),
        "leader": {k: sum(1 for r in rows if r.get("leader") == k) for k in ("primary", "shadow", "tie")},
        "newest_time_diff_seconds": describe(diffs),
        "primary_age_seconds": describe([r.get("primary_age_seconds") for r in rows]),
        "shadow_age_seconds": describe([r.get("shadow_age_seconds") for r in rows]),
        "status_counts": {side: {s: sum(1 for r in rows if r.get(f"{side}_status") == s)
                                 for s in ("healthy", "delayed", "unavailable")} for side in ("primary", "shadow")},
        "temp_mismatches": sum(r.get("temp_mismatches") or 0 for r in rows),
        "max_temp_diff_f": max([r["max_temp_diff_f"] for r in rows if r.get("max_temp_diff_f") is not None], default=None),
        "error_polls": polls, "error_categories": cats, "error_examples": examples,
    }


def build_report(arrivals, shadow, date=None, tie_seconds=60):
    """All numbers behind the report. `date` = Miami calendar date (YYYY-MM-DD) of the observations, or None."""
    if date:
        arrivals = [r for r in arrivals if (parse_time(r.get("observation_time_utc")) or datetime.min.replace(tzinfo=timezone.utc))
                    .astimezone(MIAMI).date().isoformat() == date]
        shadow = [r for r in shadow if (parse_time(r.get("at")) or datetime.min.replace(tzinfo=timezone.utc))
                  .astimezone(MIAMI).date().isoformat() == date]
    first = first_arrivals(arrivals)
    real = {s: {o: r for o, r in first[s].items() if not r.get("startup")} for s in SOURCES}

    per_source = {}
    for s in SOURCES:
        rows = list(real[s].values())
        stamps_all = {}
        for r in arrivals:
            if r.get("source") == s:
                st = stamp_of(s, r.get("snapshot_version"))
                if st is not None:
                    stamps_all[st] = r.get("snapshot_filename")
        stamps = sorted(stamps_all)
        gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
        lag, pickup, ages = [], [], []
        for r in rows:
            ages.append(r["recv"] and (r["recv"] - r["obs"]).total_seconds())
            st = stamp_of(s, r.get("snapshot_version"))
            if st is not None:
                lag.append((st - r["obs"]).total_seconds())
                pickup.append((r["recv"] - st).total_seconds())
        per_source[s] = {
            "observations": len(first[s]), "observations_after_startup": len(rows),
            "corrections": sum(1 for r in arrivals if r.get("source") == s and r.get("corrected")),
            "age_at_receipt_seconds": describe(ages), "age_histogram": histogram(ages),
            "snapshot_lag_seconds": describe(lag), "pickup_delay_seconds": describe(pickup),
            "negative_pickups": sum(1 for p in pickup if p < 0),
            "download_ms": describe([r.get("download_ms") for r in rows]),
            "parse_ms": describe([r.get("parse_ms") for r in rows]),
            "snapshot_stamps": len(stamps), "snapshot_gap_seconds": describe(gaps),
            "quarter_clustering": quarter_clustering(stamps),
        }

    both = sorted(set(real["ldm"]) & set(real["public"]))
    leads = [(real["public"][o]["recv"] - real["ldm"][o]["recv"]).total_seconds() for o in both]
    lead = {
        "n": len(leads), "tie_seconds": tie_seconds, "seconds": describe(leads),
        "ldm_first": sum(1 for x in leads if x > tie_seconds), "public_first": sum(1 for x in leads if x < -tie_seconds),
        "tie": sum(1 for x in leads if abs(x) <= tie_seconds),
    }

    union = sorted(set(first["ldm"]) | set(first["public"]))
    temp_pairs = [(o, first["ldm"][o].get("temp_f"), first["public"][o].get("temp_f"))
                  for o in union if o in first["ldm"] and o in first["public"]]
    diffs = [(o, a, b, abs(a - b)) for o, a, b in temp_pairs if a is not None and b is not None]
    temperature = {
        "compared": len(diffs), "max_abs_diff_f": max((d[3] for d in diffs), default=None),
        "mismatches": [{"observation": o.isoformat(), "ldm_f": a, "public_f": b, "diff_f": d}
                       for o, a, b, d in diffs if d > TEMP_TOLERANCE_F],
    }

    times = [parse_time(r.get("backend_received_at_utc")) for r in arrivals] + [parse_time(r.get("at")) for r in shadow]
    times = [t for t in times if t]
    freshness = None
    starts = [min((r["recv"] for r in real[s].values()), default=None) for s in SOURCES]
    if times and all(starts):
        start, end = max(starts), max(times)
        if end > start:
            freshness = freshness_simulation(arrivals, start.replace(second=0, microsecond=0), end)
            freshness["window"] = {"start": start.isoformat(), "end": end.isoformat(),
                                   "minutes": int((end - start).total_seconds() // 60)}

    table = []
    for o in union:
        row = {"observation": o.isoformat()}
        for s in SOURCES:
            r = first[s].get(o)
            if r:
                st = stamp_of(s, r.get("snapshot_version"))
                row[s] = {"received": r["recv"].isoformat(), "age_seconds": (r["recv"] - o).total_seconds(),
                          "snapshot_lag_seconds": (st - o).total_seconds() if st else None,
                          "temp_f": r.get("temp_f"), "startup": bool(r.get("startup"))}
        if o in real["ldm"] and o in real["public"]:
            row["lead_seconds"] = (real["public"][o]["recv"] - real["ldm"][o]["recv"]).total_seconds()
        table.append(row)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "date": date,
        "data": {"arrival_rows": len(arrivals), "shadow_rows": len(shadow), "observations_seen": len(union),
                 "first_row": min(times).isoformat() if times else None, "last_row": max(times).isoformat() if times else None},
        "sources": per_source, "lead": lead, "temperature": temperature, "freshness": freshness,
        "shadow": analyse_shadow(shadow),
        "missing": {s: [o.isoformat() for o in union if o not in first[s]] for s in SOURCES},
        "observations": table,
    }
    report["findings"] = findings(report)
    return report


def minutes(seconds, digits=1):
    return None if seconds is None else round(seconds / 60.0, digits)


def findings(rep):
    """Plain-language conclusions, each backed by the numbers in the report."""
    out = []
    ps, lead = rep["sources"], rep["lead"]
    if rep["data"]["arrival_rows"] == 0:
        return [("warn", "No arrival data was found for this day. Check ASOS_ARRIVAL_LOG_PATH and that the backend ran.")]
    if lead["n"] < 20:
        out.append(("warn", f"Only {lead['n']} observations were seen by both sources after start-up, so treat the "
                            "figures below as indicative, not conclusive. Longer runs give firmer numbers."))
    if lead["n"]:
        med = lead["seconds"]["median"]
        if med > lead["tie_seconds"]:
            out.append(("good", f"LDM delivered observations first in {lead['ldm_first']} of {lead['n']} cases; the median "
                                f"lead was {med:.0f} s ({minutes(med)} min) ahead of the public files."))
        elif med < -lead["tie_seconds"]:
            out.append(("bad", f"The public NOAA files were first in {lead['public_first']} of {lead['n']} cases; LDM was "
                               f"behind by a median of {-med:.0f} s ({minutes(-med)} min)."))
        else:
            out.append(("info", f"No consistent winner: LDM first {lead['ldm_first']}×, public first {lead['public_first']}×, "
                                f"within ±{lead['tie_seconds']} s {lead['tie']}× (median lead {med:.0f} s)."))
    a_l, a_p = ps["ldm"]["age_at_receipt_seconds"], ps["public"]["age_at_receipt_seconds"]
    if a_l["n"] and a_p["n"]:
        out.append(("info", f"Median age of an observation when first received: LDM {minutes(a_l['median'])} min, "
                            f"public {minutes(a_p['median'])} min (90th percentile {minutes(a_l['p90'])} vs {minutes(a_p['p90'])} min)."))
    l_l, l_p = ps["ldm"]["snapshot_lag_seconds"], ps["public"]["snapshot_lag_seconds"]
    if l_l["n"] and l_p["n"]:
        d = l_l["median"] - l_p["median"]
        if d > 120:
            out.append(("bad", f"The delay is in the SOURCE, not in our polling: LDM snapshots are stamped a median "
                               f"{minutes(l_l['median'])} min after the observation, versus {minutes(l_p['median'])} min for NOAA's "
                               f"public files ({minutes(d)} min later). Look at how/when the VPS saves snapshots."))
        elif d < -120:
            out.append(("good", f"LDM snapshots are stamped {minutes(-d)} min sooner after the observation than NOAA's public files."))
    qc = ps["ldm"]["quarter_clustering"]
    if qc and qc["n"] >= 8 and qc["fraction_within_window"] >= max(0.4, 2.5 * qc["expected_if_random"]):
        out.append(("warn", f"{qc['fraction_within_window'] * 100:.0f}% of LDM snapshot stamps fall within 90 s after a "
                            f"quarter-hour (:00/:15/:30/:45); random timing would give ~{qc['expected_if_random'] * 100:.0f}%. "
                            "That pattern suggests snapshots are saved by a timer, which adds up to 15 minutes of delay."))
    for s in SOURCES:
        p = ps[s]["pickup_delay_seconds"]
        if p["n"] and p["median"] > 90:
            out.append(("warn", f"{LABEL[s]}: this backend took a median {p['median']:.0f} s to pick a snapshot up after it was "
                                "stamped (more than one polling interval)."))
        if ps[s]["negative_pickups"]:
            out.append(("warn", f"{LABEL[s]}: {ps[s]['negative_pickups']} snapshot(s) were received BEFORE their own stamp, so "
                                "the clocks of this PC and the server disagree; pickup figures are approximate."))
    fr = rep["freshness"]
    if fr:
        fl, fp = fr["ldm"], fr["public"]
        out.append(("info", f"What the live app would have seen over {fr['window']['minutes']} minutes: average data age "
                            f"{minutes(fl['age']['mean'])} min on LDM vs {minutes(fp['age']['mean'])} min on public; "
                            f"healthy {fl['healthy_pct']:.0f}% vs {fp['healthy_pct']:.0f}% of the time; "
                            f"unavailable {fl['unavailable_pct']:.0f}% vs {fp['unavailable_pct']:.0f}%."))
    t = rep["temperature"]
    if t["compared"]:
        if not t["mismatches"]:
            out.append(("good", f"Temperatures agree on all {t['compared']} observations both sources delivered."))
        else:
            out.append(("bad", f"{len(t['mismatches'])} of {t['compared']} shared observations differ by more than "
                               f"{TEMP_TOLERANCE_F} °F (largest {t['max_abs_diff_f']:.2f} °F). See the table below."))
    for s in SOURCES:
        miss = rep["missing"][s]
        if miss and rep["observations"]:
            out.append(("warn", f"{LABEL[s]} never delivered {len(miss)} observation(s) that the other source did."))
    sh = rep["shadow"]
    if sh and (sh["error_polls"]["primary"] or sh["error_polls"]["shadow"]):
        out.append(("warn", f"Errors were recorded: {sh['error_polls']['primary']} poll(s) on the primary side and "
                            f"{sh['error_polls']['shadow']} on the comparison side (see the Errors section)."))
    return out


# ── HTML ─────────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#fbfaf7;--card:#fff;--ink:#1c1b19;--mute:#6b675f;--line:#e3dfd6;--good:#0b6b4a;--bad:#9c1d1d;--warn:#8a5a00;--bar:#13936a;--bar2:#b8923c}
@media (prefers-color-scheme:dark){:root{--bg:#161512;--card:#1f1e1a;--ink:#efece4;--mute:#a09b90;--line:#35322b;--good:#5fd0a3;--bad:#ff8f8f;--warn:#e6b455;--bar:#3ec59a;--bar2:#d9b45b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1120px;margin:0 auto;padding:28px 18px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 10px}
.sub{color:var(--mute);margin:0 0 22px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.k{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.v{font-size:24px;font-weight:700}.f{margin:6px 0;padding-left:12px;border-left:4px solid var(--line)}
.f.good{border-color:var(--good)}.f.bad{border-color:var(--bad)}.f.warn{border-color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:10px;border-radius:5px;background:var(--bar);min-width:2px}.bar.b2{background:var(--bar2)}.pos{color:var(--good);font-weight:600}.neg{color:var(--bad);font-weight:600}
.scroll{overflow-x:auto}.note{color:var(--mute);font-size:13px}code{background:var(--line);padding:1px 5px;border-radius:4px}
"""


def e(x):
    return html.escape(str(x))


def fmt_time(iso, miami=False, with_date=False):
    dt = parse_time(iso)
    if not dt:
        return "—"
    dt = dt.astimezone(MIAMI if miami else timezone.utc)
    return dt.strftime("%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def fmt_min(seconds):
    return "—" if seconds is None else f"{seconds / 60:.1f} min"


def fmt_s(seconds):
    return "—" if seconds is None else f"{seconds:+.0f} s"


def stats_row(label, d, as_minutes=True):
    if not d.get("n"):
        return f"<tr><td>{e(label)}</td><td class='n'>0</td>" + "<td class='n'>—</td>" * 6 + "</tr>"
    f = fmt_min if as_minutes else (lambda x: f"{x:.0f}")
    cells = "".join(f"<td class='n'>{f(d[k])}</td>" for k in ("min", "p10", "median", "mean", "p90", "max"))
    return f"<tr><td>{e(label)}</td><td class='n'>{d['n']}</td>{cells}</tr>"


STATS_HEAD = "<tr><th>Measure</th><th class='n'>n</th><th class='n'>min</th><th class='n'>p10</th><th class='n'>median</th><th class='n'>mean</th><th class='n'>p90</th><th class='n'>max</th></tr>"


def render_html(rep):
    ps, lead, fr, sh, t = rep["sources"], rep["lead"], rep["freshness"], rep["shadow"], rep["temperature"]
    title = f"LDM vs public NOAA — {rep['date'] or 'all logged data'}"
    p = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>",
         f"<h1>{e(title)}</h1><p class='sub'>Generated {fmt_time(rep['generated_at'], with_date=True)} UTC · station KMIA · "
         f"data from {fmt_time(rep['data']['first_row'], with_date=True)} to {fmt_time(rep['data']['last_row'], with_date=True)} UTC · "
         f"{rep['data']['observations_seen']} observations, {rep['data']['arrival_rows']} arrival rows, {rep['data']['shadow_rows']} comparisons</p>"]

    p.append("<h2>Summary</h2><div class='card'>")
    for level, text in rep["findings"]:
        p.append(f"<div class='f {level}'>{e(text)}</div>")
    p.append("</div>")

    med = lead["seconds"].get("median")
    p.append("<div class='grid'>")
    tiles = [("Observations compared", lead["n"]),
             ("LDM first", f"{lead['ldm_first']} ({100 * lead['ldm_first'] / lead['n']:.0f}%)" if lead["n"] else "—"),
             ("Public first", f"{lead['public_first']} ({100 * lead['public_first'] / lead['n']:.0f}%)" if lead["n"] else "—"),
             ("Median lead of LDM", "—" if med is None else f"{med:+.0f} s")]
    for k, v in tiles:
        p.append(f"<div class='card'><div class='k'>{e(k)}</div><div class='v'>{e(v)}</div></div>")
    p.append("</div>")

    p.append("<h2>Where the time goes</h2><div class='card scroll'><table><tr><th>Stage</th>"
             + "".join(f"<th class='n'>{e(LABEL[s])}</th>" for s in SOURCES) + "</tr>")
    for label, key in (("1 · Observation → snapshot stamp (source side)", "snapshot_lag_seconds"),
                       ("2 · Snapshot stamp → received here (polling + download)", "pickup_delay_seconds"),
                       ("Total · observation → received here", "age_at_receipt_seconds")):
        p.append(f"<tr><td>{e(label)}</td>" + "".join(
            f"<td class='n'>{fmt_min(ps[s][key].get('median'))}</td>" for s in SOURCES) + "</tr>")
    p.append("</table><p class='note'>Medians. Stage 1 is out of our hands; stage 2 is this backend's polling and download. "
             "Stamps are the server's own clocks (LDM <code>modified_at</code>, NOAA <code>Last-Modified</code>).</p></div>")

    p.append("<h2>Age of each observation when first received</h2><div class='card scroll'><table>" + STATS_HEAD)
    for s in SOURCES:
        p.append(stats_row(LABEL[s], ps[s]["age_at_receipt_seconds"]))
    p.append("</table></div><div class='grid'>")
    for s in SOURCES:
        total = sum(b["count"] for b in ps[s]["age_histogram"]) or 1
        p.append(f"<div class='card'><div class='k'>{e(LABEL[s])}</div><table>")
        for b in ps[s]["age_histogram"]:
            w = 100 * b["count"] / total
            p.append(f"<tr><td>{e(b['label'])}</td><td style='width:55%'><div class='bar {'b2' if s == 'public' else ''}' style='width:{w:.0f}%'></div></td>"
                     f"<td class='n'>{b['count']}</td></tr>")
        p.append("</table></div>")
    p.append("</div>")

    p.append("<h2>Head to head (per observation)</h2><div class='card scroll'><table>" + STATS_HEAD)
    p.append(stats_row("Lead of LDM over public (positive = LDM first)", lead["seconds"], as_minutes=False))
    p.append(f"</table><p class='note'>Lead in seconds. Ties are within ±{lead['tie_seconds']} s (about one polling interval).</p></div>")

    if fr:
        p.append(f"<h2>What the live app would have seen</h2><div class='card scroll'><table><tr><th>Source</th><th class='n'>Healthy (≤15 min)</th>"
                 "<th class='n'>Delayed (15–25)</th><th class='n'>Unavailable (&gt;25)</th><th class='n'>Mean age</th><th class='n'>Worst age</th></tr>")
        for s in SOURCES:
            x = fr[s]
            p.append(f"<tr><td>{e(LABEL[s])}</td><td class='n'>{x['healthy_pct']:.0f}%</td><td class='n'>{x['delayed_pct']:.0f}%</td>"
                     f"<td class='n'>{x['unavailable_pct']:.0f}%</td><td class='n'>{fmt_min(x['age']['mean'])}</td><td class='n'>{fmt_min(x['age']['max'])}</td></tr>")
        p.append(f"</table><p class='note'>Minute by minute over {fr['window']['minutes']} minutes "
                 f"({fmt_time(fr['window']['start'])}–{fmt_time(fr['window']['end'])} UTC): age of the newest observation each source had delivered, "
                 "using the same 15/25-minute thresholds as the app.</p></div>")

    p.append("<h2>Snapshot update pattern</h2><div class='card scroll'><table><tr><th>Source</th><th class='n'>Distinct snapshots</th>"
             "<th class='n'>Median gap</th><th class='n'>Stamped within 90 s after :00/:15/:30/:45</th><th class='n'>Random would be</th></tr>")
    for s in SOURCES:
        q, g = ps[s]["quarter_clustering"], ps[s]["snapshot_gap_seconds"]
        p.append(f"<tr><td>{e(LABEL[s])}</td><td class='n'>{ps[s]['snapshot_stamps']}</td><td class='n'>{fmt_min(g.get('median'))}</td>"
                 f"<td class='n'>{'—' if not q else f'{q['fraction_within_window'] * 100:.0f}% ({q['bins']['0–90 s']}/{q['n']})'}</td>"
                 f"<td class='n'>{'—' if not q else f'{q['expected_if_random'] * 100:.0f}%'}</td></tr>")
    p.append("</table><p class='note'>A high share right after quarter-hours means snapshots are produced by a timer rather than as data arrives.</p></div>")

    p.append("<h2>Temperature agreement</h2><div class='card'>")
    if not t["compared"]:
        p.append("<p class='note'>No observation was delivered by both sources.</p>")
    elif not t["mismatches"]:
        p.append(f"<p>All {t['compared']} shared observations match (largest difference {t['max_abs_diff_f']:.2f} °F).</p>")
    else:
        p.append(f"<p>{len(t['mismatches'])} of {t['compared']} differ by more than {TEMP_TOLERANCE_F} °F.</p><table><tr><th>Observation (UTC)</th>"
                 "<th class='n'>LDM °F</th><th class='n'>Public °F</th><th class='n'>Difference</th></tr>")
        for m in t["mismatches"][:50]:
            p.append(f"<tr><td>{fmt_time(m['observation'], with_date=True)}</td><td class='n'>{m['ldm_f']}</td><td class='n'>{m['public_f']}</td><td class='n'>{m['diff_f']:.2f}</td></tr>")
        p.append("</table>")
    p.append("</div>")

    p.append("<h2>Errors and side-by-side polls</h2><div class='card'>")
    if not sh:
        p.append("<p class='note'>No comparison log was found (ASOS_SHADOW_LOG_PATH).</p>")
    else:
        sd = sh["newest_time_diff_seconds"]
        p.append(f"<p>{sh['comparisons']} side-by-side polls (primary: <b>{e(sh['primary_source'])}</b>, compared with <b>{e(sh['shadow_source'])}</b>). "
                 f"Newest observation was ahead on the primary side {sh['leader']['primary']}×, on the comparison side {sh['leader']['shadow']}×, equal {sh['leader']['tie']}×"
                 + (f"; median difference {sd['median']:+.0f} s." if sd.get("n") else ".") + "</p>")
        p.append(f"<p>Polls with errors — primary: {sh['error_polls']['primary']}, comparison: {sh['error_polls']['shadow']}. "
                 f"Temperature mismatches across polls: {sh['temp_mismatches']}.</p>")
        for side in ("primary", "shadow"):
            if sh["error_categories"][side]:
                p.append(f"<p class='note'>{e(side)} errors by type: " + ", ".join(f"{e(k)} × {v}" for k, v in sorted(sh["error_categories"][side].items())) + "</p>")
        for ex in sh["error_examples"]:
            p.append(f"<p class='note'><code>{e(ex)}</code></p>")
    p.append("</div>")

    miss = [(s, rep['missing'][s]) for s in SOURCES if rep['missing'][s]]
    if miss:
        p.append("<h2>Observations one source never delivered</h2><div class='card'>")
        for s, items in miss:
            p.append(f"<p><b>{e(LABEL[s])}</b>: " + ", ".join(fmt_time(i) for i in items[:60]) + " UTC</p>")
        p.append("</div>")

    p.append("<h2>Every observation</h2><div class='card scroll'><table><tr><th>Observed (UTC)</th><th>Miami</th>"
             "<th class='n'>LDM received</th><th class='n'>LDM age</th><th class='n'>LDM stamp lag</th>"
             "<th class='n'>Public received</th><th class='n'>Public age</th><th class='n'>Public stamp lag</th><th class='n'>LDM lead</th><th class='n'>°F (LDM / public)</th></tr>")
    for row in rep["observations"]:
        l, pub = row.get("ldm"), row.get("public")

        def cells(x):
            if not x:
                return "<td class='n'>—</td><td class='n'>—</td><td class='n'>—</td>"
            mark = " *" if x["startup"] else ""
            return (f"<td class='n'>{fmt_time(x['received'])}{mark}</td><td class='n'>{fmt_min(x['age_seconds'])}</td>"
                    f"<td class='n'>{fmt_min(x['snapshot_lag_seconds'])}</td>")
        ld = row.get("lead_seconds")
        cls = "" if ld is None else "pos" if ld > lead["tie_seconds"] else "neg" if ld < -lead["tie_seconds"] else ""
        temps = " / ".join("—" if not x or x.get("temp_f") is None else f"{x['temp_f']}" for x in (l, pub))
        p.append(f"<tr><td>{fmt_time(row['observation'], with_date=True)}</td><td>{fmt_time(row['observation'], miami=True)}</td>{cells(l)}{cells(pub)}"
                 f"<td class='n {cls}'>{'—' if ld is None else fmt_s(ld)}</td><td class='n'>{temps}</td></tr>")
    p.append("</table><p class='note'>* first batch after a server start: arrival time not trustworthy, excluded from statistics. "
             "Lead: positive = LDM received it first.</p></div>")

    p.append("<h2>Method and limits</h2><div class='card note'><ul>"
             "<li>Both sources are polled by the same backend about once a minute, so arrival times are accurate to roughly that.</li>"
             "<li>Snapshot stamps come from the servers' own clocks; if this PC's clock is off, the \"stamp → received\" stage is off by the same amount.</li>"
             "<li>NOAA's public files and the LDM feed carry the same MADIS product, so temperatures should match; differences point to timing (a newer revision) or QC.</li>"
             "<li>One day is one sample of NOAA's behaviour; delays vary with time of day and load.</li></ul></div>")
    p.append("</main></body></html>")
    return "".join(p)


def console_summary(rep):
    lines = [f"LDM vs public NOAA — {rep['date'] or 'all data'}",
             f"  observations seen: {rep['data']['observations_seen']}   arrival rows: {rep['data']['arrival_rows']}   comparisons: {rep['data']['shadow_rows']}"]
    for level, text in rep["findings"]:
        lines.append(f"  [{level}] {text}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="LDM vs public NOAA latency report from the backend's logs.")
    ap.add_argument("--date", help="Miami calendar date YYYY-MM-DD (default: today)")
    ap.add_argument("--all", action="store_true", help="use everything in the logs")
    ap.add_argument("--arrivals", default=str(BACKEND / "logs" / "asos_arrivals.jsonl"))
    ap.add_argument("--shadow", default=str(BACKEND / "logs" / "asos_shadow.jsonl"))
    ap.add_argument("--out", default=str(BACKEND / "logs"), help="folder for the report files")
    ap.add_argument("--tie-seconds", type=float, default=60, help="leads within this many seconds count as a tie")
    args = ap.parse_args(argv)

    date = None if args.all else (args.date or datetime.now(MIAMI).date().isoformat())
    rep = build_report(load_jsonl(args.arrivals), load_jsonl(args.shadow), date=date, tie_seconds=args.tie_seconds)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"report_{date or 'all'}"
    (out / f"{stem}.html").write_text(render_html(rep), encoding="utf-8")
    (out / f"{stem}.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print(console_summary(rep))
    print(f"\nWritten: {out / (stem + '.html')}\n         {out / (stem + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
