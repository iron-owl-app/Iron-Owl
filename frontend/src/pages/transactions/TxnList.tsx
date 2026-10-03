import { useEffect, useMemo, useRef, type ReactNode, type RefObject } from 'react';
import { errorMessage, type DayTotals, type Transaction, type TxnCategory } from '../../api';
import { formatDate, formatMoney } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { ErrorPanel } from '../../components/ErrorPanel';
import { TxnRow, type RowHandlers } from './TxnRow';
import { formatDayLong } from './shared';

type Day = { date: string; rows: Transaction[] };

/** Day header: "Friday, Sep 26, 2026" with that whole day's money in / out (transfers left out; zeros hidden). */
function DayHead({ date, totals }: { date: string; totals: DayTotals | undefined }) {
  const moneyIn = totals?.money_in ?? 0;
  const moneyOut = totals?.money_out ?? 0;
  return (
    <h3 className="txn-day-head" id={`txn-day-${date}`}>
      <span>{formatDayLong(date)}</span>
      {(moneyIn > 0.004 || moneyOut < -0.004) && (
        <span className="txn-day-totals num">
          {moneyIn > 0.004 && <span className="txn-day-in">{formatMoney(moneyIn, { signed: true })}</span>}
          {moneyOut < -0.004 && <span className="txn-day-out">{formatMoney(moneyOut)} out</span>}
        </span>
      )}
    </h3>
  );
}

function ListSkeleton() {
  return (
    <div role="status" aria-label="Loading transactions">
      <div className="txn-day-head" aria-hidden="true">
        <Skeleton width={180} height={12} />
      </div>
      {Array.from({ length: 8 }, (_, i) => (
        <div className="txn-row txn-row-skel" key={i} aria-hidden="true">
          <Skeleton width={18} height={18} style={{ borderRadius: 5 }} />
          <Skeleton width={36} height={36} style={{ borderRadius: 10 }} />
          <div>
            <Skeleton className="skel-text" width={`${35 + ((i * 13) % 35)}%`} />
            <Skeleton className="skel-text" width={`${20 + ((i * 7) % 20)}%`} />
          </div>
          <Skeleton width={96} height={26} style={{ borderRadius: 999 }} />
          <Skeleton width={70} height={14} style={{ justifySelf: 'end' }} />
        </div>
      ))}
    </div>
  );
}

/**
 * The scrolling list: sticky day headers, rows, and a sentinel that loads the next page
 * when it comes within 240px. Desktop scrolls inside the list; ≤860px (`pageScroll`) the page scrolls.
 */
export function TxnList({
  listRef,
  pageScroll,
  items,
  days,
  loaded,
  loading,
  stale,
  error,
  nextCursor,
  firstDate,
  selectedId,
  checked,
  pickerTarget,
  catById,
  groupIndex,
  accountLabels,
  handlers,
  empty,
  onLoadMore,
  onRetry,
}: {
  listRef: RefObject<HTMLElement>;
  pageScroll: boolean;
  items: Transaction[];
  days: Record<string, DayTotals>;
  loaded: boolean;
  loading: boolean;
  /** A new first page is loading; the rows shown are from the previous filters. */
  stale: boolean;
  error: unknown;
  nextCursor: string | null;
  firstDate: string | null;
  selectedId: number | null;
  checked: ReadonlySet<number>;
  pickerTarget: number | 'bulk' | null;
  catById: ReadonlyMap<string, TxnCategory>;
  groupIndex: ReadonlyMap<number, number>;
  accountLabels: ReadonlyMap<number, string>;
  handlers: RowHandlers;
  empty: ReactNode;
  onLoadMore: () => void;
  onRetry: () => void;
}) {
  const sentinel = useRef<HTMLDivElement>(null);
  const loadMore = useRef(onLoadMore);
  loadMore.current = onLoadMore;

  const groups = useMemo(() => {
    const out: Day[] = [];
    for (const t of items) {
      const last = out[out.length - 1];
      if (last && last.date === t.date) last.rows.push(t);
      else out.push({ date: t.date, rows: [t] });
    }
    return out;
  }, [items]);

  // Re-armed after every request so a sentinel that's still in view loads the next page too
  // (including one that came into view while a request was in flight).
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !nextCursor || error || loading) return;
    const io = new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && loadMore.current(), {
      root: pageScroll ? null : listRef.current,
      rootMargin: '240px',
    });
    io.observe(el);
    return () => io.disconnect();
  }, [pageScroll, nextCursor, items.length, error, loading, listRef]);

  let body: ReactNode;
  if (!loaded && error) body = <ErrorPanel error={error} onRetry={onRetry} />;
  else if (!loaded) body = <ListSkeleton />;
  else if (!items.length) body = empty;
  else
    body = (
      <>
        {groups.map((d) => (
          <section key={d.date} className="txn-day" aria-labelledby={`txn-day-${d.date}`}>
            <DayHead date={d.date} totals={days[d.date]} />
            {d.rows.map((t) => (
              <TxnRow
                key={t.id}
                t={t}
                accountLabel={accountLabels.get(t.account_id) ?? t.account_name}
                selected={t.id === selectedId}
                checked={checked.has(t.id)}
                pickerOpen={pickerTarget === t.id}
                catById={catById}
                groupIndex={groupIndex}
                handlers={handlers}
              />
            ))}
          </section>
        ))}
        <div ref={sentinel} className="txn-foot" aria-live="polite">
          {error ? (
            <>
              Couldn’t load more: {errorMessage(error)}{' '}
              <button type="button" className="btn btn-sm" onClick={onRetry}>
                Try again
              </button>
            </>
          ) : nextCursor ? (
            'Loading more as you scroll…'
          ) : firstDate ? (
            `That’s everything since ${formatDate(firstDate)}`
          ) : (
            'That’s everything'
          )}
        </div>
      </>
    );

  return (
    <section ref={listRef} className={`txn-list${stale ? ' is-stale' : ''}`} aria-label="Transactions" aria-busy={loading || undefined}>
      {body}
    </section>
  );
}
