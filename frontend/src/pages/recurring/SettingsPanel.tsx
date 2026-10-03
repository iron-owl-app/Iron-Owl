import { useId } from 'react';
import type { ForecastCalendar, ForecastSeries } from '../../api';
import { Icon } from '../../components/Icon';
import { formatMoney } from '../../lib/format';
import { useCalendarPrefs } from '../../lib/calendarPrefs';
import { useMoneyDraft } from '../../lib/useMoneyDraft';
import { bySizeInFirst, cadenceText, money } from './calLib';
import { SidePanel, Switch } from './SidePanel';

/**
 * Bills and paychecks › Settings (README §11): the account, the low-balance warning, what the
 * balance counts and how the calendar looks. Replaces the old side cards and "Change account".
 */
export function SettingsPanel({
  open,
  onClose,
  cal,
  onChangeAccount,
  onThreshold,
  onLowAhead,
  onDaily,
  onToggleSeries,
}: {
  open: boolean;
  onClose: () => void;
  cal: ForecastCalendar;
  onChangeAccount: () => void;
  /** Resolves true when saved. */
  onThreshold: (v: number) => Promise<boolean>;
  onLowAhead: (on: boolean) => void;
  onDaily: (on: boolean) => void;
  onToggleSeries: (s: ForecastSeries, on: boolean) => void;
}) {
  return (
    <SidePanel open={open} title="Settings" onClose={onClose} width={440}>
      <Body cal={cal} onChangeAccount={onChangeAccount} onThreshold={onThreshold} onLowAhead={onLowAhead} onDaily={onDaily} onToggleSeries={onToggleSeries} />
    </SidePanel>
  );
}

function Body({
  cal,
  onChangeAccount,
  onThreshold,
  onLowAhead,
  onDaily,
  onToggleSeries,
}: Omit<Parameters<typeof SettingsPanel>[0], 'open' | 'onClose'>) {
  const uid = useId();
  const [prefs, setPref] = useCalendarPrefs();
  const limit = useMoneyDraft(cal.threshold, onThreshold);
  const acct = cal.account;
  const daily = formatMoney(cal.daily_spend, { cents: cal.daily_spend < 10 });
  const series = cal.series.filter((s) => s.kind !== 'card').sort(bySizeInFirst);

  return (
    <div className="calx-settings">
      <section className="calx-sec" aria-labelledby={`${uid}-acct`}>
        <h3 id={`${uid}-acct`}>Account</h3>
        <p>
          {acct ? (
            <>
              Balances are for{' '}
              <strong>
                {acct.name}
                {acct.mask ? ` ··${acct.mask}` : ''}
              </strong>
              .
            </>
          ) : (
            'No checking account is chosen yet.'
          )}
        </p>
        <button type="button" className="btn calx-btn" onClick={onChangeAccount}>
          Change account
        </button>
      </section>

      <section className="calx-sec" aria-labelledby={`${uid}-low`}>
        <h3 id={`${uid}-low`}>Low-balance warning</h3>
        <label htmlFor={`${uid}-thr`} className="calx-field-label">
          Warn me if checking will drop below
        </label>
        <div className="calx-thr">
          <span className="money-input calx-money">
            <span className="money-prefix" aria-hidden="true">
              $
            </span>
            <input id={`${uid}-thr`} className="input num" {...limit.inputProps} aria-describedby={limit.error ? `${uid}-err` : undefined} />
          </span>
          <span className="calx-saved" aria-live="polite">
            {limit.saved && (
              <>
                <Icon name="check" /> Saved
              </>
            )}
          </span>
        </div>
        {limit.error && (
          <p className="field-error" id={`${uid}-err`} role="alert">
            {limit.error}
          </p>
        )}
        <Switch checked={cal.low_ahead.enabled} onChange={onLowAhead} label={`Tell me on Home ${cal.low_ahead.days} days ahead`} />
      </section>

      <section className="calx-sec" aria-labelledby={`${uid}-counts`}>
        <h3 id={`${uid}-counts`}>What the balance counts</h3>
        <Switch
          checked={cal.include_daily}
          onChange={onDaily}
          label={cal.daily_spend >= 0.005 ? `Include everyday spending (about ${daily} a day)` : 'Include everyday spending'}
          sub={cal.daily_spend >= 0.005 ? undefined : 'Iron Owl hasn’t seen any everyday spending from checking yet.'}
        />
        <p className="calx-note">Card charges don’t change checking until you pay the card. To leave a single item out, open it on the calendar.</p>
        {series.length > 0 && (
          <>
            <h4 className="calx-subhead">Bills and paychecks in the balance</h4>
            <ul className="calx-series" aria-label="Bills and paychecks in the balance">
              {series.map((s) => (
                <li key={s.id}>
                  <Switch
                    checked={s.counted}
                    onChange={(on) => onToggleSeries(s, on)}
                    label={s.name}
                    sub={
                      <>
                        <span className="num">{money(s.amount, s.amount > 0)}</span> · {cadenceText(s)}
                        {s.source === 'spending' ? ' · From your budget' : ''}
                        {s.counted ? '' : ' · Left out of the balance'}
                      </>
                    }
                  />
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="calx-sec" aria-labelledby={`${uid}-look`}>
        <h3 id={`${uid}-look`}>How the calendar looks</h3>
        <span className="calx-field-label" id={`${uid}-week`}>
          Week starts on
        </span>
        <div className="segmented calx-seg" role="group" aria-labelledby={`${uid}-week`}>
          <button type="button" aria-pressed={prefs.weekStart === 0} onClick={() => setPref('weekStart', 0)}>
            Sunday
          </button>
          <button type="button" aria-pressed={prefs.weekStart === 1} onClick={() => setPref('weekStart', 1)}>
            Monday
          </button>
        </div>
        <Switch checked={prefs.showBalances} onChange={(on) => setPref('showBalances', on)} label="Show the balance on each day" />
      </section>
    </div>
  );
}
