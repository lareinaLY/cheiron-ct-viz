# User review: a CI analyst tries the service

Reviewer role: competitive-intelligence analyst at an oncology biotech, preparing a leadership
briefing on (a) the pembrolizumab combination landscape and (b) where recruiting NSCLC trials
are concentrated. I called `POST /query` with plain-English questions only (no explicit
filter fields), the way I would use it from a frontend. Server: `uv run uvicorn
ctviz.api.app:app`, 2026-10-18. Data timestamp reported: 2026-10-16T09:00:05.

I spot-checked a few numbers against the ClinicalTrials.gov API directly (noted where I did).

## 1. Questions and outcomes

Latency is wall-clock for the HTTP call. "Answered?" is my judgement as the person asking.

| # | My question | What came back | Top data points | Answered? | Problems from my point of view |
|---|---|---|---|---|---|
| 0 | How has the number of pembrolizumab trials changed over the years? | `time_series`, "Trials by start year for pembrolizumab", 2.4 s, 60 KB | 2022: 263, 2017: 246, 2018: 240 … 2026: 179 (in progress), 2027: 8 (projection), Unknown: 4. Total 2642 | Mostly | Contains trials "starting" in 2008 and 2010, before pembrolizumab existed (NCT04898751 is a pharmacovigilance study of ICI skin toxicity; NCT06607393 is a "real-world emulation" of KEYNOTE-189). "Trials" includes observational studies. Five warnings, one of which ("start date with no ACTUAL/ESTIMATED type (legacy records)") I could not interpret. |
| 1 | How many new pembrolizumab **combination** trials started each year since 2018? | `time_series`, "Trials by start year for pembrolizumab, started 2018 or later", 3.8 s | Same numbers as #0 (2022: 263 …), total 2036 | **No** | "Combination" was silently dropped. The numbers are all pembrolizumab trials. No warning or assumption says so. I would have put this on a slide as "combination trials". |
| 2 | What's the phase breakdown of pembrolizumab trials in non-small cell lung cancer? | `bar_chart`, "Trials by phase for pembrolizumab, non-small cell lung cancer", 2.5 s | Ph2: 176, Ph1: 146, Ph3: 120, Ph1/2: 109, Unknown: 21. Total 601 | Yes | 601 here, but 619 for "pembrolizumab, NSCLC" in #10. The phrasing changes the number. |
| 3 | Compare the phase mix of pembrolizumab, nivolumab and atezolizumab trials | `grouped_bar_chart`, "Trials by phase: pembrolizumab vs nivolumab vs atezolizumab", 2.4 s | Ph2: pembro 1163 / nivo 765 / atezo 346; Ph3: 314 / 152 / 111 | Partly | "Mix" means shares to me. I got absolute counts of very different-sized cohorts (2642 / 1760 / 806), so the bars mostly show size. 206 trials are in more than one cohort. That is reported but easy to miss. |
| 4 | Pembrolizumab vs durvalumab: trials started per year since 2015 | `time_series`, "Trials by start year: Pembrolizumab, started 2015-2026 vs durvalumab, started 2015-2026", 5.6 s | Pembro peaks 2022 (263); durva peaks 2018 (94), 2026: 46 | Yes | I never said "to 2026". An upper bound was invented and only shows up in the title. Cohort labels are "Pembrolizumab" vs "durvalumab" (inconsistent capitalisation). Six warnings, all prefixed with a cohort label. |
| 5 | Where are recruiting NSCLC trials located? Top 15 countries | `bar_chart`, "Trials by location country for NSCLC, recruiting (top 15)", 2.5 s | China 613, US 534, Spain 176, France 172, Italy 160. 1366 trials | Yes | Bars add up to 2682 against 1366 trials (multi-country trials count once per country). It is explained in `assumptions`, but the title and axis just say "Trials". |
| 6 | Which US states have the most recruiting NSCLC trials? | 422 `unsupported_question` | — | No (fair) | The message is garbled: "does not provide recruitment counts by US states". The registry *does* list site states. It doesn't suggest the nearest thing it *can* do (country, or filter country = United States). |
| 7 | Who sponsors the most pembrolizumab trials? | `bar_chart`, "Trials by lead sponsor for pembrolizumab (top 15)", 1.1 s | MSD 267, MD Anderson 75, NCI 73, MSKCC 37, Dana-Farber 31 | Partly | The top 15 cover 686 of 2642 trials. **1956 trials (74%) are invisible**, and there is no "Other" bar. Lead sponsor only. AstraZeneca, BMS, Roche and Pfizer are all in the omitted tail. (MSD = 267 matches my own API check: 266–274.) |
| 8 | Top 10 sponsors of recruiting NSCLC trials | `bar_chart`, "Trials by lead sponsor for NSCLC, recruiting (top 10)", 1.1 s | AstraZeneca 30, Sun Yat-sen Univ 22, MSD 18, MSKCC 17, BMS 16 | Partly | The top 10 cover 178 of 1366 trials (13%). The landscape is a long tail, and the chart hides that. Name variants are not merged ("Shanghai Pulmonary Hospital, Shanghai, China"). |
| 9 | What drugs is pembrolizumab most often combined with? | `network_graph`, "Drug co-occurrence network for pembrolizumab", 1.3 s, **554 KB** | pembro–carboplatin 334, –cisplatin 250, –paclitaxel 232, –pemetrexed 188, **–nivolumab 167**, –lenvatinib 146 | Partly | "Co-occurrence" means "listed in the same trial", not "given together". Pembrolizumab + nivolumab = 167 is surely mostly comparator or separate arms, not a combination. Nab-paclitaxel is split into two nodes ("130-nm albumin-bound paclitaxel" 82 and "Albumin-Bound Paclitaxel" 59). The graph shows 50 of 595 nodes. The response was 1.6 MB before the service trimmed citations. There is no ranked list, which is what I actually wanted. |
| 10 | Show the sponsor-drug network for pembrolizumab trials in NSCLC | `network_graph`, "Lead sponsor-drug network for pembrolizumab, NSCLC", 1.7 s, 327 KB | MSD–pembro 60, MSD–carboplatin 28, Lilly–pembro 15, NCI–pembro 11 | Partly | 50 of 577 nodes and 147 of 1259 edges are shown; "376 trials contribute no shown edge". The excluded-term list says "**Taxes** (21 trials) not in chemicals and drugs branch". Presumably that is taxane chemotherapy mis-mapped by MeSH and then thrown away. 619 trials here vs 601 in #2. |
| 11 | Keytruda trials by recruitment status | `bar_chart`, "Trials by overall status for Keytruda", 4.2 s | COMPLETED 765, RECRUITING 601, ACTIVE_NOT_RECRUITING 466, TERMINATED 367 | Yes | Brand name resolved correctly (same 2642 as pembrolizumab). Labels are raw enums (`ACTIVE_NOT_RECRUITING`). "UNKNOWN" here is a registry status, not missing data, but it looks just like the "Unknown" buckets elsewhere. |
| 12 | *(expected to fail)* Total planned enrollment across recruiting pembrolizumab NSCLC trials? | 422 `unsupported_question`, 0.8 s | — | No (expected) | It says enrollment "cannot be answered with available data". That's false: the data is there, the service just doesn't sum it. Meanwhile the suggestion list offers "enrollment count" as a dimension. It should have pointed me to #18. |
| 13 | *(expected to fail)* Which pembrolizumab combinations showed the best overall survival? | 422 `unsupported_question`, 0.8 s | — | No (expected) | Correct refusal. The generic suggestion list is the same for every error. |
| 14 | *(expected to fail)* How long do phase 3 pembrolizumab trials take from start to completion? | 422 `unsupported_question`, 0.9 s | — | No (expected) | Correct refusal, but the reason ("requires outcome or date calculations not supported") reads like the registry lacks the dates. It has them. |
| 15 | How has recruiting NSCLC trial activity in China vs the United States changed by start year? | `time_series`, "Trials by start year: NSCLC, in China, recruiting vs NSCLC, in United States, recruiting", 3.0 s | China 2025: 189, 2024: 143; US 2025: 146, 2024: 89 | **Misleading** | Filtering to *currently* recruiting trials and then plotting by start year always produces a rising curve, because older trials have stopped recruiting. Nothing warns about this. China = 620 here but 613 in #5, for the same cohort. |
| 16 | lung cancer trials by country | `bar_chart`, "Trials by location country for lung cancer (partial: first 10,000 of 14,627 matches)", **12.1 s**, 285 KB | US 3377, China 3039, France 1021, **Unknown 956** | Partly, clearly flagged | The partial result is flagged in the title. Good. But it is the 10,000 most recently registered, which skews the mix, and 106 bars with no top-N. |
| 17 | Where are recruiting non-small cell lung cancer trials located? Top 15 | `bar_chart`, same as #5 with full name, 3.4 s | China 568, US 515, Spain 167. **1297 trials** | Yes | Same question as #5 with the name spelled out. **1366 vs 1297 trials, China 613 vs 568.** Which one goes on the slide? |
| 18 | Distribution of enrollment sizes for recruiting pembrolizumab NSCLC trials | `histogram`, "Trials by enrollment count for pembrolizumab, NSCLC, recruiting", 1.3 s | 100–499: 63, 500–999: 36, 1–49: 25, 50–99: 12, 1000+: 11. 147 trials | Yes | It's useful, but the service never offered it when I asked #12. All 147 are "estimated enrollment" (fine for recruiting trials, but the warning makes it sound like a problem). |
| 19 | Industry vs academic sponsors of recruiting NSCLC trials | `bar_chart`, "Trials by lead sponsor class for NSCLC, recruiting", 1.2 s | OTHER 751, INDUSTRY 562, OTHER_GOV 22, NETWORK 15, NIH 13, FED 3 | Mostly | "OTHER" (the largest bar) means academic/hospital. Nothing says so. These are raw registry codes. |
| 20 | pembro combos in phase 3 lung cancer, who's running them | `bar_chart`, "Trials by lead sponsor for pembro, lung cancer, Phase 3", 1.0 s | **Empty** — "No trials matched these filters." | **No** | "pembro" was used as a literal drug name and matched nothing. I got an empty chart, not "did you mean pembrolizumab?". "Combos" was silently dropped again. |
| 21 | Merck pembrolizumab trials by phase | `bar_chart`, "Trials by phase for pembrolizumab, sponsored by Merck", 2.5 s | Ph2 503, Ph1 203, Ph1/2 178, Ph3 155. **1064 trials** | **Wrong for my purpose** | #7 says Merck (lead sponsor) has 267 pembrolizumab trials. This says 1064 "sponsored by Merck". The sponsor filter includes collaborator-only matches (Merck supplying drug to investigator studies), and any org containing "Merck". That is only in `meta.search_semantics.sponsor`. A 4× discrepancy between two charts in the same deck. |
| 22 | Phase distribution of pembrolizumab combination trials vs pembrolizumab monotherapy | 422 `unsupported_question` | — | No | It now says combination vs monotherapy "cannot be made". Fine, but then #1 and #20 should have refused too, not quietly answered a different question. |

## 2. Top 10 issues, ranked by damage to my briefing

1. **"Combination" is silently ignored (#1, #20) but refused elsewhere (#22).** Half my brief is
   about combinations. The service quietly answered "all pembrolizumab trials" and titled it in a
   way I could paste straight onto a slide. I expected one of two things. Either a refusal like #22,
   or a warning at the top: "'combination' is not a supported filter; these counts include
   monotherapy trials." Dropping part of my question without saying so is the worst thing a
   tool like this can do.

2. **"Sponsored by Merck" ≠ "lead sponsor Merck" (1064 vs 267).** The sponsor filter counts
   collaborator-only trials and any organisation containing the word. The lead-sponsor chart
   doesn't. The title "sponsored by Merck" hides the difference. I expected the default to match
   the lead-sponsor chart. Otherwise the title should say "Merck as lead sponsor **or
   collaborator**", with the lead-only count next to it.

3. **"Combined with" is answered by "appears in the same trial".** Pembrolizumab + nivolumab is
   the 5th strongest "combination" (167). In real life those are mostly head-to-head or
   separate-arm trials. For a combination landscape that is misleading. The title should say
   "Drugs listed in the same trial as pembrolizumab (not necessarily the same arm)". I expected
   at least a caveat in `warnings`, not just "co-occurrence" in the chart type. Duplicate drug
   nodes (two nab-paclitaxel nodes) and a dropped "Taxes" term (taxanes?) make the ranking itself
   unreliable.

4. **"Trials" includes observational and expanded-access studies.** 63 observational + 4 expanded-access
   studies are in the 2642 "pembrolizumab trials" (I checked: 2575 interventional). They show up as
   pre-2011 "trials" in the trend and as the "no phase / Unknown" bucket. The warning "67 trials
   have no phase" is really "67 are not interventional trials". I expected interventional-only by
   default for a "trials" question, or at least `count_unit` and the title to say "studies" with a
   study-type breakdown.

5. **The same question gives different numbers depending on wording.** "NSCLC" = 1366 recruiting
   trials, "non-small cell lung cancer" = 1297. Pembrolizumab + NSCLC = 619, + full name = 601.
   China is 613 as a country bar, but 620 as a country filter (the filter is a text search that
   also hits facility and city names). "pembro" = 0. I expected the condition and drug to be
   normalised to a canonical term (and echoed back as "interpreted 'NSCLC' as …"). When a
   filter matches nothing, I expected "did you mean pembrolizumab?", not an empty chart.

6. **Top-N charts hide most of the market without an "Other" bar.** The top 15 sponsors cover
   26% of pembrolizumab trials; the top 10 cover 13% of recruiting NSCLC trials. The warning
   ("1188 trials appear only in omitted values and are not shown") is accurate but buried and
   worded for engineers. I expected an "All other sponsors (639)" bar, and a subtitle like "top 10
   cover 178 of 1366 trials".

7. **The trend on currently-recruiting trials is structurally biased (#15), with no warning.**
   Plotting "recruiting" trials by start year always rises toward today. A leadership audience
   would read "China is accelerating". The data may or may not support that, and this chart
   can't show it. I expected a warning ("status filter reflects today's status; earlier years are
   under-represented"), or the planner to drop the status filter for a "how has it changed"
   question and say so.

8. **Titles and axes don't say what was counted.** Country bars add up to 2× the trial count, but
   the axis says "Trials". Comparisons show absolute counts when I asked for "mix". Cohorts overlap
   (206 trials in more than one of pembro/nivo/atezo), and that is mentioned only in
   `assumptions`. Invented constraints ("started 2015-2026") appear only in the title. I expected
   a subtitle line per chart: "N unique trials · a trial with sites in 3 countries counts 3 times ·
   206 trials are in more than one cohort". I also expected a share/percent option for
   comparisons.

9. **Error messages misstate the registry and give generic suggestions.** "Enrollment … cannot be
   answered with available data" is false; the service even has an enrollment histogram (#18).
   "Recruitment counts by US states" isn't what I asked. Every error has the same two
   suggestions: a list of 11 internal dimension names. I expected tailored, runnable suggestions:
   "I can't sum enrollment, but I can show the enrollment size distribution: [ask this]".

10. **Raw codes and engineering jargon in what I'm supposed to read.** I got `ACTIVE_NOT_RECRUITING`,
    `OTHER`, `OTHER_GOV` and `NETWORK` as labels, even though phases get nice labels. The warnings
    say things like "cap MAX_NETWORK_NODES=50", "NETWORK_REDUCED_EDGE_CITATION_CAP" and "start date
    with no ACTUAL/ESTIMATED type (legacy records)". There are two timestamps ("data_timestamp
    2026-10-16" and "served from the local cache stored at 2026-10-18 19:05 UTC") and I don't know
    which is the data date. I expected human labels and one "data as of" date.

Also noted, lower priority: networks are 0.3–0.6 MB and show ~9% of nodes; #16 took 12 s; each
bar carries at most 20 citations with no total, so "see all 613 trials" isn't possible.

## 3. What a frontend would need from the response

- **A plain-English "I understood your question as…" line**, including anything dropped
  ("ignored: 'combination'") or added ("assumed: start year ≤ 2026"). `meta.interpretation`
  has the plan, but it's an internal structure, not a sentence.
- **A headline number per cohort: unique trials**, separate from the bar sum, plus a ready-made
  subtitle that states the counting rule (multi-country, multi-cohort, lead vs any sponsor).
- **Structured warnings with severity and a code**, e.g. `{severity: "major", code:
  "top_n_hidden_share", user_text: "Top 10 cover 13% of trials"}`. Right now they are free prose,
  some critical and some trivia, all at one level, so a UI can't decide what to show as a banner.
- **An "Other" bucket** (or `other_count`) for top-N, and a `share` field per bar so the UI can
  switch between count and percent.
- **Human display labels for every enum** (status, sponsor class), with a short definition
  ("OTHER = academic / hospital / other non-industry").
- **Canonical entity resolution in the response**: "NSCLC" → which condition terms were searched;
  "Merck" → which organisations matched, lead vs collaborator counts; "pembro" → a "did you
  mean" suggestion.
- **For networks: a ranked edge table** ("top partners of pembrolizumab, trial count, link to
  trials") alongside the graph. Merged synonym nodes. Also a much smaller default payload
  (citations on demand rather than inline).
- **Full trial lists per bucket on demand** (citations stop at 20 with no total or "more"
  link), plus a CSV export of exactly what is charted.
- **Runnable suggestions on errors**: a ready-to-send request body, not a list of dimension names.
- **One "data as of" date** and a cache/freshness indicator a non-engineer can read.

## 4. Three things that worked better than I expected

1. **It refuses instead of making things up.** Survival, trial duration and enrollment totals got
   clean refusals in under a second, not a fabricated chart. An unmatched filter gave an empty
   result with a note, not invented bars. The lung-cancer request that hit the 10,000-record
   cap said "partial: first 10,000 of 14,627 matches" **in the title itself**. That's exactly
   where I need it.
2. **Time charts are honest about time.** "2026 is still in progress", "2027 buckets are
   projections", how many dates are estimated, and an explicit Unknown bucket. Most tools I've
   used would have drawn 2026 as a decline. Each cohort's retrieved count reconciles with the
   registry total, and overlapping cohorts are counted and disclosed.
3. **It's fast, it understands normal phrasing, and every bar is traceable.** Most answers came
   back in 1–4 s. "Keytruda" resolved to pembrolizumab. Multi-drug comparisons, country-vs-country
   and sponsor-class questions were parsed correctly from casual wording. Every bar links to real
   NCT records. Where I spot-checked (MSD as lead sponsor: 267 here, 266–274 from the registry),
   the numbers held up.
