import { useEffect, useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type DashboardSetup } from '../../api';
import { Icon } from '../../components/Icon';
import { AddAccountModal } from '../../components/AddAccountModal';
import { allUsedText, slotsFull, slotsOf, type BankSlots } from '../../components/bankSlots';
import { useApp } from '../../state';
import { BANK_SETUP, PLAID_KEYS_URL, PLAID_SIGNUP_URL } from '../settings/plaidKeys';
import { KeepPrivateNote, PlaidLink } from '../settings/BankSetupParts';

const BANK_CONNECTION = '/settings/banks?focus=bank-connection';

/**
 * Brand-new (no accounts, no bank connections): the only thing on Home besides the header.
 * Three versions: no Plaid keys yet (the 3 steps), keys saved but no bank linked yet, and
 * keys Plaid rejected. Keys are never collected here; the steps lead to Settings.
 */
export function WelcomeCard({ setup }: { setup: DashboardSetup }) {
  const headId = useId();
  const { invalidate } = useApp();
  const [linking, setLinking] = useState(false);
  const keysSaved = !setup.keys_rejected && setup.plaid_source !== 'none';
  // Plaid Trial: 10 connections, ever (removing a bank doesn't give one back). All used: say so
  // instead of offering Plaid Link. The add window checks again when it opens.
  const [slots, setSlots] = useState<BankSlots | null>(null);
  useEffect(() => {
    if (!keysSaved) return;
    let live = true;
    api.plaid
      .status()
      .then((s) => live && setSlots(slotsOf(s)))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [keysSaved]);

  const manual = (
    <p className="home-welcome-foot">
      Rather not connect a bank? <Link to="/accounts?new=1">Add an account yourself</Link>.
    </p>
  );

  if (setup.keys_rejected) {
    return (
      <section className="home-welcome" aria-labelledby={headId}>
        <div>
          <h2 id={headId}>Iron Owl can’t use your Plaid keys</h2>
          <p className="home-welcome-lead">Plaid didn’t accept the keys saved in Iron Owl, so it can’t connect to your bank yet. Check them in Settings.</p>
        </div>
        <Link to={BANK_CONNECTION} className="home-cta">
          Check your Plaid keys
          <Icon name="arrow-right" />
        </Link>
        {manual}
      </section>
    );
  }

  if (setup.plaid_source !== 'none' && slotsFull(slots) && slots) {
    // Every connection on the Plaid plan is used: adding accounts by hand is the way on.
    return (
      <section className="home-welcome" aria-labelledby={headId}>
        <div>
          <h2 id={headId}>Welcome to Iron Owl</h2>
          <p className="home-welcome-lead" role="status">
            {allUsedText(slots.limit)}
          </p>
          <p className="home-welcome-note">You type in each balance, and Iron Owl reminds you when it’s time to update it.</p>
        </div>
        <Link to="/accounts?new=1" className="home-cta">
          Add an account yourself
          <Icon name="arrow-right" />
        </Link>
      </section>
    );
  }

  if (setup.plaid_source !== 'none') {
    return (
      <section className="home-welcome" aria-labelledby={headId}>
        <div>
          <h2 id={headId}>Welcome to Iron Owl</h2>
          <p className="home-welcome-lead">Your Plaid keys are saved. Now sign in to your bank.</p>
          <p className="home-welcome-note">Plaid opens a secure window where you pick your bank and sign in. Iron Owl never sees your bank password.</p>
        </div>
        <button type="button" className="home-cta" onClick={() => setLinking(true)}>
          <Icon name="bank" />
          Link your bank
        </button>
        {manual}
        <AddAccountModal open={linking} start={{ step: 'link', kind: 'bank' }} onClose={() => setLinking(false)} onDone={() => invalidate()} />
      </section>
    );
  }

  return (
    <section className="home-welcome" aria-labelledby={headId}>
      <div>
        <h2 id={headId}>Welcome to Iron Owl</h2>
        <p className="home-welcome-lead">
          Connect your bank and this page will show how much is in checking, which bills are coming, and what’s left to spend this month.
        </p>
      </div>
      <Link to={BANK_CONNECTION} className="home-cta">
        Connect your bank
        <Icon name="arrow-right" />
      </Link>
      <ol className="home-steps">
        <li className="home-step">
          <span className="home-step-n" aria-hidden="true">
            1
          </span>
          <div className="home-step-body">
            <h3>{BANK_SETUP.account.title}</h3>
            <p>{BANK_SETUP.account.body}</p>
            <PlaidLink href={PLAID_SIGNUP_URL} className="home-step-link">
              {BANK_SETUP.account.link}
            </PlaidLink>
          </div>
        </li>
        <li className="home-step">
          <span className="home-step-n" aria-hidden="true">
            2
          </span>
          <div className="home-step-body">
            <h3>{BANK_SETUP.keys.title}</h3>
            <p>{BANK_SETUP.keys.body}</p>
            <dl className="home-keys">
              <div>
                <dt>{BANK_SETUP.keys.clientId.name}</dt>
                <dd>{BANK_SETUP.keys.clientId.desc}</dd>
              </div>
              <div>
                <dt>{BANK_SETUP.keys.secret.name}</dt>
                <dd>{BANK_SETUP.keys.secret.desc}</dd>
              </div>
            </dl>
            <KeepPrivateNote />
            <PlaidLink href={PLAID_KEYS_URL} className="home-step-link">
              {BANK_SETUP.keys.link}
            </PlaidLink>
          </div>
        </li>
        <li className="home-step">
          <span className="home-step-n" aria-hidden="true">
            3
          </span>
          <div className="home-step-body">
            <h3>{BANK_SETUP.paste.title}</h3>
            <p>
              {BANK_SETUP.paste.body} {BANK_SETUP.paste.password}
            </p>
            <Link to={BANK_CONNECTION} className="home-step-link">
              {BANK_SETUP.paste.link}
              <Icon name="arrow-right" />
            </Link>
          </div>
        </li>
      </ol>
      <p className="home-welcome-foot">{BANK_SETUP.time}</p>
    </section>
  );
}
