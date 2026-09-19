# Review interface comparison

Interface: `analysis/review_app/` (`build_data.py`, `server.py`, `index.html`, `sample.py`,
`draft_labels.py`). Reference: `analysis/server.py` and `analysis/ui/index.html`.

**Data source:** live Langfuse, restricted to the HW3 final run (2026-09-16 00:40–01:45 UTC; 250
conversations, 276 traces). No offline fallback was needed.

## What made review slow in Langfuse's own view

*(Reviewer: add your own observations from the start of Part A here.)* Found while inspecting 10
traces before designing the app:

- **A multi-turn conversation is split into separate traces**, one per user turn. Langfuse's
  `sessionId` field is empty for these traces (the session id is in `metadata.attributes`), so the
  Sessions view can't regroup them.
- **The agent's thinking and its text before each tool call are hidden inside the next generation's
  input.** Every `openai.response` generation has `output = null`, so the reasoning isn't where the
  tree suggests it is.
- **Tool results are separate spans**, away from the reasoning that led to the call and the reply that
  described the result.

## One design retained from the reference

**The file-backed server, and role colors with margin notes.** The app keeps the reference's
standard-library HTTP server and its JSON API over plain files in `analysis/state/`
(`/api/samples`, `annotations`, `patterns`, `suggestions`, `graph`). The browser auto-saves every
annotation, and the coding agent reads the same files.

It also keeps the reference's visual language:
- one hue per message role (user, reply, tool call, tool result)
- free-text notes in a right-hand margin aligned with the highlighted text
- a treemap of failure modes with their supporting notes
- agent suggestions visually separate from human notes, with explicit accept and dismiss

## One design changed after inspecting the traces

**Conversations grouped by session, with each turn rebuilt as an ordered timeline.** The reference
shows one trace at a time. After seeing that Cartwheel writes one trace per turn and stores
generation outputs as null, the app was changed to:

- group traces by `cartwheel.session_id` and show every turn of a conversation in order
- rebuild each turn from its last generation's input: user message → **thinking** → **reasoning
  text** → tool call → tool result → … → reply
- put the thinking, reasoning, tool call and tool result of one step in **one container**, so a result
  sits next to the call and the words that led to it
- show thinking fully (grey, italic), because first failures were often in the reasoning
- stop muting system messages by role (the reference dims them to 55%); only the static part of the
  system prompt is collapsed, and the per-session context stays visible
- collapse the HW3 extra metadata (expected outcome) by default, to avoid anchoring the open code

Additions the handout required that the reference lacks: a structured Pass/Fail labeling grid that
writes label files and Langfuse scores; per-batch progress; a cluster map used to draw batch 1; and
seeded, recorded batch draws.

## One limitation remaining

**Suggested labels may anchor the structured labeling pass.** To make 800 judgments tractable, the
Labels view pre-fills a suggested Pass/Fail per cell, from the reviewer's notes, two code checks, and
applicability rules. The final labels match the suggestions in 798 of 800 cells. Where a suggestion
came from the reviewer's own note that's expected, but for "no note" cells the agreement can't be
told apart from anchoring. A blind re-label of a sample of those cells, or a second reviewer, would
settle it.

Smaller limitations:
- Margin notes attach to the first occurrence of the quoted text in a block.
- The conversation cache must be rebuilt (`build_data.py`) to see new traces.
- An early version of the save path wrote one Langfuse score per request and allowed repeated
  clicks. That was fixed during labeling: saves are now single-pass and idempotent, with a stable
  score id per trace and mode, and duplicate scores were removed.
