import { useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';

/**
 * A confirmation for something that cannot be undone. Focus starts on Cancel, Escape and the backdrop cancel, and the
 * action is named for what it does. The one place in the console that floats, so it is the one that takes shadow-md.
 */
export function ConfirmDialog({ open, title, children, confirmLabel, tone = 'primary', busy = false, onConfirm, onCancel }) {
  const cancelRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    cancelRef.current?.focus();
    const onKey = (e) => e.key === 'Escape' && !busy && onCancel();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-foreground/40 p-4" onMouseDown={(e) => e.target === e.currentTarget && !busy && onCancel()}>
      <div role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" className="flex w-full max-w-md flex-col gap-4 rounded-lg border border-border bg-surface p-6 shadow-md">
        <h2 id="confirm-title" className="text-lg text-foreground">
          {title}
        </h2>
        <div className="flex flex-col gap-2 text-sm text-foreground-secondary">{children}</div>
        <div className="flex justify-end gap-2">
          <Button ref={cancelRef} variant="outline" disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
          <Button variant={tone === 'danger' ? 'destructive' : 'primary'} disabled={busy} onClick={onConfirm}>
            {busy ? 'Working…' : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
