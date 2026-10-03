import { useEffect, useId, useState, type FormEvent } from 'react';
import { api, ApiError, SUPPORT_CONTACT_MAX } from '../../api';
import { useApp } from '../../state';
import { useFail, useSay } from './parts';

/** Characters that can't be shown (control and invisible direction marks): the server refuses them too. */
// eslint-disable-next-line no-control-regex
const HIDDEN_CHARS = /[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]/;

/** Why a typed name can't be saved, in plain words, or null when it's fine. */
export function contactProblem(text: string): string | null {
  const t = text.trim();
  if (t.length > SUPPORT_CONTACT_MAX) return `Please keep it to ${SUPPORT_CONTACT_MAX} letters or fewer.`;
  if (HIDDEN_CHARS.test(t)) return 'Some of these characters can’t be shown. Please type the name again.';
  return null;
}

/**
 * Settings › Safety and backups › Who to call for help (Iron Owl 2.0.0). The name shows on the
 * password screen, the Help page and in messages when something goes wrong. It's read back
 * through /api/auth/status (also while locked), so after a save the app re-reads it there.
 */
export function SupportContactCard() {
  const { authInfo, refreshAuthInfo } = useApp();
  const say = useSay();
  const fail = useFail();
  const uid = useId();
  const current = authInfo.supportContact ?? '';
  const [text, setText] = useState(current);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [serverProblem, setServerProblem] = useState<string | null>(null);

  // Follow the saved name until the person starts typing.
  useEffect(() => {
    if (!touched) setText(current);
  }, [current, touched]);

  useEffect(() => {
    void refreshAuthInfo();
  }, [refreshAuthInfo]);

  const problem = contactProblem(text) ?? serverProblem;
  const changed = text.trim() !== current;

  async function save(value: string, prev: string) {
    setBusy(true);
    setServerProblem(null);
    try {
      const res = await api.settings.setSupportContact(value);
      await refreshAuthInfo();
      setTouched(false);
      const who = res.support_contact;
      say(who ? `Saved. Iron Owl will tell you to call ${who}.` : 'Saved. No one is named now.', {
        undo: async () => {
          await api.settings.setSupportContact(prev);
          await refreshAuthInfo();
        },
      });
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        setServerProblem('Some of these characters can’t be shown. Please type the name again.');
      } else {
        fail('Couldn’t save who to call', e);
      }
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy || problem || !changed) return;
    void save(text.trim(), current);
  }

  return (
    <section className="st-card st-card-pad" aria-labelledby={`${uid}-h`}>
      <div>
        <h2 id={`${uid}-h`}>Who to call for help</h2>
        <p className="st-desc">
          If something goes wrong, Iron Owl shows this name so you know who to ask. It also shows on the password screen and the Help page.
        </p>
      </div>
      <form className="st-contact-form" onSubmit={onSubmit} noValidate>
        <label className="st-field" htmlFor={`${uid}-in`}>
          <span className="st-field-label">Name and phone number (optional)</span>
          <input
            id={`${uid}-in`}
            className="st-input"
            type="text"
            autoComplete="off"
            spellCheck={false}
            maxLength={SUPPORT_CONTACT_MAX + 20}
            placeholder="For example: Sam 555-0100"
            value={text}
            aria-invalid={problem ? true : undefined}
            aria-describedby={`${uid}-help`}
            onChange={(e) => {
              setTouched(true);
              setServerProblem(null);
              setText(e.target.value);
            }}
          />
        </label>
        <p id={`${uid}-help`} className={problem ? 'st-warn-line' : 'st-help'} role={problem ? 'alert' : undefined}>
          {problem ?? 'Leave it empty if no one helps you with Iron Owl.'}
        </p>
        <div>
          <button type="submit" className="st-btn st-btn-lg st-btn-primary" disabled={busy || !changed || !!problem}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </form>
    </section>
  );
}
