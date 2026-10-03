import type { ForecastCalendar, ForecastOccurrence } from '../../api';
import { monthName } from './calLib';
import { billsLeftSub, dollars, lowestSub, nextPaySub, signedAmount, type BillsLeft, type LowestPoint } from './calMath';
import { NO_PAYCHECK_TEXT, NO_PAYCHECK_TITLE } from './payPeriods';

/**
 * The four tinted cards under the title (recurring-v2 design §2, SPEC 3.15 "Numbers"): in
 * checking today, next paycheck, bills left in the viewed month, and the lowest point from
 * tomorrow to the month's end (amber when under the limit).
 */
export function SummaryStrip({
  cal,
  month,
  thisMonth,
  nextPay,
  bills,
  lowest,
  lowestMonth,
}: {
  cal: ForecastCalendar;
  month: number;
  /** The viewed month is today's month. */
  thisMonth: boolean;
  nextPay: ForecastOccurrence | null;
  bills: BillsLeft;
  lowest: LowestPoint | null;
  /** Set when the viewed month has no days left: the lowest point is in this month instead. */
  lowestMonth: number | null;
}) {
  const acct = cal.account;
  return (
    <section className="cal-strip" aria-label="Summary">
      <div className="cal-stat cal-tint cal-k-check">
        <h2 className="cal-stat-label">In checking today</h2>
        <p className="cal-stat-value num">{dollars(cal.balance)}</p>
        <p className="cal-stat-sub">{acct ? `${acct.name}${acct.mask ? ` ··${acct.mask}` : ''}` : 'Checking'}</p>
      </div>
      <div className="cal-stat cal-tint cal-k-in">
        <h2 className="cal-stat-label">Next paycheck</h2>
        {nextPay ? (
          <>
            <p className="cal-stat-value num">{signedAmount(nextPay.amount)}</p>
            <p className="cal-stat-sub">{nextPaySub(nextPay.date, cal.today)}</p>
          </>
        ) : (
          <>
            <p className="cal-stat-value is-words">{NO_PAYCHECK_TITLE}</p>
            <p className="cal-stat-sub">{NO_PAYCHECK_TEXT}</p>
          </>
        )}
      </div>
      <div className="cal-stat cal-tint cal-k-out">
        <h2 className="cal-stat-label">{thisMonth ? 'Bills left this month' : `Bills in ${monthName(month)}`}</h2>
        <p className="cal-stat-value num">{dollars(bills.total)}</p>
        <p className="cal-stat-sub">{billsLeftSub(bills, thisMonth)}</p>
      </div>
      <div className={`cal-stat cal-tint ${lowest?.below ? 'cal-k-amber' : 'cal-k-subs'}`}>
        <h2 className="cal-stat-label">{lowestMonth !== null ? `Lowest point in ${monthName(lowestMonth)}` : 'Lowest point'}</h2>
        <p className={`cal-stat-value num${lowest?.below ? ' is-low' : ''}`}>{lowest ? dollars(lowest.balance) : '—'}</p>
        <p className="cal-stat-sub">{lowest ? lowestSub(lowest, cal.threshold) : 'No days left to show'}</p>
      </div>
    </section>
  );
}
