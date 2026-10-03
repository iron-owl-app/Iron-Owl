import { useCallback, useEffect, useMemo, useRef, useState, type ComponentType, type KeyboardEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, ApiError, type PlaidItem } from '../api';
import { useApp } from '../state';
import { useApi } from '../lib/useApi';
import { readPref, writePref } from '../lib/prefs';
import { useBankReauth } from '../components/useBankReauth';
import { SettingsMessages, useFail, useSay } from './settings/parts';
import { announceSettingsChanged, SettingsProvider, type SettingsShared } from './settings/context';
import { settingsNeeds, type NeedItem, type SettingsTabId } from './settings/needs';
import { useRecoverySheetFlow } from './settings/useRecoverySheetFlow';
import { CategoriesTab } from './settings/CategoriesTab';
import { PaychecksTab } from './settings/PaychecksTab';
import { RulesTab } from './settings/RulesTab';
import { AlertsTab } from './settings/AlertsTab';
import { BanksTab } from './settings/BanksTab';
import { SafetyTab } from './settings/SafetyTab';
import { AppTab } from './settings/AppTab';
import './settings/settings.css';

/**
 * The Settings tabs, in order. Data-driven: a new tab is one more entry here (plus its id in
 * `SettingsTabId`); its amber dot comes from the Needs-you items with that `tab`.
 */
export const SETTINGS_TABS: { id: SettingsTabId; label: string; Panel: ComponentType }[] = [
  { id: 'cat', label: 'Categories', Panel: CategoriesTab },
  { id: 'rules', label: 'Rules', Panel: RulesTab },
  { id: 'pay', label: 'Paychecks', Panel: PaychecksTab },
  { id: 'alerts', label: 'Alerts', Panel: AlertsTab },
  { id: 'banks', label: 'Banks', Panel: BanksTab },
  { id: 'safe', label: 'Safety and backups', Panel: SafetyTab },
  { id: 'app', label: 'Colors, text size and updates', Panel: AppTab },
];

const TAB_PREF = 'settingsTab';
const isTab = (v: unknown): v is SettingsTabId => SETTINGS_TABS.some((t) => t.id === v);

/**
 * Settings (design D7): the title, a "Needs you" card when something needs the user, and seven tabs
 * (`?tab=cat|rules|pay|alerts|banks|safe|app`, remembered in the `fintrack.ui.settingsTab` pref).
 * Old deep links still work: `focus=recovery-sheet(&make=1)` opens Safety and backups (and the
 * recovery sheet dialog), `focus=updates` opens Colors, text size and updates, and
 * `focus=bank-connection` moves to Manage banks (/settings/banks).
 */
export function SettingsPage() {
  return (
    <SettingsMessages>
      <SettingsShell />
    </SettingsMessages>
  );
}

function SettingsShell() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { sync, invalidate } = useApp();
  const say = useSay();
  const fail = useFail();
  const focus = params.get('focus');

  // Old deep link: the Plaid keys card now lives on Manage banks.
  useEffect(() => {
    if (focus === 'bank-connection') navigate('/settings/banks?focus=bank-connection', { replace: true });
  }, [focus, navigate]);

  const urlTab = params.get('tab');
  const focusTab: SettingsTabId | null = focus === 'recovery-sheet' ? 'safe' : focus === 'updates' ? 'app' : null;
  const [savedTab] = useState<SettingsTabId>(() => readPref<SettingsTabId>(TAB_PREF, 'cat', isTab));
  const tab: SettingsTabId = isTab(urlTab) ? urlTab : (focusTab ?? savedTab);

  const goTab = useCallback(
    (t: SettingsTabId) => {
      writePref(TAB_PREF, t);
      setParams(
        (p) => {
          const next = new URLSearchParams(p);
          next.set('tab', t);
          next.delete('focus');
          next.delete('make');
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  // Coming in from a deep link: remember the tab it opened.
  useEffect(() => {
    if (focusTab) writePref(TAB_PREF, focusTab);
  }, [focusTab]);

  const items = useApi(() => api.plaid.items(), []);
  const backup = useApi(() => api.autoBackup.get(), []);
  const recovery = useApi(() => api.recovery.get(), []);
  const reauth = useBankReauth({ onDone: () => items.reload() });
  const sheet = useRecoverySheetFlow(recovery);
  const [turningOn, setTurningOn] = useState(false);
  const [typeFolderAsk, setTypeFolderAsk] = useState(false);
  const [retryingBank, setRetryingBank] = useState<number | null>(null);

  const turnOnBackups = useCallback(async () => {
    if (turningOn) return;
    setTurningOn(true);
    try {
      let { dir } = await api.autoBackup.suggestedFolder();
      if (!dir) {
        // No OneDrive or Documents folder on this PC: the user picks one (never a "set" without a
        // folder, which would turn backups off).
        try {
          dir = (await api.autoBackup.pickFolder()).dir;
        } catch (e) {
          if (e instanceof ApiError && e.status === 501) {
            setTypeFolderAsk(true); // no folder window here: the Safety tab's typed-path box
            return;
          }
          if (e instanceof ApiError && e.status === 409) {
            fail('A folder window is already open', new Error('Look for it behind Iron Owl, pick a folder there, then click Select Folder.'));
            return;
          }
          throw e;
        }
        if (!dir) return; // cancelled: backups stay off
      }
      const keep = backup.data?.keep ?? 10;
      const saved = await api.autoBackup.set(dir, keep, true);
      backup.setData(saved);
      if (!saved.dir) throw new Error('Iron Owl couldn’t save the backup folder. Please try again.');
      let first = false;
      try {
        first = (await api.autoBackup.run()).ok;
      } catch {
        /* the first one then happens the next time FinTrack locks */
      }
      say(first ? 'Automatic backups are on. The first one was saved just now.' : 'Automatic backups are on. The first one is saved the next time Iron Owl locks.');
      backup.reload();
      announceSettingsChanged();
    } catch (e) {
      fail('Couldn’t turn on automatic backups', e);
    } finally {
      setTurningOn(false);
    }
  }, [backup, fail, say, turningOn]);

  const retryBank = useCallback(
    async (item: PlaidItem) => {
      setRetryingBank(item.id);
      const res = await sync({ itemId: item.id });
      setRetryingBank(null);
      items.reload();
      if (res && res.every((r) => r.ok)) invalidate();
    },
    [invalidate, items, sync],
  );

  // Home's "Make one" (/settings?focus=recovery-sheet&make=1): open the dialog once the status
  // is known, then drop the param so Back or a reload doesn't open it again.
  const makeSheet = focus === 'recovery-sheet' && params.get('make') === '1';
  const sheetRef = useRef(sheet);
  sheetRef.current = sheet;
  useEffect(() => {
    if (!makeSheet || (recovery.loading && !recovery.data && !recovery.error)) return;
    setParams(
      (p) => {
        const next = new URLSearchParams(p);
        next.delete('make');
        return next;
      },
      { replace: true },
    );
    if (recovery.data?.status === 'unconfirmed') sheetRef.current.finish();
    else sheetRef.current.make();
  }, [makeSheet, recovery.data, recovery.loading, recovery.error, setParams]);

  const needs = useMemo(() => settingsNeeds(items.data, backup.data, recovery.data), [items.data, backup.data, recovery.data]);
  const needsKey = needs.map((n) => n.key).join(',');
  const firstKey = useRef(true);
  useEffect(() => {
    if (firstKey.current) {
      firstKey.current = false;
      return;
    }
    announceSettingsChanged();
  }, [needsKey]);

  const shared: SettingsShared = {
    items,
    backup,
    recovery,
    reauth,
    sheet,
    turnOnBackups,
    turningOn,
    typeFolderAsk,
    clearTypeFolderAsk: () => setTypeFolderAsk(false),
    retryBank,
    retryingBank,
    goTab,
    focus,
  };

  function act(n: NeedItem) {
    goTab(n.tab);
    switch (n.kind) {
      case 'bank_signin':
        if (n.item) reauth.start(n.item);
        return;
      case 'bank_pending':
        if (n.item) navigate(`/settings/banks?finish=${n.item.id}`);
        return;
      case 'bank_error':
        if (n.item) void retryBank(n.item);
        return;
      case 'backup':
        void turnOnBackups();
        return;
      case 'recovery':
        if (recovery.data?.status === 'unconfirmed') sheet.finish();
        else sheet.make();
        return;
    }
  }

  function buttonText(n: NeedItem): { text: string; busy: boolean } {
    if (n.kind === 'bank_signin' && n.item && reauth.busyItemId === n.item.id)
      return { text: reauth.phase === 'updating' ? 'Updating…' : 'Opening sign-in…', busy: true };
    if (n.kind === 'bank_error' && n.item && retryingBank === n.item.id) return { text: 'Trying again…', busy: true };
    if (n.kind === 'backup' && turningOn) return { text: 'Turning on…', busy: true };
    return { text: n.button, busy: false };
  }

  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  function onTabKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    const last = SETTINGS_TABS.length - 1;
    let j = -1;
    if (e.key === 'ArrowRight') j = i === last ? 0 : i + 1;
    else if (e.key === 'ArrowLeft') j = i === 0 ? last : i - 1;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = last;
    if (j < 0) return;
    e.preventDefault();
    goTab(SETTINGS_TABS[j]!.id);
    tabRefs.current[j]?.focus();
  }

  const dots = new Set(needs.map((n) => n.tab));
  const active = SETTINGS_TABS.find((t) => t.id === tab) ?? SETTINGS_TABS[0]!;
  const Panel = active.Panel;

  return (
    <SettingsProvider value={shared}>
      <div className="st-page">
        <header className="st-head">
          <h1 className="st-title">Settings</h1>
          <div role="tablist" aria-label="Settings sections" className="st-tabs">
            {SETTINGS_TABS.map((t, i) => (
              <button
                key={t.id}
                ref={(el) => {
                  tabRefs.current[i] = el;
                }}
                type="button"
                role="tab"
                id={`st-tab-${t.id}`}
                aria-selected={t.id === tab}
                aria-controls={`st-panel-${t.id}`}
                tabIndex={t.id === tab ? 0 : -1}
                className="st-tab"
                onClick={() => goTab(t.id)}
                onKeyDown={(e) => onTabKey(e, i)}
              >
                {t.label}
                {dots.has(t.id) && (
                  <>
                    <span className="st-dot" aria-hidden="true" />
                    <span className="sr-only">, needs you</span>
                  </>
                )}
              </button>
            ))}
          </div>
        </header>

        {needs.length > 0 && (
          <section aria-labelledby="st-needs-h" className="st-needs">
            <h2 id="st-needs-h">{needs.length === 1 ? '1 thing needs you' : `${needs.length} things need you`}</h2>
            {needs.map((n) => {
              const b = buttonText(n);
              return (
                <div className="st-needs-row" key={n.key}>
                  <span className="st-needs-text">
                    <strong>{n.head}</strong> <span>{n.body}</span>
                  </span>
                  <button type="button" className="st-btn st-btn-light" onClick={() => act(n)} disabled={b.busy} aria-disabled={b.busy || undefined}>
                    {b.text}
                  </button>
                </div>
              );
            })}
          </section>
        )}

        <div role="tabpanel" id={`st-panel-${active.id}`} aria-labelledby={`st-tab-${active.id}`} className="st-panel" key={active.id}>
          <Panel />
        </div>
      </div>
      {reauth.launcher}
      {sheet.dialogs}
    </SettingsProvider>
  );
}
