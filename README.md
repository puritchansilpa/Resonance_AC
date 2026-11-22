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
| Fixer, review flags, model-based rule extraction, interface | not started |

## How to run

```powershell
python -m pip install -r requirements.txt
python -m claimcheck.clean        # Data/*.csv -> output/clean/
python -m claimcheck.replay       # history through the ledger -> output/ledger/
python -m claimcheck.precedence   # code order evidence -> rules/precedence_evidence.json
python -m claimcheck.predict      # predictions + backtest -> output/
python -m pytest -q
```

## Results (rule set `904df479f483`)

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
