import { cn } from '@/lib/utils';

export function Card({ className, ...props }) {
  return <div data-slot="card" className={cn('rounded-lg border border-border bg-surface shadow-sm', className)} {...props} />;
}

export function CardHeader({ className, ...props }) {
  return (
    <div
      data-slot="card-header"
      className={cn('flex h-12 items-center justify-between gap-3 border-b border-border px-4', className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }) {
  return <h2 data-slot="card-title" className={cn('text-sm font-semibold text-foreground', className)} {...props} />;
}

export function CardContent({ className, ...props }) {
  return <div data-slot="card-content" className={cn('p-4', className)} {...props} />;
}

export function Kbd({ className, ...props }) {
  return (
    <kbd
      className={cn(
        'inline-flex h-6 min-w-6 items-center justify-center rounded-sm border border-border-strong bg-surface-sunken px-1 font-mono text-xs text-foreground-secondary',
        className,
      )}
      {...props}
    />
  );
}

export function Separator({ className, vertical = false }) {
  return <div role="separator" className={cn(vertical ? 'w-px self-stretch' : 'h-px w-full', 'bg-border', className)} />;
}
