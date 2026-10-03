"""Categorization rules: matching, and re-applying every rule to every transaction.

Precedence for a transaction's effective category:
user override > first matching enabled rule > the budget's built-in mapping of Plaid's
detailed code (services/automap.py, source "auto") > Plaid's primary > OTHER (NULL).

The transfer flag: the user's own choice (``transfer_source = "user"``) > a transfer rule ("rule") >
a credit card payment (services/card_payments.py, "auto", Release 3.9.1) > not a transfer.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session

from ..models import Rule, Transaction, TransactionSplit
from . import automap, card_payments

# Hand-set categories: rule runs and syncs never change these (``user_bulk`` = set several at once).
USER_SOURCES = ("user", "user_bulk")


@dataclass(frozen=True)
class Matcher:
    id: int | None
    field: str  # any | merchant | name
    op: str  # contains | is
    text: str  # already trimmed + casefolded
    amount_op: str | None  # gt | lt | None
    amount_cents: int | None
    action: str  # category | transfer
    category: str | None

    @classmethod
    def build(
        cls,
        *,
        id: int | None,  # noqa: A002
        field: str,
        op: str,
        text: str,
        amount_op: str | None,
        amount_cents: int | None,
        action: str,
        category: str | None,
    ) -> Matcher:
        return cls(id, field, op, _norm(text), amount_op, amount_cents, action, category)

    @classmethod
    def from_rule(cls, rule: Rule) -> Matcher:
        return cls.build(
            id=rule.id, field=rule.field, op=rule.op, text=rule.text, amount_op=rule.amount_op,
            amount_cents=rule.amount_cents, action=rule.action, category=rule.category,
        )

    def _text_ok(self, value: str | None) -> bool:
        if value is None:
            return False
        value = _norm(value)
        return value == self.text if self.op == "is" else self.text in value

    def matches(self, name: str | None, merchant: str | None, amount_cents: int) -> bool:
        if not self.text:
            return False
        if self.field == "name":
            ok = self._text_ok(name)
        elif self.field == "merchant":
            ok = self._text_ok(merchant)
        else:
            ok = self._text_ok(name) or self._text_ok(merchant)
        if not ok:
            return False
        if self.amount_op and self.amount_cents is not None:
            magnitude = abs(int(amount_cents))
            if self.amount_op == "gt" and not magnitude > self.amount_cents:
                return False
            if self.amount_op == "lt" and not magnitude < self.amount_cents:
                return False
        return True


def _norm(text: str) -> str:
    return text.strip().casefold()


def enabled_matchers(session: Session) -> list[Matcher]:
    rules = session.scalars(
        select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.position, Rule.id)
    )
    return [Matcher.from_rule(r) for r in rules]


def first_match(matchers: list[Matcher], name: str | None, merchant: str | None, amount_cents: int) -> int | None:
    """Id of the first enabled rule (any action) matching this transaction, if any."""
    return next((m.id for m in matchers if m.matches(name, merchant, amount_cents)), None)


def resolve(
    matchers: list[Matcher],
    *,
    name: str | None,
    merchant: str | None,
    amount_cents: int,
    plaid_category: str | None,
    is_transfer: bool,
    transfer_source: str | None,
    plaid_detailed: str | None = None,
    auto_map: dict[str, str] | None = None,
    card_payment: bool = False,
    blocked_category: str | None = None,
) -> dict:
    """Desired rule-controlled fields for a non-user-categorized transaction.

    ``auto_map`` (automap.load) sorts a transaction by Plaid's detailed code when no
    category rule matches it. ``card_payment`` (``card_payments.Context.is_payment``) marks
    it a transfer (source "auto") unless the user decided themselves or a transfer rule matches; a
    category rule sets the category and leaves that mark alone.

    ``blocked_category`` (Release 3.10, ``debt_budget.blocked_category`` when this row is a
    credit card payment): a category rule for it doesn't match; the next rule decides.
    """
    rule = next(
        (
            m for m in matchers
            if not (blocked_category is not None and m.action == "category" and m.category == blocked_category)
            and m.matches(name, merchant, amount_cents)
        ),
        None,
    )
    mapped = auto_map.get(plaid_detailed) if auto_map and plaid_detailed else None
    out = {
        "category": mapped or plaid_category,
        "category_source": automap.AUTO_SOURCE if mapped else "plaid",
        "rule_id": None,
        "is_transfer": bool(is_transfer),
        "transfer_source": transfer_source,
    }
    if transfer_source != "user":
        if rule is not None and rule.action == "transfer":
            out["is_transfer"], out["transfer_source"] = True, "rule"
        elif card_payment:
            out["is_transfer"], out["transfer_source"] = True, card_payments.AUTO
        elif transfer_source in ("rule", card_payments.AUTO):
            out["is_transfer"], out["transfer_source"] = False, None
    if rule is None:
        return out
    out["rule_id"] = rule.id
    if rule.action == "category":
        out["category"], out["category_source"] = rule.category, "rule"
    return out


def card_payment(ctx: card_payments.Context, row) -> bool:  # noqa: ANN001 - a Row or a Transaction
    """``ctx.is_payment`` for a transaction row (needs id, account_id, amount_cents,
    plaid_category, plaid_detailed)."""
    return ctx.is_payment(
        txn_id=row.id, account_id=row.account_id, amount_cents=int(row.amount_cents or 0),
        plaid_category=row.plaid_category, plaid_detailed=row.plaid_detailed,
    )


def debt_blocker(session: Session, ctx: card_payments.Context):  # noqa: ANN201
    """Release 3.10: row -> ``resolve``'s ``blocked_category`` (the "Paying off debt" category
    when the row is a credit card payment, else None). Card payments never go in it."""
    from . import debt_budget  # debt_budget imports this module

    category = debt_budget.blocked_category(session)
    if category is None:
        return lambda row: None
    return lambda row: category if debt_budget.is_card_payment(ctx, row) else None


def release_hand_set(session: Session) -> int:
    """Hand-set categories and splits win (Release 3.9.1): clear the automatic card payment
    mark on any transaction the user categorized by hand or split. One statement; runs inside the
    caller's transaction. Returns how many rows changed."""
    res = session.execute(
        update(Transaction)
        .where(
            Transaction.transfer_source == card_payments.AUTO,
            or_(Transaction.category_source.in_(USER_SOURCES), _has_splits()),
        )
        .values(is_transfer=False, transfer_source=None)
        .execution_options(synchronize_session="fetch")
    )
    return int(res.rowcount or 0)


def make_room(session: Session, index: int) -> int:
    """Settings (D7): renumber the rules 0..n-1 leaving a gap at ``index`` (beyond the end =
    last) and return the position for a new rule there. Call before adding the new rule."""
    ordered = list(session.scalars(select(Rule).order_by(Rule.position, Rule.id)))
    index = max(0, min(index, len(ordered)))
    for i, rule in enumerate(ordered):
        rule.position = i if i < index else i + 1
    return index


def apply_all(session: Session) -> int:
    """Re-apply the rules to every transaction whose category isn't a user override.

    Runs inside the caller's transaction; returns how many rows changed.
    """
    matchers = enabled_matchers(session)
    auto_map = automap.load(session)
    ctx = card_payments.Context.load(session)
    blocker = debt_blocker(session, ctx)
    released = release_hand_set(session)
    rows = session.execute(
        select(
            Transaction.id, Transaction.account_id, Transaction.name, Transaction.merchant_name,
            Transaction.amount_cents, Transaction.plaid_category, Transaction.plaid_detailed, Transaction.category,
            Transaction.category_source, Transaction.rule_id, Transaction.is_transfer, Transaction.transfer_source,
        ).where(Transaction.category_source.not_in(USER_SOURCES), ~_has_splits())
    ).all()
    changes = []
    for row in rows:
        want = resolve(
            matchers, name=row.name, merchant=row.merchant_name, amount_cents=row.amount_cents,
            plaid_category=row.plaid_category, is_transfer=bool(row.is_transfer),
            transfer_source=row.transfer_source, plaid_detailed=row.plaid_detailed, auto_map=auto_map,
            card_payment=card_payment(ctx, row), blocked_category=blocker(row),
        )
        have = {
            "category": row.category, "category_source": row.category_source, "rule_id": row.rule_id,
            "is_transfer": bool(row.is_transfer), "transfer_source": row.transfer_source,
        }
        if want != have:
            changes.append({"id": row.id, **want})
    if changes:
        session.execute(update(Transaction), changes)
        session.expire_all()
    return len(changes) + released


PREVIEW_SAMPLE = 20


def preview_changes(
    session: Session, candidate: Matcher | None, *, replace_id: int | None = None,
) -> tuple[int, list[tuple[object, dict]]]:
    """Settings D7 update 2: what saving a rule would change.

    The enabled rules as they are, with ``candidate`` at the top (a new rule) or in place of
    rule ``replace_id`` (an edit; ``candidate`` None = the edited rule is off). Returns how
    many automatic transactions' category or transfer flag would change (hand-set rows and
    split transactions never do) and up to PREVIEW_SAMPLE of them, newest first, as
    ``(row, wanted)`` pairs.
    """
    rules = list(session.scalars(select(Rule).order_by(Rule.position, Rule.id)))
    matchers: list[Matcher] = []
    placed = False
    for rule in rules:
        if replace_id is not None and rule.id == replace_id:
            placed = True
            if candidate is not None:
                matchers.append(candidate)
        elif rule.enabled:
            matchers.append(Matcher.from_rule(rule))
    if replace_id is None and candidate is not None:
        matchers.insert(0, candidate)
    elif not placed and candidate is not None:
        matchers.insert(0, candidate)
    auto_map = automap.load(session)
    ctx = card_payments.Context.load(session)
    blocker = debt_blocker(session, ctx)
    rows = session.execute(
        select(
            Transaction.id, Transaction.account_id, Transaction.date, Transaction.name, Transaction.merchant_name,
            Transaction.amount_cents, Transaction.plaid_category, Transaction.plaid_detailed, Transaction.category,
            Transaction.is_transfer, Transaction.transfer_source,
        )
        .where(Transaction.category_source.not_in(USER_SOURCES), ~_has_splits())
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    ).all()
    count = 0
    sample: list[tuple[object, dict]] = []
    for row in rows:
        want = resolve(
            matchers, name=row.name, merchant=row.merchant_name, amount_cents=row.amount_cents,
            plaid_category=row.plaid_category, is_transfer=bool(row.is_transfer),
            transfer_source=row.transfer_source, plaid_detailed=row.plaid_detailed, auto_map=auto_map,
            card_payment=card_payment(ctx, row), blocked_category=blocker(row),
        )
        if want["category"] != row.category or want["is_transfer"] != bool(row.is_transfer):
            count += 1
            if len(sample) < PREVIEW_SAMPLE:
                sample.append((row, want))
    return count, sample


def preview_count(session: Session, matcher: Matcher) -> int:
    """How many transactions this rule alone would match (other rules ignored)."""
    rows = session.execute(
        select(Transaction.name, Transaction.merchant_name, Transaction.amount_cents).where(~_has_splits())
    ).all()
    return sum(1 for r in rows if matcher.matches(r.name, r.merchant_name, r.amount_cents))


def _has_splits():  # noqa: ANN202
    """Rules never change a transaction that has splits (SPEC Release 3)."""
    return exists().where(TransactionSplit.transaction_id == Transaction.id)
