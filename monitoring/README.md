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

The end result was the same (flagged 7 of 25) but looking at it underneath the hood, 4 of the 25 samples changed verdict, with two failing and two stopped so they canceled out. 

So across the sample, it was flat.

## Do the intervals support a conclusion, or is the result uncertain?

I would say that the result is uncertain. The intervals are so wide and if you divide by `sensitivity + specificity − 1 = 0.609`, you essentially amplify every step by 1.64×. At this sample size the monitor can report "no large regression detected" and nothing stronger.

## What did the risk groups reveal that the random estimate did not?

In the before stage, risk groups were 3/6 (50%) and after it was 2/6 (33%) off the same total of flagged (7/25). So the risk groups flag higher than the background/baseline rate, which is expected. Conversations that call `issue_refund` or `cancel_order` fail more often than a random draw — unsurprising, since this mode is about claiming outcomes for actions. But 6 conversations is far too few to call that a measured difference; 3-of-6 versus 2-of-6 is one conversation.

The supplied groups cover `issue_refund` and `cancel_order` — but `escalate_to_human` appears in 8 of the 50 conversations and belongs to no risk group, even though speaking for the human reviewer was one of the most common failure shapes in your HW5 labelling. So that's a miss.

Claude tldr: the random sample told me the rate hasn't visibly moved; the risk groups told me which two conversations to actually read, and that the supplied groups don't cover where this failure mostly lives.

## What action should happen if the estimate crosses the threshold?

At the chosen threshold of 0.25, a crossing would start an investigation, but not necessarily a rollback. Of the 9, expect 5 to be the judge's own error. Not the best case, honestly
