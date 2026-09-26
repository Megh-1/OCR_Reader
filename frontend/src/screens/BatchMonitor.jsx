import { AnimatePresence, motion } from 'framer-motion';
import { ArrowUpRight, FileImage, FileText, History, Play, Upload, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { errorLines, getBatchStatus, listBatches, listTemplates, submitBatch } from '@/api';
import { AnimatedNumber, EmptyState, PageHeader, Readout, TierBar } from '@/components/bits';
import { StatusPill } from '@/components/StatusPill';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { NativeSelect } from '@/components/ui/input';
import { navigate } from '@/lib/nav';
import { tierCounts } from '@/lib/tiers';
import { cn, formatBytes, seconds, timeAgo } from '@/lib/utils';

const TERMINAL = ['completed', 'completed_with_errors', 'failed'];
const SEGMENT = {
  queued: 'bg-graphite-800',
  processing: 'bg-graphite-200 scanning',
  retrying: 'hatch-spot bg-tier-spot-surface',
  completed: 'bg-graphite-500',
  failed: 'bg-tier-review',
};

function docSummary(doc) {
  const pages = doc.result?.pages ?? [];
  const counts = { auto: 0, spot_check: 0, review: 0 };
  let resultId = null;
  let template = null;
  let unconfirmed = 0;
  for (const p of pages) {
    if (!p.confirmed) {
      unconfirmed += 1;
      continue;
    }
    resultId ??= p.extraction_result_id;
    template ??= `${p.result.template_id}@v${p.result.version}`;
    const c = tierCounts(p.result.fields);
    counts.auto += c.auto;
    counts.spot_check += c.spot_check;
    counts.review += c.review;
  }
  return { counts, resultId, template, unconfirmed };
}

export default function BatchMonitor({ param, onQueueChange }) {
  const [files, setFiles] = useState([]);
  const [typeHint, setTypeHint] = useState('');
  const [templates, setTemplates] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [batch, setBatch] = useState(null);
  const [recent, setRecent] = useState([]);
  const [now, setNow] = useState(Date.now());
  const inputRef = useRef(null);
  const batchId = param;

  useEffect(() => {
    listTemplates().then((t) => setTemplates(t.sort())).catch(() => setTemplates([]));
  }, []);

  const refreshRecent = useCallback(() => {
    listBatches(10).then(setRecent).catch(() => {});
  }, []);
  useEffect(refreshRecent, [refreshRecent]);

  // No batch in the URL: open the most recent one, so the monitor is never blank when there is history.
  useEffect(() => {
    if (!batchId && recent.length > 0) navigate('batch', recent[0].batch_id);
  }, [batchId, recent]);

  // Poll the status endpoint once a second until the batch reaches a terminal state.
  useEffect(() => {
    if (!batchId) return undefined;
    let cancelled = false;
    let timer;
    setBatch((b) => (b?.batch_id === batchId ? b : null));
    const tick = async () => {
      try {
        const s = await getBatchStatus(batchId);
        if (cancelled) return;
        setBatch(s);
        if (TERMINAL.includes(s.status)) {
          refreshRecent();
          onQueueChange?.();
          return;
        }
      } catch (err) {
        if (!cancelled) setError(errorLines(err).join('; '));
      }
      timer = setTimeout(tick, 1000);
    };
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [batchId, refreshRecent, onQueueChange]);

  const live = batch && !TERMINAL.includes(batch.status);
  useEffect(() => {
    if (!live) return undefined;
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [live]);

  function addFiles(list) {
    const incoming = Array.from(list).filter((f) => /\.(pdf|jpe?g|png)$/i.test(f.name));
    setFiles((prev) => {
      const seen = new Set(prev.map((f) => f.name + f.size));
      return [...prev, ...incoming.filter((f) => !seen.has(f.name + f.size))].slice(0, 50);
    });
  }

  async function handleSubmit() {
    if (!files.length) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await submitBatch(files, typeHint || undefined);
      setFiles([]);
      refreshRecent();
      navigate('batch', res.batch_id);
    } catch (err) {
      setError(errorLines(err).join('; '));
    } finally {
      setSubmitting(false);
    }
  }

  const stats = useMemo(() => {
    if (!batch) return null;
    const docs = batch.documents;
    const finished = docs.filter((d) => d.finished_at);
    const lastFinish = finished.reduce((m, d) => Math.max(m, new Date(d.finished_at).getTime()), 0);
    const end = live ? now : lastFinish || now;
    const elapsed = Math.max(0.001, (end - new Date(batch.created_at).getTime()) / 1000);
    const totals = { auto: 0, spot_check: 0, review: 0 };
    for (const d of docs) {
      const { counts } = docSummary(d);
      totals.auto += counts.auto;
      totals.spot_check += counts.spot_check;
      totals.review += counts.review;
    }
    const worker = docs.map((d) => seconds(d.started_at, d.finished_at)).filter((s) => s != null);
    return {
      elapsed,
      done: batch.counts.completed + batch.counts.failed,
      inFlight: batch.counts.processing + batch.counts.retrying,
      perMin: finished.length ? (60 * finished.length) / elapsed : 0,
      totals,
      medianWorker: worker.length ? worker.sort((a, b) => a - b)[Math.floor(worker.length / 2)] : null,
    };
  }, [batch, live, now]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="02 — Batch"
        title="Upload & monitor"
        meta="POST /documents/batch  →  GET /documents/batch/{id}/status  ·  one RQ job per document"
      />
      <div className="grid min-h-0 flex-1 grid-cols-[380px_minmax(0,1fr)]">
        {/* ---------------------------------------------------------------- submit */}
        <div className="scroll-thin flex min-h-0 flex-col gap-4 overflow-y-auto border-r border-border p-5">
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              addFiles(e.dataTransfer.files);
            }}
            onClick={() => inputRef.current?.click()}
            className={cn(
              'bed-grid group relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-lg border border-dashed px-6 py-10 text-center transition-colors',
              dragging ? 'border-foreground bg-accent' : 'border-border-strong hover:border-graphite-500',
            )}
          >
            <motion.div
              animate={dragging ? { y: -4, scale: 1.06 } : { y: 0, scale: 1 }}
              className="grid size-11 place-items-center rounded-md border border-border-strong bg-card"
            >
              <Upload className="size-5 text-icon" />
            </motion.div>
            <div>
              <div className="text-[14px] font-medium">Drop documents to process</div>
              <div className="mt-1 text-[12px] text-muted-foreground">PDF, JPEG or PNG · up to 50 per batch</div>
            </div>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept="application/pdf,image/jpeg,image/png"
              className="hidden"
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = '';
              }}
            />
          </div>

          {files.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Staged · {files.length}</CardTitle>
                <Button variant="ghost" size="sm" onClick={() => setFiles([])}>
                  Clear
                </Button>
              </CardHeader>
              <div className="scroll-thin max-h-56 overflow-y-auto py-1">
                <AnimatePresence initial={false}>
                  {files.map((f) => (
                    <motion.div
                      key={f.name + f.size}
                      layout
                      initial={{ opacity: 0, x: -8 }}
                      animate={{ opacity: 1, x: 0 }}
                      exit={{ opacity: 0, height: 0 }}
                      className="flex items-center gap-2.5 px-4 py-1.5"
                    >
                      {/\.pdf$/i.test(f.name) ? (
                        <FileText className="size-4 shrink-0 text-icon-muted" />
                      ) : (
                        <FileImage className="size-4 shrink-0 text-icon-muted" />
                      )}
                      <span className="min-w-0 flex-1 truncate font-mono text-[12px]">{f.name}</span>
                      <span className="font-mono text-[11px] text-faint-foreground">{formatBytes(f.size)}</span>
                      <button
                        className="text-faint-foreground hover:text-foreground"
                        onClick={() => setFiles((p) => p.filter((x) => x !== f))}
                        aria-label={`Remove ${f.name}`}
                      >
                        <X className="size-3.5" />
                      </button>
                    </motion.div>
                  ))}
                </AnimatePresence>
              </div>
            </Card>
          )}

          <div className="flex flex-col gap-1.5">
            <span className="caps text-muted-foreground">Document type</span>
            <NativeSelect value={typeHint} onChange={(e) => setTypeHint(e.target.value)}>
              <option value="">Auto-detect — Module 5 classifies each file</option>
              {templates.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </NativeSelect>
          </div>

          <Button variant="primary" size="lg" disabled={!files.length || submitting} onClick={handleSubmit}>
            <Play />
            {submitting ? 'Submitting…' : files.length ? `Submit ${files.length} document${files.length > 1 ? 's' : ''}` : 'Submit batch'}
          </Button>
          {error && (
            <div className="rounded-md border border-tier-review bg-tier-review-surface px-3 py-2 text-[12px] text-signal-300">
              {error}
            </div>
          )}

          <Card className="mt-2">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <History className="size-3.5" /> Recent batches
              </CardTitle>
            </CardHeader>
            <div className="py-1">
              {recent.length === 0 && <div className="px-4 py-3 text-[12px] text-muted-foreground">None yet.</div>}
              {recent.map((b) => (
                <a
                  key={b.batch_id}
                  href={`#/batch/${b.batch_id}`}
                  className={cn(
                    'flex items-center gap-3 px-4 py-2 transition-colors hover:bg-muted',
                    b.batch_id === batchId && 'bg-accent',
                  )}
                >
                  <span className="font-mono text-[12px] text-secondary-foreground">{b.batch_id.slice(0, 8)}</span>
                  <span className="flex-1 font-mono text-[11px] text-faint-foreground">
                    {b.document_count} docs · {timeAgo(b.created_at)}
                  </span>
                  <span
                    className={cn(
                      'caps-sm',
                      b.counts.failed ? 'text-tier-review-text' : b.status === 'completed' ? 'text-muted-foreground' : 'text-foreground',
                    )}
                  >
                    {b.status.replaceAll('_', ' ')}
                  </span>
                </a>
              ))}
            </div>
          </Card>
        </div>

        {/* ---------------------------------------------------------------- monitor */}
        <div className="scroll-thin min-h-0 overflow-y-auto">
          {!batch ? (
            <EmptyState icon={ScanLineIcon} title={batchId ? 'Loading batch…' : 'No batch yet'}>
              Drop documents on the left. Each becomes its own job; this panel follows every one of them live.
            </EmptyState>
          ) : (
            <div className="flex flex-col gap-5 p-6">
              <div className="flex items-center gap-3">
                <span className="font-mono text-[13px] text-secondary-foreground">batch {batch.batch_id}</span>
                <span
                  className={cn(
                    'caps rounded-sm border px-2 py-0.5',
                    live ? 'border-graphite-500 text-foreground' : batch.counts.failed ? 'border-tier-review text-tier-review-text' : 'border-border-strong text-muted-foreground',
                  )}
                >
                  {batch.status.replaceAll('_', ' ')}
                </span>
                {batch.type_hint && <span className="font-mono text-[12px] text-muted-foreground">hint {batch.type_hint}</span>}
                <span className="ml-auto font-mono text-[12px] text-faint-foreground">{timeAgo(batch.created_at)}</span>
              </div>

              <Card>
                <CardContent className="flex flex-col gap-5">
                  <div className="grid grid-cols-6 gap-4">
                    <Readout label="Documents" value={batch.documents.length} />
                    <Readout label="Done" value={<AnimatedNumber value={stats.done} />} sub={`of ${batch.documents.length}`} />
                    <Readout label="In flight" value={<AnimatedNumber value={stats.inFlight} />} />
                    <Readout
                      label="Failed"
                      value={<AnimatedNumber value={batch.counts.failed} />}
                      tone={batch.counts.failed ? 'review' : 'default'}
                    />
                    <Readout label="Throughput" value={<AnimatedNumber value={stats.perMin} decimals={1} />} sub="docs / min" />
                    <Readout
                      label="Elapsed"
                      value={`${stats.elapsed.toFixed(1)}s`}
                      sub={stats.medianWorker != null ? `median ${stats.medianWorker.toFixed(1)}s / doc` : 'waiting for a worker'}
                    />
                  </div>
                  {/* one segment per document, in submit order */}
                  <div>
                    <div className="flex h-3 gap-[3px]">
                      {batch.documents.map((d) => (
                        <motion.div
                          key={d.index}
                          layout
                          title={`${d.filename} · ${d.status}`}
                          className={cn('h-full flex-1 rounded-[1px] transition-colors duration-500', SEGMENT[d.status])}
                        />
                      ))}
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-muted-foreground">
                      {Object.entries(batch.counts).map(([k, n]) => (
                        <span key={k} className="flex items-center gap-1.5">
                          <span className={cn('inline-block size-2 rounded-[1px]', SEGMENT[k])} />
                          {k} {n}
                        </span>
                      ))}
                      <span className="ml-auto flex items-center gap-3">
                        <span className="caps-sm text-faint-foreground">Fields</span>
                        <span>auto {stats.totals.auto}</span>
                        <span className="text-tier-spot-foreground">spot {stats.totals.spot_check}</span>
                        <span className="text-tier-review-text">review {stats.totals.review}</span>
                      </span>
                    </div>
                  </div>
                </CardContent>
              </Card>

              <Card className="overflow-hidden">
                <div className="grid grid-cols-[44px_minmax(0,1.6fr)_130px_70px_64px_minmax(0,1.2fr)_110px] items-center gap-3 border-b border-border px-4 h-9 caps-sm text-faint-foreground">
                  <span>#</span>
                  <span>Document</span>
                  <span>Status</span>
                  <span className="text-right">Tries</span>
                  <span className="text-right">Time</span>
                  <span>Field tiers</span>
                  <span />
                </div>
                {batch.documents.map((d) => (
                  <DocRow key={d.index} doc={d} />
                ))}
              </Card>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ScanLineIcon(props) {
  return <Upload {...props} />;
}

function DocRow({ doc }) {
  const { counts, resultId, template, unconfirmed } = docSummary(doc);
  const time = seconds(doc.started_at, doc.finished_at);
  return (
    <motion.div
      layout
      className={cn(
        'relative grid grid-cols-[44px_minmax(0,1.6fr)_130px_70px_64px_minmax(0,1.2fr)_110px] items-center gap-3 border-b border-border px-4 py-2.5 last:border-b-0',
        doc.status === 'failed' && 'bg-tier-review-surface/60',
      )}
    >
      {/* a flash of light when the status changes */}
      <motion.span
        key={doc.status}
        initial={{ opacity: doc.status === 'queued' ? 0 : 0.5 }}
        animate={{ opacity: 0 }}
        transition={{ duration: 0.9 }}
        className="pointer-events-none absolute inset-0 bg-graphite-700"
      />
      <span className="font-mono text-[12px] text-faint-foreground">{String(doc.index + 1).padStart(2, '0')}</span>
      <div className="min-w-0">
        <div className="truncate font-mono text-[12.5px]">{doc.filename}</div>
        <div className="truncate font-mono text-[11px] text-faint-foreground">
          {doc.status === 'failed'
            ? doc.error
            : template
              ? `${template} · ${doc.pages_done}/${doc.page_count ?? '?'} pages`
              : unconfirmed
                ? 'no template matched · sent to review'
                : doc.page_count
                  ? `${doc.pages_done}/${doc.page_count} pages`
                  : '—'}
        </div>
      </div>
      <StatusPill status={doc.status} />
      <span className={cn('text-right font-mono text-[12px]', doc.attempts > 1 ? 'text-tier-spot-foreground' : 'text-muted-foreground')}>
        {doc.attempts}
      </span>
      <span className="text-right font-mono text-[12px] text-muted-foreground">{time != null ? `${time.toFixed(1)}s` : '—'}</span>
      <div className="flex min-w-0 items-center gap-2">
        {doc.status === 'completed' && counts.auto + counts.spot_check + counts.review > 0 ? (
          <>
            <TierBar counts={counts} className="flex-1" height="h-2" />
            <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
              {counts.auto}·<span className="text-tier-spot-foreground">{counts.spot_check}</span>·
              <span className="text-tier-review-text">{counts.review}</span>
            </span>
          </>
        ) : (
          <span className="h-2 flex-1 rounded-[1px] bg-graphite-850" />
        )}
      </div>
      <div className="text-right">
        {resultId && (
          <a
            href={`#/results/${resultId}`}
            className="inline-flex items-center gap-1 text-[12px] text-muted-foreground hover:text-foreground"
          >
            Result <ArrowUpRight className="size-3.5" />
          </a>
        )}
      </div>
    </motion.div>
  );
}
