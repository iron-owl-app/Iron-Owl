import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { OwlLogo } from './OwlLogo';
import { Suspense, useEffect, useRef, useState } from 'react';
import { useApp, useUpdated } from '../state';
import { Icon, type IconName } from './Icon';
import { useApi } from '../lib/useApi';
import { api, type Summary } from '../api';
import { CommandPalette, openCommandPalette } from './CommandPalette';
import { SETTINGS_CHANGED } from '../pages/settings/context';
import { settingsNeedsCount } from '../pages/settings/needs';
import { UpdateBanner } from '../updates/UpdateBanner';
import { UpdateDialogs } from '../updates/UpdateDialogs';
import { ColorsButton, MenuColors } from './ThemePicker';

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
}

const NAV: NavItem[] = [
  { to: '/', label: 'Home', icon: 'dashboard' },
  { to: '/accounts', label: 'Accounts', icon: 'wallet' },
  { to: '/transactions', label: 'Transactions', icon: 'arrows' },
  { to: '/spending', label: 'Budget', icon: 'receipt' },
  { to: '/reports', label: 'Reports', icon: 'chart' },
  { to: '/recurring', label: 'Recurring', icon: 'repeat' },
  { to: '/goals', label: 'Goals', icon: 'target' },
  { to: '/investments', label: 'Investments', icon: 'trend' },
  { to: '/settings', label: 'Settings', icon: 'sliders' },
  { to: '/help', label: 'Help', icon: 'help' },
];

/** The page a path belongs to: exact, else the longest nav prefix (`/accounts/12` → Accounts, `/settings/banks` → Settings). */
export function navFor(pathname: string): NavItem | undefined {
  const exact = NAV.find((n) => n.to === pathname);
  if (exact) return exact;
  return NAV.filter((n) => n.to !== '/' && pathname.startsWith(`${n.to}/`)).sort((a, b) => b.to.length - a.to.length)[0];
}

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
const SHORTCUT_LABEL = IS_MAC ? '⌘K' : 'Ctrl K';

export function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      {/* The app icon itself (an owl on an amber tile), so the window, taskbar and page match. */}
      <OwlLogo />
    </span>
  );
}

function SyncButton() {
  const { sync, syncing, plaid } = useApp();
  const notConfigured = plaid !== null && !plaid.configured;
  return (
    <button
      type="button"
      className="side-action"
      onClick={() => void sync()}
      disabled={syncing || notConfigured}
      title={notConfigured ? 'Add your Plaid keys in Settings › Banks first' : undefined}
    >
      <Icon name="sync" className={syncing ? 'spin' : undefined} />
      {syncing ? 'Updating…' : 'Update from banks'}
    </button>
  );
}

function LockButton() {
  const { lockNow } = useApp();
  return (
    <button type="button" className="side-action" onClick={() => void lockNow('manual')}>
      <Icon name="lock" />
      Lock
    </button>
  );
}

/** "1 thing needs you" / "3 things need you". */
export function needsWords(needs: number): string {
  return `${needs} ${needs === 1 ? 'thing needs' : 'things need'} you`;
}

/** "3 things need you in Settings" (the menu button, which isn't the Settings link itself). */
export function settingsNeedsLabel(needs: number): string {
  return `${needsWords(needs)} in Settings`;
}

/** The Settings count: a 22px amber number pill, hidden at 0 (the link's own label carries the words). */
function NeedsPill({ needs }: { needs: number }) {
  if (needs <= 0) return null;
  return (
    <span className="needs-pill" aria-hidden="true">
      {needs > 99 ? '99+' : needs}
    </span>
  );
}

function navA11yLabel(n: NavItem, needs: number): string | undefined {
  // "Settings, 3 things need you" (the link is Settings: no "in Settings" again).
  if (n.to === '/settings' && needs > 0) return `Settings, ${needsWords(needs)}`;
  return undefined;
}

/**
 * The Settings dot: everything its "Needs you" card lists (banks needing attention from the
 * summary, automatic backups off, a recovery sheet that isn't ready). Re-read on navigation,
 * on data changes, and when Settings says backups or the sheet changed.
 */
function useSettingsNeeds(pathname: string, banks: number): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const again = () => setTick((t) => t + 1);
    window.addEventListener(SETTINGS_CHANGED, again);
    return () => window.removeEventListener(SETTINGS_CHANGED, again);
  }, []);
  const backup = useApi(() => api.autoBackup.get(), [pathname, tick]);
  const recovery = useApi(() => api.recovery.get(), [pathname, tick]);
  return settingsNeedsCount(banks, backup.data, recovery.data);
}

export function Layout() {
  const { attention, autoLockMinutes, setAttention, setUpdatedAt } = useApp();
  const updated = useUpdated();
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const first = useRef(true);
  const [menuOpen, setMenuOpen] = useState(false);

  // Summary drives the Settings count (connections needing attention) and the shared "Updated …" time.
  // Refetched on data changes (useApi) and on navigation.
  const summary = useApi<Summary>(() => api.summary(), [location.pathname]);
  useEffect(() => {
    if (!summary.data) return;
    setAttention(summary.data.items_needing_attention);
    setUpdatedAt(summary.data.last_synced_at);
  }, [summary.data, setAttention, setUpdatedAt]);
  const needs = useSettingsNeeds(location.pathname, attention);

  // Move focus to the page on route change so keyboard/screen-reader users land in content.
  useEffect(() => {
    setMenuOpen(false);
    if (first.current) {
      first.current = false;
      return;
    }
    mainRef.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }, [location.pathname]);

  const current = navFor(location.pathname);

  return (
    <div className="shell">
      <a className="skip-link" href="#main" onClick={(e) => { e.preventDefault(); mainRef.current?.focus(); }}>
        Skip to content
      </a>

      <aside className="sidebar" aria-label="Primary">
        <NavLink to="/" className="brand">
          <BrandMark />
          Iron Owl
        </NavLink>
        <button type="button" className="nav-search" onClick={openCommandPalette} aria-keyshortcuts={IS_MAC ? 'Meta+K' : 'Control+K'}>
          <Icon name="search" />
          <span className="nav-search-label">Search</span>
          <kbd className="kbd">{SHORTCUT_LABEL}</kbd>
        </button>
        <nav className="nav" aria-label="Main">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === '/'} className="nav-link" aria-label={navA11yLabel(n, needs)}>
              <Icon name={n.icon} />
              {n.label}
              {n.to === '/settings' && <NeedsPill needs={needs} />}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <SyncButton />
          <LockButton />
          {/* Not a live region: the words change every minute and mustn't be read out each time. */}
          <div className="sync-meta">
            {summary.data ? updated.text : ' '}
          </div>
          {autoLockMinutes > 0 && (
            <div className="sync-meta">
              Locks after {autoLockMinutes} {autoLockMinutes === 1 ? 'minute' : 'minutes'} of no use.
            </div>
          )}
          <ColorsButton />
        </div>
      </aside>

      <header className="topbar">
        <NavLink to="/" className="brand">
          <BrandMark />
          Iron Owl
        </NavLink>
        <button
          type="button"
          className="menu-btn"
          onClick={() => setMenuOpen(true)}
          aria-haspopup="dialog"
          aria-expanded={menuOpen}
          aria-label={`Menu${current ? `, on ${current.label}` : ''}${needs > 0 ? `, ${settingsNeedsLabel(needs)}` : ''}`}
        >
          <Icon name="menu" />
          Menu
          <NeedsPill needs={needs} />
        </button>
      </header>

      <main id="main" className="main" ref={mainRef} tabIndex={-1} style={{ outline: 'none' }}>
        <UpdateBanner />
        <div className={`main-inner${location.pathname === '/transactions' ? ' is-wide' : ''}`}>
          <Suspense fallback={<div className="page-loading" role="status" aria-label="Loading page" />}>
            <Outlet />
          </Suspense>
        </div>
      </main>

      <MenuSheet open={menuOpen} onClose={() => setMenuOpen(false)} needs={needs} />

      <CommandPalette />
      <UpdateDialogs />
    </div>
  );
}

/**
 * Small screens (≤860px): one labeled Menu button opens this sheet with every page,
 * then Search, Update from banks, Lock and the Colors choice. Rows are 48px with 15px labels.
 */
function MenuSheet({ open, onClose, needs }: { open: boolean; onClose: () => void; needs: number }) {
  const ref = useRef<HTMLDialogElement>(null);
  const { sync, syncing, plaid, lockNow } = useApp();
  const canSync = plaid === null || plaid.configured;
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    else if (!open && d.open) d.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="menu-sheet"
      aria-labelledby="menu-sheet-h"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="menu-sheet-inner">
        <div className="menu-sheet-head">
          <h2 id="menu-sheet-h">Menu</h2>
          <button type="button" className="menu-sheet-close" onClick={onClose}>
            <Icon name="x" />
            Close
          </button>
        </div>
        <nav aria-label="Pages">
          <ul className="menu-list">
            {NAV.map((n) => (
              <li key={n.to}>
                <NavLink to={n.to} end={n.to === '/'} className="menu-item" onClick={onClose} aria-label={navA11yLabel(n, needs)}>
                  <Icon name={n.icon} />
                  <span className="menu-label">{n.label}</span>
                  {n.to === '/settings' && <NeedsPill needs={needs} />}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <ul className="menu-list menu-actions" aria-label="Actions">
          <li>
            <button
              type="button"
              className="menu-item"
              onClick={() => {
                onClose();
                openCommandPalette();
              }}
            >
              <Icon name="search" />
              <span className="menu-label">Search</span>
            </button>
          </li>
          {canSync && (
            <li>
              <button type="button" className="menu-item" onClick={() => void sync()} disabled={syncing}>
                <Icon name="sync" className={syncing ? 'spin' : undefined} />
                <span className="menu-label">{syncing ? 'Updating…' : 'Update from banks'}</span>
              </button>
            </li>
          )}
          <li>
            <button
              type="button"
              className="menu-item"
              onClick={() => {
                onClose();
                void lockNow('manual');
              }}
            >
              <Icon name="lock" />
              <span className="menu-label">Lock Iron Owl</span>
            </button>
          </li>
        </ul>
        <MenuColors />
      </div>
    </dialog>
  );
}
