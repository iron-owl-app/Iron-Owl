import './shared-parts.css';

const shiftMonth = (m: string, n: number): string => {
  const y = Number(m.slice(0, 4));
  const mo = Number(m.slice(5, 7)) - 1 + n;
  const d = new Date(y, mo, 1, 12);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
};

const labelFmt = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' });
/** "September 2026". */
export function monthYearLabel(m: string): string {
  return labelFmt.format(new Date(Number(m.slice(0, 4)), Number(m.slice(5, 7)) - 1, 1, 12));
}

/**
 * ‹ September 2026 › (Spending design): 44px buttons around the month name, which is read out
 * when it changes. Each end is dimmed and disabled. Months are 'YYYY-MM'.
 */
export function MonthStepper({
  month,
  min,
  max,
  onChange,
  label = 'Month',
}: {
  month: string;
  min: string;
  max: string;
  onChange: (month: string) => void;
  label?: string;
}) {
  const atStart = month <= min;
  const atEnd = month >= max;
  const prev = shiftMonth(month, -1);
  const next = shiftMonth(month, 1);
  return (
    <div role="group" aria-label={label} className="month-stepper">
      <button
        type="button"
        className="month-stepper-btn"
        onClick={() => onChange(prev)}
        disabled={atStart}
        aria-label={atStart ? 'Previous month (this is the first month Iron Owl has)' : `Previous month, ${monthYearLabel(prev)}`}
      >
        <span aria-hidden="true">‹</span>
      </button>
      <span className="month-stepper-label" aria-live="polite">
        {monthYearLabel(month)}
      </span>
      <button
        type="button"
        className="month-stepper-btn"
        onClick={() => onChange(next)}
        disabled={atEnd}
        aria-label={atEnd ? 'Next month (this is the latest month)' : `Next month, ${monthYearLabel(next)}`}
      >
        <span aria-hidden="true">›</span>
      </button>
    </div>
  );
}
