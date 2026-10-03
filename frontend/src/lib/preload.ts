/**
 * A page loaded once, ahead of time, so it still opens when Iron Owl's server has stopped
 * (the "Iron Owl isn't running" screen can't fetch new code). `isReady()` says whether it
 * loaded; a failed load is tried again on the next `load()`.
 */
export function preloadable<T>(loader: () => Promise<T>) {
  let pending: Promise<T> | null = null;
  let ready = false;
  return {
    load(): Promise<T> {
      if (!pending) {
        pending = loader().then(
          (mod) => {
            ready = true;
            return mod;
          },
          (err: unknown) => {
            pending = null;
            throw err;
          },
        );
      }
      return pending;
    },
    isReady: () => ready,
  };
}
