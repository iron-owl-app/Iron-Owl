import type { AutoBackup, PlaidItem, RecoveryStatus } from '../../api';

export type SettingsTabId = 'cat' | 'rules' | 'pay' | 'alerts' | 'banks' | 'safe' | 'app';

export type NeedKind = 'bank_signin' | 'bank_pending' | 'bank_error' | 'backup' | 'recovery';

/** One row of the "Needs you" card; its tab shows an amber dot. */
export interface NeedItem {
  key: string;
  kind: NeedKind;
  tab: SettingsTabId;
  head: string;
  body: string;
  button: string;
  item?: PlaidItem;
}

const bankName = (it: PlaidItem) => it.institution_name ?? 'Your bank';

/**
 * Everything on the Settings page that needs the user, in the card's order: banks (every connection
 * that isn't OK, the same ones the summary counts), automatic backups off, and a recovery sheet
 * that isn't ready. The sidebar's Settings dot counts the same things (see Layout).
 */
export function settingsNeeds(items: PlaidItem[] | undefined, backup: AutoBackup | undefined, recovery: RecoveryStatus | undefined): NeedItem[] {
  const out: NeedItem[] = [];
  for (const it of items ?? []) {
    if (it.status === 'login_required') {
      out.push({
        key: `bank-${it.id}`,
        kind: 'bank_signin',
        tab: 'banks',
        head: `${bankName(it)} needs you to sign in again.`,
        body: 'Until you do, its balances won’t update.',
        button: 'Sign in again',
        item: it,
      });
    } else if (it.status === 'pending') {
      out.push({
        key: `bank-${it.id}`,
        kind: 'bank_pending',
        tab: 'banks',
        head: `${bankName(it)} isn’t set up all the way.`,
        body: 'Pick which of its accounts Iron Owl should bring in.',
        button: 'Finish setting up',
        item: it,
      });
    } else if (it.status !== 'ok') {
      out.push({
        key: `bank-${it.id}`,
        kind: 'bank_error',
        tab: 'banks',
        head: `${bankName(it)} isn’t updating.`,
        body: 'The bank had a problem last time. This usually fixes itself.',
        button: 'Try again',
        item: it,
      });
    }
  }
  if (backup && !backup.dir) {
    out.push({
      key: 'backup',
      kind: 'backup',
      tab: 'safe',
      head: 'Automatic backups are off.',
      body: 'If this computer breaks, your Iron Owl data could be lost.',
      button: 'Turn on backups',
    });
  }
  if (recovery && recovery.status !== 'active') {
    const r = RECOVERY_NEED[recovery.status];
    out.push({ key: 'recovery', kind: 'recovery', tab: 'safe', ...r });
  }
  return out;
}

const RECOVERY_NEED: Record<Exclude<RecoveryStatus['status'], 'active'>, { head: string; body: string; button: string }> = {
  none: {
    head: 'You don’t have a recovery sheet yet.',
    body: 'It’s the only way back in if you forget your password.',
    button: 'Make a recovery sheet',
  },
  unconfirmed: {
    head: 'Your recovery sheet isn’t finished.',
    body: 'Check that you printed it or wrote it down, or make a new one.',
    button: 'Finish the sheet…',
  },
  stale: {
    head: 'Your recovery sheet is out of date.',
    body: 'It may not work any more. Make a new one so you can still get back in.',
    button: 'Make a new sheet…',
  },
};

/** The sidebar dot: bank connections needing attention (from the summary) + backups off + no ready sheet. */
export function settingsNeedsCount(banks: number, backup: AutoBackup | undefined, recovery: RecoveryStatus | undefined): number {
  return banks + (backup && !backup.dir ? 1 : 0) + (recovery && recovery.status !== 'active' ? 1 : 0);
}
