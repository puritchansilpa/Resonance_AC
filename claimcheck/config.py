"""Fixed reference values used by the pipeline.

Everything here is a cleaning or validation rule, not a payer billing rule.
Payer billing rules (rounding, filing limits, daily caps...) live in the rule set,
which is built separately and versioned.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "Data"
OUTPUT_DIR = REPO_ROOT / "output"
CLEAN_DIR = OUTPUT_DIR / "clean"
LEDGER_DIR = OUTPUT_DIR / "ledger"

RAW_FILES = {
    "claims_history": "claims_history.csv",
    "claims_to_check": "claims_to_check.csv",
    "authorizations": "authorizations.csv",
    "evv_visits": "evv_visits.csv",
}

# Every payer spelling seen in the exports, mapped to its payer ID.
# An unseen spelling raises an error instead of being guessed.
PAYER_ALIASES = {
    "PY01": "PY01",
    "NORTHWIND HP": "PY01",
    "NORTHWIND HEALTH PLAN": "PY01",
    "PY02": "PY02",
    "HARBOR MEDICAID": "PY02",
    "HARBOR MEDICAID PLAN": "PY02",
    "PY03": "PY03",
    "SUMMIT CARE": "PY03",
    "SUMMIT CARE PARTNERS": "PY03",
}

PAYER_NAMES = {
    "PY01": "Northwind Health Plan",
    "PY02": "Harbor Medicaid Plan",
    "PY03": "Summit Care Partners",
}

SERVICE_CODES = {"T1019", "T1005", "S5125"}
MODIFIERS = {"U1", "U2", "U3"}
FREQUENCY_CODES = {1, 7}

# Rate per 15-minute unit. Used only to check billed_amount = units x rate.
SERVICE_RATES = {"T1019": 6.10, "T1005": 4.80, "S5125": 5.40}

VERIFICATION_STATUSES = {"verified", "manual_edit_pending", "rejected"}

# Accepted date formats, tried in order. Slash dates were checked to be
# month/day/year: no file has a first number above 12.
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
