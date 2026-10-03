import { useId, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import type { PlaidItem } from '../../api';
import { hueFor, initialsFor } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { useSettings } from './context';
import { Chip, type ChipTone } from './parts';

const CHIP: Record<PlaidItem['status'], { tone: ChipTone; label: string }> = {
  ok: { tone: 'ok', label: 'Connected' },
  login_required: { tone: 'bad', label: 'Sign in again' },
  pending: { tone: 'warn', label: 'Not finished' },
  error: { tone: 'warn', label: 'Not updating' },
};

/**
 * Settings › Banks (design D7 tab 5): one compact row per bank connection with its status.
 * A bank that needs sign-in gets a tinted row and "Sign in again" (Plaid's window, the shared
 * useBankReauth). Everything else about banks is on Manage banks (/settings/banks).
 */
export function BanksTab() {
  const { items, reauth, retryBank, retryingBank } = useSettings();
  const uid = useId();
  const list = items.data ?? [];

  return (
    <section className="st-card is-clip" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Banks</h2>
          <p className="st-desc">Where Iron Owl gets your balances and purchases.</p>
        </div>
        <Link to="/settings/banks" className="st-link st-link-44">
          Manage banks →
        </Link>
      </div>
      {items.loading && !items.data ? (
        <div className="st-loading" aria-busy="true" aria-label="Loading banks">
          <Skeleton height={48} style={{ borderRadius: 10 }} />
        </div>
      ) : items.error && !items.data ? (
        <p className="st-error">
          Iron Owl couldn’t load your banks.{' '}
          <button type="button" className="st-link st-link-inline" onClick={items.reload}>
            Try again
          </button>
        </p>
      ) : list.length === 0 ? (
        <p className="st-empty">
          No banks are connected yet. You can connect one on{' '}
          <Link to="/settings/banks?focus=connections" className="st-link st-link-inline">
            Manage banks
          </Link>
          .
        </p>
      ) : (
        list.map((it) => {
          const name = it.institution_name ?? 'Unknown bank';
          const chip = CHIP[it.status] ?? CHIP.error;
          const needs = it.status === 'login_required';
          const busy = reauth.busyItemId === it.id;
          return (
            <div key={it.id} className={`st-item${needs ? ' is-needs' : ''}`}>
              <span className="st-avatar" style={{ '--h': hueFor(name) } as CSSProperties} aria-hidden="true">
                {initialsFor(name)}
              </span>
              <span className="st-bank-main">
                <span className="st-bank-name">{name}</span>
                <Chip tone={chip.tone}>{chip.label}</Chip>
              </span>
              {needs && (
                <button
                  type="button"
                  className="st-btn st-btn-light"
                  onClick={() => reauth.start(it)}
                  disabled={reauth.busyItemId !== null && !busy}
                  aria-disabled={busy || undefined}
                  aria-label={busy ? undefined : `Sign in again to ${name}`}
                >
                  {busy ? (reauth.phase === 'updating' ? 'Updating…' : 'Opening sign-in…') : 'Sign in again'}
                </button>
              )}
              {it.status === 'pending' && (
                <Link to={`/settings/banks?finish=${it.id}`} className="st-btn st-btn-plain">
                  Finish setting up
                </Link>
              )}
              {it.status === 'error' && (
                <button type="button" className="st-btn st-btn-plain" onClick={() => void retryBank(it)} disabled={retryingBank !== null}>
                  {retryingBank === it.id ? 'Trying again…' : 'Try again'}
                </button>
              )}
            </div>
          );
        })
      )}
    </section>
  );
}
