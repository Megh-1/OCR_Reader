import { Slot } from '@radix-ui/react-slot';
import { cva } from 'class-variance-authority';
import { cn } from '@/lib/utils';

// shadcn/ui Button, on the console's tokens. Primary is the bright neutral key, never the
// accent: orange is reserved for "a person is needed" and never used to decorate an action.
const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium transition-[background-color,border-color,color,transform] duration-150 disabled:pointer-events-none disabled:opacity-40 active:translate-y-px [&_svg]:pointer-events-none [&_svg]:shrink-0 cursor-pointer select-none',
  {
    variants: {
      variant: {
        primary: 'bg-primary text-primary-foreground hover:bg-white [&_svg]:text-primary-foreground',
        secondary: 'bg-secondary text-foreground border border-border-strong hover:bg-graphite-700 hover:border-graphite-600',
        outline: 'border border-border-strong text-foreground hover:bg-accent',
        ghost: 'text-muted-foreground hover:text-foreground hover:bg-accent',
        destructive: 'bg-destructive text-tier-review-foreground hover:bg-signal-400',
      },
      size: {
        sm: 'h-7 px-2.5 text-[12px] [&_svg]:size-3.5',
        md: 'h-8 px-3 text-[13px] [&_svg]:size-4',
        lg: 'h-10 px-4 text-[14px] [&_svg]:size-4',
        icon: 'size-8 [&_svg]:size-4',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
);

export function Button({ className, variant, size, asChild = false, ...props }) {
  const Comp = asChild ? Slot : 'button';
  return <Comp data-slot="button" className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}

export { buttonVariants };
