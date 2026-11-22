# Exchange4Health claims data pack (synthetic)

All data here is made up. It describes 12 home care agencies billing three payers between June and November 2026.

## Your goal
For each claim in `claims_to_check.csv`, work out whether the payer will pay or deny it and, if denied, which denial code the payer will report. How you build that, and how far you go beyond it, is up to you.

If you produce predictions, a `predictions.csv` with `claim_id,status,denial_code` lets us compare approaches on the day.

## Files
| File | What it is |
|---|---|
| `claims_history.csv` | About 3,800 claims already adjudicated, with status, denial code and the payer's remark. Exported from the billing system as-is. |
| `claims_to_check.csv` | About 550 claims not yet submitted. Same columns, no outcome. |
| `authorizations.csv` | Each payer authorization: who, which service, the dates it covers and the units approved. |
| `evv_visits.csv` | Electronic Visit Verification records: one row per visit, with clock-in, clock-out and verification status. |
| `billing_basics.md` | Exchange4Health's help article on how claims, EVV and authorizations fit together. |
| `denial_codes.md` | The denial codes payers use. |
| `payers/` | Each payer's provider manual excerpt, plus any provider bulletins they've issued this year. |

## Things to know
- One unit is 15 minutes. `billed_amount` is units multiplied by a rate per service code.
- `submitted_date` is when a claim was, or will be, submitted. There is no "today": judge each claim on the dates it carries.
- When a claim breaks more than one rule, the payer reports one code only.
- The data comes from real-world-style exports, so expect some inconsistency in formats and naming.
