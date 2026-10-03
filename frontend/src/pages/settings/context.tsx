import { createContext, useContext, type ReactNode } from 'react';
import type { AutoBackup, PlaidItem, RecoveryStatus } from '../../api';
import type { ApiState } from '../../lib/useApi';
import type { ReauthPhase, ReauthTarget } from '../../components/useBankReauth';
import type { RecoverySheetFlow } from './useRecoverySheetFlow';
import type { SettingsTabId } from './needs';

/** Shared by the Settings shell (the Needs-you card) and its tabs, so each is fetched once. */
export interface SettingsShared {
  items: ApiState<PlaidItem[]>;
  backup: ApiState<AutoBackup>;
  recovery: ApiState<RecoveryStatus>;
  reauth: { start: (item: ReauthTarget) => void; busyItemId: number | null; phase: ReauthPhase | null };
  sheet: RecoverySheetFlow;
  /** "Turn on backups": the suggested folder (made when missing), then a first backup. With
   * no suggested folder, the folder window; where that doesn't exist, `typeFolderAsk`. */
  turnOnBackups: () => Promise<void>;
  turningOn: boolean;
  /** "Turn on" needs a typed folder path: the Safety tab opens its box, then clears this. */
  typeFolderAsk: boolean;
  clearTypeFolderAsk: () => void;
  /** Bank-side error: sync that bank again. */
  retryBank: (item: PlaidItem) => Promise<void>;
  retryingBank: number | null;
  goTab: (t: SettingsTabId) => void;
  /** `?focus=` from an old deep link (recovery-sheet, updates), read once by the tab it points at. */
  focus: string | null;
}

const Ctx = createContext<SettingsShared | null>(null);

export function SettingsProvider({ value, children }: { value: SettingsShared; children: ReactNode }) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSettings(): SettingsShared {
  const v = useContext(Ctx);
  if (!v) throw new Error('useSettings outside the Settings page');
  return v;
}

/** Tells the sidebar to recount its Settings dot (backups or the recovery sheet changed). */
export const SETTINGS_CHANGED = 'fintrack:settings-changed';
export function announceSettingsChanged(): void {
  window.dispatchEvent(new Event(SETTINGS_CHANGED));
}
