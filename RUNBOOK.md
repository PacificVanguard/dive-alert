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
| never started | a run GitHub gave no machine is rerun once; a second miss texts the keeper | `rescue.yml` |
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
- **Flicker rule**: a gate must hold across `GATE_CONFIRM_RUNS` (2)
  consecutive runs before it rings. `gate_raw` / the feed's `aligned` keep
  the physical answer; `gate` is the promise. Only ~27% of gates seen at
  24-72h lead survive to the morning, and Wed 2026-09-16 opened for a single
  hour, texted, and came in at 7.3. Any failing run resets the count.
  If you ever need the ring path in one run (drills), set GATE_CONFIRM_RUNS
  to 1 around it — never delete the rule.
- **Forecaster's veto**: US bells fetch active NWS alerts at their point
  (`fetch_nws_alerts`, products in `NWS_VETO_EVENTS` — Small Craft is
  deliberately excluded). A window under one cannot ring and the forecast
  names no cove for it. A dead NWS feed vetoes nothing; it is logged as
  down like any instrument. Sydney and Bonaire carry no NWS key at all.
- **Model review**: a weekly Claude scheduled task on the keeper's Mac
  (`~/.claude/scheduled-tasks/dive-bell-model-review`, Mondays 7am) reads
  the week's logs and opens at most ONE pull request with a drill. It never
  merges; a human does. It runs only while the Claude app is open.

## What the bell is (and the only words it needs)

Three things, decided from first principles on 2026-10-05; nothing here
should grow a fourth. (1) THE RING — a text when the water is perfect;
rare; the staple. (2) THE FORECAST — the week ahead each Wednesday;
comes with the bell; `BELL ONLY` switches it off, `FORECAST` brings it
back and also answers on demand. (3) YOUR WORD BACK — `FINS` / `REEF` /
`BUDDY`; the bell replies with the dive it logged ("Friday evening") and
the likeliest correction. Old words (WEEK, WEEKLY, DIGEST, QUIET) still
work silently. The forecast text is written ONCE, by the engine
(`sms_digest_text`), and published per bell as `forecast_sms` in
zones.json — the webhook just returns it. A question is not a move:
"MAUI FORECAST" or "FINS DANA" must never re-home a diver
(`sms_subscribers`, `homeBell`). Tried and removed the same day: a
weekly "best morning" text (a third push dilutes the ring) and a YES/NO
confirmation dialogue (one text in, one back is enough).

## The pages are the funnel (2026-10-06)

Growth is search and instructors, not virality — five rings a year cannot
compound. So each bell has a REAL page (`/laguna-beach/` etc., `bell_page`),
rewritten every run: the dated answer sentence first, the week scored, the
coves from `sites`, the instruments, the join word, and the ledger of rings
(`ring_ledger` — a diary until five rings, a score after). Plain HTML, no
script, canonical URL, JSON-LD; the OG tags still carry the live state for
the unfurl. `sitemap.xml` is written with them. `robots.txt` welcomes the AI
crawlers by name, `llms.txt` explains the bell to them, `openapi.json`
describes `data/zones.json` (which now carries `join` per bell) so an
assistant can call it with no key. Until this date every bell page was a
694-byte redirect into the hash route — fifteen bells, one URL to Google.
One human act, once: submit the sitemap in Google Search Console.

## The public voice (2026-10-06)

The bell posts only when it has something to say — never daily: a RING, a
TAKE-BACK, the WEEK card (once per ISO week, the silence counter rides on
it), and a HAZARD post when the forecaster vetoed a 7. Each is queued by the
run as a debt in `state["social_queue"]` (deduped by key, `social_posted`
remembers 60 days) and paid by `python dive_alert.py post` (the "Speak in
public" step, `continue-on-error` so a dead network never costs the data
commit). Accounts are wired by secret and nothing else: `BSKY_HANDLE` +
`BSKY_APP_PASSWORD` (an app password, never the account password);
`THREADS_USER_ID` + `THREADS_TOKEN` (a long-lived token — it expires every
60 days and must be refreshed by hand; a failed Threads post stays owed and
the log says why); `X_API_KEY/SECRET` + `X_ACCESS_TOKEN/SECRET` (OAuth 1.0a,
signed in stdlib). No secret = that network does not exist. Cards are one
SVG grammar (`card_svg`), rasterized on the runner by rsvg-convert or
ImageMagick; with neither, the post goes as words. Threads is always words
(its API fetches images by public URL, and the card is not public yet).
Mark the accounts as automated in their settings — X requires it, and it
is true. `python dive_alert.py post --dry-run` shows what would leave.

## The Wednesday reading rides whichever cron lands (don't "fix" this)

GitHub fires crons hours late — 3-4h has been normal. The guard classifies
a run by the LOCAL HOUR it actually lands in, not by which cron fired it,
so on Wednesdays the 4:30am "morning" cron arriving at ~8:30 becomes the
weekly run, and the real 9:00 weekly cron often slides into the midday
ingest window. Together they cover Wed 6-10 for anything from punctual
to ~5.5h late, and the per-ISO-week dedupe (`ntfy_digest_sent`,
`sms_digest_sent`) guarantees exactly one reading. Collapsing this to a
single cron would make the ritual depend on GitHub's punctuality. And if
no run lands in 6-10 at all (2026-10-05: GitHub never gave the run a
machine), `weekly_owed` lets the first ordinary scoring run from Wednesday
6am through Thursday open the week's debt itself — the dedupe still holds
it to one reading. A Wednesday with no reading now means two days of runs
all failed; check the run list before touching anything else.

## Is the bell actually texting?

`smscheck` (workflow, manual) asks Twilio for real outbound STATUS and
carrier error codes — statuses only, never numbers, because this repo's
logs are public. Use it whenever someone says "no texts": our own logs
count a 201 as delivery, and acceptance is not arrival. Also check the
Weekly digest step still carries the TWILIO_* secrets — it silently
shipped without them once, so DIGEST could never send.

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
- **A Wednesday passed with no reading** (a run died, GitHub skipped the
  window): `gh workflow run dive.yml -f mode=weekly`. The per-week dedupe
  means it is safe to run even if you're not sure — a reading already
  sent this ISO week will not be sent twice. The app copy waits for the
  bell's own morning (6am-1pm local); the text goes within 8am-9pm.
- **One bell failed, the rest ran**: normal now. The failed bell keeps its
  last plate on the board marked `stale`; ops (and the keeper's phone, if
  `KEEPER_PHONE` is set) get "Bells failed this run". Look at the ZONE
  line in the log; a whole-fleet failure is almost always the runner's
  network, and the next run simply pays whatever was owed.

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
· `KEEPER_PHONE` (the keeper's own number: every ops alarm and every
workflow failure is also texted there; unset = app/email only)
· `HEALTHCHECKS_URL` (optional ping). Rotate in the repo settings; nothing
else holds credentials.
