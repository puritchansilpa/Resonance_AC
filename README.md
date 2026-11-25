# Claim Check

Checks home-care claims before they go to the payer. For each claim it predicts **paid or denied**, the
**denial code** the payer will report, **how to fix it**, and **why**, citing the rule, its source text and
the evidence (EVV visits, authorization balance, filing dates). Built for agency billing teams.

**Design goal:** every prediction is explainable, auditable and identical on every run. Claims are
decided by code reading an approved rule set. Claude drafts and proposes rules, and a person
approves them. No model runs when a claim is decided.

## Problem
Of 3,830 past claims, 1,099 (28.7%) were denied: $114,913 billed. Three preventable causes make up 58%
of denials: units above verified EVV time (D09), late filing (D03) and no verified visit (D01). Each denial
means rework and slower payment, and a late claim is lost for good. 

**Good means:** the right status and exact code on past claims; every prediction names its rule, quote and
evidence; the same output on every run; every denial comes with a next step.

## Results
| | |
|---|---|
| Backtest on 3,830 past claims | **98.1%** exact code · **99.7%** excluding D99s the payer gave no reason for |
| 546 claims to check | 377 paid · 169 denied: 48 fixable, 32 need a decision, 89 can't be fixed by editing |
| Money | $36,278 of $54,065 paid as submitted · +$4,050 from safe fixes · +$3,193 awaiting a decision |
| Unbilled visits | 425 ready to file ($16,769) · 443 already past filing limits ($17,983 lost) |

## Run it (Python 3.10+)
```powershell
python -m pip install -r requirements.txt
python -m claimcheck.clean       # clean the 4 CSVs -> output/clean/
python -m claimcheck.predict     # predictions + backtest -> output/predictions.csv
python -m claimcheck.app         # web app at http://localhost:8765
python -m pytest -q              # 91 tests
```
Claude features need `ANTHROPIC_API_KEY`. Every response is cached by a hash of its request, so the
committed results replay without a key. Live demo of onboarding a new payer: `demo/README.md`.

## How it works
1. **Clean** the exports: payer names → IDs, codes, two date formats; 32 duplicate rows dropped; every change logged.
2. **Ledger:** all claims run in submission order. Authorization balances, daily caps and duplicates depend on earlier claims.
3. **Check** every rule in force for the payer and date of care. If several fail, the payer's code order picks the one reported.
4. **Fix:** one edit per failed rule, then re-check the whole claim (corrections, required cuts, or a decision for a person).
5. **App:** claims, claim detail, EVV visits and automator, authorizations, payer rules, add payer, notices, AI review.

**People decide:** approve every rule and bulletin change, answer open questions, choose fewer units vs
asking the payer, review flagged claims, send messages. Nothing is submitted automatically.

## How we know it's right
- **Backtest:** each past claim is judged on the balances the payer saw at the time, then its real outcome
  is recorded. 98.4% paid/denied (vs ~71% for "predict all paid"), 98.1% exact code. All 74 misses are named: 61 D99s with no reason, 12 Summit D99s whose remark is the whole-hours rule, 1 inherited from a predicted claim.
- **AI rule drafting:** drafted without seeing the payer's own file, 25/25 rules match, same backtest;
  both bulletins rebuilt exactly.
- **Learned rules** are kept only if history supports them: Summit whole hours, 35 for, 0 against.
- **Limit:** the code order and Summit rule are learned from the same history they're scored on (learning
  from pre-September claims only gives the same answers). Next: a time-split score, then a pilot.

## Additional Features
A. **Claims Automator**: drafts claims for verified EVV visits that are on no claim, then checks each one like any other claim using the payer and authorization on file. Sorted by filing deadline so the most urgent come first. *Why:* a checker only sees claims that exist. 900 verified visits were never billed, and 443 of them are already past the filing limit ($17,983 lost). It reuses the same checker, so it costs little to add.

B. **Rule Drafter**: Claude (Opus 5.5) drafts a payer's rules from its manual, or only the changes from a new bulletin. Every rule quotes its source, unclear clauses that don't fit become open questions, and a person approves. *Why:* rules change (two payers issued bulletins this year) and new payers get added. Writing rules by hand doesn't scale and is where errors creep in.

C. **Messages**: drafted only when someone other than the biller has to act. Code picks who (care coordinator, payer care manager, billing lead) and what to ask; Claude (Haiku 5.5) writes the wording from the claim's facts only. *Why:* many denials can't be fixed by editing the claim. The fix sits with someone else (an EVV record, more authorization units, a write-off decision), and a ready draft with the exact facts saves the back-and-forth.

D. **Rule Finder**: Claude reads payer remarks on denials the rules missed and proposes rules; history tests them. It rediscovered Summit's whole-hours rule from 2026-07-01. *Why:* some rules aren't in any document (Summit's is in a contract we don't have), but payer remarks give them away.

## Assumptions
- History and claims-to-check are one timeline. This affects which claims use an authorization first. Same-day ties go by claim ID (changing it alters no history result).
- A visit counts on its clock-in day, and same-day visits are added together.
- Authorizations aren't topped up; a new period needs a new authorization. A paid claim draws its full units and denied claims draw none.
- From history, not documents, so they may change: each payer's code order, and Summit's whole-hours rule (named only in payer remarks).
- 61 D99s ("contact payer") break no rule and can't be predicted (~1.6% of claims).
- The automator dates drafts 2026-11-22. Lakeside (PY04) in `demo/` is fictional.

## Given more time
- **D99s with no reason (61 claims, 1.6%).** So far they are only grouped by remark, and Claude's remark
  reader judged them unexplainable. Next: compare them with similar paid claims on traits no rule checks
  (payer, agency, service, caregiver, amount, days to file, authorization near its end or balance, visits
  that cross midnight, timing after a bulletin). A pattern that holds across history with no paid claim
  contradicting it becomes a learned rule, tested and approved like Summit's whole hours. If none holds,
  report a D99 risk rate instead: 61 of the 2,791 past claims the rules call paid came back D99 (~2%), so
  about 8 of the 377 "will be paid" claims may come back D99.
- **Dashboard with charts.** One screen showing the number of claims or their dollar value, broken down
  by time (date of service or submission), outcome or denial code, payer, or agency, with filters that
  combine. It would show trends the tables hide, such as a payer's denials rising after a bulletin or
  money lost to late filing each month.
