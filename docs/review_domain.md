# Domain review: can these numbers be shown to a medical or commercial audience?

Reviewer stance: clinical development scientist (oncology), assessing counting semantics,
drug and condition identity, networks and framing. Inputs: `docs/decisions.md`,
`docs/api_findings.md` (all sections), `docs/architecture.md`, all nine responses in
`examples/`, and my own probes against the live ClinicalTrials.gov API v2 on 2026-10-18
(`countTotal=true` requests with the same clauses the query builder sends, plus a few full
fetches to read intervention and condition strings). Probe counts can drift by a few trials
from the examples because the registry refreshes daily.

**Verdict.** The engineering discipline (exact counts, reconciliation, citations, nothing
silent) is better than most commercial landscaping tools I have used. The problems are
about meaning, not arithmetic. The numbers are correct for the question the code asks, but
in several places that question is not the one a clinician or commercial reader will think
was answered. Three framings would mislead decisions in the hands of a non-specialist today:
drug co-occurrence presented as "combination", drug names that silently pull in other
molecules, and "trials" that are really registry records of any kind.

Fixability legend: **<1 h** (wording, labels, regenerate examples), **~1 day** (new
filter, dimension or relation using fields the API already serves), **new source** (needs
data the API does not have: a drug dictionary, eligibility parsing, other registries).

---

## Findings, ranked by how badly they could mislead a decision

### 1. Drug co-occurrence is not combination therapy, and the tool routes "combination" questions to it

**Evidence.**
- The relation's planner description (`aggregate/registry.py`, `DRUG_DRUG_COOCCURRENCE`)
  reads "Network of drugs tested together in the same trial. For 'what is X combined with'
  or 'drug combinations'." Example h was asked "Which drugs frequently co-occur with
  pembrolizumab in **combination studies**?", and nothing in its meta says that
  co-occurrence is not combination.
- Pembrolizumab–nivolumab is the fifth-heaviest edge (167 trials). Two anti-PD-1
  antibodies are essentially never given together. I read the first 60 of the 169 trials
  carrying both MeSH terms: **none co-administers them**. They are interchangeable
  "nivolumab or pembrolizumab per label" options (NCT04114136, NCT06488482, NCT04017897),
  comparator arms (NCT06561386: nivolumab + relatlimab + chemo vs pembrolizumab + chemo),
  observational real-world cohorts (NCT07718139, NCT07394062, NCT05611229: 17 of the 143
  name matches are observational), physician's-choice control arms (NCT06743126,
  NCT06264180), continued-access and platform protocols that list every backbone
  (NCT05203172 FLOTILLA, NCT04801966), and "PD-1 inhibitor" add-on designs (NCT02840994).
- Carboplatin–cisplatin (151 trials, one of the heaviest non-hub edges) is almost entirely
  "investigator's choice of cisplatin **or** carboplatin" (NCT06225596 "Gemcitabine +
  cisplatin Or carboplatin", NCT05613413, NCT07524257, NCT04675333). Nobody gives both
  platinums. Paclitaxel–pemetrexed (71) is mostly histology-dependent alternatives
  (squamous vs non-squamous backbone).
- Docetaxel (92) is in large part the **comparator** pembrolizumab beat:
  KEYNOTE-010 (NCT01905657), KEYNOTE-045 (NCT02256436, vs paclitaxel, docetaxel or
  vinflunine), NCT07660094 ("vs Docetaxel"), NCT03933449 (vs investigator's choice).
- Atezolizumab (64), durvalumab (52) and ipilimumab (75) edges carry the same mix of
  alternatives, comparators and "nivolumab + ipilimumab vs pembrolizumab" designs.

**Why it misleads.** A BD or competitive-intelligence reader will take the top edges as
"what pembrolizumab is being combined with". Three of the top ten partners (nivolumab,
cisplatin as a carboplatin partner, docetaxel) would be read as combination strategies
when they are, respectively, interchangeable class members, an either-or choice, and the
control arm.

**Correct framing.** "Drugs listed in the same trial record (any arm, including comparator
and alternative options; observational studies included)". A combination network should be
built at the arm level: two drugs count as combined only when they are both in one
`EXPERIMENTAL` arm (`armGroups[].interventionNames` + `armGroups[].type`, both served by
the API). Exclude observational studies and expanded access from this relation by default.

**Fixability.** Wording, an assumption line, and removing "drug combinations" from the
planner description: **<1 h**. Arm-level co-administration and an interventional-only
default: **~1 day** (the arm↔intervention link is by raw name, so it needs the same care
as the intervention↔MeSH problem in decision 15, but the API serves it).

### 2. A drug name silently includes other molecules, and the answer depends on which name the user typed

**Evidence** (live, `(AREA[InterventionName]"X" OR AREA[InterventionMeshTerm]"X")`):

| Query | Trials | What is in it |
|---|---|---|
| `trastuzumab` | 1,321 | All 165 trastuzumab deruxtecan and 136 trastuzumab emtansine trials. **232 trials (17.6%) carry no Trastuzumab (or trastuzumab biosimilar) MeSH term** but do carry an ADC or radiotracer term (T-DXd, T-DM1, trastuzumab duocarmazine, ⁶⁴Cu-/⁸⁹Zr-trastuzumab PET) |
| `Abraxane` / `Albumin-Bound Paclitaxel` | 4,602 / 4,602 | Identical to `paclitaxel` (4,602): the brand expands to the parent concept, so a nab-paclitaxel question returns every solvent-based paclitaxel trial |
| `nab-paclitaxel` | 1,357 | Same product as Abraxane, **3.4× fewer trials** |
| `trastuzumab deruxtecan` vs `T-DXd` | 165 vs 45 | 16 `T-DXd` trials are not in the generic-name result |
| `datopotamab deruxtecan` vs `Dato-DXd` / `DS-1062a` | 31 vs 26 / 3 | 16 and 3 trials missed by the generic name |
| `tislelizumab` vs `BGB-A317` | 674 vs 14 | 3 missed |
| `bevacizumab` vs `Avastin` | 2,840 vs 2,852 | 12 missed by the generic name; 40 bevacizumab-cohort trials name a biosimilar |
| `pembrolizumab` | 2,642 | Includes biosimilar BCD-201 (79 trials by name in §3) and coformulations (MK-7684A pembrolizumab/vibostolimab, NCT05007106; at least 12 trials) |

In networks, one molecule is split across nodes: Paclitaxel (232), 130-nm albumin-bound
paclitaxel (82) and Albumin-Bound Paclitaxel (59) are three nodes, while trastuzumab
biosimilar HLX02 is its own node and T-DXd sits next to Trastuzumab.

**Why it misleads.** In HER2-positive breast cancer, trastuzumab, T-DM1 and T-DXd are
different products with different owners, labels and competitive positions. A
"trastuzumab trials by phase" chart that is one-sixth ADC trials is wrong for any
commercial or regulatory use. A biosimilar sponsor counted as running "pembrolizumab
trials" inflates the originator's apparent footprint. And because concept expansion is
per drug and undocumented (§16 already found it expands MK-3475 but not BMS-936558), two
analysts asking about the same product by brand and by INN get different charts.

**Correct framing.** The unit should be a molecule (an active-ingredient identifier such
as UNII or ChEMBL id) with explicit inclusion rules: ADC conjugates, radiolabelled tracers,
biosimilars and fixed-dose coformulations are separate molecules unless the user asks for
the family. Until then, meta must say that a name match includes any intervention or MeSH
term **containing** the name, and should list the distinct intervention MeSH terms in the
cohort that contain the query string (trastuzumab deruxtecan, Ado-Trastuzumab Emtansine,
trastuzumab biosimilar HLX02...) with their trial counts.

**Fixability.** The "terms containing the query" warning: **~1 day** (the MeSH list is
already fetched). A molecule dictionary with code names, brands, biosimilars and
conjugates: **new source** (NCI Thesaurus, which ClinicalTrials.gov itself uses for
intervention mapping, or ChEMBL/UNII).

### 3. "Trials" are registry records of any kind; the "Unknown" phase bucket is almost entirely observational studies

**Evidence** (`query.cond=breast cancer`, 16,913):
- 13,187 interventional, **3,694 observational**, the rest expanded access.
- The phase chart's "Unknown" bucket is 3,728. Interventional studies without a phase: **2**.
  So 99% of "Unknown" is observational studies, for which phase does not exist. The
  warning says "3728 of 16913 trials have no phase", and the bucket label says Unknown.
- Primary purpose TREATMENT: 8,530. Supportive care, screening, prevention, diagnostic,
  health services, basic science and other: 4,532. The "breast cancer" cohort includes
  lymphedema physiotherapy (NCT07878598), insomnia (NCT07859098), physical activity
  (NCT07840599) and gait (NCT07864792) studies.
- 533 breast cancer and 122 pembrolizumab records are WITHDRAWN (never enrolled a patient)
  and counted like any other trial.
- `meta.count_unit` is `"trials"` in every response.

**Why it misleads.** "16,913 breast cancer trials, 22% of unknown phase" reads as a large
therapeutic field with a data-quality problem. A medical reader expects about 8,500
treatment studies and no unknown phase. A commercial reader sizing the field will
overstate it by 2×. "Unknown" suggests missing data, which invites someone to "clean"
it or to drop it, when it is a correct and complete value. Withdrawn records count toward
trends and sponsor activity although no patient was ever treated.

**Correct framing.** Count unit "registered studies (interventional and observational,
any status)". Phase bucket "Not applicable: observational / expanded access" derived from
`studyType` (deterministic, not a guess), keeping a true "Unknown" for interventional
studies without a phase. Offer `study_type`, `primary_purpose` and an
"exclude withdrawn" filter, and state the default in the title ("Interventional treatment
studies by phase" vs "All registered studies by phase").

**Fixability.** Relabel bucket and count unit, add an assumption: **<1 h** to word,
though changing a bucket rule should follow the test-first rule. Study-type,
primary-purpose and status-exclusion filters: **~1 day** (all are `filter.advanced`
fields; they also fit the count route).

### 4. Condition search uses the condition MeSH terms the project decided not to trust

**Evidence.** `query.cond` searches ConditionMeshTerm (.5) and ConditionAncestorTerm (.4)
(§2). `query.cond=Parkinson disease` returns 5,043 studies, of which **196 are
pembrolizumab trials** and 266 list a cancer condition: the same "Parkinson Disease 4"
mis-mapping from "PD-1" that decision 14 rejects for grouping. Decision 14 protects the
grouping but not the cohort.

**Why it misleads.** A neurology user asking "Parkinson's disease trials by phase" gets
about 5% oncology immunotherapy trials. The same will happen wherever automated text
mapping fires on abbreviations (PD for Parkinson/progressive disease/PD-1, CAR, ALL,
AML in non-oncology text).

**Correct framing.** State in `meta.search_semantics` that condition search includes
derived MeSH terms and their ancestors. Better, offer a stricter condition mode
(`AREA[Condition]` plus titles, no MeSH) and show the difference, as the drug match mode
already does.

**Fixability.** Semantics wording: **<1 h**. A strict condition mode with a measured
comparison like §16: **~1 day**.

### 5. The sponsor network shows lead sponsors only, which hides who actually runs the programme

**Evidence.**
- Merck Sharp & Dohme LLC is lead sponsor on 267 of 2,642 pembrolizumab trials (10%) but a
  **collaborator on 791 more** (30%). MSD supplies drug and funding to most investigator
  trials. The network says MSD touches 10% of the programme; with both roles it is about
  40% (267 + 791, assuming no record lists MSD in both roles).
- NCI is lead on 73 and collaborator on 184. NCTN cooperative groups (SWOG, ECOG-ACRIN,
  Alliance, NRG, COG, CCTG, EORTC) lead only 24, so the most practice-changing academic
  trials are scattered under group names or under NCI depending on registration habit.
- The readability cap leaves **1,697 of 2,642 trials (64%) without a shown edge** in
  example g (stated in a warning, but the picture is dominated by MSD and a dozen US
  cancer centres). The long tail of single-trial investigator sponsors, which is most of
  the pembrolizumab programme, is invisible.
- The assumption "Sponsors are lead sponsors only; collaborators are not included" is in
  meta, among eight assumptions.

**Why it misleads.** "Who sponsors pembrolizumab research" from this chart says academic
centres dominate and Merck is one large node; the commercial reality is that Merck is
behind four in ten trials. For any partnering question (which pharma companies combine
their asset with pembrolizumab: Eli Lilly 21, GSK 19, Astellas 19 lead, with MSD as
collaborator), lead-only is the right edge but must be labelled as such.

**Correct framing.** Two edge types, lead and collaborator, with the lead/collaborator
split in the node tooltip; or a sponsor dimension "any role". Title: "Lead sponsor–drug
network".

**Fixability.** The title already says "Lead sponsor"; a lead/collaborator count in the
assumption: **<1 h**. Collaborator edges: **~1 day** (`collaborators[]` is in the
projection already). Rolling up sponsor names (subsidiaries and renamed companies, e.g.
Genentech/Roche) needs a curated sponsor hierarchy: **new source**.

### 6. Condition matching is term search, not disease definition

**Evidence.**
- Spelling changes the cohort: `NSCLC` 8,800 vs `non-small cell lung cancer` 8,601;
  `breast cancer` 16,913 vs `breast carcinoma` 16,763.
- Of 1,297 recruiting "NSCLC" studies, only **710 (55%) list lung conditions only**; 364
  list four or more conditions and 187 mention solid tumours or advanced cancer (basket
  and pan-tumour trials). 12 do not mention NSCLC at all: small cell lung cancer
  (NCT07010263, NCT05623267), small-cell cervical and esophageal carcinoma, prostate.
- `breast cancer` includes 322 of 326 DCIS studies and 1,265 male breast cancer studies.
  That is defensible, but DCIS is stage 0 and usually analysed separately.
- `metastatic breast cancer` (4,619) is a word match, not a stage filter. Its newest
  results include NCT07793656 ("Secondary Lymphedema", matched through
  secondary ≈ metastatic synonymy) and many non-metastatic supportive-care studies.

**Why it misleads.** A feasibility lead counting "recruiting NSCLC trials in Spain" counts
basket trials where NSCLC is one cohort among ten as equal to a dedicated NSCLC phase 3.
A user who adds "metastatic" thinks they have restricted to stage IV.

**Correct framing.** The current `search_semantics` sentence is accurate but technical.
Add plainly: "Includes any study that lists this condition among others (basket and
pan-tumour trials), and words are matched with synonyms: qualifiers such as 'metastatic',
'early', 'HER2-positive' or 'first-line' are not stage, biomarker or line filters."
Report how many matched studies list other conditions.

**Fixability.** Wording and a "lists other conditions" count: **<1 h** / **~1 day**.
Histology, stage, biomarker and line of therapy need eligibility-criteria parsing or a
curated indication layer: **new source**.

### 7. Country counts are trials per country, regardless of sites or site status

**Evidence.** Example c ranks China (568) over the United States (515). A single-site
Chinese investigator trial and a 300-site global phase 3 with one US site count the same.
"Recruiting" is the overall status: trials with a recruiting site in the country are
US 507 (not 515), China 556 (568), Spain 156 (167), via
`SEARCH[Location](AREA[LocationCountry]X AND AREA[LocationStatus]RECRUITING)`.

**Why it misleads.** For site feasibility and for "where is the competition enrolling",
the expected unit is sites (or recruiting sites), and the US vs China ranking can flip
depending on the unit. The site-status gap is small (2–7%) but systematic.

**Correct framing.** Title "Trials with at least one site in the country", and offer a
"recruiting sites" variant and a "sites per country" metric (sites are already fetched).

**Fixability.** Wording: **<1 h**. Site-status-aware country counting and a site count
metric: **~1 day**.

### 8. Start-year trends: withdrawn, never-started and late-registered trials

**Evidence** (examples b and d, pembrolizumab from 2015): the 2023–2025 buckets include
76 trials whose start date is still ESTIMATED although it is in the past, and 75 that are
withdrawn or not yet recruiting. 62 withdrawn pembrolizumab trials have an estimated start
date (they never started). The curve flattens from 2022 (255) to 2025 (201).

**Why it misleads.** A flat or falling line reads as "investigator interest in
pembrolizumab peaked in 2022". Part of the decline is registration lag: many investigator
trials, especially outside the US, are registered months after first enrolment, so the
last one or two complete years fill in later. Conversely, past ESTIMATED dates on
withdrawn records are plans that never happened.

**What the tool already does well.** `is_future`, the in-progress flag for the current
year, per-bucket `estimated_count` and the legacy no-type count are exactly what a
careful analyst would want; keep them.

**Correct framing.** Add a warning on the last two complete years ("may still grow:
trials are often registered after they start"), and exclude or separately mark withdrawn
records in temporal views. Consider a "first posted year" option, which has no lag and no
estimates, for "how much new activity was registered".

**Fixability.** Warning: **<1 h**. Withdrawn exclusion and a first-posted dimension:
**~1 day**. Quantifying the lag needs historical snapshots: **new source** (AACT archives).

### 9. The shipped examples disagree with each other on the same drug

**Evidence.** Examples a, b and d were generated with name-only matching (2,575 and 2,518
trials; `search_semantics` "Exact intervention name"), while e, g and h use the current
name-or-MeSH default (2,642). Pembrolizumab Phase 2 is 1,148 in a and 1,163 in e; Phase 3
311 vs 314. The pembrolizumab node in g and h has size 2,570 although the cohort is 2,642,
because 72 trials name the drug in a combination string ("Pembrolizumab+Lenvatinib") with
no MeSH term.

**Why it misleads.** The first thing a reviewer does is compare two charts about the same
drug. Different numbers with no explanation cost more trust than any of the findings above.

**Fixability.** Regenerate a, b, d: **<1 h**. Add a line in network meta when the anchor
node is smaller than the cohort ("72 trials name pembrolizumab but have no pembrolizumab
MeSH term; they are in the cohort but not in the node"): **<1 h**.

### 10. Phase semantics: correct, but missing the two views clinicians use

**What is right.** Trial as the unit, a multi-phase trial in its own exclusive
"Phase 1/Phase 2" bucket, NA kept apart from missing, and Unrecognized apart from Unknown
are all defensible and match how FDA and ClinicalTrials.gov themselves report. A
regulatory colleague would accept them.

**What they would object to.**
- Phase is a property of the trial, not of the drug in it. A phase 3 where pembrolizumab
  is the comparator or the backbone for someone else's drug counts as a "pembrolizumab
  phase 3". For the drug owner that is right; for "how advanced is drug X" it is not.
- "Not Applicable" in oncology is mostly radiotherapy, surgery, device and behavioural
  studies (1,650 breast cancer NA studies have primary purpose TREATMENT). It is not
  "non-therapeutic".
- Early Phase 1 is FDA's exploratory-IND "phase 0"; non-specialists read it as phase 1.
- Commercial users will ask for the **highest phase per drug per indication**
  (development stage), which this tool cannot produce.

**Fixability.** Bucket descriptions in meta: **<1 h**. Highest phase per drug: **~1 day**
for a per-trial rollup, but it is only meaningful with a molecule dictionary and an
indication layer (**new source**).

### 11. Intervention MeSH in retrieval: the cohort, not only the nodes, carries mis-mappings

**Evidence.** The OR branch on `InterventionMeshTerm` (decision 84) adds class
placeholders ("PD(L)-1 inhibitor", NCT07419932; "Immune checkpoint inhibitor",
NCT05846659; "(chemo)immunotherapy", NCT04465942) to the pembrolizumab and nivolumab
cohorts, and trials whose MeSH is mapped from free text: NCT02423863 (sole intervention
Hiltonol) and NCT05595447 (sole intervention brentuximab vedotin) carry both PD-1 MeSH
terms. This is stated in `search_semantics`, which is good.

**Assessment.** The trade-off (+67 pembrolizumab, +85 nivolumab, including 28
nivolumab trials registered only as ONO-4538/BMS-936558) is the right one for a "trials
involving X" question; missing an entire code-name programme is worse than a few
placeholders. But a "PD-1 inhibitor of investigator's choice" trial is not a
pembrolizumab trial for a commercial reader. Report the count of cohort trials that
match only through MeSH, so the reader can see the size of the soft margin.

**Fixability.** That count is one extra `countTotal` request: **<1 h**.

---

## Point-by-point answers to the eight questions

**1. Counting semantics.** Trial as the unit, the exclusive multi-phase bucket and
NA/Unknown/Unrecognized are sound and well documented (finding 10). The objection is to
what counts as a "trial": observational, expanded access, non-treatment and withdrawn
records are all in (finding 3), and "Unknown" phase is really "observational". Countries
counted per trial with overlap is correct and clearly flagged, but feasibility users expect
sites and site status (finding 7). Estimated vs actual dates, `is_future` and the in-progress
year are handled better than in most tools; add registration-lag and withdrawn caveats
(finding 8).

**2. Drug identity.** Name-or-MeSH is the right default, but name matching is
"contains", so conjugates, tracers, biosimilars and coformulations are included, and brand
expansion is inconsistent (Abraxane = all paclitaxel; T-DXd, Dato-DXd and BGB-A317 not
expanded) (finding 2). Names that changed (MK-3475 → lambrolizumab → pembrolizumab) are
handled when NCI's concept mapping knows them, and not otherwise; this cannot be checked
without a dictionary.

**3. Condition matching.** Over-includes basket trials, small-cell and other histologies
at the margins, supportive care, DCIS and male breast cancer, and anything whose derived
condition MeSH mis-maps (Parkinson); qualifiers do not restrict stage or biomarker. The
semantics sentence is accurate but too technical for the intended reader (findings 4, 6).

**4. Networks.** Lead-only sponsors understate MSD's role by about 4× (finding 5).
Co-occurrence is not combination (finding 1). The MeSH-tree filter is a real improvement
and the excluded terms I checked are all non-drugs; keeping `drug_class` nodes visible
but typed is right. Excluding radiotherapy is correct for a drug network, but in
pembrolizumab specifically, radiotherapy (79 trials) and radiosurgery (42) are among the
most important partners, so a "therapies" network is worth adding (decision 99 already
anticipates it). Saline and placebo exclusion is correct.
Top-10 I would challenge: **nivolumab** (alternative, not partner), **docetaxel**
(comparator), **cisplatin** as a carboplatin partner (either-or), **paclitaxel vs
albumin-bound paclitaxel** split across nodes, **cyclophosphamide** (mixes AC in
KEYNOTE-522-style neoadjuvant breast regimens with lymphodepletion for cell therapy, two
unrelated uses). Carboplatin, pemetrexed, paclitaxel and lenvatinib edges are credible
combinations. In g: **Merck at 267** (should show ~1,058 with collaborator role), and the
academic centres' prominence is a lead-only artefact.

**5. Taxes / Parkinson.** The reasoning is right: both terms are automated text
mappings, so MeSH is a grouping key, not ground truth. Trusting intervention MeSH more
than condition MeSH is reasonable because drug MeSH is mapped from a short intervention
name, while condition MeSH is mapped from longer free text. But the project still uses
condition MeSH indirectly through `query.cond`, and that leaks (finding 4).

**6. count_per_bucket and the truncation table.** The arithmetic checks out (both columns
sum to 10,000 and 16,913; the 38.7% vs 23.0% phase 2+3 shares follow from the table).
The breast cancer shares are plausible: Phase 2 21%, Phase 3 8%, NA 29% (radiotherapy,
surgery, behavioural), no phase 22% (observational). The direction of the bias is also
plausible: recent registrations are richer in academic observational and NA studies. Two
things to note rather than suspicious numbers: (a) §12 shows the API's default order gave
Phase 2 = 2,094 of 10,000, much closer to the true 21.1% than the sorted subset (1,699),
so the sorted subset was chosen for explainability, not accuracy, which is fine but worth
saying; (b) country, sponsor and condition dimensions still truncate, and for breast
cancer the "10,000 most recently registered" subset will over-represent China and
academic sponsors far more than it skews phase. A "breast cancer by country" chart is
the one I would not show.

**7. Titles, assumptions, warnings.** "Trials by phase for breast cancer" (should say
"registered studies", finding 3); "N trials have no phase; they are counted in the
'Unknown' bucket" (they are observational); "Drug co-occurrence network" answered to a
"combination" question with no caveat (finding 1); "Trials by location country ...
recruiting" (overall status, not site status, finding 7). The `top_n` warning ("79 trials
appear only in omitted values") and the truncation suffix are exemplary.

**8. Questions users will ask that the tool cannot answer or answers wrongly**, most
common first:
1. "Who is developing drugs against target/mechanism X in indication Y?" (HER2 ADCs,
   KRAS G12C, claudin 18.2): no target or mechanism layer. **New source.**
2. "What is the most advanced phase for each competitor in this indication?" Not
   supported; per-trial phase only. **~1 day** for rollup, meaningful only with a
   molecule dictionary.
3. "When do the phase 3s read out?" Primary completion date is not a dimension; it is
   in the API. **~1 day.**
4. "First-line, PD-L1-high, HER2-low, stage III..." No line, biomarker or stage; the
   words are matched as text and silently fail to restrict (finding 6). **New source.**
5. "What is X combined with?" Answered wrongly today (finding 1). **~1 day.**
6. "How many patients / sites?" Enrollment is binned, not summed; sites are not a metric
   (finding 7). **~1 day.**
7. "Which trials use X as comparator / head-to-head vs Y?" Arm roles not used. **~1 day.**
8. "Which trials stopped early and why?" `whyStopped`, `hasResults` not exposed. **~1 day.**
9. "Include China/EU/Japan registries." ChiCTR, CTIS, jRCT trials are partly absent from
   ClinicalTrials.gov, which matters most for Chinese domestic oncology. **New source.**

---

## Summary table

| # | Finding | Mislead risk | Fix |
|---|---|---|---|
| 1 | Co-occurrence presented as combination | High | <1 h framing; ~1 day arm-level |
| 2 | Drug names include ADCs, tracers, biosimilars; brand expansion inconsistent | High | ~1 day warning; new source for molecules |
| 3 | "Trials" = any registry record; Unknown phase = observational | High | <1 h labels; ~1 day filters |
| 4 | Condition search leaks condition-MeSH mis-maps (Parkinson ← PD-1) | Medium–high | <1 h wording; ~1 day strict mode |
| 5 | Lead-sponsor-only network hides MSD's ~40% role | Medium–high | <1 h assumption; ~1 day collaborator edges |
| 6 | Condition = term search, basket trials, qualifiers not filters | Medium | <1 h wording; new source for stage/biomarker |
| 7 | Countries counted per trial, overall not site status | Medium | <1 h title; ~1 day site metrics |
| 8 | Trend includes withdrawn, never-started, lagged registrations | Medium | <1 h warning; ~1 day filters |
| 9 | Examples a/b/d stale vs e/g/h | Medium (trust) | <1 h |
| 10 | Phase per trial, not per drug; NA not non-therapeutic | Low–medium | <1 h wording |
| 11 | MeSH branch adds placeholders to cohorts | Low | <1 h count |

---

## Five sentences for the README's limitations

1. Counts are of ClinicalTrials.gov registry records, including observational studies,
   non-treatment studies and withdrawn records that never enrolled a patient, unless a
   filter excludes them; they are not a count of therapeutic trials, patients or sites.
2. A drug is matched by name and by NLM's automated MeSH mapping, so a name also matches
   conjugates, radiolabelled forms, biosimilars and coformulations that contain it (for
   example, "trastuzumab" includes trastuzumab deruxtecan), and brand names, code names
   and generic names can return different results.
3. Two drugs linked in a network were listed in the same trial record, which includes
   comparator arms, either-or options and observational cohorts; a link is not evidence
   that the drugs were given together.
4. Conditions are matched as words with synonyms, including derived MeSH terms that are
   sometimes wrong, so results include basket trials and occasional unrelated studies, and
   words such as "metastatic", "first-line" or "HER2-positive" do not filter by stage,
   line of therapy or biomarker.
5. Sponsor networks show lead sponsors only, and the registry does not cover trials
   registered only in other national registries, so neither the sponsor picture nor any
   country ranking should be read as a complete view of a development programme.
