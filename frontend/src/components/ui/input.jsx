import { cn } from '@/lib/utils';

const control =
  'h-10 w-full min-w-0 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground transition-colors placeholder:text-foreground-subtle hover:border-foreground-subtle focus-visible:border-primary focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-ring disabled:cursor-not-allowed disabled:bg-surface-sunken disabled:text-foreground-subtle aria-invalid:border-danger';

export function Input({ className, mono = false, ...props }) {
  return <input data-slot="input" className={cn(control, mono && 'font-mono', className)} {...props} />;
}

export function NativeSelect({ className, children, ...props }) {
  return (
    <select data-slot="select" className={cn(control, 'cursor-pointer', className)} {...props}>
      {children}
    </select>
  );
}

export function Label({ className, ...props }) {
  return <label data-slot="label" className={cn('label flex flex-col gap-1', className)} {...props} />;
}
