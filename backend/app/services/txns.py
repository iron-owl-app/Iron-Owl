"""Release 3 transaction features: splits, tags, bulk recategorize, and the output shape."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from fractions import Fraction

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from ..models import Account, CategoryGroup, Tag, Transaction, TransactionSplit, TransactionTag, TxnCategory
from ..schemas import MAX_SPLITS, MAX_TAGS_PER_TXN, transaction_out_r2
from ..seeds import OTHER
from ..utils import fmt_usd, from_cents, to_cents
from . import debt_budget, rules
from .categories import CategoryMap
from .categories import load as load_categories
from .recurring import merchant_key

MIN_SPLITS = 2
MAX_SUGGESTIONS = 2
TRANSFER_KIND = "transfer"


class TxnError(ValueError):
    """Validation failure; the message is safe to show (422)."""


class TxnNotFound(LookupError):
    """A requested transaction doesn't exist (404)."""


# ------------------------------------------------------------------ needs a category (Release 3.3)
# "Anything not in the budget": the SQL clause and the per-row check below are the same
# definition (SPEC R3.3) and must change together; tests compare them row by row.


def has_groups(session: Session) -> bool:
    return session.scalar(select(CategoryGroup.id).limit(1)) is not None


def outside_budget(cmap: CategoryMap, category_id: str | None, grouped: bool) -> bool:
    """A category that leaves a bank-categorized transaction needing a category."""
    info = cmap.by_id.get(category_id) if category_id is not None else None
    if info is None or info.id == OTHER or info.hidden:
        return True  # NULL, Other, an id that no longer exists, or a hidden category
    # With a budget (any group), a bank category outside every group isn't in it; transfers never are.
    return grouped and info.group_id is None and info.kind != TRANSFER_KIND


def needs_category(t: Transaction, cmap: CategoryMap, grouped: bool, split: bool) -> bool:
    if split or t.is_transfer or (t.category_source or "plaid") != "plaid":
        return False  # split parts have their own categories; hand- and rule-set rows are settled
    return outside_budget(cmap, t.category, grouped)


def needs_category_clause():  # noqa: ANN201 - SQL twin of needs_category()
    in_budget = select(TxnCategory.id).where(TxnCategory.hidden.is_(False), TxnCategory.id != OTHER)
    ungrouped = select(TxnCategory.id).where(TxnCategory.group_id.is_(None), TxnCategory.kind != TRANSFER_KIND)
    return and_(
        ~exists().where(TransactionSplit.transaction_id == Transaction.id),
        Transaction.is_transfer.is_(False),
        Transaction.category_source == "plaid",
        or_(
            Transaction.category.is_(None),
            Transaction.category.not_in(in_budget),
            and_(select(CategoryGroup.id).exists(), Transaction.category.in_(ungrouped)),
        ),
    )


# ------------------------------------------------------------------ output


def extras(session: Session, ids: list[int]) -> tuple[dict[int, list[TransactionSplit]], dict[int, list[Tag]]]:
    """Splits and tags for these transactions, in display order."""
    splits: dict[int, list[TransactionSplit]] = defaultdict(list)
    tags: dict[int, list[Tag]] = defaultdict(list)
    if not ids:
        return splits, tags
    for chunk in _chunks(ids):
        for s in session.scalars(
            select(TransactionSplit)
            .where(TransactionSplit.transaction_id.in_(chunk))
            .order_by(TransactionSplit.position, TransactionSplit.id)
        ):
            splits[s.transaction_id].append(s)
        for txn_id, tag in session.execute(
            select(TransactionTag.transaction_id, Tag)
            .join(Tag, Tag.id == TransactionTag.tag_id)
            .where(TransactionTag.transaction_id.in_(chunk))
            .order_by(func.lower(Tag.name), Tag.id)
        ):
            tags[txn_id].append(tag)
    return splits, tags


def _chunks(ids: list[int], size: int = 900):  # noqa: ANN202 - SQLite bound-parameter limit
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def out_many(session: Session, rows: list[tuple[Transaction, str]], cmap: CategoryMap | None = None) -> list[dict]:
    cmap = cmap or load_categories(session)
    splits, tags = extras(session, [t.id for t, _ in rows])
    grouped = has_groups(session)
    matchers = rules.enabled_matchers(session) if rows else []
    needs = [t for t, _ in rows if needs_category(t, cmap, grouped, bool(splits.get(t.id)))]
    suggested = suggestions(session, cmap, grouped, needs)
    needs_ids = {t.id for t in needs}
    out = []
    for t, name in rows:
        split = bool(splits.get(t.id))
        item = transaction_out_r2(t, name, cmap, splits.get(t.id, ()), tags.get(t.id, ()))
        item["needs_category"] = t.id in needs_ids
        item["suggested_categories"] = suggested.get(t.id, [])
        # The rule behind the category (or the one that would set it); rules skip split rows.
        item["matching_rule_id"] = (
            None if split else rules.first_match(matchers, t.name, t.merchant_name, int(t.amount_cents))
        )
        out.append(item)
    return out


def suggestions(
    session: Session, cmap: CategoryMap, grouped: bool, targets: list[Transaction]
) -> dict[int, list[str]]:
    """Up to two categories used most on past transactions of the same merchant, per transaction.

    One query for the page. The merchant key (whitespace-collapsed, Unicode-lowercased) is
    computed in Python for every candidate row, because SQLite's lower() only folds ASCII
    and can't collapse spacing. Categories that would leave the row still needing a
    category are never suggested.
    """
    if not targets:
        return {}
    keys = {t.id: merchant_key(t.merchant_name, t.name) for t in targets}
    wanted = set(keys.values())
    query = select(
        Transaction.merchant_name, Transaction.name, Transaction.category, Transaction.date
    ).where(
        Transaction.category.is_not(None),
        Transaction.is_transfer.is_(False),
        ~exists().where(TransactionSplit.transaction_id == Transaction.id),
    )
    counts: dict[tuple[str, str], int] = defaultdict(int)
    latest: dict[tuple[str, str], dt.date] = {}
    for row in session.execute(query):
        key = merchant_key(row.merchant_name, row.name)
        if key not in wanted:
            continue
        category = cmap.effective_id(row.category)
        if outside_budget(cmap, category, grouped):
            continue
        counts[(key, category)] += 1
        if (key, category) not in latest or row.date > latest[(key, category)]:
            latest[(key, category)] = row.date
    ranked: dict[str, list[str]] = defaultdict(list)
    for key, category in sorted(
        counts,
        key=lambda kc: (-counts[kc], -latest[kc].toordinal(), cmap.get(kc[1]).name.casefold(), kc[1]),
    ):
        ranked[key].append(category)
    return {txn_id: ranked.get(key, [])[:MAX_SUGGESTIONS] for txn_id, key in keys.items()}


def out_ids(session: Session, ids: list[int]) -> list[dict]:
    """These transactions (only these), in this order."""
    by_id: dict[int, tuple[Transaction, str]] = {}
    for chunk in _chunks(ids):
        for t, name in session.execute(
            select(Transaction, Account.name)
            .join(Account, Account.id == Transaction.account_id)
            .where(Transaction.id.in_(chunk))
        ).all():
            by_id[t.id] = (t, name)
    return out_many(session, [by_id[i] for i in ids if i in by_id])


def out_one(session: Session, transaction_id: int) -> dict:
    txn = session.get(Transaction, transaction_id)
    account_name = session.scalar(select(Account.name).where(Account.id == txn.account_id))
    return out_many(session, [(txn, account_name)])[0]


# ------------------------------------------------------------------ splits


def set_splits(session: Session, txn: Transaction, parts: list[tuple[float, str, str | None]]) -> None:
    """Replace the transaction's splits (an empty list removes them). Raises TxnError."""
    if not parts:
        session.execute(delete(TransactionSplit).where(TransactionSplit.transaction_id == txn.id))
        session.flush()
        return
    if not MIN_SPLITS <= len(parts) <= MAX_SPLITS:
        raise TxnError(f"Split a transaction into {MIN_SPLITS} to {MAX_SPLITS} parts.")
    total = int(txn.amount_cents)
    if total == 0:
        raise TxnError("A $0.00 transaction can't be split.")
    known = set(session.scalars(select(TxnCategory.id)))
    rows: list[tuple[int, str, str | None]] = []
    for amount, category, notes in parts:
        cents = to_cents(amount)
        if cents == 0:
            raise TxnError("Each part needs an amount other than $0.00.")
        if (cents > 0) != (total > 0):
            what = "money in" if total > 0 else "money out"
            raise TxnError(f"Every part must be {what}, like the transaction itself.")
        if category not in known:
            raise TxnError(f"Unknown category {category!r}.")
        cleaned = (notes or "").strip() or None
        rows.append((cents, category, cleaned))
    parts_total = sum(c for c, _, _ in rows)
    if parts_total != total:
        diff = abs(total - parts_total)
        raise TxnError(
            f"The parts add up to {fmt_usd(parts_total)}, not {fmt_usd(total)}. "
            f"{'Add' if abs(parts_total) < abs(total) else 'Remove'} {fmt_usd(diff)}."
        )
    session.execute(delete(TransactionSplit).where(TransactionSplit.transaction_id == txn.id))
    for position, (cents, category, notes) in enumerate(rows):
        session.add(TransactionSplit(
            transaction_id=txn.id, amount_cents=cents, category=category, notes=notes, position=position,
        ))
    session.flush()
    rules.release_hand_set(session)  # the user's split wins over the card payment mark


def _round_half_away(value: Fraction) -> int:
    whole = abs(value.numerator) * 2 + value.denominator
    rounded = whole // (2 * value.denominator)
    return rounded if value >= 0 else -rounded


def rescale(parts: list[int], old_total: int, new_total: int) -> list[int] | None:
    """Scale split parts proportionally to a new parent amount (SPEC R3 "Sync").

    Each part is rounded to the cent (half away from zero); the rounding remainder goes to
    the largest part. Returns None when the result can't stay a valid split (a new total of
    zero, or a part that would round to zero or change sign): the caller then drops the splits.
    """
    if old_total == 0 or new_total == 0 or not parts:
        return None
    ratio = Fraction(new_total, old_total)
    scaled = [_round_half_away(ratio * p) for p in parts]
    largest = max(range(len(parts)), key=lambda i: (abs(scaled[i]), -i))
    scaled[largest] += new_total - sum(scaled)
    positive = new_total > 0
    if any(s == 0 or (s > 0) != positive for s in scaled):
        return None
    return scaled


def rescale_splits(session: Session, txn_id: int, old_total: int, new_total: int) -> None:
    """After Plaid changed a split transaction's amount: scale its parts (or drop them)."""
    rows = list(session.scalars(
        select(TransactionSplit)
        .where(TransactionSplit.transaction_id == txn_id)
        .order_by(TransactionSplit.position, TransactionSplit.id)
    ))
    if not rows or old_total == new_total:
        return
    scaled = rescale([r.amount_cents for r in rows], old_total, new_total)
    if scaled is None:
        session.execute(delete(TransactionSplit).where(TransactionSplit.transaction_id == txn_id))
        return
    for row, cents in zip(rows, scaled):
        row.amount_cents = cents


# ------------------------------------------------------------------ bulk recategorize


def _norm(text: str | None) -> str:
    return (text or "").strip().casefold()


def _recategorize_rows(session: Session, field: str, text: str, category: str) -> tuple[CategoryMap, list, list]:
    """(cmap, matches, already): unsplit transactions whose field is exactly ``text``."""
    cmap = load_categories(session)
    if category not in cmap.by_id:
        raise TxnError(f"Unknown category {category!r}.")
    needle = _norm(text)
    if not needle:
        raise TxnError("Enter the merchant or name to match.")
    column = Transaction.merchant_name if field == "merchant" else Transaction.name
    has_splits = select(TransactionSplit.id).where(TransactionSplit.transaction_id == Transaction.id).exists()
    rows = session.execute(
        select(Transaction.id, column.label("value"), Transaction.category, Transaction.category_source)
        .where(column.is_not(None), ~has_splits)
    ).all()
    matching = [r for r in rows if _norm(r.value) == needle]
    already = [r for r in matching if cmap.effective_id(r.category) == category]
    matches = [r for r in matching if cmap.effective_id(r.category) != category]
    return cmap, matches, already


def recategorize_preview(session: Session, field: str, text: str, category: str) -> dict:
    _, matches, already = _recategorize_rows(session, field, text, category)
    return {
        "matches": len(matches),
        "already": len(already),
        "manual": sum(1 for r in matches if r.category_source in rules.USER_SOURCES),
    }


def recategorize(
    session: Session, field: str, text: str, category: str, include_manual: bool,
    skip: Callable[[list[int]], set[int]] | None = None,
) -> int:
    """One-time change (no rule): the category is set by hand so later rule runs keep it.
    ``skip`` (Release 3.10) returns ids to leave alone (card payments can't go in "Paying off
    debt")."""
    _, matches, _ = _recategorize_rows(session, field, text, category)
    ids = [r.id for r in matches if include_manual or r.category_source not in rules.USER_SOURCES]
    if skip is not None and ids:
        left_out = skip(ids)
        ids = [i for i in ids if i not in left_out]
    for chunk in _chunks(ids):
        for txn in session.scalars(select(Transaction).where(Transaction.id.in_(chunk))):
            txn.category = category
            txn.category_source = "user"
            txn.rule_id = None
    session.flush()
    rules.release_hand_set(session)  # the user's category wins over the card payment mark
    return len(ids)


# ------------------------------------------------------------------ set several at once, Undo (Release 3.3)


def _load(session: Session, ids: list[int]) -> dict[int, Transaction]:
    """Every requested transaction by id; raises TxnNotFound if any is missing."""
    found: dict[int, Transaction] = {}
    for chunk in _chunks(ids):
        for txn in session.scalars(select(Transaction).where(Transaction.id.in_(chunk))):
            found[txn.id] = txn
    if len(found) != len(set(ids)):
        raise TxnNotFound("transaction not found")
    return found


def _split_ids(session: Session, ids: list[int]) -> set[int]:
    out: set[int] = set()
    for chunk in _chunks(ids):
        out.update(session.scalars(
            select(TransactionSplit.transaction_id).where(TransactionSplit.transaction_id.in_(chunk)).distinct()
        ))
    return out


def category_state(txn: Transaction) -> dict:
    return {"id": txn.id, "category": txn.category, "category_source": txn.category_source or "plaid"}


def _to_automatic(txn: Transaction) -> None:
    """Back to automatic; the caller re-applies the rules once afterwards."""
    txn.category_source = "plaid"
    txn.rule_id = None
    txn.category = txn.plaid_category


def bulk_set_category(
    session: Session, ids: list[int], category: str | None
) -> tuple[list[int], list[dict], list[int]]:
    """Set (or reset to automatic, with None) the category of several transactions.

    Validates everything before writing; runs inside the caller's transaction. Split
    transactions are skipped. Returns (changed ids, their previous states, skipped ids),
    each in request order. Raises TxnError (422) or TxnNotFound (404).
    """
    ids = list(dict.fromkeys(ids))
    if category is not None and session.get(TxnCategory, category) is None:
        raise TxnError("unknown category")
    rows = _load(session, ids)
    split = _split_ids(session, ids)
    skipped = [i for i in ids if i in split]
    targets = [rows[i] for i in ids if i not in split]
    previous = {t.id: category_state(t) for t in targets}
    if category is not None:
        # Already hand-set to this category: nothing to change.
        changing = [
            t for t in targets
            if t.category != category or t.category_source not in rules.USER_SOURCES or t.rule_id is not None
        ]
        source = "user_bulk" if len(changing) >= 2 else "user"
        for t in changing:
            t.category, t.category_source, t.rule_id = category, source, None
        session.flush()
        rules.release_hand_set(session)  # the user's category wins over the card payment mark
        changed = [t.id for t in changing]
    else:
        before = {t.id: (t.category, t.category_source, t.rule_id) for t in targets}
        for t in targets:
            _to_automatic(t)
        session.flush()
        rules.apply_all(session)
        changed = [t.id for t in targets if (t.category, t.category_source, t.rule_id) != before[t.id]]
    return changed, [previous[i] for i in changed], skipped


def restore_categories(session: Session, items: list[tuple[int, str | None, str]]) -> list[int]:
    """Undo: put back (id, category, category_source) states. Returns the restored ids.

    Hand-set states come back exactly; ``plaid``/``rule`` states go back to automatic (the
    rules decide ``rule_id`` again, so a client can never set it). Split transactions are
    skipped. Validates everything before writing; raises TxnError (422) or TxnNotFound (404).
    """
    ids = [i for i, _, _ in items]
    if len(set(ids)) != len(ids):
        raise TxnError("Each transaction can appear only once.")
    rows = _load(session, ids)
    wanted = {c for _, c, source in items if source in rules.USER_SOURCES and c is not None}
    known: set[str] = set()
    for chunk in _chunks(sorted(wanted)):
        known.update(session.scalars(select(TxnCategory.id).where(TxnCategory.id.in_(chunk))))
    for _, category, source in items:
        if source in rules.USER_SOURCES and (category is None or category not in known):
            raise TxnError("unknown category")
    # Release 3.10: a credit card payment never goes in "Paying off debt", Undo included.
    blocked = debt_budget.blocked_category(session)
    filed = [i for i, category, source in items if source in rules.USER_SOURCES and blocked is not None and category == blocked]
    if filed and debt_budget.card_payment_ids(session, filed):
        raise TxnError(debt_budget.CARD_PAYMENT)
    split = _split_ids(session, ids)
    # The transfer mark filing under "Paying off debt" cleared comes back with the Undo.
    marks = debt_budget.marks_to_put_back(session, rows, [it for it in items if it[0] not in split])
    restored: list[int] = []
    automatic = False
    for txn_id, category, source in items:
        if txn_id in split:
            continue
        txn = rows[txn_id]
        if source in rules.USER_SOURCES:
            txn.category, txn.category_source, txn.rule_id = category, source, None
        else:
            _to_automatic(txn)
            automatic = True
        if txn_id in marks:
            txn.is_transfer, txn.transfer_source = True, marks[txn_id]
        restored.append(txn_id)
    session.flush()
    if automatic:
        rules.apply_all(session)
    else:
        rules.release_hand_set(session)
    return restored


def related(session: Session, txn: Transaction) -> dict:
    """Every transaction from the same merchant (or with the same name when there's no merchant)."""
    merchant = (txn.merchant_name or "").strip()
    field = "merchant" if merchant else "name"
    text = merchant or (txn.name or "").strip()
    column = Transaction.merchant_name if field == "merchant" else Transaction.name
    needle = _norm(text)
    # Same exact-after-trim-and-casefold match as the bulk recategorize.
    rows = session.execute(
        select(column.label("value"), Transaction.amount_cents, Transaction.date).where(column.is_not(None))
    ).all()
    matching = [r for r in rows if _norm(r.value) == needle]
    return {
        "field": field,
        "text": text,
        "count": len(matching),
        "total": from_cents(sum(int(r.amount_cents) for r in matching)),
        "first_date": min(r.date for r in matching).isoformat() if matching else None,
    }


# ------------------------------------------------------------------ tags


MAX_TAGS = 1000


def set_tags(session: Session, txn: Transaction, tag_ids: list[int]) -> None:
    wanted = list(dict.fromkeys(tag_ids))
    if len(wanted) > MAX_TAGS_PER_TXN:
        raise TxnError(f"A transaction can have at most {MAX_TAGS_PER_TXN} tags.")
    if wanted:
        found = set(session.scalars(select(Tag.id).where(Tag.id.in_(wanted))))
        if len(found) != len(wanted):
            raise TxnError("Unknown tag.")
    session.execute(delete(TransactionTag).where(TransactionTag.transaction_id == txn.id))
    for tag_id in wanted:
        session.add(TransactionTag(transaction_id=txn.id, tag_id=tag_id))
    session.flush()


def tag_spending(
    session: Session, cmap: CategoryMap, start: dt.date | None = None, end: dt.date | None = None
) -> dict[int, int]:
    """Spending per tag (split-aware, Release 2 spending definition): max(0, -Σ amount)."""
    from .spending import spending_lines  # local: spending imports this module's neighbours

    start = start or dt.date(1, 1, 1)
    end = end or dt.date(9999, 12, 31)
    tagged = select(TransactionTag.transaction_id)
    net: dict[int, int] = defaultdict(int)
    by_txn: dict[int, int] = defaultdict(int)
    for row in spending_lines(session, start, end, Transaction.id.in_(tagged)):
        if cmap.kind(row.category) == "spending":
            by_txn[row.txn_id] += row.amount_cents
    if not by_txn:
        return {}
    for chunk in _chunks(list(by_txn)):
        for txn_id, tag_id in session.execute(
            select(TransactionTag.transaction_id, TransactionTag.tag_id).where(TransactionTag.transaction_id.in_(chunk))
        ):
            net[tag_id] += by_txn[txn_id]
    return {tag_id: max(0, -cents) for tag_id, cents in net.items()}


def tags_out(session: Session) -> list[dict]:
    cmap = load_categories(session)
    counts = dict(session.execute(
        select(TransactionTag.tag_id, func.count()).group_by(TransactionTag.tag_id)
    ).all())
    spent = tag_spending(session, cmap)
    tags = sorted(session.scalars(select(Tag)), key=lambda t: (t.name.casefold(), t.id))
    return [tag_out(t, counts.get(t.id, 0), spent.get(t.id, 0)) for t in tags]


def tag_out(tag: Tag, count: int = 0, spent_cents: int = 0) -> dict:
    return {"id": tag.id, "name": tag.name, "hue": int(tag.hue), "count": int(count), "spent": from_cents(spent_cents)}
