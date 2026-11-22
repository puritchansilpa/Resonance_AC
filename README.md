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
| Rule set, evidence checker, claim checker, fixer | not started |

## How to run

```powershell
python -m pip install -r requirements.txt
python -m claimcheck.clean     # Data/*.csv -> output/clean/
python -m claimcheck.replay    # history through the ledger -> output/ledger/
python -m pytest -q
```

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
5. **Harbor's daily cap of 32 units** is hand-copied into `replay.py` for validation until the
   versioned rule set exists.
