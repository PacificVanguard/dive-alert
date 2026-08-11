# dive-alert

Scores Laguna Beach dive **setups** 1–10 and pushes ntfy notifications.

**What the score means, precisely:** how calm, dry, sunny and easy the window
will be. It does NOT predict visibility. Measured over 265 days, the score has
no relationship to independently observed water clarity (r=+0.07), because
clarity decorrelates in ~3 days and the only direct measurement arrives ~10
days late. So the app finds the days with the best odds and says the wildcard
out loud — it positions you for a great dive rather than promising one.

Two rhythms:

- **Wednesday 7am — the week ahead.** A digest every week no matter what,
  showing all seven days at a glance and naming the day worth planning
  around. Wednesday because 3–4 day forecast skill makes the coming weekend
  genuinely readable; a Monday digest would be guessing about Saturday.
- **Twice daily — the opportunistic ping.** Fires only when a window clears
  7.0 at 12–48h lead, with streak logic so a good stretch doesn't spam you.
- **The conjunction gate — "everything just lined up."** A strict AND across
  flat / glass / dry / sun / warm / score, with no partial credit and no
  trading one axis against another; anything unknown fails closed. Tuned to
  ~21 days a year. This is the only path to the loud voice.

One file, stdlib only, runs on GitHub Actions. Everything tunable is in
`CONFIG` at the top of `dive_alert.py` — search for `CALIBRATION:` comments.

**Get set up** — `python3 dive_alert.py setup` generates a topic, sends a
welcome push, and prints the GitHub commands; add `--github` to run them.
Change the digest day with `alerting.digest_weekday` (0=Mon).

**Add a dive buddy (30 seconds, no account)** — `python3 dive_alert.py share`
prints an invite to paste into a text. Buddies just install ntfy and subscribe;
they get every alert, and their feedback button presses feed the same
calibration log — every subscriber is another ground-truth sensor. (The topic
name is the only access control, so share it like a group-chat invite, not a
public post.)

**Tell it how it did** — every alert carries 🤿 Epic / 👍 Decent / 👎 Meh
buttons. Press one after you dive; the next run folds it into
`data/dive_log.csv`. That's the calibration loop, and it needs no secrets.

**Check the data sources** — `python3 dive_alert.py validate` compares the
swell model against ~45 days of buoy observations and tells you whether
`model_height_scale` needs moving. Re-run each season.

**Change a weight** — edit the anchor lists in `CONFIG["scoring"]`
(`surf_penalty`, `period_factor`, exposure map per zone). Each is
piecewise-linear `(input, output)` points. Run `python3 dive_alert.py test`
after; the scenario tests are the spec.

Two knobs interact and must be re-fit together: each zone's
`cove_damage_factor` (how hard that coastline gets hit — Laguna's sites are
pocket coves, not open beach) and the global `surf_penalty` curve (how damage
maps to 1–10). Change one, rebuild `hindcast`, and check the distribution:
today it yields ~4.4 windows/week ≥7 across 180 days (31%). Streak logic and
the 12–48h lead filter mean far fewer actual notifications than that.

**Edit the voice** — `CONFIG["voice"]`: hook strings per tier plus the
downgrade template. Words only, no logic.

**Edit the tip library** — `CONFIG["tips"]`, keyed by site. Tags: `viz`
(any/high), `surge` (any/low), `tide` (any/low/mid/high), `season`
(any/summer/winter), `lane`. No `take` lane while Zone A is the only zone —
it's all State Marine Reserve / no-take SMCA.

**Enable Zone B/C** — flip `enabled: True` on the zone, then re-verify each
site's `take_allowed` against current CDFW MPA boundaries (comments mark the
ones needing checks). Night windows + moon logic wake automatically for
take-legal sites in lobster season.

**Read a backtest** — log dives as you do them:
`python3 dive_alert.py log-dive --site "Shaw's Cove" --viz 15 --surge low`
then `python3 dive_alert.py backtest`. It joins your logged viz against the
scored features and prints correlations: score and dry_hours should be
positive, damage and wind negative. If not, adjust the CALIBRATION anchor the
table points at.

**Rotate the ntfy topic** — pick a new random topic string, update the
`NTFY_TOPIC` Actions secret (repo → Settings → Secrets → Actions), and
re-subscribe in the ntfy phone app. Nothing in the repo changes.

**Local runs** — `python3 dive_alert.py run --dry-run` (prints, sends
nothing), `--offline` (fixtures, no network), `test` (scenario suite),
`hindcast --days 180` (rebuild history). Secrets locally via git-ignored
`local_config.json`: `{"NTFY_TOPIC": "...", "HEALTHCHECKS_URL": "..."}`.


**Tune what "perfect" means** — `CONFIG["perfect_gate"]`. Every condition must
pass. Loosening to damage 2.5 / cloud 40% / score 8.0 gives ~47 days a year
(roughly weekly, stops feeling like an event); the shipped 1.5 / 25% / 8.5
gives ~21. Note `min_dry_hours` can never exceed `features.rain_lookback_h` —
dry_hours saturates there, and asking for more silently disables the gate.

**A finding worth keeping** — two independent analyses (satellite clarity, and
the gate) both say Laguna's best setups cluster **October–January**, not
summer. Summer brings persistent south swell and a marine layer that kills the
light.
