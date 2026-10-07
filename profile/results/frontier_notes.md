# Frontier notes

`frontier.csv` is written by `profile/scripts/frontier.py` from four saved development results in `optimize/results/`. Each comes from gpt-4o-mini on the same 11 development case runs.

## The costs are not all priced the same way

`cost_per_100_conversations_usd` is copied from what the runner reported. The runner bills cached input tokens at the cached price only when `cached_input` is set for the model in `optimize/config.json`. For gpt-4o-mini, that price was added before Part C:

| candidate | cached input tokens | cached price applied | reported $/100 | $/100 with no cache discount |
|---|---|---|---|---|
| final (HW8) | not recorded (runner predates cached-token reporting) | no | 0.0926 | 0.0926 |
| fewer-tokens | 45,312 of 62,099 | no | 0.1005 | 0.1005 |
| cache-before | 34,816 of 54,435 | yes | 0.0636 | 0.0874 |
| cache-after | 34,176 of 50,753 | yes | 0.0589 | 0.0822 |

At the cached price, fewer-tokens would cost 0.0696 per 100 conversations.

The `frontier_status` labels are the same under all three pricings: reported, no cache discount, and fewer-tokens repriced. cache-before has the higher score and the lower cost than both final and fewer-tokens in every case, and cache-after is the cheapest in every case.

## final and cache-before are the same agent

Both runs use prompt hash `fb19876449fe` and the same agent code. The cache-before run exists only to measure caching before the Part C reorder. The two runs differ because of run-to-run noise:

- The score difference (0.571 vs 0.600) comes from the flaky case e-002.
- The cost difference is mostly the cached-token discount, plus normal variation in token counts.

So "cache-before dominates final" means one run of the HW8 final agent came out ahead of an earlier run of the same agent. It is not evidence that a different configuration is better.

## write_pass_5

`write_pass_5` is 0 for every configuration, so it does not separate them. None of these configurations meets a high `write_pass_5` requirement, e.g., for refunds.

## Reverted changes

- **fewer-tokens (Part B):** reverted in `f7e5a02`. Its cost and input tokens went up, with the same score.
- **cache-after (Part C):** reverted in `cbcfbd8`. Its score dropped from 0.600 to 0.486.

The agent in the repository is the HW8 final agent, the same agent as final and cache-before.

## Configuration choice

The student chose **cache-before**: the HW8 final agent, already in the repository, running on gpt-4o-mini with prompt caching. It has the highest development score of the four configurations and is on the frontier. It needs no code change, because the caching discount comes from the LLM API.
