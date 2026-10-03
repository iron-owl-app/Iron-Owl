"""Release 3.9.1: credit card payments are transfers between the user's own accounts.

The card's purchases already count as spending, so the payment that settles the card must not
count again (it used to, as a "Loan payments" fixed bill). Rule runs and syncs mark a card
payment ``is_transfer = true`` with ``transfer_source = "auto"``; everything that already
leaves transfers out (spending, fixed, income, refunds, the budget, reports, Home, alerts,
"Needs a category", the Transactions money in/out, CSV's ``transfer`` column) then does the
right thing without a second definition.

Which transactions (``Context.is_payment``; SPEC "Credit card payments are transfers"):

- **The card side**: on a credit card, Plaid's detailed code ``LOAN_PAYMENTS_CREDIT_CARD_PAYMENT``
  (either sign), or money in filed under Loan payments or Transfer in (the payment as the card
  sees it, or a balance transfer). It is never pay, spending or a refund.
- **The bank side** (a bank/other account): **only when it pairs** with a card-side row on one
  of their visible credit cards: the same amount the other way, dated within ``PAIR_DAYS`` days
  (each card row pairs once; nearest dates first, ties by the lower ids). Candidates are
  visible bank/other rows with the detailed code (either sign: a returned payment pairs with
  money out of the card), and older rows (no detailed code, synced before v7): money out
  filed under Loan payments whose name has no loan word (mortgage, loan, lease...).

A payment with no card-side match keeps counting: paying a card FinTrack doesn't see (not
linked, hidden, linked later than the bank, a store card) is the only record of that
spending, and a false negative is preferred over hiding real spending. A payment whose card
side arrives in a later sync counts until then; the rule run after that sync marks it.

Precedence (``rules.resolve``): the user's own "Not a transfer" / "Mark as transfer"
(``transfer_source = "user"``) > a transfer rule (``"rule"``) > this (``"auto"``). Hand-set
categories and split transactions are never marked (``rules.release_hand_set`` clears the
mark when they set one by hand). The mark is recomputed on every rule run, so it goes away
when the evidence does.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import Account, AppSetting, Transaction

log = logging.getLogger("fintrack.card_payments")

AUTO = "auto"  # transactions.transfer_source for a card payment marked by FinTrack
DETAILED = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
LOAN = "LOAN_PAYMENTS"
TRANSFER_IN = "TRANSFER_IN"
CARD_IN_PRIMARIES = (LOAN, TRANSFER_IN)
CASH_ACCOUNTS = ("bank", "other")
CARD_ACCOUNT = "credit"
PAIR_DAYS = 5

# The one-time pass over existing transactions (Release 3.9.1): absent = still to do,
# "1" = done, "gave_up" = stopped after BACKFILL_MAX_FAILURES failures in a row.
BACKFILL_KEY = "card_payment_backfill_done"
BACKFILL_FAILURES_KEY = "card_payment_backfill_failures"
BACKFILL_MAX_FAILURES = 3

# Older bank rows (no detailed code) filed under Loan payments that name a loan are never
# card payments, even when an equal amount went into a card.
_LOAN_WORDS = re.compile(
    r"\b(mortgage|mtg|mtge|loan|lease|student|heloc|auto\s*(ln|fin\w*)|car\s*(payment|pmt))\b",
    re.IGNORECASE,
)


def names_a_loan(name: str | None, merchant: str | None) -> bool:
    """The description names a loan (mortgage, student loan, car payment...)."""
    return bool(_LOAN_WORDS.search(f"{name or ''} {merchant or ''}"))


def card_side(amount_cents: int, plaid_category: str | None, plaid_detailed: str | None) -> bool:
    """A credit card row that is the payment as the card sees it (or its return)."""
    if not amount_cents:
        return False
    if plaid_detailed == DETAILED:
        return True
    return amount_cents > 0 and plaid_category in CARD_IN_PRIMARIES


@dataclass(frozen=True)
class Context:
    """What ``is_payment`` needs besides the transaction itself (loaded once per rule run)."""

    kinds: dict[int, str]  # account id -> account category
    paired: frozenset[int]  # bank-side payments with a matching card-side payment

    @classmethod
    def load(cls, session: Session) -> Context:
        kinds: dict[int, str] = {}
        visible_cards: set[int] = set()
        visible_cash: set[int] = set()
        for account_id, category, hidden in session.execute(select(Account.id, Account.category, Account.hidden)):
            kinds[account_id] = category
            if not hidden and category == CARD_ACCOUNT:
                visible_cards.add(account_id)
            elif not hidden and category in CASH_ACCOUNTS:
                visible_cash.add(account_id)
        paired = _pairs(session, visible_cash, visible_cards) if visible_cash and visible_cards else frozenset()
        return cls(kinds, paired)

    def is_payment(
        self, *, txn_id: int | None, account_id: int, amount_cents: int, plaid_category: str | None,
        plaid_detailed: str | None,
    ) -> bool:
        """The card side by its Plaid codes; the bank side only when it pairs (``_pairs``).
        A row without an id yet (new in this sync) pairs at the rule run after the sync."""
        kind = self.kinds.get(account_id)
        if kind is None or not amount_cents:
            return False
        if kind == CARD_ACCOUNT:
            return card_side(amount_cents, plaid_category, plaid_detailed)
        return kind in CASH_ACCOUNTS and txn_id is not None and txn_id in self.paired


def _pairs(session: Session, cash_ids: set[int], card_ids: set[int]) -> frozenset[int]:
    """Ids of bank-side card payments (visible bank/other accounts) with a card-side payment
    on a visible credit card: the same amount the other way, dated within ``PAIR_DAYS`` days.

    Bank-side candidates: the detailed code (either sign), or an older row (no detailed code)
    of money out filed under Loan payments that names no loan. Card-side candidates:
    ``card_side`` rows, pending included. Each row pairs at most once; all possible pairs are
    taken nearest dates first, then by the lower bank id, then the lower card id, so the
    result doesn't depend on the order rows were read.
    """
    bank = [
        (txn_id, day, int(cents))
        for txn_id, day, cents, detailed, name, merchant in session.execute(
            select(
                Transaction.id, Transaction.date, Transaction.amount_cents, Transaction.plaid_detailed,
                Transaction.name, Transaction.merchant_name,
            ).where(
                Transaction.account_id.in_(cash_ids), Transaction.amount_cents != 0,
                or_(
                    Transaction.plaid_detailed == DETAILED,
                    and_(
                        Transaction.plaid_detailed.is_(None), Transaction.plaid_category == LOAN,
                        Transaction.amount_cents < 0,
                    ),
                ),
            )
        )
        if detailed == DETAILED or not names_a_loan(name, merchant)
    ]
    if not bank:
        return frozenset()
    cards: dict[int, list[tuple]] = defaultdict(list)  # amount -> [(date, id)]
    for txn_id, day, cents in session.execute(
        select(Transaction.id, Transaction.date, Transaction.amount_cents).where(
            Transaction.account_id.in_(card_ids), Transaction.amount_cents != 0,
            or_(
                Transaction.plaid_detailed == DETAILED,
                and_(Transaction.amount_cents > 0, Transaction.plaid_category.in_(CARD_IN_PRIMARIES)),
            ),
        )
    ):
        cards[int(cents)].append((day, txn_id))
    candidates = []
    for bank_id, day, cents in bank:
        for card_day, card_id in cards.get(-cents, ()):
            gap = abs((card_day - day).days)
            if gap <= PAIR_DAYS:
                candidates.append((gap, bank_id, card_id))
    candidates.sort()
    used_bank: set[int] = set()
    used_card: set[int] = set()
    for _gap, bank_id, card_id in candidates:
        if bank_id in used_bank or card_id in used_card:
            continue
        used_bank.add(bank_id)
        used_card.add(card_id)
    return frozenset(used_bank)


# ------------------------------------------------------------------ one-time pass (Release 3.9.1)


def backfill_once(session: Session) -> int:
    """Mark the card payments already in the vault (one rule run), once per vault.

    Rule runs after every sync and rule change keep the marks current afterwards; this only
    makes sure an upgraded vault shows the right numbers on the first unlock, before any sync.
    Returns how many transactions changed (0 when it already ran).
    """
    if session.get(AppSetting, BACKFILL_KEY) is not None:
        return 0
    from . import rules  # rules imports this module

    changed = rules.apply_all(session)
    session.add(AppSetting(key=BACKFILL_KEY, value="1"))
    session.flush()
    return changed


def safe_backfill(session: Session) -> None:
    """``backfill_once`` and commit; a failure is logged (type only) and retried next unlock.

    After ``BACKFILL_MAX_FAILURES`` failures in a row it stops (flag ``gave_up``), so a pass
    that can't succeed doesn't run on every unlock; the rule run after the next sync marks
    the payments anyway.
    """
    try:
        if session.get(AppSetting, BACKFILL_KEY) is not None:
            return
        backfill_once(session)
        count = session.get(AppSetting, BACKFILL_FAILURES_KEY)
        if count is not None:
            session.delete(count)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - never block an unlock
        session.rollback()
        log.error("card payment backfill failed: %s", type(exc).__name__)
        try:
            _count_backfill_failure(session)
            session.commit()
        except Exception as exc2:  # noqa: BLE001
            session.rollback()
            log.error("card payment backfill failure count failed: %s", type(exc2).__name__)


def _count_backfill_failure(session: Session) -> None:
    row = session.get(AppSetting, BACKFILL_FAILURES_KEY)
    try:
        failures = int(row.value) + 1 if row is not None and row.value else 1
    except ValueError:
        failures = 1
    if failures >= BACKFILL_MAX_FAILURES:
        if row is not None:
            session.delete(row)
        if session.get(AppSetting, BACKFILL_KEY) is None:
            session.add(AppSetting(key=BACKFILL_KEY, value="gave_up"))
        log.error("card payment backfill given up after %d failures", failures)
    elif row is None:
        session.add(AppSetting(key=BACKFILL_FAILURES_KEY, value=str(failures)))
    else:
        row.value = str(failures)
