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
  It is a residual on top of `model_height_scale` — the swell model's own
  bias at that bell's buoy. The global 1.13 is Laguna's; a bell may carry
  its own in its zone config after two consistent months of `validate`
  (La Jolla 0.73 and Sydney 1.38 since 2026-10-12, Oahu North Shore 0.85
  since 2026-10-13), so a buoy-down hour is scored with its own coast's
  correction. With the buoy up the anchor cancels this scale exactly, so
  it moves buoy-down and hindcast scores only — judge it by those. Geography is `marine_height_scale`,
  a different knob, fit by castings — don't retune one to fix the other.
  A bell whose primary buoy IS its offshore buoy (Kona 51003, Santa
  Barbara 46054) gets no anchor at all (2026-10-14): that ratio is
  geography, and Kona sat at the 1.6 clamp scoring 1.0 for three weeks.
  Since 2026-10-14 every `score_log.csv` row carries the `anchor` that
  scaled it (blank = no measurement) — the raw model height is not logged.
- **Skill loop**: `cmd_skill` (monthly) grades logged forecasts against
  archive truth into `data/skill_log.csv`; `load_skill_correction` applies
  the sign-flipped bias live (capped ±0.5, n≥25). Since 2026-10-12 each
  bell with n≥25 in a bucket gets its own row (`zone` column) and its own
  correction; thin buckets keep the fleet's. **`score_log.csv` stores
  the RAW score** so the grader measures residual bias — never log the
  corrected score there or the loop oscillates.
- **Drift log**: `validate` writes `data/drift_log.csv` with a `zone`
  column since 2026-10-12. Rows before that ran in drift.yml's order
  (A D E F H I J K O; a bell with <100 matched hours is skipped silently).
- **Gate hold**: `gate_skill.csv` carries `flips` — which axis a lost 24-72h
  promise failed on its morning. First reading (2026-10-12, 53 of 71 lost):
  sun 31, flat 23, glass 16. The cloud forecast is the overconfident one.
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
- **Improvement loop**: a daily Claude scheduled task on the keeper's Mac
  (`~/.claude/scheduled-tasks/dive-bell-model-review`, 7am, only while the
  Claude app is open). Each run: re-read the evidence, land ONE change with
  a drill on its own branch, merge it once drill + tests + audit pass, then
  re-read and go again — up to three changes a run, stopping the moment
  nothing meets the bar (most days). It also re-checks its own past changes
  against `REVIEW_LOG.md`, where every change carries what would prove it
  wrong and a check date; a proven-wrong change is reverted first. A knob it
  turned is frozen for 14 days. The keeper lifted the human-merge rule on
  2026-10-12; the never-edit list and the no-dispatch rule still stand.

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

The page is also the instrument panel (2026-10-08): a "Right now" row
(water, the buoy as it stands, the tide and its next turn, the satellite's
last clear look at Kd490 — a witness, never a judge — and the dry spell),
the water index as a UV-style band (`index_gauge_svg`; it is the setup
score, never a visibility claim), each window as a card with its FIVE
LIGHTS (`gate_axes`: flat · glass · dry · sun · warm, on / off / unknowable),
swell, wind, tide and cloud, and three days of tide drawn from the same
predictions the forecast reads (`tide_curve_svg`, `tide_at` — cosine
between highs and lows). The board's week strip carries the lights too.
All of it rides zones.json (`axes`, `swell`, `wind_kn`, `tide`, `tides`,
`buoy_now`, `kd490`, `chla` per bell) — no new source, no new fetch.
Round two (2026-10-09): the moon by arithmetic (`moon_phase`, from the
2000-01-06 new moon), plankton as a word (`chla_word`), the buoy's verdict
on the model (`anchor` = `buoy_anchor`), the water's week as a sparkline
(`sst_trend_days` from the marine SST series: four days behind, three
ahead), first light / sun up / sun down on each window card (derived from
the window itself), and the cove each ring named (`log_cove`, from
score_log). Instrument alarms are now ONE text per dead instrument naming
every bell it died at, not one per bell.

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

## The number that got into the state file (2026-10-29) — never again

From 2026-09-14 to 2026-10-29 the ring's delivery ledger
(`state["sms_rung_nums"]`) stored subscriber numbers, and `data/alert_state.json`
is tracked, committed and served. Two numbers sat in a public repo for six
weeks. A multi-agent red team found it; the keeper's own drills had not,
because none of them looked at what `save_state` wrote. Now: `num_tag`
(HMAC-SHA256 under the Twilio auth token, 16 hex) is all the ledgers ever
hold; `scrub_state` runs on every load and every save and replaces any
E.164 string anywhere in the state (tag inside the ledgers, `[redacted]`
elsewhere); failure logs print `#` + six hex, never six digits of a number;
drill (pi) writes a fake number through the whole path and greps the
serialized state. Still the keeper's decision: whether to rewrite the 189
commits of public history that carry the file (forks and every Pages
deploy hash change with it) and whether to tell the two subscribers.

## Found the same day, fixed the same day

- The Wednesday reading was marked PAID before the send loop; one 5xx
  mid-list silently cost a bell's divers their week and the per-week dedupe
  refused the retry. Now `sms_digest_nums` holds per-number tags and the
  week is paid only when Twilio accepted it for everyone; the rest stay owed.
- State reached disk only at the end of the run. `checkpoint_state` saves
  the live state right after any ring or reading is sent; the commit step
  is `if: always()`; page writes are wrapped so a page can never cost the
  state.
- `ledger_watch` judged a standing promise by the gate's arithmetic: the
  skill correction is not applied under 6h lead, so a ring that stood on it
  read as "taken back" at the final reading. A promise is about the WATER:
  it stands while the five physical lights stay on.
- A buoy-only morning (fewer than two lights) was frozen "unverified" on the
  first look; now it waits and is re-read until the weather record lands.
- `verdict_words` names the lights that testified and the ones nobody read
  ("held on glass, dry, sun (flat not read)"); the record page states the
  four rules and marks rings entered from the log.
- Every "a dozen mornings a year" on the site is gone; each bell's own
  record line speaks (La Jolla rang eight times in October).
- Crons moved off :00/:30 (GitHub's most crowded slots; the morning cron ran
  a median 262 min late) and the morning window opens at 2am local.
- bellwatch now reads the LIVE board's timestamp and alarms when the site
  serves a board more than 8h old (a Pages build was cancelled 2026-10-12
  and nothing noticed).
- `_sms_history` pages at Twilio's maximum (1000) to exhaustion.
- A missed morning is owed: `cmd_run` stamps `state["last_scored"]`; the
  guard's midday slot runs `python dive_alert.py owed` and does the full
  read when no scoring run has landed since 2am Pacific (`morning_owed`).
- The forecast text says when it was read ("the week ahead as of Thu
  1pm"), and the ear refuses an on-demand FORECAST when the bell's own
  plate is more than 36h old (`read_at` on each plate, kept by
  `merge_board` when a bell fails, so a carried plate on a fresh board still
  shows its true age; `STALE_HOURS` in incoming.js; re-run `wire-sms` after
  any edit there). "Stale" alone never refuses: the dated text speaks for itself.

## The record (2026-10-29) — every ring, and the morning after

The moat. A ring enters `state["ring_ledger"]` the moment it is made
(`ledger_promise`: score, cove, the forecast, and `buoy_expect_ft` — what
the bell's buoy should read if the forecast is right, in this bell's own
scale). Every run until the window, `ledger_watch` re-reads the physical
gate: collapsed = taken back. After the window, `ledger_verify` lets the
instruments testify (`observe_window`: the buoy's median height in the
window from NDBC's 45 days of rows; wind, rain and sky from the feed's past
hours or `archive_weather`) and judges HELD / BROKE (naming the light) /
UNVERIFIED. The rules are the gate's own: wind ≤ glass line, the 0.2 in
rain rule, cloud ≤ sun line, buoy ≤ expectation × 1.25 + 0.3. Without a
buoy expectation the flat light is simply not read — never guessed; that is
why Monterey's seeded October rings cannot be judged on the buoy (46236 is
offshore; McAbee sits in the lee). `ledger_seed` entered the pre-ledger
rings from the score log once ("stood at its last reading"), and the old
dawn-blind counter is gone: `rec.rings/held/broke/taken_back` and
`last_ring` now come from the ledger. Published at `/record/`,
`data/record.json`, each bell's ledger, the feed, and (when wired) a public
"kept its word / was wrong" post. The first real reading: La Jolla 6 of 8
held; two broke on rain. Nobody edits it.

## The feed and the search engines (2026-10-29)

Everything the bell says in public is also an item in `feed.xml` (RSS,
`write_feed`, from `state["feed"]`), so anyone — or any RSS-to-social
bridge — can carry it without keys. After every data commit the workflow
POSTs the sitemap's URLs to IndexNow (`indexnow.txt` holds the public
key); Bing, DuckDuckGo and the assistants that search through them learn
of every rewrite within minutes. Google ignores IndexNow and reads the
sitemap; the Search Console submission is still the keeper's one manual act.

## Ops texts are one line, and only when a human might act (2026-10-29)

`quiet_notices` groups dead sources by host and texts (`notify_ops(...,
text=...)`) only for load-bearing ones — the buoy, tides, the swell model,
the weather feed. CoastWatch dark at every bell is one app notice reading
"No action; the bells score on", never a text. 150 characters, no link.

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
- **A bell's buoys "declared down" while NDBC is up**: a retired station's
  404 was once counted as an outage (46240 Monterey and 46217 Ventura left
  realtime2 in Oct 2026) and two in a row tripped the breaker before Maui's
  turn. A 404 now resets the count (drill (ad)); the real fix is a new
  station ID in that zone's config.
- **`degraded: marine_alt failed (... handshake operation timed out)` on a
  few bells every run**: Open-Meteo's second rapid dial per bell stalls at
  the TLS handshake now and then (a fifth of bell-fetches, Oct 2026) while
  the first never does. `Sources._dial` redials once after a 2s breath
  before counting a failure (drill (ai)); if the line still shows on >5% of
  bell-fetches a week later, the pause is too short, not the limit too low.
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
