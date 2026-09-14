#!/usr/bin/env python3
"""Offline audit checks for PacificVanguard/dive-alert.

Usage: python3 thedivebell-regression-checks.py /path/to/dive-alert
Audited revision: c74af0bfa5e88643e7e008a8ea9036e57405b17a

These assert desired behavior. Failures are expected on the audited revision.
They make no external requests, send no messages, and modify no project files.
The raw-wind test expresses the audit's recommended use of the existing cap;
it is a proposed policy correction, not an empirically validated dive limit.
"""

import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("repository", type=Path)
args = parser.parse_args()
repository = args.repository.resolve()
source = repository / "dive_alert.py"
if not source.is_file():
    parser.error("The supplied directory must contain dive_alert.py")


def no_network(*args, **kwargs):
    raise RuntimeError("Network disabled in audit checks")


socket.socket.connect = no_network
socket.create_connection = no_network
spec = importlib.util.spec_from_file_location("dive_alert_audit_target", source)
d = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = d
spec.loader.exec_module(d)


class DiveBellAuditChecks(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, 4, tzinfo=d.PT)
        self.zone = d.CONFIG["zones"]["A"]
        self.window = d._window_at(self.now, 24)

    def calm(self, wind=1, wind_direction=270):
        fetches = d._mk_fetches(
            self.now, 0.5, 14, 195, wind_kn=wind, wind_dir=wind_direction
        )
        fetches["weather"].data["cloud_cover"] = d._flat_series(self.now, 200, 10)
        return fetches

    def evaluate(self, fetches):
        features = d.compute_features(self.window, fetches, self.zone, self.now)
        score, _, _, _ = d.score_window(features)
        gate, failures = d.perfect_gate(features, score, 20, self.zone)
        return score, gate, failures, features

    def assert_no_ideal(self, fetches, reason):
        score, gate, _, _ = self.evaluate(fetches)
        self.assertFalse(gate, f"{reason}: ideal gate passed at {score}/10")

    def test_01_complete_calm_control(self):
        _, gate, _, _ = self.evaluate(self.calm())
        self.assertTrue(gate, "The fully populated calm control should qualify")

    def test_02_failed_marine_forecast_cannot_pass_as_flat(self):
        fetches = self.calm()
        fetches["marine"] = d.Fetch("marine", False, error="audit source outage")
        self.assert_no_ideal(fetches, "Marine forecast absent, buoy still available")

    def test_03_missing_wave_partitions_cannot_pass_as_flat(self):
        fetches = self.calm()
        for key in list(fetches["marine"].data):
            if key.startswith(("swell_wave", "wind_wave")):
                fetches["marine"].data[key] = {}
        self.assert_no_ideal(fetches, "Both wave partitions absent")

    def test_04_one_rain_sample_cannot_certify_72_dry_hours(self):
        fetches = self.calm()
        fetches["weather"].data["precipitation"] = {
            d.floor_hour(self.window["start"]) - timedelta(hours=1): 0.0
        }
        self.assert_no_ideal(fetches, "Only one rainfall observation available")

    def test_05_heavy_rain_during_dive_blocks_dry_gate(self):
        fetches = self.calm()
        fetches["weather"].data["precipitation"][
            d.floor_hour(self.window["start"]) + timedelta(hours=1)
        ] = 0.4
        self.assert_no_ideal(fetches, "0.4 inches of forecast rain during the dive")

    def test_06_one_wind_sample_cannot_certify_whole_window(self):
        fetches = self.calm()
        fetches["weather"].data["wind_speed_10m"] = {
            d.floor_hour(self.window["start"]): 1.0
        }
        self.assert_no_ideal(fetches, "Only one wind sample available")

    def test_07_one_cloud_sample_cannot_certify_whole_window(self):
        fetches = self.calm()
        fetches["weather"].data["cloud_cover"] = {
            d.floor_hour(self.window["start"]): 10.0
        }
        self.assert_no_ideal(fetches, "Only one cloud-cover sample available")

    def test_08_raw_wind_cap_is_not_discounted_by_direction(self):
        score, gate, _, features = self.evaluate(self.calm(19, 45))
        self.assertLessEqual(
            score,
            d.CONFIG["scoring"]["wind_cap_score"],
            f"19kt raw / {features['wind_window_eff_kn']:.2f}kt effective "
            f"wind scored {score}/10; ideal={gate}",
        )

    def test_09_skill_adjustment_cannot_raise_hard_rain_cap(self):
        fetches = self.calm()
        fetches["weather"].data["precipitation"][
            d.floor_hour(self.window["start"]) - timedelta(hours=10)
        ] = 0.5
        scored = d.score_zone(
            "A", self.zone, fetches, self.now,
            skill_corr={"12-24h": 0.5, "24-48h": 0.5, ">48h": 0.5},
        )
        rainy = [s for s in scored if s.get("cap_reason") and "rain" in s["cap_reason"]]
        self.assertTrue(rainy, "Control must exercise a rain cap")
        cap = d.CONFIG["scoring"]["first_flush_cap"]
        self.assertTrue(
            all(s["score"] <= cap for s in rainy),
            f"Hard cap {cap}; post-correction scores {[s['score'] for s in rainy]}",
        )

    def test_10_hawaii_fallback_windows_use_hawaii_clock(self):
        zone = d.CONFIG["zones"]["J"]
        windows = d.build_windows(d.Fetch("weather", False), self.now, zone)
        self.assertTrue(windows)
        first = windows[0]["start"]
        local = first.astimezone(d.zone_tz(zone))
        self.assertEqual(
            first.utcoffset(), local.utcoffset(),
            f"Fallback labels {first.isoformat()}, which is {local.isoformat()} locally",
        )

    def ingest_sample(self, message, zone_key="A"):
        rows = []
        body = json.dumps({
            "event": "message", "time": int(self.now.timestamp()), "message": message
        })
        with patch.object(d, "http_get", return_value=body), \
             patch.object(d, "append_log", side_effect=lambda p, c, r, dry: rows.extend(r)), \
             patch.object(d, "now_pt", return_value=self.now), \
             patch.object(d, "zone_topic", return_value="audit-offline-only"), \
             contextlib.redirect_stdout(io.StringIO()):
            d.ingest_feedback({}, True, d.CONFIG["zones"][zone_key], zone_key)
        return rows

    def test_11_unidentified_sms_report_cannot_become_measured_visibility(self):
        rows = self.ingest_sample("clear|sms")
        self.assertFalse(
            any(r.get("viz_ft") not in (None, "") for r in rows),
            f"An unknown dive/window became a numeric measurement: {rows}",
        )

    def test_12_feedback_preserves_known_zone(self):
        rows = self.ingest_sample("clear|20260911T0542-dawn", "P")
        self.assertTrue(rows, "Valid categorical feedback should be retained")
        self.assertTrue(
            all(r.get("zone") == "P" for r in rows),
            f"Maui identity was lost: {rows}",
        )

    def test_13_published_last_ring_is_not_in_future(self):
        board = json.loads((repository / "data" / "zones.json").read_text())
        published = datetime.fromisoformat(board["updated"])
        invalid = []
        for key, zone in board["zones"].items():
            if zone.get("last_ring"):
                local_day = published.astimezone(d.zone_tz(zone)).date()
                if datetime.fromisoformat(zone["last_ring"]).date() > local_day:
                    invalid.append((key, zone["last_ring"]))
        self.assertFalse(invalid, f"Future dates presented as past rings: {invalid}")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
