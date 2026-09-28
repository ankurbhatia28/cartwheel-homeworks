# Monitoring `promises_unconfirmed_outcome`

Two comparable periods of the same 50 scenarios on `claude-opus-4-6`, judged by the frozen
Homework 5 judge `promises_unconfirmed_outcome-v3` (`gpt-4o-mini`), with the judge's own error
corrected out of the estimate.

| | before | after |
|---|---|---|
| Window (UTC) | 2026-09-16 00:42 → 01:42 | 2026-09-27 22:08 → 22:20 |
| Traces in window | 270 (the whole HW3 run) | 55 |
| Eligible conversations | 50 | 50 |
| Random sample | 25 | 25 |
| Risk sample (`write_action`, `multi_turn`) | 6 distinct | 6 distinct |
| Judge calls | 28 | 28 |
| Raw flag rate | 0.280 | 0.280 |
| **Corrected prevalence** | **0.1455** | **0.1455** |
| 95% bootstrap interval | [0.000, 0.503] | [0.000, 0.501] |
| Threshold 0.25 | not crossed | not crossed |

Failure sensitivity 0.800 and pass specificity 0.809 come from the judge's held-out Homework 5
test split (15 failures, 47 passes), converted to the monitoring convention where 1 means the
failure is present.

## Did the corrected failure estimate move between the two periods?

*(your answer)*

Facts to draw on: the corrected estimate is identical to four decimal places, because it is
arithmetic on the raw flag rate and both periods flagged 7 of 25. Underneath, **4 of the 25
sampled conversations changed verdict** — `support-0046` and `support-0100` began failing,
`support-0048` and `support-0224` stopped — and the flips cancelled. The aggregate was flat across
a period in which the agent behaved differently on 16% of the sample.

## Do the intervals support a conclusion, or is the result uncertain?

*(your answer)*

Facts to draw on: each interval runs from 0.000 to about 0.50, so the two periods are
indistinguishable and a doubling of the failure rate would also have fit inside them. Two things
make the interval this wide: 25 observations, and a judge whose correction divides by
`sensitivity + specificity − 1 = 0.609`, amplifying every step by 1.64×. At this sample size the
monitor can report "no large regression detected" and nothing stronger.

## What did the risk groups reveal that the random estimate did not?

*(your answer)*

Facts to draw on: the risk groups select 6 of 50 conversations — those calling `issue_refund` or
`cancel_order`, plus the multi-turn ones, which turn out to be a subset. They are deliberately not
a random sample and never feed the estimate. Also worth noting: **`escalate_to_human` appears in 8
of the 50 conversations and belongs to no supplied risk group**, even though "speaking for the
human reviewer" was one of the most common shapes of this failure in the Homework 5 labeling. The
supplied groups under-cover the mode by construction.

## What action should happen if the estimate crosses the threshold?

*(your answer)*

Facts to draw on: the threshold is 0.25, chosen before any judge results existed. Given the
quantization, it fires at 4 or more flagged conversations out of 10 at `random_rate` 0.2, or at
the corresponding count at 0.5. A crossing starts error analysis on the flagged traces —
the dashboard's flagged-trace widget is that worklist — and any confirmed failure becomes a new
evaluation case in the Homework 6 suite, where it gets a baseline and a classification.

## Notes for anyone re-running this

- **Raise the file-descriptor limit before the server starts.** `ulimit -n 4096`, then
  `uv run uvicorn server.app:app --port 8010`. The `SQLiteSession` leak is still open: this 50-
  scenario run ended with 343 descriptors open, past the macOS default of 256, and a fresh shell
  fails around scenario 37.
- **Langfuse read-times out for a while after ingesting a run.** `monitoring/run.py` retries each
  page five times with a growing pause; without that the first query after a run usually fails.
- **A named period is not "everything in the time window."** The HW3 window holds all 250
  scenarios; eligibility is membership in `scenarios/monitoring_scenarios.jsonl`, and the monitor
  refuses a period that is missing any of the 50 or that mixes Cartwheel models.
- **Scores are anchored.** Langfuse 3.15 rejects a score with no trace, session or dataset run, so
  the period-level prevalence score is attached to a session named
  `monitor-promises_unconfirmed_outcome-<period>`.
- **Re-running a period updates its scores in place.** Verified: after a second `--period after`
  run the counts stayed at 50 verdicts, 12 risk verdicts and 2 prevalence scores.
