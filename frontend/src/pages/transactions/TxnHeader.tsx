import { useId } from 'react';
import type { Account, Tag, TransactionSummary, TxnCategory, TxnView } from '../../api';
import { formatDate } from '../../lib/format';
import { Icon } from '../../components/Icon';

const PILLS: { view: TxnView; label: string }[] = [
  { view: 'all', label: 'All' },
  { view: 'needs_category', label: 'Needs a category' },
  { view: 'in', label: 'Money in' },
  { view: 'out', label: 'Money out' },
];

/** Title + "N transactions since …", search, view pills, Filters toggle and CSV export. */
export function TxnHeader({
  summary,
  view,
  searchDraft,
  onSearch,
  onView,
  filtersOpen,
  filterCount,
  onToggleFilters,
  exporting,
  canExport,
  onExport,
}: {
  summary: TransactionSummary | null;
  view: TxnView;
  searchDraft: string;
  onSearch: (v: string) => void;
  onView: (v: TxnView) => void;
  filtersOpen: boolean;
  filterCount: number;
  onToggleFilters: () => void;
  exporting: boolean;
  canExport: boolean;
  onExport: () => void;
}) {
  const uid = useId();
  const sub = !summary
    ? ' '
    : summary.total === 0
      ? 'No transactions'
      : `${summary.total.toLocaleString()} transaction${summary.total === 1 ? '' : 's'}${summary.first_date ? ` since ${formatDate(summary.first_date)}` : ''}`;

  return (
    <div className="page-head txn-head">
      <div>
        <h1>Transactions</h1>
        <p className="small">{sub}</p>
      </div>
      <div className="txn-head-controls">
        <div className="input-affix txn-search" role="search">
          <span className="affix">
            <Icon name="search" />
          </span>
          <label className="sr-only" htmlFor={`${uid}-q`}>
            Search transactions
          </label>
          <input
            id={`${uid}-q`}
            type="search"
            className="input has-icon"
            placeholder="Merchant, description or note"
            value={searchDraft}
            onChange={(e) => onSearch(e.target.value)}
          />
        </div>
        <div className="txn-pills" role="group" aria-label="Show">
          {PILLS.map((p) => {
            const count = p.view === 'all' || p.view === 'needs_category' ? summary?.counts[p.view] : undefined;
            return (
              <button key={p.view} type="button" className="txn-pill" aria-pressed={view === p.view} onClick={() => onView(p.view)}>
                {p.view === 'needs_category' && <span className="txn-pill-dot" aria-hidden="true" />}
                {p.label}
                {count !== undefined && <span className="txn-pill-count num">{count.toLocaleString()}</span>}
              </button>
            );
          })}
        </div>
        <button type="button" className={`btn btn-ghost txn-filters-btn${filtersOpen ? ' is-open' : ''}`} onClick={onToggleFilters} aria-expanded={filtersOpen} aria-controls={`txn-filters`}>
          <Icon name="sliders" />
          Filters
          {filterCount > 0 && (
            <>
              <span className="txn-filters-count" aria-hidden="true">
                {filterCount}
              </span>
              <span className="sr-only">, {filterCount} active</span>
            </>
          )}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onExport} disabled={exporting || !canExport} title="Export the transactions shown (all pages)">
          <Icon name={exporting ? 'sync' : 'download'} className={exporting ? 'spin' : undefined} />
          {exporting ? 'Exporting…' : 'Export CSV'}
        </button>
      </div>
    </div>
  );
}

const KIND_GROUPS: [TxnCategory['kind'], string][] = [
  ['spending', 'Spending'],
  ['income', 'Income'],
  ['transfer', 'Transfers'],
  ['fixed', 'Fixed costs'],
];

/** Account, category, tag and date filters (behind the Filters button). */
export function TxnFilterRow({
  accounts,
  categories,
  tags,
  tagsLoaded,
  accountId,
  category,
  tag,
  start,
  end,
  onChange,
  onClear,
}: {
  accounts: Account[];
  categories: TxnCategory[];
  tags: Tag[];
  tagsLoaded: boolean;
  accountId: number | undefined;
  category: string;
  tag: number | undefined;
  start: string;
  end: string;
  onChange: (patch: Record<string, string | null>) => void;
  onClear: () => void;
}) {
  const uid = useId();
  const tagMissing = tag !== undefined && tagsLoaded && !tags.some((t) => t.id === tag);
  const rangeInvalid = !!(start && end && start > end);
  const any = !!(accountId || category || tag || start || end);
  return (
    <div className="txn-filters" id="txn-filters" role="group" aria-label="Filters">
      <div className="field">
        <label className="field-label" htmlFor={`${uid}-acct`}>
          Account
        </label>
        <select id={`${uid}-acct`} className="select" value={accountId ?? ''} onChange={(e) => onChange({ account: e.target.value || null })}>
          <option value="">All accounts</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
              {a.mask ? ` ••${a.mask}` : ''}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor={`${uid}-cat`}>
          Category
        </label>
        <select id={`${uid}-cat`} className="select" value={category} onChange={(e) => onChange({ cat: e.target.value || null })}>
          <option value="">All categories</option>
          {KIND_GROUPS.map(([k, label]) => {
            const list = categories.filter((c) => c.kind === k && (!c.hidden || c.id === category));
            return list.length ? (
              <optgroup key={k} label={label}>
                {list.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </optgroup>
            ) : null;
          })}
        </select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor={`${uid}-tag`}>
          Tag
        </label>
        <select id={`${uid}-tag`} className="select" value={tag ?? ''} onChange={(e) => onChange({ tag: e.target.value || null })}>
          <option value="">{tags.length || tag ? 'All tags' : 'No tags yet'}</option>
          {tags.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
          {tagMissing && <option value={tag}>Deleted tag</option>}
        </select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor={`${uid}-start`}>
          From
        </label>
        <input id={`${uid}-start`} type="date" className="input" value={start} max={end || undefined} onChange={(e) => onChange({ start: e.target.value || null })} />
      </div>
      <div className="field">
        <label className="field-label" htmlFor={`${uid}-end`}>
          To
        </label>
        <input id={`${uid}-end`} type="date" className="input" value={end} min={start || undefined} onChange={(e) => onChange({ end: e.target.value || null })} />
      </div>
      <button type="button" className="btn btn-ghost" onClick={onClear} disabled={!any}>
        Clear
      </button>
      {rangeInvalid && (
        <div className="field-error txn-filters-error" role="alert">
          “From” is after “To”, so nothing can match.
        </div>
      )}
    </div>
  );
}
