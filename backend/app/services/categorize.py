from __future__ import annotations

RETIREMENT_SUBTYPES = frozenset(
    {
        "401k",
        "403b",
        "457b",
        "ira",
        "roth",
        "roth 401k",
        "sep ira",
        "simple ira",
        "pension",
        "retirement",
        "keogh",
        "401a",
        "thrift savings plan",
        "sarsep",
        "non-taxable brokerage account",
    }
)


def categorize(plaid_type: str | None, plaid_subtype: str | None) -> str:
    """Map a Plaid (type, subtype) pair to one of our account categories."""
    t = (plaid_type or "").strip().lower()
    s = (plaid_subtype or "").strip().lower()
    if t in ("depository", "investment") and s == "hsa":
        return "hsa"
    if t == "depository":
        return "bank"
    if t == "investment":
        return "retirement" if s in RETIREMENT_SUBTYPES else "investment"
    if t == "loan":
        return "loan"
    if t == "credit":
        return "credit"
    return "other"
