import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { api, type AlertKey, type AlertSetting, type AlertsPatch } from '../../api';
import { useApp } from '../../state';
import { useApi } from '../../lib/useApi';
import { formatDateSmart, toISODate } from '../../lib/format';
import { Icon } from '../../components/Icon';
import { Skeleton } from '../../components/ui';
import { useMoneyDraft } from '../../lib/useMoneyDraft';
import { StSwitch, useFail, useSay } from './parts';

/** The rows (design D7 tab 4, plus Release 3.19's "Amount changes") and the alert_settings keys each one sets (SPEC "D7 update 2"). */
interface Row {
  id: keyof AlertsPatch;
  keys: AlertKey[];
  name: string;
  /** The key whose value the row's $ box edits. */
  valueKey?: 'low' | 'big';
  sentence: (value: ReactNode, s: Map<AlertKey, AlertSetting>) => ReactNode;
  note?: string;
}

const ROWS: Row[] = [
  {
    id: 'low',
    keys: ['low', 'low_ahead'],
    name: 'Low balance',
    valueKey: 'low',
    sentence: (v, s) => (
      <>
        <span>When checking may go under</span>
        {v}
        <span>in the next {s.get('low_ahead')?.value ?? 7} days</span>
      </>
    ),
    note: 'Those days also show as Low in Bills and paychecks.',
  },
  {
    id: 'big',
    keys: ['big'],
    name: 'Large purchase',
    valueKey: 'big',
    sentence: (v) => (
      <>
        <span>When one purchase is over</span>
        {v}
      </>
    ),
  },
  {
    id: 'budget',
    keys: ['budget'],
    name: 'Category over plan',
    sentence: () => <span>When a category goes over what you planned for the month</span>,
    note: 'Bills paid exactly as planned don’t count, so those won’t send alerts.',
  },
  {
    id: 'reminder',
    keys: ['reminder', 'due'],
    name: 'Bill due soon',
    sentence: () => <span>Remind me before a bill is due.</span>,
    note: 'You pick which bills, and how many days before, in Bills and paychecks.',
  },
  {
    id: 'newrec',
    keys: ['newrec'],
    name: 'New repeating charge',
    sentence: () => <span>When a charge starts repeating that isn’t on your Bills list</span>,
    note: 'Often a free trial that turned into a subscription.',
  },
  {
    id: 'price',
    keys: ['price'],
    name: 'Amount changes',
    sentence: () => <span>When a bill or paycheck comes in at a different amount than the one you set</span>,
    note: 'Iron Owl asks before changing an amount you typed yourself.',
  },
];

/**
 * Settings › Alerts (design D7 tab 4, replaces the alerts half of Rules & alerts). No email
 * (owner decision): each alert is On or Off, with its amount where it has one. Two rows set
 * two keys at once (Low balance: `low` + `low_ahead`; Bill due soon: `reminder` + `due`).
 * Then the bill reminders (read-only, set in Bills and paychecks) and the recent alerts.
 */
export function AlertsTab() {
  const fail = useFail();
  const settings = useApi(() => api.alerts.settings(), []);
  const uid = useId();
  const byKey = new Map((settings.data ?? []).map((s) => [s.key, s]));

  async function save(body: AlertsPatch): Promise<boolean> {
    const prev = settings.data;
    // Show the change at once (a two-key row changes both keys, like the server does).
    const touched = (k: AlertKey) => (k === 'low_ahead' ? body.low : k === 'due' ? body.reminder : body[k as keyof AlertsPatch]);
    settings.setData((p) =>
      (p ?? []).map((s): AlertSetting => {
        const t: { enabled?: boolean; value?: number } | undefined = touched(s.key);
        if (!t) return s;
        return {
          ...s,
          enabled: t.enabled ?? s.enabled,
          value: (s.key === 'low' || s.key === 'big') && t.value !== undefined ? t.value : s.value,
        };
      }),
    );
    try {
      settings.setData(await api.alerts.updateMany(body));
      return true;
    } catch (e) {
      if (prev) settings.setData(prev);
      fail('Couldn’t save the alert', e);
      return false;
    }
  }

  return (
    <>
      <section className="st-card" aria-labelledby={`${uid}-h`}>
        <div className="st-card-head">
          <div className="st-card-head-text">
            <h2 id={`${uid}-h`}>Alerts</h2>
            <p className="st-desc">Alerts show at the top of your Home screen when something needs a look.</p>
          </div>
        </div>
        {settings.loading && !settings.data ? (
          <div className="st-loading" aria-busy="true" aria-label="Loading alerts">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} height={64} style={{ borderRadius: 10 }} />
            ))}
          </div>
        ) : settings.error && !settings.data ? (
          <p className="st-error">
            Iron Owl couldn’t load your alerts.{' '}
            <button type="button" className="st-link st-link-inline" onClick={settings.reload}>
              Try again
            </button>
          </p>
        ) : (
          ROWS.filter((r) => r.keys.some((k) => byKey.has(k))).map((r) => <AlertRow key={r.id} row={r} byKey={byKey} onSave={save} />)
        )}
      </section>
      <BillReminders reminderOn={byKey.get('reminder')?.enabled ?? true} />
      <RecentAlerts />
    </>
  );
}

function AlertRow({ row, byKey, onSave }: { row: Row; byKey: Map<AlertKey, AlertSetting>; onSave: (p: AlertsPatch) => Promise<boolean> }) {
  const uid = useId();
  const on = row.keys.some((k) => byKey.get(k)?.enabled);
  const valueSetting = row.valueKey ? byKey.get(row.valueKey) : undefined;
  const money = useMoneyDraft(valueSetting?.value ?? 0, (v) => onSave({ [row.valueKey!]: { value: v } }));
  const toggle = (next: boolean) => void onSave({ [row.id]: { enabled: next } });

  const box = valueSetting ? (
    <span className="st-money st-money-alert">
      <span aria-hidden="true">$</span>
      <input className="st-input" aria-label={`${row.name} amount`} {...money.inputProps} aria-describedby={money.error ? `${uid}-err` : undefined} />
    </span>
  ) : null;

  return (
    <div className="st-row st-alert-row">
      <div className={`st-alert-main${on ? '' : ' is-off'}`}>
        <span className="st-alert-name" id={`${uid}-name`}>
          {row.name}
        </span>
        <span className="st-alert-sentence">
          {row.sentence(box, byKey)}
          {money.saved && (
            <span className="st-saved" role="status">
              <Icon name="check" /> Saved
            </span>
          )}
        </span>
        {money.error && (
          <span className="st-alert is-plain" id={`${uid}-err`} role="alert">
            {money.error}
          </span>
        )}
        {row.note && <span className="st-help-faint">{row.note}</span>}
      </div>
      <StSwitch checked={on} onChange={toggle} label={row.name} />
    </div>
  );
}

/** The bills with a reminder (set per bill in Bills and paychecks), read-only here. */
function BillReminders({ reminderOn }: { reminderOn: boolean }) {
  const uid = useId();
  const items = useApi(() => api.recurring.list(), []);
  const list = (items.data ?? []).filter((i) => i.status === 'active' && i.reminder_days > 0).sort((a, b) => a.name.localeCompare(b.name));
  return (
    <section className="st-card" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Bill reminders</h2>
          <p className="st-desc">Iron Owl reminds you on the Home screen before these bills are due.</p>
        </div>
      </div>
      <div className="st-row is-stack is-last">
        <div className="st-rems-head">
          <span className="st-label">Bills with a reminder</span>
          <Link to="/recurring" className="st-link">
            Change in Bills and paychecks →
          </Link>
        </div>
        {!reminderOn && <p className="st-warn-line">Bill due soon is off above, so these reminders won’t show.</p>}
        {items.loading && !items.data ? (
          <Skeleton height={46} style={{ borderRadius: 10 }} />
        ) : list.length === 0 ? (
          <p className="st-help">No bill reminders yet. In Bills and paychecks, click a bill on the calendar and pick when to be reminded.</p>
        ) : (
          <div className="st-rems">
            {list.map((i) => (
              <div className="st-rem" key={i.id}>
                <span>{i.name}</span>
                <span>Reminder {i.reminder_days === 1 ? '1 day' : `${i.reminder_days} days`} before</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

const SEVERITY_LABEL = { warn: 'Warning', neg: 'Needs attention', accent: 'Notice' } as const;

function dateLabel(d: string): string {
  const iso = d.length > 10 ? toISODate(new Date(d)) : d;
  return formatDateSmart(iso);
}

/** The alert history (moved here from the old Rules & alerts page). Opening it marks them read. */
function RecentAlerts() {
  const say = useSay();
  const fail = useFail();
  const { invalidate } = useApp();
  const uid = useId();
  const events = useApi(() => api.alerts.events(50), []);
  const [clearing, setClearing] = useState(false);
  const newIds = useRef<Set<number> | null>(null);
  const marked = useRef(false);

  useEffect(() => {
    if (!events.data || marked.current) return;
    marked.current = true;
    const unread = events.data.filter((e) => !e.read);
    newIds.current = new Set(unread.map((e) => e.id));
    if (unread.length) {
      api.alerts
        .markRead()
        .then(() => invalidate())
        .catch(() => {
          /* not critical: they stay unread */
        });
    }
  }, [events.data, invalidate]);

  async function clearAll() {
    setClearing(true);
    try {
      await api.alerts.clear();
      events.setData([]);
      invalidate();
      say('Recent alerts were cleared.');
    } catch (e) {
      fail('Couldn’t clear the alerts', e);
    } finally {
      setClearing(false);
    }
  }

  const list = events.data ?? [];
  return (
    <section className="st-card" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Recent alerts</h2>
          <p className="st-desc">What Iron Owl noticed lately, newest first.</p>
        </div>
        {list.length > 0 && (
          <button type="button" className="st-btn st-btn-plain st-btn-sm" onClick={() => void clearAll()} disabled={clearing}>
            {clearing ? 'Clearing…' : 'Clear all'}
          </button>
        )}
      </div>
      {events.loading && !events.data ? (
        <div className="st-loading" aria-busy="true" aria-label="Loading alerts">
          <Skeleton height={56} style={{ borderRadius: 10 }} />
        </div>
      ) : list.length === 0 ? (
        <p className="st-empty">You’re all caught up.</p>
      ) : (
        <ul className="st-feed">
          {list.map((e) => {
            const isNew = newIds.current?.has(e.id) ?? !e.read;
            return (
              <li key={e.id} className="st-item">
                <span className={`feed-bell is-${e.severity}`} aria-hidden="true">
                  <Icon name="bell" />
                </span>
                <span className="st-item-main">
                  <span className="st-item-name">
                    <span className="sr-only">
                      {SEVERITY_LABEL[e.severity]}
                      {isNew ? ', new' : ''}:{' '}
                    </span>
                    {e.title}
                    {isNew && <span className="feed-new" aria-hidden="true" />}
                  </span>
                  <span className="st-item-sub">{e.body}</span>
                  <span className="st-help-faint">{dateLabel(e.created_at)}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
