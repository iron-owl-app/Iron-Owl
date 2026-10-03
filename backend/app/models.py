from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .utils import utcnow


class Base(DeclarativeBase):
    pass


class PlaidItem(Base):
    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    plaid_item_id: Mapped[str] = mapped_column(String, unique=True)
    access_token: Mapped[str] = mapped_column(String)
    institution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    institution_name: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)  # bank | investment | loan
    transactions_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ok")  # ok | login_required | error | pending
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    # JSON list of Plaid account_ids the user chose not to import (Release 2).
    excluded_account_ids: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    # When the last sync attempt failed (v5); Home's bank-error "Not now" resets on a new one.
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    accounts: Mapped[list[Account]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # never include the access token
        return f"<PlaidItem id={self.id} kind={self.kind} status={self.status}>"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("plaid_items.id", ondelete="CASCADE"), nullable=True, index=True
    )
    plaid_account_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    source: Mapped[str] = mapped_column(String)  # plaid | manual
    name: Mapped[str] = mapped_column(String)
    official_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mask: Mapped[str | None] = mapped_column(String, nullable=True)
    institution_name: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str] = mapped_column(String)
    plaid_type: Mapped[str | None] = mapped_column(String, nullable=True)
    plaid_subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    current_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    available_balance_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="USD")
    interest_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    minimum_payment_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_payment_due: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    # Release 3: custom group label for liabilities; NULL = the automatic group.
    loan_group: Mapped[str | None] = mapped_column(Text, nullable=True)

    item: Mapped[PlaidItem | None] = relationship(back_populates="accounts")
    snapshots: Mapped[list[BalanceSnapshot]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    holdings: Mapped[list[Holding]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "date", name="uq_snapshot_account_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[dt.date] = mapped_column(Date)
    balance_cents: Mapped[int] = mapped_column(Integer)
    # Release 3: reconstructed from transactions (backfill), not a recorded balance.
    estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    account: Mapped[Account] = relationship(back_populates="snapshots")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    plaid_transaction_id: Mapped[str] = mapped_column(String, unique=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String)
    merchant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Our convention: negative = money out, positive = money in (Plaid's is the opposite).
    amount_cents: Mapped[int] = mapped_column(Integer)
    # Effective category id (user > first matching rule > plaid_category); NULL counts as OTHER.
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    pending: Mapped[bool] = mapped_column(Boolean, default=False)
    # Release 2
    plaid_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_source: Mapped[str] = mapped_column(Text, default="plaid", server_default="plaid")  # plaid|auto|rule|user|user_bulk
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    transfer_source: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL | rule | user | auto (card payment, R3.9.1)
    # Release 3.6 phase 2 (schema v7): Plaid's detailed personal finance category
    # (e.g. FOOD_AND_DRINK_GROCERIES), stored from v7 on; NULL for older rows and manual ones.
    plaid_detailed: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[Account] = relationship(back_populates="transactions")


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    security_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    ticker: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_cents: Mapped[int] = mapped_column(Integer)
    cost_basis_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    account: Mapped[Account] = relationship(back_populates="holdings")


# ------------------------------------------------------------------ Release 2


class TxnCategory(Base):
    """Transaction category (not the account category). Seeded ids are Plaid PFC primaries."""

    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    hue: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)  # spending | income | transfer | fixed
    custom: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Release 3
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("category_groups.id", ondelete="SET NULL"), nullable=True
    )
    target_kind: Mapped[str | None] = mapped_column(Text, nullable=True)  # monthly | by_date
    target_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class BudgetMonth(Base):
    """Release 2 "spending money" pool. Unused since Release 2.1 (envelopes); kept for old data."""

    __tablename__ = "budget_months"

    month: Mapped[str] = mapped_column(String, primary_key=True)  # YYYY-MM
    pool_cents: Mapped[int] = mapped_column(Integer)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("month", "category", name="uq_budget_month_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String, index=True)
    category: Mapped[str] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    # Release 2.1: the amount *assigned* to the envelope this month (may be negative: money
    # moved out of the leftover). The latest row at or before a month decides membership.
    limit_cents: Mapped[int] = mapped_column(Integer)
    removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Re-added in the month it was removed: the envelope starts with no carryover, so the
    # leftover that removing released can't come back.
    restart: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Release 3.17 (migration v8): the part of ``limit_cents`` that came from one-time moves
    # (Move money, Cover it). The plan that repeats into later months is limit_cents - moved_cents.
    moved_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class RecurringItem(Base):
    __tablename__ = "recurring_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    merchant_key: Mapped[str] = mapped_column(String, index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer)  # signed: + in, - out
    cadence: Mapped[str] = mapped_column(String)
    next_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="suggested")  # suggested|active|dismissed
    include_in_forecast: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String, default="detected")  # detected | manual
    last_seen_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # JSON anchor day(s) of the month: [day] for monthly/quarterly/yearly, [a, b] for
    # semimonthly (31 = the month's last day). The API shows it read-only as ``anchor_days``.
    month_days: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    # Release 3.6 (schema v6): in-app reminder N days before (0, 1 or 3); occurrences before
    # start_date don't exist (manual items start at their first date; NULL = no limit); the
    # budget category the bill belongs to.
    reminder_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    start_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    category_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )


class RecurringOverride(Base):
    """A change to one occurrence of a recurring item: moved, skipped or marked paid."""

    __tablename__ = "recurring_overrides"
    __table_args__ = (UniqueConstraint("recurring_id", "base_date", name="uq_recurring_override_base"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recurring_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_items.id", ondelete="CASCADE"), index=True
    )
    base_date: Mapped[dt.date] = mapped_column(Date)  # the date the rule gives
    moved_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    paid: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    paid_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the item's sign
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # save | debt
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    target_cents: Mapped[int] = mapped_column(Integer, default=0)
    start_cents: Mapped[int] = mapped_column(Integer, default=0)
    current_cents: Mapped[int] = mapped_column(Integer, default=0)
    monthly_cents: Mapped[int] = mapped_column(Integer, default=0)
    apr: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    hue: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    field: Mapped[str] = mapped_column(String)  # any | merchant | name
    op: Mapped[str] = mapped_column(String)  # contains | is
    text: Mapped[str] = mapped_column(String)
    amount_op: Mapped[str | None] = mapped_column(String, nullable=True)  # gt | lt
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String)  # category | transfer
    category: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AlertSetting(Base):
    __tablename__ = "alert_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)  # warn | neg | accent
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_key: Mapped[str] = mapped_column(String, unique=True)
    # "Clear all" keeps a scrubbed tombstone so the same dedupe_key can't fire again.
    cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    # Schema v5: optional JSON for the Home screen (``price``: recurring_id, old/new cents, cadence).
    data: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


# ------------------------------------------------------------------ Release 3


class CategoryGroup(Base):
    __tablename__ = "category_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique ignoring (ASCII) case in the DB; the routes also compare with casefold().
    name: Mapped[str] = mapped_column(String(collation="NOCASE"), unique=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class TransactionSplit(Base):
    """One part of a split transaction; the parts replace the parent in every total."""

    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(ForeignKey("categories.id"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(collation="NOCASE"), unique=True)
    hue: Mapped[int] = mapped_column(Integer, default=0)


class TransactionTag(Base):
    __tablename__ = "transaction_tags"

    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True)
