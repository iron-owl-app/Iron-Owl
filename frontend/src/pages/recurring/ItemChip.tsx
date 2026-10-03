import type { DragEvent } from 'react';
import type { ForecastOccurrence } from '../../api';
import { chipLabel, isLeftOut, shownAmount } from './calLib';
import { chipStatus, signedAmount } from './calMath';
import { askText, type PriceAsk } from './priceAsk';

/**
 * One bill, paycheck, card charge or budget plan on the calendar (recurring-v2 design §5): a
 * full-width <button> with a 4px bar in the kind color, the full name (never cut off), and the
 * amount with a short status (✓ Paid, ! Late, ↷ Moved, Skipped). Click opens Item details;
 * upcoming ones can also be dragged to another day. Release 3.19: a bill with a new price to
 * confirm (`ask`) shows a small amber "New price?" tag; Item details has the Yes / No.
 */
export function ItemChip({
  o,
  ask,
  dragging,
  onOpen,
  onDragStart,
  onDragEnd,
}: {
  o: ForecastOccurrence;
  /** Release 3.19: "now charges $X. Update your amount?" waits on this chip. */
  ask?: PriceAsk;
  dragging: boolean;
  onOpen: (o: ForecastOccurrence) => void;
  onDragStart: (o: ForecastOccurrence) => void;
  onDragEnd: () => void;
}) {
  const amt = shownAmount(o);
  const status = chipStatus(o);
  const cls = ['cal-chip', `cal-k-${o.kind}`, `s-${o.status}`, isLeftOut(o) ? 'is-off' : '', dragging ? 'is-dragging' : ''].filter(Boolean).join(' ');

  function start(e: DragEvent<HTMLButtonElement>) {
    if (!o.movable) {
      e.preventDefault();
      return;
    }
    e.stopPropagation();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', o.key);
    onDragStart(o);
  }

  return (
    <button
      type="button"
      className={cls}
      data-occ={o.key}
      draggable={o.movable}
      onDragStart={start}
      onDragEnd={onDragEnd}
      onClick={(e) => {
        e.stopPropagation();
        onOpen(o);
      }}
      aria-label={ask ? `${chipLabel(o)} ${askText(ask)}` : chipLabel(o)}
      title={o.movable ? `${o.name}: click for details, or drag to another day` : `${o.name}: click for details`}
    >
      <span className="cal-chip-name" aria-hidden="true">
        {o.name}
      </span>
      <span className="cal-chip-line" aria-hidden="true">
        <span className={`cal-chip-amt num${amt > 0 ? ' is-in' : ''}`}>{signedAmount(amt)}</span>
        {status && <span className="cal-chip-status">{status}</span>}
      </span>
      {ask && (
        <span className="cal-chip-ask" aria-hidden="true">
          New price?
        </span>
      )}
    </button>
  );
}
