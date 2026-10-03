import { useState, type DragEvent, type MouseEvent } from 'react';
import type { ForecastDay, ForecastOccurrence } from '../../api';
import { Icon } from '../../components/Icon';
import { ItemChip } from './ItemChip';
import { MaybeChip } from './MaybeChip';
import { longDay, md, money, weekdayNames, type GridCell } from './calLib';
import { dayCountText, dollars, fadedDay, isAddDoubleClick } from './calMath';
import type { PriceAsk } from './priceAsk';

export const dayHeadingId = (date: string) => `cal-day-${date}`;

/**
 * The big month (recurring-v2 design §5): weekday names (full from 1100px), one <li> per day
 * (136px tall) with its number, a Today pill, a + button, the items, and the end-of-day
 * balance (" · Low" in amber under the limit). Double-click a day, or its +, to add; a
 * double-click on a chip doesn't add. Past days fade unless something there is late. Only
 * today and later accept a dragged chip. "Maybe" bills (Release 3.19, `maybeByDate`) come
 * after a day's items, with their own Yes / No; they never move or open details.
 */
export function MonthGrid({
  cells,
  weekStart,
  longNames,
  today,
  horizonEnd,
  days,
  byDate,
  maybeByDate,
  askByKey,
  busyId,
  showBalances,
  flash,
  onAdd,
  onOpen,
  onDrop,
  onMaybe,
}: {
  cells: GridCell[];
  weekStart: 0 | 1;
  /** Full weekday names (wide screens). */
  longNames: boolean;
  today: string;
  horizonEnd: string;
  days: Map<string, ForecastDay>;
  byDate: Map<string, ForecastOccurrence[]>;
  /** Release 3.19: the calendar's `maybe` rows by date (display only). */
  maybeByDate: Map<string, ForecastOccurrence[]>;
  /** Release 3.19: occurrence key -> "now charges $X" question shown on that chip. */
  askByKey: Map<string, PriceAsk>;
  /** The recurring id being answered (its Yes / No are disabled). */
  busyId: number | null;
  showBalances: boolean;
  flash: string | null;
  onAdd: (date: string) => void;
  onOpen: (o: ForecastOccurrence) => void;
  onDrop: (o: ForecastOccurrence, date: string) => void;
  onMaybe: (o: ForecastOccurrence, yes: boolean) => void;
}) {
  const [dragging, setDragging] = useState<ForecastOccurrence | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const names = weekdayNames(weekStart, longNames);
  const fullNames = weekdayNames(weekStart, true);

  return (
    <div className="cal-month">
      <div className="cal-dow" aria-hidden="true">
        {names.map((n) => (
          <div key={n}>{n}</div>
        ))}
      </div>
      <ol className="cal-days" aria-label={`Days, ${fullNames[0]} to ${fullNames[6]}`}>
        {cells.map((c) => {
          if (!c.inMonth) return <li key={c.date} className="cal-day is-out" aria-hidden="true" />;
          const past = c.date < today;
          const open = !past && c.date <= horizonEnd;
          const day = days.get(c.date);
          const bal = day?.balance ?? null;
          const low = bal !== null && !!day?.below;
          const occ = byDate.get(c.date) ?? [];
          const maybe = maybeByDate.get(c.date) ?? [];
          const isToday = c.date === today;
          const tip = `Add a bill or paycheck on ${md(c.date)}`;
          const cls = [
            'cal-day',
            fadedDay(c.date, today, occ) ? 'is-past' : '',
            isToday ? 'is-today' : '',
            low ? 'is-low' : '',
            open ? 'is-open' : '',
            over === c.date ? 'is-over' : '',
            flash === c.date ? 'is-flash' : '',
          ]
            .filter(Boolean)
            .join(' ');
          const sr = `${longDay(c.date)}${isToday ? ', today' : ''}.${bal !== null ? ` Projected ${money(bal)}${low ? ', below your limit' : ''}.` : ''}${dayCountText(occ.length, maybe.length)}`;

          function dragOver(e: DragEvent<HTMLLIElement>) {
            if (!open || !dragging) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            if (over !== c.date) setOver(c.date);
          }
          function drop(e: DragEvent<HTMLLIElement>) {
            e.preventDefault();
            const o = dragging;
            setDragging(null);
            setOver(null);
            if (o && open && o.date !== c.date) onDrop(o, c.date);
          }
          function dblClick(e: MouseEvent<HTMLLIElement>) {
            // A double-click on a chip (or the +) is not a request to add.
            if (open && isAddDoubleClick(e.target)) onAdd(c.date);
          }

          return (
            <li
              key={c.date}
              className={cls}
              onDoubleClick={dblClick}
              onDragOver={dragOver}
              onDragLeave={() => over === c.date && setOver(null)}
              onDrop={drop}
              title={open ? tip : undefined}
              data-day={c.date}
            >
              <div className="cal-day-top">
                <span className="cal-date" id={dayHeadingId(c.date)} tabIndex={-1}>
                  <span className="cal-date-num" aria-hidden="true">
                    {Number(c.date.slice(8))}
                  </span>
                  <span className="sr-only">{sr}</span>
                </span>
                {isToday && (
                  <span className="cal-today-pill" aria-hidden="true">
                    Today
                  </span>
                )}
                {open && (
                  <button type="button" className="cal-add" onClick={() => onAdd(c.date)} aria-label={tip} title={tip}>
                    <Icon name="plus" />
                  </button>
                )}
              </div>
              {occ.length + maybe.length > 0 && (
                <div className="cal-day-items">
                  {occ.map((o) => (
                    <ItemChip
                      key={o.key}
                      o={o}
                      ask={askByKey.get(o.key)}
                      dragging={dragging?.key === o.key}
                      onOpen={onOpen}
                      onDragStart={setDragging}
                      onDragEnd={() => {
                        setDragging(null);
                        setOver(null);
                      }}
                    />
                  ))}
                  {maybe.map((o) => (
                    <MaybeChip key={o.key} o={o} busy={busyId === o.recurring_id} onAnswer={onMaybe} />
                  ))}
                </div>
              )}
              {bal !== null && (showBalances || low) && (
                <div className={`cal-bal num${low ? ' is-low' : ''}`} aria-hidden="true">
                  {showBalances ? dollars(bal) : ''}
                  {low ? (showBalances ? ' · Low' : 'Low') : ''}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
