import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Icon, type IconName } from './Icon';

export type ToastTone = 'success' | 'error' | 'info' | 'warn';

export interface ToastInput {
  tone?: ToastTone;
  title: string;
  body?: ReactNode;
  /** ms; 0 = stay until closed. Defaults: 6s; warnings 10s; errors stay until closed. */
  timeout?: number;
}

interface ToastItem extends Required<Pick<ToastInput, 'tone' | 'title'>> {
  id: number;
  body?: ReactNode;
  timeout: number;
}

interface ToastApi {
  push: (t: ToastInput) => number;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast outside ToastProvider');
  return ctx;
}

const ICON: Record<ToastTone, IconName> = { success: 'check', error: 'x', info: 'info', warn: 'alert' };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((x) => x.id !== id)), []);
  const push = useCallback((t: ToastInput) => {
    const id = nextId.current++;
    const tone = t.tone ?? 'info';
    const timeout = t.timeout ?? (tone === 'error' ? 0 : tone === 'warn' ? 10000 : 6000);
    setItems((xs) => [...xs.slice(-3), { id, tone, title: t.title, body: t.body, timeout }]);
    return id;
  }, []);

  const api = useMemo(() => ({ push, dismiss }), [push, dismiss]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" role="region" aria-label="Notifications">
        <div aria-live="polite" aria-relevant="additions">
          {items
            .filter((t) => t.tone !== 'error')
            .map((t) => (
              <ToastView key={t.id} item={t} onDismiss={dismiss} />
            ))}
        </div>
        <div aria-live="assertive" aria-relevant="additions">
          {items
            .filter((t) => t.tone === 'error')
            .map((t) => (
              <ToastView key={t.id} item={t} onDismiss={dismiss} />
            ))}
        </div>
      </div>
    </ToastContext.Provider>
  );
}

function ToastView({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (!item.timeout || paused) return;
    const h = window.setTimeout(() => onDismiss(item.id), item.timeout);
    return () => window.clearTimeout(h);
  }, [item.id, item.timeout, paused, onDismiss]);

  return (
    <div
      className={`toast toast-${item.tone}`}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <span className="toast-icon" aria-hidden="true">
        <Icon name={ICON[item.tone]} />
      </span>
      <div className="toast-content">
        <div className="toast-title">{item.title}</div>
        {item.body && <div className="toast-body">{item.body}</div>}
      </div>
      <button type="button" className="toast-close" onClick={() => onDismiss(item.id)} aria-label={`Close: ${item.title}`}>
        Close
      </button>
    </div>
  );
}
