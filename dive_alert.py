#!/usr/bin/env python3
"""
dive_alert.py — Laguna Beach dive-conditions scorer + alerter.

One file, stdlib only. Runs on GitHub Actions twice a day, scores the next
72h of dawn/dusk windows for Zone A (Laguna), and pushes an ntfy notification
when a window scores >= 7.0 and sits 12-48h out.

Commands
    python3 dive_alert.py run        [--dry-run] [--offline [--fixtures NAME]] [--brief]
    python3 dive_alert.py log-dive   --site "Shaw's Cove" --viz 15 --surge low [--notes "..."]
    python3 dive_alert.py hindcast   [--days 180]
    python3 dive_alert.py backtest
    python3 dive_alert.py test                  # scenario + fixture suite (the spec)
    python3 dive_alert.py record-fixtures       # snapshot live responses into fixtures/normal

State: data/score_log.csv, data/alert_state.json, data/dive_log.csv, data/hindcast.csv
Secrets: env NTFY_TOPIC / HEALTHCHECKS_URL, or git-ignored local_config.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
FIXTURES = os.path.join(ROOT, "fixtures")

# =====================================================================
# CONFIG — everything tunable lives here. Each constant carries a
# CALIBRATION note saying what dive-log evidence would move it.
# =====================================================================

CONFIG = {
    "zones": {
        "A": {
            "name": "Laguna",
            "enabled": True,
            # Every bell is individually cast and named. Topics are public by
            # design (they're printed on the site); the NTFY_TOPIC secret
            # remains as an override for Zone A only, for continuity.
            "bell": {"no": 1, "name": "Laguna Beach", "cast": "2026-08-11"},
            "topic": "laguna-dive-86dd82e0",
            "keeper": "Curtis",
            # per-zone instruments: the same seam that lets Monterey's cold
            # water in later lets a tideless international coast in (Phase 3)
            "tide_station": "9410580", "buoy": "46253", "buoy_offshore": "46086",
            # sworn = buoy-validated + hindcast-fitted; its gate may ring.
            # provisional = newly cast; it watches and speaks but cannot ring.
            "tier": "sworn",
            "region": 'Southern California',
            "season_note": 'December mornings, mostly, after the Santa Anas have swept the sea flat',
            "casting": {"pct7": 24, "med": 5.6, "season": 'December through January, mostly',
                        "note": "Held against three years of ocean: 41 rings \u2014 twenty-one in the best year, five in the storm-wrecked El Ni\u00f1o winter, nearly all of them October through January.",},
            "lat": 33.542, "lon": -117.785,   # Open-Meteo query point, just offshore of Heisler
            # Direction exposure map: swell direction (deg true, coming-from) -> 0..1
            # exposure of the Laguna coves. Coast faces ~SW; strong S/SW exposure,
            # partial Catalina/Palos Verdes shadow to the W/NW, land to the N/E.
            # CALIBRATION: if a logged WNW day showed more surge than scored,
            # raise the 270-300 values; if S swell days score too harsh, lower 180-220.
            # 2026-08-10: due-south was drafted at 1.00, which made the hindcast
            # call June/July near-undiveable (0-2% of windows >=7). Confirmed
            # with the diver: summer S swell is surgy but diveable, so the S
            # window is eased. CALIBRATION: if summer logged viz keeps beating
            # the score, ease 180-220 further; if summer alerts disappoint, raise it.
            "exposure": [(0, 0.10), (90, 0.15), (157, 0.52), (180, 0.80),
                         (220, 0.80), (245, 0.70), (270, 0.55), (285, 0.38),
                         (300, 0.28), (330, 0.15), (360, 0.10)],
            # Direction the offshore (land) wind comes FROM — Laguna Canyon
            # funnels Santa Anas out of the NE. Wind from here flattens the
            # nearshore instead of chopping it.
            "offshore_dir": 45,
            # Every Zone A site is a pocket cove between rocky points, not open
            # beach. The zone-level swell model describes open coast, so without
            # this the model systematically over-penalizes: divers get into
            # Shaw's on days that close out an open beach. One knob, tuned
            # against the hindcast. CALIBRATION: raise toward 1.0 if alerts
            # over-promise on surgy days; lower if good days score too low.
            "cove_damage_factor": 0.62,
            # All Zone A sites sit inside Laguna Beach SMR or Laguna Beach SMCA
            # (No-Take): take_allowed is False everywhere. Spot-checked against
            # CDFW's Southern California MPA network 2026-08; CDFW's site
            # restructure blocked deep links — re-verify boundaries if enabling
            # any take-oriented feature. shelter is a site-rank bonus, +-0.5 max.
            "sites": [
                {"name": "Crescent Bay", "depth_ft": 30, "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.1, "tide": "any",
                 "entry": "sand walk-in; deep channel mid-beach",
                 "note": "Seal Rock reef on the north end; longest swim to structure"},
                {"name": "Shaw's Cove", "depth_ft": 20, "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.3, "tide": "any",
                 "entry": "stairs to sand; easy walk-in",
                 "note": "most protected cove; west reef crevice at mid-high tide"},
                {"name": "Divers Cove", "depth_ft": 25, "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.2, "tide": "any",
                 "entry": "sand walk-in between reefs",
                 "note": "short swim to kelp; garibaldi central"},
                {"name": "Fisherman's Cove", "depth_ft": 20, "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.2, "tide": "mid-high",
                 "entry": "narrow sand channel between rock shelves",
                 "note": "channel gets shallow and grabby at low tide"},
                {"name": "Picnic Beach (Heisler)", "depth_ft": 15, "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.0, "tide": "high",
                 "entry": "rocky shelf; ankle-twister at low tide",
                 "note": "best structure close in; enter north of the point"},
                {"name": "Wood's Cove", "depth_ft": 25, "take_allowed": False, "creek_adjacent": False,
                 "shelter": -0.1, "tide": "mid",
                 "entry": "stairs; rock outcrops both sides",
                 "note": "boulder field holds fish; watch the shorebreak slot"},
                {"name": "Cleo Street barge", "depth_ft": 25, "take_allowed": False, "creek_adjacent": False,
                 "shelter": -0.3, "tide": "any",
                 "entry": "sand entry, ~150 yd surface swim",
                 "note": "barge wreck ~25 ft; calm days only, mind the swim"},
            ],
        },
        "B": {
            "name": "South Laguna / Aliso",
            "enabled": False,   # flip to True to go live; fetch geometry is ready
            "lat": 33.510, "lon": -117.752,
            "exposure": [(0, 0.10), (90, 0.15), (157, 0.65), (180, 1.00),
                         (220, 1.00), (245, 0.90), (270, 0.65), (285, 0.45),
                         (300, 0.30), (330, 0.15), (360, 0.10)],
            "sites": [
                # VERIFY take_allowed against current CDFW boundaries before enabling:
                # Aliso sits inside Laguna Beach SMCA (No-Take); Thousand Steps is
                # south of the Table Rock boundary.
                {"name": "Aliso Beach", "depth_ft": 15, "take_allowed": False, "creek_adjacent": True,
                 "shelter": -0.2, "tide": "any",
                 "entry": "steep sand; shorebreak-prone", "note": "Aliso Creek outflow — 96h rain rule"},
                {"name": "Thousand Steps", "depth_ft": 20, "take_allowed": True, "creek_adjacent": False,
                 "shelter": 0.0, "tide": "mid-high",
                 "entry": "long stairs, sand entry", "note": "VERIFY take boundary before enabling"},
            ],
        },
        "C": {
            "name": "Dana Point",
            "enabled": True,
            # BELL No. 2 — cast 2026-08-17. Provisional: no local diver has
            # confirmed its water, so its gate is locked shut until it is
            # sworn (buoy check + first verdicts). It scores, digests and
            # alerts, and says on its plate that it is still earning its ring.
            "bell": {"no": 2, "name": "Dana Point", "cast": "2026-08-17"},
            "topic": "danapoint-dive-8952a5b5",
            "tier": "provisional",
            "keeper": None,
            "tide_station": "9410580", "buoy": "46253", "buoy_offshore": "46086",
            "offshore_dir": 40,
            # Salt Creek is open beach, the headland shelters less than
            # Laguna's pocket coves — less protection than Laguna's 0.62.
            # CALIBRATION: set from the casting hindcast; retune on verdicts.
            "cove_damage_factor": 0.75,
            "region": 'Southern California',
            "season_note": 'winter mornings, when the swell finally lets the headland rest',
            "casting": {"pct7": 19, "med": 4.9, "season": 'December through February',},
            "lat": 33.460, "lon": -117.714,
            "exposure": [(0, 0.10), (90, 0.20), (157, 0.70), (180, 1.00),
                         (220, 1.00), (245, 0.90), (270, 0.55), (285, 0.35),
                         (300, 0.25), (330, 0.15), (360, 0.10)],
            "sites": [
                {"name": "Salt Creek", "depth_ft": 20, "take_allowed": False, "creek_adjacent": True,
                 "shelter": 0.0, "tide": "any",
                 "entry": "long beach walk", "note": "Dana Point SMCA — VERIFY current take rules"},
                # take_allowed stays False until CDFW rules are verified in
                # person — an unverified True would wake the take machinery.
                {"name": "Dana Point Harbor breakwall (outside)", "depth_ft": 30, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "boat or long swim", "note": "VERIFY take rules before flipping take_allowed"},
            ],
        },

        "D": {
            "name": "La Jolla",
            "enabled": True,
            # BELL No.3 — cast 2026-08-17. A second pocket-cove water, new
            # buoy (Scripps 46254) and tide station (9410230): the first bell
            # whose instruments are entirely its own.
            "bell": {"no": 3, "name": "La Jolla", "cast": "2026-08-17"},
            "topic": "lajolla-dive-8ee44bdc",
            "tier": "provisional",
            "keeper": None,
            "tide_station": "9410230", "buoy": "46254", "buoy_offshore": "46086",
            "offshore_dir": 75,
            "cove_damage_factor": 0.62,   # the Cove is a true pocket, Laguna-like
            "region": 'Southern California',
            "season_note": 'the cold clear mornings after a north swell fades out',
            "casting": {"pct7": 23, "med": 5.3, "season": 'March, August, October',},
            "lat": 32.850, "lon": -117.272,
            # Point La Jolla shelters the south; the Cove faces W-NW.
            # CALIBRATION: drafted from coast geometry, unconfirmed by a local.
            "exposure": [(0, 0.12), (90, 0.12), (157, 0.35), (200, 0.55),
                         (245, 0.80), (285, 1.00), (310, 0.90), (330, 0.40),
                         (360, 0.15)],
            "sites": [
                # Matlahuayl SMR — no take, full stop.
                {"name": "La Jolla Cove", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "steps to a small sand pocket",
                 "note": "sea lions, leopard sharks over the sand in late summer"},
                {"name": "La Jolla Shores (Canyon)", "depth_ft": 40, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.0, "tide": "any",
                 "entry": "long flat sand walk-in",
                 "note": "canyon head drops fast; navigate by the depth, not the sand"},
            ],
        },
        "E": {
            "name": "Monterey",
            # First casting FAILED (median 1.4, 5% >=7) and the bell was
            # benched — see marine_height_scale below for the diagnosis and
            # the mechanism that recast it. hindcast_E.csv holds both pours.
            "enabled": True,
            # BELL No.4 — cast 2026-08-17. The first bell of a NEW WATER
            # FAMILY: bay-sheltered and cold. The peninsula blocks the south
            # entirely and blunts the NW; and 55°F is a fine morning here, so
            # the gate's 'warm' threshold is overridden per-zone — cold water
            # is this bell's normal, not a defect.
            "bell": {"no": 4, "name": "Monterey", "cast": "2026-08-17"},
            "topic": "monterey-dive-d01f1d8e",
            "tier": "provisional",
            "keeper": None,
            "tide_station": "9413450", "buoy": "46240", "buoy_offshore": "46042",
            "offshore_dir": 190,          # land lies south of the breakwater shore
            "cove_damage_factor": 0.50,   # famously protected inside the bay
            "perfect_gate_overrides": {"min_sst_c": 11.5},   # ~53°F: warm, for Monterey
            # THE BAY MECHANISM (2026-08-17): every candidate model point snaps
            # to one coarse grid cell that reports ~4.4ft mean where the
            # Breakwater famously sits at 1-2ft — inside-bay shelter is
            # sub-grid, invisible to the model. This scale says "the model
            # overstates this water's height by ~half" and feeds BOTH damage
            # and the turbidity memory. Fit on the failed casting's own
            # evidence: s=0.55 → 27% >=7, med 5.5, mid-band with slack both
            # ways (in-band range was 0.50-0.70, not a knife-edge).
            # CALIBRATION: first verdicts from a Monterey keeper judge it.
            "marine_height_scale": 0.55,
            "region": 'Central Coast',
            "season_note": "the bay's still mornings, when the peninsula holds the ocean off",
            "casting": {"pct7": 28, "med": 5.5, "season": 'June through September',},
            "lat": 36.611, "lon": -121.898,
            # North-facing shore inside the bay: only wrapped NW-N energy
            # arrives. CALIBRATION: drafted from geometry, unconfirmed by a local.
            "exposure": [(0, 0.50), (45, 0.35), (90, 0.15), (157, 0.05),
                         (220, 0.05), (270, 0.15), (300, 0.35), (315, 0.55),
                         (335, 0.62), (360, 0.50)],
            "sites": [
                {"name": "Breakwater (San Carlos)", "depth_ft": 30, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "sand ramp beside the wall",
                 "note": "the wall to the sea lions, Metridium fields out over the sand"},
                {"name": "McAbee Beach", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.1, "tide": "mid-high",
                 "entry": "short sand pocket off Cannery Row",
                 "note": "kelp thickens fast in summer — plan the swim-out lanes"},
            ],
        },
        "F": {
            "name": "Catalina",
            "enabled": True,
            # BELL No.5 — Casino Point Dive Park, Avalon. THE SoCal dive park.
            # NE-facing lee of the island: the model's channel cell sees swell
            # the island itself blocks — height scale expected at casting.
            "bell": {"no": 5, "name": "Catalina", "cast": "2026-08-17"},
            "topic": "catalina-dive-8fac8d86", "tier": "provisional", "keeper": None,
            "tide_station": "9410079", "buoy": "46221", "buoy_offshore": "46086",
            "offshore_dir": 225,          # the island's interior lies SW of Avalon
            "cove_damage_factor": 0.70,
            # Fit on this bell's own casting evidence (model cell already sits in the island's lee my exposure map discounts
            # again — the scale RAISES effective height to undo the double count).
            "marine_height_scale": 1.60,
            "region": 'Southern California',
            "season_note": "the island's quiet mornings, in the lee where the channel goes glass",
            "casting": {"pct7": 34, "med": 6.3, "season": 'March through April, mostly',},
            "lat": 33.345, "lon": -118.325,
            "exposure": [(0, 0.45), (45, 0.60), (90, 0.40), (157, 0.15),
                         (200, 0.10), (245, 0.06), (285, 0.06), (330, 0.20), (360, 0.45)],
            "sites": [
                {"name": "Casino Point Dive Park", "depth_ft": 40, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "stairs off the breakwater, gear-up benches",
                 "note": "kelp cathedral, the wrecks on the sand line; ferry over, walk in"},
                {"name": "Lover's Cove", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "cobble beach east of the ferry landing",
                 "note": "garibaldi thick as confetti; snorkel-friendly"},
            ],
        },
        "G": {
            "name": "Palos Verdes",
            "enabled": True,
            # BELL No.6 — Terranea / Old Marineland. Laguna's northern cousin:
            # SW-facing open coast, rocky coves, same swell family.
            "bell": {"no": 6, "name": "Palos Verdes", "cast": "2026-08-17"},
            "topic": "palosverdes-dive-0dcfd89c", "tier": "provisional", "keeper": None,
            "tide_station": "9410660", "buoy": "46221", "buoy_offshore": "46086",
            "offshore_dir": 45,
            "cove_damage_factor": 0.70,
            # Fit on this bell's own casting evidence (near-band first pour; light ease).
            "marine_height_scale": 0.85,
            "region": 'Southern California',
            "season_note": 'December mornings, when the point stops taking the swell',
            "casting": {"pct7": 24, "med": 5.3, "season": 'March, August, December',},
            "lat": 33.738, "lon": -118.396,
            "exposure": [(0, 0.10), (90, 0.12), (157, 0.50), (180, 0.80),
                         (220, 0.90), (245, 0.85), (270, 0.70), (285, 0.50),
                         (300, 0.35), (330, 0.15), (360, 0.10)],
            "sites": [
                {"name": "Terranea (Old Marineland)", "depth_ft": 30, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.1, "tide": "mid-high",
                 "entry": "cobble cove below the resort trail",
                 "note": "120 Reef beyond the kelp line; watch the cobble in surge"},
                {"name": "Christmas Tree Cove", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "steep trail, small pocket beach",
                 "note": "clearest water on the peninsula on its day"},
            ],
        },
        "H": {
            "name": "Point Lobos",
            "enabled": True,
            # BELL No.7 — Whalers Cove, Point Lobos SNR. Permit water; the
            # finest shore diving in California by reputation. Carmel Bay
            # shelters the south entirely; NW wraps in through the mouth.
            "bell": {"no": 7, "name": "Point Lobos", "cast": "2026-08-17"},
            "topic": "pointlobos-dive-e69f7b6a", "tier": "provisional", "keeper": None,
            "tide_station": "9413450", "buoy": "46239", "buoy_offshore": "46042",
            "offshore_dir": 160,
            "cove_damage_factor": 0.55,
            "perfect_gate_overrides": {"min_sst_c": 11.5},
            # Fit on this bell's own casting evidence (bay-mouth shelter deeper than pre-set).
            "marine_height_scale": 0.49,
            "region": 'Central Coast',
            "season_note": 'the rare still mornings inside Carmel Bay',
            "casting": {"pct7": 24, "med": 5.0, "season": 'June through September, mostly',},
            "lat": 36.522, "lon": -121.940,
            "exposure": [(0, 0.55), (30, 0.45), (90, 0.15), (157, 0.05),
                         (220, 0.08), (270, 0.25), (300, 0.50), (330, 0.60), (360, 0.55)],
            "sites": [
                {"name": "Whalers Cove", "depth_ft": 40, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "boat ramp; reserve a dive permit ahead",
                 "note": "Middle Reef's hydrocoral; the cove IS the reserve — permits cap divers"},
                {"name": "Bluefish Cove", "depth_ft": 60, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.1, "tide": "any",
                 "entry": "swim or scooter from Whalers",
                 "note": "the drop past the pinnacles — advanced, worth every kick"},
            ],
        },
        "I": {
            "name": "Santa Barbara",
            "enabled": True,
            # BELL No.8 — Refugio / Tajiguas. South-facing lee of Point
            # Conception; the Channel Islands shadow much of the south swell —
            # double shelter, height scale expected at casting.
            "bell": {"no": 8, "name": "Santa Barbara", "cast": "2026-08-17"},
            "topic": "santabarbara-dive-64e572f7", "tier": "provisional", "keeper": None,
            "tide_station": "9411340", "buoy": "46054", "buoy_offshore": "46054",
            "offshore_dir": 350,
            "cove_damage_factor": 0.70,
            "perfect_gate_overrides": {"min_sst_c": 13.5},
            # Fit on this bell's own casting evidence (Point Conception + island shadow double-counted with exposure; raised).
            "marine_height_scale": 1.15,
            "region": 'Central Coast',
            "season_note": 'the mornings the islands hold the swell offshore',
            "casting": {"pct7": 31, "med": 6.0, "season": 'August through October, mostly',},
            "lat": 34.462, "lon": -120.070,
            "exposure": [(0, 0.10), (90, 0.15), (157, 0.70), (180, 0.90),
                         (220, 0.85), (245, 0.50), (270, 0.20), (285, 0.10),
                         (300, 0.08), (330, 0.08), (360, 0.10)],
            "sites": [
                {"name": "Refugio State Beach", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": True, "shelter": 0.2, "tide": "any",
                 "entry": "sand beside the point, palms at your back",
                 "note": "eelgrass and reef west of the point; VERIFY take rules"},
                {"name": "Tajiguas Reef", "depth_ft": 30, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.1, "tide": "mid-high",
                 "entry": "pullout beach, short swim",
                 "note": "low-relief reef fingers; navigation practice water"},
            ],
        },
        "J": {
            "name": "Oahu North Shore",
            "enabled": True,
            # BELL No.9 — Shark's Cove, Pupukea. The inverse of SoCal: glass
            # all summer, unrideable all winter. First bell on Hawaii clock
            # and Hawaii rain (no SoCal flush season).
            "bell": {"no": 9, "name": "Oahu North Shore", "cast": "2026-08-17"},
            "topic": "oahunorth-dive-c7559382", "tier": "provisional", "keeper": None,
            "tz": "Pacific/Honolulu",
            "first_flush_months": [],
            "tide_station": "1612340", "buoy": "51201", "buoy_offshore": "51003",
            "offshore_dir": 140,          # trades come over the island from ESE
            "cove_damage_factor": 0.62,
            # Fit on this bell's own casting evidence (the cell drinks the full N Pacific winter Shark's Cove never swims in).
            "marine_height_scale": 0.45,
            "region": 'Hawaii',
            "season_note": 'summer mornings, when the winter giants are half a world away',
            "casting": {"pct7": 23, "med": 4.0, "season": 'August through October, mostly',},
            "lat": 21.655, "lon": -158.063,
            "exposure": [(0, 0.85), (45, 0.50), (90, 0.20), (157, 0.10),
                         (200, 0.15), (245, 0.30), (285, 0.70), (315, 1.00),
                         (340, 0.95), (360, 0.85)],
            "sites": [
                {"name": "Shark's Cove", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "mid-high",
                 "entry": "lava-rock puzzle at the north end",
                 "note": "caves and arches left of the cove mouth; summer water only"},
                {"name": "Three Tables", "depth_ft": 30, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "sand between the table rocks",
                 "note": "the tables' seaward walls; turtles on the ledges"},
            ],
        },
        "K": {
            "name": "Kona",
            "enabled": True,
            # BELL No.10 — Two Step, Honaunau. Leeward Big Island: the island
            # blocks the trades and most swell; height scale expected. The
            # calmest famous shore dive in America.
            "bell": {"no": 10, "name": "Kona", "cast": "2026-08-17"},
            "topic": "kona-dive-2127ce9f", "tier": "provisional", "keeper": None,
            "tz": "Pacific/Honolulu",
            "first_flush_months": [],
            "tide_station": "1617433", "buoy": "51003", "buoy_offshore": "51003",
            "offshore_dir": 90,           # the volcano's slope lies due east
            "cove_damage_factor": 0.62,
            # Fit on this bell's own casting evidence (lee of the lee; raised to undo the double count).
            "marine_height_scale": 1.40,
            "region": 'Hawaii',
            "season_note": 'the leeward mornings, when the island keeps the trades off the water',
            "casting": {"pct7": 23, "med": 5.3, "season": 'October through November, mostly',},
            "lat": 19.421, "lon": -155.913,
            "exposure": [(0, 0.10), (90, 0.08), (157, 0.15), (200, 0.35),
                         (245, 0.50), (270, 0.45), (300, 0.25), (330, 0.12), (360, 0.10)],
            "sites": [
                {"name": "Two Step (Honaunau)", "depth_ft": 35, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "the lava two-step beside the boat ramp",
                 "note": "coral garden drops to the aquarium; dolphins some mornings"},
                {"name": "Kahalu'u", "depth_ft": 15, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "mid-high",
                 "entry": "sand channel through the breakwater",
                 "note": "turtle cleaning stations in snorkel depth"},
            ],
        },
        "L": {
            "name": "Bonaire",
            "enabled": True,
            # BELL No.11 — the shore-diving capital of Earth, and the first
            # bell beyond US waters. No NDBC ear, no NOAA tide — and honestly
            # tideless: Caribbean microtides barely move. Model-read forever
            # until the world grows instruments here.
            "bell": {"no": 11, "name": "Bonaire", "cast": "2026-08-17"},
            "topic": "bonaire-dive-c06b33ce", "tier": "provisional", "keeper": None,
            "tz": "America/Kralendijk", "first_flush_months": [],
            "tide_station": "none", "buoy": "none", "buoy_offshore": "none",
            "offshore_dir": 90,           # trades cross the island from the east
            "cove_damage_factor": 0.62,
            # Fit on this bell's own casting evidence (eternal-lee double
            # count, the Catalina pattern; offline sweep said 1.3, pipeline
            # gap says land lower band).
            "marine_height_scale": 1.30,
            "region": 'Caribbean',
            "season_note": 'the mornings the trades ease and the lee goes to glass',
            "casting": {"pct7": 27, "med": 5.8, "season": 'August through October, mostly',},
            "lat": 12.16, "lon": -68.29,
            "exposure": [(0, 0.35), (45, 0.20), (90, 0.08), (157, 0.10),
                         (200, 0.25), (245, 0.50), (285, 0.70), (315, 0.60),
                         (340, 0.45), (360, 0.35)],
            "sites": [
                {"name": "Bari Reef", "depth_ft": 40, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "dock steps at Sand Dollar",
                 "note": "the most fish-counted reef in the Caribbean; tanks by the door"},
                {"name": "1000 Steps", "depth_ft": 35, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "67 limestone steps that feel like 1000 with tanks",
                 "note": "elkhorn stands and turtle traffic; the stairs earn the name on exit"},
            ],
        },
        "M": {
            "name": "Malibu",
            "enabled": True,
            # BELL No.12 — Point Dume's cove and pinnacles; Santa Monica Bay's
            # western gate. SoCal swell family, Santa Anas down the canyons.
            "bell": {"no": 12, "name": "Malibu", "cast": "2026-08-17"},
            "topic": "malibu-dive-139a19c8", "tier": "provisional", "keeper": None,
            "tide_station": "9410840", "buoy": "46221", "buoy_offshore": "46086",
            "offshore_dir": 30,
            "cove_damage_factor": 0.70,
            "region": 'Southern California',
            "season_note": 'the offshore mornings, when the canyons breathe out to sea',
            "casting": {"pct7": 23, "med": 5.4, "season": 'October through January, mostly',},
            "lat": 33.990, "lon": -118.805,
            "exposure": [(0, 0.10), (90, 0.12), (157, 0.55), (180, 0.85),
                         (220, 0.90), (245, 0.80), (270, 0.55), (285, 0.35),
                         (300, 0.25), (330, 0.12), (360, 0.10)],
            "sites": [
                {"name": "Point Dume Cove", "depth_ft": 30, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "mid-high",
                 "entry": "the stairs then sand, east of the point",
                 "note": "pinnacles off the point hold the life; seals patrol the wall"},
                {"name": "Leo Carrillo (Sequit Point)", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": True, "shelter": 0.1, "tide": "any",
                 "entry": "sand west of the point, kelp close in",
                 "note": "Arroyo Sequit runs after rain — the creek rule earns its keep here"},
            ],
        },
        "N": {
            "name": "Ventura County",
            "enabled": True,
            # BELL No.13 — County Line's kelp reef and the La Jenelle wreck.
            # The Anacapa Passage buoy is the right ear for this water.
            "bell": {"no": 13, "name": "Ventura County", "cast": "2026-08-17"},
            "topic": "ventura-dive-b1a44410", "tier": "provisional", "keeper": None,
            "tide_station": "9411189", "buoy": "46217", "buoy_offshore": "46054",
            "offshore_dir": 40,
            "cove_damage_factor": 0.80,   # open reef coast, little pocket shelter
            "region": 'Southern California',
            "season_note": 'winter mornings, between the swells that feed the point',
            "casting": {"pct7": 27, "med": 5.6, "season": 'December through January, mostly',},
            "lat": 34.051, "lon": -118.964,
            "exposure": [(0, 0.10), (90, 0.12), (157, 0.60), (180, 0.80),
                         (220, 0.85), (245, 0.70), (270, 0.50), (285, 0.35),
                         (300, 0.30), (330, 0.12), (360, 0.10)],
            "sites": [
                {"name": "County Line Reef (Yerba Buena)", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.1, "tide": "mid-high",
                 "entry": "sand beside the point, watch the surfers' lineup",
                 "note": "kelp rows over low reef fingers; share the water politely"},
                {"name": "La Jenelle (Port Hueneme)", "depth_ft": 20, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "beach beside the jetty at Silver Strand",
                 "note": "the liner that beached in '70 — her bones make the jetty's south arm"},
            ],
        },
        "O": {
            "name": "Sydney",
            "enabled": True,
            # BELL No.14 — Shelly Beach, Cabbage Tree Bay. Southern hemisphere:
            # seasons inverted, Sydney clock, no NDBC ear (BOM's buoys speak
            # another network). Model-read until an Australian keeper appears.
            "bell": {"no": 14, "name": "Sydney", "cast": "2026-08-17"},
            "topic": "sydney-dive-75fdf41a", "tier": "provisional", "keeper": None,
            "tz": "Australia/Sydney", "first_flush_months": [],
            "tide_station": "none", "buoy": "none", "buoy_offshore": "none",
            "offshore_dir": 240,
            "cove_damage_factor": 0.55,
            "perfect_gate_overrides": {"min_sst_c": 15.0},
            "region": 'Australia',
            "season_note": 'the winter mornings, when the southerly finally sleeps',
            "casting": {"pct7": 33, "med": 4.7, "season": 'September through November, mostly',},
            "lat": -33.800, "lon": 151.297,
            "exposure": [(0, 0.70), (30, 0.60), (60, 0.40), (90, 0.25),
                         (135, 0.12), (180, 0.08), (225, 0.10), (270, 0.15),
                         (315, 0.40), (350, 0.65), (360, 0.70)],
            "sites": [
                {"name": "Shelly Beach (Cabbage Tree Bay)", "depth_ft": 20, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "sand walk-in off the promenade",
                 "note": "aquatic reserve — dusky whalers, blue gropers, weedy seadragons"},
                {"name": "Fairy Bower", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "mid-high",
                 "entry": "the bower steps, swim the pipeline out",
                 "note": "sponge gardens along the wall toward Manly"},
            ],
        },
        "P": {
            "name": "Maui",
            "enabled": True,
            # BELL No.15 — Kahekili and Black Rock, leeward West Maui.
            "bell": {"no": 15, "name": "Maui", "cast": "2026-08-17"},
            "topic": "maui-dive-134339c2", "tier": "provisional", "keeper": None,
            "tz": "Pacific/Honolulu", "first_flush_months": [],
            "tide_station": "1615680", "buoy": "51205", "buoy_offshore": "51003",
            "offshore_dir": 70,
            "cove_damage_factor": 0.62,
            "region": 'Hawaii',
            "season_note": "the leeward mornings, in the island's own wind shadow",
            "casting": {"pct7": 34, "med": 4.0, "season": 'January, April, August',},
            "lat": 20.938, "lon": -156.694,
            "exposure": [(0, 0.30), (45, 0.15), (90, 0.08), (157, 0.15),
                         (200, 0.40), (245, 0.60), (285, 0.55), (315, 0.45),
                         (340, 0.35), (360, 0.30)],
            "sites": [
                {"name": "Kahekili (Airport Beach)", "depth_ft": 30, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.3, "tide": "any",
                 "entry": "sand, reef starts at your fins",
                 "note": "herbivore reserve — the healthiest coral on the west side"},
                {"name": "Black Rock (Pu'u Keka'a)", "depth_ft": 25, "take_allowed": False,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "off the sand at Ka'anapali's north end",
                 "note": "the wall wraps the point; turtles thick by the cliff jumpers"},
            ],
        },
    },

    # ---- windows ------------------------------------------------------
    # Dawn = first light (sunrise - 30 min) for 3h. Dusk = 2h before sunset.
    # CALIBRATION: if logged dawn dives say the light is diveable earlier,
    # widen first_light_offset_min.
    "windows": {
        "dawn_first_light_offset_min": 30,
        "dawn_hours": 3.0,
        "dusk_hours": 2.0,
        # Night windows exist but only activate for take_allowed sites in season.
        "night_start_after_sunset_h": 1.0,
        "night_hours": 3.0,
        # VERIFY exact CDFW dates when a take zone is enabled. Approximation:
        # season opens the Saturday before the first Wednesday in October and
        # closes the first Wednesday after March 15.
        "lobster_season": {"open_month": 10, "close_month": 3},
    },

    # ---- feature math -------------------------------------------------
    "features": {
        "wind_lookback_h": 48, "wind_half_life_h": 18,   # CALIBRATION: shorten half-life if afternoon blows recover faster than scored
        "swell_lookback_h": 72, "swell_half_life_h": 24,  # CALIBRATION: lengthen if big-swell turbidity lingers in the log
        "rain_threshold_in": 0.2,                         # rolling 24h sum that counts as "rain"
        "rain_lookback_h": 72,
        "creek_rain_lookback_h": 96,                      # dormant until a creek_adjacent site is enabled
    },

    # ---- scoring ------------------------------------------------------
    # damage = height_ft^2 * period_factor * exposure, meaned over window hours.
    # Short-period windswell wrecks viz far beyond its height; long-period
    # groundswell at moderate height is comparatively gentle.
    "scoring": {
        # (period_s, multiplier) piecewise-linear.
        # CALIBRATION: log pairs of same-height days at different periods; if the
        # 8s day dove better than scored, pull the <=9s values down.
        "period_factor": [(6, 2.8), (9, 2.5), (11, 1.4), (13, 1.0), (14, 0.6), (16, 0.45), (20, 0.35)],
        # damage units -> penalty points off 10.
        # CALIBRATION: anchor vs logged viz — a day scoring 8 should have shown
        # ~15-20ft. Tightened 2026-08-10 after the first 180d hindcast put 62%
        # of windows >=7; average summer days (2ft @ 11s S) now land ~6.5.
        # NOTE: this curve and each zone's cove_damage_factor interact — the
        # factor sets how hard that coastline is hit, this curve maps damage to
        # the 1-10 scale. Re-fit this after changing a cove factor, or overall
        # alert volume moves. Fitted 2026-08-10 to ~2.5 qualifying windows/week.
        # Re-fitted 2026-08-10 AFTER the off-the-hour bug fix (see floor_hour):
        # the previous fit was made against corrupted features and was ~1 point
        # too generous. MEASURED on the full pipeline: 24% of windows >=7
        # (~3.4/week), 7% >=8. Note the offline sweep under-predicts because it
        # assumes a default wind penalty where the real run computes actual wind.
        # UNRESOLVED: Jun-Jul 2026 score near-zero while Aug scores 62%. That is
        # real south-swell persistence in the data, not obviously a model fault,
        # but no global curve separates them and there is no ground truth yet.
        # This is the #1 thing the dive log should settle. Easing south
        # exposure alone could NOT fix summer — Mar-Aug are all south-facing,
        # so scaling that band scales everything. Summer scores lower because
        # it genuinely had bigger surf (3.3ft mean vs 2.2ft in March).
        "surf_penalty": [(0, 0.0), (1.0, 0.3), (3.1, 1.6), (6.2, 3.2), (12.5, 5.0),
                         (25, 7.2), (52, 9.0)],
        # decayed sum of ft^2*s over 72h -> penalty (stirred-up water memory).
        "turbidity_penalty": [(0, 0.0), (1000, 0.3), (3000, 1.0), (8000, 2.5), (15000, 4.0)],
        # decayed sum of kn^2 over 48h -> penalty (chop/mixing history).
        "wind_hist_penalty": [(0, 0.0), (400, 0.3), (2200, 1.5), (5000, 3.0), (9000, 4.5), (15000, 6.0)],
        # max sustained kn during the window -> penalty (surface texture on the day).
        "wind_now_penalty": [(0, 0.0), (5, 0.1), (8, 0.5), (10, 1.0), (12, 1.8), (15, 3.0)],
        # hard rules
        "rain_cap": 4.0,            # precip within 72h pre-window
        "creek_rain_cap": 3.0,      # creek_adjacent sites, 96h window
        "wind_cap_kt": 15.0,        # forecast sustained wind during window
        "wind_cap_score": 5.0,
        # chlorophyll bloom: confidence knock only, never a score change.
        "chla_bloom_mg_m3": 2.0,    # CALIBRATION: if green-water days slip through, lower this
        # Kd490 is still FETCHED and LOGGED, but no longer scores. Measured
        # 2026-08-11 over 265 days: water clarity decorrelates from itself in
        # ~3 days, and at the ~10-day lag this product actually arrives the
        # autocorrelation is r=0.018 — no information. Scoring on it was fake
        # precision. It stays in the log because it is the only direct clarity
        # measurement available and backtesting may yet find a use for it.
        "kd490_stale_days": 21,
        # WIND DIRECTION. A knot of offshore wind is not a knot of onshore
        # wind: offshore (Santa Ana) flattens the nearshore and blows chop out
        # to sea; onshore sea breeze builds it. Factor on wind speed by angular
        # distance from the zone's offshore_dir before any wind penalty.
        # CALIBRATION: if a Santa Ana morning still scored low, lower the 0-60
        # values; if an offshore day was rougher than scored, raise them.
        "wind_dir_factor": [(0, 0.35), (60, 0.55), (90, 0.80), (120, 1.0), (180, 1.0)],
        # FIRST FLUSH. The first rain after the long dry season carries a
        # summer of oil and grime to the ocean in one pulse — cap harder than
        # ordinary post-rain. Live runs only see ~4 days of precip history, so
        # season stands in for true antecedent dryness: any May-Oct rain in
        # SoCal is first-flush-ish. CALIBRATION: replace with a real antecedent
        # dry-day counter when the dive log shows season alone mis-fires.
        "first_flush_months": [5, 6, 7, 8, 9, 10],
        "first_flush_cap": 3.0,
        # SPRING TIDES mix the water column and drain entries hard; neaps are
        # kind. Daily tide range (ft, MLLW) -> small penalty. Newport neaps run
        # ~3-4ft, springs ~6-8ft. CALIBRATION: zero this if logged dives show
        # no tide-range effect at Laguna.
        "tide_range_penalty": [(3.5, 0.0), (5.0, 0.15), (6.5, 0.4), (8.0, 0.6)],
        # LIGHT. Cloud cover during the window, mean %. Unlike clarity this is
        # genuinely forecastable, and it is a huge part of how a dive FEELS —
        # sunbeams through kelp at 25ft are most of the magic, and an overcast
        # 25ft day reads like 15ft. Deliberately modest: gloom makes a good dive
        # worse, it does not make it bad.
        # CALIBRATION: if logged dives say overcast barely matters, flatten this.
        "cloud_penalty": [(0, 0.0), (40, 0.15), (70, 0.5), (90, 0.9), (100, 1.1)],
        # Multiplier on Open-Meteo model wave heights before damage math.
        # CALIBRATION: set from `python3 dive_alert.py validate` (model-vs-buoy
        # bias over the last 45 days); keep 1.0 unless bias exceeds ~10%.
        # 2026-08-10: 1100 matched hours vs buoy 46253 showed the model running
        # 11% light (buoy 3.14ft, model 2.78ft, r=0.86) → 1.13. Because damage
        # goes as height^2 this is a ~28% damage increase, so surf_penalty was
        # re-fit against the hindcast afterwards. Re-run validate each season;
        # if this drifts past ~1.25 the model has changed, not the ocean.
        "model_height_scale": 1.13,
    },

    # Flip to True the day carrier verification clears. Until then the site
    # must not invite anyone to text a number that cannot answer — a broken
    # promise on the signup page poisons a product built on kept ones.
    "sms_live": False,

    # ---- the conjunction gate: what "perfect" means --------------------
    # A weighted average lets one strong axis hide a fatal one — which is how
    # this model produced 9.0s on days the satellite said were murky. Perfection
    # is an AND, not a sum. EVERY condition below must hold; there is no partial
    # credit and no trading one off against another. Rare by construction.
    #
    # Note what is NOT here: visibility. It is unforecastable at 12-48h lead
    # (measured: swell score r=+0.07, SST r=-0.15, satellite decorrelates in 3d).
    # So the gate says "every knowable thing has lined up" — it positions you
    # for magic rather than predicting it, and the alert says so out loud.
    # Tuned against the full year 2025-08 → 2026-08: these thresholds yield 21
    # qualifying days (~2/month). Loosening to dmg 2.5 / cloud 40% / score 8.0
    # gives 47/yr, which is roughly weekly and stops feeling like an event.
    "perfect_gate": {
        "max_damage": 1.5,        # CALIBRATION: the calmest few % of windows
        "max_wind_kn": 7.0,       # glass, not merely light
        # NOTE: dry_hours saturates at features.rain_lookback_h (72), so this
        # must never exceed it — asking for 96 made the gate unreachable and it
        # silently never fired. If you want a longer dry requirement, raise
        # rain_lookback_h too.
        "min_dry_hours": 72,      # no rain anywhere in the lookback
        "max_cloud_pct": 25,      # sun on the kelp
        "min_sst_c": 16.5,        # not punishing
        "min_score": 8.5,         # the weighted score must agree too
    },

    # ---- alerting -----------------------------------------------------
    "alerting": {
        "threshold": 7.0,
        "lead_min_h": 12, "lead_max_h": 48,
        "material_change": 1.0,     # score move that re-alerts inside a streak
        # The weekly digest: a predictable ritual, sent whether or not anything
        # clears the threshold. Wednesday because forecast skill at 3-4 days is
        # solid, so the coming weekend is genuinely readable — a Monday digest
        # would be guessing about Saturday. 0=Mon .. 6=Sun.
        "digest_weekday": 2,
        "digest_days": 7,
    },

    # ---- voice: template strings, editable without touching logic -----
    "voice": {
        "tier1_hooks": ["Solid window", "Worth the drive", "Quietly good",
                        "Sneaky good morning", "Easy yes"],
        "tier2_hooks": ["Laguna's firing", "This is the one", "Drop your plans",
                        "Don't sleep on this one", "The coves are calling"],
        # Keep hooks dash-free: the title already joins with an em dash.
        "tier3_hooks": ["Rare air", "Clear the morning", "Cancel everything"],
        "downgrade": "{label} slipped — {old:.1f} → {new:.1f}. {why} Skip it.",
        "emoji": "🤿",
        # score -> plain-English viz expectation, straight off the anchored scale
        "viz_expect": [(9.0, "25ft+ viz"), (8.0, "15–20ft viz"), (7.0, "12–15ft viz"),
                       (5.0, "8–10ft viz"), (0.0, "single-digit viz")],
        # What the SETUP will feel like. Note these describe water state and
        # effort, never visibility — viz is not forecastable at this lead time
        # and promising it is how a tool like this loses its credibility.
        "setup_feel": [
            (8.0, "flat, easy, nothing in the way"),
            (7.0, "clean water and a relaxed entry"),
            (5.0, "workable, a bit of texture"),
            (0.0, "washing machine"),
        ],
        # Said out loud on every alert. The honest core of the product: we can
        # call the setup, nobody can call the water. Must match the site's vow.
        "wildcard": "The water keeps the last card — nobody can forecast that.",
        # Only ever used when the conjunction gate passes. This is the phrase
        # the website turns gold for; phone and site must say the same words.
        "perfect_hook": "The bell is ringing",
        "perfect_line": ("Flat, dry, sunny, warm — every knowable thing has lined up. "
                         "The water's the only unknown left, and this is the day to gamble."),
        # Gate days replace the tip with an invite: the ~21-a-year message is
        # the one that gets screenshotted into group chats, so it carries its
        # own way in. Scarcity is the growth engine — this line must NEVER
        # appear on an ordinary alert.
        "gate_share_line": "Forwarded this? The bell lives at thedivebell.com",
        # First words a new subscriber ever hears. The site's cadence — short
        # declaratives, every line carrying a fact — compressed to a phone
        # screen. The program is all here: cadence, threshold, lead time,
        # ring rate, and the one question the buttons ask.
        "welcome_title": "You're on the bell 🤿",
        "welcome_body": ("It reads the water twice a day — swell, wind, rain, tide, "
                         "light — every dawn and dusk on Laguna's coves, scored out of 10.\n"
                         "Most mornings it says nothing. That's the discipline.\n"
                         "Wednesdays at 7am it reads you the week. When a window's worth "
                         "the drive, you get a day or two of warning. And a dozen mornings "
                         "a year — flat, glass, dry, sun, warm, all at once — the ocean "
                         "says yes, and it rings. Twenty-one in a good year; an El Niño "
                         "winter might allow five.\n"
                         "When you surface, tell the bell what the water gave you:\n"
                         "👀 the whole reef · 🙂 your buddy · 🌫 just your fins"),
        # SST °F -> what wetsuit to throw in the car
        "wetsuit": [(58, "5mm-and-hood water"), (64, "solid 4/3 water"),
                    (70, "comfy 4/3 water"), (999, "spring-suit warm")],
        # Feedback buttons (max 3 — ntfy limit). Each is a VERDICT in the same
        # visual language the alerts promise in ("you should see your buddy
        # across the reef") — a diver answers instantly, no numbers to guess.
        # FEEDBACK_MAP translates each verdict to viz-in-feet for the backtest.
        "fb_buttons": [("👀 Saw it all", "clear"), ("🙂 Saw my buddy", "fair"),
                       ("🌫 Saw my fins", "murk")],
    },

    # ---- tip library --------------------------------------------------
    # Tags: viz (any|high), surge (any|low), tide (any|low|mid|high), season
    # (any|summer|winter), lane (sightsee|photo|skills|entry). No 'take' lane
    # exists for Zone A and none may be added while take_allowed is False.
    # These are drafting placeholders showing the tagging pattern — edit the words.
    "tips": {
        "Crescent Bay": [
            {"id": "cb1", "viz": "high", "surge": "any", "tide": "any", "season": "any", "lane": "sightsee",
             "text": "Work the Seal Rock wall on the north end — best structure in the cove."},
            {"id": "cb2", "viz": "any", "surge": "low", "tide": "any", "season": "summer", "lane": "photo",
             "text": "Morning sun angles light the sand channel — bring the wide lens."},
            {"id": "cb3", "viz": "any", "surge": "any", "tide": "low", "season": "any", "lane": "entry",
             "text": "At low tide enter mid-beach; the flanks get rocky."},
            {"id": "cb4", "viz": "any", "surge": "any", "tide": "any", "season": "any", "lane": "skills",
             "text": "Long sand run-out — good day to drill compass legs to Seal Rock and back."},
        ],
        "Shaw's Cove": [
            {"id": "sh1", "viz": "high", "surge": "low", "tide": "mid", "season": "any", "lane": "sightsee",
             "text": "The west-reef crevice is the show — go at slack, single file."},
            {"id": "sh2", "viz": "any", "surge": "any", "tide": "any", "season": "any", "lane": "skills",
             "text": "Most protected entry in Laguna — right day to bring a new buddy."},
            {"id": "sh3", "viz": "high", "surge": "any", "tide": "any", "season": "winter", "lane": "photo",
             "text": "Winter viz lights up the east reef fingers — shoot into the sun for silhouettes."},
            {"id": "sh4", "viz": "any", "surge": "low", "tide": "high", "season": "any", "lane": "sightsee",
             "text": "High tide floods the inner reef flats — octopus hunt over the shallows."},
        ],
        "Divers Cove": [
            {"id": "dv1", "viz": "any", "surge": "any", "tide": "any", "season": "any", "lane": "sightsee",
             "text": "Straight out 100 yd to the kelp line — garibaldi nests on the inner rocks."},
            {"id": "dv2", "viz": "high", "surge": "low", "tide": "any", "season": "summer", "lane": "photo",
             "text": "Kelp canopy light shafts mid-morning — meter for the beams, not the fish."},
            {"id": "dv3", "viz": "any", "surge": "any", "tide": "low", "season": "any", "lane": "entry",
             "text": "Low tide exposes rocks on both flanks — enter dead center."},
        ],
        "Fisherman's Cove": [
            {"id": "fi1", "viz": "any", "surge": "low", "tide": "mid", "season": "any", "lane": "sightsee",
             "text": "The channel opens onto a boulder garden at 20 ft — circle it slowly."},
            {"id": "fi2", "viz": "any", "surge": "any", "tide": "high", "season": "any", "lane": "entry",
             "text": "Ride the channel out at high tide; it narrows to a grabby slot when it drains."},
            {"id": "fi3", "viz": "high", "surge": "low", "tide": "any", "season": "any", "lane": "photo",
             "text": "Macro day: the shelf edges hold nudibranchs — go slow on the south wall."},
        ],
        "Picnic Beach (Heisler)": [
            {"id": "pi1", "viz": "any", "surge": "low", "tide": "high", "season": "any", "lane": "sightsee",
             "text": "Best close-in reef in the zone — most life inside the first 100 yd."},
            {"id": "pi2", "viz": "any", "surge": "any", "tide": "high", "season": "any", "lane": "entry",
             "text": "Enter north of the point over sand, not the shelf — save your ankles."},
            {"id": "pi3", "viz": "any", "surge": "any", "tide": "any", "season": "any", "lane": "skills",
             "text": "Shallow shelf, easy nav — good site to practice SMB deployment."},
        ],
        "Wood's Cove": [
            {"id": "wo1", "viz": "high", "surge": "low", "tide": "mid", "season": "any", "lane": "sightsee",
             "text": "The boulder field south of the stairs stacks sheephead — work it in a grid."},
            {"id": "wo2", "viz": "any", "surge": "any", "tide": "any", "season": "any", "lane": "entry",
             "text": "Time the shorebreak slot between sets; fins on past the outcrop."},
            {"id": "wo3", "viz": "any", "surge": "low", "tide": "any", "season": "winter", "lane": "photo",
             "text": "Winter storms rearrange the sand — new ledges show up; hunt fresh exposures."},
        ],
        "Salt Creek": [
            {"id": "sc1", "viz": "any", "surge": "low", "tide": "any", "season": "any", "lane": "sightsee",
             "text": "Work the reef fingers off the point — the structure runs deeper than it looks."},
            {"id": "sc2", "viz": "any", "surge": "any", "tide": "low", "season": "any", "lane": "entry",
             "text": "Long walk, soft sand — stage your gear high and time the shorebreak."},
            {"id": "sc3", "viz": "high", "surge": "low", "tide": "any", "season": "winter", "lane": "photo",
             "text": "Winter sun on the headland wall — shoot the ledges side-lit, morning only."},
        ],
        "Dana Point Harbor breakwall (outside)": [
            {"id": "db1", "viz": "any", "surge": "low", "tide": "any", "season": "any", "lane": "sightsee",
             "text": "The outer rocks stack bass and lobster-watchers — follow the wall, don't cross it."},
            {"id": "db2", "viz": "any", "surge": "any", "tide": "any", "season": "any", "lane": "skills",
             "text": "Boat traffic overhead — fly a flag, surface close to the wall, listen up."},
        ],
        "Cleo Street barge": [
            {"id": "cl1", "viz": "high", "surge": "low", "tide": "any", "season": "any", "lane": "sightsee",
             "text": "Line up the lifeguard tower and swim the 150 yd — the barge ribs hold bass."},
            {"id": "cl2", "viz": "any", "surge": "low", "tide": "any", "season": "any", "lane": "skills",
             "text": "Flat day only — practice the surface swim with a dive float and flag."},
            {"id": "cl3", "viz": "high", "surge": "low", "tide": "any", "season": "summer", "lane": "photo",
             "text": "The wreck at 25 ft gets full sun before 10am — wide angle, shoot up the ribs."},
        ],
    },

    # ---- data sources (all verified with live requests 2026-08-10) ----
    "sources": {
        # CDIP ERDDAP carries no MOP alongshore data (buoy aggregations only),
        # so per the resolution order the swell backbone is Open-Meteo Marine
        # with NDBC nearshore/offshore buoys for observed truth + agreement.
        "marine": "https://marine-api.open-meteo.com/v1/marine",
        "weather": "https://api.open-meteo.com/v1/forecast",
        "weather_archive": "https://archive-api.open-meteo.com/v1/archive",
        "ndbc_primary": "46253",   # San Pedro South (CDIP 213) — nearest live buoy; 46223 Dana Point is decommissioned
        "ndbc_offshore": "46086",  # San Clemente Basin — offshore trend
        "ndbc_url": "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt",
        "tide_station": "9410580",  # Newport Bay Entrance — closest CO-OPS station to Laguna
        "tides": "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
        "sst": "https://coastwatch.pfeg.noaa.gov/erddap/griddap/jplMURSST41.json",
        # Endpoint verified by identity 2026-08-10 but the server was slow;
        # fetch is best-effort with a short timeout and degrades gracefully.
        "chla": "https://coastwatch.noaa.gov/erddap/griddap/noaacwNPPVIIRSSQchlaWeekly.json",
        # Daily science-quality light attenuation; verified live 2026-08-10.
        "kd490": "https://coastwatch.pfeg.noaa.gov/erddap/griddap/nesdisVHNSQkd490Daily.json",
        "ntfy": "https://ntfy.sh",
    },
}

# =====================================================================
# small utilities
# =====================================================================

def now_pt() -> datetime:
    return datetime.now(tz=PT).replace(minute=0, second=0, microsecond=0)


def sentence_case(s: str) -> str:
    """Uppercase the first letter only. str.capitalize() lowercases the rest,
    which turns 'best since February' into 'best since february' and
    'a mellow SSW swell' into 'ssw'."""
    return s[:1].upper() + s[1:] if s else s


def piecewise(x: float, points) -> float:
    """Piecewise-linear interpolation over [(x, y), ...]; clamps at the ends."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def compass(deg: float) -> str:
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((deg + 11.25) % 360 // 22.5)]


def m_to_ft(m: float) -> float:
    return m * 3.28084


def parse_local(s: str, tz=None) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=tz or PT)


def hourly_map(times, vals, tz=None):
    """{aware datetime -> float} skipping nulls. tz: the zone's own clock —
    a Hawaii bell must speak Hawaii time or 'in by 9:12am' is a lie."""
    out = {}
    for t, v in zip(times, vals):
        if v is None:
            continue
        out[parse_local(t, tz)] = float(v)
    return out


def zone_tz(zone_cfg):
    return ZoneInfo(zone_cfg.get("tz", "America/Los_Angeles")) if zone_cfg else PT


# =====================================================================
# fetch layer — every source independently wrapped; offline mode reads
# the same shapes from fixtures/<name>/.
# =====================================================================

class Fetch:
    def __init__(self, name, ok, data=None, error=None):
        self.name, self.ok, self.data, self.error = name, ok, data, error

    def __repr__(self):
        return "Fetch(%s ok=%s%s)" % (self.name, self.ok, "" if self.ok else " err=%s" % self.error)


def http_get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "dive-alert/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


class Sources:
    """Live or fixture-backed raw responses. Raw text in, parsing downstream."""

    def __init__(self, offline=False, fixture_set="normal"):
        self.offline = offline
        self.dir = os.path.join(FIXTURES, fixture_set)
        self.recorded = {}

    def get(self, key: str, url: str, timeout: int = 25) -> str:
        if self.offline:
            path = os.path.join(self.dir, key + (".json" if "json" in url or "format=json" in url else ".txt"))
            for ext in ("", ".json", ".txt"):
                p = os.path.join(self.dir, key + ext)
                if os.path.exists(p):
                    with open(p) as f:
                        return f.read()
            raise FileNotFoundError("fixture missing: %s (%s)" % (key, path))
        body = http_get(url, timeout)
        self.recorded[key] = body
        return body

    def fixture_now(self):
        meta = os.path.join(self.dir, "meta.json")
        if self.offline and os.path.exists(meta):
            try:
                with open(meta) as f:
                    return datetime.fromisoformat(json.load(f)["now"]).astimezone(PT)
            except (ValueError, KeyError, json.JSONDecodeError):
                return None  # corrupt meta must degrade, not crash the run
        return None


def fetch_marine(src: Sources, lat, lon, tzname="America/Los_Angeles") -> Fetch:
    try:
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "hourly": "wave_height,wave_period,wave_direction,"
                      "swell_wave_height,swell_wave_period,swell_wave_direction,"
                      "wind_wave_height,wind_wave_period,wind_wave_direction",
            "past_days": 4, "forecast_days": 8, "timezone": tzname})
        d = json.loads(src.get("marine", CONFIG["sources"]["marine"] + "?" + q))
        h = d["hourly"]
        tz = ZoneInfo(tzname)
        data = {k: hourly_map(h["time"], h[k], tz) for k in h if k != "time"}
        return Fetch("marine", True, data)
    except Exception as e:
        return Fetch("marine", False, error=str(e))


def fetch_weather(src: Sources, lat, lon, tzname="America/Los_Angeles") -> Fetch:
    try:
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation,cloud_cover",
            "daily": "sunrise,sunset",
            "wind_speed_unit": "kn", "precipitation_unit": "inch",
            "past_days": 4, "forecast_days": 8, "timezone": tzname})
        d = json.loads(src.get("weather", CONFIG["sources"]["weather"] + "?" + q))
        h = d["hourly"]
        tz = ZoneInfo(tzname)
        data = {k: hourly_map(h["time"], h[k], tz) for k in h if k != "time"}
        data["sunrise"] = [parse_local(t, tz) for t in d["daily"]["sunrise"]]
        data["sunset"] = [parse_local(t, tz) for t in d["daily"]["sunset"]]
        return Fetch("weather", True, data)
    except Exception as e:
        return Fetch("weather", False, error=str(e))


def parse_ndbc(text: str):
    """NDBC realtime2 .txt -> latest rows of (dt_utc, WVHT_m, DPD_s, MWD_deg)."""
    rows = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        p = line.split()
        if len(p) < 12:
            continue
        try:
            dt = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=timezone.utc)
        except ValueError:
            continue
        def num(s):
            return None if s == "MM" else float(s)
        rows.append({"t": dt, "wvht_m": num(p[8]), "dpd_s": num(p[9]), "mwd_deg": num(p[11])})
    return rows


def fetch_ndbc(src: Sources, station: str, key: str) -> Fetch:
    if station in (None, "none"):
        return Fetch(key, False, error="no buoy for this water")
    try:
        url = CONFIG["sources"]["ndbc_url"].format(station=station)
        rows = parse_ndbc(src.get(key, url, timeout=20))
        good = [r for r in rows if r["wvht_m"] is not None]
        if not good:
            return Fetch(key, False, error="no valid rows")
        return Fetch(key, True, {"rows": rows, "latest": good[0], "station": station})
    except Exception as e:
        return Fetch(key, False, error=str(e))


def fetch_tides(src: Sources, begin: datetime, station=None, tz=None) -> Fetch:
    # station "none" is a legitimate Phase-3 state (tideless international
    # coasts): the tide term and entry-timing FYI degrade gracefully.
    station = station or CONFIG["sources"]["tide_station"]
    if station in (None, "none"):
        return Fetch("tides", False, error="no tide station for this water")
    try:
        q = urllib.parse.urlencode({
            "product": "predictions", "application": "dive_alert",
            "begin_date": begin.strftime("%Y%m%d"),
            # 8 days, not 4: the weekly digest reads a full week ahead, and a
            # short tide fetch silently degrades its far days to "steady tide"
            "end_date": (begin + timedelta(days=8)).strftime("%Y%m%d"),
            "datum": "MLLW", "station": station,
            "time_zone": "lst_ldt", "units": "english", "interval": "hilo", "format": "json"})
        d = json.loads(src.get("tides", CONFIG["sources"]["tides"] + "?" + q))
        ev = [{"t": datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=tz or PT),
               "ft": float(p["v"]), "type": p["type"]} for p in d["predictions"]]
        return Fetch("tides", True, ev)
    except Exception as e:
        return Fetch("tides", False, error=str(e))


def _grid_mean(d):
    vals = [r[-1] for r in d["table"]["rows"] if r[-1] is not None]
    return sum(vals) / len(vals) if vals else None


def fetch_sst(src: Sources, lat, lon) -> Fetch:
    # Nearshore pixels are sometimes masked in the newest slice; sample a
    # small box and step back one slice before giving up.
    err = None
    for tsel in ("last", "last-1"):
        try:
            u = (CONFIG["sources"]["sst"] +
                 "?analysed_sst%%5B(%s)%%5D%%5B(%.2f):(%.2f)%%5D%%5B(%.2f):(%.2f)%%5D"
                 % (tsel, lat - 0.03, lat + 0.03, lon - 0.05, lon + 0.02))
            v = _grid_mean(json.loads(src.get("sst_%s" % tsel, u, timeout=30)))
            if v is not None:
                return Fetch("sst", True, {"c": v})
            err = "all pixels empty (%s)" % tsel
        except Exception as e:
            err = str(e)
    return Fetch("sst", False, error=err)


def fetch_chla(src: Sources, lat, lon) -> Fetch:
    err = None
    for tsel in ("last", "last-1"):
        try:
            u = (CONFIG["sources"]["chla"] +
                 "?chlor_a%%5B(%s)%%5D%%5B(0.0)%%5D%%5B(%.2f):(%.2f)%%5D%%5B(%.2f):(%.2f)%%5D"
                 % (tsel, lat - 0.06, lat + 0.06, lon - 0.12, lon + 0.03))
            v = _grid_mean(json.loads(src.get("chla_%s" % tsel, u, timeout=20)))
            if v is not None:
                return Fetch("chla", True, {"mg_m3": v})
            err = "all pixels empty (%s)" % tsel
        except Exception as e:
            err = str(e)
    return Fetch("chla", False, error=err)


def fetch_kd490(src: Sources, lat, lon) -> Fetch:
    """Freshest non-null daily Kd490 near the zone; stale (>N days) = missing,
    because scoring on month-old water clarity is worse than not scoring it."""
    try:
        begin = (datetime.now(timezone.utc)
                 - timedelta(days=CONFIG["scoring"]["kd490_stale_days"] + 6)
                 ).strftime("%Y-%m-%dT00:00:00Z")
        u = (CONFIG["sources"]["kd490"] +
             "?kd_490%%5B(%s):(last)%%5D%%5B(0.0)%%5D%%5B(%.2f):(%.2f)%%5D%%5B(%.2f):(%.2f)%%5D"
             % (begin, lat - 0.06, lat + 0.06, lon - 0.12, lon + 0.03))
        d = json.loads(src.get("kd490", u, timeout=30))
        by_time = {}
        for r in d["table"]["rows"]:
            if r[-1] is not None:
                by_time.setdefault(r[0], []).append(r[-1])
        if not by_time:
            return Fetch("kd490", False, error="all pixels empty in last 16d")
        latest = max(by_time)
        age_d = (datetime.now(timezone.utc)
                 - datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
        if age_d > CONFIG["scoring"]["kd490_stale_days"]:
            return Fetch("kd490", False, error="stale (%dd old)" % age_d)
        vals = by_time[latest]
        return Fetch("kd490", True, {"m1": sum(vals) / len(vals), "age_d": age_d})
    except Exception as e:
        return Fetch("kd490", False, error=str(e))


def parse_ndbc_spec(text: str):
    """NDBC .spec file: observed swell/wind-wave SEPARATION from the buoy.
    Cols: YY MM DD hh mm WVHT SwH SwP WWH WWP SwD WWD STEEPNESS APD MWD."""
    rows = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        p = line.split()
        if len(p) < 12:
            continue
        try:
            dt = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=timezone.utc)
        except ValueError:
            continue
        def num(s):
            return None if s in ("MM", "N/A") else float(s)
        rows.append({"t": dt, "swh_m": num(p[6]), "swp_s": num(p[7]),
                     "wwh_m": num(p[8]), "wwp_s": num(p[9])})
    return rows


def fetch_ndbc_spec(src: Sources, station: str) -> Fetch:
    try:
        url = "https://www.ndbc.noaa.gov/data/realtime2/%s.spec" % station
        rows = parse_ndbc_spec(src.get("ndbc_spec", url, timeout=20))
        good = [r for r in rows if r["wwh_m"] is not None or r["swh_m"] is not None]
        if not good:
            return Fetch("ndbc_spec", False, error="no valid rows")
        return Fetch("ndbc_spec", True, {"latest": good[0], "station": station})
    except Exception as e:
        return Fetch("ndbc_spec", False, error=str(e))


def fetch_all(zone_cfg, offline=False, fixture_set="normal"):
    src = Sources(offline, fixture_set)
    lat, lon = zone_cfg["lat"], zone_cfg["lon"]
    buoy = zone_cfg.get("buoy", CONFIG["sources"]["ndbc_primary"])
    buoy_off = zone_cfg.get("buoy_offshore", CONFIG["sources"]["ndbc_offshore"])
    tzname = zone_cfg.get("tz", "America/Los_Angeles")
    f = {
        "marine": fetch_marine(src, lat, lon, tzname),
        "weather": fetch_weather(src, lat, lon, tzname),
        "ndbc_primary": fetch_ndbc(src, buoy, "ndbc_primary"),
        "ndbc_offshore": fetch_ndbc(src, buoy_off, "ndbc_offshore"),
        "tides": fetch_tides(src, (src.fixture_now() or now_pt()),
                             zone_cfg.get("tide_station"), ZoneInfo(tzname)),
        "sst": fetch_sst(src, lat, lon),
        "chla": fetch_chla(src, lat, lon),
        "kd490": fetch_kd490(src, lat, lon),
        # observed swell/chop split — feeds agreement only, so it is
        # deliberately NOT in SOURCE_WEIGHTS (losing it must not dent
        # completeness; it's a bonus check, not a pipeline input)
        "ndbc_spec": fetch_ndbc_spec(src, buoy),
    }
    return f, src


# =====================================================================
# windows
# =====================================================================

def build_windows(weather: Fetch, t_now: datetime, zone_cfg, horizon_h=72):
    """Dawn/dusk windows starting within the next 72h. Night windows only for
    take-legal zones in lobster season (dormant while only Zone A is live)."""
    wins = []
    if weather.ok:
        sunrises, sunsets = weather.data["sunrise"], weather.data["sunset"]
    else:
        # degraded: fixed approximations, flagged via weather.ok upstream
        base = t_now.replace(hour=6, minute=0)
        sunrises = [base + timedelta(days=i) for i in range(9)]
        sunsets = [base.replace(hour=19, minute=30) + timedelta(days=i) for i in range(9)]
    wc = CONFIG["windows"]
    horizon = t_now + timedelta(hours=horizon_h)
    for sr, ss in zip(sunrises, sunsets):
        dawn_start = sr - timedelta(minutes=wc["dawn_first_light_offset_min"])
        dusk_start = ss - timedelta(hours=wc["dusk_hours"])
        for kind, start, hours in (("dawn", dawn_start, wc["dawn_hours"]),
                                   ("dusk", dusk_start, wc["dusk_hours"])):
            if t_now < start <= horizon:
                wins.append({
                    "kind": kind, "start": start, "end": start + timedelta(hours=hours),
                    "label": "%s %s" % (start.strftime("%a"), kind),
                    "key": "%s-%s" % (start.strftime("%Y%m%dT%H%M"), kind),
                })
        if zone_takes_allowed(zone_cfg) and in_lobster_season(sr):
            night_start = ss + timedelta(hours=wc["night_start_after_sunset_h"])
            if t_now < night_start <= horizon:
                wins.append({"kind": "night", "start": night_start,
                             "end": night_start + timedelta(hours=wc["night_hours"]),
                             "label": "%s night" % night_start.strftime("%a"),
                             "key": "%s-night" % night_start.strftime("%Y%m%dT%H%M")})
    wins.sort(key=lambda w: w["start"])
    return wins


def zone_takes_allowed(zone_cfg) -> bool:
    return any(s["take_allowed"] for s in zone_cfg["sites"])


def in_lobster_season(dt: datetime) -> bool:
    ls = CONFIG["windows"]["lobster_season"]
    m = dt.month
    return m >= ls["open_month"] or m <= ls["close_month"]


def moon_phase_frac(dt: datetime) -> float:
    """0=new, 0.5=full. Dormant: only surfaces in take-legal night windows."""
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)  # known new moon
    days = (dt.astimezone(timezone.utc) - ref).total_seconds() / 86400.0
    return (days % 29.530588) / 29.530588


# =====================================================================
# features — everything is relative to WINDOW START, not run time.
# History up to now is observed; now -> window start is forecast; same
# decay math throughout. obs_frac feeds confidence.
# =====================================================================

def floor_hour(dt: datetime) -> datetime:
    """Snap to the top of the hour. Hourly series are keyed on the hour, but
    window starts come off sunrise/sunset and land on :30, :47 etc. Stepping in
    whole hours from an offset start makes EVERY lookup miss and silently
    return zero — which is how the rain rule and both energy features died
    against live data while on-the-hour test fixtures kept passing."""
    return dt.replace(minute=0, second=0, microsecond=0)


def decayed_sum(series, start: datetime, lookback_h: int, half_life_h: float, fn):
    total, n_obs, n_all = 0.0, 0, 0
    start = floor_hour(start)
    t = start - timedelta(hours=lookback_h)
    while t < start:
        v = series.get(t)
        if v is not None:
            age_h = (start - t).total_seconds() / 3600.0
            total += (0.5 ** (age_h / half_life_h)) * fn(v)
            n_all += 1
        t += timedelta(hours=1)
    return total, n_all


def obs_fraction(start: datetime, lookback_h: int, t_now: datetime) -> float:
    """Share of the lookback that is observed history (vs forecast)."""
    lo = start - timedelta(hours=lookback_h)
    if t_now >= start:
        return 1.0
    if t_now <= lo:
        return 0.0
    return (t_now - lo).total_seconds() / (start - lo).total_seconds()


def dry_hours(precip, start: datetime, lookback_h: int, thresh_in: float):
    """Hours from the last rain event (rolling 24h sum >= thresh) to window start."""
    last_wet = None
    start = floor_hour(start)
    t = start - timedelta(hours=lookback_h)
    while t < start:
        s = 0.0
        for k in range(24):
            s += precip.get(t - timedelta(hours=k), 0.0)
        if s >= thresh_in:
            last_wet = t
        t += timedelta(hours=1)
    if last_wet is None:
        return float(lookback_h)
    return (start - last_wet).total_seconds() / 3600.0


def window_hours(w):
    t = w["start"].replace(minute=0)
    out = []
    while t < w["end"]:
        out.append(t)
        t += timedelta(hours=1)
    return out or [w["start"].replace(minute=0)]


def swell_damage(marine, w, exposure_map, cove_factor=1.0, h_scale=1.0):
    """Mean over window hours of h_ft^2 * period_factor * exposure, swell and
    wind-wave partitions scored jointly then summed — height, period and
    direction always interact, never scored independently."""
    pf = CONFIG["scoring"]["period_factor"]
    hours = window_hours(w)
    tot, cnt, parts = 0.0, 0, {"hgt_ft": 0.0, "per_s": 0.0, "dir": 0.0}
    for t in hours:
        d = 0.0
        sh = marine.get("swell_wave_height", {}).get(t)
        sp = marine.get("swell_wave_period", {}).get(t)
        sd = marine.get("swell_wave_direction", {}).get(t)
        scale = CONFIG["scoring"]["model_height_scale"] * h_scale
        if sh is not None and sp is not None and sd is not None:
            d += (m_to_ft(sh) * scale) ** 2 * piecewise(sp, pf) * piecewise(sd % 360, exposure_map)
            parts["hgt_ft"] += m_to_ft(sh) * scale; parts["per_s"] += sp; parts["dir"] += sd
        wh = marine.get("wind_wave_height", {}).get(t)
        wp = marine.get("wind_wave_period", {}).get(t)
        wd = marine.get("wind_wave_direction", {}).get(t)
        if wh is not None and wp is not None and wd is not None:
            d += (m_to_ft(wh) * scale) ** 2 * piecewise(max(wp, 3.0), pf) * piecewise(wd % 360, exposure_map)
        tot += d; cnt += 1
    if cnt == 0:
        return None, parts
    for k in parts:
        parts[k] /= cnt
    return (tot / cnt) * cove_factor, parts


def wind_dir_factor(wdir, zone_cfg):
    """A knot of offshore wind is not a knot of onshore wind. Scale wind speed
    by angular distance from the zone's offshore direction before penalizing."""
    off = zone_cfg.get("offshore_dir", 45)
    ang = abs((wdir - off) % 360)
    ang = min(ang, 360 - ang)
    return piecewise(ang, CONFIG["scoring"]["wind_dir_factor"])


def tide_range_ft(tides, w):
    """Daily tide swing around the window — spring tides mix and drain hard."""
    lo, hi = w["start"] - timedelta(hours=18), w["start"] + timedelta(hours=18)
    fts = [e["ft"] for e in (tides or []) if lo <= e["t"] <= hi]
    return (max(fts) - min(fts)) if len(fts) >= 2 else None


def buoy_anchor(fetches, t_now):
    """Rolling correction of the model against the zone's own buoy: median of
    obs/model height over the last 48h of matched hours. The casting scale
    says how this GEOGRAPHY differs from the model; the anchor says how THIS
    WEEK'S SWELL differs. Clamped hard (0.6-1.6) and defaulting to 1.0 on any
    doubt — a correction must never be able to do more damage than the error
    it corrects. Absent from hindcasts by necessity (no archived realtime
    buoy): a live-accuracy layer, centered on 1.0 by construction."""
    mp, ma = fetches.get("ndbc_primary"), fetches.get("marine")
    if not (mp and mp.ok and ma and ma.ok):
        return 1.0
    model = ma.data.get("wave_height", {})
    ratios = []
    for r in mp.data.get("rows", []):
        if r["wvht_m"] is None:
            continue
        hr = r["t"].astimezone(PT).replace(minute=0, second=0, microsecond=0)
        if (t_now - hr).total_seconds() > 48 * 3600:
            continue
        mv = model.get(hr)
        if mv and mv > 0.15:
            ratios.append(r["wvht_m"] / mv)
    if len(ratios) < 12:
        return 1.0
    ratios.sort()
    return round(max(0.6, min(1.6, ratios[len(ratios) // 2])), 3)


def compute_features(w, fetches, zone_cfg, t_now, anchor=1.0):
    fc = CONFIG["features"]
    marine = fetches["marine"].data if fetches["marine"].ok else {}
    wx = fetches["weather"].data if fetches["weather"].ok else {}
    wind = wx.get("wind_speed_10m", {})
    wdirs = wx.get("wind_direction_10m", {})
    precip = wx.get("precipitation", {})

    # direction-weighted wind: offshore hours count ~1/3 of onshore hours
    wind_eff = {t: v * wind_dir_factor(wdirs.get(t, 180.0), zone_cfg)
                for t, v in wind.items()}
    wind_e, _ = decayed_sum(wind_eff, w["start"], fc["wind_lookback_h"], fc["wind_half_life_h"], lambda v: v * v)
    swell_series = marine.get("wave_height", {})
    swell_per = marine.get("wave_period", {})

    def swell_fn_pair():
        total = 0.0
        t0h = floor_hour(w["start"])   # hour-snap (see floor_hour); NOT the buoy anchor
        t = t0h - timedelta(hours=fc["swell_lookback_h"])
        while t < t0h:
            h, p = swell_series.get(t), swell_per.get(t)
            if h is not None and p is not None:
                age = (t0h - t).total_seconds() / 3600.0
                hs = m_to_ft(h) * zone_cfg.get("marine_height_scale", 1.0) * anchor
                total += (0.5 ** (age / fc["swell_half_life_h"])) * (hs ** 2) * p
            t += timedelta(hours=1)
        return total

    swell_e = swell_fn_pair()
    dry = dry_hours(precip, w["start"], fc["rain_lookback_h"], fc["rain_threshold_in"]) if precip else None
    dmg, dmg_parts = swell_damage(marine, w, zone_cfg["exposure"],
                                  zone_cfg.get("cove_damage_factor", 1.0),
                                  zone_cfg.get("marine_height_scale", 1.0) * anchor)
    wind_in_window = [wind.get(t) for t in window_hours(w)]
    wind_in_window = [v for v in wind_in_window if v is not None]
    wind_eff_window = [wind_eff.get(t) for t in window_hours(w)]
    wind_eff_window = [v for v in wind_eff_window if v is not None]
    tides_f = fetches.get("tides")
    trange = tide_range_ft(tides_f.data if (tides_f is not None and tides_f.ok) else None, w)
    kd = fetches.get("kd490")
    clouds = [wx.get("cloud_cover", {}).get(t) for t in window_hours(w)]
    clouds = [c for c in clouds if c is not None]
    feats = {
        "cloud_pct": (sum(clouds) / len(clouds)) if clouds else None,
        "wind_energy_48h": wind_e if wind else None,
        "swell_energy_72h": swell_e if swell_series else None,
        "dry_hours": dry,
        "damage": dmg, "dmg_parts": dmg_parts,
        "kd490": kd.data["m1"] if (kd is not None and kd.ok) else None,
        "wind_window_max_kn": max(wind_in_window) if wind_in_window else None,
        "wind_window_eff_kn": max(wind_eff_window) if wind_eff_window else None,
        "tide_range_ft": trange,
        "month": w["start"].month,
        "first_flush_months": zone_cfg.get("first_flush_months"),
        "buoy_anchor": anchor,
        "obs_frac": (obs_fraction(w["start"], fc["wind_lookback_h"], t_now)
                     + obs_fraction(w["start"], fc["swell_lookback_h"], t_now)) / 2.0,
        "missing": [k for k, f in fetches.items() if not f.ok],
    }
    return feats


# =====================================================================
# scoring — weighted penalties + hard rules that override everything.
# =====================================================================

def score_window(feats, creek_adjacent=False):
    sc = CONFIG["scoring"]
    breakdown, flags = {}, []
    score = 10.0
    if feats["damage"] is not None:
        breakdown["surf"] = piecewise(feats["damage"], sc["surf_penalty"])
    else:
        breakdown["surf"] = 2.0
        flags.append("no swell data — assumed mid penalty")
    if feats["swell_energy_72h"] is not None:
        breakdown["turbidity"] = piecewise(feats["swell_energy_72h"], sc["turbidity_penalty"])
    else:
        breakdown["turbidity"] = 0.8
        flags.append("no swell history")
    if feats["wind_energy_48h"] is not None:
        breakdown["wind_hist"] = piecewise(feats["wind_energy_48h"], sc["wind_hist_penalty"])
    else:
        breakdown["wind_hist"] = 0.8
        flags.append("no wind history")
    # penalize the direction-weighted wind: 12kt offshore ~ 4kt onshore
    eff_wind = feats.get("wind_window_eff_kn", feats.get("wind_window_max_kn"))
    if eff_wind is not None:
        breakdown["wind_now"] = piecewise(eff_wind, sc["wind_now_penalty"])
    else:
        breakdown["wind_now"] = 0.5
        flags.append("no window wind forecast")
    if feats.get("tide_range_ft") is not None:
        breakdown["tide_mix"] = piecewise(feats["tide_range_ft"], sc["tide_range_penalty"])
    # light: forecastable, and a real part of how the dive feels
    if feats.get("cloud_pct") is not None:
        breakdown["light"] = piecewise(feats["cloud_pct"], sc["cloud_penalty"])
    score -= sum(breakdown.values())
    score = max(1.0, min(10.0, score))

    cap, cap_reason = None, None
    # hard rules override the weighted score (wind cap on the direction-
    # weighted speed: a 16kt Santa Ana flattens the coves, it doesn't cap them)
    if eff_wind is not None and eff_wind >= sc["wind_cap_kt"]:
        cap, cap_reason = sc["wind_cap_score"], "wind ≥%dkt in window" % sc["wind_cap_kt"]
    rain_lb = CONFIG["features"]["creek_rain_lookback_h"] if creek_adjacent else CONFIG["features"]["rain_lookback_h"]
    rain_cap = sc["creek_rain_cap"] if creek_adjacent else sc["rain_cap"]
    if feats["dry_hours"] is not None and feats["dry_hours"] < rain_lb:
        c = rain_cap
        # first flush: dry-season rain carries months of grime in one pulse
        ff_months = feats.get("first_flush_months")
        if ff_months is None:
            ff_months = sc["first_flush_months"]
        if feats.get("month") in ff_months:
            c = min(c, sc["first_flush_cap"])
            flags.append("first-flush")
        if cap is None or c < cap:
            cap, cap_reason = c, "post-rain (%.0fh since ≥%.1f\")" % (
                feats["dry_hours"], CONFIG["features"]["rain_threshold_in"])
        flags.append("post-rain")
    if cap is not None and score > cap:
        score = cap
    return round(score, 1), breakdown, cap_reason, flags


# =====================================================================
# confidence — completeness and agreement computed separately, reported
# as one word; material disagreement gets NAMED, never averaged away.
# =====================================================================

SOURCE_WEIGHTS = {"marine": 0.35, "weather": 0.25, "ndbc_primary": 0.15,
                  "ndbc_offshore": 0.05, "tides": 0.05, "sst": 0.05, "chla": 0.05,
                  "kd490": 0.05}


def confidence(fetches, feats, t_now):
    src_score = sum(w for k, w in SOURCE_WEIGHTS.items()
                    if k in fetches and fetches[k].ok)
    # CALIBRATION: the 50/50 blend makes a 40h-out window read "medium" even
    # with every source up — that is deliberate (mostly-forecast features).
    completeness = 0.5 * src_score + 0.5 * feats["obs_frac"]

    # Agreement needs two independent swell sources; without the buoy we can't
    # verify the model, so agreement is capped as "unverified", never assumed.
    agreement, disagree_note = 0.7, None
    mp = fetches.get("ndbc_primary")
    ma = fetches.get("marine")
    if mp and mp.ok and ma and ma.ok:
        latest = mp.data["latest"]
        t = latest["t"].astimezone(PT).replace(minute=0, second=0, microsecond=0)
        model = None
        for k in range(4):
            model = ma.data.get("wave_height", {}).get(t - timedelta(hours=k))
            if model is not None:
                break
        if model is not None and latest["wvht_m"]:
            rel = abs(latest["wvht_m"] - model) / max(latest["wvht_m"], model, 0.1)
            agreement = max(0.0, 1.0 - rel)  # verified: replaces the 0.7 cap

            if rel > 0.35:
                disagree_note = "buoy %s shows %.1fft vs model %.1fft" % (
                    mp.data["station"], m_to_ft(latest["wvht_m"]), m_to_ft(model))
    # observed CHOP check: the buoy's measured wind-wave height vs the model's.
    # Chop is the heaviest term in the score, so the model's chop claim gets
    # verified against an instrument every run, not trusted.
    spec = fetches.get("ndbc_spec")
    if spec is not None and spec.ok and ma is not None and ma.ok:
        latest = spec.data["latest"]
        t = latest["t"].astimezone(PT).replace(minute=0, second=0, microsecond=0)
        mww = None
        for k in range(4):
            mww = ma.data.get("wind_wave_height", {}).get(t - timedelta(hours=k))
            if mww is not None:
                break
        oww = latest["wwh_m"]
        # Materiality needs an ABSOLUTE floor: buoy and model partition swell
        # vs wind-wave differently, so 0.4m-vs-0.1m is a definitional quibble,
        # not a forecast bust. Only a large-relative AND large-absolute gap
        # counts, and chop-partition disagreement alone can knock confidence
        # to medium, never to low (that right is reserved for total height).
        if (mww is not None and oww is not None
                and abs(oww - mww) >= 0.35 and max(oww, mww) >= 0.5):
            rel = abs(oww - mww) / max(oww, mww, 0.1)
            if rel > 0.5:
                agreement = min(agreement, max(0.5, 1.0 - rel))
                note = "buoy chop %.1fft vs model %.1fft" % (m_to_ft(oww), m_to_ft(mww))
                disagree_note = (disagree_note + "; " + note) if disagree_note else note

    chla = fetches.get("chla")
    bloom_note = None
    if chla and chla.ok and chla.data["mg_m3"] >= CONFIG["scoring"]["chla_bloom_mg_m3"]:
        bloom_note = "chlorophyll %.1f mg/m³ — bloom risk" % chla.data["mg_m3"]

    level = min(completeness, agreement)
    if bloom_note:
        level = min(level, 0.6)  # bloom knocks confidence, never the score
    word = "high" if level >= 0.75 else ("medium" if level >= 0.45 else "low")
    notes = [n for n in (disagree_note, bloom_note) if n]
    if feats["missing"]:
        notes.append("missing: " + ", ".join(feats["missing"]))
    return word, completeness, agreement, notes


# =====================================================================
# site selection + tide FYI
# =====================================================================

def tide_state_at(tides, when: datetime):
    """(height_ft interpolated between hi/lo events, trend str, next event)."""
    if not tides:
        return None, None, None
    prev_e, next_e = None, None
    for e in tides:
        if e["t"] <= when:
            prev_e = e
        elif next_e is None:
            next_e = e
    if prev_e is None or next_e is None:
        e = prev_e or next_e
        return e["ft"], "steady", next_e
    span = (next_e["t"] - prev_e["t"]).total_seconds()
    frac = (when - prev_e["t"]).total_seconds() / span if span else 0.0
    # cosine interp approximates the real tide curve far better than linear
    h = prev_e["ft"] + (next_e["ft"] - prev_e["ft"]) * (1 - math.cos(math.pi * frac)) / 2
    trend = "rising" if next_e["ft"] > prev_e["ft"] else "falling"
    return h, trend, next_e


def tide_band(h_ft):
    if h_ft is None:
        return "any"
    if h_ft < 2.0:
        return "low"
    if h_ft < 4.5:
        return "mid"
    return "high"


def site_tide_fit(site, band):
    pref = site["tide"]
    if pref == "any" or band == "any":
        return 0.0
    if pref == band:
        return 0.3
    if pref == "mid-high" and band in ("mid", "high"):
        return 0.3
    if (pref in ("high", "mid-high") and band == "low") or (pref == "mid" and band == "low"):
        return -0.4
    return 0.0


def surge_at_depth(depth_ft, period_s):
    """Fraction of surface orbital motion surviving at depth (deep-water decay
    exp(-2πd/L), L≈5.12T² ft). Short chop dies fast with depth — a 7s sea keeps
    ~50% at 25ft; a 16s groundswell keeps ~94%. So chop days favor the deeper
    sites and long-period days flatten the differences."""
    if not period_s:
        return 1.0
    wavelength = 5.12 * period_s * period_s
    return math.exp(-2 * math.pi * depth_ft / wavelength)


def best_entries(zone_cfg, w, tides, damage, period_s=None):
    h, trend, next_e = tide_state_at(tides, w["start"] + (w["end"] - w["start"]) / 2)
    band = tide_band(h)
    # CALIBRATION: the 0.06 sets how strongly the day's swell steers site
    # choice vs the static shelter ranking; raise if chop days keep naming
    # shallow coves the log says were washing-machines.
    ranked = sorted(zone_cfg["sites"],
                    key=lambda s: -(s["shelter"] * (1.0 + min(damage or 0, 10) / 5.0)
                                    + site_tide_fit(s, band)
                                    - min(damage or 0, 10)
                                    * surge_at_depth(s.get("depth_ft", 20), period_s) * 0.06))
    names = [s["name"] for s in ranked[:2]]
    fyi = None
    if h is not None:
        # plain words, no datum-speak: "rising tide, high at 9:46am"
        fyi = "%s tide" % trend
        if next_e is not None:
            fyi += ", %s at %s" % ("high" if next_e["type"] == "H" else "low",
                                   next_e["t"].strftime("%-I:%M%p").lower())
    return names, fyi, band


# =====================================================================
# tips — rule-based selection over the curated library. LLM EXTENSION
# POINT: replace/augment select_tip() and the why-line with a single
# model call in v2; everything else stays identical.
# =====================================================================

def select_tip(site_names, score, damage, band, when, state):
    month = when.month
    season = "summer" if 5 <= month <= 10 else "winter"
    viz_tier = "high" if score >= 8.0 else "any"
    surge = "low" if (damage or 0) < 6 else "any"
    candidates = []
    for site in site_names:
        for tip in CONFIG["tips"].get(site, []):
            if tip.get("lane") == "take":
                continue  # reserve sites never get take content, belt-and-braces
            if tip["viz"] not in ("any", viz_tier):
                continue
            if tip["surge"] == "low" and surge != "low":
                continue
            if tip["tide"] not in ("any", band):
                continue
            if tip["season"] not in ("any", season):
                continue
            candidates.append(tip)
    if not candidates:
        for site in site_names:
            candidates += [t for t in CONFIG["tips"].get(site, []) if t["season"] in ("any", season)]
    if not candidates:
        return None
    last = state.get("last_tip_id")
    pick = next((t for t in candidates if t["id"] != last), candidates[0])
    state["last_tip_id"] = pick["id"]
    return pick["text"]


# =====================================================================
# voice + message rendering
# =====================================================================

def tier_for(score):
    if score >= 9.0:
        return 3
    if score >= 8.0:
        return 2
    return 1


def hook_for(score, state):
    v = CONFIG["voice"]
    hooks = v["tier%d_hooks" % tier_for(score)]
    idx = state.get("hook_idx", 0)
    state["hook_idx"] = idx + 1
    return hooks[idx % len(hooks)]


def viz_phrase(score):
    for th, phrase in CONFIG["voice"]["viz_expect"]:
        if score >= th:
            return phrase
    return CONFIG["voice"]["viz_expect"][-1][1]


def wetsuit_phrase(sst_c):
    if sst_c is None:
        return None
    f = sst_c * 9 / 5 + 32
    for th, phrase in CONFIG["voice"]["wetsuit"]:
        if f < th:
            return "%.0f° — %s" % (f, phrase)
    return None


def sensory_phrase(score, kd490=None):
    """What the SETUP will feel like — never a visibility promise. Measured
    2026-08-11: this score has no relationship to independently observed water
    clarity (r=+0.07 over 265 days), so any viz claim here would be invented.
    kd490 is accepted and ignored, kept so callers need not change."""
    for th, phrase in CONFIG["voice"]["setup_feel"]:
        if score >= th:
            return phrase
    return CONFIG["voice"]["setup_feel"][-1][1]


def why_line(feats, score, kind="dawn"):
    """One sentence, the way a friend who checked the buoys would say it:
    the two or three things that actually set up the day, then what you
    should be able to see. Numbers live in the log, not here."""
    p = feats.get("dmg_parts") or {}
    per, hgt = p.get("per_s") or 0, p.get("hgt_ft") or 0
    if per >= 14 and hgt <= 3.0:
        swell = "long-period swell with no size to it"
    elif per >= 14:
        swell = "big groundswell running"
    elif per and per <= 9 and hgt >= 1.5:
        swell = "short chop working the coves"
    elif hgt and hgt <= 1.5:
        swell = "barely a ripple"
    elif hgt:
        swell = "a mellow %s swell" % compass(p.get("dir") or 200)
    else:
        swell = "quiet water"
    clauses = []
    dh = feats.get("dry_hours")
    if dh is not None and dh >= 96:
        clauses.append("%d days without rain" % int(dh // 24))
    clauses.append(swell)
    w = feats.get("wind_window_max_kn")
    if w is not None:
        if w < 6:
            clauses.append("glass at %s" % ("dawn" if kind == "dawn" else "sunset"))
        elif w < 10:
            clauses.append("just a breath of wind")
        else:
            clauses.append("%dkt of wind to work around" % round(w))
    return "%s — %s." % (sentence_case(", ".join(clauses)),
                         sensory_phrase(score, feats.get("kd490")))


def load_history(before=None):
    """(window_start, score) pairs from hindcast + score log, newest-run wins."""
    best = {}
    for path in (HINDCAST, LOG_PATH):
        for r in _read_csv(path):
            try:
                best[r["window_key"]] = (datetime.fromisoformat(r["window_start"]), float(r["score"]))
            except (KeyError, ValueError):
                continue
    hist = list(best.values())
    if before is not None:
        hist = [(t, s) for t, s in hist if t < before]
    return hist


def superlative(score, t_now, hist=None):
    """A brag line the data can actually back up, or None. Never invents."""
    hist = load_history(before=t_now) if hist is None else hist
    if len(hist) < 60 or score < 8.0:
        return None
    better = [t for t, s in hist if s >= score]
    if not better:
        return "best window in the whole log"
    days_since = (t_now - max(better)).days
    if days_since >= 60:
        return "best since %s" % max(better).strftime("%b %-d")
    pct_below = 100.0 * sum(1 for _, s in hist if s < score) / len(hist)
    if pct_below >= 95.0:
        return "top %d%% of the season" % max(1, round(100 - pct_below))
    return None


def render_alert(w, score, feats, conf_word, conf_notes, entries, tide_fyi, tip, state,
                 quiet=False, sst_c=None, brag=None, actions=None, gate=None):
    v = CONFIG["voice"]
    is_perfect = gate if gate is not None else perfect_gate(feats, score, sst_c)[0]
    hook = v["perfect_hook"] if is_perfect else hook_for(score, state)
    title = "%s — %.1f/10 %s %s" % (hook, score, w["label"], v["emoji"])
    lines = [v["perfect_line"] if is_perfect
             else why_line(feats, score, w.get("kind", "dawn"))]

    # Line 2 is a sentence about where to go, not a field dump. The brag leads
    # when it's earned, because rarity is the thing that makes you clear a morning.
    plan = []
    if brag and tier_for(score) >= 2:
        plan.append(sentence_case(brag) + ".")
    where = entries[0] if entries else None
    if where:
        s = "Take %s" % where
        if len(entries) > 1:
            s += " (or %s)" % entries[1]
        if tide_fyi:
            s += ", %s" % tide_fyi
        plan.append(s + ".")
    if tier_for(score) >= 2 and not is_perfect:
        plan.append(v["wildcard"])
    suit = wetsuit_phrase(sst_c)
    if suit:
        plan.append(suit + ".")
    if conf_word != "high":
        note = "Call it %s confidence" % conf_word
        if conf_notes:
            note += " — %s" % conf_notes[0]
        plan.append(note + ".")
    lines.append(" ".join(plan))
    topic = get_secret("NTFY_TOPIC")
    if is_perfect and topic:
        # the rare alert carries its own invite; the tip yields the line
        lines.append(v["gate_share_line"].format(topic=topic))
    elif tip:
        lines.append("Tip: " + tip)
    return {"title": title, "message": "\n".join(lines[:3]),
            "priority": (2 if quiet else (4 if score >= 8.0 else 3)),
            "actions": actions or []}


def render_downgrade(w, old, new, feats):
    p = feats.get("dmg_parts") or {}
    if p.get("per_s") and p["per_s"] <= 10:
        why = "Windswell moved in."
    elif feats.get("wind_window_max_kn") and feats["wind_window_max_kn"] >= 12:
        why = "Wind forecast came up."
    elif feats.get("dry_hours") is not None and feats["dry_hours"] < 72:
        why = "Rain got into the window."
    else:
        why = "Forecast backed off."
    msg = CONFIG["voice"]["downgrade"].format(label=w["label"], old=old, new=new, why=why)
    # split on ". " (not ".") so decimal scores in the title survive
    return {"title": msg.split(". ")[0], "message": msg, "priority": 3}


def perfect_gate(feats, score, sst_c, zone_cfg=None):
    """Every knowable axis aligned, as a strict AND. Returns (passed, failures).
    Deliberately unforgiving: no partial credit, no averaging one axis against
    another. If anything is unknown it fails — silence beats a false promise.
    Zones may override thresholds (Monterey's 53°F is its own warm); the AND
    itself is never overridable."""
    g = dict(CONFIG["perfect_gate"])
    if zone_cfg:
        g.update(zone_cfg.get("perfect_gate_overrides", {}))
    checks = [
        ("flat", feats.get("damage"), lambda v: v <= g["max_damage"]),
        ("glass", feats.get("wind_window_eff_kn", feats.get("wind_window_max_kn")),
         lambda v: v <= g["max_wind_kn"]),
        ("dry", feats.get("dry_hours"), lambda v: v >= g["min_dry_hours"]),
        ("sun", feats.get("cloud_pct"), lambda v: v <= g["max_cloud_pct"]),
        ("warm", sst_c, lambda v: v >= g["min_sst_c"]),
        ("score", score, lambda v: v >= g["min_score"]),
    ]
    failed = [name for name, val, ok in checks if val is None or not ok(val)]
    return (not failed), failed


# What is holding this window back, in one plain word. The score answers
# "how good"; this answers "why not better" — the thing a diver actually
# reads a forecast for.
LIMIT_WORDS = {"surf": "swell", "turbidity": "stirred up", "wind_hist": "recent wind",
               "wind_now": "wind", "light": "grey", "tide_mix": "big tide"}


def limiting_factor(breakdown, cap_reason, score):
    if cap_reason and "rain" in cap_reason:
        return "rain"
    if cap_reason and "wind" in cap_reason:
        return "wind"
    if score >= 8.5:
        return "all clear"
    if not breakdown:
        return None
    worst = max(breakdown.items(), key=lambda kv: kv[1])
    return LIMIT_WORDS.get(worst[0]) if worst[1] >= 0.5 else "all clear"


def best_of(scored):
    return max((s["score"] for s in scored), default=0.0)


def render_digest(scored, t_now, state, sst_c=None, tip=None, actions=None):
    """The weekly ritual: the shape of the week at a glance, then the one day
    worth planning around. Sent whether or not anything clears the threshold —
    a quiet week is useful information, and predictability is the point."""
    v = CONFIG["voice"]
    # Only days you can still plan around, and exactly one calendar week so the
    # strip never wraps to a duplicate weekday name.
    planned = [s for s in scored
               if (s["w"]["start"] - t_now).total_seconds() / 3600.0
               >= CONFIG["alerting"]["lead_min_h"]] or scored
    by_day, order = {}, []
    for s in planned:
        d = s["w"]["start"].date()
        if d not in by_day:
            if len(order) >= CONFIG["alerting"]["digest_days"]:
                continue
            by_day[d] = s
            order.append(d)
        elif s["score"] > by_day[d]["score"]:
            by_day[d] = s
    best = max((by_day[d] for d in order), key=lambda s: s["score"])
    gate_days = [d for d in order if by_day[d].get("gate")]
    strip = " · ".join(
        "%s %.1f%s" % (by_day[d]["w"]["start"].strftime("%a"), by_day[d]["score"],
                       "✨" if d in gate_days else ("★" if by_day[d] is best else ""))
        for d in order)

    def day_name(s):
        return "%s %s" % (s["w"]["start"].strftime("%A"), s["w"].get("kind", "dawn"))

    def in_by(s):
        return s["w"]["start"].strftime("%-I:%M%p").lower()

    # One verdict in the title; the body never repeats it — it adds the exact
    # details a diver plans around: time in the water, the cove, the tide,
    # the temperature. A fact in every sentence, or the sentence goes.
    suit = wetsuit_phrase(sst_c)
    if gate_days:
        gd = by_day[gate_days[0]]
        headline = "✨ %s — everything lines up" % gd["w"]["label"]
        lead = "%s: flat, dry, sunny, warm — the rare kind. In the water by %s" \
               % (sentence_case(day_name(gd)), in_by(gd))
        if gd["entries"]:
            lead += " at %s" % gd["entries"][0]
        if gd["tide_fyi"]:
            lead += "; " + gd["tide_fyi"]
        lead += "; %s." % suit if suit else "."
        body = [strip, lead]
        best = gd
    elif best["score"] >= CONFIG["alerting"]["threshold"]:
        headline = "%s looks like the one" % best["w"]["label"]
        lead = "%s, %s — in the water by %s" % (
            sentence_case(sensory_phrase(best["score"], (best.get("feats") or {}).get("kd490"))),
            best["score"], in_by(best))
        if best["entries"]:
            lead += " at %s" % best["entries"][0]
        if best["tide_fyi"]:
            lead += "; " + best["tide_fyi"]
        lead += "; %s." % suit if suit else "."
        body = [strip, lead]
    else:
        headline = "the bell stays quiet"
        lead = "Best of it: %s at %.1f — %s." % (
            day_name(best), best["score"],
            sensory_phrase(best["score"], (best.get("feats") or {}).get("kd490")))
        detail = "If you go: in by %s" % in_by(best)
        if best["tide_fyi"]:
            detail += ", " + best["tide_fyi"]
        if suit:
            detail += ", " + suit
        body = [strip, lead + " " + detail + "."]
    brag = superlative(best["score"], t_now)
    if brag:
        body[1] += " (%s.)" % sentence_case(brag)
    if tip:
        body.append("Tip: " + tip)
    # a ringing bell doesn't share the week-ahead frame — it IS the news
    title = ("✨ The bell rings %s %s" % (day_name(best), v["emoji"]) if gate_days
             else "The week ahead — %s %s" % (headline, v["emoji"]))
    return {"title": title, "message": "\n".join(body),
            "priority": 4 if gate_days else 3, "actions": actions or []}


# =====================================================================
# alert decision + streak logic
# =====================================================================

def decide_alert(scored, t_now, state, zone_key="A"):
    """scored: list of dicts with window/score/feats/... . Returns (action, payload)
    where action in (None, 'alert', 'quiet_alert', 'downgrade'). Streak state is
    per-zone so enabling Zone B/C later can't cross-contaminate Laguna's."""
    al = CONFIG["alerting"]
    streaks = state.setdefault("streaks", {})
    if "streak" in state:  # migrate pre-multizone state files in place
        legacy = state.pop("streak")
        if legacy:
            streaks.setdefault(zone_key, legacy)
    eligible = [s for s in scored
                if al["lead_min_h"] <= (s["w"]["start"] - t_now).total_seconds() / 3600.0 <= al["lead_max_h"]]
    qualifying = [s for s in eligible if s["score"] >= al["threshold"]]
    best = max(qualifying, key=lambda s: s["score"]) if qualifying else None
    streak = streaks.get(zone_key)

    if best is None:
        if streak:
            best_any = max(eligible, key=lambda s: s["score"]) if eligible else None
            streaks.pop(zone_key, None)
            if best_any is not None and streak["score"] - best_any["score"] >= al["material_change"]:
                return "downgrade", {"w": best_any["w"], "old": streak["score"],
                                     "new": best_any["score"], "feats": best_any["feats"]}
        return None, None

    gate_now = bool(best.get("gate"))
    if not streak:
        streaks[zone_key] = {"score": best["score"], "entries": best["entries"],
                             "window_key": best["w"]["key"], "since": t_now.isoformat(),
                             "gate": gate_now}
        return "alert", best

    # A gate flip IS material change — arguably the most material there is.
    # Without this, "everything just lined up" arriving on day 3 of a modest
    # streak would be silently swallowed by the anti-spam logic.
    gate_flip = gate_now and not streak.get("gate", False)
    moved = abs(best["score"] - streak["score"]) >= al["material_change"]
    entries_changed = best["entries"] != streak["entries"]
    if gate_flip or moved or entries_changed:
        dropped = best["score"] < streak["score"]
        streak.update({"score": best["score"], "entries": best["entries"],
                       "window_key": best["w"]["key"], "gate": gate_now})
        if gate_flip:
            return "alert", best   # loud: the rare day gets the loud voice
        if dropped and moved:
            return "downgrade", {"w": best["w"], "old": streak["score"],
                                 "new": best["score"], "feats": best["feats"]}
        return "quiet_alert", best
    streak["gate"] = gate_now
    return None, None


# =====================================================================
# delivery + health
# =====================================================================

SENTINEL_RUNS = 6   # ~3 days at two runs a day

def sentinel_update(state, fetches, zone_key="A"):
    """The 20-year rule: a data source must not be able to die without a human
    hearing about it. Buoys get decommissioned (46223 Dana Point did), APIs
    move — and fail-soft would otherwise hide it forever. Tracks consecutive
    failures per source and announces each outage exactly once, at the
    threshold crossing; recovery silently resets."""
    counts = state.setdefault("src_fail", {})
    newly = []
    for name, f in fetches.items():
        key = "%s:%s" % (zone_key, name)   # zones share source NAMES, not health
        if f.ok:
            counts[key] = 0
        else:
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == SENTINEL_RUNS:
                newly.append(name)
    return newly

def get_secret(name):
    v = os.environ.get(name)
    if v:
        return v
    lc = os.path.join(ROOT, "local_config.json")
    if os.path.exists(lc):
        with open(lc) as f:
            return json.load(f).get(name)
    return None


def zone_topic(zone_cfg=None):
    """Product topic for a zone. Zone A honors the legacy secret override."""
    if zone_cfg is None or zone_cfg.get("bell", {}).get("no") == 1:
        return get_secret("NTFY_TOPIC") or (zone_cfg or CONFIG["zones"]["A"]).get("topic")
    return zone_cfg.get("topic")


def ops_topic():
    """Keeper channel: sentinels, watchdogs, drift, the council. Divers never
    see the plumbing — ops rides <bell-1-topic>-ops."""
    base = zone_topic()
    return (base + "-ops") if base else None


def feedback_topic(zone_cfg=None):
    t = zone_topic(zone_cfg)
    return (t + "-fb") if t else None


def feedback_actions(window_key, zone_cfg=None):
    """ntfy action buttons that post a verdict to the zone's feedback topic.
    Zero secrets leave the repo: the buttons just publish plain text, and the
    next scheduled run polls the topic and folds verdicts into dive_log.csv."""
    fb = feedback_topic(zone_cfg)
    if not fb:
        return []
    return [{"action": "http", "label": label,
             "url": "%s/%s" % (CONFIG["sources"]["ntfy"], fb),
             "method": "POST", "body": "%s|%s" % (verdict, window_key), "clear": True}
            for label, verdict in CONFIG["voice"]["fb_buttons"]]


# verdict -> viz/surge for the dive log. The buttons speak in what-you-saw;
# this is where each verdict becomes a number the backtest can use:
#   saw the whole reef ≈ 25ft · saw your buddy across it ≈ 12ft ·
#   saw only your fins ≈ 4ft
# CALIBRATION: these seed backtests until precise numbers arrive via log-dive.
FEEDBACK_MAP = {"clear": (25.0, "low"), "fair": (12.0, "low"), "murk": (4.0, "med")}


def ingest_feedback(state, dry_run, zone_cfg=None, zone_key="A"):
    """Poll a zone's feedback topic for button presses since last ingest; join
    each verdict back to its window and append to dive_log.csv. Fail-soft."""
    fb = feedback_topic(zone_cfg)
    if not fb:
        return 0
    try:
        since_map = state.setdefault("fb_since_by_zone", {})
        if "fb_since" in state:  # migrate the single-zone key in place
            since_map.setdefault("A", state.pop("fb_since"))
        since = since_map.get(zone_key, "48h")
        url = "%s/%s/json?poll=1&since=%s" % (CONFIG["sources"]["ntfy"], fb, since)
        body = http_get(url, timeout=20)
        rows, latest = [], None
        for line in body.splitlines():
            if not line.strip():
                continue
            m = json.loads(line)
            if m.get("event") != "message":
                continue
            latest = max(latest or 0, m.get("time", 0))
            parts = (m.get("message") or "").split("|", 1)
            if len(parts) != 2 or parts[0] not in FEEDBACK_MAP:
                continue
            verdict, window_key = parts
            viz, surge = FEEDBACK_MAP[verdict]
            try:
                ts = datetime.strptime(window_key.split("-")[0], "%Y%m%dT%H%M").replace(tzinfo=PT)
            except ValueError:
                ts = now_pt()
            rows.append({"ts": ts.isoformat(), "site": "(alert feedback)",
                         "viz_ft": viz, "surge": surge,
                         "notes": "feedback:%s window:%s" % (verdict, window_key)})
        if rows:
            append_log(DIVE_LOG, ["ts", "site", "viz_ft", "surge", "notes"], rows, dry_run)
            print("ingested %d feedback verdict(s)" % len(rows))
        if latest:
            since_map[zone_key] = str(latest + 1)
        return len(rows)
    except Exception as e:
        print("feedback ingest skipped: %s" % e, file=sys.stderr)
        return 0


def notify(payload, dry_run, topic=None):
    topic = topic or zone_topic()
    if dry_run or not topic:
        print("--- NOTIFICATION (%s) ---" % ("dry-run" if dry_run else "NO TOPIC SET"))
        print("Title: " + payload["title"])
        print(payload["message"])
        if payload.get("actions"):
            print("[buttons: %s]" % " · ".join(a["label"] for a in payload["actions"]))
        print("-------------------------")
        return not dry_run and topic is None
    msg = {"topic": topic, "title": payload["title"],
           "message": payload["message"], "priority": payload["priority"]}
    if payload.get("actions"):
        msg["actions"] = payload["actions"]
    req = urllib.request.Request(CONFIG["sources"]["ntfy"], data=json.dumps(msg).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    return True


# =====================================================================
# SMS — the ring, by text. Design constraints, in order:
#   1. The repo is PUBLIC: phone numbers never touch it. Twilio's own
#      message history is the subscriber list — anyone who texted in is
#      subscribed; their latest bell keyword names their bell; Twilio's
#      Advanced Opt-Out enforces STOP at the carrier.
#   2. SMS carries ONLY the ring (and the Function's welcome). ~15 msgs
#      per subscriber per year. Digests stay on free channels.
#   3. TCPA quiet hours: rings send only 8am-9pm in the BELL's timezone
#      (the best proxy we have for its subscribers). A dawn-run ring
#      defers; the afternoon run re-sees the window and sends then.
# Env: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM. Absent env
# = SMS layer silently off; nothing else changes.
# =====================================================================

def _twilio_env():
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    return (sid, tok, frm) if (sid and tok and frm) else None


def _twilio_req(path, sid, tok, data=None):
    import base64
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/%s" % (sid, path)
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(("%s:%s" % (sid, tok)).encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


BELL_KEYWORDS = None   # built lazily: LAGUNA->A, MAUI->P, ...

def bell_keyword_map():
    global BELL_KEYWORDS
    if BELL_KEYWORDS is None:
        BELL_KEYWORDS = {}
        for zk, zc in CONFIG["zones"].items():
            if zc.get("enabled"):
                words = zc["bell"]["name"].upper().split()
                # 'LA'/'POINT'/'SANTA' would match half of English (and each
                # other) — take the first DISTINCTIVE word, min 4 letters
                word = next((w for w in words if len(w) >= 4
                             and w not in ("POINT", "SANTA", "NORTH", "SHORE", "COUNTY")),
                            words[-1])
                BELL_KEYWORDS[word] = zk
    return BELL_KEYWORDS


def sms_subscribers():
    """{zone_key: [numbers]} from Twilio's inbound history. The latest bell
    keyword a number texted decides its bell; numbers with no recognizable
    keyword ride Bell No.1. Returns {} when SMS env is absent."""
    env = _twilio_env()
    if not env:
        return {}
    sid, tok, frm = env
    kw = bell_keyword_map()
    latest = {}
    page = "Messages.json?" + urllib.parse.urlencode({"To": frm, "PageSize": 400})
    for _ in range(20):   # paginate, bounded
        d = _twilio_req(page, sid, tok)
        for m in d.get("messages", []):
            num, body = m.get("from"), (m.get("body") or "").strip().upper()
            if not num:
                continue
            zk = next((z for w, z in kw.items() if w in body), None)
            if num not in latest or (zk and latest[num] is None):
                latest[num] = zk
        nxt = d.get("next_page_uri")
        if not nxt:
            break
        page = nxt.split("/%s/" % sid, 1)[-1]
    out = {}
    for num, zk in latest.items():
        out.setdefault(zk or "A", []).append(num)
    return out


def sms_quiet_ok(zone_cfg, when=None):
    h = (when or datetime.now(tz=zone_tz(zone_cfg))).astimezone(zone_tz(zone_cfg)).hour
    return 8 <= h < 21


def sms_ring(zone_key, zone_cfg, payload, state, dry_run):
    """One segment, the ring only, deduped per window, quiet-hours safe.
    Twilio refuses opted-out numbers on its own."""
    env = _twilio_env()
    if not env:
        return
    wkey = payload["w"]["key"]
    sent = state.setdefault("sms_rung", {})
    if sent.get(zone_key) == wkey or not sms_quiet_ok(zone_cfg):
        return
    subs = sms_subscribers().get(zone_key, [])
    if not subs:
        sent[zone_key] = wkey
        return
    body = ("THE BELL IS RINGING - %s. %s. In by %s at %s. thedivebell.com "
            "Reply STOP to end.") % (
        zone_cfg["bell"]["name"], payload["w"]["label"],
        payload["w"]["start"].strftime("%-I:%M%p").lower(),
        payload["entries"][0] if payload["entries"] else "your cove")
    sid, tok, frm = env
    ok = 0
    for num in subs:
        try:
            if not dry_run:
                _twilio_req("Messages.json", sid, tok,
                            {"To": num, "From": frm, "Body": body})
            ok += 1
        except Exception as e:
            print("sms to %s… failed: %s" % (num[:6], str(e)[:60]), file=sys.stderr)
    sent[zone_key] = wkey
    print("sms ring (%s): %d/%d sent%s" % (zone_key, ok, len(subs),
                                           " [dry]" if dry_run else ""))


def notify_ops(payload, dry_run):
    """Plumbing news goes to the keeper channel, never to divers."""
    return notify(payload, dry_run, topic=ops_topic())


def ping_health(ok=True, msg=""):
    url = get_secret("HEALTHCHECKS_URL")
    if not url:
        return
    try:
        target = url if ok else url.rstrip("/") + "/fail"
        req = urllib.request.Request(target, data=msg.encode() if msg else None)
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print("healthcheck ping failed: %s" % e, file=sys.stderr)


# =====================================================================
# state + logging
# =====================================================================

STATE_PATH = os.path.join(DATA, "alert_state.json")
LOG_PATH = os.path.join(DATA, "score_log.csv")
DIVE_LOG = os.path.join(DATA, "dive_log.csv")
HINDCAST = os.path.join(DATA, "hindcast.csv")

LOG_COLS = ["run_ts", "zone", "window_key", "window_start", "window_kind", "lead_h",
            "score", "cap_reason", "confidence", "completeness", "agreement",
            "damage", "swell_hgt_ft", "swell_per_s", "swell_dir",
            "wind_energy_48h", "swell_energy_72h", "dry_hours", "wind_window_max_kn",
            "obs_frac", "cloud_pct", "sst_c", "chla_mg_m3", "kd490_m1",
            "perfect_gate", "best_entries", "alerted", "flags"]


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state, dry_run):
    if dry_run:
        return
    os.makedirs(DATA, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, default=str)


def append_log(path, cols, rows, dry_run):
    if dry_run:
        return
    os.makedirs(DATA, exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            wr.writeheader()
        for r in rows:
            wr.writerow(r)


# =====================================================================
# run pipeline
# =====================================================================

def score_zone(zone_key, zone_cfg, fetches, t_now, horizon_h=72):
    windows = build_windows(fetches["weather"], t_now, zone_cfg, horizon_h)
    tides = fetches["tides"].data if fetches["tides"].ok else []
    scored = []
    anchor = buoy_anchor(fetches, t_now)
    for w in windows:
        feats = compute_features(w, fetches, zone_cfg, t_now, anchor)
        creek = False  # zone-level scoring; creek rule applies per-site when Zone B wakes
        score, breakdown, cap_reason, flags = score_window(feats, creek)
        conf_word, comp, agree, notes = confidence(fetches, feats, t_now)
        entries, tide_fyi, band = best_entries(zone_cfg, w, tides, feats["damage"],
                                               (feats["dmg_parts"] or {}).get("per_s"))
        sstv = fetches["sst"].data["c"] if fetches["sst"].ok else None
        gate_ok, _ = perfect_gate(feats, score, sstv, zone_cfg)
        # a provisional bell watches and speaks but cannot ring — it has not
        # been sworn (buoy check + first local verdicts) for this water
        if zone_cfg.get("tier") == "provisional":
            gate_ok = False
        scored.append({"zone": zone_key, "w": w, "score": score, "feats": feats,
                       "limit": limiting_factor(breakdown, cap_reason, score),
                       "breakdown": breakdown, "cap_reason": cap_reason, "flags": flags,
                       "conf": conf_word, "completeness": comp, "agreement": agree,
                       "conf_notes": notes, "entries": entries, "tide_fyi": tide_fyi,
                       "band": band, "gate": gate_ok})
    return scored


def cmd_run(args):
    t_now = now_pt()
    state = load_state()
    all_scored, any_swell_ok = [], False
    board = {}
    for zk, zc in CONFIG["zones"].items():
        if not zc["enabled"]:
            continue
        if not args.offline:
            ingest_feedback(state, args.dry_run, zc, zk)
        ztopic = zone_topic(zc)
        fetches, src = fetch_all(zc, offline=args.offline, fixture_set=args.fixtures)
        fx_now = src.fixture_now()
        if fx_now:
            t_now = fx_now
        swell_ok = fetches["marine"].ok or fetches["ndbc_primary"].ok
        any_swell_ok = any_swell_ok or swell_ok
        for name, f in fetches.items():
            if not f.ok:
                print("degraded: %s failed (%s)" % (name, f.error), file=sys.stderr)
        for dead in sentinel_update(state, fetches, zk):
            notify_ops({"title": "An instrument went quiet 🔧",
                        "message": "%s (bell %s) has failed %d straight runs (~3 days). "
                                   "The bell scores on without it, at lower confidence. "
                                   "github.com/PacificVanguard/dive-alert/actions"
                                   % (dead, zc.get("bell", {}).get("name", zk), SENTINEL_RUNS),
                        "priority": 4}, args.dry_run)
        if not swell_ok:
            print("zone %s: ALL swell sources failed" % zk, file=sys.stderr)
            continue
        horizon = 24 * CONFIG["alerting"]["digest_days"] if args.weekly else 72
        scored = score_zone(zk, zc, fetches, t_now, horizon)
        sst = fetches["sst"].data["c"] if fetches["sst"].ok else None
        chla = fetches["chla"].data["mg_m3"] if fetches["chla"].ok else None
        rows = []
        for s in scored:
            lead = (s["w"]["start"] - t_now).total_seconds() / 3600.0
            p = s["feats"]["dmg_parts"] or {}
            rows.append({
                "run_ts": t_now.isoformat(), "zone": zk, "window_key": s["w"]["key"],
                "window_start": s["w"]["start"].isoformat(), "window_kind": s["w"]["kind"],
                "lead_h": round(lead, 1), "score": s["score"], "cap_reason": s["cap_reason"] or "",
                "confidence": s["conf"], "completeness": round(s["completeness"], 2),
                "agreement": round(s["agreement"], 2),
                "damage": round(s["feats"]["damage"], 2) if s["feats"]["damage"] is not None else "",
                "swell_hgt_ft": round(p.get("hgt_ft", 0), 1), "swell_per_s": round(p.get("per_s", 0), 1),
                "swell_dir": round(p.get("dir", 0)),
                "wind_energy_48h": round(s["feats"]["wind_energy_48h"], 0) if s["feats"]["wind_energy_48h"] is not None else "",
                "swell_energy_72h": round(s["feats"]["swell_energy_72h"], 0) if s["feats"]["swell_energy_72h"] is not None else "",
                "dry_hours": round(s["feats"]["dry_hours"], 0) if s["feats"]["dry_hours"] is not None else "",
                "wind_window_max_kn": round(s["feats"]["wind_window_max_kn"], 1) if s["feats"]["wind_window_max_kn"] is not None else "",
                "obs_frac": round(s["feats"]["obs_frac"], 2),
                "cloud_pct": round(s["feats"]["cloud_pct"]) if s["feats"].get("cloud_pct") is not None else "",
                "sst_c": sst if sst is not None else "", "chla_mg_m3": chla if chla is not None else "",
                "kd490_m1": s["feats"].get("kd490") if s["feats"].get("kd490") is not None else "",
                "perfect_gate": "PASS" if perfect_gate(s["feats"], s["score"], sst)[0] else "",
                "best_entries": " / ".join(s["entries"]), "alerted": "",
                "flags": "; ".join(s["flags"])})
        all_scored += scored

        print("\n=== Zone %s (%s) — %s ===" % (zk, zc["name"], t_now.strftime("%a %Y-%m-%d %H:%M %Z")))
        for s in scored:
            lead = (s["w"]["start"] - t_now).total_seconds() / 3600.0
            print("  %-10s %+5.0fh  score %-4.1f conf %-6s %s%s" % (
                s["w"]["label"], lead, s["score"], s["conf"],
                " / ".join(s["entries"]),
                ("  [%s]" % s["cap_reason"]) if s["cap_reason"] else ""))

        if args.weekly:
            tip = select_tip(scored[0]["entries"] if scored else [], best_of(scored),
                             None, "any", t_now, state) if scored else None
            notify(render_digest(scored, t_now, state, sst_c=sst, tip=tip,
                                 actions=feedback_actions("digest-%s" % t_now.date(), zc)),
                   args.dry_run, topic=ztopic)
        elif args.brief:
            best = max(scored, key=lambda s: s["score"]) if scored else None
            if best:
                txt = "Zone %s best: %.1f/10 %s — %s. %s" % (
                    zk, best["score"], best["w"]["label"], " / ".join(best["entries"]),
                    best["tide_fyi"] or "")
                notify({"title": "Dive brief — Zone %s" % zk, "message": txt, "priority": 2},
                       args.dry_run, topic=ztopic)
        else:
            action, payload = decide_alert(scored, t_now, state, zk)
            if action == "alert" or action == "quiet_alert":
                tip = select_tip(payload["entries"], payload["score"], payload["feats"]["damage"],
                                 payload["band"], payload["w"]["start"], state)
                msg = render_alert(payload["w"], payload["score"], payload["feats"], payload["conf"],
                                   payload["conf_notes"], payload["entries"], payload["tide_fyi"],
                                   tip, state, quiet=(action == "quiet_alert"),
                                   sst_c=sst, brag=superlative(payload["score"], t_now),
                                   actions=feedback_actions(payload["w"]["key"], zc),
                                   gate=payload.get("gate"))
                notify(msg, args.dry_run, topic=ztopic)
                if payload.get("gate"):
                    sms_ring(zk, zc, payload, state, args.dry_run)
                rec["promises"] += 1
                for r in rows:
                    if r["window_key"] == payload["w"]["key"]:
                        r["alerted"] = action
            elif action == "downgrade":
                notify(render_downgrade(payload["w"], payload["old"], payload["new"], payload["feats"]),
                       args.dry_run, topic=ztopic)
        append_log(LOG_PATH, LOG_COLS, rows, args.dry_run)
        # the bell remembers: a gate day seen is a ring recorded
        gate_days_seen = [s["w"]["start"].date().isoformat() for s in scored if s.get("gate")]
        rec = state.setdefault("record", {}).setdefault(zk, {"promises": 0, "rings": 0})
        if gate_days_seen:
            prev = state.setdefault("last_ring", {}).get(zk)
            if max(gate_days_seen) != prev:
                rec["rings"] += 1
            state["last_ring"][zk] = max(gate_days_seen)
        board[zk] = {
            "keeper": zc.get("keeper"),
            "region": zc.get("region", "Elsewhere"),
            "season_note": zc.get("season_note"),
            "casting": zc.get("casting"),
            "warm_f": round((dict(CONFIG["perfect_gate"],
                                  **zc.get("perfect_gate_overrides", {}))["min_sst_c"])
                            * 9 / 5 + 32),
            "tz": zc.get("tz", "America/Los_Angeles"),
            "record": dict(rec),
            "bell": zc.get("bell", {}), "tier": zc.get("tier", "provisional"),
            "topic": ztopic, "name": zc["name"],
            "last_ring": state.get("last_ring", {}).get(zk),
            "sst_f": round(sst * 9 / 5 + 32) if sst is not None else None,
            "windows": [{"label": s["w"]["label"],
                         "start": s["w"]["start"].isoformat(),
                         "kind": s["w"].get("kind", "dawn"),
                         "score": s["score"], "conf": s["conf"],
                         "limit": s.get("limit"),
                         "entries": s["entries"][:2],
                         "gate": bool(s.get("gate"))} for s in scored],
        }

    # the website's heartbeat: one file for the board, and latest.json kept
    # as Bell No.1's view for continuity (bellwatch reads its age)
    if not args.dry_run and board:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "zones.json"), "w") as jf:
            json.dump({"updated": t_now.isoformat(),
                       "sms_live": CONFIG.get("sms_live", False),
                       "zones": board}, jf, indent=1)
        first = board.get("A") or list(board.values())[0]
        with open(os.path.join(DATA, "latest.json"), "w") as jf:
            json.dump({"updated": t_now.isoformat(), "zone": "A",
                       "sst_f": first["sst_f"], "windows": first["windows"]}, jf, indent=1)

    save_state(state, args.dry_run)
    if not any_swell_ok:
        ping_health(ok=False, msg="all swell sources failed")
        sys.exit(1)
    ping_health(ok=True)


# =====================================================================
# hindcast + dive log + backtest
# =====================================================================

def cmd_hindcast(args):
    """Rebuild historical zone scores from Open-Meteo archives so backtesting
    works from day one. Dawn/dusk per day, features from the archive series."""
    zc = CONFIG["zones"][getattr(args, "zone", "A")]
    end = now_pt().date() - timedelta(days=2)  # archive lags ~2 days
    start = end - timedelta(days=args.days)
    def arch(url, extra):
        q = dict(latitude=zc["lat"], longitude=zc["lon"],
                 start_date=str(start - timedelta(days=4)), end_date=str(end),
                 timezone="America/Los_Angeles")
        q.update(extra)
        return json.loads(http_get(url + "?" + urllib.parse.urlencode(q), timeout=60))["hourly"]
    print("fetching archives %s → %s ..." % (start, end))
    mh = arch(CONFIG["sources"]["marine"],
              {"hourly": "wave_height,wave_period,swell_wave_height,swell_wave_period,"
                         "swell_wave_direction,wind_wave_height,wind_wave_period,wind_wave_direction"})
    # cloud_cover included so the hindcast scores with the SAME model that
    # alerts — omitting a scored input here silently validates a model we
    # never ship (this exact asymmetry has bitten twice now).
    wh = arch(CONFIG["sources"]["weather_archive"],
              {"hourly": "wind_speed_10m,wind_direction_10m,precipitation,cloud_cover",
               "wind_speed_unit": "kn", "precipitation_unit": "inch"})
    marine = {k: hourly_map(mh["time"], mh[k]) for k in mh if k != "time"}
    marine["wave_direction"] = marine.get("swell_wave_direction", {})
    wx = {k: hourly_map(wh["time"], wh[k]) for k in wh if k != "time"}

    # Satellite clarity history, so hindcast scores are built by the SAME model
    # that alerts. Without this, backtests validate a model we never ship.
    kd_by_date = {}
    try:
        kd_end = min(end, (datetime.now(timezone.utc) - timedelta(days=11)).date())
        u = (CONFIG["sources"]["kd490"] +
             "?kd_490%%5B(%sT12:00:00Z):(%sT12:00:00Z)%%5D%%5B(0.0)%%5D"
             "%%5B(%.2f):(%.2f)%%5D%%5B(%.2f):(%.2f)%%5D"
             % (start - timedelta(days=30), kd_end,
                zc["lat"] - 0.06, zc["lat"] + 0.06, zc["lon"] - 0.12, zc["lon"] + 0.03))
        kd = json.loads(http_get(u, timeout=120))
        acc = {}
        for r in kd["table"]["rows"]:
            if r[-1] is not None:
                acc.setdefault(r[0][:10], []).append(r[-1])
        kd_by_date = {k: sum(v) / len(v) for k, v in acc.items()}
        print("  kd490: %d days with data" % len(kd_by_date))
    except Exception as e:
        print("  kd490 history unavailable (%s) — hindcast omits the clarity term" % e)

    def kd_for(day):
        """Most recent clarity value at or before `day`, within the stale gate —
        the same lookback rule live scoring uses."""
        for back in range(0, CONFIG["scoring"]["kd490_stale_days"] + 1):
            v = kd_by_date.get(str(day - timedelta(days=back)))
            if v is not None:
                return v
        return None

    # Historical tide predictions (CO-OPS serves past dates; chunked) — the
    # tide_mix term must exist in hindcast or we validate a model we never
    # ship. Fail-soft: no tides just zeroes that term.
    tide_events = []
    try:
        span_start = start - timedelta(days=1)
        while span_start <= end:
            span_end = min(span_start + timedelta(days=300), end + timedelta(days=1))
            tq = urllib.parse.urlencode({
                "product": "predictions", "application": "dive_alert",
                "begin_date": span_start.strftime("%Y%m%d"),
                "end_date": span_end.strftime("%Y%m%d"),
                "datum": "MLLW",
                "station": zc.get("tide_station", CONFIG["sources"]["tide_station"]),
                "time_zone": "lst_ldt", "units": "english", "interval": "hilo",
                "format": "json"})
            dd = json.loads(http_get(CONFIG["sources"]["tides"] + "?" + tq, timeout=60))
            tide_events += [{"t": datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=PT),
                             "ft": float(p["v"]), "type": p["type"]}
                            for p in dd["predictions"]]
            span_start = span_end + timedelta(days=1)
        print("  tides: %d hi/lo events" % len(tide_events))
    except Exception as e:
        print("  tide history unavailable (%s) — hindcast omits the tide_mix term" % e)

    rows = []
    d = start
    fetches = {"marine": Fetch("marine", True, marine),
               "weather": Fetch("weather", True, {"wind_speed_10m": wx["wind_speed_10m"],
                                                  "wind_direction_10m": wx.get("wind_direction_10m", {}),
                                                  "precipitation": wx["precipitation"],
                                                  "cloud_cover": wx.get("cloud_cover", {}),
                                                  "sunrise": [], "sunset": []}),
               "ndbc_primary": Fetch("ndbc_primary", False, error="hindcast"),
               "ndbc_offshore": Fetch("ndbc_offshore", False, error="hindcast"),
               "ndbc_spec": Fetch("ndbc_spec", False, error="hindcast"),
               "tides": (Fetch("tides", True, tide_events) if tide_events
                         else Fetch("tides", False, error="hindcast")),
               "sst": Fetch("sst", False, error="hindcast"),
               "chla": Fetch("chla", False, error="hindcast")}
    while d <= end:
        kdv = kd_for(d)
        fetches["kd490"] = (Fetch("kd490", True, {"m1": kdv, "age_d": 0})
                            if kdv is not None else Fetch("kd490", False, error="no data"))
        # fixed 6:00 dawn / 17:30 dusk approximations; hindcast compares
        # day-to-day, exact sun times matter little at this granularity
        for kind, hh, mm, dur in (("dawn", 6, 0, 3), ("dusk", 17, 30, 2)):
            st = datetime(d.year, d.month, d.day, hh, mm, tzinfo=PT)
            w = {"kind": kind, "start": st, "end": st + timedelta(hours=dur),
                 "label": "%s %s" % (st.strftime("%a"), kind),
                 "key": "%s-%s" % (st.strftime("%Y%m%dT%H%M"), kind)}
            feats = compute_features(w, fetches, zc, st)  # t_now=start → all "observed"
            score, _, cap_reason, flags = score_window(feats)
            p = feats["dmg_parts"] or {}
            rows.append({"window_key": w["key"], "window_start": st.isoformat(),
                         "window_kind": kind, "score": score, "cap_reason": cap_reason or "",
                         "damage": round(feats["damage"], 2) if feats["damage"] is not None else "",
                         "swell_hgt_ft": round(p.get("hgt_ft", 0), 1),
                         "swell_per_s": round(p.get("per_s", 0), 1),
                         "swell_dir": round(p.get("dir", 0)),
                         "wind_energy_48h": round(feats["wind_energy_48h"] or 0),
                         "swell_energy_72h": round(feats["swell_energy_72h"] or 0),
                         "dry_hours": round(feats["dry_hours"] or 0),
                         "cloud_pct": round(feats["cloud_pct"]) if feats.get("cloud_pct") is not None else "",
                         "kd490": round(kdv, 4) if kdv is not None else ""})
        d += timedelta(days=1)
    cols = list(rows[0].keys())
    os.makedirs(DATA, exist_ok=True)
    zone_arg = getattr(args, "zone", "A")
    out_path = HINDCAST if zone_arg == "A" else HINDCAST.replace(".csv", "_%s.csv" % zone_arg)
    with open(out_path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for r in rows:
            wr.writerow(r)
    print("wrote %d windows to %s" % (len(rows), out_path))
    good = [r for r in rows if r["score"] >= 7]
    print("windows ≥7: %d (%.0f%%); best: %s at %.1f" % (
        len(good), 100.0 * len(good) / len(rows),
        max(rows, key=lambda r: r["score"])["window_key"],
        max(r["score"] for r in rows)))


def cmd_log_dive(args):
    cols = ["ts", "site", "viz_ft", "surge", "notes"]
    append_log(DIVE_LOG, cols, [{"ts": now_pt().isoformat(), "site": args.site,
                                 "viz_ft": args.viz, "surge": args.surge,
                                 "notes": args.notes or ""}], dry_run=False)
    print("logged: %s viz %sft surge %s" % (args.site, args.viz, args.surge))


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def cmd_backtest(args):
    dives = _read_csv(DIVE_LOG)
    hist = _read_csv(HINDCAST) + _read_csv(LOG_PATH)
    if not dives:
        print("no dives logged yet — use: dive_alert.py log-dive --site ... --viz ... --surge ...")
        return
    if not hist:
        print("no scored windows — run hindcast first")
        return
    print("%-12s %-22s %5s %6s | %5s %6s %6s %6s" % (
        "date", "site", "viz", "surge", "score", "dmg", "windE", "dryH"))
    pairs = []
    for d in dives:
        dt = datetime.fromisoformat(d["ts"])
        best, best_gap = None, 1e9
        for h in hist:
            hw = datetime.fromisoformat(h["window_start"])
            gap = abs((hw - dt).total_seconds())
            if gap < best_gap:
                best, best_gap = h, gap
        if best and best_gap < 12 * 3600:
            pairs.append((float(d["viz_ft"]), float(best["score"] or 0),
                          float(best["damage"] or 0), float(best["wind_energy_48h"] or 0),
                          float(best["dry_hours"] or 0)))
            print("%-12s %-22s %5s %6s | %5s %6s %6s %6s" % (
                dt.strftime("%Y-%m-%d"), d["site"][:22], d["viz_ft"], d["surge"],
                best["score"], best["damage"], best["wind_energy_48h"], best["dry_hours"]))
    if len(pairs) >= 3:
        def corr(xs, ys):
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
            return num / den if den else 0.0
        viz = [p[0] for p in pairs]
        print("\ncorrelation with logged viz (hand-tuning hints):")
        for i, name in ((1, "score"), (2, "damage"), (3, "wind_energy_48h"), (4, "dry_hours")):
            print("  %-16s r=%+.2f" % (name, corr([p[i] for p in pairs], viz)))
        print("(want: score/dry_hours positive, damage/wind negative — if not, revisit CALIBRATION notes)")
    else:
        print("\n(%d matched dives — need 3+ for correlation hints)" % len(pairs))


# =====================================================================
# fixtures
# =====================================================================

def cmd_record_fixtures(args):
    zc = CONFIG["zones"]["A"]
    fetches, src = fetch_all(zc, offline=False)
    outdir = os.path.join(FIXTURES, "normal")
    os.makedirs(outdir, exist_ok=True)
    for key, body in src.recorded.items():
        ext = ".txt" if key.startswith("ndbc") else ".json"
        with open(os.path.join(outdir, key + ext), "w") as f:
            f.write(body)
    with open(os.path.join(outdir, "meta.json"), "w") as f:
        json.dump({"now": now_pt().isoformat()}, f)
    print("recorded %d fixtures to %s" % (len(src.recorded), outdir))
    for k, fetch in fetches.items():
        print("  %-14s %s" % (k, "ok" if fetch.ok else "FAILED: %s" % fetch.error))


# =====================================================================
# setup — one command from zero to notifications
# =====================================================================

def cmd_setup(args):
    import secrets as pysecrets
    lc_path = os.path.join(ROOT, "local_config.json")
    lc = {}
    if os.path.exists(lc_path):
        with open(lc_path) as f:
            lc = json.load(f)
    topic = args.topic or lc.get("NTFY_TOPIC") or ("laguna-dive-" + pysecrets.token_hex(4))
    lc["NTFY_TOPIC"] = topic
    with open(lc_path, "w") as f:
        json.dump(lc, f, indent=1)
    os.environ["NTFY_TOPIC"] = topic

    print("1. ntfy topic: %s" % topic)
    print("   → install the ntfy app (iOS/Android), subscribe to: %s" % topic)
    print("   → or open https://ntfy.sh/%s in a browser" % topic)
    print("   feedback topic (automatic): %s" % feedback_topic())

    welcome = {
        "title": CONFIG["voice"]["welcome_title"],
        "message": CONFIG["voice"]["welcome_body"],
        "priority": 3,
        "actions": feedback_actions("setup-test"),
    }
    try:
        notify(welcome, dry_run=False)
        print("2. welcome notification sent (cached ~12h — subscribe and you'll see it)")
    except Exception as e:
        print("2. welcome notification FAILED: %s" % e)

    print("3. Healthchecks (optional): create a check at healthchecks.io (period 1 day,")
    print("   grace 6h) and add its ping URL as HEALTHCHECKS_URL — skipped if unset.")

    gh_cmds = [
        "gh repo create dive-alert --private --source=. --push",
        "gh secret set NTFY_TOPIC --body '%s'" % topic,
        "gh secret set HEALTHCHECKS_URL --body '<your ping url>'   # optional",
        "gh workflow run dive-alert",
    ]
    if args.github:
        import subprocess
        for c in gh_cmds[:2] + gh_cmds[3:]:
            print("→ " + c)
            r = subprocess.run(c, shell=True, cwd=ROOT)
            if r.returncode != 0:
                print("   failed — finish manually with the commands below")
                break
    else:
        print("4. GitHub (run these, or re-run: python3 dive_alert.py setup --github):")
        for c in gh_cmds:
            print("   " + c)


def cmd_share(args):
    """Onboarding for a dive buddy is subscribe-only: the compute runs in one
    person's repo, ntfy is pub-sub, and the feedback buttons work for every
    subscriber — each buddy is another ground-truth sensor. Prints an invite
    ready to paste into a text message."""
    topic = get_secret("NTFY_TOPIC")
    if not topic:
        print("No topic yet — run `python3 dive_alert.py setup` first.")
        sys.exit(1)
    print("─" * 60)
    print("Dive-conditions alerts for the Laguna coves — free, ~1/week.")
    print("Setup is 30 seconds and needs no account:")
    print("  1. Install the ntfy app:")
    print("     iPhone: https://apps.apple.com/app/ntfy/id1625396347")
    print("     Android: https://play.google.com/store/apps/details?id=io.heckel.ntfy")
    print("  2. In the app, tap + and subscribe to: %s" % topic)
    print("     (or open https://ntfy.sh/%s in a browser)" % topic)
    print("After a dive, press the buttons on the alert — it tunes the model.")
    print("─" * 60)
    print("(copy everything above into a text; that's the whole onboarding)")


# =====================================================================
# validate — pressure-test the swell model against 45 days of buoy truth
# =====================================================================

BUOY_POSITIONS = {"46253": (33.576, -118.181), "46254": (32.868, -117.267),
                  "46240": (36.626, -121.907), "46086": (32.499, -118.052),
                  "46042": (36.785, -122.398), "46221": (33.855, -118.633),
                  "46239": (36.342, -122.102), "46054": (34.265, -120.477),
                  "51201": (21.673, -158.117), "51003": (19.196, -160.639)}


def cmd_validate(args):
    zc = CONFIG["zones"][getattr(args, "zone", "A")]
    st = zc.get("buoy", CONFIG["sources"]["ndbc_primary"])
    blat, blon = BUOY_POSITIONS.get(st, (zc["lat"], zc["lon"]))
    print("fetching %s buoy record (45d) and Open-Meteo model at the buoy..." % st)
    obs_rows = parse_ndbc(http_get(CONFIG["sources"]["ndbc_url"].format(station=st), 30))
    # compare the model AT the buoy so we test the model, not the geography
    q = urllib.parse.urlencode({
        "latitude": blat, "longitude": blon,
        "hourly": "wave_height,wave_period", "past_days": 46, "forecast_days": 1,
        "timezone": "America/Los_Angeles"})
    d = json.loads(http_get(CONFIG["sources"]["marine"] + "?" + q, 40))["hourly"]
    model_h = hourly_map(d["time"], d["wave_height"])
    model_p = hourly_map(d["time"], d["wave_period"])

    obs_by_hour = {}
    for r in obs_rows:
        if r["wvht_m"] is None:
            continue
        hr = r["t"].astimezone(PT).replace(minute=0, second=0, microsecond=0)
        obs_by_hour.setdefault(hr, []).append(r)
    pairs = []
    for hr, rs in obs_by_hour.items():
        if hr in model_h:
            o = sum(x["wvht_m"] for x in rs) / len(rs)
            op = [x["dpd_s"] for x in rs if x["dpd_s"] is not None]
            pairs.append((o, model_h[hr], (sum(op) / len(op)) if op else None,
                          model_p.get(hr)))
    if len(pairs) < 100:
        print("only %d matched hours — not enough to judge" % len(pairs))
        return
    n = len(pairs)
    mo = sum(p[0] for p in pairs) / n
    mm = sum(p[1] for p in pairs) / n
    bias_pct = 100.0 * (mm - mo) / mo
    mae_ft = m_to_ft(sum(abs(p[1] - p[0]) for p in pairs) / n)
    def corr(idx_a, idx_b):
        xs = [p[idx_a] for p in pairs]; ys = [p[idx_b] for p in pairs]
        mx, my = sum(xs) / n, sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
        return num / den if den else 0.0
    per_pairs = [(p[2], p[3]) for p in pairs if p[2] is not None and p[3] is not None]
    per_mae = sum(abs(a - b) for a, b in per_pairs) / len(per_pairs) if per_pairs else float("nan")
    print("\nmodel vs buoy %s, %d matched hours (~%d days)" % (st, n, n // 24))
    print("  mean height:  buoy %.2fft   model %.2fft   bias %+.0f%%" %
          (m_to_ft(mo), m_to_ft(mm), bias_pct))
    print("  height MAE:   %.2fft      correlation r=%.2f" % (mae_ft, corr(0, 1)))
    print("  period MAE:   %.1fs (dominant vs model mean — expect a gap; different definitions)" % per_mae)
    scale = CONFIG["scoring"]["model_height_scale"]
    suggested = round(mo / mm, 2) if mm else 1.0
    print("  model_height_scale: current %.2f, data suggests %.2f" % (scale, suggested))
    breach = abs(suggested - scale) / scale > 0.10
    if breach:
        print("  → drift past 10%% of current scale: consider model_height_scale = %.2f" % suggested)
    else:
        print("  → within 10% of current scale; leave it")

    # The 20-year memory: every validation appends to a drift log, so 'has the
    # upstream model quietly changed?' is a chart, not a recollection.
    append_log(os.path.join(DATA, "drift_log.csv"),
               ["date", "n_hours", "buoy_ft", "model_ft", "bias_pct", "mae_ft",
                "r", "scale_current", "scale_suggested"],
               [{"date": now_pt().date().isoformat(), "n_hours": n,
                 "buoy_ft": round(m_to_ft(mo), 2), "model_ft": round(m_to_ft(mm), 2),
                 "bias_pct": round(bias_pct, 1), "mae_ft": round(mae_ft, 2),
                 "r": round(corr(0, 1), 3), "scale_current": scale,
                 "scale_suggested": suggested}], dry_run=False)
    if breach and getattr(args, "notify", False):
        notify_ops({"title": "The bell's ear is drifting 🔎",
                "message": "Swell model vs buoy %s: bias %+.0f%% over %d hours. "
                           "Current height scale %.2f, data suggests %.2f. "
                           "See data/drift_log.csv." % (st, bias_pct, n, scale, suggested),
                "priority": 4}, dry_run=False)


# =====================================================================
# cast — the swearing-in of a new bell
# =====================================================================

def cmd_cast(args):
    """Casting report for a zone: run its hindcast, judge the distribution
    against the honesty bands, and print the checklist a bell must clear
    before its tier flips from provisional to sworn. Judgment steps stay
    human; this automates the evidence."""
    zk = args.zone
    zc = CONFIG["zones"][zk]
    print("CASTING REPORT — Bell No.%s · %s (tier: %s)\n" % (
        zc.get("bell", {}).get("no", "?"), zc.get("bell", {}).get("name", zc["name"]),
        zc.get("tier", "unset")))
    args.days = getattr(args, "days", 365)
    cmd_hindcast(args)
    path = HINDCAST if zk == "A" else HINDCAST.replace(".csv", "_%s.csv" % zk)
    rows = _read_csv(path)
    scores = sorted(float(r["score"]) for r in rows)
    n = len(scores)
    pct7 = 100 * sum(1 for x in scores if x >= 7) / n
    pct8 = 100 * sum(1 for x in scores if x >= 8) / n
    print("\n  distribution: n=%d  med=%.1f  >=7 %.0f%%  >=8 %.0f%%" % (
        n, scores[n // 2], pct7, pct8))
    if 15 <= pct7 <= 35:
        print("  → distribution inside the honesty band (15-35%% >=7): curve fits this water")
    else:
        print("  → OUTSIDE the honesty band: tune cove_damage_factor before trusting alerts")
    print("\n  to swear this bell in (tier -> sworn):")
    print("   [ ] validate vs nearest live buoy (dive_alert.py validate pattern)")
    print("   [ ] a local diver confirms the water reads true")
    print("   [ ] first verdicts arrive on its feedback topic")
    print("   [ ] take_allowed flags verified against current regs")


# =====================================================================
# skill — the system grades its own forecasts
# =====================================================================

def cmd_skill(args):
    """We calibrate on archive (analysis) data but ALERT on forecasts, and the
    error between them at 12-48h lead was never measured until this. For every
    logged window old enough to have an archive truth (>2 days past), rebuild
    it from the archive at t_now=window_start and compare with what we
    predicted at each lead. Truth here = the model's own analysis — this
    measures FORECAST error, the thing confidence words claim to know."""
    rows = [r for r in _read_csv(LOG_PATH) if r.get("lead_h")]
    cutoff_lo = now_pt() - timedelta(days=45)
    cutoff_hi = now_pt() - timedelta(days=2)
    by_zone = {}
    for r in rows:
        try:
            ws = datetime.fromisoformat(r["window_start"])
            lead = float(r["lead_h"])
        except (ValueError, KeyError):
            continue
        if cutoff_lo <= ws <= cutoff_hi and lead >= 6:
            by_zone.setdefault(r.get("zone", "A"), []).append((ws, lead, float(r["score"]), r["window_kind"]))
    errs = []   # (zone, lead_bucket, predicted, truth)
    for zk, entries in by_zone.items():
        zc = CONFIG["zones"].get(zk)
        if not zc or len(entries) < 6:
            continue
        d0 = min(e[0] for e in entries).date() - timedelta(days=4)
        d1 = max(e[0] for e in entries).date()
        try:
            tzname = zc.get("tz", "America/Los_Angeles")
            tz = ZoneInfo(tzname)
            def arch(url, extra):
                q = dict(latitude=zc["lat"], longitude=zc["lon"], start_date=str(d0),
                         end_date=str(d1), timezone=tzname)
                q.update(extra)
                return json.loads(http_get(url + "?" + urllib.parse.urlencode(q), 120))["hourly"]
            mh = arch(CONFIG["sources"]["marine"],
                      {"hourly": "wave_height,wave_period,swell_wave_height,swell_wave_period,"
                                 "swell_wave_direction,wind_wave_height,wind_wave_period,"
                                 "wind_wave_direction"})
            wh = arch(CONFIG["sources"]["weather_archive"],
                      {"hourly": "wind_speed_10m,wind_direction_10m,precipitation,cloud_cover",
                       "wind_speed_unit": "kn", "precipitation_unit": "inch"})
        except Exception as e:
            print("  %s: archive unavailable (%s)" % (zk, str(e)[:60]))
            continue
        marine = {k: hourly_map(mh["time"], mh[k], tz) for k in mh if k != "time"}
        wx = {k: hourly_map(wh["time"], wh[k], tz) for k in wh if k != "time"}
        F = {"marine": Fetch("m", True, marine),
             "weather": Fetch("w", True, dict(wx, sunrise=[], sunset=[])),
             **{k: Fetch(k, False, error="skill") for k in
                ("ndbc_primary", "ndbc_offshore", "ndbc_spec", "tides", "sst", "chla", "kd490")}}
        for ws, lead, predicted, kind in entries:
            wnd = {"kind": kind, "start": ws, "end": ws + timedelta(hours=3 if kind == "dawn" else 2),
                   "label": "x", "key": "x"}
            truth, _, _, _ = score_window(compute_features(wnd, F, zc, ws))
            bucket = "12-24h" if lead < 24 else ("24-48h" if lead <= 48 else ">48h")
            errs.append((zk, bucket, predicted, truth))
    if not errs:
        print("SKILL: no windows old enough to grade yet")
        return
    print("FORECAST SKILL — predicted vs archive truth, %d graded windows" % len(errs))
    out_rows = []
    for bucket in ("12-24h", "24-48h", ">48h"):
        e = [(p, t) for _, b, p, t in errs if b == bucket]
        if not e:
            continue
        mae = sum(abs(p - t) for p, t in e) / len(e)
        bias = sum(p - t for p, t in e) / len(e)
        big = 100 * sum(1 for p, t in e if abs(p - t) >= 1.5) / len(e)
        print("  %-7s n=%-3d MAE %.2f  bias %+.2f  |err|>=1.5: %.0f%%" % (bucket, len(e), mae, bias, big))
        out_rows.append({"date": now_pt().date().isoformat(), "bucket": bucket,
                         "n": len(e), "mae": round(mae, 2), "bias": round(bias, 2),
                         "big_miss_pct": round(big, 1)})
    append_log(os.path.join(DATA, "skill_log.csv"),
               ["date", "bucket", "n", "mae", "bias", "big_miss_pct"], out_rows, dry_run=False)
    worst = max((r["mae"] for r in out_rows), default=0)
    if getattr(args, "notify", False) and worst >= 1.2:
        notify_ops({"title": "The bell's foresight is slipping 🔎",
                    "message": "Forecast MAE reached %.2f points. data/skill_log.csv has "
                               "the trend; confidence words may be overclaiming." % worst,
                    "priority": 3}, dry_run=False)


# =====================================================================
# report — the recursive ratchet: promises vs. what the water gave
# =====================================================================

PROMISE_ORDER = {"clear": 2, "fair": 1, "murk": 0}


def promise_tier(score):
    """What the alert's sensory line promised: >=8 'see it all' territory,
    >=7 'see your buddy'. Below 7 nothing was promised, nothing is graded."""
    return "clear" if score >= 8.0 else ("fair" if score >= 7.0 else None)


def verdict_tier(viz_ft):
    return "clear" if viz_ft >= 18 else ("fair" if viz_ft >= 8 else "murk")


def promise_kept(score, viz_ft):
    p = promise_tier(score)
    if p is None:
        return None
    return PROMISE_ORDER[verdict_tier(viz_ft)] >= PROMISE_ORDER[p]


def cmd_report(args):
    """The learning loop's ratchet: grade every promise against every verdict,
    surface miscalibration as a PROPOSAL for a human to act on — never a
    silent self-edit. Sparse noisy labels + selection bias make full autotune
    a trap: you only dive on days the bell praised, so it can never learn it
    was wrong about the days it dismissed. This names that, too."""
    dives = _read_csv(DIVE_LOG)
    hist = {r["window_key"]: r for r in _read_csv(LOG_PATH) if r.get("window_key")}
    graded, low_side = [], 0
    for d in dives:
        wk = None
        for tok in (d.get("notes") or "").split():
            if tok.startswith("window:"):
                wk = tok.split(":", 1)[1]
        row = hist.get(wk)
        if row is None:
            continue
        score, viz = float(row["score"]), float(d["viz_ft"])
        if score < 7.0:
            low_side += 1
        kept = promise_kept(score, viz)
        if kept is not None:
            graded.append((score, viz, kept))
    n = len(graded)
    print("THE BELL'S RECORD — %d graded promises, %d verdicts total" % (n, len(dives)))
    if n < 10:
        print("  the ratchet needs ~10 graded promises before it speaks (has %d)" % n)
        return
    for band, lo, hi in (("clear (8.0+)", 8.0, 99), ("fair (7.0-7.9)", 7.0, 8.0)):
        g = [k for s, v, k in graded if lo <= s < hi]
        if g:
            print("  promised %-15s n=%2d  kept %3.0f%%" % (band, len(g), 100 * sum(g) / len(g)))
    kept_rate = sum(k for _, _, k in graded) / n
    proposals = []
    if kept_rate < 0.5:
        proposals.append("Promises kept under half the time — the score runs hot. "
                         "Raise surf_penalty ~10% or min_score, rebuild hindcast, re-tune.")
    elif kept_rate > 0.9 and n >= 15:
        proposals.append("Promises kept over 90% — the bell may be too shy. "
                         "Consider easing surf_penalty ~5%.")
    if low_side == 0 and len(dives) >= 6:
        proposals.append("Every verdict comes from a day the bell praised — it can never "
                         "learn it was wrong about the days it dismissed. One dive on a "
                         "5-6 day would teach it more than five on ringing days.")
    for p in proposals:
        print("  PROPOSAL: " + p)
    if proposals and getattr(args, "notify", False):
        notify_ops({"title": "The bell's council 🔎",
                "message": ("%d promises graded, %.0f%% kept.\n" % (n, 100 * kept_rate))
                           + "\n".join(proposals[:2]),
                "priority": 3}, dry_run=False)


# =====================================================================
# scenario tests — these ARE the spec.
# =====================================================================

def _flat_series(start, hours, val):
    return {start + timedelta(hours=h): val for h in range(-hours, hours)}


def _mk_fetches(t0, swell_ft, per_s, dir_deg, wind_kn=4.0, precip_series=None,
                windwave_ft=0.0, ww_per=5.0, wind_dir=270.0):
    hours = 200
    m = {
        "wave_height": _flat_series(t0, hours, swell_ft / 3.28084),
        "wave_period": _flat_series(t0, hours, per_s),
        "wave_direction": _flat_series(t0, hours, dir_deg),
        "swell_wave_height": _flat_series(t0, hours, swell_ft / 3.28084),
        "swell_wave_period": _flat_series(t0, hours, per_s),
        "swell_wave_direction": _flat_series(t0, hours, dir_deg),
        "wind_wave_height": _flat_series(t0, hours, windwave_ft / 3.28084),
        "wind_wave_period": _flat_series(t0, hours, ww_per),
        "wind_wave_direction": _flat_series(t0, hours, dir_deg),
    }
    wx = {
        "wind_speed_10m": _flat_series(t0, hours, wind_kn),
        "wind_direction_10m": _flat_series(t0, hours, wind_dir),
        "wind_gusts_10m": _flat_series(t0, hours, wind_kn * 1.4),
        "precipitation": precip_series if precip_series is not None else _flat_series(t0, hours, 0.0),
        "sunrise": [t0.replace(hour=6) + timedelta(days=i) for i in range(5)],
        "sunset": [t0.replace(hour=19) + timedelta(days=i) for i in range(5)],
    }
    return {
        "marine": Fetch("marine", True, m),
        "weather": Fetch("weather", True, wx),
        "ndbc_primary": Fetch("ndbc_primary", True,
                              {"rows": [], "station": "46253",
                               "latest": {"t": t0.astimezone(timezone.utc),
                                          "wvht_m": swell_ft / 3.28084, "dpd_s": per_s,
                                          "mwd_deg": dir_deg}}),
        "ndbc_offshore": Fetch("ndbc_offshore", True,
                               {"rows": [], "station": "46086",
                                "latest": {"t": t0.astimezone(timezone.utc),
                                           "wvht_m": swell_ft / 3.28084, "dpd_s": per_s,
                                           "mwd_deg": dir_deg}}),
        "tides": Fetch("tides", True, [
            {"t": t0 + timedelta(hours=2), "ft": 1.0, "type": "L"},
            {"t": t0 + timedelta(hours=8), "ft": 5.0, "type": "H"},
            {"t": t0 + timedelta(hours=14), "ft": 1.5, "type": "L"},
            {"t": t0 + timedelta(hours=20), "ft": 5.5, "type": "H"},
            {"t": t0 + timedelta(hours=26), "ft": 1.0, "type": "L"},
            {"t": t0 + timedelta(hours=32), "ft": 5.0, "type": "H"},
            {"t": t0 + timedelta(hours=38), "ft": 1.5, "type": "L"},
            {"t": t0 + timedelta(hours=44), "ft": 5.5, "type": "H"},
        ]),
        "sst": Fetch("sst", True, {"c": 20.0}),
        "chla": Fetch("chla", True, {"mg_m3": 0.4}),
    }


def _window_at(t0, lead_h, kind="dawn", dur=3):
    st = t0 + timedelta(hours=lead_h)
    return {"kind": kind, "start": st, "end": st + timedelta(hours=dur),
            "label": "%s %s" % (st.strftime("%a"), kind),
            "key": "%s-%s" % (st.strftime("%Y%m%dT%H%M"), kind)}


def cmd_test(args):
    zc = CONFIG["zones"]["A"]
    t0 = datetime(2026, 8, 10, 6, 0, tzinfo=PT)
    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print("  [%s] %s %s" % (status, name, detail))
        if not cond:
            failures.append(name)

    print("scenario tests (the spec):")

    # (a) 5 dry days + small 16s S groundswell + calm → >=8.5, Tier 2/3 voice
    f = _mk_fetches(t0, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4)
    w = _window_at(t0, 24)
    feats = compute_features(w, f, zc, t0)
    sa, _, _, _ = score_window(feats)
    check("(a) groundswell+calm >= 8.5", sa >= 8.5, "score=%.1f" % sa)
    check("(a) tier 2/3 voice", tier_for(sa) >= 2, "tier=%d" % tier_for(sa))

    # (b) 3ft at 7s windswell → <= 4
    f = _mk_fetches(t0, swell_ft=3.0, per_s=7, dir_deg=200, wind_kn=8)
    feats = compute_features(w, f, zc, t0)
    sb, _, _, _ = score_window(feats)
    check("(b) 3ft@7s windswell <= 4", sb <= 4.0, "score=%.1f" % sb)

    # (c) 0.3" rain 24h pre-window → <= 4 and flagged
    rain = _flat_series(t0, 200, 0.0)
    rain[w["start"] - timedelta(hours=24)] = 0.3
    f = _mk_fetches(t0, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4, precip_series=rain)
    feats = compute_features(w, f, zc, t0)
    sc_, _, cap_reason, flags = score_window(feats)
    check("(c) post-rain capped <= 4", sc_ <= 4.0, "score=%.1f" % sc_)
    check("(c) post-rain flagged", "post-rain" in flags and cap_reason is not None,
          "cap=%s" % cap_reason)

    # (d) same swell, exposed vs shadowed direction → materially different
    f1 = _mk_fetches(t0, swell_ft=3.0, per_s=10, dir_deg=190, wind_kn=4)
    f2 = _mk_fetches(t0, swell_ft=3.0, per_s=10, dir_deg=290, wind_kn=4)
    s_exp, _, _, _ = score_window(compute_features(w, f1, zc, t0))
    s_shad, _, _, _ = score_window(compute_features(w, f2, zc, t0))
    check("(d) exposure matters >= 1.0", s_shad - s_exp >= 1.0,
          "exposed=%.1f shadowed=%.1f" % (s_exp, s_shad))

    # (e) same conditions at 40h vs 6h → same score, lower confidence at 40h, no alert at 6h
    f = _mk_fetches(t0, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4)
    w40, w6 = _window_at(t0, 40), _window_at(t0, 6)
    ft40 = compute_features(w40, f, zc, t0)
    ft6 = compute_features(w6, f, zc, t0)
    s40, _, _, _ = score_window(ft40)
    s6, _, _, _ = score_window(ft6)
    _, c40, _, _ = confidence(f, ft40, t0)
    cw40, _, _, _ = confidence(f, ft40, t0)
    cw6, _, _, _ = confidence(f, ft6, t0)
    check("(e) same score 40h vs 6h", abs(s40 - s6) < 0.05, "%.2f vs %.2f" % (s40, s6))
    rank = {"low": 0, "medium": 1, "high": 2}
    check("(e) lower confidence at 40h", rank[cw40] < rank[cw6], "%s vs %s" % (cw40, cw6))
    tides = f["tides"].data
    sc40 = {"zone": "A", "w": w40, "score": s40, "feats": ft40, "conf": cw40,
            "conf_notes": [], "entries": ["Shaw's Cove", "Divers Cove"],
            "tide_fyi": "", "band": "mid"}
    sc6 = dict(sc40, w=w6, score=s6)
    act, _ = decide_alert([sc6], t0, {})
    check("(e) no alert at 6h lead", act is None, "action=%s" % act)
    act, _ = decide_alert([sc40], t0, {})
    check("(e) alert at 40h lead", act == "alert", "action=%s" % act)

    # (f) reserve zone → no night windows, no take tips, ever
    wins = build_windows(f["weather"], t0, zc)
    check("(f) no night windows in reserve", all(x["kind"] != "night" for x in wins),
          "kinds=%s" % sorted(set(x["kind"] for x in wins)))
    tipset = [t for site in CONFIG["tips"].values() for t in site]
    check("(f) no take-lane tips in library", all(t["lane"] != "take" for t in tipset))
    st = {}
    tip = select_tip(["Shaw's Cove"], 8.5, 2.0, "mid", t0, st)
    check("(f) tip selected is non-take", tip is not None and "lobster" not in tip.lower())

    # (g) day 3 of a steady 7.4 streak, no material change → no alert
    state = {"streak": {"score": 7.4, "entries": ["Shaw's Cove", "Divers Cove"],
                        "window_key": "x", "since": t0.isoformat()}}
    sc_steady = dict(sc40, score=7.4)
    act, _ = decide_alert([sc_steady], t0, state)
    check("(g) steady streak stays quiet", act is None, "action=%s" % act)
    # and a material drop does fire a downgrade
    state = {"streak": {"score": 8.1, "entries": ["Shaw's Cove", "Divers Cove"],
                        "window_key": "x", "since": t0.isoformat()}}
    sc_drop = dict(sc40, score=6.4)
    act, payload = decide_alert([sc_drop], t0, state)
    check("(g2) material drop fires downgrade", act == "downgrade", "action=%s" % act)

    # (h) 7.2 window → Tier 1 voice, no Tier 2 language
    st = {}
    hook = hook_for(7.2, st)
    check("(h) 7.2 → tier 1", tier_for(7.2) == 1 and hook in CONFIG["voice"]["tier1_hooks"],
          "hook=%r" % hook)
    msg = render_alert(w, 7.2, ft40, "high", [], ["Shaw's Cove"], "tide 2.1ft rising", "tip", st)
    t2_words = CONFIG["voice"]["tier2_hooks"] + CONFIG["voice"]["tier3_hooks"]
    check("(h) no tier-2 language in message",
          all(x.lower() not in (msg["title"] + msg["message"]).lower() for x in t2_words))

    # (i) viz phrase tracks the anchored scale
    check("(i) viz phrase anchored", viz_phrase(9.3) == "25ft+ viz"
          and viz_phrase(7.2) == "12–15ft viz" and viz_phrase(4.0) == "single-digit viz")

    # (j) feedback verdict round-trip: button body -> dive_log row semantics
    body = "clear|20260814T0600-dawn"
    verdict, wk = body.split("|", 1)
    check("(j) feedback parses", verdict in FEEDBACK_MAP
          and datetime.strptime(wk.split("-")[0], "%Y%m%dT%H%M").year == 2026)

    # (k) superlatives never invent: same-score history yields no brag
    hist_flat = [(t0 - timedelta(days=i), 8.5) for i in range(1, 100)]
    check("(k) no false brag", superlative(8.5, t0, hist=hist_flat) is None)
    hist_low = [(t0 - timedelta(days=i), 5.0) for i in range(1, 100)]
    check("(k2) real standout brags", superlative(8.5, t0, hist=hist_low) is not None)
    check("(k3) tier1 never brags", superlative(7.5, t0, hist=hist_low) is None)

    # (l) Satellite clarity must NOT move the score — it decorrelates in ~3 days
    # and arrives ~10 days late (r=0.018 at that lag). Scoring it was fake
    # precision. It is still fetched and logged for future backtesting.
    f_kd = _mk_fetches(t0, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4)
    s_no, _, _, _ = score_window(compute_features(w, f_kd, zc, t0))
    f_kd["kd490"] = Fetch("kd490", True, {"m1": 0.30, "age_d": 2})
    s_murk, _, _, _ = score_window(compute_features(w, f_kd, zc, t0))
    check("(l) stale satellite clarity does not move the score", s_no == s_murk,
          "no-data=%.1f murky=%.1f" % (s_no, s_murk))

    # (l2) LIGHT does move it — cloud cover is genuinely forecastable, unlike viz.
    ft_sun = dict(compute_features(w, f_kd, zc, t0), cloud_pct=5)
    ft_gloom = dict(compute_features(w, f_kd, zc, t0), cloud_pct=95)
    s_sun, _, _, _ = score_window(ft_sun)
    s_gloom, _, _, _ = score_window(ft_gloom)
    check("(l2) overcast costs a little, not a lot", 0.4 <= (s_sun - s_gloom) <= 1.3,
          "sun=%.1f gloom=%.1f" % (s_sun, s_gloom))

    # (o) REGRESSION: real windows start off-the-hour (sunrise-derived). Hourly
    # series are keyed on the hour, so lagged features must snap to the hour or
    # every lookup misses and returns zero. This shipped broken once: rain,
    # wind history and swell history were all silently dead on live data while
    # on-the-hour fixtures passed. Any lagged feature added later must be
    # exercised at an offset start.
    f_off = _mk_fetches(t0, swell_ft=2.5, per_s=11, dir_deg=200, wind_kn=12)
    w_on = _window_at(t0, 24)
    w_off = dict(w_on)
    w_off["start"] = w_on["start"] + timedelta(minutes=37)
    w_off["end"] = w_on["end"] + timedelta(minutes=37)
    ft_on, ft_off = compute_features(w_on, f_off, zc, t0), compute_features(w_off, f_off, zc, t0)
    for key in ("wind_energy_48h", "swell_energy_72h"):
        check("(o) %s nonzero off-the-hour" % key, (ft_off[key] or 0) > 0,
              "on=%.0f off=%.0f" % (ft_on[key] or 0, ft_off[key] or 0))
    rain_off = _flat_series(t0, 200, 0.0)
    rain_off[floor_hour(w_off["start"]) - timedelta(hours=20)] = 0.35
    f_rain = _mk_fetches(t0, swell_ft=1.2, per_s=16, dir_deg=190, wind_kn=3,
                         precip_series=rain_off)
    ft_rain = compute_features(w_off, f_rain, zc, t0)
    s_rain, _, cap_r, _ = score_window(ft_rain)
    check("(o2) rain rule fires on an off-the-hour window", s_rain <= 4.0 and cap_r,
          "score=%.1f cap=%s" % (s_rain, cap_r))

    # (n) THE CREDIBILITY RULE: no message may promise visibility. Measured
    # 2026-08-11, this score has no relationship to observed clarity, so a viz
    # claim would be invention. The alert must say the wildcard out loud.
    viz_words = ("viz", "visibility", "see the bottom", "ft of vis")
    for sc_ in (7.2, 8.6, 9.4):
        txt = sensory_phrase(sc_).lower()
        check("(n) %.1f setup phrase makes no viz claim" % sc_,
              not any(v in txt for v in viz_words), txt)
    ft_mid = {"dmg_parts": {"hgt_ft": 1.3, "per_s": 15, "dir": 195}, "dry_hours": 72,
              "wind_window_max_kn": 5, "cloud_pct": 12, "damage": 1.1, "kd490": None}
    msg_t2 = render_alert(_window_at(t0, 24), 8.6, ft_mid, "high", [], ["Shaw's Cove"],
                          "slack at 9am", None, {}, sst_c=None)
    check("(n2) tier-2 alert names the wildcard",
          CONFIG["voice"]["wildcard"] in msg_t2["message"])

    # (p) THE CONJUNCTION GATE: perfection is an AND. Every axis must pass, and
    # anything unknown fails — silence beats a false promise.
    sst_ok = 20.0
    ok, fails = perfect_gate(ft_mid, 8.6, sst_ok)
    check("(p) all axes aligned -> gate passes", ok, "failed=%s" % fails)
    for axis, mutate in (("flat", {"damage": 9.0}), ("glass", {"wind_window_max_kn": 12}),
                         ("dry", {"dry_hours": 20}), ("sun", {"cloud_pct": 95})):
        bad = dict(ft_mid); bad.update(mutate)
        ok_b, fails_b = perfect_gate(bad, 8.6, sst_ok)
        check("(p) %s alone breaks the gate" % axis, (not ok_b) and axis in fails_b,
              "failed=%s" % fails_b)
    ok_u, fails_u = perfect_gate(dict(ft_mid, cloud_pct=None), 8.6, sst_ok)
    check("(p2) unknown axis fails closed", not ok_u, "failed=%s" % fails_u)
    ok_s, _ = perfect_gate(ft_mid, 7.4, sst_ok)
    check("(p3) weighted score must agree too", not ok_s)
    perfect_msg = render_alert(_window_at(t0, 24), 8.6, ft_mid, "high", [],
                               ["Shaw's Cove"], "slack at 9am", None, {}, sst_c=sst_ok)
    check("(p4) gate pass uses the reserved hook",
          CONFIG["voice"]["perfect_hook"] in perfect_msg["title"], perfect_msg["title"])
    check("(p5) a gate pass still doesn't promise viz",
          not any(v in perfect_msg["message"].lower() for v in viz_words))

    # (t) VIRALITY IS SCARCITY: the invite line rides ONLY the gate alert.
    os.environ["NTFY_TOPIC"] = "laguna-test-topic"
    try:
        m_gate = render_alert(_window_at(t0, 24), 8.6, ft_mid, "high", [],
                              ["Shaw's Cove"], "slack at 9am", "a tip", {}, sst_c=sst_ok)
        check("(t) gate alert carries its own invite",
              "thedivebell.com" in m_gate["message"], m_gate["message"].split("\n")[-1])
        check("(t2) invite replaces the tip, keeping the line budget",
              "Tip:" not in m_gate["message"] and len(m_gate["message"].split("\n")) <= 3)
        m_norm = render_alert(_window_at(t0, 24), 7.2, ft40, "high", [],
                              ["Shaw's Cove"], "slack at 9am", "a tip", {}, sst_c=sst_ok)
        check("(t3) ordinary alerts never carry the invite",
              "thedivebell.com" not in m_norm["message"] and "Tip: a tip" in m_norm["message"])
    finally:
        del os.environ["NTFY_TOPIC"]

    # (m) weekly digest: exactly one calendar week, no duplicate weekday, and
    # a quiet week must not borrow tier-2 language it hasn't earned.
    def _mkw(day, sc, base):
        stt = base + timedelta(days=day, hours=1)
        return {"w": {"start": stt, "end": stt + timedelta(hours=3), "kind": "dawn",
                      "label": "%s dawn" % stt.strftime("%a"), "key": "d%d" % day},
                "score": sc, "entries": ["Shaw's Cove", "Divers Cove"],
                "tide_fyi": "rising tide", "feats": {}}
    wk = [_mkw(i, s, t0) for i, s in enumerate([5.9, 6.2, 5.4, 6.6, 6.1, 5.8, 6.3, 7.9])]
    dg = render_digest(wk, t0, {})
    strip = dg["message"].split("\n")[0]
    days = [x.split()[0] for x in strip.split(" · ")]
    check("(m) digest is one week", len(days) == CONFIG["alerting"]["digest_days"],
          "%d days" % len(days))
    check("(m2) no duplicate weekday", len(set(days)) == len(days), strip)
    quiet_txt = (dg["title"] + dg["message"]).lower()
    check("(m3) quiet week stays honest",
          all(h.lower() not in quiet_txt for h in CONFIG["voice"]["tier2_hooks"]))
    hot = [_mkw(i, s, t0) for i, s in enumerate([5.9, 8.4, 5.4, 6.6, 6.1, 5.8, 6.3])]
    check("(m4) good week names the day", "Tue" in render_digest(hot, t0, {})["title"],
          render_digest(hot, t0, {})["title"])
    # (m5) a gate day in the outlook takes the headline over a higher plain score
    gated_wk = [_mkw(i, s, t0) for i, s in enumerate([5.9, 8.9, 5.4, 8.2, 6.1, 5.8, 6.3])]
    gated_wk[3]["gate"] = True   # Thu gates at 8.2; Tue is higher at 8.9 but doesn't
    dgg = render_digest(gated_wk, t0, {})
    check("(m5) gate day owns the digest headline",
          "✨" in dgg["title"] and "Thu" in dgg["title"], dgg["title"])
    check("(m5b) strip marks the gate day", "Thu 8.2✨" in dgg["message"],
          dgg["message"].split("\n")[0])

    # (u) WIND DIRECTION: a knot offshore is not a knot onshore.
    f_on = _mk_fetches(t0, swell_ft=1.5, per_s=15, dir_deg=195, wind_kn=12, wind_dir=225)
    f_off = _mk_fetches(t0, swell_ft=1.5, per_s=15, dir_deg=195, wind_kn=12, wind_dir=45)
    s_on, _, _, _ = score_window(compute_features(w, f_on, zc, t0))
    s_off, _, _, _ = score_window(compute_features(w, f_off, zc, t0))
    check("(u) 12kt offshore beats 12kt onshore", s_off - s_on >= 1.0,
          "offshore=%.1f onshore=%.1f" % (s_off, s_on))
    f_cap_on = _mk_fetches(t0, swell_ft=1.5, per_s=15, dir_deg=195, wind_kn=16, wind_dir=225)
    f_cap_off = _mk_fetches(t0, swell_ft=1.5, per_s=15, dir_deg=195, wind_kn=16, wind_dir=45)
    s_c_on, _, cap_on, _ = score_window(compute_features(w, f_cap_on, zc, t0))
    s_c_off, _, cap_off, _ = score_window(compute_features(w, f_cap_off, zc, t0))
    check("(u2) 16kt onshore caps at 5; Santa Ana doesn't",
          cap_on is not None and s_c_on <= 5.0 and cap_off is None and s_c_off > 6.5,
          "on=%.1f(%s) off=%.1f(%s)" % (s_c_on, cap_on, s_c_off, cap_off))

    # (v) PERIOD REACHES DEEPER THAN CHOP: orbital survival physics.
    check("(v) chop dies with depth, groundswell doesn't",
          surge_at_depth(25, 7) < 0.6 < 0.85 < surge_at_depth(25, 16),
          "7s@25ft=%.2f 16s@25ft=%.2f" % (surge_at_depth(25, 7), surge_at_depth(25, 16)))
    check("(v2) shallow sites eat the chop", surge_at_depth(15, 7) > surge_at_depth(30, 7))

    # (w) FIRST FLUSH: dry-season rain caps harder than winter rain.
    rain_s = _flat_series(t0, 200, 0.0)
    rain_s[floor_hour(w["start"]) - timedelta(hours=24)] = 0.3
    f_ff = _mk_fetches(t0, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4, precip_series=rain_s)
    s_ff, _, _, fl_ff = score_window(compute_features(w, f_ff, zc, t0))   # t0 = August
    t0w = datetime(2026, 2, 10, 6, 0, tzinfo=PT)
    ww_feb = _window_at(t0w, 24)
    rain_w = _flat_series(t0w, 200, 0.0)
    rain_w[floor_hour(ww_feb["start"]) - timedelta(hours=24)] = 0.3
    f_wr = _mk_fetches(t0w, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4, precip_series=rain_w)
    s_wr, _, _, fl_wr = score_window(compute_features(ww_feb, f_wr, zc, t0w))
    check("(w) August rain is first flush, cap 3", s_ff <= 3.0 and "first-flush" in fl_ff,
          "score=%.1f flags=%s" % (s_ff, fl_ff))
    check("(w2) February rain caps at 4, no flush flag",
          s_wr <= 4.0 and s_wr > 3.0 and "first-flush" not in fl_wr,
          "score=%.1f" % s_wr)

    # (x) SPRING TIDES MIX: bigger swing, small honest penalty.
    f_tide = _mk_fetches(t0, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4)
    f_spring = _mk_fetches(t0, swell_ft=1.5, per_s=16, dir_deg=190, wind_kn=4)
    f_spring["tides"] = Fetch("tides", True, [
        {"t": w["start"] - timedelta(hours=6), "ft": -1.5, "type": "L"},
        {"t": w["start"] + timedelta(hours=1), "ft": 7.0, "type": "H"},
        {"t": w["start"] + timedelta(hours=7), "ft": -1.0, "type": "L"}])
    s_neap, _, _, _ = score_window(compute_features(w, f_tide, zc, t0))
    s_spring, _, _, _ = score_window(compute_features(w, f_spring, zc, t0))
    check("(x) spring tide costs a little", 0.2 <= (s_neap - s_spring) <= 0.8,
          "neap=%.1f spring=%.1f" % (s_neap, s_spring))

    # (q) THE MAGIC MUST NOT BE SWALLOWED: a gate flip inside a steady streak
    # is material change of the highest order and fires the LOUD alert.
    state_q = {"streaks": {"A": {"score": 7.4, "entries": ["Shaw's Cove", "Divers Cove"],
                                 "window_key": "x", "since": t0.isoformat(), "gate": False}}}
    sc_gate = dict(sc40, score=7.6, gate=True)   # +0.2 — below material_change alone
    act, payload = decide_alert([sc_gate], t0, state_q)
    check("(q) gate flip breaks through streak, loud", act == "alert", "action=%s" % act)
    act2, _ = decide_alert([sc_gate], t0, state_q)
    check("(q2) gate already-known stays quiet", act2 is None, "action=%s" % act2)
    state_leg = {"streak": {"score": 7.4, "entries": ["Shaw's Cove", "Divers Cove"],
                            "window_key": "x", "since": t0.isoformat()}}
    act3, _ = decide_alert([dict(sc40, score=7.4)], t0, state_leg)
    check("(q3) legacy single-streak state migrates silently",
          act3 is None and "streak" not in state_leg and "A" in state_leg["streaks"])

    # (r) CHAOS: every source returns HTML garbage at once. The pipeline must
    # degrade to a low-confidence guess and never raise. Fail soft, never silent.
    fx_chaos, src_chaos = fetch_all(zc, offline=True, fixture_set="chaos")
    check("(r) all chaos fetchers fail closed", all(not f_.ok for f_ in fx_chaos.values()),
          str([k for k, f_ in fx_chaos.items() if f_.ok]))
    check("(r2) corrupt meta.json doesn't crash", src_chaos.fixture_now() is None)
    try:
        scored_chaos = score_zone("A", zc, fx_chaos, t0)
        crash = None
    except Exception as e:
        scored_chaos, crash = [], e
    check("(r3) zero-data scoring survives", crash is None and len(scored_chaos) > 0,
          "crash=%r windows=%d" % (crash, len(scored_chaos)))
    if scored_chaos:
        check("(r4) zero-data confidence is low", scored_chaos[0]["conf"] == "low",
              scored_chaos[0]["conf"])
        check("(r5) zero-data never passes the gate",
              not any(s["gate"] for s in scored_chaos))
        check("(r6) swell-dead rule would exit nonzero",
              not (fx_chaos["marine"].ok or fx_chaos["ndbc_primary"].ok))

    # (z) THE 20-YEAR RULE: a dying source announces itself exactly once.
    st_z = {}
    fx_dead = {"ndbc_primary": Fetch("ndbc_primary", False, error="gone"),
               "marine": Fetch("marine", True, {})}
    fired = []
    for i in range(SENTINEL_RUNS + 3):
        fired += sentinel_update(st_z, fx_dead, "A")
    check("(z) outage announced exactly once at run %d" % SENTINEL_RUNS,
          fired == ["ndbc_primary"], "fired=%s" % fired)
    fx_back = {"ndbc_primary": Fetch("ndbc_primary", True, {"x": 1}),
               "marine": Fetch("marine", True, {})}
    sentinel_update(st_z, fx_back, "A")
    check("(z2) recovery resets the counter", st_z["src_fail"]["A:ndbc_primary"] == 0)
    fired2 = []
    for i in range(SENTINEL_RUNS):
        fired2 += sentinel_update(st_z, fx_dead, "A")
    check("(z3) a second outage announces again", fired2 == ["ndbc_primary"])
    # (z4) MULTI-ZONE: one zone's healthy source must not erase another's outage
    st_mz = {}
    for i in range(SENTINEL_RUNS):
        f_c = sentinel_update(st_mz, fx_dead, "C")
        sentinel_update(st_mz, fx_back, "A")
    check("(z4) zone C outage survives zone A health", f_c == ["ndbc_primary"],
          "fired=%s" % f_c)

    # (bb) THE RATCHET grades promises against verdicts, and only where a
    # promise was actually made.
    check("(bb) 8.6 + saw-it-all = kept", promise_kept(8.6, 25.0) is True)
    check("(bb2) 8.6 + saw-buddy = broken", promise_kept(8.6, 12.0) is False)
    check("(bb3) 7.2 + saw-buddy = kept", promise_kept(7.2, 12.0) is True)
    check("(bb4) no promise below 7, nothing graded", promise_kept(6.4, 25.0) is None)

    # (dd) THE BAY MECHANISM: a zone's height scale must damp both the surf
    # term and the turbidity memory — sub-grid shelter the model can't see.
    zc_bay = dict(zc, marine_height_scale=0.5)
    f_bay = _mk_fetches(t0, swell_ft=3.0, per_s=10, dir_deg=190, wind_kn=4)
    ft_open = compute_features(w, f_bay, zc, t0)
    ft_shel = compute_features(w, f_bay, zc_bay, t0)
    check("(dd) height scale damps damage ~4x",
          0.2 <= ft_shel["damage"] / ft_open["damage"] <= 0.3,
          "open=%.1f bay=%.1f" % (ft_open["damage"], ft_shel["damage"]))
    check("(dd2) and the turbidity memory too",
          0.2 <= ft_shel["swell_energy_72h"] / ft_open["swell_energy_72h"] <= 0.3)

    # (ee) THE DYNAMIC ANCHOR: live buoy truth corrects the forecast series,
    # clamped, and defaulting to 1.0 on any doubt.
    f_an = _mk_fetches(t0, swell_ft=2.0, per_s=12, dir_deg=200, wind_kn=5)
    model_h = f_an["marine"].data["wave_height"]
    rows_13 = [{"t": (t0 - timedelta(hours=hh)).astimezone(timezone.utc),
                "wvht_m": model_h[t0 - timedelta(hours=hh)] * 1.3,
                "dpd_s": 12.0, "mwd_deg": 200.0} for hh in range(1, 30)]
    f_an["ndbc_primary"] = Fetch("ndbc_primary", True,
                                 {"rows": rows_13, "station": "46253",
                                  "latest": rows_13[0]})
    a13 = buoy_anchor(f_an, t0)
    check("(ee) buoy 1.3x model -> anchor ~1.3", 1.25 <= a13 <= 1.35, "a=%.3f" % a13)
    rows_9 = [dict(r, wvht_m=r["wvht_m"] * 9) for r in rows_13]
    f_an["ndbc_primary"] = Fetch("ndbc_primary", True,
                                 {"rows": rows_9, "station": "46253", "latest": rows_9[0]})
    check("(ee2) wild ratios clamp at 1.6", buoy_anchor(f_an, t0) == 1.6)
    f_an["ndbc_primary"] = Fetch("ndbc_primary", False, error="gone")
    check("(ee3) no buoy -> anchor 1.0", buoy_anchor(f_an, t0) == 1.0)
    ft_a = compute_features(w, _mk_fetches(t0, swell_ft=2.0, per_s=12, dir_deg=200), zc, t0, 1.3)
    ft_b = compute_features(w, _mk_fetches(t0, swell_ft=2.0, per_s=12, dir_deg=200), zc, t0, 1.0)
    check("(ee4) anchored damage scales ~1.69x",
          1.6 <= ft_a["damage"] / ft_b["damage"] <= 1.8,
          "ratio=%.2f" % (ft_a["damage"] / ft_b["damage"]))

    # (ff) SMS layer: quiet hours by the bell's clock; keywords name bells;
    # no env means no sends, ever.
    zc_hi = CONFIG["zones"]["P"]
    noon_hi = datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("Pacific/Honolulu"))
    late_hi = datetime(2026, 8, 17, 22, 0, tzinfo=ZoneInfo("Pacific/Honolulu"))
    check("(ff) noon in Maui is sendable", sms_quiet_ok(zc_hi, noon_hi))
    check("(ff2) 10pm in Maui is not", not sms_quiet_ok(zc_hi, late_hi))
    kw = bell_keyword_map()
    check("(ff3) keywords route to bells", kw.get("LAGUNA") == "A" and kw.get("MAUI") == "P"
          and kw.get("JOLLA") == "D" and kw.get("BARBARA") == "I"
          and all(len(k) >= 4 for k in kw), str(sorted(kw))[:70])
    check("(ff4) no env, no subscribers", sms_subscribers() == {})

    # fixture suite: degraded + disagreement
    print("fixture tests:")
    for name in ("degraded", "disagreement"):
        d = os.path.join(FIXTURES, name)
        if not os.path.isdir(d):
            check("fixture set %s present" % name, False, "(record + derive fixtures first)")
            continue
        fetches, src = fetch_all(zc, offline=True, fixture_set=name)
        fx_now = src.fixture_now() or t0
        swell_ok = fetches["marine"].ok or fetches["ndbc_primary"].ok
        check("%s: pipeline survives" % name, swell_ok is not None)
        scored = score_zone("A", zc, fetches, fx_now)
        check("%s: windows scored" % name, len(scored) > 0, "%d windows" % len(scored))
        if name == "degraded":
            missing = [k for k, ff in fetches.items() if not ff.ok]
            check("degraded: gap named", len(missing) > 0, "missing=%s" % missing)
            if scored:
                check("degraded: confidence not high", scored[0]["conf"] != "high",
                      "conf=%s" % scored[0]["conf"])
        if name == "disagreement" and scored:
            notes = scored[0]["conf_notes"]
            check("disagreement: named in notes", any("buoy" in n for n in notes),
                  "notes=%s" % notes)

    if failures:
        print("FAILURES: %s" % ", ".join(failures))
        sys.exit(1)
    print("all tests passed")


# =====================================================================
# main
# =====================================================================

def main():
    ap = argparse.ArgumentParser(description="Laguna dive conditions alert")
    sub = ap.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--offline", action="store_true")
    p_run.add_argument("--fixtures", default="normal")
    p_run.add_argument("--brief", action="store_true")
    p_run.add_argument("--weekly", action="store_true",
                       help="send the week-ahead digest regardless of threshold")
    p_log = sub.add_parser("log-dive")
    p_log.add_argument("--site", required=True)
    p_log.add_argument("--viz", required=True, type=float)
    p_log.add_argument("--surge", required=True, choices=["low", "med", "high"])
    p_log.add_argument("--notes", default="")
    p_hc = sub.add_parser("hindcast")
    p_hc.add_argument("--days", type=int, default=180)
    p_hc.add_argument("--zone", default="A")
    p_cast = sub.add_parser("cast")
    p_cast.add_argument("--zone", required=True)
    p_cast.add_argument("--days", type=int, default=365)
    sub.add_parser("backtest")
    sub.add_parser("test")
    sub.add_parser("record-fixtures")
    p_val = sub.add_parser("validate")
    p_val.add_argument("--notify", action="store_true")
    p_val.add_argument("--zone", default="A")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--notify", action="store_true")
    p_skill = sub.add_parser("skill")
    p_skill.add_argument("--notify", action="store_true")
    sub.add_parser("ingest")
    sub.add_parser("share")
    p_setup = sub.add_parser("setup")
    p_setup.add_argument("--github", action="store_true")
    p_setup.add_argument("--topic", default=None)
    args = ap.parse_args()
    for _f in ("brief", "weekly", "offline", "dry_run", "fixtures"):
        if not hasattr(args, _f):
            setattr(args, _f, False if _f != "fixtures" else "normal")
    if args.cmd is None:
        args = ap.parse_args(["run", "--dry-run"])

    if args.cmd == "run":
        try:
            cmd_run(args)
        except SystemExit:
            raise
        except Exception:
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            ping_health(ok=False, msg=tb[-4000:])
            sys.exit(1)
    elif args.cmd == "log-dive":
        cmd_log_dive(args)
    elif args.cmd == "hindcast":
        cmd_hindcast(args)
    elif args.cmd == "backtest":
        cmd_backtest(args)
    elif args.cmd == "test":
        cmd_test(args)
    elif args.cmd == "record-fixtures":
        cmd_record_fixtures(args)
    elif args.cmd == "validate":
        cmd_validate(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "skill":
        cmd_skill(args)
    elif args.cmd == "cast":
        cmd_cast(args)
    elif args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "share":
        cmd_share(args)
    elif args.cmd == "ingest":
        state = load_state()
        ingest_feedback(state, dry_run=False)
        save_state(state, dry_run=False)


if __name__ == "__main__":
    main()
