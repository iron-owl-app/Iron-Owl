import { useId } from 'react';
import { api } from '../../api';
import { useApi } from '../../lib/useApi';
import { toISODate } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { ErrorPanel } from '../../components/ErrorPanel';
import { perMonth, priceNote, subscriptionNextLine, subscriptionsLead, subscriptionsTotal } from '../reports/habitsMath';
import './panels.css';

/**
 * Subscriptions (README §7, SPEC Release 3.15): the same list and words as Reports › Habits
 * (`GET /api/reports/subscriptions` + the habitsMath helpers), plus each one's next charge.
 */
export function SubscriptionsCard({ reloadKey = 0 }: { reloadKey?: number }) {
  const subs = useApi(() => api.reports.subscriptions(), [reloadKey]);
  const headId = useId();
  const month = toISODate(new Date()).slice(0, 7);
  if (!subs.data && subs.error) return <ErrorPanel error={subs.error} onRetry={subs.reload} title="Couldn’t load your subscriptions" />;
  const items = subs.data?.items ?? [];
  return (
    <section className="calx-card calx-subs cal-k-subs" aria-labelledby={headId}>
      <h2 id={headId} className="calx-h2">
        Subscriptions
      </h2>
      {!subs.data ? (
        <div role="status" aria-label="Loading subscriptions">
          <Skeleton className="skel-text" width="60%" />
          <Skeleton height={120} style={{ marginTop: 10, borderRadius: 10 }} />
        </div>
      ) : !items.length ? (
        <p className="calx-sub">No subscriptions yet. Charges that repeat on a credit card show up here.</p>
      ) : (
        <>
          <p className="calx-sub">{subscriptionsLead(items)}</p>
          <ul className="calx-subs-list">
            {items.map((s) => {
              const note = priceNote(s, month);
              const line = subscriptionNextLine(s, month);
              return (
                <li key={s.id}>
                  <span className="calx-subs-main">
                    <span className="calx-subs-name">
                      <strong>{s.name}</strong>
                      {note && note.tone !== 'same' && <span className={`calx-tag ${note.tone === 'up' ? 'is-up' : 'is-down'}`}>{note.text}</span>}
                    </span>
                    {line && <span className="calx-subs-line">{line}</span>}
                  </span>
                  <strong className="num calx-subs-amt">{perMonth(s.monthly)}</strong>
                </li>
              );
            })}
          </ul>
          <p className="calx-subs-foot num">
            <strong>{subscriptionsTotal(subs.data.monthly_total)}</strong>
          </p>
        </>
      )}
    </section>
  );
}
