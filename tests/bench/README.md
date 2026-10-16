# find_tool datasets

All questions are synthetic, offline examples, not saved network traffic.

- `find_tool_questions.yaml`: original 60-question development benchmark. Labels
  are unchanged, including older alternatives that may not cover newer tools.
- `find_tool_development.yaml`: 30 additional development questions. Used to
  diagnose failures and choose the router change, not as held-out evidence.
- `find_tool_evaluation.yaml`: 36 fresh synthetic questions (12 per product),
  frozen before router edits. Aggregate baseline scores only were inspected
  before tuning. Do not tune on its misses. After the first final comparison it
  is regression data; another tuning cycle needs new evaluation questions.
- `find_tool_baseline.json`: aggregate measurements from merged commit
  `72e41cea43530a896aa43e256291b827cadaf46d` (includes PR #12), and dataset SHA-256
  hashes. The original 75-question held-out set was not available. These sets
  do not reproduce or replace its reported results.

## Development failure classification before router edits

Product-filtered first-pick failures in the additional development set:

| Question | Expected rank (top ten) | Category |
| --- | --- | --- |
| central create a webhook | 2 | wrong action |
| port keeps flapping on the switch | 2 | wrong ordering |
| aruba remove a report | 5 | wrong action |
| mist create a wlan | 8 | wrong action |
| mist remove a wlan | 2 | wrong action |
| mist restart an access point | 7 | wrong action |
| clearpass remove a guest | 4 | wrong action |

Classification meanings:

- **candidate missing**: no acceptable tool has a name-token match after query
  expansion (not merely absent from the top ten).
- **wrong action**: the first hit performs a different action from the request.
- **wrong scope**: the action matches, but the hit operates on a different scope.
- **wrong ordering**: another ranking failure with an acceptable candidate present.

There were six wrong-action failures, one wrong-ordering failure, and no missing
candidate or wrong-scope failures. Every expected tool for these failures had a
name-token match. Description-based retrieval is therefore not justified by this
sample and is not part of this change.

All six wrong-action questions start with a product name or alias. Intent parsing
stops at that word, so the action verb is never considered. The chosen change is
to skip a leading product name/alias alongside conversational filler. Scope,
scoring weights, summaries, synonyms and candidate retrieval stay unchanged.

Run a set with:

```
uv run python scripts/bench_find_tool.py --questions tests/bench/find_tool_development.yaml
uv run python scripts/bench_find_tool.py --questions tests/bench/find_tool_evaluation.yaml --json
```

Reports distinguish wrong-first results from top-three misses, and include the
first acceptable answer's rank (or `>10`), top-ten accuracy and MRR. Product-
filtered and unfiltered scores are separate. Latency remains warm, filtered
`top_k=3` calls, three rounds per question; no network or backend invocation.
