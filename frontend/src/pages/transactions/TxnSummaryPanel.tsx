import type { TransactionSummary } from '../../api';
import { formatDate, formatMoney } from '../../lib/format';
import { Skeleton } from '../../components/ui';

/** Side panel with nothing selected: totals for what the list shows. */
export function TxnSummaryPanel({ summary, unfiltered }: { summary: TransactionSummary | null; unfiltered: boolean }) {
  const title = unfiltered && summary?.first_date ? `Since ${formatDate(summary.first_date)}` : 'Matching transactions';
  return (
    <section className="txn-panel txn-summary" aria-label="Summary">
      <h2 className="txn-summary-title">{summary ? title : <Skeleton width={140} height={14} />}</h2>
      <dl className="txn-summary-list">
        <dt>Money in</dt>
        <dd className="num txn-pos">{summary ? formatMoney(summary.money_in, { signed: true }) : '–'}</dd>
        <dt>Money out</dt>
        <dd className="num">{summary ? formatMoney(summary.money_out) : '–'}</dd>
        <dt>Needs a category</dt>
        <dd className="num txn-warn">{summary ? summary.needs_category.toLocaleString() : '–'}</dd>
      </dl>
      <p className="txn-hint">Transfers between your accounts aren’t counted.</p>
      <p className="txn-hint">Select a transaction to see its details, note and bank description here.</p>
    </section>
  );
}
