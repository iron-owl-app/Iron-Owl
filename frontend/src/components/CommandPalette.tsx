import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, errorMessage, saveBlob, type Account, type Transaction, type TransactionQuery } from '../api';
import { useApp } from '../state';
import { CATEGORY_SINGULAR, formatDateSmart, toISODate } from '../lib/format';
import { Icon, type IconName } from './Icon';
import { Avatar, Money } from './ui';
import { useToast } from './Toast';

// Mounted once in Layout. Opens on Ctrl/⌘+K and on the `fintrack:open-palette`
// window event (dispatched by the sidebar search button via `openCommandPalette()`).

export const OPEN_PALETTE_EVENT = 'fintrack:open-palette';

export function openCommandPalette() {
  window.dispatchEvent(new Event(OPEN_PALETTE_EVENT));
}

type Item = {
  key: string;
  group: 'Actions' | 'Pages' | 'Accounts' | 'Transactions';
  label: string;
  sub?: string;
  icon?: IconName;
  lead?: ReactNode;
  trail?: ReactNode;
  keywords?: string;
  run: () => void;
};

const PAGES: { to: string; label: string; icon: IconName; keywords: string }[] = [
  { to: '/', label: 'Home', icon: 'dashboard', keywords: 'dashboard today net worth overview' },
  { to: '/accounts', label: 'Accounts', icon: 'wallet', keywords: 'balances banks cards' },
  { to: '/transactions', label: 'Transactions', icon: 'arrows', keywords: 'activity spending history' },
  { to: '/spending', label: 'Budget', icon: 'receipt', keywords: 'budgets plan review month envelopes' },
  { to: '/reports', label: 'Reports', icon: 'chart', keywords: 'overview charts where it went habits' },
  { to: '/reports?tab=spending', label: 'Spending', icon: 'pie', keywords: 'spending where money went categories month' },
  { to: '/reports?tab=debt', label: 'Paying off debt', icon: 'percent', keywords: 'mortgage debt credit cards apr payoff loans' },
  { to: '/recurring', label: 'Bills and paychecks', icon: 'repeat', keywords: 'recurring calendar subscriptions forecast cash paycheck income' },
  { to: '/goals', label: 'Goals', icon: 'target', keywords: 'savings payoff debt emergency fund' },
  { to: '/investments', label: 'Investments', icon: 'trend', keywords: 'holdings portfolio 401k hsa brokerage' },
  { to: '/settings?tab=rules', label: 'Rules', icon: 'rule', keywords: 'categorize automatic sorting rules' },
  { to: '/settings?tab=alerts', label: 'Alerts', icon: 'bell', keywords: 'notifications alerts reminders low balance' },
  { to: '/settings', label: 'Settings', icon: 'sliders', keywords: 'categories paychecks backup restore password security text size colors theme dark light warm night updates' },
  { to: '/settings/banks', label: 'Manage banks', icon: 'bank', keywords: 'plaid connections link bank sync keys' },
  { to: '/help', label: 'Help', icon: 'help', keywords: 'how to questions support who to call install plaid keys recovery sheet backups updates' },
];

/** A page entry with a query (`/reports?tab=debt`) matches exactly; a plain one matches its path unless another entry matches exactly. */
function isCurrentPage(to: string, pathname: string, search: string): boolean {
  const here = `${pathname}${search}`;
  if (to.includes('?')) return here === to;
  return pathname === to && !PAGES.some((o) => o.to.includes('?') && o.to === here);
}

/**
 * Fuzzy score: every query character must appear in order. Rewards prefix and
 * word-start hits and consecutive runs. -1 = no match.
 */
export function fuzzyScore(query: string, text: string): number {
  const q = query.toLowerCase().replace(/\s+/g, ' ').trim();
  if (!q) return 0;
  const t = text.toLowerCase();
  const direct = t.indexOf(q);
  if (direct >= 0) return 1000 - direct * 2 + (direct === 0 || /\W/.test(t[direct - 1] ?? ' ') ? 200 : 0);
  let score = 0;
  let ti = 0;
  let run = 0;
  for (const ch of q) {
    if (ch === ' ') continue;
    const found = t.indexOf(ch, ti);
    if (found < 0) return -1;
    const wordStart = found === 0 || /\W/.test(t[found - 1] ?? ' ');
    run = found === ti ? run + 1 : 0;
    score += 10 + run * 6 + (wordStart ? 14 : 0) - Math.min(found - ti, 10);
    ti = found + 1;
  }
  return score;
}

function best(q: string, ...fields: (string | null | undefined)[]): number {
  let s = -1;
  for (const f of fields) if (f) s = Math.max(s, fuzzyScore(q, f));
  return s;
}

export function CommandPalette() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const { sync, lockNow, plaid } = useApp();
  const uid = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [txns, setTxns] = useState<{ q: string; items: Transaction[] } | null>(null);
  const [txnLoading, setTxnLoading] = useState(false);
  const txnReq = useRef(0);

  const show = useCallback(() => {
    if (dialogRef.current?.open) {
      inputRef.current?.select();
      return;
    }
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery('');
    setTxns(null);
    setActive(0);
    setOpen(true);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
  }, []);

  // Open/close the native dialog; restore focus to where it was.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (open && !d.open) {
      d.showModal();
      inputRef.current?.focus();
    } else if (!open && d.open) {
      d.close();
      const el = returnFocus.current;
      returnFocus.current = null;
      if (el && el.isConnected) requestAnimationFrame(() => el.focus());
    }
  }, [open]);

  // Global shortcut + the sidebar button's event.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
        e.preventDefault();
        if (dialogRef.current?.open) close();
        else show();
      }
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener(OPEN_PALETTE_EVENT, show);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener(OPEN_PALETTE_EVENT, show);
    };
  }, [show, close]);

  // Fresh account list each time it opens.
  useEffect(() => {
    if (!open) return;
    let live = true;
    api.accounts
      .list()
      .then((a) => live && setAccounts(a))
      .catch(() => {
        /* the palette still works without accounts */
      });
    return () => {
      live = false;
    };
  }, [open]);

  // Debounced transaction search.
  useEffect(() => {
    if (!open) return;
    const q = query.trim();
    if (q.length < 2) {
      setTxns(null);
      setTxnLoading(false);
      return;
    }
    setTxnLoading(true);
    const id = ++txnReq.current;
    const h = window.setTimeout(() => {
      api
        .transactions({ search: q, limit: 8 })
        .then((page) => {
          if (id === txnReq.current) setTxns({ q, items: page.items });
        })
        .catch(() => {
          if (id === txnReq.current) setTxns({ q, items: [] });
        })
        .finally(() => {
          if (id === txnReq.current) setTxnLoading(false);
        });
    }, 220);
    return () => window.clearTimeout(h);
  }, [query, open]);

  const go = useCallback(
    (to: string) => {
      close();
      navigate(to);
    },
    [close, navigate],
  );

  const exportCsv = useCallback(async () => {
    close();
    // On the Transactions page, export what's on screen (its filters); elsewhere, everything.
    const q: TransactionQuery = {};
    if (location.pathname === '/transactions') {
      const p = new URLSearchParams(location.search);
      if (p.get('account')) q.account_id = Number(p.get('account'));
      if (p.get('q')) q.search = p.get('q')!;
      if (p.get('start')) q.start = p.get('start')!;
      if (p.get('end')) q.end = p.get('end')!;
      if (p.get('cat')) q.category = p.get('cat')!;
      const tag = Number(p.get('tag'));
      if (Number.isInteger(tag) && tag > 0) q.tag = tag;
      const view = p.get('view');
      if (view === 'needs_category' || view === 'in' || view === 'out') q.view = view;
    }
    try {
      const blob = await api.exportTransactionsCsv(q);
      saveBlob(blob, `iron-owl-transactions-${toISODate(new Date())}.csv`);
      toast.push({ tone: 'success', title: 'CSV exported', body: Object.keys(q).length ? 'Transactions matching the current filters.' : 'All transactions.', timeout: 3500 });
    } catch (e) {
      toast.push({ tone: 'error', title: 'Export failed', body: errorMessage(e) });
    }
  }, [close, location.pathname, location.search, toast]);

  const items = useMemo<Item[]>(() => {
    const q = query.trim();
    const actions: Item[] = [
      { key: 'a-sync', group: 'Actions', label: 'Sync now', icon: 'sync', keywords: 'refresh update plaid', sub: plaid && !plaid.configured ? 'Add Plaid keys in Settings first' : 'Refresh balances and transactions', run: () => { close(); void sync(); } },
      { key: 'a-add', group: 'Actions', label: 'Add account', icon: 'plus', keywords: 'link bank plaid manual new', run: () => go('/accounts?add=1') },
      { key: 'a-keys', group: 'Actions', label: 'Bank connection (Plaid keys)', icon: 'key', keywords: 'plaid keys client id secret sandbox production connect bank', run: () => go('/settings/banks?focus=bank-connection') },
      { key: 'a-csv', group: 'Actions', label: 'Export transactions (CSV)', icon: 'download', keywords: 'download csv spreadsheet export', run: () => void exportCsv() },
      { key: 'a-rule', group: 'Actions', label: 'New rule', icon: 'rule', keywords: 'categorize automatic create', run: () => go('/settings?tab=rules&new=1') },
      { key: 'a-goal', group: 'Actions', label: 'New goal', icon: 'target', keywords: 'savings payoff create', run: () => go('/goals?new=save') },
      { key: 'a-lock', group: 'Actions', label: 'Lock Iron Owl', icon: 'lock', keywords: 'sign out logout secure', run: () => { close(); void lockNow('manual'); } },
    ];
    const pages: Item[] = PAGES.map((p) => ({
      key: `p-${p.to}`,
      group: 'Pages',
      label: p.label,
      icon: p.icon,
      keywords: p.keywords,
      sub: isCurrentPage(p.to, location.pathname, location.search) ? 'Current page' : undefined,
      run: () => go(p.to),
    }));

    if (!q) return [...actions, ...pages];

    const rank = (list: Item[], max: number) =>
      list
        .map((it) => ({ it, s: Math.max(best(q, it.label), best(q, it.keywords) - 200) }))
        .filter((x) => x.s >= 0)
        .sort((a, b) => b.s - a.s)
        .slice(0, max)
        .map((x) => x.it);

    const acctItems: Item[] = (accounts ?? []).map((a) => ({
      key: `acc-${a.id}`,
      group: 'Accounts',
      label: a.name,
      sub: [a.institution_name, a.mask ? `••${a.mask}` : null, CATEGORY_SINGULAR[a.category], a.hidden ? 'Hidden' : null].filter(Boolean).join(' · '),
      lead: <Avatar name={a.institution_name || a.name} size="sm" />,
      keywords: `${a.institution_name ?? ''} ${a.official_name ?? ''} ${a.mask ?? ''}`,
      run: () => go(`/accounts/${a.id}`),
    }));

    const txItems: Item[] = [];
    if (q.length >= 2) {
      for (const t of txns?.q === q ? txns.items : []) {
        txItems.push({
          key: `t-${t.id}`,
          group: 'Transactions',
          label: t.merchant_name || t.name,
          sub: `${formatDateSmart(t.date)} · ${t.account_name} · ${t.category_name}`,
          lead: <Avatar name={t.merchant_name || t.name} size="sm" />,
          trail: <Money value={t.amount} tone="in" signed={t.amount > 0} className="palette-amount" />,
          run: () => go(`/transactions?q=${encodeURIComponent(t.merchant_name || t.name)}`),
        });
      }
      txItems.push({
        key: 't-all',
        group: 'Transactions',
        label: `Search all transactions for “${q}”`,
        icon: 'search',
        run: () => go(`/transactions?q=${encodeURIComponent(q)}`),
      });
    }

    return [...rank(pages, 6), ...rank(acctItems, 6), ...rank(actions, 6), ...txItems];
  }, [query, accounts, txns, location.pathname, location.search, plaid, go, close, sync, lockNow, exportCsv]);

  // Keep the active row valid and visible.
  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    if (active >= items.length) setActive(Math.max(0, items.length - 1));
  }, [items.length, active]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    const n = items.length;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (n) setActive((a) => (a + 1) % n);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (n) setActive((a) => (a - 1 + n) % n);
    } else if (e.key === 'Home' && e.ctrlKey) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === 'End' && e.ctrlKey) {
      e.preventDefault();
      setActive(Math.max(0, n - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      items[active]?.run();
    }
  }

  const listId = `${uid}-list`;
  const optId = (i: number) => `${uid}-opt-${i}`;
  const groups: { name: Item['group']; rows: { it: Item; i: number }[] }[] = [];
  items.forEach((it, i) => {
    const g = groups[groups.length - 1];
    if (g && g.name === it.group) g.rows.push({ it, i });
    else groups.push({ name: it.group, rows: [{ it, i }] });
  });
  const q = query.trim();
  const searchingTx = q.length >= 2 && (txnLoading || txns?.q !== q);

  return (
    <dialog
      ref={dialogRef}
      className="palette"
      aria-label="Command palette"
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
      onClick={(e) => {
        if (e.target === dialogRef.current) close();
      }}
    >
      {open && (
        <div className="palette-inner">
          <div className="palette-search">
            <Icon name="search" />
            <input
              ref={inputRef}
              className="palette-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onKey}
              placeholder="Search or jump to…"
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-activedescendant={items[active] ? optId(active) : undefined}
              aria-autocomplete="list"
              aria-label="Search Iron Owl"
              autoComplete="off"
              spellCheck={false}
            />
            <kbd className="palette-kbd">Esc</kbd>
          </div>
          <div ref={listRef} id={listId} className="palette-list" role="listbox" aria-label="Results">
            {groups.map((g) => (
              <div key={g.name} role="group" aria-labelledby={`${uid}-g-${g.name}`} className="palette-group">
                <div className="palette-group-head" id={`${uid}-g-${g.name}`} role="presentation">
                  {g.name}
                </div>
                {g.rows.map(({ it, i }) => (
                  <div
                    key={it.key}
                    id={optId(i)}
                    data-i={i}
                    role="option"
                    aria-selected={i === active}
                    className={`palette-opt${i === active ? ' is-active' : ''}`}
                    onPointerMove={() => i !== active && setActive(i)}
                    onClick={() => it.run()}
                  >
                    {it.lead ?? (
                      <span className="palette-icon" aria-hidden="true">
                        <Icon name={it.icon ?? 'chevronRight'} />
                      </span>
                    )}
                    <span className="palette-text">
                      <span className="palette-label truncate">{it.label}</span>
                      {it.sub && <span className="palette-sub truncate">{it.sub}</span>}
                    </span>
                    {it.trail}
                    {i === active && <Icon name="enter" className="palette-enter" />}
                  </div>
                ))}
              </div>
            ))}
            {searchingTx && (
              <div className="palette-status" role="status">
                <Icon name="sync" className="spin" />
                Searching transactions…
              </div>
            )}
            {items.length === 0 && !searchingTx && <div className="palette-status">Nothing matches “{q}”.</div>}
          </div>
          <div className="palette-foot" aria-hidden="true">
            <span>
              <kbd className="palette-kbd">↑</kbd>
              <kbd className="palette-kbd">↓</kbd> to move
            </span>
            <span>
              <kbd className="palette-kbd">Enter</kbd> to open
            </span>
            <span>
              <kbd className="palette-kbd">Esc</kbd> to close
            </span>
          </div>
        </div>
      )}
    </dialog>
  );
}
