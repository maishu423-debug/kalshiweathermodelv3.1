"""tools/ldm_vs_public_report.py on synthetic logs whose right answers are known in advance."""
import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import ldm_vs_public_report as rpt  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)               # 06:00 in Miami


def arrival(source, obs, recv, stamp, temp=84.0, startup=False, corrected=False):
    version = f"1000|{stamp.isoformat()}" if source == "ldm" else format_datetime(stamp, usegmt=True)
    return {"source": source, "role": "primary" if source == "ldm" else "shadow",
            "observation_time_utc": obs.isoformat(), "backend_received_at_utc": recv.isoformat(),
            "logged_at": recv.isoformat(), "age_at_receipt_seconds": (recv - obs).total_seconds(), "temp_f": temp,
            "snapshot_filename": "x.gz", "snapshot_version": version, "download_ms": 300.0, "parse_ms": 20.0,
            "startup": startup, "corrected": corrected}


def observations(n=12):
    return [T0 + timedelta(minutes=5 * i) for i in range(n)]


def next_quarter(t):
    q = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
    return q if q >= t else q + timedelta(minutes=15)


def slow_ldm_rows(n=12, temp_shift=None):
    """The pattern seen in practice: the LDM snapshot is only saved at quarter-hours (stamped ~16 s after) and carries
    readings that are at least 10 minutes old; NOAA's public file has each reading ~7 min after it was taken."""
    rows = []
    for i, obs in enumerate(observations(n)):
        stamp = next_quarter(obs + timedelta(minutes=10)) + timedelta(seconds=16)
        rows.append(arrival("ldm", obs, stamp + timedelta(seconds=23), stamp, temp=84.0 + 0.1 * i))
        pub_stamp = obs + timedelta(minutes=7, seconds=15)
        shift = temp_shift.get(i, 0.0) if temp_shift else 0.0
        rows.append(arrival("public", obs, pub_stamp + timedelta(minutes=3), pub_stamp, temp=84.0 + 0.1 * i + shift))
    return rows


def fast_ldm_rows(n=12):
    rows = []
    for i, obs in enumerate(observations(n)):
        rows.append(arrival("ldm", obs, obs + timedelta(minutes=5), obs + timedelta(minutes=4, seconds=40), temp=80.0))
        rows.append(arrival("public", obs, obs + timedelta(minutes=8), obs + timedelta(minutes=7), temp=80.0))
    return rows


def test_slow_ldm_is_reported_as_slower_with_the_cause_in_the_source():
    rep = rpt.build_report(slow_ldm_rows(n=36), [], date="2026-10-29")          # three hours: enough distinct snapshots
    lead = rep["lead"]
    assert lead["n"] == 36 and lead["public_first"] > lead["ldm_first"] == 0 and lead["seconds"]["median"] < 0
    ldm, public = rep["sources"]["ldm"], rep["sources"]["public"]
    assert ldm["age_at_receipt_seconds"]["median"] > public["age_at_receipt_seconds"]["median"]
    assert public["snapshot_lag_seconds"]["median"] == pytest.approx(7 * 60 + 15)             # 7 min 15 s after the reading
    assert ldm["pickup_delay_seconds"]["median"] == pytest.approx(23.0)                        # our own pickup was quick
    assert ldm["quarter_clustering"]["fraction_within_window"] == 1.0
    assert public["quarter_clustering"]["fraction_within_window"] < 0.5
    text = " ".join(t for _, t in rep["findings"])
    assert "public NOAA files were first" in text and "SOURCE" in text and "timer" in text


def test_fast_ldm_is_reported_as_ahead():
    rep = rpt.build_report(fast_ldm_rows(), [], date="2026-10-29")
    lead = rep["lead"]
    assert lead["ldm_first"] == 12 and lead["seconds"]["median"] == pytest.approx(180.0)
    assert rep["sources"]["ldm"]["age_at_receipt_seconds"]["median"] == pytest.approx(300.0)
    assert rep["sources"]["ldm"]["quarter_clustering"]["fraction_within_window"] < 0.5
    text = " ".join(t for _, t in rep["findings"])
    assert "LDM delivered observations first in 12 of 12" in text and "timer" not in text


def test_startup_rows_are_shown_but_excluded_from_latency_statistics():
    rows = fast_ldm_rows()
    rows[0] = arrival("ldm", T0, T0 + timedelta(hours=3), T0 + timedelta(minutes=4), startup=True)   # absurd, but flagged
    rep = rpt.build_report(rows, [], date="2026-10-29")
    assert rep["sources"]["ldm"]["observations"] == 12 and rep["sources"]["ldm"]["observations_after_startup"] == 11
    assert rep["sources"]["ldm"]["age_at_receipt_seconds"]["max"] == pytest.approx(300.0)          # the 3 h outlier ignored
    assert rep["lead"]["n"] == 11


def test_corrections_are_counted_but_do_not_change_first_arrival():
    rows = fast_ldm_rows()
    rows.append(arrival("ldm", T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=29), temp=99.0, corrected=True))
    rep = rpt.build_report(rows, [], date="2026-10-29")
    assert rep["sources"]["ldm"]["corrections"] == 1
    assert rep["sources"]["ldm"]["age_at_receipt_seconds"]["max"] == pytest.approx(300.0)
    assert rep["temperature"]["mismatches"] == []


def test_temperature_mismatch_is_reported():
    rep = rpt.build_report(slow_ldm_rows(temp_shift={3: 1.0}), [], date="2026-10-29")
    t = rep["temperature"]
    assert t["compared"] == 12 and len(t["mismatches"]) == 1 and t["max_abs_diff_f"] == pytest.approx(1.0)
    assert any(level == "bad" and "differ" in text for level, text in rep["findings"])
    assert any("Temperatures agree" in text for _, text in rpt.build_report(fast_ldm_rows(), [], date="2026-10-29")["findings"])


def test_an_observation_only_one_source_delivered_is_listed():
    rows = [r for r in fast_ldm_rows() if not (r["source"] == "public" and r["observation_time_utc"] == observations()[4].isoformat())]
    rep = rpt.build_report(rows, [], date="2026-10-29")
    assert rep["missing"]["public"] == [observations()[4].isoformat()] and rep["missing"]["ldm"] == []
    assert rep["lead"]["n"] == 11


def test_freshness_simulation_matches_the_app_thresholds():
    rep = rpt.build_report(slow_ldm_rows(n=12), [], date="2026-10-29")
    fr = rep["freshness"]
    assert fr["window"]["minutes"] > 20
    assert fr["ldm"]["age"]["mean"] > fr["public"]["age"]["mean"]
    for s in ("ldm", "public"):
        total = fr[s]["healthy_pct"] + fr[s]["delayed_pct"] + fr[s]["unavailable_pct"]
        assert total == pytest.approx(100.0)
    # NOAA's data is ~10 min old and creeps past 15 min just before each new reading lands, so "healthy" is high, not 100%
    assert fr["public"]["healthy_pct"] > 90.0 and fr["ldm"]["healthy_pct"] < fr["public"]["healthy_pct"]


def test_date_filter_uses_the_miami_calendar_day():
    other_day = [arrival("ldm", T0 - timedelta(days=1), T0 - timedelta(days=1) + timedelta(minutes=9), T0 - timedelta(days=1))]
    rep = rpt.build_report(other_day + slow_ldm_rows(), [], date="2026-10-29")
    assert rep["data"]["arrival_rows"] == 24
    # 03:30Z on the 29th is still the 28th in Miami (EDT) and must not count towards the 29th
    late = [arrival("ldm", datetime(2026, 10, 29, 3, 30, tzinfo=UTC), datetime(2026, 10, 29, 3, 40, tzinfo=UTC), datetime(2026, 10, 29, 3, 38, tzinfo=UTC))]
    assert rpt.build_report(late, [], date="2026-10-29")["data"]["arrival_rows"] == 0
    assert rpt.build_report(late, [], date="2026-10-28")["data"]["arrival_rows"] == 1
    assert rpt.build_report(late + slow_ldm_rows(), [], date=None)["data"]["arrival_rows"] == 25


def shadow_row(at, **kw):
    base = {"at": at.isoformat(), "primary_source": "ldm", "shadow_source": "public", "newest_time_diff_seconds": -300.0,
            "leader": "primary", "primary_age_seconds": 600.0, "shadow_age_seconds": 900.0, "primary_status": "healthy",
            "shadow_status": "healthy", "temp_mismatches": 0, "max_temp_diff_f": 0.0, "primary_errors": [], "shadow_errors": []}
    base.update(kw)
    return base


def test_shadow_log_errors_are_tallied_by_category():
    shadow = [shadow_row(T0 + timedelta(minutes=i)) for i in range(5)]
    shadow.append(shadow_row(T0 + timedelta(minutes=6), primary_errors=["LDM listing: [timeout] timeout after 15s for /files"],
                             leader=None, newest_time_diff_seconds=None))
    shadow.append(shadow_row(T0 + timedelta(minutes=7), shadow_errors=["public 20261029_1000.gz: ConnectionError: x"], temp_mismatches=2))
    rep = rpt.build_report(fast_ldm_rows(), shadow, date="2026-10-29")
    sh = rep["shadow"]
    assert sh["comparisons"] == 7 and sh["leader"] == {"primary": 6, "shadow": 0, "tie": 0} or sh["leader"]["primary"] == 5
    assert sh["error_polls"] == {"primary": 1, "shadow": 1}
    assert sh["error_categories"]["primary"] == {"timeout": 1} and sh["error_categories"]["shadow"] == {"other": 1}
    assert sh["temp_mismatches"] == 2 and sh["newest_time_diff_seconds"]["median"] == -300.0
    assert any("Errors were recorded" in text for _, text in rep["findings"])


def test_no_data_gives_a_clear_message_not_a_crash():
    rep = rpt.build_report([], [], date="2026-10-29")
    assert rep["findings"][0][0] == "warn" and "No arrival data" in rep["findings"][0][1]
    page = rpt.render_html(rep)
    assert "No arrival data" in page and page.startswith("<!doctype html>")


def test_html_report_contains_every_section_and_escapes_text():
    rows = slow_ldm_rows()
    shadow = [shadow_row(T0, primary_errors=["<script>alert(1)</script> [auth] bad"])]
    page = rpt.render_html(rpt.build_report(rows, shadow, date="2026-10-29"))
    for heading in ("Summary", "Where the time goes", "Age of each observation when first received", "Head to head",
                    "What the live app would have seen", "Snapshot update pattern", "Temperature agreement",
                    "Errors and side-by-side polls", "Every observation", "Method and limits"):
        assert heading in page, heading
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert page.count("<td>10-29 10:") + page.count("<td>10-29 11:") == 12      # one table row per observation
    assert "LDM (private API)" in page and "Public NOAA (ASOS/MADIS)" in page


def test_command_line_writes_html_and_json(tmp_path, capsys):
    arrivals, shadow = tmp_path / "arr.jsonl", tmp_path / "shadow.jsonl"
    arrivals.write_text("\n".join(json.dumps(r) for r in slow_ldm_rows()) + "\n{broken json", encoding="utf-8")   # a torn last line
    shadow.write_text(json.dumps(shadow_row(T0)) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    code = rpt.main(["--date", "2026-10-29", "--arrivals", str(arrivals), "--shadow", str(shadow), "--out", str(out)])
    assert code == 0
    html_file, json_file = out / "report_2026-10-29.html", out / "report_2026-10-29.json"
    assert html_file.exists() and json_file.exists()
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["lead"]["n"] == 12 and data["data"]["arrival_rows"] == 24
    assert "LDM vs public NOAA" in capsys.readouterr().out


def test_command_line_with_missing_logs_still_writes_a_report(tmp_path):
    out = tmp_path / "out"
    assert rpt.main(["--all", "--arrivals", str(tmp_path / "none.jsonl"), "--shadow", str(tmp_path / "none2.jsonl"), "--out", str(out)]) == 0
    assert (out / "report_all.html").exists()


def test_snapshot_stamps_are_parsed_for_both_sources():
    assert rpt.stamp_of("ldm", "708366|2026-10-29T10:45:16.245016+00:00") == datetime(2026, 10, 29, 10, 45, 16, 245016, tzinfo=UTC)
    assert rpt.stamp_of("public", "Thu, 29 Oct 2026 10:32:15 GMT") == datetime(2026, 10, 29, 10, 32, 15, tzinfo=UTC)
    assert rpt.stamp_of("ldm", "garbage") is None and rpt.stamp_of("public", None) is None
