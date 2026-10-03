import type { ForecastDay, ForecastOccurrence } from '../../api';
import { dayHeadingId } from './MonthGrid';
import { MaybeRow } from './MaybeChip';
import { PriceAskBox } from './PriceAskBox';
import { askText, type PriceAsk } from './priceAsk';
import { chipLabel, shownAmount } from './calLib';
import { balanceRowIndex, dayRows, dollars, signedAmount, statusWords, weekGroups, weekdayShort } from './calMath';

/**
 * The List view (always on phones ≤640px; a choice on wider screens), styled like
 * recurring-v2 design layout C: the viewed month's days that
 * have something, grouped by week. Each item is one big row (a button that opens Item
 * details): a date block, the name, its status in words, the amount, and on a day's last
 * row the balance after it ("Then $2,141"). "Maybe" bills (Release 3.19) follow a day's
 * items with the question and Yes / No; the balance stays on the day's last real row. A bill
 * with a new price to confirm (Release 3.19, `askByKey`) has "now charges $X. Update your
 * amount?" with Yes / No under its row.
 */
export function ForecastList({
  wide = false,
  dates,
  today,
  weekStart,
  monthFirst,
  monthLast,
  days,
  byDate,
  maybeByDate,
  askByKey,
  busyId,
  showBalances,
  flash,
  onOpen,
  onMaybe,
  onPrice,
}: {
  /** Desktop: the line length is capped so rows don't stretch across the page. */
  wide?: boolean;
  /** The month's dates, in order. */
  dates: string[];
  today: string;
  weekStart: 0 | 1;
  monthFirst: string;
  monthLast: string;
  days: Map<string, ForecastDay>;
  byDate: Map<string, ForecastOccurrence[]>;
  /** Release 3.19: the calendar's `maybe` rows by date (display only). */
  maybeByDate: Map<string, ForecastOccurrence[]>;
  /** Release 3.19: occurrence key -> "now charges $X" question shown under that row. */
  askByKey: Map<string, PriceAsk>;
  busyId: number | null;
  showBalances: boolean;
  flash: string | null;
  onOpen: (o: ForecastOccurrence) => void;
  onMaybe: (o: ForecastOccurrence, yes: boolean) => void;
  onPrice: (a: PriceAsk, yes: boolean) => void;
}) {
  const shown = dates.filter((d) => (byDate.get(d)?.length ?? 0) + (maybeByDate.get(d)?.length ?? 0) > 0);
  if (!shown.length) {
    return <p className="cal-list-empty">Nothing is expected this month. Use “Add bill or income” to put something on the calendar.</p>;
  }
  const weeks = weekGroups(shown, weekStart, monthFirst, monthLast);
  return (
    <div className={`cal-list${wide ? ' is-wide' : ''}`}>
      {weeks.map((w) => (
        <section key={w.label} className="cal-list-week" aria-label={w.label}>
          <h3 className="cal-list-weekhead">{w.label}</h3>
          <ol className="cal-list-rows">
            {w.dates.flatMap((d) => {
              const occ = dayRows(byDate.get(d), maybeByDate.get(d));
              const balAt = balanceRowIndex(occ);
              const day = days.get(d);
              const bal = day?.balance ?? null;
              const low = bal !== null && !!day?.below;
              return occ.map((o, j) => {
                if (o.maybe) {
                  return (
                    <li key={o.key} id={j === 0 ? dayHeadingId(d) : undefined} tabIndex={j === 0 ? -1 : undefined} className={`cal-lrow cal-lrow-maybe${flash === d ? ' is-flash' : ''}`}>
                      <MaybeRow o={o} busy={busyId === o.recurring_id} onAnswer={onMaybe} />
                    </li>
                  );
                }
                const amt = shownAmount(o);
                const last = j === balAt;
                const faded = d < today && o.status !== 'late' && o.status !== 'pending';
                const ask = askByKey.get(o.key);
                return (
                  <li key={o.key} id={j === 0 ? dayHeadingId(d) : undefined} tabIndex={j === 0 ? -1 : undefined} className={`cal-lrow cal-k-${o.kind} s-${o.status}${faded ? ' is-past' : ''}${flash === d ? ' is-flash' : ''}`}>
                    <button type="button" className="cal-lrow-btn" data-occ={o.key} onClick={() => onOpen(o)} aria-label={ask ? `${chipLabel(o)} ${askText(ask)}` : chipLabel(o)}>
                      <span className="cal-lrow-date" aria-hidden="true">
                        <span className="cal-lrow-num">{Number(d.slice(8))}</span>
                        <span className="cal-lrow-wd">{weekdayShort(d)}</span>
                      </span>
                      <span className="cal-lrow-main" aria-hidden="true">
                        <span className="cal-lrow-name">{o.name}</span>
                        <span className="cal-lrow-status">{statusWords(o)}</span>
                      </span>
                      <span className="cal-lrow-right" aria-hidden="true">
                        <span className={`cal-lrow-amt num${amt > 0 ? ' is-in' : ''}`}>{o.status === 'skipped' ? <s>{signedAmount(amt)}</s> : signedAmount(amt)}</span>
                        {last && bal !== null && (showBalances || low) && (
                          <span className={`cal-lrow-bal num${low ? ' is-low' : ''}`}>
                            {showBalances ? `Then ${dollars(bal)}` : ''}
                            {low ? (showBalances ? ' · Low' : 'Low') : ''}
                          </span>
                        )}
                      </span>
                    </button>
                    {ask && <PriceAskBox ask={ask} busy={busyId === ask.recurringId} onAnswer={onPrice} />}
                  </li>
                );
              });
            })}
          </ol>
        </section>
      ))}
    </div>
  );
}
