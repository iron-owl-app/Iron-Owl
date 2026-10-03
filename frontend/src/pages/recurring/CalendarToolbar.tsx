import type { CalendarView } from '../../lib/calendarPrefs';
import { Icon } from '../../components/Icon';
import { monthName, monthTitle } from './calLib';

/**
 * The calendar's toolbar (recurring-v2 design §5): ‹ September 2026 › (44px), Back to today
 * when another month is showing, then Calendar · List (not on phones, which always list),
 * Undo (it stays after its message is gone) and Print September on the right.
 */
export function CalendarToolbar({
  month,
  first,
  last,
  view,
  showView,
  undoDepth,
  undoBusy,
  onMonth,
  onView,
  onUndo,
  onPrint,
}: {
  view: CalendarView;
  /** False on phones (always the list). */
  showView: boolean;
  onView: (v: CalendarView) => void;
  month: number;
  first: number;
  last: number;
  undoDepth: number;
  undoBusy: boolean;
  onMonth: (m: number) => void;
  onUndo: () => void;
  onPrint: () => void;
}) {
  return (
    <div className="cal-toolbar">
      <div className="cal-nav">
        <button
          type="button"
          className="cal-arrow"
          onClick={() => onMonth(month - 1)}
          disabled={month <= first}
          aria-label={month > first ? `Previous month, ${monthTitle(month - 1)}` : 'Previous month'}
          title="Previous month"
        >
          <Icon name="chevronLeft" />
        </button>
        <h2 className="cal-title" aria-live="polite">
          {monthTitle(month)}
        </h2>
        <button
          type="button"
          className="cal-arrow"
          onClick={() => onMonth(month + 1)}
          disabled={month >= last}
          aria-label={month < last ? `Next month, ${monthTitle(month + 1)}` : 'Next month'}
          title="Next month"
        >
          <Icon name="chevronRight" />
        </button>
        {month !== first && (
          <button type="button" className="btn cal-today-btn" onClick={() => onMonth(first)}>
            Back to today
          </button>
        )}
      </div>
      <div className="cal-actions">
        {showView && (
          <div className="segmented cal-view" role="group" aria-label="Show as">
            <button type="button" aria-pressed={view === 'calendar'} onClick={() => onView('calendar')}>
              <Icon name="calendar" />
              Calendar
            </button>
            <button type="button" aria-pressed={view === 'list'} onClick={() => onView('list')}>
              <Icon name="table" />
              List
            </button>
          </div>
        )}
        {undoDepth > 0 && (
          <button type="button" className="btn" onClick={onUndo} disabled={undoBusy} title="Undo your last change">
            <Icon name="undo" />
            {undoBusy ? 'Undoing…' : 'Undo'}
          </button>
        )}
        <button type="button" className="btn cal-print-btn" onClick={onPrint}>
          <Icon name="printer" />
          Print {monthName(month)}
        </button>
      </div>
    </div>
  );
}
