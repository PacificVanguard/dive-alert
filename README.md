# dive-alert

Scores Laguna Beach dive windows 1–10 and pushes ntfy notifications. Two
rhythms:

- **Wednesday 7am — the week ahead.** A digest every week no matter what,
  showing all seven days at a glance and naming the day worth planning
  around. Wednesday because 3–4 day forecast skill makes the coming weekend
  genuinely readable; a Monday digest would be guessing about Saturday.
- **Twice daily — the opportunistic ping.** Fires only when a window clears
  7.0 at 12–48h lead, with streak logic so a good stretch doesn't spam you.

One file, stdlib only, runs on GitHub Actions. Everything tunable is in
`CONFIG` at the top of `dive_alert.py` — search for `CALIBRATION:` comments.

**Get set up** — `python3 dive_alert.py setup` generates a topic, sends a
welcome push, and prints the GitHub commands; add `--github` to run them.
Change the digest day with `alerting.digest_weekday` (0=Mon).

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
