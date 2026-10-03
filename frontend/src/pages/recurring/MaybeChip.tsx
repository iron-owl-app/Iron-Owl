import type { ForecastOccurrence } from '../../api';
import { maybeLabel, maybeNoLabel, maybeWords, maybeYesLabel, signedAmount, weekdayShort } from './calMath';

/** What hovering a Maybe chip says. */
const TIP = 'Iron Owl thinks this repeats. Yes puts it on your calendar. No, and Iron Owl won’t suggest it again.';

/**
 * Release 3.19: a "Maybe" bill (a suggested item) on the calendar. Faded, with a dashed border
 * and the word "Maybe", never counted. Not a button: clicking it opens nothing (no move, no
 * mark paid); only its Yes / No buttons act. `data-maybe` keeps a double-click on it from
 * adding a bill (`isAddDoubleClick`).
 */
export function MaybeChip({ o, busy, onAnswer }: { o: ForecastOccurrence; busy: boolean; onAnswer: (o: ForecastOccurrence, yes: boolean) => void }) {
  return (
    <div className={`cal-maybe cal-k-${o.kind}`} data-maybe={o.key} role="group" aria-label={maybeLabel(o)} title={TIP}>
      <span className="cal-maybe-tag" aria-hidden="true">
        Maybe
      </span>
      <span className="cal-chip-name" aria-hidden="true">
        {o.name}
      </span>
      <span className={`cal-chip-amt num${o.amount > 0 ? ' is-in' : ''}`} aria-hidden="true">
        {signedAmount(o.amount)}
      </span>
      <Answer o={o} busy={busy} onAnswer={onAnswer} />
    </div>
  );
}

/** The List view's Maybe row: the same row layout, with the question and Yes / No under it. */
export function MaybeRow({ o, busy, onAnswer }: { o: ForecastOccurrence; busy: boolean; onAnswer: (o: ForecastOccurrence, yes: boolean) => void }) {
  return (
    <div className={`cal-maybe-box cal-k-${o.kind}`} data-maybe={o.key} role="group" aria-label={maybeLabel(o)}>
      <span className="cal-lrow-date" aria-hidden="true">
        <span className="cal-lrow-num">{Number(o.date.slice(8))}</span>
        <span className="cal-lrow-wd">{weekdayShort(o.date)}</span>
      </span>
      <span className="cal-lrow-main" aria-hidden="true">
        <span className="cal-lrow-name">
          <span className="cal-maybe-tag">Maybe</span> {o.name}
        </span>
        <span className="cal-lrow-status">{maybeWords(o)}</span>
      </span>
      <span className="cal-lrow-right" aria-hidden="true">
        <span className={`cal-lrow-amt num${o.amount > 0 ? ' is-in' : ''}`}>{signedAmount(o.amount)}</span>
      </span>
      <div className="cal-maybe-ask">
        <span className="cal-maybe-q" aria-hidden="true">
          {o.amount > 0 ? 'Is this repeating income?' : 'Is this a repeating bill?'}
        </span>
        <Answer o={o} busy={busy} onAnswer={onAnswer} />
      </div>
    </div>
  );
}

function Answer({ o, busy, onAnswer }: { o: ForecastOccurrence; busy: boolean; onAnswer: (o: ForecastOccurrence, yes: boolean) => void }) {
  return (
    <div className="cal-maybe-actions">
      <button type="button" className="btn cal-maybe-btn cal-maybe-yes" aria-label={maybeYesLabel(o)} disabled={busy} onClick={() => onAnswer(o, true)}>
        Yes
      </button>
      <button type="button" className="btn cal-maybe-btn" aria-label={maybeNoLabel(o)} disabled={busy} onClick={() => onAnswer(o, false)}>
        No
      </button>
    </div>
  );
}
