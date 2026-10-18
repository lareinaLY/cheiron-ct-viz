# Planner eval

Run on 2026-10-17 with `scripts/eval_planner.py` over `tests/eval/questions.yaml` (15 questions).
Each question goes through the real planner (prompt, merge, validation, one retry).

## Per-field accuracy

| model | outcome | cohort_count | filters | grouping | granularity | top_n | all fields correct | mean latency | p50 | max | retries |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `gpt-4.1-mini` | 15/15 | 13/13 | 13/13 | 13/13 | 4/4 | 13/13 | 15/15 | 1.2s | 1.2s | 1.4s | 0 |
| `gpt-5-mini` | 15/15 | 13/13 | 13/13 | 13/13 | 4/4 | 13/13 | 15/15 | 6.6s | 5.9s | 10.7s | 0 |

## Per question

| id | class | `gpt-4.1-mini` | `gpt-5-mini` |
|---|---|---|---|
| trend_drug_year_range | trend | ok (1.4s) | ok (4.1s) |
| trend_drug_monthly | trend | ok (1.1s) | ok (10.7s) |
| trend_condition_over_time | trend | ok (1.1s) | ok (7.2s) |
| distribution_brand_name_phase | distribution | ok (1.1s) | ok (5.9s) |
| distribution_sponsor_class | distribution | ok (1.1s) | ok (5.8s) |
| distribution_enrollment | distribution | ok (1.2s) | ok (7.3s) |
| comparison_two_drugs_phase | comparison | ok (1.3s) | ok (5.8s) |
| comparison_statuses_abbreviation | comparison | ok (1.3s) | ok (6.5s) |
| comparison_start_years_shared_filter | comparison | ok (1.3s) | ok (5.2s) |
| geographic_top_n_phase | geographic | ok (1.2s) | ok (5.9s) |
| geographic_sponsor_recruiting | geographic | ok (1.1s) | ok (4.8s) |
| network_sponsor_drug | network | ok (1.2s) | ok (8.3s) |
| network_condition_drug | network | ok (1.3s) | ok (5.3s) |
| unsupported_adverse_events | unsupported | ok (1.1s) | ok (7.6s) |
| ambiguous_two_groupings | ambiguous | ok (1.3s) | ok (8.6s) |

## Misses

### `gpt-4.1-mini`

No misses.

### `gpt-5-mini`

No misses.

## Observations from this run (written by hand)

- Both models planned all 15 questions correctly on this run, with no validation
  retries: names copied verbatim (Ozempic, NSCLC, Alzheimer's), no invented filters,
  shared filters copied into both cohorts of a comparison, `top_n` set only when a
  number was given, and the unsupported (adverse events) and ambiguous (two groupings)
  questions declined with structured errors.
- Run-to-run variance is visible: on the first full run of the same day, `gpt-5-mini`
  missed `trend_drug_monthly` ("Monthly number of new semaglutide trials since 2024")
  by asking a clarification. Repeating that question 5 times per model: `gpt-4.1-mini`
  planned `start_month` 5/5; `gpt-5-mini` planned it 4/5 and once asked whether
  "monthly" meant month-of-year pooled across years or a year-month series (the pooled
  reading is not a supported dimension). A spurious clarification is a safe failure
  (a 422 with options, never a wrong chart) but costs the user a round trip.
- `gpt-5-mini` is about 5x slower (mean 6.6 s vs 1.2 s per question, max 10.7 s) for no
  accuracy gain on this set. `gpt-4.1-mini` stays the default (first entry of `LLM_FALLBACK_CHAIN`).

## Later additions

- 2026-10-17: `geographic_ranking_without_number` ("Which countries have the most
  recruiting trials for non-small cell lung cancer?", expected `top_n` 15 from the
  ranking default) was added with the `ranking_intent` prompt change and run alone with
  `--ids`: `gpt-4.1-mini` got every field right (2.9s). The tables above predate this
  case and the prompt change; the full set has not been re-run.

## Limitations

- 15 questions detect systematic failures (a query class the planner cannot map, a rule
  it ignores), not statistically robust accuracy: one miss moves a field by 7-8 points,
  and a perfect score does not mean the planner is always right.
- Results are for one prompt version on one day. Model outputs vary between runs at the
  same settings, providers update models behind the same name, and any change to the
  prompt, few-shot examples or registry descriptions needs a new run.
- Questions were written by the developer of the prompt, so they share its vocabulary;
  real users phrase questions differently and misspell names.
- Latency is wall-clock time from one machine, including the network and any retry.
