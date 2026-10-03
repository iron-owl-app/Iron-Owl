import { useEffect, useId, useState, type ChangeEvent } from 'react';
import { TEXT_SIZES, useTextSize, type TextSize } from '../../lib/textSize';
import { useTheme } from '../../lib/theme';
import { themeLabel, type ThemeChoice } from '../../lib/themeCore';
import { ThemeDot, ThemeRadios } from '../../components/ThemePicker';
import { useUpdates } from '../../updates/UpdateProvider';
import { ABOUT, GITHUB, SOURCE_OFF, displayVersion } from '../../updates/copy';
import { useSettings } from './context';
import { StSwitch, useSay } from './parts';

/** The "Aa" samples stay 22 / 27 / 32 px whatever size is picked, so the three compare. */
const SAMPLE_PX: Record<TextSize, number> = { normal: 22, large: 27, larger: 32 };

/**
 * Settings › Colors, text size and updates (design D7 tab 7). Colors (Release 3.16): Match
 * Windows / Light / Dark / Warm night, the same choice as the sidebar's Colors button. Text size scales the root font size,
 * so every page (the sidebar, dialogs and the lock screen too) grows right away, with Undo.
 * Updates: the version, whether it's up to date, and "Install from a file" (design D5). Copies
 * that update from GitHub (Iron Owl 2.0.0) also get "Check now", the last check, any problem
 * with it (amber) and the "Check for updates once a day" switch.
 */
export function AppTab() {
  return (
    <>
      <ThemeCard />
      <TextSizeCard />
      <UpdatesCard />
    </>
  );
}

function ThemeCard() {
  const [theme, setTheme] = useTheme();
  const say = useSay();
  const uid = useId();

  function pick(next: ThemeChoice) {
    if (next === theme) return;
    const prev = theme;
    setTheme(next);
    say(`Colors are now ${themeLabel(next)}.`, { undo: async () => setTheme(prev) });
  }

  return (
    <section className="st-card st-card-pad" aria-labelledby={`${uid}-h`}>
      <div>
        <h2 id={`${uid}-h`}>How Iron Owl looks</h2>
        <p className="st-desc">Pick the colors you like. Warm night is dark with soft, warm colors, easy on the eyes in the evening.</p>
      </div>
      <ThemeRadios
        value={theme}
        onPick={pick}
        labelledBy={`${uid}-h`}
        className="st-themes"
        itemClassName="st-theme"
        renderItem={(t) => (
          <>
            <ThemeDot id={t.id} big />
            <span className="st-theme-label">{t.label}</span>
            <span className="st-theme-hint">{t.hint}</span>
          </>
        )}
      />
    </section>
  );
}

function TextSizeCard() {
  const [size, setSize] = useTextSize();
  const say = useSay();
  const uid = useId();

  function pick(next: TextSize) {
    if (next === size) return;
    const prev = size;
    setSize(next);
    const label = TEXT_SIZES.find((t) => t.id === next)!.label;
    say(`Text size is now ${label}.`, { undo: async () => setSize(prev) });
  }

  return (
    <section className="st-card st-card-pad" aria-labelledby={`${uid}-h`}>
      <div>
        <h2 id={`${uid}-h`}>Text size</h2>
        <p className="st-desc">Makes words and numbers bigger on every page. You’ll see the change right away.</p>
      </div>
      <div role="radiogroup" aria-labelledby={`${uid}-h`} className="st-sizes">
        {TEXT_SIZES.map((t, i) => (
          <button
            key={t.id}
            type="button"
            role="radio"
            className="st-size"
            aria-checked={size === t.id}
            tabIndex={size === t.id ? 0 : -1}
            onClick={() => pick(t.id)}
            onKeyDown={(e) => {
              const n = TEXT_SIZES.length;
              const j = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? (i + 1) % n : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? (i + n - 1) % n : -1;
              if (j < 0) return;
              e.preventDefault();
              pick(TEXT_SIZES[j]!.id);
              (e.currentTarget.parentElement?.children[j] as HTMLElement | undefined)?.focus();
            }}
          >
            <span className="st-size-aa" style={{ fontSize: `${SAMPLE_PX[t.id]}px` }} aria-hidden="true">
              Aa
            </span>
            <span className="st-size-label">{t.label}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function UpdatesCard() {
  const { status, statusError, contact, checkingFile, openNews, checkFile, refresh, flow, checkNow, setAutoCheck } = useUpdates();
  const { focus } = useSettings();
  const say = useSay();
  const uid = useId();
  const inputId = useId();
  const [checking, setChecking] = useState(false);
  const [checkedNow, setCheckedNow] = useState(false);
  const [savingAuto, setSavingAuto] = useState(false);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Old deep link /settings?focus=updates: after Layout's own focus move, put focus on this card.
  useEffect(() => {
    if (focus !== 'updates') return;
    const h = window.setTimeout(() => {
      const el = document.getElementById(`${uid}-h`);
      el?.scrollIntoView({ block: 'center' });
      el?.focus({ preventScroll: true });
    });
    return () => window.clearTimeout(h);
  }, [focus, uid]);

  function onPick(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = '';
    if (f) checkFile(f);
  }

  async function onCheckNow() {
    if (checking) return;
    setChecking(true);
    setCheckedNow(false);
    const ok = await checkNow();
    setChecking(false);
    if (ok) setCheckedNow(true);
    else say(GITHUB.failed);
  }

  async function onAuto(next: boolean) {
    if (savingAuto) return;
    setSavingAuto(true);
    const ok = await setAutoCheck(next);
    setSavingAuto(false);
    if (ok) say(GITHUB.autoOn(next), { undo: async () => void (await setAutoCheck(!next)) });
    else say(GITHUB.switchFailed);
  }

  const mode = status?.mode;
  const source = status?.source;
  const sourceOff = mode === 'enabled' && source === 'off';
  const canUpdate = mode === 'enabled' && !sourceOff;
  const github = canUpdate && source === 'github';
  const offer = status?.offer ?? null;
  const git = mode === 'git' || mode === 'dev';
  const busy = checkingFile !== null || flow.kind === 'news' || checking;
  const version = status ? status.current.display_version || displayVersion(status.current.version) : '';
  const installedAt = status?.current.installed_at ? new Date(status.current.installed_at) : null;
  const installed = installedAt && !Number.isNaN(installedAt.getTime()) ? installedAt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : null;
  const c = contact?.trim();

  return (
    <section className="st-card st-card-pad st-updates" id="about-updates" aria-labelledby={`${uid}-h`}>
      <div className="st-updates-text">
        <h2 id={`${uid}-h`} tabIndex={-1}>
          Updates
        </h2>
        {!status ? (
          <p className="st-desc">{statusError ? ABOUT.loadError : '…'}</p>
        ) : (
          <>
            <p className="st-desc">
              Version {version}
              {canUpdate &&
                (offer?.can_install ? (
                  <>
                    {' · '}
                    <span className="st-status-ready">{ABOUT.ready(offer.version)}</span>
                  </>
                ) : offer ? (
                  <>
                    {' · '}
                    <span className="st-status-ready">{ABOUT.needsHelp(offer.version, contact)}</span>
                  </>
                ) : (
                  <>
                    {' · '}
                    <span className="st-status-ok">{ABOUT.upToDate}</span>
                  </>
                ))}
              {git && ` · ${ABOUT.git}`}
              {installed && !git ? ` · installed ${installed}` : ''}
            </p>
            {canUpdate && !github && (
              <p className="st-desc-2">{c ? `${c} emails updates as a file.` : 'Updates come by email as a file.'} Iron Owl finds it in Downloads by itself.</p>
            )}
            {github && (
              <>
                <p className="st-desc-2">{GITHUB.explain}</p>
                <p className="st-desc-2">{GITHUB.lastChecked(status.last_check)}</p>
                {status.check_error && <p className="st-warn-line st-updates-problem">{status.check_error}</p>}
              </>
            )}
            {sourceOff && <p className="st-desc-2">{SOURCE_OFF}</p>}
            {git && <p className="st-desc-2">{ABOUT.gitText}</p>}
            {mode === 'not_installed' && <p className="st-desc-2">{ABOUT.notInstalled(contact)}</p>}
            <p className="st-desc-2" role="status">
              {checkingFile ? ABOUT.checking(checkingFile) : checking ? GITHUB.checking : checkedNow && github && !offer && !status.check_error ? GITHUB.checked : ''}
            </p>
          </>
        )}
      </div>
      {canUpdate && (
        <div className="st-updates-actions">
          {offer?.can_install && (
            <button type="button" className="st-btn st-btn-primary" onClick={openNews} disabled={busy}>
              {ABOUT.install}
            </button>
          )}
          {github && (
            <button type="button" className="st-btn st-btn-outline" onClick={() => void onCheckNow()} disabled={busy} aria-busy={checking}>
              {checking ? GITHUB.checking : GITHUB.checkNow}
            </button>
          )}
          <label htmlFor={inputId} className={`st-link st-link-44${busy ? ' is-disabled' : ''}`}>
            Install from a file →
            <input id={inputId} type="file" accept=".ftupdate" className="st-file-input" onChange={onPick} disabled={busy} />
          </label>
        </div>
      )}
      {github && status && (
        <div className="st-row st-updates-auto">
          <span className="st-row-text">
            <span className="st-label">{GITHUB.auto}</span>
            <span className="st-help" id={`${uid}-auto`}>
              {GITHUB.autoHelp} {GITHUB.privacy}
            </span>
          </span>
          <StSwitch checked={status.auto_check} onChange={(on) => void onAuto(on)} label={GITHUB.auto} describedBy={`${uid}-auto`} disabled={savingAuto} />
        </div>
      )}
    </section>
  );
}
