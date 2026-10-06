# Notes on costs.csv

Created by `uv run python profile/scripts/costs.py`. The script reads committed files only and makes no model calls.

## Measured and estimated counts

- **API counts:** HW3 input tokens (from Langfuse), and every token on the HW8 evaluation replay rows (from `optimize.runner`).
- **Estimated:** everything else, counted with the o200k tokenizer. Each row's `purpose` says which.
- **HW3 output tokens:** the export recorded 0 output tokens and no output text on all 738 generations. The script counts output from the final replies plus the tool-call arguments. That is a lower bound, for two reasons: Claude's tokenizer gives more tokens than o200k, and text written before a tool call was not kept.
- **`calls`:** this counts model requests on the HW3 and judge rows. On the HW8 replay rows it counts evaluated conversations, because the runner does not save per-request counts.

## Left out on purpose

- **`unsupported_policy_claim` (600 verdicts on claude-opus-4-6):** the instructor ran this judge and committed it with the starter code. I did not pay for these calls.
- **GEPA reflection calls (claude-opus-4-6, HW8 Part D):** these rewrite prompts. They are neither customer conversation nor judge calls, and GEPA saved no token counts for them.
- **HW6 baseline and CI judge calls, and scheduled HW7 monitor runs:** no results were saved locally, so they cannot be counted.
