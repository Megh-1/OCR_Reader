import { useCallback, useState } from 'react';

// "Advanced tools" = the Templates screen and the Evidence tab. They are hidden by default (the demo path is
// Upload -> Results -> Review) and nothing is deleted: set VITE_SHOW_ADVANCED=1 at build time, or flip the
// switch at the bottom of the sidebar (remembered in this browser).
const KEY = 'idp.showAdvanced';

function read() {
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored !== null) return stored === '1';
  } catch {
    /* storage blocked: fall back to the build-time default */
  }
  return import.meta.env.VITE_SHOW_ADVANCED === '1';
}

export function showAdvanced() {
  return read();
}

export function useAdvanced() {
  const [on, setOn] = useState(read);
  const toggle = useCallback(() => {
    setOn((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(KEY, next ? '1' : '0');
      } catch {
        /* the switch still works for this page view */
      }
      return next;
    });
  }, []);
  return [on, toggle];
}
