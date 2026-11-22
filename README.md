# Resonance_AC: claim denial checker (Exchange4Health case study)

Predicts whether each unsubmitted home-care claim will be paid or denied, which denial code the
payer will report, and how to fix it. The design goal is that every prediction is **explainable**
(it names the rule and the evidence), **auditable** (every input change and ledger movement is
logged), and **repeatable** (same input gives byte-identical output; no model calls at decision time).

## Status

| Component | State |
|---|---|
| Data pipeline (`claimcheck/clean.py`) | built and tested |
| Ledger engine (`claimcheck/ledger.py`) | built, validated against history |
| Rule set (`rules/*.json`, `claimcheck/ruleset.py`) | hand-written baseline, versioned |
| Evidence checker (`claimcheck/evidence.py`) | built |
| Claim checker (`claimcheck/checker.py`) | built, backtested |
| Precedence learner (`claimcheck/precedence.py`) | built (code order only) |
| Fixer (`claimcheck/fixer.py`) | built |
| Manual-review flags (`claimcheck/review.py`) | built |
| Rule extractor (`claimcheck/extract.py`) | built, run against Claude, evaluated |
| Remark reader (`claimcheck/hypotheses.py`) | built, run against Claude |
| Bulletin step (`claimcheck/bulletins.py`) | built, run against Claude |
| Message drafts (`claimcheck/messages.py`) | built, run against Claude |
| Payer onboarding (`claimcheck/onboard.py`) | built, tested with a stand-in client; live script in `demo/README.md` |
| Automator (`claimcheck/automator.py`) | built: drafts claims for verified visits on no claim |
| Overdraw check (`review.overdrawn_authorizations`) | built: flags predictions that later payer decisions contradict |
| Web app (`claimcheck/app/`) | built: 8 screens over the live pipeline |

## The app

```powershell
python -m claimcheck.app
```

Then open http://localhost:8765. It loads in a few seconds and binds to this computer only.

| Screen | What it shows and does |
|---|---|
| Claims | All 546 claims awaiting review (filter: will be denied, needs a decision, flagged, will be paid), plus the 3,830 past claims split into denied (payer code and remark, what the rules find, the fix) and paid. Search, payer filter, paging. |
| Claim detail | Any claim: every rule checked with its source, the EVV visits, the authorization ledger and the claims that used it, the fix plan, review flags, the drafted message (or a button to draft one), and the audit trail. Past claims also show what the payer decided. |
| EVV visits | All 5,076 visits with their claims (clickable), what to do next, and the automator: 891 claims drafted from unbilled visits, sorted by filing deadline, each checked like any claim. |
| Authorizations | Units left, end dates and the full ledger per authorization; an errors panel you can minimise for overdrawn authorizations (AUTH11084). |
| Payer rules | Each payer's rules with their quotes, code order, rules not applied and open questions; "Add payer". |
| Add payer | Register a payer and upload its document, Claude drafts the rules, score against an answer key, approve. Approving rebuilds everything on the new rule set. |
| Notices | Bulletins and notices; upload one, Claude drafts only the change, approve it. |
| Rules & AI review | Claude's proposed rule from payer remarks with the history test, open questions, extraction and bulletin results, reviewer corrections. |

Actions that call Claude need `ANTHROPIC_API_KEY` set before starting the app; anything already
cached (all three payers, both bulletins, the hypotheses, 116 messages) works without it.

**Automator, run as of 2026-11-22:** of 891 drafts, 425 would be paid ($16,769), 443 are past the
payer's filing limit ($17,983 no longer billable), 22 would fail on authorization balance and 1 on the
daily cap.

## How to run

```powershell
python -m pip install -r requirements.txt
python -m claimcheck.clean        # Data/*.csv -> output/clean/
python -m claimcheck.replay       # history through the ledger -> output/ledger/
python -m claimcheck.precedence   # code order evidence -> rules/precedence_evidence.json
python -m claimcheck.predict      # predictions + backtest -> output/
python -m pytest -q
```

The rule extractor needs an Anthropic API key (it is never needed for predictions):

```powershell
$env:ANTHROPIC_API_KEY = "<your key>"
python -m claimcheck.extract evaluate              # draft all 3 payers, compare, backtest
python -m claimcheck.extract run PY03              # draft one payer -> rules/proposed/
python -m claimcheck.extract approve PY03 --by "Name" --note "why the draft was changed"
python -m claimcheck.hypotheses propose            # Claude reads remarks on missed denials; history tests
python -m claimcheck.bulletins evaluate            # replay the two real bulletins as changes
python -m claimcheck.messages draft --all          # notes for coordinators, payers, billing lead
```

## Results (rule set `d03a749180dc`)

Backtest on 3,830 history claims, each judged on the ledger state the payer saw at the time:

| | |
|---|---|
| Paid vs denied correct | 98.4% |
| Exact code correct | 98.1% |
| Exact code correct, excluding D99s that give no reason | 99.7% |

All 74 misses fall into named categories:
- **61 opaque D99s.** Remark "Denied - contact payer" or "Under review"; these claims break no rule, so nothing can predict them (about 1.6% of claims).
- **12 D99s whose remark is the Summit whole-hours rule.** The engine predicts D10, which matches the payer's own remark.
- **1 miss inherited from a claim-to-check.** C012372 was paid, but a claim-to-check on the same authorization was predicted paid. Had it really been paid, C012372 couldn't have been. It was most likely one of the opaque D99s.

**Time split.** The code order and the Summit cut-off were learned from all of history. Learning them only from claims submitted before 1 September gives the same answers: no later claim contradicts the early pairs, and the early data alone puts the Summit cut-off at 1 July.

`output/predictions.csv` has the 546 predictions. `output/decisions/to_check.jsonl` has, for each claim, every rule result (passed and failed), the evidence (visit IDs, minutes, authorization checks, ledger balances and which claims used them) and the rule-set version.

## Fixer

For each predicted denial, the fixer applies the edit for each failed rule. It then re-runs the
whole checker on the edited claim against the same ledger state, and repeats until the claim passes
or nothing more can change. Re-checking matters: cutting units for D09 can expose Summit's
whole-hours rule (D10), and attaching the right authorization brings in that authorization's balance (D02).

| Edit type | Example | Who decides |
|---|---|---|
| correction | modifier U2 → U3; attach the authorization on file; resubmit a duplicate of a *denied* claim as a corrected claim (frequency 7) | safe to apply |
| required reduction | cut units to verified time (D09) or whole hours (D10) | safe to apply: billing what the evidence supports |
| business choice | cut units to the authorization balance (D02) or daily cap (D06) | a person: the alternative is asking the payer for more units |

Some denials can't be fixed by editing the claim: late filing (D03), no verified visit (D01; a
visit at `manual_edit_pending` means "hold until verified"), no authorization on file, or a
duplicate of a paid claim (D05).

A second run applies every fix and replays the stream. A fix can change *other* claims: once a fixed
claim gets paid, it draws units another claim was counting on. These ripples are flagged.

**Claims-to-check (546):** 377 predicted paid as submitted, 48 fixable, 32 need a decision, 89 not
fixable. $36,278 of $54,065 is predicted paid as submitted. Safe fixes add $4,050, and fixes
needing a decision add $3,193.

## Manual-review flags

Named, deterministic reasons a prediction rests on an assumption, each marked `status_may_change`
or `code_may_change`. 71 claims are flagged:

| Flag | Claims | Meaning |
|---|---|---|
| DEPENDS_ON_PREDICTED | 24 | a D02/D06 caused by units drawn by other claims-to-check, whose outcomes are predictions |
| LEARNED_RULE | 17 | reported code comes from a rule learned from history (Summit D10; Summit sometimes reports it as D99) |
| CORRECTED_CLAIM | 14 | frequency 7: no corrected claims in history to confirm payer handling |
| PENDING_EVV | 4 | a visit that day is awaiting aggregator approval |
| UNOBSERVED_PRECEDENCE | 2 | several codes fail and history never showed their order, even through a chain |
| FIX_RIPPLE | 2 | fixing earlier claims changes this claim's outcome |
| ORDER_ASSUMPTION | 0 | a same-day claim shares state and swapping the order would change the outcome (checked; none does) |

`output/review/claims_to_check.csv` has one row per claim: prediction, reason, fix, amount after
fix, and flags. `output/decisions/to_check.jsonl` carries the full fix plan (each edit, its type
and reason, how many re-check rounds it took) and the flags.

## Rule extractor: where the AI is

Claude (`claude-opus-5-5`) is used in one place: drafting a payer's rule file from its manual and
bulletins. It is never in the path of a claim decision. Adding a payer means drafting their file,
reviewing it and approving it; the engine then runs it deterministically.

1. **Draft** (`extract run`). The prompt lists the engine's rule types and the denial codes, and
   includes one worked example: a *different* payer's documents and its approved rule file. The
   response must match the rule template's JSON schema (structured output).
2. **Check.** The draft passes the same validation as a hand-written file. Every rule must quote its
   document word for word, and each quote is checked against the source text. Requirements the
   documents mention but don't state (Summit 9.5, "additional billing requirements may apply") go to
   `open_questions`, never into a rule.
3. **Review and approve** (`extract approve`). A person edits the proposal in `rules/proposed/` if
   needed, then approves it. Approval keeps the rules learned from history (Summit whole hours) and the
   learned code order, records who approved it and which extraction it came from, and writes every
   difference between the model's draft and the approved file to `rules/extraction_corrections.jsonl`.
4. **Learn from corrections.** Logged corrections are included as lessons in later extraction prompts.
   This is in-context learning from reviewed examples, not fine-tuning.

**Repeatability.** Each response is cached under the SHA-256 of the exact request (model, prompt,
schema). Re-running uses the cache, so the same documents always give the same draft, and the cache
lets anyone reproduce a draft without an API key. `--refresh` calls the model again.

**Evaluation** (`extract evaluate`). Each payer is drafted leave-one-out, matched rule by rule against
the hand-written approved file (by type, code, effective dates and parameters, not IDs), then the
claims history is backtested on the drafted rules and compared with the approved rules.

| Payer | Rules matching the approved file | Quotes verified | Open questions raised |
|---|---|---|---|
| Northwind | 8 / 8 | all | 0 |
| Harbor | 10 / 10 | all | 2 |
| Summit | 7 / 7 | all | 1 |

The backtest on the drafted rules is identical to the approved rules (98.07% exact, 99.66% excluding
opaque D99), and no claim-to-check prediction changes. The three drafts cost about 9.6k input and 7.3k
output tokens (roughly $0.20). The open questions are worth reading in `rules/proposed/PY0x.meta.json`:
- **Summit 9.5:** the contract terms aren't published, so a reviewer must get the contract. History
  fills this gap with the learned whole-hours rule.
- **Harbor filing:** the exemption covers *in-state* providers only. Are any agencies out of state?
- **Harbor bulletin:** it says "from dates of service on 1 September", not "on or after". The draft
  assumes "on or after", and asks a reviewer to confirm.

Caveat: the approved files were also written with AI help (Claude Code), so 25/25 shows that two
readings of the documents agree, not that they are right. The independent check is the backtest
against what payers actually did.

## Other places Claude helps (never in a claim decision)

All Claude calls share `claimcheck/llm.py`: structured output against a fixed schema, and a cache
keyed by the SHA-256 of the exact request (`llm_cache/`, `rules/proposed/cache/`), so every result
replays identically without an API key.

**Remark reader: rules nobody wrote down** (`claimcheck/hypotheses.py`, Claude Opus 5.5). It starts from
the documents-only rules (approved files with learned rules removed), backtests history, and groups
the claims it gets wrong by payer, outcome and remark. Claude reads those groups and proposes
hypotheses, each written as an engine rule type. **Code then decides**: for each hypothesis it counts
the claims the rule would fail, how many the payer denied with that code (support) and how many it
paid (contradictions). It searches for a start date on both date bases, then confirms with a full
backtest (claims fixed, claims broken). Tested by hand on the Summit idea: start 1 July 2026 by date
of service, 35 claims support it, 0 contradict it, backtest 97.15% → 98.07%, 0 broken. A submission-date
start explains fewer claims (26), which is how history shows the rule follows the date of care. A wrong
idea (Summit 20-day filing) is rejected: 169 paid claims contradict it. **Run against Claude:** from
the documents-only baseline, Claude proposed one hypothesis, Summit whole hours (D10), noting that both
"amendment 3" remarks describe the same rule. History confirmed it (start 1 July 2026, 35 support, 0
contradictions, 35 claims fixed, 0 broken). Claude marked all six "contact payer / under review" groups
as not explainable, and named C012372's authorization balance as the likely cause of the one remaining
miss, which matches the inherited miss described above. Supported hypotheses go to
`rules/hypotheses/proposals.json`; `approve H1 --by <name>` adds one to the payer's file and refuses
a rule that is already there.

**Bulletins: draft only the change** (`claimcheck/bulletins.py`, Claude Opus 5.5). Given the current
approved file and a new bulletin, Claude lists the rules the bulletin ends (with their last date) and
the rules it adds, quoting the bulletin. Code applies the change, validates it and checks the quotes;
rules the bulletin doesn't touch can't drift. The evaluation rebuilds each payer's file as it stood
before its real bulletin, has Claude draft the change (with the other bulletin as the worked example),
and compares the result with today's file. **Run against Claude:** Northwind 8/8 and Harbor 10/10 rules
match, end dates correct, every untouched rule left as it was, all quotes verified. Claude raised two
questions on the Harbor bulletin: does "on 1 September" mean "on or after", and is a weekend claim
carrying both U2 and U3 denied?

**Message drafts** (`claimcheck/messages.py`, Claude Haiku 5.5, one short call per claim as the brief
suggests). Code decides who gets a message and what it asks, from the fix plan:

| Recipient | When | Claims |
|---|---|---|
| care coordinator | no verified visit (pending, rejected or missing) | 28 |
| payer care manager | authorization used up, too small or not on file | 15 |
| billing lead | decisions and write-offs: fewer units, late filing, duplicates | 73 |

Claude only writes the wording from the facts given. Every claim, visit, authorization and member ID,
and every date, in a draft must appear in those facts; anything else marks the draft
`check_references`. Drafts are written to `output/messages/`; nothing is sent. **Run against Claude:**
116 drafts, none flagged.

## Rule set

One JSON file per payer in `rules/`. Each rule names a rule type from `claimcheck/rule_kinds.py`, its
parameters, the denial code, optional effective dates (by date of service), and its source: a
document section with a quote, or `learned_from_history` with the evidence. Rules a payer
does *not* apply are recorded too (`not_applied`), for example Harbor's lack of a filing limit. The
rule set is schema-checked on load, and its SHA-256 fingerprint is stamped on every prediction.

Two facts come from history, not documents:
- **Summit whole hours (D10)** from 1 July 2026 by date of service (contract amendment 3, named
  only in payer remarks).
- **The order of codes each payer reports** when several rules fail. No pair is ever seen both ways:

| Payer | Order |
|---|---|
| Northwind | D01 > D09 > D07 > D08 > D02 > D03 > D05 |
| Harbor | D01 > D09 > D07 > D08 > D04 > D05 > D06 > D02 |
| Summit | D01 > D07 > D08 > D10 > D09 > D02 > D03 > D05 |

## Data pipeline

Cleans the four CSVs. It only changes how a value is written, never what it means:

- Payer names → payer ID (`NORTHWIND HP` → `PY01`). An unseen spelling is an error, not a guess.
- Service codes and modifiers trimmed and upper-cased. Blank modifiers and blank authorization
  numbers stay blank: they are evidence for D04 and D07.
- Dates in `MM/DD/YYYY` → ISO. Checked, not assumed: no slash date in any file has a first number above 12.
- 32 history rows that repeat an earlier row in every column are dropped (export artefacts; keeping
  them would double-count authorization units and look like D05 duplicates).
- EVV visits get `service_date` = the clock-in date (a visit belongs to the day it started; 191
  visits cross midnight) and `duration_minutes`.

Outputs: clean tables, `cleaning_log.csv` (one row per changed value: raw, clean, rule, source
row), and `data_quality.json` (input file SHA-256 hashes, counts per rule, warnings).

## Ledger engine

Three denial codes depend on other claims, not just the one being checked:

| Code | Depends on |
|---|---|
| D02 authorization exhausted | units drawn by earlier **paid** claims on the same authorization |
| D06 daily maximum (Harbor) | units already **paid** for the member, payer and date, all services |
| D05 duplicate | every earlier **submission** of the same payer, member, service and date |

The ledger replays claims in submission order and keeps these three tallies. `view(claim)` returns
what the payer knows when a claim arrives, including *which* claims used up the balance;
`record(claim, outcome)` updates the tallies and appends an audit entry (balances before and after).

**Validation against history** (real outcomes recorded, no rule engine): every paid claim (2,731)
fits its authorization, the daily cap and the duplicate check; all 79 D06 and 27 D05 denials are
reproduced; 53 of 65 D02 denials are reproduced. Each of the other 12 is fully explained by
claims-to-check submitted earlier on the same authorization.

## Assumptions

1. **History and claims-to-check are one timeline.** 540 of 546 claims-to-check were submitted before
   the last history claim. A claim-to-check sees history claims submitted before it, and later
   history claims already reflect it (that is what explains the 12 D02s above). `ledger.simulate`
   runs both files as one stream.
2. **Same-day ties are broken by claim ID.** The data has no time of day. Reversing or changing the
   tie-break changes nothing in history; 27 claims-to-check share a same-day group with another
   claim and will be flagged as resting on this assumption.
3. **Authorizations are not topped up.** Each patient, payer and service has one authorization for
   June–August and one for September–November; unused units don't carry over.
4. **Payers pay whole claims or deny them.** Every paid history claim was paid its full billed amount.
5. **Harbor's daily cap of 32 units** is hand-copied into `replay.py`, which checks the ledger without
   the rule engine. The engine reads it from `rules/PY02.json`.
6. **Code pairs never seen together in history are placed by analogy**, for example Summit D05 versus
   D02/D03. `rules/precedence_evidence.json` lists every unobserved pair.
7. **Claims-to-check are predicted without hindsight.** Later history claims sometimes reveal whether
   an earlier claim-to-check was paid (C012372 above). The engine does not use this.
