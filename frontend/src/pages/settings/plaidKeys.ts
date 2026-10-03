// Shared copy and helpers for Settings → Bank connection (Plaid keys kept in the vault).
// Visible copy avoids "environment", "vault" and "Item": the main user isn't technical.
// "Client ID" and "Secret" stay because they must match the labels on Plaid's dashboard.
import type { PlaidEnv, PlaidKeysTest } from '../../api';

export const PLAID_SIGNUP_URL = 'https://dashboard.plaid.com/signup';
export const PLAID_KEYS_URL = 'https://dashboard.plaid.com/developers/keys';

/**
 * The three setup steps, shared word for word by Home's Welcome card and Settings →
 * Bank connection (so the two never drift apart). Plain words for a non-technical reader.
 */
export const BANK_SETUP = {
  time: 'It takes about 10 minutes. You can stop at any step and come back later.',
  account: {
    title: 'Get a free Plaid account',
    body: 'Plaid is the service that connects Iron Owl to your bank. Sign up with your email address on Plaid’s website.',
    link: 'Open Plaid sign-up',
  },
  keys: {
    title: 'Copy your two keys',
    body: 'In Plaid, open Developers, then Keys. Copy these two:',
    clientId: { name: 'Client ID', desc: 'A long mix of letters and numbers. It’s the same for test and real banks.' },
    secret: {
      name: 'Secret',
      desc: 'Copy the Production secret if Plaid has approved you for real banks; otherwise the Sandbox one.',
    },
    link: 'Open Plaid keys page',
  },
  private: {
    title: 'Keep these keys private.',
    body: 'Don’t share them with anyone: not by email, text or phone, and not with anyone who says they’re from Iron Owl or Plaid. Only paste them into Iron Owl on this computer.',
  },
  paste: {
    title: 'Paste them into Iron Owl',
    body: 'Paste both keys into the boxes, save, then sign in to your bank to link it.',
    password: 'You’ll be asked for your Iron Owl password.',
    link: 'Go to where you paste them',
  },
} as const;

/** Mirrors the server: letters and digits only, 16–64 long. Plaid's are 24 (client ID) and 30 (secret). */
export const KEY_RE = /^[A-Za-z0-9]{16,64}$/;
export const CLIENT_ID_LEN = 24;
export const SECRET_LEN = 30;

/**
 * Paste forgiveness: keys never contain whitespace or quotes, so drop spaces and
 * newlines anywhere and quotes around the value ("abc…", “abc…”, `abc…`).
 */
export function cleanKey(raw: string): string {
  return raw.replace(/\s+/g, '').replace(/^["'`‘’“”]+|["'`‘’“”]+$/g, '');
}

export const ENV_SHORT: Record<PlaidEnv, string> = { production: 'Real banks', sandbox: 'Test banks' };
export const ENV_LONG: Record<PlaidEnv, string> = { production: 'Real banks (Production)', sandbox: 'Test banks (Sandbox)' };

/** "5f3a…9c21" — enough to compare with Plaid's dashboard without a wall of hex. */
export function middleTruncate(id: string | null): string {
  if (!id) return '—';
  return id.length > 12 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id;
}

/** "••••1a2b"; the hint can be missing (a short secret in the settings file), so fall back to dots only. */
export function maskedSecret(hint: string | null): string {
  return hint ? `••••${hint}` : '••••••••';
}

/**
 * Did Plaid actually judge the keys and say no? False for "couldn't reach Plaid" and
 * Plaid-side hiccups, where the keys may be fine and the next step is to check again.
 */
export function keysRejected(t: Pick<PlaidKeysTest, 'ok' | 'code'> | null | undefined): boolean {
  return !!t && !t.ok && t.code !== 'unreachable' && t.code !== 'plaid_error';
}
