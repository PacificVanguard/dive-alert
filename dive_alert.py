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
                {"name": "Crescent Bay", "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.1, "tide": "any",
                 "entry": "sand walk-in; deep channel mid-beach",
                 "note": "Seal Rock reef on the north end; longest swim to structure"},
                {"name": "Shaw's Cove", "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.3, "tide": "any",
                 "entry": "stairs to sand; easy walk-in",
                 "note": "most protected cove; west reef crevice at mid-high tide"},
                {"name": "Divers Cove", "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.2, "tide": "any",
                 "entry": "sand walk-in between reefs",
                 "note": "short swim to kelp; garibaldi central"},
                {"name": "Fisherman's Cove", "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.2, "tide": "mid-high",
                 "entry": "narrow sand channel between rock shelves",
                 "note": "channel gets shallow and grabby at low tide"},
                {"name": "Picnic Beach (Heisler)", "take_allowed": False, "creek_adjacent": False,
                 "shelter": 0.0, "tide": "high",
                 "entry": "rocky shelf; ankle-twister at low tide",
                 "note": "best structure close in; enter north of the point"},
                {"name": "Wood's Cove", "take_allowed": False, "creek_adjacent": False,
                 "shelter": -0.1, "tide": "mid",
                 "entry": "stairs; rock outcrops both sides",
                 "note": "boulder field holds fish; watch the shorebreak slot"},
                {"name": "Cleo Street barge", "take_allowed": False, "creek_adjacent": False,
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
                {"name": "Aliso Beach", "take_allowed": False, "creek_adjacent": True,
                 "shelter": -0.2, "tide": "any",
                 "entry": "steep sand; shorebreak-prone", "note": "Aliso Creek outflow — 96h rain rule"},
                {"name": "Thousand Steps", "take_allowed": True, "creek_adjacent": False,
                 "shelter": 0.0, "tide": "mid-high",
                 "entry": "long stairs, sand entry", "note": "VERIFY take boundary before enabling"},
            ],
        },
        "C": {
            "name": "Dana Point",
            "enabled": False,
            "lat": 33.460, "lon": -117.714,
            "exposure": [(0, 0.10), (90, 0.20), (157, 0.70), (180, 1.00),
                         (220, 1.00), (245, 0.90), (270, 0.55), (285, 0.35),
                         (300, 0.25), (330, 0.15), (360, 0.10)],
            "sites": [
                {"name": "Salt Creek", "take_allowed": False, "creek_adjacent": True,
                 "shelter": 0.0, "tide": "any",
                 "entry": "long beach walk", "note": "Dana Point SMCA — VERIFY current take rules"},
                {"name": "Dana Point Harbor breakwall (outside)", "take_allowed": True,
                 "creek_adjacent": False, "shelter": 0.2, "tide": "any",
                 "entry": "boat or long swim", "note": "VERIFY take rules + season before enabling"},
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
        # call the setup, nobody can call the water.
        "wildcard": "Water clarity is the wildcard — nobody can forecast that.",
        # Only ever used when the conjunction gate passes.
        "perfect_hook": "Everything just lined up",
        "perfect_line": ("Flat, dry, sunny, warm — every knowable thing has lined up. "
                         "The water's the only unknown left, and this is the day to gamble."),
        # SST °F -> what wetsuit to throw in the car
        "wetsuit": [(58, "5mm-and-hood water"), (64, "solid 4/3 water"),
                    (70, "comfy 4/3 water"), (999, "spring-suit warm")],
        # Feedback buttons (max 3 — ntfy limit). These are anchored to VIZ IN
        # FEET, not vibes: viz is the variable the model is actually trying to
        # predict, so "Epic/Decent/Meh" would have collected the wrong data.
        # Three taps that land straight in the backtest.
        "fb_buttons": [("👀 20ft+", "clear"), ("🙂 ~10ft", "fair"), ("🌫 Murky", "murk")],
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


def parse_local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=PT)


def hourly_map(times, vals):
    """{aware datetime -> float} skipping nulls."""
    out = {}
    for t, v in zip(times, vals):
        if v is None:
            continue
        out[parse_local(t)] = float(v)
    return out


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
            with open(meta) as f:
                return datetime.fromisoformat(json.load(f)["now"]).astimezone(PT)
        return None


def fetch_marine(src: Sources, lat, lon) -> Fetch:
    try:
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "hourly": "wave_height,wave_period,wave_direction,"
                      "swell_wave_height,swell_wave_period,swell_wave_direction,"
                      "wind_wave_height,wind_wave_period,wind_wave_direction",
            "past_days": 4, "forecast_days": 8, "timezone": "America/Los_Angeles"})
        d = json.loads(src.get("marine", CONFIG["sources"]["marine"] + "?" + q))
        h = d["hourly"]
        data = {k: hourly_map(h["time"], h[k]) for k in h if k != "time"}
        return Fetch("marine", True, data)
    except Exception as e:
        return Fetch("marine", False, error=str(e))


def fetch_weather(src: Sources, lat, lon) -> Fetch:
    try:
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation,cloud_cover",
            "daily": "sunrise,sunset",
            "wind_speed_unit": "kn", "precipitation_unit": "inch",
            "past_days": 4, "forecast_days": 8, "timezone": "America/Los_Angeles"})
        d = json.loads(src.get("weather", CONFIG["sources"]["weather"] + "?" + q))
        h = d["hourly"]
        data = {k: hourly_map(h["time"], h[k]) for k in h if k != "time"}
        data["sunrise"] = [parse_local(t) for t in d["daily"]["sunrise"]]
        data["sunset"] = [parse_local(t) for t in d["daily"]["sunset"]]
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
    try:
        url = CONFIG["sources"]["ndbc_url"].format(station=station)
        rows = parse_ndbc(src.get(key, url, timeout=20))
        good = [r for r in rows if r["wvht_m"] is not None]
        if not good:
            return Fetch(key, False, error="no valid rows")
        return Fetch(key, True, {"rows": rows, "latest": good[0], "station": station})
    except Exception as e:
        return Fetch(key, False, error=str(e))


def fetch_tides(src: Sources, begin: datetime) -> Fetch:
    try:
        q = urllib.parse.urlencode({
            "product": "predictions", "application": "dive_alert",
            "begin_date": begin.strftime("%Y%m%d"),
            "end_date": (begin + timedelta(days=4)).strftime("%Y%m%d"),
            "datum": "MLLW", "station": CONFIG["sources"]["tide_station"],
            "time_zone": "lst_ldt", "units": "english", "interval": "hilo", "format": "json"})
        d = json.loads(src.get("tides", CONFIG["sources"]["tides"] + "?" + q))
        ev = [{"t": datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=PT),
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


def fetch_all(zone_cfg, offline=False, fixture_set="normal"):
    src = Sources(offline, fixture_set)
    lat, lon = zone_cfg["lat"], zone_cfg["lon"]
    f = {
        "marine": fetch_marine(src, lat, lon),
        "weather": fetch_weather(src, lat, lon),
        "ndbc_primary": fetch_ndbc(src, CONFIG["sources"]["ndbc_primary"], "ndbc_primary"),
        "ndbc_offshore": fetch_ndbc(src, CONFIG["sources"]["ndbc_offshore"], "ndbc_offshore"),
        "tides": fetch_tides(src, (src.fixture_now() or now_pt())),
        "sst": fetch_sst(src, lat, lon),
        "chla": fetch_chla(src, lat, lon),
        "kd490": fetch_kd490(src, lat, lon),
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


def swell_damage(marine, w, exposure_map, cove_factor=1.0):
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
        scale = CONFIG["scoring"]["model_height_scale"]
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


def compute_features(w, fetches, zone_cfg, t_now):
    fc = CONFIG["features"]
    marine = fetches["marine"].data if fetches["marine"].ok else {}
    wx = fetches["weather"].data if fetches["weather"].ok else {}
    wind = wx.get("wind_speed_10m", {})
    precip = wx.get("precipitation", {})

    wind_e, _ = decayed_sum(wind, w["start"], fc["wind_lookback_h"], fc["wind_half_life_h"], lambda v: v * v)
    swell_series = marine.get("wave_height", {})
    swell_per = marine.get("wave_period", {})

    def swell_fn_pair():
        total = 0.0
        anchor = floor_hour(w["start"])
        t = anchor - timedelta(hours=fc["swell_lookback_h"])
        while t < anchor:
            h, p = swell_series.get(t), swell_per.get(t)
            if h is not None and p is not None:
                age = (anchor - t).total_seconds() / 3600.0
                total += (0.5 ** (age / fc["swell_half_life_h"])) * (m_to_ft(h) ** 2) * p
            t += timedelta(hours=1)
        return total

    swell_e = swell_fn_pair()
    dry = dry_hours(precip, w["start"], fc["rain_lookback_h"], fc["rain_threshold_in"]) if precip else None
    dmg, dmg_parts = swell_damage(marine, w, zone_cfg["exposure"],
                                  zone_cfg.get("cove_damage_factor", 1.0))
    wind_in_window = [wind.get(t) for t in window_hours(w)]
    wind_in_window = [v for v in wind_in_window if v is not None]
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
    if feats["wind_window_max_kn"] is not None:
        breakdown["wind_now"] = piecewise(feats["wind_window_max_kn"], sc["wind_now_penalty"])
    else:
        breakdown["wind_now"] = 0.5
        flags.append("no window wind forecast")
    # light: forecastable, and a real part of how the dive feels
    if feats.get("cloud_pct") is not None:
        breakdown["light"] = piecewise(feats["cloud_pct"], sc["cloud_penalty"])
    score -= sum(breakdown.values())
    score = max(1.0, min(10.0, score))

    cap, cap_reason = None, None
    # hard rules override the weighted score
    if feats["wind_window_max_kn"] is not None and feats["wind_window_max_kn"] >= sc["wind_cap_kt"]:
        cap, cap_reason = sc["wind_cap_score"], "wind ≥%dkt in window" % sc["wind_cap_kt"]
    rain_lb = CONFIG["features"]["creek_rain_lookback_h"] if creek_adjacent else CONFIG["features"]["rain_lookback_h"]
    rain_cap = sc["creek_rain_cap"] if creek_adjacent else sc["rain_cap"]
    if feats["dry_hours"] is not None and feats["dry_hours"] < rain_lb:
        c = rain_cap
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


def best_entries(zone_cfg, w, tides, damage):
    h, trend, next_e = tide_state_at(tides, w["start"] + (w["end"] - w["start"]) / 2)
    band = tide_band(h)
    ranked = sorted(zone_cfg["sites"],
                    key=lambda s: -(s["shelter"] * (1.0 + min(damage or 0, 10) / 5.0)
                                    + site_tide_fit(s, band)))
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
                 quiet=False, sst_c=None, brag=None, actions=None):
    v = CONFIG["voice"]
    is_perfect, _ = perfect_gate(feats, score, sst_c)
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
    if tip:
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


def perfect_gate(feats, score, sst_c):
    """Every knowable axis aligned, as a strict AND. Returns (passed, failures).
    Deliberately unforgiving: no partial credit, no averaging one axis against
    another. If anything is unknown it fails — silence beats a false promise."""
    g = CONFIG["perfect_gate"]
    checks = [
        ("flat", feats.get("damage"), lambda v: v <= g["max_damage"]),
        ("glass", feats.get("wind_window_max_kn"), lambda v: v <= g["max_wind_kn"]),
        ("dry", feats.get("dry_hours"), lambda v: v >= g["min_dry_hours"]),
        ("sun", feats.get("cloud_pct"), lambda v: v <= g["max_cloud_pct"]),
        ("warm", sst_c, lambda v: v >= g["min_sst_c"]),
        ("score", score, lambda v: v >= g["min_score"]),
    ]
    failed = [name for name, val, ok in checks if val is None or not ok(val)]
    return (not failed), failed


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
    strip = " · ".join(
        "%s %.1f%s" % (by_day[d]["w"]["start"].strftime("%a"), by_day[d]["score"],
                       "★" if by_day[d] is best else "")
        for d in order)

    if best["score"] >= CONFIG["alerting"]["threshold"]:
        headline = "%s looks like the one" % best["w"]["label"]
        lead = "%s is the pick — %s." % (
            sentence_case(best["w"]["label"]),
            sensory_phrase(best["score"], (best.get("feats") or {}).get("kd490")))
        where = "Take %s" % best["entries"][0] if best["entries"] else ""
        if best["tide_fyi"] and where:
            where += ", " + best["tide_fyi"]
        body = [strip, lead + ((" " + where + ".") if where else "")]
    else:
        headline = "quiet week, best is %s" % best["w"]["label"]
        body = [strip,
                "Nothing clears 7 yet — %s is the pick at %.1f, so %s. "
                "Forecasts firm up midweek."
                % (best["w"]["label"], best["score"],
                   sensory_phrase(best["score"], (best.get("feats") or {}).get("kd490")))]
    brag = superlative(best["score"], t_now)
    if brag:
        body[1] += " (%s)" % brag
    suit = wetsuit_phrase(sst_c)
    if suit:
        body[1] += " · %s" % suit
    if tip:
        body.append("Tip: " + tip)
    return {"title": "Laguna week ahead — %s %s" % (headline, v["emoji"]),
            "message": "\n".join(body), "priority": 3, "actions": actions or []}


# =====================================================================
# alert decision + streak logic
# =====================================================================

def decide_alert(scored, t_now, state):
    """scored: list of dicts with window/score/feats/... . Returns (action, payload)
    where action in (None, 'alert', 'quiet_alert', 'downgrade')."""
    al = CONFIG["alerting"]
    eligible = [s for s in scored
                if al["lead_min_h"] <= (s["w"]["start"] - t_now).total_seconds() / 3600.0 <= al["lead_max_h"]]
    qualifying = [s for s in eligible if s["score"] >= al["threshold"]]
    best = max(qualifying, key=lambda s: s["score"]) if qualifying else None
    streak = state.get("streak")

    if best is None:
        if streak:
            best_any = max(eligible, key=lambda s: s["score"]) if eligible else None
            state["streak"] = None
            if best_any is not None and streak["score"] - best_any["score"] >= al["material_change"]:
                return "downgrade", {"w": best_any["w"], "old": streak["score"],
                                     "new": best_any["score"], "feats": best_any["feats"]}
        return None, None

    if not streak:
        state["streak"] = {"score": best["score"], "entries": best["entries"],
                           "window_key": best["w"]["key"], "since": t_now.isoformat()}
        return "alert", best

    moved = abs(best["score"] - streak["score"]) >= al["material_change"]
    entries_changed = best["entries"] != streak["entries"]
    if moved or entries_changed:
        dropped = best["score"] < streak["score"]
        state["streak"].update({"score": best["score"], "entries": best["entries"],
                                "window_key": best["w"]["key"]})
        if dropped and moved:
            return "downgrade", {"w": best["w"], "old": streak["score"],
                                 "new": best["score"], "feats": best["feats"]}
        return "quiet_alert", best
    return None, None


# =====================================================================
# delivery + health
# =====================================================================

def get_secret(name):
    v = os.environ.get(name)
    if v:
        return v
    lc = os.path.join(ROOT, "local_config.json")
    if os.path.exists(lc):
        with open(lc) as f:
            return json.load(f).get(name)
    return None


def feedback_topic():
    t = get_secret("NTFY_TOPIC")
    return (t + "-fb") if t else None


def feedback_actions(window_key):
    """ntfy action buttons that post a verdict to the companion feedback topic.
    Zero secrets leave the repo: the buttons just publish plain text, and the
    next scheduled run polls the topic and folds verdicts into dive_log.csv."""
    fb = feedback_topic()
    if not fb:
        return []
    return [{"action": "http", "label": label,
             "url": "%s/%s" % (CONFIG["sources"]["ntfy"], fb),
             "method": "POST", "body": "%s|%s" % (verdict, window_key), "clear": True}
            for label, verdict in CONFIG["voice"]["fb_buttons"]]


# verdict -> rough viz/surge equivalents for the dive log.
# CALIBRATION: these seed backtests until you log precise numbers with log-dive.
FEEDBACK_MAP = {"clear": (20.0, "low"), "fair": (10.0, "low"), "murk": (5.0, "med")}


def ingest_feedback(state, dry_run):
    """Poll the feedback topic for button presses since last ingest; join each
    verdict back to its window and append to dive_log.csv. Fail-soft."""
    fb = feedback_topic()
    if not fb:
        return 0
    try:
        since = state.get("fb_since", "48h")
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
            state["fb_since"] = str(latest + 1)
        return len(rows)
    except Exception as e:
        print("feedback ingest skipped: %s" % e, file=sys.stderr)
        return 0


def notify(payload, dry_run):
    topic = get_secret("NTFY_TOPIC")
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
    for w in windows:
        feats = compute_features(w, fetches, zone_cfg, t_now)
        creek = False  # zone-level scoring; creek rule applies per-site when Zone B wakes
        score, breakdown, cap_reason, flags = score_window(feats, creek)
        conf_word, comp, agree, notes = confidence(fetches, feats, t_now)
        entries, tide_fyi, band = best_entries(zone_cfg, w, tides, feats["damage"])
        scored.append({"zone": zone_key, "w": w, "score": score, "feats": feats,
                       "breakdown": breakdown, "cap_reason": cap_reason, "flags": flags,
                       "conf": conf_word, "completeness": comp, "agreement": agree,
                       "conf_notes": notes, "entries": entries, "tide_fyi": tide_fyi,
                       "band": band})
    return scored


def cmd_run(args):
    t_now = now_pt()
    state = load_state()
    if not args.offline:
        ingest_feedback(state, args.dry_run)
    all_scored, any_swell_ok = [], False
    for zk, zc in CONFIG["zones"].items():
        if not zc["enabled"]:
            continue
        fetches, src = fetch_all(zc, offline=args.offline, fixture_set=args.fixtures)
        fx_now = src.fixture_now()
        if fx_now:
            t_now = fx_now
        swell_ok = fetches["marine"].ok or fetches["ndbc_primary"].ok
        any_swell_ok = any_swell_ok or swell_ok
        for name, f in fetches.items():
            if not f.ok:
                print("degraded: %s failed (%s)" % (name, f.error), file=sys.stderr)
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
                                 actions=feedback_actions("digest-%s" % t_now.date())),
                   args.dry_run)
        elif args.brief:
            best = max(scored, key=lambda s: s["score"]) if scored else None
            if best:
                txt = "Zone %s best: %.1f/10 %s — %s. %s" % (
                    zk, best["score"], best["w"]["label"], " / ".join(best["entries"]),
                    best["tide_fyi"] or "")
                notify({"title": "Dive brief — Zone %s" % zk, "message": txt, "priority": 2}, args.dry_run)
        else:
            action, payload = decide_alert(scored, t_now, state)
            if action == "alert" or action == "quiet_alert":
                tip = select_tip(payload["entries"], payload["score"], payload["feats"]["damage"],
                                 payload["band"], payload["w"]["start"], state)
                msg = render_alert(payload["w"], payload["score"], payload["feats"], payload["conf"],
                                   payload["conf_notes"], payload["entries"], payload["tide_fyi"],
                                   tip, state, quiet=(action == "quiet_alert"),
                                   sst_c=sst, brag=superlative(payload["score"], t_now),
                                   actions=feedback_actions(payload["w"]["key"]))
                notify(msg, args.dry_run)
                for r in rows:
                    if r["window_key"] == payload["w"]["key"]:
                        r["alerted"] = action
            elif action == "downgrade":
                notify(render_downgrade(payload["w"], payload["old"], payload["new"], payload["feats"]),
                       args.dry_run)
        append_log(LOG_PATH, LOG_COLS, rows, args.dry_run)

    save_state(state, args.dry_run)
    if not any_swell_ok:
        ping_health(ok=False, msg="all swell sources failed")
        sys.exit(1)
    ping_health(ok=True)


# =====================================================================
# hindcast + dive log + backtest
# =====================================================================

def cmd_hindcast(args):
    """Rebuild historical Zone A scores from Open-Meteo archives so backtesting
    works from day one. Dawn/dusk per day, features from the archive series."""
    zc = CONFIG["zones"]["A"]
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
              {"hourly": "wind_speed_10m,precipitation,cloud_cover",
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

    rows = []
    d = start
    fetches = {"marine": Fetch("marine", True, marine),
               "weather": Fetch("weather", True, {"wind_speed_10m": wx["wind_speed_10m"],
                                                  "precipitation": wx["precipitation"],
                                                  "cloud_cover": wx.get("cloud_cover", {}),
                                                  "sunrise": [], "sunset": []}),
               "ndbc_primary": Fetch("ndbc_primary", False, error="hindcast"),
               "ndbc_offshore": Fetch("ndbc_offshore", False, error="hindcast"),
               "tides": Fetch("tides", False, error="hindcast"),
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
    with open(HINDCAST, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for r in rows:
            wr.writerow(r)
    print("wrote %d windows to %s" % (len(rows), HINDCAST))
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
        "title": "dive-alert is live 🤿",
        "message": "You're subscribed. Alerts fire when Laguna scores 7+, 12-48h out.\n"
                   "Buttons on each alert feed the calibration loop — press them after you dive.",
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


# =====================================================================
# validate — pressure-test the swell model against 45 days of buoy truth
# =====================================================================

def cmd_validate(args):
    st = CONFIG["sources"]["ndbc_primary"]
    print("fetching %s buoy record (45d) and Open-Meteo model at the buoy..." % st)
    obs_rows = parse_ndbc(http_get(CONFIG["sources"]["ndbc_url"].format(station=st), 30))
    # NDBC 46253 sits at 33.576N 118.181W — compare the model AT the buoy so
    # we test the model, not the geography between San Pedro and Laguna.
    q = urllib.parse.urlencode({
        "latitude": 33.576, "longitude": -118.181,
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
    if abs(bias_pct) > 10:
        print("  → bias exceeds 10%%: set CONFIG['scoring']['model_height_scale'] = %.2f" % suggested)
    else:
        print("  → bias within 10%: leave model_height_scale at 1.0")


# =====================================================================
# scenario tests — these ARE the spec.
# =====================================================================

def _flat_series(start, hours, val):
    return {start + timedelta(hours=h): val for h in range(-hours, hours)}


def _mk_fetches(t0, swell_ft, per_s, dir_deg, wind_kn=4.0, precip_series=None,
                windwave_ft=0.0, ww_per=5.0):
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
        "wind_direction_10m": _flat_series(t0, hours, 270.0),
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
    sub.add_parser("backtest")
    sub.add_parser("test")
    sub.add_parser("record-fixtures")
    sub.add_parser("validate")
    sub.add_parser("ingest")
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
    elif args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "ingest":
        state = load_state()
        ingest_feedback(state, dry_run=False)
        save_state(state, dry_run=False)


if __name__ == "__main__":
    main()
