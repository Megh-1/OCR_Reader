import { Slot } from '@radix-ui/react-slot';
import { cva } from 'class-variance-authority';
import { cn } from '@/lib/utils';

// Heights: sm 32, md 40 (default), lg 48. Focus uses the global :focus-visible ring.
const buttonVariants = cva(
  'inline-flex shrink-0 cursor-pointer select-none items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'bg-primary text-primary-foreground shadow-sm hover:bg-primary-hover active:bg-primary-active',
        secondary: 'bg-surface-sunken text-foreground hover:bg-border active:bg-border-strong',
        outline: 'border border-border-strong bg-surface text-foreground shadow-sm hover:bg-surface-sunken active:bg-border',
        ghost: 'text-foreground-secondary hover:bg-surface-sunken hover:text-foreground active:bg-border',
        destructive: 'bg-danger text-primary-foreground shadow-sm hover:bg-danger-hover',
      },
      size: {
        sm: 'h-8 px-3 [&_svg]:size-4',
        md: 'h-10 px-4 [&_svg]:size-4',
        lg: 'h-12 px-6 text-base [&_svg]:size-5',
        icon: 'size-8 [&_svg]:size-4',
      },
    },
    defaultVariants: { variant: 'outline', size: 'md' },
  },
);

export function Button({ className, variant, size, asChild = false, ...props }) {
  const Comp = asChild ? Slot : 'button';
  return <Comp data-slot="button" className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
