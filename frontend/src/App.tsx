import { lazy, Suspense, useEffect, type ComponentType } from 'react';
import { HashRouter, Navigate, Route, Routes, useLocation, useSearchParams } from 'react-router-dom';
import { AppProvider, useApp } from './state';
import { ToastProvider } from './components/Toast';
import { Layout } from './components/Layout';
import { StartingScreen } from './components/gate/StartingScreen';
import { PasswordScreen } from './components/gate/PasswordScreen';
import { NotRunningScreen } from './components/gate/NotRunningScreen';
import { ElsewhereScreen } from './components/gate/ElsewhereScreen';
import { SetupPage } from './pages/Setup';
import { UpdateProvider, useUpdates, FULL_WINDOW_FLOWS } from './updates/UpdateProvider';
import { InstallScreen } from './updates/InstallScreen';
import { HomePage } from './pages/Home';
import { helpModule } from './pages/helpModule';

// Everything except Home loads on first visit, keeping the initial bundle small.
const lazyPage = <K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K) =>
  lazy(() => load().then((m) => ({ default: m[name] })));
const AccountsShell = lazyPage(() => import('./pages/accounts/AccountsShell'), 'AccountsShell');
const AccountsList = lazyPage(() => import('./pages/accounts/AccountsList'), 'AccountsList');
const AccountDetail = lazyPage(() => import('./pages/accounts/AccountDetail'), 'AccountDetail');
const TransactionsPage = lazyPage(() => import('./pages/Transactions'), 'TransactionsPage');
const InvestmentsPage = lazyPage(() => import('./pages/Investments'), 'InvestmentsPage');
const SettingsPage = lazyPage(() => import('./pages/Settings'), 'SettingsPage');
const SpendingPage = lazyPage(() => import('./pages/Spending'), 'SpendingPage');
const RecurringPage = lazyPage(() => import('./pages/recurring/RecurringPage'), 'RecurringPage');
const GoalsPage = lazyPage(() => import('./pages/Goals'), 'GoalsPage');
const SettingsBanksPage = lazyPage(() => import('./pages/SettingsBanks'), 'SettingsBanksPage');
const ReportsPage = lazyPage(() => import('./pages/Reports'), 'ReportsPage');
const HelpPage = lazyPage(helpModule.load, 'HelpPage');
const HelpScreen = lazyPage(helpModule.load, 'HelpScreen');

/**
 * HashRouter: the backend serves frontend/dist as static files at "/", so hash
 * routes work on reload without any server-side SPA fallback.
 */
export function App() {
  return (
    <ToastProvider>
      <AppProvider>
        <HashRouter>
          <UpdateProvider>
            <Gate />
          </UpdateProvider>
        </HashRouter>
      </AppProvider>
    </ToastProvider>
  );
}

/** Which screen the window shows before (or instead of) the app itself (design D4). */
function Gate() {
  const { phase } = useApp();
  const { flow } = useUpdates();
  const { pathname } = useLocation();

  // Load Help's code while the server answers, so Help still opens if it stops later.
  useEffect(() => {
    if (phase !== 'unreachable' && !helpModule.isReady()) helpModule.load().catch(() => {});
  }, [phase]);

  // An update installing, or its result: above everything else (FinTrack restarts meanwhile).
  if (FULL_WINDOW_FLOWS.includes(flow.kind)) return <InstallScreen />;
  // Help works before the app opens too (locked, first setup, already open elsewhere): no menu, a Back button.
  if (pathname === '/help' && phase !== 'unlocked') {
    return (
      <Suspense fallback={<StartingScreen />}>
        <HelpScreen />
      </Suspense>
    );
  }
  if (phase === 'loading') return <StartingScreen />;
  if (phase === 'unreachable') return <NotRunningScreen />;
  if (phase === 'setup') return <SetupPage />;
  if (phase === 'locked') return <PasswordScreen />;
  if (phase === 'elsewhere') return <ElsewhereScreen />;

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="accounts" element={<AccountsShell />}>
          <Route index element={<AccountsList />} />
          <Route path=":id" element={<AccountDetail />} />
        </Route>
        <Route path="transactions" element={<TransactionsPage />} />
        <Route path="spending" element={<SpendingPage />} />
        <Route path="recurring" element={<RecurringPage />} />
        <Route path="goals" element={<GoalsPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="investments" element={<InvestmentsPage />} />
        {/* Loans & credit moved into Reports › Paying off debt (Release 3.10). */}
        <Route path="loans" element={<Navigate to="/reports?tab=debt" replace />} />
        <Route path="rules" element={<RulesRedirect />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="settings/banks" element={<SettingsBanksPage />} />
        <Route path="help" element={<HelpPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

/** The Rules & alerts page moved into Settings (design D7): rules on its Rules tab, alerts on Alerts. */
function RulesRedirect() {
  const [params] = useSearchParams();
  return <Navigate to={`/settings?tab=rules${params.get('new') === '1' ? '&new=1' : ''}`} replace />;
}
