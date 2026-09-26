import { cn } from '@/lib/utils';

export function Card({ className, ...props }) {
  return <div data-slot="card" className={cn('rounded-lg border border-border bg-card', className)} {...props} />;
}

export function CardHeader({ className, ...props }) {
  return (
    <div
      data-slot="card-header"
      className={cn('flex items-center justify-between gap-3 border-b border-border px-4 h-11', className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }) {
  return <div data-slot="card-title" className={cn('caps text-secondary-foreground', className)} {...props} />;
}

export function CardContent({ className, ...props }) {
  return <div data-slot="card-content" className={cn('p-4', className)} {...props} />;
}

export function Kbd({ className, ...props }) {
  return (
    <kbd
      className={cn(
        'inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-sm border border-border-strong bg-muted px-1 font-mono text-[10px] text-muted-foreground',
        className,
      )}
      {...props}
    />
  );
}

export function Separator({ className, vertical = false }) {
  return <div className={cn(vertical ? 'w-px self-stretch' : 'h-px w-full', 'bg-border', className)} />;
}
