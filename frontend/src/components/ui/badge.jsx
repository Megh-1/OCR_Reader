import { cva } from 'class-variance-authority';
import { cn } from '@/lib/utils';

// The one pill. Tone says what kind of state it is; the label and icon always say it too,
// so no meaning is carried by color alone.
const badgeVariants = cva('inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-sm border text-xs font-medium', {
  variants: {
    tone: {
      neutral: 'border-border bg-surface-sunken text-foreground-secondary',
      primary: 'border-primary-surface bg-primary-surface text-primary-text',
      success: 'border-success-border bg-success-surface text-success',
      warning: 'border-warning-border bg-warning-surface text-warning',
      danger: 'border-danger-border bg-danger-surface text-danger',
    },
    size: {
      sm: 'h-5 px-1',
      md: 'h-6 px-2',
    },
  },
  defaultVariants: { tone: 'neutral', size: 'md' },
});

export function Badge({ tone, size, className, children, ...props }) {
  return (
    <span data-slot="badge" className={cn(badgeVariants({ tone, size }), className)} {...props}>
      {children}
    </span>
  );
}
