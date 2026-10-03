import {
  useId,
  useState,
  type CSSProperties,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type Ref,
} from 'react';
import { formatMoney, hueFor, initialsFor, type MoneyOptions } from '../lib/format';
import { Icon, type IconName } from './Icon';

/** Money with tabular numerals. `tone` colors it semantically. */
export function Money({
  value,
  tone,
  className,
  splitCents,
  ...opts
}: MoneyOptions & {
  value: number;
  /** auto: green when > 0, red when < 0. liab: liability ink. */
  tone?: 'auto' | 'liab' | 'in';
  className?: string;
  /** Render cents in a quieter weight (hero numbers). */
  splitCents?: boolean;
}) {
  const text = formatMoney(value, opts);
  let toneClass = '';
  if (tone === 'auto') toneClass = value > 0.004 ? 'gain' : value < -0.004 ? 'loss' : '';
  else if (tone === 'liab') toneClass = 'liab';
  else if (tone === 'in') toneClass = value > 0.004 ? 'amount-in' : '';
  const cls = ['num', toneClass, className].filter(Boolean).join(' ');

  if (splitCents) {
    const m = /^(.*?)([.,]\d{2})(\D*)$/.exec(text);
    if (m) {
      return (
        <span className={cls}>
          {m[1]}
          <span className="cents">{m[2]}</span>
          {m[3]}
        </span>
      );
    }
  }
  return <span className={cls}>{text}</span>;
}

/** sm 26 · (default) 32 · md 36 · lg 40 · xl 44 */
export function Avatar({ name, size }: { name: string; size?: 'sm' | 'md' | 'lg' | 'xl' }) {
  const style = { '--h': hueFor(name) } as CSSProperties;
  return (
    <span className={`avatar${size ? ` avatar-${size}` : ''}`} style={style} aria-hidden="true">
      {initialsFor(name)}
    </span>
  );
}

export function Skeleton({ width, height, className, style }: { width?: number | string; height?: number | string; className?: string; style?: CSSProperties }) {
  return <span className={`skel ${className ?? ''}`} style={{ width, height, ...style }} aria-hidden="true" />;
}

export function SkeletonRows({ rows = 4, label = 'Loading' }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <div className="skel-row" key={i}>
          <Skeleton width={32} height={32} style={{ borderRadius: 9 }} />
          <div style={{ flex: 1 }}>
            <Skeleton className="skel-text" width={`${40 + ((i * 17) % 30)}%`} />
            <Skeleton className="skel-text" width={`${20 + ((i * 11) % 20)}%`} />
          </div>
          <Skeleton width={90} height={14} />
        </div>
      ))}
    </div>
  );
}

export function Banner({
  tone = 'warn',
  icon,
  children,
  action,
}: {
  tone?: 'warn' | 'info' | 'error';
  icon?: IconName;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={`banner${tone === 'info' ? ' banner-info' : tone === 'error' ? ' banner-error' : ''}`} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon name={icon ?? (tone === 'info' ? 'info' : 'alert')} />
      <div className="banner-body">{children}</div>
      {action}
    </div>
  );
}

/**
 * Labelled password field with a show/hide toggle and a Caps Lock warning (on by default:
 * "Caps Lock is on. Passwords care about capital letters." while the box has focus).
 * `size="xl"`: the 52px box and text Show/Hide button used on the unlock and recovery screens.
 */
export function PasswordField({
  label,
  hint,
  error,
  inputRef,
  toggleNoun = 'password',
  capsLockWarning = true,
  size,
  errorLive = true,
  ...input
}: {
  label: string;
  hint?: ReactNode;
  error?: ReactNode;
  inputRef?: Ref<HTMLInputElement>;
  toggleNoun?: string;
  capsLockWarning?: boolean;
  size?: 'xl';
  /** false when the caller announces the error itself (the error still describes the box). */
  errorLive?: boolean;
} & Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'size'>) {
  const id = useId();
  const [shown, setShown] = useState(false);
  const [caps, setCaps] = useState(false);
  const [focused, setFocused] = useState(false);
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-err` : undefined;
  const capsId = `${id}-caps`;
  const showCaps = capsLockWarning && caps && focused && !input.disabled;
  const { onKeyDown, onKeyUp, onMouseDown, onFocus, onBlur, ...rest } = input;

  const readCaps = (e: KeyboardEvent<HTMLInputElement> | MouseEvent<HTMLInputElement>) => {
    if (capsLockWarning && typeof e.getModifierState === 'function') {
      setCaps(e.getModifierState('CapsLock'));
      setFocused(true); // a key or click in the box means it has focus, even if the focus event was missed
    }
  };
  const xl = size === 'xl';
  const describedBy = [hintId, errId, showCaps ? capsId : undefined].filter(Boolean).join(' ') || undefined;
  const toggleLabel = `${shown ? 'Hide' : 'Show'} ${toggleNoun}`;

  return (
    <div className={`field${xl ? ' field-xl' : ''}`}>
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <div className={xl ? 'pw-row' : 'input-affix'}>
        <input
          ref={inputRef}
          id={id}
          type={shown ? 'text' : 'password'}
          className={xl ? 'input input-xl' : 'input input-lg has-toggle'}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          onKeyDown={(e) => {
            readCaps(e);
            onKeyDown?.(e);
          }}
          onKeyUp={(e) => {
            readCaps(e);
            onKeyUp?.(e);
          }}
          onMouseDown={(e) => {
            readCaps(e);
            onMouseDown?.(e);
          }}
          onFocus={(e) => {
            setFocused(true);
            onFocus?.(e);
          }}
          onBlur={(e) => {
            setFocused(false);
            onBlur?.(e);
          }}
          {...rest}
        />
        {xl ? (
          <button type="button" className="btn pw-toggle-xl" onClick={() => setShown((s) => !s)} aria-label={toggleLabel} aria-pressed={shown}>
            {shown ? 'Hide' : 'Show'}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-sm pw-toggle"
            onClick={() => setShown((s) => !s)}
            aria-label={toggleLabel}
            aria-pressed={shown}
          >
            <Icon name={shown ? 'eyeOff' : 'eye'} />
          </button>
        )}
      </div>
      {capsLockWarning && (
        <div role="status" className="caps-live">
          {showCaps && (
            <div className="caps-warn" id={capsId}>
              <Icon name="alert" />
              <span>
                <strong>Caps Lock is on.</strong> Passwords care about capital letters.
              </span>
            </div>
          )}
        </div>
      )}
      {hint && (
        <div className="field-hint" id={hintId}>
          {hint}
        </div>
      )}
      {error && (
        <div className="field-error" id={errId} role={errorLive ? 'alert' : undefined}>
          {error}
        </div>
      )}
    </div>
  );
}

/** A dollar-amount input (text + inputMode so we control parsing). */
export function parseMoneyInput(raw: string): number | null {
  const cleaned = raw.replace(/[\s$,]/g, '').replace(/−/g, '-');
  if (cleaned === '' || cleaned === '-' || cleaned === '.') return null;
  if (!/^-?\d*(\.\d{0,2})?$/.test(cleaned)) return NaN;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : NaN;
}

export function Spinner({ label }: { label?: string }) {
  return <Icon name="sync" className="spin" aria-label={label} />;
}
