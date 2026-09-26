import { motion } from 'framer-motion';
import { ChartColumnBig, Inbox, Layers3, ScanLine } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { listReviewQueue } from '@/api';
import { AnimatedNumber } from '@/components/bits';
import { cn } from '@/lib/utils';
import BatchMonitor from '@/screens/BatchMonitor';
import Results from '@/screens/Results';
import ReviewQueue from '@/screens/ReviewQueue';
import TemplateDesigner from '@/screens/TemplateDesigner';

const ROUTES = [
  { key: 'templates', label: 'Templates', Icon: Layers3, Screen: TemplateDesigner, hint: 'Draw fields, save versions' },
  { key: 'batch', label: 'Batch', Icon: ScanLine, Screen: BatchMonitor, hint: 'Upload and monitor' },
  { key: 'review', label: 'Review', Icon: Inbox, Screen: ReviewQueue, hint: 'The human in the loop' },
  { key: 'results', label: 'Results', Icon: ChartColumnBig, Screen: Results, hint: 'Output and confidence' },
];

function parseHash() {
  const [key, ...rest] = window.location.hash.replace(/^#\/?/, '').split('/');
  return { key: ROUTES.some((r) => r.key === key) ? key : 'templates', param: rest.join('/') || null };
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [queue, setQueue] = useState({ review: 0, spot: 0, reachable: null });

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const refreshQueue = useCallback(async () => {
    try {
      const entries = await listReviewQueue('pending');
      setQueue({
        review: entries.filter((e) => e.entry_type !== 'spot_check').length,
        spot: entries.filter((e) => e.entry_type === 'spot_check').length,
        reachable: true,
      });
    } catch {
      setQueue((q) => ({ ...q, reachable: false }));
    }
  }, []);

  useEffect(() => {
    refreshQueue();
    const t = setInterval(refreshQueue, 5000);
    return () => clearInterval(t);
  }, [refreshQueue]);

  const current = ROUTES.find((r) => r.key === route.key);
  const { Screen } = current;

  return (
    <div className="flex h-full min-h-0">
      <aside className="flex w-[232px] shrink-0 flex-col border-r border-border bg-card">
        <div className="flex h-16 items-center gap-3 border-b border-border px-5">
          <div className="relative grid size-7 place-items-center rounded-sm border border-border-strong bg-well">
            <span className="size-2.5 rounded-[1px] bg-signal shadow-[0_0_10px_var(--signal-500)]" />
          </div>
          <div className="leading-tight">
            <div className="caps text-foreground">Extraction Console</div>
            <div className="font-mono text-[11px] text-muted-foreground">idp-pipeline · phase 1</div>
          </div>
        </div>

        <nav className="flex flex-col gap-0.5 p-3">
          {ROUTES.map(({ key, label, Icon, hint }) => {
            const active = key === route.key;
            return (
              <a
                key={key}
                href={`#/${key}`}
                className={cn(
                  'group relative flex h-10 items-center gap-3 rounded-md px-3 text-[13px] font-medium transition-colors',
                  active ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
              >
                {active && (
                  <motion.span layoutId="nav-rule" className="absolute inset-y-2 left-0 w-[2px] rounded-full bg-foreground" />
                )}
                <Icon className={cn('size-4', active ? 'text-foreground' : 'text-icon-muted group-hover:text-icon')} />
                <span className="flex-1">{label}</span>
                {key === 'review' && (queue.review > 0 || queue.spot > 0) && (
                  <span className="flex items-center gap-1">
                    {queue.review > 0 && (
                      <span className="grid h-5 min-w-5 place-items-center rounded-sm bg-tier-review px-1 font-mono text-[11px] font-semibold text-tier-review-foreground">
                        <AnimatedNumber value={queue.review} />
                      </span>
                    )}
                    {queue.spot > 0 && (
                      <span className="grid h-5 min-w-5 place-items-center rounded-sm border border-tier-spot-border px-1 font-mono text-[11px] text-tier-spot-foreground">
                        <AnimatedNumber value={queue.spot} />
                      </span>
                    )}
                  </span>
                )}
                <span className="sr-only">{hint}</span>
              </a>
            );
          })}
        </nav>

        <div className="mt-auto border-t border-border p-4">
          <div className="caps-sm mb-2 text-faint-foreground">Signal</div>
          <div className="flex flex-col gap-1.5 text-[12px] text-muted-foreground">
            <div className="flex items-center gap-2">
              <span className="size-2 rounded-[1px] bg-tier-auto" /> auto · trusted, quiet
            </div>
            <div className="flex items-center gap-2">
              <span className="size-2 rounded-[1px] border-[1.5px] border-tier-spot" /> spot check · glance
            </div>
            <div className="flex items-center gap-2">
              <span className="size-2 rounded-[1px] bg-tier-review" /> review · act
            </div>
          </div>
          <div className="mt-4 flex items-center gap-2 font-mono text-[11px] text-faint-foreground">
            <span
              className={cn(
                'size-1.5 rounded-full',
                queue.reachable === false ? 'bg-tier-review' : queue.reachable ? 'bg-graphite-300' : 'bg-graphite-600',
              )}
            />
            {queue.reachable === false ? 'API unreachable' : 'API connected'}
          </div>
        </div>
      </aside>

      <main className="relative min-w-0 flex-1 overflow-hidden">
        {/* Enter-only: an exit animation waited on the outgoing screen, and a live screen
            (the batch monitor re-renders every 250 ms) could hold the swap indefinitely. */}
        <motion.div
          key={route.key}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.2 }}
          className="h-full"
        >
          <Screen param={route.param} onQueueChange={refreshQueue} />
        </motion.div>
      </main>
    </div>
  );
}
