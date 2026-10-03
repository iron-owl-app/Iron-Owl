import type { ForecastCalendar, ForecastDip, ForecastOccurrence } from '../../api';
import { Icon } from '../../components/Icon';
import { lowLineText } from './calMath';

/**
 * The one amber line under the summary strip (recurring-v2 design "Banners"): the viewed
 * month's first day below the limit, its cause, and Show day / Move {cause}. Late items have
 * no banner any more: they lead Coming up and show "! Late" on the calendar.
 */
export function LowLine({
  cal,
  dip,
  cause,
  onShowDay,
  onMove,
}: {
  cal: ForecastCalendar;
  dip: ForecastDip | null;
  cause: ForecastOccurrence | null;
  onShowDay: (date: string) => void;
  onMove: (cause: ForecastOccurrence) => void;
}) {
  if (!dip) return null;
  return (
    <div className="cal-lowline" role="status">
      <Icon name="alert" className="cal-lowline-icon" />
      <p>{lowLineText(dip, cause?.name ?? null, cal.threshold)}</p>
      <div className="cal-lowline-actions">
        <button type="button" className="btn" onClick={() => onShowDay(dip.date)}>
          Show day
        </button>
        {cause && cause.movable && (
          <button type="button" className="btn" onClick={() => onMove(cause)}>
            Move {cause.name}
          </button>
        )}
      </div>
    </div>
  );
}
