# Claude Code Prompt — Laguna Beach Dive Conditions Alert System (v4)

Copy everything below the line into Claude Code. Zone A (Laguna) is the live zone; confirm site list at setup step 1.

---

## Mission

Build a zero-cost, zero-maintenance system that scores dive conditions (1–10) for the Laguna Beach coves and pushes me a clean, enthusiastic, genuinely useful notification — with a tip for that day — when a dive window scores ≥7, with enough lead time to plan. It must run unattended for years. Optimize for simplicity, debuggability, and graceful degradation.

## Score scale (anchored — implement exactly this)

- **10:** exceptional; ~25ft+ expected viz, glassy, trivial entry. A handful of days a year.
- **8–9:** drop everything; ~15–20ft viz, minor texture, easy entry.
- **7:** solidly worth the drive; ~12–15ft, manageable surge. ← alert threshold starts here
- **5–6:** diveable but mediocre; ~8–10ft.
- **3–4:** poor; short-period chop or recent rain.
- **1–2:** blown out.

## Geography model — Zone A live, B/C as disabled stubs

**Zone A — Laguna (ENABLED, the product):** Crescent Bay, Shaw's Cove, Divers Cove, Fisherman's Cove, Picnic Beach/Heisler Park, Wood's Cove, Cleo Street barge. One swell/wind model for the zone; each site carries only entry notes, tide-sensitivity, and a shelter modifier (±0.5 max). Confirm this list with me at setup.

**Zone B — South Laguna/Aliso and Zone C — Dana Point (DISABLED stubs):** present in config with `enabled: false`, full structure (sites, lat/lon, `take_allowed`, `creek_adjacent`) so enabling later is a one-line flip. No fetches or scoring for disabled zones.

**MPA / take-legality layer (mandatory even though A is no-take):** Laguna's coastline is State Marine Reserve / Conservation Area — no lobster, no spearfishing. Verify current CDFW boundaries at setup; every site carries `take_allowed`. All lobster-season night windows, moon modifiers, and take-oriented tips are gated behind `take_allowed: true` — dormant while only Zone A is live, and they wake automatically when a take-legal zone (Dana Point) is enabled. Never emit take-oriented content for a reserve site; Zone A tips are sightseeing/photo/skills oriented.

**Alert framing:** score the zone, then name the best 1–2 entries for that window based on tide + entry type + shelter modifier.

## Architecture — non-negotiable constraints

1. **One Python file** (`dive_alert.py`), Python 3.11+, **stdlib only**. Known tension: CDIP MOP data is primarily NetCDF. Resolution order: (a) CDIP ERDDAP with `.json`/`.csv` output; (b) if unworkable, NDBC nearshore buoys + Open-Meteo Marine as the swell backbone — acceptable v1, tell me and move on; (c) a third-party package only if you stop and ask first. Never silently add dependencies.
2. **Stateless and idempotent.** Each run: fetch → features → score → maybe notify → append logs → exit. State = `data/score_log.csv` (every window scored, all features) and `data/alert_state.json` (alert/streak/tip-rotation state), committed back.
3. **Fail soft, never silent.** Fetches independently wrapped; missing source → score with remainder, lower confidence, name the gap. Exit nonzero only if ALL swell sources fail.
4. **Dead man's switch.** Ping `HEALTHCHECKS_URL` on every successful run; `/fail` variant on unhandled exceptions.
5. **GitHub Actions runtime.** `.github/workflows/dive.yml`: UTC cron (~4:00am and ~6:00pm Pacific; dual offsets + early exit for PST/PDT; scheduled runs fire 15–60 min late — nothing depends on exact run time; data commit-back with `[skip ci]` also prevents 60-day auto-disable). Secrets `NTFY_TOPIC` and `HEALTHCHECKS_URL` via Actions secrets → env vars; git-ignored `local_config.json` for local runs. No secrets in committed files, ever.
6. **Config as one commented dict:** zones, sites, weights, thresholds, endpoints, voice/tone strings, tip library. Every constant gets a `# CALIBRATION:` comment stating what dive-log evidence would move it.

## Data sources (all free, no API keys) — for Zone A

1. **Swell primary:** nearest CDIP MOP point to Laguna (per constraint 1). Show me candidates + your recommendation at setup; don't guess silently.
2. **Swell fallback/offshore trend:** nearest active NDBC buoy (verify live with a real request; San Pedro Channel area candidates — confirm IDs, don't assume).
3. **Swell tertiary:** Open-Meteo Marine (hourly height/period/direction, past + forecast).
4. **Wind:** Open-Meteo hourly speed/direction/gusts, past 72h AND next 72h in one call.
5. **Rain:** Open-Meteo precipitation, past 72h and next 72h.
6. **Tides:** NOAA CO-OPS, next 72h. NOT scored in v1 — selects best entries and appears as an FYI line. Promote to scored input only when the dive log justifies it.
7. **SST + chlorophyll:** NOAA CoastWatch ERDDAP nearest pixel — confidence modifier only; degrade gracefully.
8. **Moon phase:** computed locally; dormant until a `take_allowed` zone is enabled.

## Feature design — the core correctness rule

Score Zone A per window over the **next 72h**. Windows: dawn (first light +3h) and dusk (−2h to sunset). Night windows exist in code but only activate for enabled `take_allowed` zones in lobster season (verify season dates when that day comes).

**Features are computed relative to WINDOW START, not now.** For a window T hours out, lagged features integrate observed history up to now PLUS forecast values from now to window start, same decay math throughout:

- `wind_energy_48h(window)`: decayed sum of hourly speed² over the 48h before window start (half-life ~18h)
- `swell_energy_72h(window)`: decayed sum of (height² × period) over 72h before window start (half-life ~24h)
- `dry_hours(window)`: hours from last ≥0.2" precip (observed or forecast) to window start

Tag every feature with its observed/forecast mix — feeds confidence.

**Interactions, never independent scores:** height × period × direction jointly. Long-period (≥14s) groundswell at moderate height ≪ damage of short-period (≤9s) windswell at half the height. Zone A direction exposure map in config — draft with me: strong exposure to S/SW summer swell, partial shadowing of W/NW; every value `# CALIBRATION:`.

**Hard rules (override weighted score):**
- Precip ≥ 0.2" within 72h before window start → cap 4, flag post-rain.
- Creek rule (dormant until Zone B enabled): `creek_adjacent` sites use a 96h rain window, cap 3.
- Forecast wind ≥ [15]kt during the window → cap 5.

**Confidence:** completeness (sources returned; observed-vs-forecast mix) and agreement (swell sources concur), computed separately, reported as one word (high/medium/low). Material disagreement gets named in the alert — never averaged away.

## Alerting — threshold ≥7, streak logic, voice spec

- **Lead-time rule:** alert only on windows 12–48h out. Evening run owns next-morning windows; morning run owns next-evening/next-day.
- **Threshold ≥7.0 with anti-spam (keep exactly this):**
  - Each run alerts on at most the single best qualifying window in the next 48h.
  - **Streak logic:** after a good stretch begins, follow-ups fire only on material change — score moves ≥1.0 (up OR down — downgrades prevent wasted drives), or the best-entries list changes on tide. Streak state in `alert_state.json`. First alert of a stretch is the loud one; the rest stay quiet.
- **Delivery:** ntfy.sh POST to `NTFY_TOPIC`.

### Voice — enthusiastic, scaled to the score. Implement as tone tiers:

Enthusiasm must be EARNED by the number, or the alerts train me to ignore them. Three tiers, template strings in config so I can edit the voice without touching logic:

- **Tier 1 (7.0–7.9) — "solid, worth it":** confident, understated. "Solid window," "worth the drive," "clean morning."
- **Tier 2 (8.0–8.9) — "stoked":** energetic. "Laguna's firing," "this is the one," "don't sleep on Saturday."
- **Tier 3 (9.0+) — "rare day":** genuinely excited, still short. "Best window of the season so far — clear the morning."

Rules: ≤4 lines total, one emoji max (🤿), no exclamation stacking, no fake hype below 8, never bury the score. Downgrade alerts stay direct: "Saturday slipped — 8.1 → 6.4. Windswell moved in. Skip it."

**Message format — exactly:**

Title: `⟨tier-voiced hook⟩ — ⟨score⟩/10 ⟨window⟩` (e.g., "Laguna's firing — 8.6/10 Sat dawn 🤿")
Line 1: why, plain English, ≤12 words, tier-voiced.
Line 2: confidence (only if not high) + best entries + tide FYI.
Line 3: `Tip: ⟨selected tip⟩`
Nothing else. No source dumps, no feature values (those live in the log). Clean and short is a feature.

- `--brief` mode: one short all-enabled-zones summary regardless of threshold (I may schedule it).

## Tips & inspiration — v1 is a curated library, not an LLM

- Config holds a tip library for Zone A sites: 8–10 tips per site tagged by applicability (viz tier, surge, tide state, season). Content lanes for Laguna (no-take): entry lines for a given tide, where viz holds in each cove, photo subjects and fish life, skills ideas for mid days. Draft the initial library with me at setup with clear placeholder examples showing the tagging pattern — I'll edit the words.
- Selection is rule-based: filter by the window's conditions, rotate among matches (persist last-used in `alert_state.json`; never repeat back-to-back).
- **Leave one clearly marked extension point** where a single LLM call could later replace/augment tip selection and the "why" line (v2 feature — no LLM calls in v1).

## Calibration & backtesting

- `log-dive --site "Shaw's Cove" --viz 15 --surge low --notes "..."` → `data/dive_log.csv`, joined with that window's features.
- `hindcast --days 180`: rebuild historical Zone A scores via Open-Meteo archive (+ CDIP/NDBC history where reachable) so backtesting works on day one.
- `backtest`: predicted vs. actual with per-feature correlation hints. No ML — honest tables for hand-tuning config.

## Testing & acceptance — all required

1. `--dry-run` (prints, no sends, no state writes) and `--offline` (full pipeline on fixtures).
2. Fixtures include one degraded case (missing source) and one disagreement case (swell sources conflict).
3. Named scenario tests against the anchored scale: (a) 5 dry days + small 16s S groundswell + calm → ≥8.5 with Tier 2/3 voice; (b) 3ft at 7s windswell → ≤4; (c) 0.3" rain 24h pre-window → ≤4 flagged; (d) same swell with exposure map vs. shadowed direction → materially different; (e) same conditions at 40h vs. 6h lead → same score, lower confidence at 40h, NO alert at 6h; (f) reserve site → no night window, no take tip, ever; (g) day 3 of a steady 7.4 streak, no material change → no alert; (h) 7.2 window → Tier 1 voice, no Tier 2 language. These scenarios ARE the spec — if one feels wrong, stop and ask me.
4. One real end-to-end run with live fetches; show me full output (including rendered alert text at each tier) before scheduling.
5. Walk me through: ntfy topic + phone app, Healthchecks, Actions secrets, first two scheduled runs green.

## What NOT to do

- No frameworks, async, or premature abstraction. No LLM calls in v1. No scraping DiveViz/Surfline/forums in v1 (marked extension point only).
- Never invent endpoint URLs — verify each with a real request; dead → tell me + propose alternative.
- Never let degradation pass invisibly. Never commit secrets. Never emit take-oriented content for reserve sites. Never use Tier 2+ voice on a sub-8 score.

## Definition of done

Two scheduled runs green; one real notification received; offline + scenario tests pass; hindcast populated; Healthchecks green; README ≤1 page covering: change a weight, edit the voice strings, edit the tip library, enable Zone B/C, read a backtest, rotate the ntfy topic.

Work order: (1) confirm Zone A sites/MPA flags/MOP-buoy selection with me → (2) fetchers + fixtures → (3) features + scoring + scenario tests → (4) hindcast → (5) alerting + streak logic + voice tiers + tip library → (6) Actions + secrets → (7) end-to-end verification with me.
