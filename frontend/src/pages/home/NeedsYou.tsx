import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { BackupErrorCode, HomeAlert } from '../../api';
import { Icon } from '../../components/Icon';
import { formatMoney, plural, toISODate } from '../../lib/format';
import { backupWhen, cadencePhrase, dollars, inDays, moneyAuto, weekdayMonthDay, withMask } from './homeFormat';
import { monthLong } from './dashMath';
import { CaughtUp } from './CaughtUp';
import { askFromNeed, askNote, askTitle, ASK_QUESTION, noLabel, yesLabel } from '../recurring/priceAsk';

/** What an alert's main button does. Links navigate; runs call back into Home; asks are Yes / No (Release 3.19). */
export type AlertAction =
  | { type: 'link'; to: string; label: string }
  | { type: 'run'; label: string; busyLabel: string; busy: boolean; onRun: () => void }
  | { type: 'ask'; yesLabel: string; noLabel: string; busy: boolean; onAnswer: (yes: boolean) => void };

export interface AlertView {
  alert: HomeAlert;
  title: string;
  body: string;
  action: AlertAction;
}

/** Rows shown before "Show N more" (SPEC owner defaults). */
const FIRST = 3;

const BACKUP_REASON: Record<BackupErrorCode, (folder: string) => string> = {
  missing: (f) => `The ${f} folder couldn’t be found, so no copy was saved.`,
  denied: (f) => `Iron Owl wasn’t allowed to save in the ${f} folder, so no copy was saved.`,
  write: (f) => `The copy couldn’t be written to the ${f} folder.`,
  network: (f) => `The ${f} folder couldn’t be reached, so no copy was saved.`,
  other: () => 'Something went wrong, so no copy was saved.',
};

export function backupReason(code: BackupErrorCode, folder: string | null): string {
  return BACKUP_REASON[code](folder ?? 'backup');
}

/** "Rent, Electric and Water"; "Rent, Electric, Water and 2 more" when there are more than it names. */
function joinNames(names: string[], count: number): string {
  const rest = count - names.length;
  const parts = rest > 0 ? [...names, `${rest} more`] : names;
  if (parts.length <= 1) return parts[0] ?? '';
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
}

/** Title, body and button for each kind of need (the design's copy where it has one). */
export function describeAlert(
  a: HomeAlert,
  run: {
    signInBusy: 'opening' | 'updating' | null;
    retryBusy: boolean;
    backupBusy: boolean;
    priceBusy?: boolean;
    onSignIn: () => void;
    onRetry: () => void;
    onBackup: () => void;
    onPrice?: (yes: boolean) => void;
  },
): AlertView {
  switch (a.kind) {
    case 'bank_signin': {
      const bank = a.data.institution_name ?? 'Your bank';
      return {
        alert: a,
        title: `${bank} needs you to sign in again`,
        body:
          a.data.item_kind === 'bank'
            ? 'Until you do, Iron Owl can’t get new purchases or balances from this bank.'
            : `Until you do, Iron Owl can’t get new balances from ${bank}.`,
        action: {
          type: 'run',
          label: `Sign in to ${bank}`,
          busyLabel: run.signInBusy === 'updating' ? 'Updating…' : 'Opening sign-in…',
          busy: run.signInBusy !== null,
          onRun: run.onSignIn,
        },
      };
    }
    case 'bank_error': {
      const bank = a.data.institution_name ?? 'your bank';
      return {
        alert: a,
        title: `Iron Owl couldn’t update from ${bank}`,
        body: 'The bank had a problem last time. This is usually on their side and fixes itself. You can try again now.',
        action: { type: 'run', label: 'Try again', busyLabel: 'Trying again…', busy: run.retryBusy, onRun: run.onRetry },
      };
    }
    case 'finish_setup': {
      const bank = a.data.institution_name ?? 'your bank';
      return {
        alert: a,
        title: `Finish setting up ${bank}`,
        body: 'You signed in to the bank, but haven’t chosen which accounts to bring into Iron Owl yet.',
        action: { type: 'link', to: `/settings/banks?finish=${a.data.item_id}`, label: 'Finish setting up' },
      };
    }
    case 'low_balance': {
      const d = a.data;
      const cause = d.cause ? `, after the ${d.cause.replace(/\s+payment$/i, '')} payment` : '';
      const today = d.date <= toISODate(new Date());
      return {
        alert: a,
        title: today ? `Checking is below ${dollars(d.threshold)}` : `Checking is expected to go below ${dollars(d.threshold)}`,
        body: today ? `Balance now: about ${dollars(d.balance)}.` : `On ${weekdayMonthDay(d.date)}${cause}. Balance then: about ${dollars(d.balance)}.`,
        action: { type: 'link', to: `/recurring?d=${d.date}`, label: 'Show that day' },
      };
    }
    case 'over_plan': {
      const d = a.data;
      if ('count' in d) {
        return {
          alert: a,
          title: `${d.count} categories are over plan`,
          body: `${joinNames(d.names, d.count)}. Together they’re ${moneyAuto(d.total_over)} over.`,
          action: { type: 'link', to: '/spending', label: 'See Budget' },
        };
      }
      return {
        alert: a,
        title: `${d.name} is ${moneyAuto(d.over)} over plan`,
        body: `You planned ${moneyAuto(d.planned)} for ${monthLong(d.month)} and have spent ${moneyAuto(d.spent)}.`,
        action: { type: 'link', to: '/spending', label: 'See Budget' },
      };
    }
    case 'backup_failed': {
      const d = a.data;
      return {
        alert: a,
        title: `${backupWhen(d.at)} backup didn’t work`,
        body: `Your data is fine. ${backupReason(d.code, d.folder_name)}`,
        action: { type: 'run', label: 'Back up now', busyLabel: 'Backing up…', busy: run.backupBusy, onRun: run.onBackup },
      };
    }
    case 'bill_due': {
      const d = a.data;
      const when = inDays(d.days);
      const on = `${formatMoney(d.amount)} on ${weekdayMonthDay(d.date)}`;
      const acct = d.account_name;
      return {
        alert: a,
        title: `${d.name} is due ${when}`,
        body: d.kind === 'card' ? `${on}, on your ${acct ?? 'credit card'}.` : `${on}${acct ? `, from ${acct}` : ''}.`,
        action: { type: 'link', to: `/recurring?d=${d.date}`, label: 'See bills' },
      };
    }
    case 'card_due': {
      const d = a.data;
      const name = withMask(d.name, d.mask);
      const due = weekdayMonthDay(d.due_date);
      return {
        alert: a,
        title: `${name} payment is due ${inDays(d.days)}`,
        body: d.minimum_payment ? `The minimum is ${moneyAuto(d.minimum_payment)}, due ${due}.` : `It’s due ${due}.`,
        action: { type: 'link', to: `/accounts/${d.account_id}`, label: d.account_category === 'credit' ? 'See card' : 'See loan' },
      };
    }
    case 'update_balance': {
      const d = a.data;
      const name = withMask(d.name, d.mask);
      return {
        alert: a,
        title: d.days === null ? `Add a balance for ${name}` : `Update the balance for ${name}`,
        body:
          d.days === null
            ? 'You add this account yourself, and it doesn’t have a balance yet.'
            : `You last updated it ${d.days === 1 ? 'yesterday' : `${d.days} days ago`}. Type in the balance from your latest statement.`,
        action: { type: 'link', to: `/accounts/${d.account_id}`, label: 'Update balance' },
      };
    }
    case 'new_recurring': {
      const d = a.data;
      if ('count' in d) {
        return {
          alert: a,
          title: `${d.count} new repeating charges`,
          body: `${joinNames(d.names, d.count)}. Add them to your bills so they’re part of your plan.`,
          action: { type: 'link', to: '/recurring', label: 'See them' },
        };
      }
      const from = d.account_name ? `, from ${d.account_name}` : '';
      return {
        alert: a,
        title: `New repeating charge: ${d.name}`,
        body: `${formatMoney(d.amount)}${cadencePhrase(d.cadence)}${from}. Add it to your bills so it’s part of your plan.`,
        action: { type: 'link', to: '/recurring', label: 'See it' },
      };
    }
    case 'price_change': {
      const ask = askFromNeed(a.data);
      return {
        alert: a,
        title: `${askTitle(ask)} ${ASK_QUESTION}`,
        body: askNote(ask),
        action: { type: 'ask', yesLabel: yesLabel(ask), noLabel: noLabel(ask), busy: !!run.priceBusy, onAnswer: (yes) => run.onPrice?.(yes) },
      };
    }
    case 'needs_category': {
      const n = a.data.count;
      return {
        alert: a,
        title: `${plural(n, 'purchase')} this month need${n === 1 ? 's' : ''} a category`,
        body: 'Sorting them keeps “Left to spend” accurate.',
        action: { type: 'link', to: `/transactions?view=needs_category&start=${a.data.month_start}`, label: 'Sort them' },
      };
    }
    case 'big_purchase': {
      const d = a.data;
      const card = d.account_name ? `, with ${withMask(d.account_name, d.account_mask)}` : '';
      return {
        alert: a,
        title: `Was this you? ${formatMoney(d.amount)} at ${d.name}`,
        body: `Bought on ${weekdayMonthDay(d.date)}${card}. If it wasn’t you, call your bank.`,
        action: { type: 'link', to: `/transactions?start=${d.date}&end=${d.date}`, label: 'See it' },
      };
    }
    case 'recovery_sheet': {
      const to = '/settings?focus=recovery-sheet&make=1';
      if (a.data.status === 'stale') {
        return {
          alert: a,
          title: 'Make a new recovery sheet',
          body: 'Your recovery sheet may not work any more. A new one takes 2 minutes and one sheet of paper.',
          action: { type: 'link', to, label: 'Make one' },
        };
      }
      if (a.data.status === 'unconfirmed') {
        return {
          alert: a,
          title: 'Is your recovery sheet on paper?',
          body: 'Setting it up wasn’t finished. If you didn’t print it or write it down, make a new one. It takes 2 minutes.',
          action: { type: 'link', to, label: 'Make one' },
        };
      }
      return {
        alert: a,
        title: 'Add a recovery sheet',
        body: 'So you can get back in if you forget your password. It takes 2 minutes and one sheet of paper.',
        action: { type: 'link', to, label: 'Make one' },
      };
    }
  }
}

/**
 * "Things that need you": the first 3, most urgent first, then "Show N more"; or the green
 * "You're all caught up". After "Not now", focus moves to the next row (or the previous one,
 * or the green strip).
 */
export function NeedsYou({
  views,
  onDismiss,
  error,
  settle,
}: {
  views: AlertView[];
  onDismiss: (a: HomeAlert) => void;
  error?: ReactNode;
  /** An action just fixed this alert (sign-in, backup…): once its row is gone, move focus to the next one. */
  settle?: { key: string; seq: number } | null;
}) {
  const headId = useId();
  const listId = useId();
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const okRef = useRef<HTMLDivElement>(null);
  const [focusNext, setFocusNext] = useState<string | 'ok' | null>(null);
  const [expanded, setExpanded] = useState(false);
  const lastIndex = useRef(new Map<string, number>());
  const settled = useRef(0);

  useLayoutEffect(() => {
    views.forEach((v, i) => lastIndex.current.set(v.alert.key, i));
  }, [views]);

  useEffect(() => {
    if (!settle || settle.seq === settled.current) return;
    if (views.some((v) => v.alert.key === settle.key)) return; // still there: wait for the refetch
    settled.current = settle.seq;
    // Only when focus was lost with the removed row (don't pull focus from somewhere else).
    const active = document.activeElement;
    if (active && active !== document.body) return;
    const i = lastIndex.current.get(settle.key) ?? 0;
    const next = views[i] ?? views[i - 1];
    setFocusNext(next ? next.alert.key : 'ok');
  }, [settle, views]);

  useLayoutEffect(() => {
    if (focusNext === null) return;
    const el = focusNext === 'ok' ? okRef.current : rowRefs.current.get(focusNext);
    if (el) {
      el.focus({ preventScroll: false });
      setFocusNext(null);
    }
  }, [focusNext, views, expanded]);

  // If the target never appears (it was refetched away), give up after a moment.
  useEffect(() => {
    if (focusNext === null) return;
    const h = window.setTimeout(() => setFocusNext(null), 1500);
    return () => window.clearTimeout(h);
  }, [focusNext]);

  function dismiss(i: number) {
    const a = views[i]!.alert;
    const next = views[i + 1] ?? views[i - 1];
    setFocusNext(next ? next.alert.key : 'ok');
    onDismiss(a);
  }

  const n = views.length;
  const shown = expanded ? views : views.slice(0, FIRST);
  const hidden = n - shown.length;

  if (n === 0 && !error) {
    return (
      <section className="home-needs" aria-labelledby={headId}>
        <h2 id={headId} className="sr-only">
          Things that need you
        </h2>
        <CaughtUp ref={okRef} />
      </section>
    );
  }

  return (
    <section className="home-needs" aria-labelledby={headId}>
      <h2 id={headId} className={n ? 'home-needs-title' : 'sr-only'}>
        {n ? `${plural(n, 'thing')} need${n === 1 ? 's' : ''} you` : 'Things that need you'}
      </h2>
      {error}
      <div className="home-needs-list" id={listId}>
        {shown.map((v, i) => (
          <AlertRow
            key={v.alert.key}
            view={v}
            rowRef={(el) => {
              if (el) rowRefs.current.set(v.alert.key, el);
              else rowRefs.current.delete(v.alert.key);
            }}
            onDismiss={() => dismiss(i)}
          />
        ))}
      </div>
      {n > FIRST && (
        <button
          type="button"
          className="home-btn home-btn-quiet home-needs-more"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => {
            if (!expanded) {
              const first = views[FIRST];
              if (first) setFocusNext(first.alert.key);
            }
            setExpanded((x) => !x);
          }}
        >
          {expanded ? 'Show fewer' : `Show ${hidden} more`}
        </button>
      )}
    </section>
  );
}

function AlertRow({ view, rowRef, onDismiss }: { view: AlertView; rowRef: (el: HTMLDivElement | null) => void; onDismiss: () => void }) {
  const titleId = useId();
  const { alert: a, action } = view;
  return (
    <div className={`home-alert tone-${a.tone}`} ref={rowRef} tabIndex={-1} role="group" aria-labelledby={titleId}>
      <div className="home-alert-text">
        <div className="home-alert-title" id={titleId}>
          {view.title}
        </div>
        <div className="home-alert-body">{view.body}</div>
      </div>
      <div className="home-alert-actions">
        {action.type === 'link' ? (
          <Link to={action.to} className="home-btn home-btn-strong">
            {action.label}
          </Link>
        ) : action.type === 'ask' ? (
          <>
            <button
              type="button"
              className="home-btn home-btn-strong home-btn-ask"
              aria-label={action.yesLabel}
              aria-disabled={action.busy || undefined}
              onClick={() => {
                if (!action.busy) action.onAnswer(true);
              }}
            >
              {action.busy && <Icon name="sync" className="spin" />}
              Yes
            </button>
            <button
              type="button"
              className="home-btn home-btn-quiet home-btn-ask"
              aria-label={action.noLabel}
              aria-disabled={action.busy || undefined}
              onClick={() => {
                if (!action.busy) action.onAnswer(false);
              }}
            >
              No
            </button>
          </>
        ) : (
          <button
            type="button"
            className="home-btn home-btn-strong"
            onClick={() => {
              if (!action.busy) action.onRun();
            }}
            aria-disabled={action.busy || undefined}
          >
            {action.busy && <Icon name="sync" className="spin" />}
            {action.busy ? action.busyLabel : action.label}
          </button>
        )}
        {a.dismissible && (
          <button type="button" className="home-btn home-btn-quiet" onClick={onDismiss} aria-label={`Not now: ${view.title}`}>
            Not now
          </button>
        )}
      </div>
    </div>
  );
}
