from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .models import Account, Holding, PlaidItem, Tag, Transaction, TransactionSplit
from .services.categories import CategoryMap
from .services.categorize import categorize
from .utils import LIABILITY_CATEGORIES, from_cents, iso_utc, to_cents

Category = Literal["bank", "hsa", "retirement", "investment", "loan", "credit", "other"]
ItemKind = Literal["bank", "investment", "loan"]
Money = float  # dollars in requests/responses; cents in the DB

_MAX_MONEY = 1_000_000_000_000


class StrictModel(BaseModel):
    # Reject unknown fields so clients can't smuggle e.g. `source` or `item_id`.
    model_config = ConfigDict(extra="forbid")


# A recurring item's dates: far enough either way for any real bill, and nowhere near
# date.max, so schedule arithmetic (next date + 400 days...) can't overflow.
SANE_DATE_MIN, SANE_DATE_MAX = dt.date(1900, 1, 1), dt.date(9000, 12, 31)


def _sane_date(value: dt.date) -> dt.date:
    if not SANE_DATE_MIN <= value <= SANE_DATE_MAX:
        raise ValueError("Use a date between 1900 and 9000.")
    return value


SaneDate = Annotated[dt.date, AfterValidator(_sane_date)]


# ---------------------------------------------------------------- requests


class PasswordBody(StrictModel):
    password: str


class ChangePasswordBody(StrictModel):
    current_password: str
    new_password: str


# Recovery sheet. The code is checked by the vault (ASCII digits, spaces, dashes); these caps
# only bound the input. Validation errors never echo it (main._validation_message).
class RecoveryCodeBody(StrictModel):
    code: str = Field(max_length=128)


class RecoverBody(StrictModel):
    code: str = Field(max_length=128)
    new_password: str = Field(max_length=4096)


class PendingBody(StrictModel):
    pending_id: str = Field(min_length=1, max_length=64)


class ConfirmSheetBody(StrictModel):
    sheet: str = Field(pattern=r"^[0-9]{3}-[0-9]{3}$")  # ASCII digits only


class AccountCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    category: Category
    current_balance: Money = Field(ge=-_MAX_MONEY, le=_MAX_MONEY)
    institution_name: str | None = Field(default=None, max_length=200)
    interest_rate: float | None = Field(default=None, ge=0, le=1000)
    minimum_payment: Money | None = Field(default=None, ge=0, le=_MAX_MONEY)
    next_payment_due: dt.date | None = None
    notes: str | None = Field(default=None, max_length=5000)


class AccountPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: Category | None = None
    hidden: bool | None = None
    notes: str | None = Field(default=None, max_length=5000)
    interest_rate: float | None = Field(default=None, ge=0, le=1000)
    minimum_payment: Money | None = Field(default=None, ge=0, le=_MAX_MONEY)
    next_payment_due: dt.date | None = None
    current_balance: Money | None = Field(default=None, ge=-_MAX_MONEY, le=_MAX_MONEY)
    # Release 3: liabilities only; null resets to the automatic group.
    loan_group: str | None = Field(default=None, max_length=40)


class LinkTokenBody(StrictModel):
    kind: ItemKind
    item_id: int | None = None


class ExchangeBody(StrictModel):
    public_token: str = Field(min_length=1, max_length=500)
    kind: ItemKind


class SyncBody(StrictModel):
    item_id: int | None = None


# ---------------------------------------------------------------- responses


LOAN_GROUP_CREDIT = "Credit cards"
LOAN_GROUP_OTHER = "Other loans"
_AUTO_LOAN_GROUPS = {
    "auto": "Car loans",
    "student": "Student loans",
    "mortgage": "Home loans",
    "home equity": "Home loans",
}


def loan_group(a: Account) -> str | None:
    """Effective group of a liability: the custom label, else the automatic one; None for assets."""
    if a.category not in LIABILITY_CATEGORIES:
        return None
    if a.loan_group:
        return a.loan_group
    if a.category == "credit":
        return LOAN_GROUP_CREDIT
    return _AUTO_LOAN_GROUPS.get((a.plaid_subtype or "").strip().lower(), LOAN_GROUP_OTHER)


def account_out(a: Account) -> dict:
    return {
        "id": a.id,
        "source": a.source,
        "item_id": a.item_id,
        "name": a.name,
        "official_name": a.official_name,
        "mask": a.mask,
        "institution_name": a.institution_name,
        "category": a.category,
        "plaid_type": a.plaid_type,
        "plaid_subtype": a.plaid_subtype,
        "current_balance": from_cents(a.current_balance_cents),
        "available_balance": from_cents(a.available_balance_cents),
        "currency": a.currency,
        "interest_rate": a.interest_rate,
        "minimum_payment": from_cents(a.minimum_payment_cents),
        "next_payment_due": a.next_payment_due.isoformat() if a.next_payment_due else None,
        "notes": a.notes,
        "hidden": bool(a.hidden),
        "is_liability": a.category in LIABILITY_CATEGORIES,
        "updated_at": iso_utc(a.updated_at),
        "loan_group": loan_group(a),
    }


def item_out(item: PlaidItem, account_count: int) -> dict:
    # Deliberately built field by field: the access token must never leave the DB.
    return {
        "id": item.id,
        "institution_name": item.institution_name,
        "kind": item.kind,
        "status": item.status,
        "error_code": item.error_code,
        "last_synced_at": iso_utc(item.last_synced_at),
        "account_count": account_count,
    }


def transaction_out(t: Transaction, account_name: str) -> dict:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "account_name": account_name,
        "date": t.date.isoformat(),
        "name": t.name,
        "merchant_name": t.merchant_name,
        "amount": from_cents(t.amount_cents),
        "category": t.category,
        "pending": bool(t.pending),
    }


def holding_out(h: Holding, account_name: str) -> dict:
    return {
        "id": h.id,
        "account_id": h.account_id,
        "account_name": account_name,
        "name": h.name,
        "ticker": h.ticker,
        "quantity": h.quantity,
        "price": h.price,
        "value": from_cents(h.value_cents),
        "cost_basis": from_cents(h.cost_basis_cents),
    }


# ---------------------------------------------------------------- Release 2 requests

CategoryKind = Literal["spending", "income", "transfer", "fixed"]
Cadence = Literal["once", "weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly"]
RecurringStatus = Literal["suggested", "active", "dismissed"]
ReminderDays = Literal[0, 1, 3]


class ImportBody(StrictModel):
    plaid_account_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=500
    )


class CategoryCreate(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    hue: int | None = Field(default=None, ge=0, le=359)
    kind: CategoryKind
    # Settings (D7): straight into a group (null or omitted = ungrouped).
    group_id: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None


class CategoryPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    hue: int | None = Field(default=None, ge=0, le=359)
    kind: CategoryKind | None = None
    hidden: bool | None = None


class TransactionPatch(StrictModel):
    category: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=5000)
    is_transfer: bool | None = None


class BudgetSave(StrictModel):
    # Amounts are range- and finiteness-checked by the service, which answers with a plain detail.
    assigned: Annotated[dict[Annotated[str, Field(max_length=64)], Money], Field(max_length=500)] | None = None
    removed: Annotated[list[Annotated[str, Field(max_length=64)]], Field(max_length=500)] | None = None
    # Release 3.17: the one-time part (Move money, Cover it) of each listed category's
    # ``assigned``; every key must be in ``assigned`` too (the service says so in plain words).
    moved: Annotated[dict[Annotated[str, Field(max_length=64)], Money], Field(max_length=500)] | None = None
    # Release 3.6: this month's expected income (null = back to the suggestion); only for
    # today's month with expected income on. ``accept_received``: "Leave it for next month".
    # ``restore``: Undo putting back an earlier state, so ``income_expected`` may be below what
    # already came in (every other rule still applies). Strict: no "5000" strings or "yes" bools.
    income_expected: Annotated[float, Field(ge=0, le=_MAX_MONEY, allow_inf_nan=False, strict=True)] | None = None
    accept_received: Annotated[bool, Field(strict=True)] | None = None
    restore: Annotated[bool, Field(strict=True)] | None = None


class BudgetAccountsBody(StrictModel):
    account_ids: list[int] = Field(max_length=10000)


class RecurringCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=200)
    amount: Money = Field(ge=-_MAX_MONEY, le=_MAX_MONEY)
    cadence: Cadence
    next_date: SaneDate
    account_id: int | None = None
    # Release 3.6
    reminder_days: ReminderDays = 0
    # Omitted: guessed from the merchant's past transactions. null: no category.
    category_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class RecurringPatch(StrictModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Money | None = Field(default=None, ge=-_MAX_MONEY, le=_MAX_MONEY)
    cadence: Cadence | None = None
    next_date: SaneDate | None = None
    account_id: int | None = None
    status: RecurringStatus | None = None
    include_in_forecast: bool | None = None
    # Release 3.6
    reminder_days: ReminderDays | None = None
    category_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class ForecastAccountBody(StrictModel):
    account_id: int


class RuleInput(StrictModel):
    field: Literal["any", "merchant", "name"]
    op: Literal["contains", "is"]
    text: str = Field(min_length=1, max_length=200)
    amount_op: Literal["gt", "lt"] | None = None
    amount: Money | None = Field(default=None, ge=0, le=_MAX_MONEY)
    action: Literal["category", "transfer"]
    category: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    # Create only: put the new rule ahead of every existing one (same as position 0).
    first: bool = False
    # Settings (D7): insert at this position (0 = first), shifting the rest; beyond the end
    # = last (the default). Ignored by /preview.
    position: Annotated[int, Field(ge=0, le=100_000)] | None = None


class RulePreviewBody(RuleInput):
    """Settings D7 update 2: ``rule_id`` = previewing an edit of that rule (at its position)."""

    rule_id: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None


class RulePatch(StrictModel):
    field: Literal["any", "merchant", "name"] | None = None
    op: Literal["contains", "is"] | None = None
    text: str | None = Field(default=None, min_length=1, max_length=200)
    amount_op: Literal["gt", "lt"] | None = None
    amount: Money | None = Field(default=None, ge=0, le=_MAX_MONEY)
    action: Literal["category", "transfer"] | None = None
    category: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class ReorderBody(StrictModel):
    ids: list[int] = Field(max_length=10000)


class AlertSettingPatch(StrictModel):
    enabled: bool | None = None
    value: float | None = None


class AlertSettingsBody(StrictModel):
    """Settings D7 update 2: ``PUT /api/alerts/settings`` (every row optional)."""

    low: AlertSettingPatch | None = None
    big: AlertSettingPatch | None = None
    budget: AlertSettingPatch | None = None
    reminder: AlertSettingPatch | None = None
    newrec: AlertSettingPatch | None = None
    # Release 3.19: "When an amount you set changes" (asks about new prices of hand-set items).
    price: AlertSettingPatch | None = None


def non_null(body: BaseModel, names: tuple[str, ...]) -> str | None:
    """The first of ``names`` explicitly sent as null, if any (for PATCH bodies)."""
    for name in names:
        if name in body.model_fields_set and getattr(body, name) is None:
            return name
    return None


# ---------------------------------------------------------------- Release 2 responses


def transaction_out_r2(
    t: Transaction,
    account_name: str,
    cmap: CategoryMap,
    splits: list[TransactionSplit] | tuple = (),
    tags: list[Tag] | tuple = (),
) -> dict:
    info = cmap.get(t.category)
    return {
        **transaction_out(t, account_name),
        "category_name": info.name,
        "category_hue": info.hue,
        "plaid_category": t.plaid_category,
        "category_source": t.category_source or "plaid",
        "rule_id": t.rule_id,
        "notes": t.notes,
        "is_transfer": bool(t.is_transfer),
        "splits": [split_out(s, cmap) for s in splits],
        "tags": [tag_ref(tag) for tag in tags],
    }


def split_out(s: TransactionSplit, cmap: CategoryMap) -> dict:
    info = cmap.get(s.category)
    return {
        "id": s.id,
        "amount": from_cents(s.amount_cents),
        "category": s.category,
        "category_name": info.name,
        "category_hue": info.hue,
        "notes": s.notes,
    }


def tag_ref(tag: Tag) -> dict:
    return {"id": tag.id, "name": tag.name, "hue": int(tag.hue)}


# ---------------------------------------------------------------- Release 3 requests


class StrictModel3(BaseModel):
    """Release 3 bodies: unknown fields and NaN/Infinity are rejected (422)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


PositiveMoney = Annotated[float, Field(gt=0, le=_MAX_MONEY, allow_inf_nan=False)]
NonNegMoney = Annotated[float, Field(ge=0, le=_MAX_MONEY, allow_inf_nan=False)]
Id = Annotated[int, Field(ge=1, le=2**63 - 1)]

MAX_SPLITS = 20
MAX_TAGS_PER_TXN = 20
GROUP_NAME_MAX = 60
TAG_NAME_MAX = 40


class BudgetRowState(StrictModel3):
    """One ``budgets`` row as it was (Release 3.14 exact Undo): ``assigned`` in dollars."""

    assigned: Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False, strict=True)]
    removed: Annotated[bool, Field(strict=True)]
    restart: Annotated[bool, Field(strict=True)]
    # Release 3.17: the one-time part of ``assigned`` (states read before it have none: 0).
    moved: Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False, strict=True)] = 0


class BudgetRowRestore(StrictModel3):
    """One row to put back: ``state`` null = the month had no row; ``expected`` = the row right
    after the change being undone (null = no row)."""

    category: Annotated[str, Field(min_length=1, max_length=64)]
    state: BudgetRowState | None
    expected: BudgetRowState | None


class BudgetRowsRestore(StrictModel3):
    """``PUT /api/budgets/{month}/rows``: all or nothing; each category at most once."""

    rows: Annotated[list[BudgetRowRestore], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def _unique(self) -> BudgetRowsRestore:
        if len({r.category for r in self.rows}) != len(self.rows):
            raise ValueError("each category can appear only once")
        return self


class TargetBody(StrictModel3):
    kind: Literal["monthly", "by_date", "bills"]
    # Required for monthly / by_date; a ``bills`` target has none (it follows the month's
    # bills) and may only echo ``amount: null`` (the router says so in plain words).
    amount: PositiveMoney | None = None
    # by_date only; monthly and bills targets may echo ``date: null``.
    date: dt.date | None = None


class CategoryPatch3(CategoryPatch):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    group_id: Id | None = None
    target: TargetBody | None = None
    # Undo putting back an earlier ``target``: a by-date target's date may be in the past.
    restore: Annotated[bool, Field(strict=True)] | None = None


class GroupCreate(StrictModel3):
    name: str = Field(min_length=1, max_length=GROUP_NAME_MAX)


class GroupReorder(StrictModel3):
    ids: list[Id] = Field(max_length=1000)


class SplitPart(StrictModel3):
    amount: Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False)]
    category: str = Field(min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=500)


class SplitsBody(StrictModel3):
    splits: list[SplitPart] = Field(max_length=MAX_SPLITS)


class RecategorizeBody(StrictModel3):
    field: Literal["merchant", "name"]
    text: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=64)


class RecategorizeApply(RecategorizeBody):
    include_manual: bool = False


class TagCreate(StrictModel3):
    name: str = Field(min_length=1, max_length=TAG_NAME_MAX)
    hue: int | None = Field(default=None, ge=0, le=359)


class TagPatch(StrictModel3):
    name: str | None = Field(default=None, min_length=1, max_length=TAG_NAME_MAX)
    hue: int | None = Field(default=None, ge=0, le=359)


class TxnTagsBody(StrictModel3):
    tag_ids: list[Id] = Field(max_length=MAX_TAGS_PER_TXN)


class DebtLump(StrictModel3):
    """Release 3.10: a one-time payment (Try it), on ``account_id`` first, else plan order."""

    amount: PositiveMoney
    account_id: Id | None = None


class DebtPlanBody(StrictModel3):
    extra: NonNegMoney = 0.0
    strategy: Literal["avalanche", "snowball", "custom"]
    order: list[Id] | None = Field(default=None, max_length=100)
    account_ids: list[Id] | None = Field(default=None, max_length=100)
    lump: DebtLump | None = None


class AutoBackupBody(StrictModel3):
    dir: str | None = Field(max_length=1000)
    keep: int = Field(ge=1, le=100)
    # Settings (D7): make the folder first when it doesn't exist ("Turn on" with the
    # suggested folder); the usual checks still apply to the result.
    create: Annotated[bool, Field(strict=True)] = False


class AutoSyncBody(StrictModel3):
    hours: Literal[0, 3, 6, 12, 24]


# ---------------------------------------------------------------- Release 3.3 (Transactions redesign)

TxnView = Literal["all", "needs_category", "in", "out"]
CategorySource = Literal["plaid", "auto", "rule", "user", "user_bulk"]
MAX_BULK = 1000
CategoryId = Annotated[str, Field(min_length=1, max_length=64)]


class BulkCategoryBody(StrictModel3):
    ids: list[Id] = Field(min_length=1, max_length=MAX_BULK)
    # Required; null resets the transactions to automatic (rules, then Plaid).
    category: CategoryId | None


class RestoreItem(StrictModel3):
    id: Id
    category: Annotated[str, Field(max_length=64)] | None
    category_source: CategorySource


class RestoreBody(StrictModel3):
    items: list[RestoreItem] = Field(min_length=1, max_length=MAX_BULK)


# ---------------------------------------------------------------- Release 3.4 (Plaid keys in the vault)

PlaidEnv = Literal["sandbox", "production"]
# Plaid's client IDs are 24 hex characters and secrets 30; accept any plausible length.
_PLAID_KEY_RE = re.compile(r"[A-Za-z0-9]{16,64}")


def _plaid_key(value: Any, what: str) -> Any:
    # Messages never include the value: it may be the secret (and the handler never echoes input).
    if value is None:
        return None
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    value = value.strip()
    if not _PLAID_KEY_RE.fullmatch(value):
        raise ValueError(f"{what} must be 16–64 letters and digits")
    return value


class PlaidKeysIn(StrictModel):
    client_id: str
    secret: SecretStr  # masked in repr/str; read only where it is used
    env: PlaidEnv

    @field_validator("client_id", mode="before")
    @classmethod
    def _client_id(cls, value: Any) -> Any:
        return _plaid_key(value, "client_id")

    @field_validator("secret", mode="before")
    @classmethod
    def _secret(cls, value: Any) -> Any:
        return _plaid_key(value, "secret")


class PlaidKeysSaveBody(PlaidKeysIn):
    current_password: str = Field(default="", max_length=4096)


class PlaidKeysTestBody(StrictModel):
    """Candidate keys (all three; ``env: "auto"`` detects it), or ``{}`` for the active keys."""

    client_id: str | None = None
    secret: SecretStr | None = None
    env: Literal["sandbox", "production", "auto"] | None = None

    @field_validator("client_id", mode="before")
    @classmethod
    def _client_id(cls, value: Any) -> Any:
        return _plaid_key(value, "client_id")

    @field_validator("secret", mode="before")
    @classmethod
    def _secret(cls, value: Any) -> Any:
        return _plaid_key(value, "secret")

    @model_validator(mode="after")
    def _all_or_none(self) -> PlaidKeysTestBody:
        given = [v is not None for v in (self.client_id, self.secret, self.env)]
        if any(given) and not all(given):
            raise ValueError("send client_id, secret and env together, or none of them")
        return self


class CurrentPasswordBody(StrictModel):
    current_password: str = Field(default="", max_length=4096)


# ---------------------------------------------------------------- Home "Today"


# ---------------------------------------------------------------- Release 3.6: Budget for a beginner

SETUP_KEYS = ("groceries", "gas", "eating_out", "bills", "fun", "other")
SetupKey = Literal["groceries", "gas", "eating_out", "bills", "fun", "other"]


class BudgetSettingsBody(StrictModel3):
    income_mode: Literal["expected", "off"] | None = None
    savings_category: str | None = Field(default=None, min_length=1, max_length=64)


class BudgetSetupBody(StrictModel3):
    answers: dict[SetupKey, NonNegMoney] = Field(default_factory=dict)
    income_expected: NonNegMoney
    keep_existing_in_savings: bool = True
    skip: bool = False


class BudgetCategoryBody(StrictModel3):
    name: str = Field(min_length=1, max_length=60)
    group_id: Id | None = None
    plan: NonNegMoney = 0.0


class EmptyBody(StrictModel3):
    """``{}``: a POST that takes no input (unknown fields 422)."""


# ---------------------------------------------------------------- Release 3.6 (cash forecast calendar)

MAX_OVERRIDES = 500


class ForecastSettingsBody(StrictModel3):
    include_daily: bool | None = None
    excluded_plans: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=200
    )


class OccurrenceBody(StrictModel3):
    """The whole override row; omitted fields take their defaults (all defaults = no override)."""

    moved_to: dt.date | None = None
    skipped: bool = False
    paid: bool = False
    paid_amount: PositiveMoney | None = None


class RescheduleBody(StrictModel3):
    from_base: dt.date
    to: dt.date


class SnapshotOverride(OccurrenceBody):
    base_date: SaneDate
    moved_to: SaneDate | None = None


class SnapshotItem(StrictModel3):
    id: Id
    name: str = Field(min_length=1, max_length=200)
    merchant_key: str = Field(min_length=1, max_length=120)
    account_id: Id | None = None
    amount: Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False)]
    cadence: Cadence
    next_date: SaneDate
    status: RecurringStatus
    include_in_forecast: bool
    source: Literal["detected", "manual"]
    last_seen_date: SaneDate | None = None
    reminder_days: ReminderDays = 0
    start_date: SaneDate | None = None
    anchor_days: list[Annotated[int, Field(ge=1, le=31)]] | None = Field(default=None, min_length=1, max_length=2)
    category_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    created_at: dt.datetime
    # Read-only fields of RecurringItem, accepted so a snapshot can be sent back as it came (ignored).
    account_name: str | None = Field(default=None, max_length=1000)
    category_name: str | None = Field(default=None, max_length=1000)


class CategoryGuessBody(StrictModel3):
    """The Add dialog's category pre-fill (a body, so the typed name stays out of URLs)."""

    name: str = Field(min_length=1, max_length=200)
    # Only money in (1) vs out (-1) matters, so the amount itself isn't sent.
    amount_sign: Literal[-1, 1] | None = None
    account_id: Id | None = None


class SnapshotBody(StrictModel3):
    item: SnapshotItem
    overrides: list[SnapshotOverride] = Field(default_factory=list, max_length=MAX_OVERRIDES)


class PriceAnswerBody(StrictModel3):
    """Release 3.19, "Netflix now charges $17.99. Update your amount?": ``no`` keeps their amount
    (Yes is ``PATCH /api/recurring/{id}`` with the new amount); ``undo`` asks again."""

    transaction_id: Id
    answer: Literal["no", "undo"]


class DismissBody(StrictModel3):
    # What the alert looked like when dismissed ("t9812", an event id, an ISO time...).
    fingerprint: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9:._-]+$")  # Rust regex: $ is end of text


def discovered_account_out(pa: dict, imported: bool) -> dict:
    def enum(value: object) -> str | None:
        return None if value is None else str(getattr(value, "value", value))

    ptype, psub = enum(pa.get("type")), enum(pa.get("subtype"))
    balances = pa.get("balances") or {}
    current = balances.get("current")
    if current is None:
        current = balances.get("available")
    category = categorize(ptype, psub)
    return {
        "plaid_account_id": pa.get("account_id"),
        "name": pa.get("name") or pa.get("official_name") or "Account",
        "official_name": pa.get("official_name"),
        "mask": pa.get("mask"),
        "category": category,
        "plaid_type": ptype,
        "plaid_subtype": psub,
        "current_balance": from_cents(to_cents(current)) if current is not None else 0.0,
        "is_liability": category in LIABILITY_CATEGORIES,
        "imported": bool(imported),
    }


# ---------------------------------------------------------------- Goals (D8, Release 3.8)

GoalKind = Literal["emergency", "save"]
MonthStr = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$", max_length=7)]  # Rust regex
GOAL_NAME_MAX = 60
# Strict numbers: "600" and true are refused (422), not read as 600 and 1.
GoalMoney = Annotated[float, Field(gt=0, le=_MAX_MONEY, allow_inf_nan=False, strict=True)]
GoalMoney0 = Annotated[float, Field(ge=0, le=_MAX_MONEY, allow_inf_nan=False, strict=True)]
SignedGoalMoney = Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False, strict=True)]
GoalId = Annotated[int, Field(ge=1, le=2**63 - 1, strict=True)]


class GoalCreateBody(StrictModel3):
    kind: GoalKind
    name: str = Field(min_length=1, max_length=GOAL_NAME_MAX)
    target: GoalMoney
    due_month: MonthStr | None = None
    monthly: GoalMoney0
    already_saved: GoalMoney0 = 0.0
    account_id: GoalId | None = None


class GoalPlanBefore(StrictModel3):
    """The page's Undo of an edit: this month's plan as it was before (``month`` = that month)."""

    month: MonthStr
    planned: SignedGoalMoney


class GoalPatchBody(StrictModel3):
    name: str | None = Field(default=None, min_length=1, max_length=GOAL_NAME_MAX)
    target: GoalMoney | None = None
    due_month: MonthStr | None = None
    monthly: GoalMoney0 | None = None
    account_id: GoalId | None = None
    # Only on the PATCH that links an old goal to the Budget.
    already_saved: GoalMoney0 | None = None
    # Only on the page's Undo of an edit: puts this month's plan back exactly.
    plan_before: GoalPlanBefore | None = None


class GoalUndoGoal(StrictModel3):
    kind: GoalKind
    name: str = Field(min_length=1, max_length=200)  # an old goal (not in the Budget) may have a longer name
    target: GoalMoney
    due_month: MonthStr | None = None
    monthly: GoalMoney0
    account_id: GoalId | None = None


class GoalRestoreBody(StrictModel3):
    """The ``undo`` a DELETE returned, sent back as it was."""

    goal: GoalUndoGoal
    category_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    month: MonthStr
    assigned: SignedGoalMoney
    released: SignedGoalMoney = 0.0
    seed_month: MonthStr | None = None
    removed: Annotated[bool, Field(strict=True)] | None = None
    # Names the server's own record of the delete (restore uses that record, once).
    token: Annotated[str, Field(min_length=1, max_length=64)]


# ---------------------------------------------------------------- Settings redesign (D7)

AUTO_LOCK_CHOICES = (5, 15, 30, 60)
MAX_RESTORE_IDS = 50_000


class SettingsPatch(StrictModel3):
    auto_lock_minutes: Literal[5, 15, 30, 60] | None = None
    paycheck_notify: Annotated[bool, Field(strict=True)] | None = None


class SupportContactBody(StrictModel3):
    """Settings > Safety and backups > Who to call for help. "" = no one named. Trimmed; at
    most SUPPORT_CONTACT_MAX characters and no control characters (422 otherwise; the
    error never repeats the value)."""

    contact: Annotated[str, Field(strict=True, max_length=200)]

    @field_validator("contact")
    @classmethod
    def _check(cls, value: str) -> str:
        from .services.support_contact import ContactError, check

        try:
            return check(value)
        except ContactError as exc:
            raise ValueError(str(exc)) from None


class SnapshotCategory(StrictModel3):
    # Only custom categories can be deleted (and so restored): ids are c_<n>, never reused.
    id: str = Field(min_length=3, max_length=64, pattern=r"^c_[0-9]{1,18}$")
    name: str = Field(min_length=1, max_length=60)
    hue: int = Field(ge=0, le=359)
    kind: CategoryKind
    custom: Literal[True]
    hidden: bool
    position: int = Field(ge=-(2**31), le=2**31 - 1)
    group_id: Id | None = None
    target: TargetBody | None = None


class SnapshotHandSet(StrictModel3):
    transaction_id: Id
    source: Literal["user", "user_bulk"]


class SnapshotBudget(StrictModel3):
    month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    assigned: Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False)]
    removed: bool = False
    restart: bool = False
    # Release 3.17: the one-time part of ``assigned`` (snapshots taken before it have none).
    moved: Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False)] = 0


class SnapshotRule(StrictModel3):
    """A ``Rule`` as ``GET /api/rules`` shows it (``matches`` is accepted and ignored)."""

    id: Id
    position: int = Field(ge=0, le=100_000)
    field: Literal["any", "merchant", "name"]
    op: Literal["contains", "is"]
    text: str = Field(min_length=1, max_length=200)
    amount_op: Literal["gt", "lt"] | None = None
    amount: NonNegMoney | None = None
    action: Literal["category"]
    category: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    matches: int | None = Field(default=None, ge=0, le=2**63 - 1)


class SnapshotFlags(StrictModel3):
    savings_category: bool = False
    auto_map_key: Literal["groceries", "eating_out", "gas"] | None = None
    excluded_plan: bool = False


class CategorySnapshot(StrictModel3):
    category: SnapshotCategory
    hand_set: list[SnapshotHandSet] = Field(default_factory=list, max_length=MAX_RESTORE_IDS)
    splits: list[Id] = Field(default_factory=list, max_length=MAX_RESTORE_IDS)
    budgets: list[SnapshotBudget] = Field(default_factory=list, max_length=MAX_RESTORE_IDS)
    bills: list[Id] = Field(default_factory=list, max_length=MAX_RESTORE_IDS)
    rules: list[SnapshotRule] = Field(default_factory=list, max_length=10_000)
    flags: SnapshotFlags = Field(default_factory=SnapshotFlags)


class GroupRestoreBody(StrictModel3):
    id: Id
    name: str = Field(min_length=1, max_length=GROUP_NAME_MAX)
    position: int = Field(ge=0, le=100_000)
    category_ids: list[CategoryId] = Field(default_factory=list, max_length=MAX_RESTORE_IDS)


# ---------------------------------------------------------------- Release 3.10 (Paying off debt in the Budget)


class DebtBudgetBody(StrictModel3):
    """``PUT /api/debt/budget``: the extra a month for paying off debt, and the plan it came from."""

    extra: PositiveMoney
    strategy: Literal["avalanche", "snowball"]
    account_ids: list[Id] = Field(min_length=1, max_length=100)


class DebtUndoBody(StrictModel3):
    token: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------- Release 3.10 Accounts (pair 1)

AccountKind = Literal["checking", "savings", "credit_card", "student", "auto", "other_loan", "investment", "other"]
AnyMoney = Annotated[float, Field(ge=-_MAX_MONEY, le=_MAX_MONEY, allow_inf_nan=False)]


class AccountCreate310(StrictModel3):
    """``POST /api/accounts``: ``kind`` (the add-account form) or ``category`` (older clients);
    when both are sent they must agree. Debts are stored as the positive amount owed."""

    name: str = Field(min_length=1, max_length=200)
    kind: AccountKind | None = None
    category: Category | None = None
    current_balance: AnyMoney
    institution_name: str | None = Field(default=None, max_length=200)
    mask: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    interest_rate: float | None = Field(default=None, ge=0, le=1000)
    minimum_payment: NonNegMoney | None = None
    next_payment_due: SaneDate | None = None
    notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _kind_or_category(self) -> "AccountCreate310":
        if self.kind is None and self.category is None:
            raise ValueError("Pick what kind of account this is.")
        return self


class AccountBalanceBody(StrictModel3):
    balance: AnyMoney


class ItemsLinkedBody(StrictModel3):
    """``PUT /api/plaid/items-linked``: bank connections used, from Plaid's dashboard (a whole
    number; the route also refuses less than the connections there are now)."""

    count: int = Field(ge=0, le=10, strict=True)


class ItemsCapBody(StrictModel3):
    """``PUT /api/plaid/items-cap`` (Iron Owl 2.0.0): count bank connections against Plaid's
    Trial limit of 10, or not."""

    on: bool = Field(strict=True)


class AccountUndoBody(StrictModel3):
    token: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
