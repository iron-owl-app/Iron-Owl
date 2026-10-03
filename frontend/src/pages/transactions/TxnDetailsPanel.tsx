import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { api, errorMessage, type Rule, type Tag, type Transaction, type TransactionRelated, type TxnCategory } from '../../api';
import { formatDate, formatMoney, humanizeCategory } from '../../lib/format';
import { toneFor, toneStyle } from '../../lib/categoryColors';
import { Icon } from '../../components/Icon';
import { Avatar, Money } from '../../components/ui';
import { CategoryDot } from '../../components/CategoryPopover';
import { TagEditor } from '../../components/TagEditor';
import { useToast } from '../../components/Toast';
import { BulkOffer, type RuleOffer } from './BulkOffer';
import { MAX_RULE_TEXT, SOURCE_TEXT, formatDayLong, ruleMatchFor, ruleText, txnTitle } from './shared';

/** Note: saves on blur or Ctrl+Enter (max 500). Remount per transaction (`key`). */
function NoteField({ t, onUpdated }: { t: Transaction; onUpdated: (u: Transaction) => void }) {
  const uid = useId();
  const toast = useToast();
  const [notes, setNotes] = useState(t.notes ?? '');
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');

  async function save() {
    const next = notes.trim();
    if (next === (t.notes ?? '')) return;
    setStatus('saving');
    try {
      onUpdated(await api.updateTransaction(t.id, { notes: next || null }));
      setStatus('saved');
    } catch (e) {
      setStatus('error');
      toast.push({ tone: 'error', title: 'Couldn’t save the note', body: errorMessage(e) });
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void save();
    }
  }

  return (
    <div className="field txn-note">
      <label className="txn-label" htmlFor={`${uid}-notes`}>
        Note
      </label>
      <textarea
        id={`${uid}-notes`}
        className="textarea"
        rows={3}
        maxLength={500}
        value={notes}
        placeholder="Add a note for yourself. Notes are searchable."
        onChange={(e) => {
          setNotes(e.target.value);
          setStatus('idle');
        }}
        onBlur={() => void save()}
        onKeyDown={onKey}
        aria-describedby={`${uid}-notes-status`}
      />
      <span className="field-hint" id={`${uid}-notes-status`} aria-live="polite">
        {status === 'saving' ? 'Saving…' : status === 'saved' ? 'Saved' : status === 'error' ? 'Not saved' : 'Saves when you leave the box (or Ctrl+Enter).'}
      </span>
    </div>
  );
}

/** "14 transactions from Starbucks since Aug 3, 2026 · −$86.40". */
function RelatedLine({ id }: { id: number }) {
  const [rel, setRel] = useState<TransactionRelated | null>(null);
  useEffect(() => {
    let alive = true;
    setRel(null);
    api
      .transactionRelated(id)
      .then((r) => alive && setRel(r))
      .catch(() => {
        /* optional footer */
      });
    return () => {
      alive = false;
    };
  }, [id]);
  if (!rel) return <div className="txn-related" aria-hidden="true">&nbsp;</div>;
  return (
    <div className="txn-related">
      {rel.count > 1 ? (
        <>
          {rel.count.toLocaleString()} transactions from {rel.text} since {formatDate(rel.first_date)} · <span className="num">{formatMoney(rel.total)}</span> total
        </>
      ) : (
        <>First transaction from {rel.text}</>
      )}
    </div>
  );
}

/** Side panel for the selected transaction (a right-hand drawer at narrower widths). */
export function TxnDetailsPanel({
  t,
  accountLabel,
  categories,
  catById,
  groupIndex,
  groupNames,
  rule,
  tags,
  sameCount,
  offer,
  pickerOpen,
  onClose,
  onOpenPicker,
  onUpdated,
  onTagsChanged,
  onTagCreated,
  onSplit,
  onToggleTransfer,
  onRemoveRule,
  onMakeRule,
  onSelectSame,
  onOfferClose,
  onOfferApplied,
}: {
  t: Transaction;
  accountLabel: string;
  categories: TxnCategory[];
  catById: ReadonlyMap<string, TxnCategory>;
  groupIndex: ReadonlyMap<number, number>;
  groupNames: ReadonlyMap<number, string>;
  rule: Rule | null;
  tags: Tag[];
  /** Loaded rows from the same merchant, this one included. */
  sameCount: number;
  offer: RuleOffer | null;
  pickerOpen: boolean;
  onClose: () => void;
  onOpenPicker: (anchor: HTMLElement) => void;
  onUpdated: (u: Transaction) => void;
  onTagsChanged: () => void;
  onTagCreated: (t: Tag) => void;
  onSplit: (anchor: HTMLElement) => void;
  onToggleTransfer: (v: boolean) => Promise<void>;
  onRemoveRule: (r: Rule) => Promise<void>;
  onMakeRule: () => Promise<void>;
  onSelectSame: () => void;
  onOfferClose: () => void;
  onOfferApplied: () => void;
}) {
  const uid = useId();
  const [tagsOpen, setTagsOpen] = useState(false);
  const [transferBusy, setTransferBusy] = useState(false);
  const [ruleBusy, setRuleBusy] = useState(false);
  const tagsRef = useRef<HTMLDivElement>(null);
  useEffect(() => setTagsOpen(false), [t.id]);
  // "Add tag" reveals the editor: put the cursor in it.
  useEffect(() => {
    if (tagsOpen) tagsRef.current?.querySelector('input')?.focus();
  }, [tagsOpen]);

  const title = txnTitle(t);
  const split = t.splits.length > 0;
  const needs = t.needs_category && !split;
  const cat = catById.get(t.category ?? 'OTHER');
  const groupName = cat?.group_id != null ? (groupNames.get(cat.group_id) ?? null) : null;
  const tone = toneFor(t.category, catById, groupIndex, t.category_hue);
  const match = ruleMatchFor(t);
  const canMakeRule = !rule && t.matching_rule_id === null && !needs && !split && t.category !== null && match.text.length > 0 && match.text.length <= MAX_RULE_TEXT;
  // Release 3.6 phase 2: "Sorted automatically" is the budget's built-in sorting (the
  // category itself shows right above, so it isn't repeated here).
  const sourceText = needs ? 'Not set yet' : split ? 'Split' : SOURCE_TEXT[t.category_source];

  async function toggleTransfer() {
    setTransferBusy(true);
    await onToggleTransfer(!t.is_transfer);
    setTransferBusy(false);
  }

  /** "Always put…" and rule Remove: one request at a time. */
  async function ruleAction(run: () => Promise<void>) {
    if (ruleBusy) return;
    setRuleBusy(true);
    try {
      await run();
    } finally {
      setRuleBusy(false);
    }
  }

  return (
    <section className="txn-panel txn-details" id="txn-panel" aria-labelledby={`${uid}-title`}>
      <div className="txn-details-head">
        <Avatar name={title} size="xl" />
        <div className="txn-details-titles">
          <h2 id={`${uid}-title`} className="truncate" tabIndex={-1} data-panel-title="">
            {title}
          </h2>
          <div className="txn-details-date">{formatDayLong(t.date)}</div>
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close details" title="Close (Esc)">
          <Icon name="x" />
        </button>
      </div>

      <Money value={t.amount} tone="in" signed={t.amount > 0} className="txn-details-amt" />

      {split ? (
        <div className="txn-field">
          <div className="txn-splits-head">
            <span className="txn-label">Split into {t.splits.length}</span>
            <button type="button" className="btn btn-sm" onClick={(e) => onSplit(e.currentTarget)} disabled={!categories.length}>
              <Icon name="split" />
              Edit split
            </button>
          </div>
          <ul className="tx-split-lines">
            {t.splits.map((sp) => (
              <li key={sp.id}>
                <CategoryDot hue={sp.category_hue} />
                <span className="tx-split-name">
                  <span className="truncate">{sp.category_name}</span>
                  {sp.notes && <span className="tx-split-note truncate">{sp.notes}</span>}
                </span>
                <Money value={sp.amount} tone="in" signed={sp.amount > 0} className="tx-split-amt" />
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="txn-field">
          <span className="txn-label" id={`${uid}-cat`}>
            Category
          </span>
          <button
            type="button"
            className={`txn-cat-btn${needs ? ' is-needs' : ''}`}
            style={needs ? undefined : toneStyle(tone)}
            onClick={(e) => onOpenPicker(e.currentTarget)}
            aria-haspopup="dialog"
            aria-expanded={pickerOpen}
            aria-describedby={`${uid}-cat`}
            disabled={!categories.length}
          >
            <span className="txn-chip-dot" aria-hidden="true" />
            <span className="txn-cat-name truncate">{needs ? 'Choose category' : t.category_name}</span>
            {groupName && <span className="txn-cat-group truncate">{groupName}</span>}
            <Icon name="chevronDown" className="txn-chip-chev" />
          </button>
        </div>
      )}

      <dl className="txn-facts">
        <dt>Account</dt>
        <dd className="truncate">{accountLabel}</dd>
        <dt>Status</dt>
        <dd>{t.pending ? 'Pending' : 'Posted'}</dd>
        <dt>Category from</dt>
        <dd className={needs ? 'txn-warn' : (t.category_source === 'rule' || t.category_source === 'auto') && !split ? 'txn-accent' : undefined}>{sourceText}</dd>
        {t.plaid_category && (
          <>
            <dt>Bank’s category</dt>
            <dd>{humanizeCategory(t.plaid_category)}</dd>
          </>
        )}
        <dt>Bank description</dt>
        <dd className="txn-raw" title={t.name}>
          {t.name}
        </dd>
      </dl>

      {rule ? (
        <div className="txn-rule">
          <Icon name="bolt" />
          <span className="txn-rule-text">{ruleText(rule, catById)}</span>
          <button type="button" className="txn-link-btn" onClick={() => void ruleAction(() => onRemoveRule(rule))} disabled={ruleBusy}>
            {ruleBusy ? 'Removing…' : 'Remove'}
          </button>
        </div>
      ) : (
        canMakeRule && (
          <button type="button" className="txn-make-rule" onClick={() => void ruleAction(onMakeRule)} disabled={ruleBusy}>
            <Icon name="bolt" />
            <span className="truncate">
              Always put {match.text} in {t.category_name}
            </span>
          </button>
        )
      )}

      {offer && offer.txnId === t.id && <BulkOffer key={`${offer.txnId}-${offer.category}`} offer={offer} onClose={onOfferClose} onApplied={onOfferApplied} />}

      <NoteField key={t.id} t={t} onUpdated={onUpdated} />

      {(tagsOpen || t.tags.length > 0) && (
        <div ref={tagsRef}>
          <TagEditor
            txn={t}
            allTags={tags}
            onUpdated={(u) => {
              onUpdated(u);
              onTagsChanged();
            }}
            onTagCreated={onTagCreated}
          />
        </div>
      )}

      <div className="txn-actions">
        {sameCount > 1 && (
          <button type="button" className="btn btn-sm" onClick={onSelectSame}>
            Select all {sameCount.toLocaleString()} from {title}
          </button>
        )}
        {!split && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={(e) => onSplit(e.currentTarget)}
            disabled={t.amount === 0 || !categories.length}
            title={t.amount === 0 ? 'A $0.00 transaction can’t be split' : 'Spread this across categories'}
          >
            <Icon name="split" />
            Split
          </button>
        )}
        {!tagsOpen && t.tags.length === 0 && (
          <button type="button" className="btn btn-sm" onClick={() => setTagsOpen(true)}>
            <Icon name="tag" />
            Add tag
          </button>
        )}
        <button type="button" className="btn btn-sm" onClick={() => void toggleTransfer()} disabled={transferBusy} aria-describedby={`${uid}-tr`}>
          <Icon name="arrows" />
          {t.is_transfer ? 'Not a transfer' : 'Mark as transfer'}
        </button>
      </div>
      <p className="txn-hint" id={`${uid}-tr`}>
        Transfers between your own accounts are left out of spending, income and budgets.
      </p>

      <RelatedLine id={t.id} />
    </section>
  );
}
