import { memo, type MouseEvent, type ReactNode } from 'react';
import type { Transaction, TxnCategory } from '../../api';
import { toneFor, toneStyle, type CategoryTone } from '../../lib/categoryColors';
import { Icon } from '../../components/Icon';
import { Avatar, Money } from '../../components/ui';
import { TagChips } from '../../components/TagEditor';
import { txnTitle } from './shared';

export interface RowHandlers {
  /** `fromName`: activated from the name button (click / Enter): focus moves into the panel. */
  onSelect: (id: number, fromName?: boolean) => void;
  onToggleCheck: (id: number) => void;
  onOpenPicker: (id: number, anchor: HTMLElement) => void;
  onSuggest: (id: number, category: string) => void;
  onOpenSplit: (id: number, anchor: HTMLElement) => void;
}

const stop = (e: MouseEvent) => e.stopPropagation();

/**
 * Category chip: colored by the category's group ("Choose category" in warn when the row
 * needs one). `data-chip` lets the keyboard flow anchor the picker to it.
 */
export function CategoryChip({
  label,
  tone,
  needs,
  expanded,
  ariaLabel,
  title,
  onClick,
  className,
  children,
}: {
  label: string;
  tone: CategoryTone | null;
  needs: boolean;
  expanded?: boolean;
  ariaLabel: string;
  title?: string;
  onClick: (e: MouseEvent<HTMLButtonElement>) => void;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <button
      type="button"
      data-chip=""
      className={`txn-chip${needs ? ' is-needs' : ''}${className ? ` ${className}` : ''}`}
      style={tone && !needs ? toneStyle(tone) : undefined}
      onClick={onClick}
      aria-haspopup="dialog"
      aria-expanded={expanded}
      aria-label={ariaLabel}
      title={title}
    >
      <span className="txn-chip-dot" aria-hidden="true" />
      <span className="txn-chip-label truncate">{label}</span>
      {children}
      <Icon name="chevronDown" className="txn-chip-chev" />
    </button>
  );
}

/** "Dine out?" — one click (or 1/2 on the selected row) sets it. */
function Suggestion({ cat, tone, n, showKey, onPick }: { cat: TxnCategory; tone: CategoryTone; n: number; showKey: boolean; onPick: (e: MouseEvent) => void }) {
  return (
    <button type="button" className="txn-sug" style={toneStyle(tone)} onClick={onPick} title={`Set to ${cat.name}${showKey ? ` (${n})` : ''}`} aria-label={`Set category to ${cat.name}`}>
      <span className="txn-chip-dot" aria-hidden="true" />
      {cat.name}?
      {showKey && (
        <span className="txn-sug-key" aria-hidden="true">
          {n}
        </span>
      )}
    </button>
  );
}

/** One transaction. Memoized: every prop is a stable reference unless the row itself changed. */
export const TxnRow = memo(function TxnRow({
  t,
  accountLabel,
  selected,
  checked,
  pickerOpen,
  catById,
  groupIndex,
  handlers,
}: {
  t: Transaction;
  accountLabel: string;
  selected: boolean;
  checked: boolean;
  pickerOpen: boolean;
  catById: ReadonlyMap<string, TxnCategory>;
  groupIndex: ReadonlyMap<number, number>;
  handlers: RowHandlers;
}) {
  const title = txnTitle(t);
  const split = t.splits.length > 0;
  const needs = t.needs_category && !split;
  const sugs = needs
    ? t.suggested_categories
        .map((id) => catById.get(id))
        .filter((c): c is TxnCategory => !!c && !c.hidden)
        .slice(0, 2)
    : [];
  const bankSays = t.category && t.category !== 'OTHER' ? t.category_name : null;
  const cls = ['txn-row', selected && 'is-selected', checked && 'is-checked', needs && 'is-needs'].filter(Boolean).join(' ');

  return (
    <div className={cls} data-tx={t.id} onClick={() => handlers.onSelect(t.id)}>
      <label className="txn-check" onClick={stop} title="Select (X)">
        <input type="checkbox" checked={checked} onChange={() => handlers.onToggleCheck(t.id)} aria-label={`Select ${title}`} />
        <Icon name="check" />
      </label>
      <Avatar name={title} size="md" />
      <div className="txn-text">
        <div className="txn-title">
          <button
            type="button"
            className="txn-name"
            data-name=""
            onClick={(e) => {
              e.stopPropagation();
              handlers.onSelect(t.id, true);
            }}
            aria-expanded={selected}
            aria-controls={selected ? 'txn-panel' : undefined}
          >
            {title}
            <span className="sr-only">, details</span>
          </button>
          {t.pending && <span className="badge badge-outline">Pending</span>}
          {t.is_transfer && <span className="badge">Transfer</span>}
        </div>
        <div className="txn-sub">
          <span className="truncate">{accountLabel}</span>
          {t.tags.length > 0 && <TagChips tags={t.tags} max={2} />}
        </div>
      </div>
      <div className="txn-cats">
        {sugs.map((c, i) => (
          <Suggestion
            key={c.id}
            cat={c}
            tone={toneFor(c.id, catById, groupIndex, c.hue)}
            n={i + 1}
            showKey={selected}
            onPick={(e) => {
              e.stopPropagation();
              handlers.onSuggest(t.id, c.id);
            }}
          />
        ))}
        {split ? (
          <button
            type="button"
            data-chip=""
            className="txn-chip is-split"
            onClick={(e) => {
              e.stopPropagation();
              handlers.onOpenSplit(t.id, e.currentTarget);
            }}
            aria-label={`${title} is split into ${t.splits.length} categories: ${t.splits.map((s) => s.category_name).join(', ')}. Edit split`}
            title={t.splits.map((s) => s.category_name).join(', ')}
          >
            <Icon name="split" className="txn-chip-icon" />
            <span className="truncate">Split · {t.splits.length}</span>
          </button>
        ) : (
          <CategoryChip
            label={needs ? 'Choose category' : t.category_name}
            tone={needs ? null : toneFor(t.category, catById, groupIndex, t.category_hue)}
            needs={needs}
            expanded={pickerOpen}
            ariaLabel={needs ? `Category for ${title}: not set yet${bankSays ? ` (your bank says ${bankSays})` : ''}. Choose` : `Category for ${title}: ${t.category_name}. Change`}
            title={needs && bankSays ? `Your bank says ${bankSays}` : undefined}
            onClick={(e) => {
              e.stopPropagation();
              handlers.onOpenPicker(t.id, e.currentTarget);
            }}
          />
        )}
      </div>
      <span className="txn-amt">
        <Money value={t.amount} tone="in" signed={t.amount > 0} />
        <span className="sr-only">{t.amount < 0 ? ' money out' : ' money in'}</span>
      </span>
    </div>
  );
});
