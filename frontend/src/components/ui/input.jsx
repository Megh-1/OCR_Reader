import { cn } from '@/lib/utils';

export function Input({ className, mono = false, ...props }) {
  return (
    <input
      data-slot="input"
      className={cn(
        'h-8 w-full min-w-0 rounded-md border border-input bg-well px-2.5 text-[13px] text-foreground shadow-[inset_0_1px_2px_rgba(0,0,0,0.6)] outline-none transition-colors placeholder:text-faint-foreground focus-visible:border-ring focus-visible:outline-none disabled:opacity-40 aria-invalid:border-tier-review',
        mono && 'font-mono',
        className,
      )}
      {...props}
    />
  );
}

export function NativeSelect({ className, children, ...props }) {
  return (
    <select
      data-slot="select"
      className={cn(
        'h-8 w-full rounded-md border border-input bg-well px-2 text-[13px] text-foreground outline-none focus-visible:border-ring cursor-pointer',
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
}

export function Label({ className, ...props }) {
  return <label data-slot="label" className={cn('caps text-muted-foreground flex flex-col gap-1.5', className)} {...props} />;
}
