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
| 2026-10-12 | runbook: the review merges its own PR once drill+tests+audit pass (07182da) | keeper's decision | — | — | rule |
| 2026-10-12 | gate_flip_axes + `flips` column in gate_skill.csv (9db88de) | 53 of 71 far promises lost; sun 31, flat 23, glass 16 | the first monthly `flips` column has an axis other than sun on top, or the drill (ag) breaks on real rows | 2026-11-04 | open |
| 2026-10-12 | drift_log.csv gains a `zone` column (9db88de) | rows were unattributable; drift.yml order A D E F H I J K O | the 2026-11-03 drift run writes rows without a zone, or append_log misaligns old rows | 2026-11-04 | open |
| 2026-10-12 | per-zone `model_height_scale`: D La Jolla 0.73, O Sydney 1.38 (9db88de) | drift log two months: D +46%/+37% (0.69, 0.73); O -27.5%/-27.4% (1.38, 1.38) | the 2026-11-03 validate suggests a value >10% from the one set, or buoy-down scores at D/O drift away from buoy-up scores instead of toward them | 2026-11-04 | open |
| 2026-10-12 | skill correction per bell: `zone` rows in skill_log.csv, `skill_adjust` (9db88de) | fleet bias -0.70 at 24-48h (n=1970) pinned at the ±0.5 cap while bells miss in opposite directions | a bell's own bias is farther from zero than the fleet's was, or a bell's correction flips sign month to month | 2026-12-04 (two gradings) | open |
| 2026-10-12 | buoys: E 46240→46236, N 46217→46053 (9db88de) | 46240 and 46217 404 on .txt and .spec; E/N completeness 0.566/0.583 vs fleet ~0.66 | E completeness not above 0.62 within a week; N: 46053 still silent on 2026-10-19 → switch to 46251 | 2026-10-19 | open |
| 2026-10-12 | a 404 no longer trips the dead-host breaker; no bell dials none.spec (cdc5a94) | three runs/week: NDBC "declared down" before Maui every run; Maui completeness 0.568 vs Oahu 0.649 / Kona 0.661 | any dive.yml log after merge with "declared down" for www.ndbc.noaa.gov while NDBC is up, or Maui completeness still <0.62 after a week | 2026-10-19 | open |
