import { useId } from 'react';
import type { ForecastOccurrence, RecurringCandidate } from '../../api';
import { Icon } from '../../components/Icon';
import { candidateWhy } from './calLib';
import { amountText, canMarkPaid, comingCount, kindWords, markLabel, signedAmount, whenText } from './calMath';

/**
 * "Coming up in the next 7 days" (recurring-v2 design §3–4): late items first, then what's due
 * through a week from today, each with Mark paid / Mark received (never for card charges).
 * "Possible repeating charges" sits inside the same card when there are any: since Release 3.19
 * only suggestions with no Maybe chip on the calendar in the next 35 days (`boxCandidates`).
 */
export function ComingUp({
  items,
  today,
  busy,
  candidates,
  busyId,
  onOpen,
  onMarkPaid,
  onAddCandidate,
  onIgnoreCandidate,
}: {
  items: ForecastOccurrence[];
  today: string;
  busy: boolean;
  candidates: RecurringCandidate[];
  busyId: number | null;
  onOpen: (o: ForecastOccurrence) => void;
  onMarkPaid: (o: ForecastOccurrence) => void;
  onAddCandidate: (c: RecurringCandidate) => void;
  onIgnoreCandidate: (c: RecurringCandidate) => void;
}) {
  const headId = useId();
  const candId = useId();
  const count = comingCount(items);
  return (
    <section className="cal-card cal-coming" aria-labelledby={headId}>
      <div className="cal-card-top">
        <h2 id={headId}>Coming up in the next 7 days</h2>
        {count && <span className="cal-card-count">{count}</span>}
      </div>
      {items.length === 0 ? (
        <p className="cal-empty">Nothing due in the next 7 days.</p>
      ) : (
        <ul className="cal-coming-grid">
          {items.map((o) => {
            const late = o.status === 'late';
            return (
              <li key={o.key} className={`cal-due cal-k-${o.kind}${late ? ' is-late' : ''}`}>
                <span className="cal-due-when">{whenText(o, today)}</span>
                <button type="button" className="cal-due-name" data-occ={o.key} onClick={() => onOpen(o)}>
                  {o.name}
                </button>
                <span className="cal-due-amt">
                  <strong className={`num${o.amount > 0 ? ' is-in' : ''}`}>{signedAmount(o.amount)}</strong> · {kindWords(o.kind)}
                </span>
                {canMarkPaid(o) && (
                  <button type="button" className="btn cal-due-pay" disabled={busy} onClick={() => onMarkPaid(o)}>
                    {markLabel(o)}
                    <span className="sr-only">: {o.name}</span>
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {candidates.length > 0 && (
        <div className="cal-cands" role="group" aria-labelledby={candId}>
          <h3 id={candId} className="cal-cands-title">
            <Icon name="sparkle" />
            {candidates.length === 1 ? '1 possible repeating charge' : `${candidates.length} possible repeating charges`}
          </h3>
          {candidates.map((c) => {
            const it = c.item;
            return (
              <div className="cal-cand" key={it.id}>
                <p className="cal-cand-name">
                  {it.name} · <span className="num">{amountText(it.amount)}</span>
                </p>
                <p className="cal-cand-why">{candidateWhy(c)}</p>
                <div className="cal-cand-actions">
                  <button type="button" className="btn btn-primary" onClick={() => onAddCandidate(c)} disabled={busyId === it.id}>
                    Add to calendar
                  </button>
                  <button type="button" className="btn" onClick={() => onIgnoreCandidate(c)} disabled={busyId === it.id}>
                    Not a repeating bill
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
