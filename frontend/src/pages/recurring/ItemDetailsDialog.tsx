import { useEffect, useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type ForecastCalendar, type ForecastDay, type ForecastOccurrence, type ForecastSeries, type RecurringItem, type ReminderDays, type TxnCategory } from '../../api';
import { Modal } from '../../components/Modal';
import { parseMoneyInput } from '../../components/ui';
import { useApi } from '../../lib/useApi';
import { monthOf } from '../../lib/budget';
import { REMIND_OPTIONS, addDays, cadenceText, isMoved, longDay, md, money } from './calLib';
import { canMarkPaid, detailStatus, dollars, moveChoices, signedAmount } from './calMath';
import { budgetCategories } from './AddItemDialog';
import { PriceAskBox } from './PriceAskBox';
import type { PriceAsk } from './priceAsk';

export interface DetailsActions {
  onMove: (o: ForecastOccurrence, to: string) => void;
  onSkip: (o: ForecastOccurrence) => void;
  onPutBack: (o: ForecastOccurrence) => void;
  onMarkPaid: (o: ForecastOccurrence, amount: number | null) => void;
  onUnpay: (o: ForecastOccurrence) => void;
  onReminder: (o: ForecastOccurrence, days: ReminderDays) => void;
  onCategory: (item: RecurringItem, categoryId: string | null) => void;
  onEdit: (item: RecurringItem) => void;
  onRemove: (item: RecurringItem) => void;
  onPlanCounted: (planCategory: string, counted: boolean) => void;
  /** Release 3.19: Yes / No to "now charges $X. Update your amount?". */
  onPrice: (a: PriceAsk, yes: boolean) => void;
}

/**
 * Item details (recurring-v2 design §8): a 520px window with a top border in the kind color,
 * four facts (date, amount, status, balance after this day), then the actions: Mark as paid
 * (not for card charges), Skip this time / Count it again, Move this one to (±10 days, never
 * before today), and "Change every time", which opens the full form. Less common choices
 * (a different amount, reminder, budget category, removing it) sit under "More choices".
 * Release 3.19: a bill with a new price to confirm asks first ("now charges $X", Yes / No).
 */
export function ItemDetailsDialog({
  occ,
  series,
  item,
  ask,
  cal,
  day,
  categories,
  busy,
  onClose,
  actions,
}: {
  occ: ForecastOccurrence | null;
  series: ForecastSeries | undefined;
  item: RecurringItem | undefined;
  /** Release 3.19: the item's open "now charges $X" question. */
  ask?: PriceAsk;
  cal: ForecastCalendar;
  /** The occurrence's day (its projected end-of-day balance). */
  day: ForecastDay | undefined;
  categories: TxnCategory[];
  busy: boolean;
  onClose: () => void;
  actions: DetailsActions;
}) {
  const account = series?.account ?? null;
  const sub = occ
    ? occ.plan_category !== null
      ? 'Planned in your budget'
      : [
          account ? `${occ.amount > 0 ? 'Into ' : account.category === 'credit' ? 'On ' : 'From '}${account.name}${account.mask ? ` ··${account.mask}` : ''}` : occ.kind === 'card' ? 'On a card' : occ.amount > 0 ? 'Into checking' : 'From checking',
          item?.category_name,
        ]
          .filter(Boolean)
          .join(' · ')
    : undefined;
  return (
    <Modal open={!!occ} title={occ?.name ?? ''} subtitle={sub} onClose={onClose} width={520} busy={busy} className={`cal-details-dialog${occ ? ` cal-k-${occ.kind}` : ''}`}>
      {occ && <Body occ={occ} series={series} item={item} ask={ask} cal={cal} day={day} categories={categories} busy={busy} actions={actions} />}
    </Modal>
  );
}

function Body({
  occ,
  series,
  item,
  ask,
  cal,
  day,
  categories,
  busy,
  actions,
}: {
  occ: ForecastOccurrence;
  series: ForecastSeries | undefined;
  item: RecurringItem | undefined;
  ask?: PriceAsk;
  cal: ForecastCalendar;
  day: ForecastDay | undefined;
  categories: TxnCategory[];
  busy: boolean;
  actions: DetailsActions;
}) {
  const uid = useId();
  const id = (k: string) => `${uid}-${k}`;
  const isPlan = occ.plan_category !== null;
  const today = cal.today;
  // Around its usual date; a bill late for more than 10 days gets the days from today on.
  const around = moveChoices(occ.base_date, today, cal.horizon_end);
  const choices = around.length ? around : moveChoices(today, today, cal.horizon_end, occ.base_date);
  const pickDefault = () => (choices.some((c) => c.value === occ.date) ? occ.date : (choices[0]?.value ?? ''));
  const [moveTo, setMoveTo] = useState(pickDefault);
  const [payOpen, setPayOpen] = useState(false);
  const [payText, setPayText] = useState(String(Math.abs(occ.amount)));
  const [payError, setPayError] = useState<string | null>(null);
  useEffect(() => {
    setMoveTo(pickDefault());
    setPayOpen(false);
    setPayText(String(Math.abs(occ.amount)));
    setPayError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [occ.key, occ.date, occ.amount, today]);

  const inOut = occ.amount > 0 ? 'received' : 'paid';
  const canPay = canMarkPaid(occ);
  const canSkip = !isPlan && occ.movable && occ.status === 'upcoming';
  const account = series?.account;
  const cat = item?.category_id ?? occ.category_id;
  const catName = item?.category_name ?? categories.find((c) => c.id === cat)?.name ?? null;
  const late = occ.status === 'late' || occ.status === 'pending';
  const bal = day?.balance ?? null;

  function savePaid() {
    const v = parseMoneyInput(payText);
    if (v === null || Number.isNaN(v) || v <= 0) {
      setPayError('Use a positive amount, like 142.18');
      return;
    }
    setPayError(null);
    actions.onMarkPaid(occ, Math.abs(v - Math.abs(occ.amount)) < 0.005 ? null : v);
  }

  // One extra sentence when the facts alone don't say it.
  let note: string | null = null;
  if (occ.status === 'paid' && occ.actual) {
    const a = occ.actual;
    const diff = Math.abs(a.amount - occ.amount) > 0.004;
    note = `${inOut === 'paid' ? 'Paid' : 'Received'} ${money(Math.abs(a.amount))}${a.date ? ` on ${md(a.date)}` : ''}${diff ? ` (expected ${money(Math.abs(occ.amount))})` : ''}.${a.by === 'you' ? ' You marked this yourself.' : ''}`;
  } else if (late && occ.carried && occ.counted_on) note = `Still counted in your balance on ${md(occ.counted_on)}.`;
  else if (occ.status === 'past') note = 'This date has passed. Iron Owl can’t check bills you added by hand against your bank.';
  else if (occ.kind === 'card' && occ.status === 'upcoming') note = 'Charged to a card, so it doesn’t change checking. Your card payment does.';
  else if (occ.status === 'upcoming' && occ.carried && occ.counted_on) note = `Counted on ${md(occ.counted_on)} until it posts.`;

  const txnHref = `/transactions?q=${encodeURIComponent(occ.name)}&start=${addDays(occ.date, -7)}&end=${addDays(occ.date, 7)}${account ? `&account=${account.id}` : ''}`;

  return (
    <div className="cal-details">
      {ask && <PriceAskBox ask={ask} busy={busy} onAnswer={actions.onPrice} />}
      <dl className="cal-facts">
        <div>
          <dt>Date</dt>
          <dd>{longDay(occ.date)}</dd>
        </div>
        <div>
          <dt>Amount</dt>
          <dd className={`num${occ.amount > 0 ? ' is-in' : ''}`}>{signedAmount(occ.status === 'paid' && occ.actual ? occ.actual.amount : occ.amount)}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd className={occ.status === 'late' ? 'is-late' : ''}>{detailStatus(occ)}</dd>
        </div>
        <div>
          <dt>Balance after this day</dt>
          <dd className="num">{occ.kind === 'card' ? 'No change (on card)' : bal !== null ? dollars(bal) : '—'}</dd>
        </div>
      </dl>
      {note && <p className="cal-details-note">{note}</p>}

      {(canPay || canSkip || occ.status === 'skipped' || (occ.status === 'paid' && occ.actual?.by === 'you') || (isMoved(occ) && occ.status === 'upcoming')) && (
        <div className="cal-detail-actions">
          {canPay && (
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => actions.onMarkPaid(occ, null)}>
              {occ.amount > 0 ? 'Mark received' : 'Mark as paid'}
            </button>
          )}
          {canSkip && (
            <button type="button" className="btn" disabled={busy} onClick={() => actions.onSkip(occ)}>
              Skip this time
            </button>
          )}
          {occ.status === 'skipped' && (
            <button type="button" className="btn" disabled={busy} onClick={() => actions.onPutBack(occ)}>
              Count it again
            </button>
          )}
          {occ.status === 'paid' && occ.actual?.by === 'you' && (
            <button type="button" className="btn" disabled={busy} onClick={() => actions.onUnpay(occ)}>
              Not {inOut} yet
            </button>
          )}
          {isMoved(occ) && occ.status === 'upcoming' && (
            <button type="button" className="btn" disabled={busy} onClick={() => actions.onPutBack(occ)}>
              Put back on {md(occ.base_date)}
            </button>
          )}
        </div>
      )}

      {occ.movable && choices.length > 0 && (
        <div className="cal-move">
          <label className="field-label" htmlFor={id('move')}>
            {occ.date < today ? 'Expect it on' : 'Move this one to'}
          </label>
          <div className="cal-move-row">
            <select id={id('move')} className="select" value={moveTo} onChange={(e) => setMoveTo(e.target.value)} disabled={busy}>
              {choices.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
            <button type="button" className="btn" disabled={busy || !moveTo || moveTo === occ.date} onClick={() => actions.onMove(occ, moveTo)}>
              Move
            </button>
          </div>
          {occ.next_in_series && series?.can_move_all && <p className="field-hint">After moving, you can move every later one too.</p>}
        </div>
      )}

      {isPlan ? (
        <>
          <Link className="cal-link" to={`/spending?cat=${encodeURIComponent(occ.plan_category!)}`}>
            Change this plan on Budget
          </Link>
          {series?.counted !== false ? (
            <button type="button" className="btn cal-quiet" onClick={() => actions.onPlanCounted(occ.plan_category!, false)} disabled={busy}>
              Leave it out of the balance
            </button>
          ) : (
            <button type="button" className="btn" onClick={() => actions.onPlanCounted(occ.plan_category!, true)} disabled={busy}>
              Count it in the balance again
            </button>
          )}
        </>
      ) : (
        item && (
          <button type="button" className="link-btn cal-link cal-change" onClick={() => actions.onEdit(item)} disabled={busy}>
            Change every time (amount, date, how often)
          </button>
        )
      )}

      {!isPlan && (
        <details className="cal-more">
          <summary>More choices</summary>
          <div className="cal-more-body">
            <p className="cal-details-repeat">
              {series ? cadenceText(series) : item ? cadenceText(item) : 'Once'} ·{' '}
              {account ? `${account.name}${account.mask ? ` ··${account.mask}` : ''}${account.category === 'credit' ? ' (card)' : ''}` : 'Checking'} ·{' '}
              {item?.source === 'manual' ? 'added by you' : 'found in your transactions'}
            </p>
            {(occ.status === 'paid' && occ.actual?.by === 'match') || late ? (
              <Link className="cal-link" to={txnHref}>
                {late ? 'Look for it in Transactions' : 'See this payment in Transactions'}
              </Link>
            ) : null}

            {canPay && (
              <div className="cal-pay">
                <button type="button" className="btn" aria-expanded={payOpen} onClick={() => setPayOpen((v) => !v)}>
                  {occ.amount > 0 ? 'Got a different amount?' : 'Paid a different amount?'}
                </button>
                {payOpen && (
                  <>
                    <label className="field-label" htmlFor={id('pay')}>
                      Amount {inOut}
                    </label>
                    <div className="cal-move-row">
                      <span className="money-input">
                        <span className="money-prefix" aria-hidden="true">
                          $
                        </span>
                        <input
                          id={id('pay')}
                          className="input"
                          inputMode="decimal"
                          value={payText}
                          onChange={(e) => setPayText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault();
                              savePaid();
                            }
                          }}
                          aria-invalid={payError ? true : undefined}
                          aria-describedby={payError ? id('pay-err') : undefined}
                        />
                      </span>
                      <button type="button" className="btn btn-primary" disabled={busy} onClick={savePaid}>
                        Save as {inOut}
                      </button>
                    </div>
                    {payError && (
                      <div className="field-error" id={id('pay-err')}>
                        {payError}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}

            {(occ.status === 'upcoming' || late) && (
              <div className="field">
                <span className="field-label" id={id('rem')}>
                  Remind me
                </span>
                <div className="segmented cal-seg" role="group" aria-labelledby={id('rem')} aria-describedby={id('rem-hint')}>
                  {REMIND_OPTIONS.map(([v, l]) => (
                    <button key={v} type="button" aria-pressed={occ.reminder_days === v} disabled={busy} onClick={() => occ.reminder_days !== v && actions.onReminder(occ, v)}>
                      {l}
                    </button>
                  ))}
                </div>
                <p className="field-hint" id={id('rem-hint')}>
                  Reminders show on your Home page when you open Iron Owl. This applies to every {occ.name}.
                </p>
              </div>
            )}

            {item && (
              <div className="cal-budget">
                <div className="field">
                  <label className="field-label" htmlFor={id('cat')}>
                    Budget category
                  </label>
                  <select id={id('cat')} className="select" value={cat ?? ''} disabled={busy} onChange={(e) => actions.onCategory(item, e.target.value || null)}>
                    <option value="">None</option>
                    {budgetCategories(categories, item.amount > 0 ? 'in' : 'out').map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                    {cat && !budgetCategories(categories, item.amount > 0 ? 'in' : 'out').some((c) => c.id === cat) && <option value={cat}>{catName ?? cat}</option>}
                  </select>
                </div>
                {/* Only spending categories have an envelope; income, fixed and transfer ones get no budget line. */}
                {cat && (categories.find((c) => c.id === cat)?.kind ?? 'spending') === 'spending' && <BudgetLine categoryId={cat} name={catName ?? cat} />}
              </div>
            )}

            {item && (
              <button type="button" className="btn cal-quiet" onClick={() => actions.onRemove(item)} disabled={busy}>
                {item.source === 'manual' ? `Delete ${item.name}` : 'Remove from the calendar'}
              </button>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

/** "Budget: Utilities — $250 planned, $213.18 spent, $36.82 left this month [Open budget]" (the Budget page's Planned / Spent / Left). */
function BudgetLine({ categoryId, name }: { categoryId: string; name: string }) {
  const month = monthOf(new Date());
  const bm = useApi(() => api.budgets.get(month), [month]);
  const line = bm.data?.categories.find((c) => c.category === categoryId);
  let text: string;
  if (!bm.data) text = bm.error ? `Budget: ${name}.` : `Budget: ${name} …`;
  else if (!line) text = `Budget: ${name} — not in this month’s budget yet.`;
  else text = `Budget: ${name} — ${money(line.assigned)} planned, ${money(line.spent)} spent, ${money(line.available)} left this month.`;
  return (
    <p className="cal-budget-line">
      {text}{' '}
      <Link to={`/spending?cat=${encodeURIComponent(categoryId)}`} className="cal-link">
        Open budget
      </Link>
    </p>
  );
}
