import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, ApiError, errorMessage, type DashboardSection, type HomeAlert } from '../api';
import { useApp } from '../state';
import { useToast } from '../components/Toast';
import { Skeleton } from '../components/ui';
import { ErrorPanel } from '../components/ErrorPanel';
import { useBankReauth } from '../components/useBankReauth';
import { useHome } from './home/useHome';
import { HomeHeader } from './home/HomeHeader';
import { NeedsYou, backupReason, describeAlert } from './home/NeedsYou';
import { LeftToSpendCard } from './home/LeftToSpendCard';
import { MoneyCard } from './home/MoneyCard';
import { StatTiles } from './home/StatTiles';
import { ComingUpTable } from './home/ComingUpTable';
import { LeftOverChart } from './home/LeftOverChart';
import { GoalsMini } from './home/GoalsMini';
import { WelcomeCard } from './home/WelcomeCard';
import { CardError } from './home/CardError';
import { answeredText, askFromNeed, yesAmount } from './recurring/priceAsk';
import './home/home.css';

/** Re-render every minute so "Updated 2 minutes ago" and the greeting stay true. */
function useNow(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const h = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(h);
  }, []);
  return now;
}

/**
 * Home (the Dashboard v2 handoff, SPEC "Home v2"): what needs the user first, then a card grid:
 * Left to spend (highlighted), Money you have, three small cards; Coming up, Left over each
 * month and Goals. Brand-new vaults see only the Welcome card.
 */
export function HomePage() {
  const home = useHome();
  const now = useNow();
  const toast = useToast();
  const { sync, syncing, invalidate } = useApp();
  const navigate = useNavigate();
  const [settle, setSettle] = useState<{ key: string; seq: number } | null>(null);
  const settleOn = (key: string) => setSettle((prev) => ({ key, seq: (prev?.seq ?? 0) + 1 }));
  const reauth = useBankReauth({ onDone: (ok, item) => ok && settleOn(`bank_signin:${item.id}`) });
  const [retryingItem, setRetryingItem] = useState<number | null>(null);
  const [backingUp, setBackingUp] = useState(false);
  const [pricing, setPricing] = useState<number | null>(null);

  const d = home.data;

  if (!d) {
    return (
      <div className="home">
        <HomeHeader now={now} showUpdate={false} />
        {home.error ? (
          <ErrorPanel error={home.error} onRetry={home.reload} title="Couldn’t load your Home page" />
        ) : (
          <div className="home-loading" role="status" aria-label="Loading your Home page">
            <Skeleton height={72} style={{ borderRadius: 18 }} />
            <div className="home-row">
              <Skeleton height={300} style={{ borderRadius: 24, flex: '1.4 1 400px' }} />
              <Skeleton height={300} style={{ borderRadius: 24, flex: '1 1 300px' }} />
              <Skeleton height={300} style={{ borderRadius: 24, flex: '1 1 260px' }} />
            </div>
          </div>
        )}
      </div>
    );
  }

  const brandNew = d.setup.visible_accounts === 0 && d.setup.linked_items + d.setup.pending_items === 0;
  const liveBanks = d.banks.filter((b) => b.status !== 'pending');
  const showUpdate = d.setup.plaid_configured && liveBanks.length > 0 && !brandNew;
  const failed = (s: DashboardSection) => d.errors.includes(s);
  // A section that failed server-side (or came back empty) shows "couldn't load … Try again".
  const cardError = (s: DashboardSection, what: string, present = true) =>
    failed(s) || !present ? <CardError what={what} onRetry={home.reload} /> : undefined;

  async function retryBank(itemId: number, bank: string | null) {
    if (!d?.setup.plaid_configured) {
      // No Plaid keys (removed since the bank was linked): same as "Sign in" without keys.
      toast.push({ tone: 'info', title: 'Your Plaid keys aren’t set', body: 'Add them under Bank connection, then try again.' });
      navigate('/settings/banks?focus=bank-connection');
      return;
    }
    setRetryingItem(itemId);
    const results = await sync({ itemId, quiet: 'success' });
    setRetryingItem(null);
    if (results && results.length > 0 && results.every((r) => r.ok)) {
      toast.push({ tone: 'success', title: `${bank ?? 'Your bank'} is working again. Balances are up to date.` });
      settleOn(`bank_error:${itemId}`);
    }
  }

  async function backupNow(folder: string | null) {
    if (backingUp) return;
    setBackingUp(true);
    try {
      const res = await api.autoBackup.run();
      if (res.ok) {
        toast.push({ tone: 'success', title: folder ? `Backup saved to ${folder}.` : 'Backup saved.' });
        settleOn('backup_failed');
      } else {
        const code = res.backup.last_error_code ?? 'other';
        toast.push({ tone: 'error', title: 'The backup still didn’t work', body: `Your data is fine. ${backupReason(code, folder)}` });
      }
      invalidate();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      const body =
        e instanceof ApiError && e.status === 409
          ? 'Choose a backup folder first. It’s under Settings → Safety and backups.'
          : e instanceof ApiError && e.status === 429
            ? 'Iron Owl just tried. Wait a minute, then try again.'
            : errorMessage(e);
      toast.push({ tone: 'error', title: 'Couldn’t back up right now', body });
      // No folder any more (turned off elsewhere): refresh so the stale alert gives way.
      if (e instanceof ApiError && e.status === 409) invalidate();
    } finally {
      setBackingUp(false);
    }
  }

  /** Release 3.19: Yes / No to "Netflix now charges $17.99. Update your amount?". */
  async function answerPrice(a: HomeAlert, yes: boolean) {
    if (a.kind !== 'price_change' || pricing !== null) return;
    const ask = askFromNeed(a.data);
    setPricing(ask.recurringId);
    try {
      if (yes) await api.recurring.update(ask.recurringId, { amount: yesAmount(ask) });
      else await api.recurring.priceAnswer(ask.recurringId, { transaction_id: ask.transactionId, answer: 'no' });
      toast.push({ tone: 'success', title: answeredText(ask, yes) });
      settleOn(a.key);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      toast.push({ tone: 'error', title: `Couldn’t change ${ask.name}`, body: errorMessage(e) });
    } finally {
      setPricing(null);
      invalidate();
    }
  }

  const views = home.alerts.map((a) =>
    describeAlert(a, {
      signInBusy: a.kind === 'bank_signin' && reauth.busyItemId === a.data.item_id ? reauth.phase : null,
      retryBusy: a.kind === 'bank_error' && retryingItem === a.data.item_id,
      backupBusy: backingUp,
      priceBusy: a.kind === 'price_change' && pricing === a.data.recurring_id,
      onSignIn: () => {
        if (a.kind === 'bank_signin') reauth.start({ id: a.data.item_id, kind: a.data.item_kind, institution_name: a.data.institution_name });
      },
      onRetry: () => {
        if (a.kind === 'bank_error' && !syncing) void retryBank(a.data.item_id, a.data.institution_name);
      },
      onBackup: () => {
        if (a.kind === 'backup_failed') void backupNow(a.data.folder_name);
      },
      onPrice: (yes) => void answerPrice(a, yes),
    }),
  );

  return (
    <div className="home">
      <HomeHeader now={now} showUpdate={showUpdate} />
      {brandNew ? (
        <WelcomeCard setup={d.setup} />
      ) : (
        <>
          <NeedsYou views={views} onDismiss={(a) => void home.dismiss(a)} error={cardError('needs', 'what needs you')} settle={settle} />
          <div className="home-row">
            <LeftToSpendCard budget={d.budget} today={d.today} error={cardError('budget', 'your budget', !!d.budget)} />
            <MoneyCard cash={d.cash} banks={d.banks} error={cardError('cash', 'your checking and savings', !!d.cash)} />
            <StatTiles d={d} />
          </div>
          <div className="home-row">
            <ComingUpTable c={d.coming_up} today={d.today} error={cardError('coming_up', 'your bills and paychecks', !!d.coming_up)} />
            <div className="home-col">
              <LeftOverChart months={d.months} error={cardError('months', 'what came in and went out', !!d.months)} />
              <GoalsMini goals={d.goals} error={cardError('goals', 'your goals', !!d.goals)} />
            </div>
          </div>
        </>
      )}
      {reauth.launcher}
    </div>
  );
}
