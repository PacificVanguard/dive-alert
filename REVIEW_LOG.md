# Review log — what the loop changed, and what would prove it wrong

The daily improvement loop (`~/.claude/scheduled-tasks/dive-bell-model-review`,
described in RUNBOOK "Self-correction") reads this file before it chooses a
change and writes one row for every change it lands. A row is a promise with
a falsifier: the loop re-checks every `open` row whose check date has come,
marks it `held` or `proven wrong`, and a proven-wrong row's revert is that
day's first change. Newest first. Append-only; never edit a closed row.

Constants and stations touched here are frozen for 14 days afterwards — the
evidence must accumulate before the same knob is turned again, or the loop
oscillates instead of converging.

| date | change (commit) | evidence | proves it wrong | check by | status |
|---|---|---|---|---|---|
| 2026-10-14 | no anchor for a bell whose primary buoy is its offshore buoy: K Kona 51003, I Santa Barbara 46054 (`buoy_anchor`) | zones.json `anchor` on the four sampled runs 2026-10-09..14 (the field's whole history): K 1.39/1.58/1.60/1.56, I 1.11/1.34/1.45/1.29; K live median 1.0 for 21 days, 96% of windows ≤1.5 this week, vs casting pct7 23% and an Oct-2025 hindcast median 6.4 with 0% ≤1.5; I 0 windows ≥7 in 21 days vs casting 31%; the hindcast never carries the anchor; 51003 sits 205 NM SW of Honolulu reading 2.2 m while the Kona cell reads 0.9 m | K's live median over 2026-10-15..11-13 stays ≤1.5 while its hindcast for the same days reads ≥4 (the anchor was not what pinned it), or K or I rings and the first word back is FINS | 2026-11-14 | open |
| 2026-10-13 | one redial after a stalled TLS handshake, before a failure is counted (`Sources._dial`) | ten dive.yml runs 2026-10-09..13: marine_alt lost 31 of 150 bell-fetches, weather 11, all "handshake operation timed out", primary marine 0; score_log 14 days: 40 of 540 bell-runs (7.4%) blind to wind/cloud/rain | "handshake operation timed out" still appears on more than 5% of bell-fetches across a week of dive.yml logs (the redial did not help), or a dive.yml run exceeds 10 minutes | 2026-10-20 | open |
| 2026-10-13 | per-zone `model_height_scale`: J Oahu North Shore 0.85 | the unlabeled +14.3% / +17.9% drift rows (2026-09-03, 2026-10-03; suggested 0.88, 0.85) are J's: a read-only 45-day re-read of 51201 today gives +17.7% (n=1082, r=0.908, both halves +15.6/+19.9%, suggested 0.85) while I 46054 reads +2.6% and K 51003 +1.1% | the 2026-11-03 validate row for J suggests a value >10% from 0.85, or J's buoy-down windows score farther from its buoy-up windows of the same day than they did in October | 2026-11-04 | open |
| 2026-10-12 | runbook: the review merges its own PR once drill+tests+audit pass (07182da) | keeper's decision | — | — | rule |
| 2026-10-12 | gate_flip_axes + `flips` column in gate_skill.csv (9db88de) | 53 of 71 far promises lost; sun 31, flat 23, glass 16 | the first monthly `flips` column has an axis other than sun on top, or the drill (ag) breaks on real rows | 2026-11-04 | open |
| 2026-10-12 | drift_log.csv gains a `zone` column (9db88de) | rows were unattributable; drift.yml order A D E F H I J K O | the 2026-11-03 drift run writes rows without a zone, or append_log misaligns old rows | 2026-11-04 | open |
| 2026-10-12 | per-zone `model_height_scale`: D La Jolla 0.73, O Sydney 1.38 (9db88de) | drift log two months: D +46%/+37% (0.69, 0.73); O -27.5%/-27.4% (1.38, 1.38) | the 2026-11-03 validate suggests a value >10% from the one set, or buoy-down scores at D/O drift away from buoy-up scores instead of toward them | 2026-11-04 | open |
| 2026-10-12 | skill correction per bell: `zone` rows in skill_log.csv, `skill_adjust` (9db88de) | fleet bias -0.70 at 24-48h (n=1970) pinned at the ±0.5 cap while bells miss in opposite directions | a bell's own bias is farther from zero than the fleet's was, or a bell's correction flips sign month to month | 2026-12-04 (two gradings) | open |
| 2026-10-12 | buoys: E 46240→46236, N 46217→46053 (9db88de) | 46240 and 46217 404 on .txt and .spec; E/N completeness 0.566/0.583 vs fleet ~0.66 | E completeness not above 0.62 within a week; N: 46053 still silent on 2026-10-19 → switch to 46251 | 2026-10-19 | open |
| 2026-10-12 | a 404 no longer trips the dead-host breaker; no bell dials none.spec (cdc5a94) | three runs/week: NDBC "declared down" before Maui every run; Maui completeness 0.568 vs Oahu 0.649 / Kona 0.661 | any dive.yml log after merge with "declared down" for www.ndbc.noaa.gov while NDBC is up, or Maui completeness still <0.62 after a week | 2026-10-19 | open |
