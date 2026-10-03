import { useId, useMemo, useState } from 'react';
import type { Account, ForecastCalendar, RecurringItem } from '../../api';
import { amountText, countText, FIND_FILTERS, filterRows, findRows, noMatchText, type FindFilter, type FindKind } from './findItems';
import { SidePanel } from './SidePanel';
import { PriceAskBox } from './PriceAskBox';
import type { PriceAsk } from './priceAsk';

const KIND_CLASS: Record<FindKind, string> = { in: 'cal-k-in', out: 'cal-k-out', card: 'cal-k-card' };

/**
 * Find a bill or paycheck (README §10): every repeating item, A–Z, with search and filters.
 * Replaces "All repeating items": items on other accounts say "Not on this calendar", items
 * switched off say "Left out of the balance", suggestions for other accounts can be added or
 * hidden, and hidden suggestions have Add back. Release 3.19: a bill with a new price to
 * confirm has "now charges $X. Update your amount?" with Yes / No.
 */
export function FindPanel({
  open,
  onClose,
  items,
  onCalendar,
  accounts,
  cal,
  busyId,
  asks,
  onShow,
  onEdit,
  onRemove,
  onActivate,
  onDismiss,
  onPrice,
}: {
  open: boolean;
  onClose: () => void;
  items: RecurringItem[];
  /** Recurring ids the calendar shows. */
  onCalendar: ReadonlySet<number>;
  accounts: Account[];
  cal: ForecastCalendar;
  busyId: number | null;
  /** Release 3.19: open "now charges $X" questions by recurring id. */
  asks: Map<number, PriceAsk>;
  /** Jump to the item's next date on the calendar and open it. */
  onShow: (i: RecurringItem) => void;
  onEdit: (i: RecurringItem) => void;
  onRemove: (i: RecurringItem) => void;
  onActivate: (i: RecurringItem) => void;
  onDismiss: (i: RecurringItem) => void;
  onPrice: (a: PriceAsk, yes: boolean) => void;
}) {
  const uid = useId();
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<FindFilter>('all');

  const rows = useMemo(() => {
    const nextOf = new Map(cal.series.flatMap((s) => (s.recurring_id !== null ? [[s.recurring_id, s.next_date] as const] : [])));
    // The oldest late or not-yet-posted one: Show on calendar opens it first.
    const overdue = new Map<number, { date: string; late: boolean }>();
    for (const o of cal.occurrences) {
      if (o.recurring_id === null || (o.status !== 'late' && o.status !== 'pending')) continue;
      const cur = overdue.get(o.recurring_id);
      if (!cur || o.date < cur.date) overdue.set(o.recurring_id, { date: o.date, late: o.status === 'late' });
    }
    return findRows(items, {
      accounts,
      onCalendar,
      checkingId: cal.account?.id ?? null,
      today: cal.today,
      next: (id) => nextOf.get(id) ?? null,
      overdue: (id) => overdue.get(id) ?? null,
    });
  }, [items, accounts, onCalendar, cal]);
  const shown = filterRows(rows, query, filter);
  const activeCount = rows.filter((r) => r.group === 'active').length;

  const head = (
    <div className="calx-find-head">
      <label htmlFor={`${uid}-q`} className="calx-field-label">
        Search
      </label>
      <input
        id={`${uid}-q`}
        className="input calx-search"
        type="search"
        placeholder="e.g. Netflix, rent, 85"
        autoComplete="off"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        data-find-search=""
      />
      <div className="calx-pills" role="group" aria-label="Show">
        {FIND_FILTERS.map(([f, label]) => (
          <button key={f} type="button" className="calx-pill" aria-pressed={filter === f} onClick={() => setFilter(f)}>
            {label}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <SidePanel open={open} title="All bills and paychecks" onClose={onClose} width={520} head={head} initialFocus="[data-find-search]">
      <p className="calx-count" aria-live="polite">
        {countText(shown.length, rows.length)}
      </p>
      {rows.length === 0 ? (
        <p className="calx-empty">No bills or paychecks yet. Add one with “Add bill or income”.</p>
      ) : shown.length === 0 ? (
        <p className="calx-empty">{query.trim() ? noMatchText(query) : 'Nothing here with this filter.'}</p>
      ) : (
        <ul className="calx-find-list">
          {shown.map((r, idx) => {
            const i = r.item;
            const busy = busyId === i.id;
            const ask = r.group === 'active' ? asks.get(i.id) : undefined;
            const firstOfGroup = idx === 0 || shown[idx - 1]!.group !== r.group;
            return (
              <li key={i.id} className={`calx-find-li${firstOfGroup && r.group !== 'active' && activeCount > 0 ? ' is-group-start' : ''}`}>
                {firstOfGroup && r.group !== 'active' && (
                  <h3 className="calx-group">{r.group === 'suggested' ? 'Suggested, not for this calendar' : 'Hidden suggestions'}</h3>
                )}
                <div className={`calx-find-row cal-tint ${KIND_CLASS[r.kind]}${r.group !== 'active' ? ' is-muted' : ''}`}>
                  <div className="calx-find-top">
                    <span className="calx-find-name">{i.name}</span>
                    <span className={`calx-find-amt num${i.amount > 0 ? ' is-in' : ''}`}>{amountText(i.amount)}</span>
                  </div>
                  <p className="calx-find-meta">
                    {r.meta}
                    {r.late && (
                      <>
                        {' · '}
                        <span className={r.late.amber ? 'calx-late' : undefined}>{r.late.text}</span>
                      </>
                    )}
                  </p>
                  {ask && <PriceAskBox ask={ask} busy={busy} onAnswer={onPrice} />}
                  <div className="calx-find-actions">
                    {r.group === 'active' && (
                      <>
                        {r.onCalendar && (
                          <button type="button" className="btn calx-btn" onClick={() => onShow(i)} disabled={busy}>
                            Show on calendar
                          </button>
                        )}
                        <button type="button" className="btn calx-btn" onClick={() => onEdit(i)} disabled={busy}>
                          Edit
                        </button>
                        <button type="button" className="btn btn-ghost calx-btn calx-remove" onClick={() => onRemove(i)} disabled={busy}>
                          {i.source === 'manual' ? 'Delete' : 'Remove'}
                        </button>
                      </>
                    )}
                    {r.group === 'suggested' && (
                      <>
                        <button type="button" className="btn calx-btn" onClick={() => onActivate(i)} disabled={busy}>
                          Add
                        </button>
                        <button type="button" className="btn btn-ghost calx-btn" onClick={() => onDismiss(i)} disabled={busy}>
                          Not a repeating bill
                        </button>
                      </>
                    )}
                    {r.group === 'hidden' && (
                      <button type="button" className="btn calx-btn" onClick={() => onActivate(i)} disabled={busy}>
                        Add back
                      </button>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </SidePanel>
  );
}
