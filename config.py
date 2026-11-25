"""Shared configuration for the search prototype.

THRESHOLD is tuned on data/tuning_queries.csv via tune.py,
NOT on the control set data/queries.csv.
"""

# Minimal RapidFuzz score (0..100) to accept a fuzzy candidate.
# Updated by `python tune.py --apply`.
THRESHOLD = 55

# Small hand-made abbreviation dictionary used by the smart mode
# before falling back to fuzzy matching.
ABBREVIATIONS = {
    "pwd": "password",
    "mgr": "manager",
    "antiv": "antivirus",
    "txt": "text",
    "ent": "enterprise",
    "prof": "professional",
    "std": "standard",
    "port": "portable",
    "rec": "recovery",
    "gen": "generator",
    "stu": "student",
    "biz": "business",
    "srv": "server",
    "calc": "calculator",
}

# Tokens that denote a product edition / architecture. When a query
# explicitly mentions one of them, candidates without a compatible
# edition are dropped. Values map a token to its canonical group.
EDITION_TOKENS = {
    "pro": "professional",
    "professional": "professional",
    "home": "home",
    "enterprise": "enterprise",
    "standard": "standard",
    "lite": "lite",
    "portable": "portable",
    "student": "student",
    "plus": "plus",
    "free": "free",
    "business": "business",
    "server": "server",
    "x64": "x64",
    "x86": "x86",
}
