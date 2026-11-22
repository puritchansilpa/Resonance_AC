# How claims, EVV and authorizations fit together

*Exchange4Health help centre article, last updated May 2026*

When a caregiver clocks in and out on the Exchange4Health+ app, we create an EVV visit record. Each record goes through verification: most are verified automatically, but a record that a coordinator has edited by hand sits at `manual_edit_pending` until the payer's aggregator accepts it, and some are `rejected` outright. Payers treat only verified visits as evidence that care took place.

A visit belongs to the date the caregiver clocked in, even when it runs past midnight. If a caregiver visits the same patient for the same service more than once in a day, payers add the visits together.

Agencies bill each service on a claim for a date of service. Most payers need the claim to quote a prior authorization, and the authorization has to be for that patient and service, and in force on the date of service. Every paid claim draws its units down from the authorization, in the order claims are submitted. Once the units run out, further claims against it are denied until a new authorization is issued.

If a claim is denied for something you can fix, such as a missing modifier or authorization number, submit a corrected claim with frequency code `7` and the original claim's ID in `original_claim_id`. Payers process a corrected claim in its own right and don't treat it as a duplicate. A new claim (frequency code `1`) for the same patient, service, date of service and payer as one already submitted is a duplicate, whatever happened to the first one.

Rounding rules for converting visit minutes to units differ by payer. Check each payer's manual.
