# The Dive Bell — Keeper's Runbook

For whoever operates this in 2031 — including you, having forgotten
everything. The system is one Python file, one HTML file, and four
workflows. No servers, no database, no build step. The repo is the
website; the bot commits data back into it; GitHub Pages serves it at
thedivebell.com.

## The machine, in one breath

`dive_alert.py` (stdlib only) runs on GitHub Actions crons (`dive.yml`,
4x daily), fetches ocean data per zone, scores dawn/dusk windows 1–10,
writes `data/*.json` + per-bell share cards, commits them, and Pages
deploys. Alerts ride ntfy topics (per bell + `-ops` for plumbing);
SMS rings ride Twilio from 833-858-BELL. Signup is a text: Twilio's
message history IS the subscriber database — zero PII in this repo.

## The guard stack (each layer exists because something got past the one above)

| Layer | What | Where |
|---|---|---|
| instruments | per-source failure sentinel, one ops ping per outage | `sentinel_update` |
| engine | 30h staleness watch, daily | `bellwatch.yml` |
| alarm channel | ntfy health check — fails the workflow so GitHub *emails* | `bellwatch.yml` |
| purse | Twilio balance < $5 → ops ping (SMS dies silently at $0) | `bellwatch.yml` |
| model | monthly buoy validation + forecast self-grading | `drift.yml`, `cmd_skill` |
| code paths | drill family forces every rare branch, on every push | `ci.yml`, tests gg–jj |
| capability | fleet-wide gate-axis blindness alarm | `capability_sentinel` |
| outside the building | Healthchecks.io dead-man ping (external email) | `HEALTHCHECKS_URL` secret |
| rust | Dependabot PRs for aging Action pins | `.github/dependabot.yml` |

## Self-correction (don't break the loops)

- **Buoy anchor**: rolling 48h obs/model height ratio, applied live per zone.
- **Skill loop**: `cmd_skill` (monthly) grades logged forecasts against
  archive truth into `data/skill_log.csv`; `load_skill_correction` applies
  the sign-flipped bias live (capped ±0.5, n≥25). **`score_log.csv` stores
  the RAW score** so the grader measures residual bias — never log the
  corrected score there or the loop oscillates.
- **Second voice**: ECMWF wave model fetched alongside best_match;
  disagreement dents confidence, and it substitutes (marked, capped) when
  the primary dies.

## When things break (it will be one of these)

- **Runs failing at the commit step (exit 128)**: a new generated file
  isn't staged. The commit step must `git add -A`; grep `dive.yml`.
- **Runs green but site stale**: Pages deploy failed silently — they don't
  retry. `gh api -X POST repos/PacificVanguard/dive-alert/pages/builds`.
- **HTTPS cert stuck**: remove and re-add the custom domain via API.
- **Whole fleet can't ring, no errors**: capability sentinel should have
  pinged ops. A gate axis is failing closed fleet-wide (the CoastWatch-403
  class). Check `sst`/`kd490` fetches; `zone_sst` falls back to Open-Meteo.
- **Crons firing hours late**: normal GitHub behavior; windows are wide by
  design. Never add an exact-hour guard.
- **Scheduled workflows disabled**: GitHub does this after 60 days without
  commits — only possible here if runs fail long enough to stop bot
  commits. Healthchecks catches it; re-enable in the Actions tab.
- **SMS not sending**: check Twilio balance (bellwatch watches it), then
  Twilio console → the number → webhook still points at the Function.
- **Editing the SMS welcome**: change `twilio/incoming.js`, then run the
  `wire-sms` workflow (manual). It redeploys the Function by API.

## Sacred invariants (tests enforce most; keep it that way)

1. Provisional is an honesty label, never a ring lock (gg5 forbids it).
2. The gate fails closed on unknowns; the AND is not overridable.
3. No viz claims — the score is a *setup* score (tested).
4. Every rare-path branch gets a drill that forces it.
5. Scores are relative to each bell's own water (per-zone scales).
6. Never print subscriber numbers anywhere — Actions logs are public.
7. One home bell per subscriber; never aggregate rings into one feed.

## Adding a bell

`cast` command + the honesty band (15–35% of windows ≥7 on its own
hindcast). Fit `marine_height_scale` on the casting's evidence. Add the
SMS keyword in `twilio/incoming.js` AND `SMS_WORD` in `index.html`
(keep in sync), re-run `wire-sms`. Blue Heron Bridge / Puget Sound wait
on a slack-window scorer that doesn't exist yet — don't force them in.

## Secrets (Actions)

`NTFY_TOPIC` · `TWILIO_ACCOUNT_SID` · `TWILIO_AUTH_TOKEN` · `TWILIO_FROM`
· `HEALTHCHECKS_URL` (optional ping). Rotate in the repo settings; nothing
else holds credentials.
