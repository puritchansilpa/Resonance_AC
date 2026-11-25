# Demo: onboarding a new payer live

**Lakeside Community Health (PY04)** is a fictional payer written for the demo. Its two documents
are in `demo/lakeside/`:

| File | What it tests |
|---|---|
| `lakeside_billing_manual.md` | 8 rules the engine can express (EVV, 8-minute rule, authorization number / fit / balance, U1 on S5125, 90-day filing, duplicates) and 2 it can't: a respite-only daily cap (5.2) and "Lakeside Choice may be subject to additional review" (6.3). A good draft turns those 2 into open questions. |
| `lakeside_notice_2026-10.md` | A dated change (90 → 60 days from 2026-11-01) and a holiday modifier U4 the engine can't represent (no holiday list, unknown modifier). |
| `expected_PY04.json`, `expected_PY04_after_notice.json` | Hand-written answer keys used to score Claude's drafts live. |

## In the app (recommended for the demo)

Start the app with the key set, then: **Payer rules → Add payer → Use demo manual → Register payer →
Draft rules → (read the score and open questions) → Approve rules**. Then **Notices → Use demo notice
→ Upload → Draft the change with Claude → Approve change**.

```powershell
$env:ANTHROPIC_API_KEY = "<your key>"
```
```powershell
python -m claimcheck.app
```

## Script from the command line (about 2 minutes, roughly $0.15 of API credit)

Run in a PowerShell window with `ANTHROPIC_API_KEY` set.

1. Register the payer and its manual:
   ```powershell
   python -m claimcheck.onboard add PY04 --name "Lakeside Community Health" --alias "LAKESIDE CH" --doc demo/lakeside/lakeside_billing_manual.md
   ```
2. Claude drafts the rule file (Harbor's approved file is the worked example):
   ```powershell
   python -m claimcheck.extract run PY04
   ```
3. Score the draft against the answer key and read the open questions:
   ```powershell
   python -m claimcheck.onboard check rules/proposed/PY04.json demo/lakeside/expected_PY04.json
   ```
4. Approve it (edit `rules/proposed/PY04.json` first if a rule is wrong; edits are logged as lessons):
   ```powershell
   python -m claimcheck.extract approve PY04 --by "Your name" --note "Reviewed in demo"
   ```
5. The notice arrives. Claude drafts only the change (Northwind's bulletin is the worked example):
   ```powershell
   python -m claimcheck.bulletins draft PY04 demo/lakeside/lakeside_notice_2026-10.md
   ```
6. Score the change:
   ```powershell
   python -m claimcheck.onboard check rules/proposed/bulletins/lakeside_notice_2026-10.json demo/lakeside/expected_PY04_after_notice.json
   ```

Both Claude responses are cached under their request fingerprint, so a second run of the
demo replays the same drafts without calling the model.

## What to point out
- Claude only drafts; every rule quotes the manual word for word, and the quote is checked.
- The two clauses no rule type can express should come back as open questions, not rules.
- Lakeside has no claims history, so its code order is a default and it has no learned rules yet.
- Approving changes the rule-set fingerprint printed on every prediction.

## Reset afterwards
The demo writes the registry, the stored documents, PY04's draft and rule files, and any reviewer
corrections. This removes whichever of them exist (missing ones are skipped silently; notices uploaded
in the app are stored as `PY04__<file name>`, which the wildcard covers):
```powershell
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue onboarding, rules/PY04.json, rules/proposed/PY04.json, rules/proposed/PY04.meta.json, rules/proposed/bulletins/*lakeside_notice*, rules/extraction_corrections.jsonl
```
Then restart the app so it reloads the rule set. Keep the `llm_cache/` and `rules/proposed/cache/`
entries: they let the next run replay the same drafts without calling Claude. The cached Lakeside draft
from rehearsal scored 8/8 against the answer key, with both untranslatable clauses raised as open questions.
