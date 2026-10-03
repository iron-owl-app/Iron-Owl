import type { ReactNode } from 'react';
import { Icon } from '../../components/Icon';
import { BANK_SETUP } from './plaidKeys';
import './bank-setup.css';

/**
 * Small pieces shared by Settings → Bank connection and Home's Welcome card. Kept apart from
 * BankConnectionSection so Home (in the first bundle) doesn't pull in the whole Settings card.
 */

/** Amber "Keep these keys private" note (setup step 2). */
export function KeepPrivateNote({ className }: { className?: string }) {
  return (
    <p className={`bc-private${className ? ` ${className}` : ''}`} role="note">
      <Icon name="lock" />
      <span>
        <strong>{BANK_SETUP.private.title}</strong> {BANK_SETUP.private.body}
      </span>
    </p>
  );
}

/** An outside link to Plaid's dashboard that says it opens a new tab. */
export function PlaidLink({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  return (
    <a className={className ?? 'bc-ext'} href={href} target="_blank" rel="noopener noreferrer">
      {children}
      {className ? <Icon name="external" /> : null}
      <span className={className ? 'sr-only' : 'bc-ext-note'}> (opens in a new tab)</span>
    </a>
  );
}
