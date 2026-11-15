import { ArrowUpRight, Check, Download, FileImage, FileText, Inbox, Play, Trash2, TriangleAlert, Upload, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { clearHistory, errorLines, getBatchStatus, listBatches, listReviewQueue, listTemplates, submitBatch } from '@/api';
import { EmptyState, PageHeader, Readout, TierBar } from '@/components/bits';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { StatusPill } from '@/components/StatusPill';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Label, NativeSelect } from '@/components/ui/input';
import { navigate } from '@/lib/nav';
import { tierCounts } from '@/lib/tiers';
import { cn, formatBytes, seconds, timeAgo } from '@/lib/utils';

const TERMINAL = ['completed', 'completed_with_errors', 'failed'];
const SEGMENT = {
  queued: 'bg-border-strong',
  processing: 'bg-primary',
  retrying: 'bg-warning-solid',
  completed: 'bg-success-solid',
  failed: 'bg-danger-solid',
};
const SEGMENT_LABEL = { queued: 'Queued', processing: 'Processing', retrying: 'Retrying', completed: 'Done', failed: 'Failed' };
const BATCH_TONE = { completed: 'success', completed_with_errors: 'warning', failed: 'danger' };

const UNMATCHED_LABEL = {
  no_match: 'No template matched',
  ambiguous_match: 'More than one template matched',
  unknown_type_hint: 'Chosen type not found',
};

const batchStatusLabel = (status) => {
  const text = status.replaceAll('_', ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
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

function docNote(doc, template, unconfirmed) {
  if (doc.status === 'failed') return doc.error;
  if (template) return `${template} · ${doc.pages_done}/${doc.page_count ?? '?'} pages`;
  if (unconfirmed) return 'No matching template · sent to Review';
  if (doc.page_count) return `${doc.pages_done}/${doc.page_count} pages`;
  return '';
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
  const [notice, setNotice] = useState(null);
  const [now, setNow] = useState(Date.now());
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [waiting, setWaiting] = useState(null); // review items this upload left
  const inputRef = useRef(null);
  const batchId = param;

  async function runClear() {
    setClearing(true);
    try {
      const out = await clearHistory();
      setConfirmClear(false);
      setBatch(null);
      setRecent([]);
      setError(null);
      setNotice(`Cleared ${out.batches} upload${out.batches === 1 ? '' : 's'}, ${out.documents} documents and ${out.results} results.`);
      if (batchId) navigate('batch');
      onQueueChange?.();
    } catch (err) {
      setConfirmClear(false);
      setError(errorLines(err).join('; '));
    } finally {
      setClearing(false);
    }
  }

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
  const finished = batch && TERMINAL.includes(batch.status);
  useEffect(() => {
    if (!finished) {
      setWaiting(null);
      return;
    }
    listReviewQueue('pending', batch.batch_id).then((e) => setWaiting(e.length)).catch(() => setWaiting(null));
  }, [finished, batch?.batch_id]);
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
    return {
      elapsed: Math.max(0.001, (end - new Date(batch.created_at).getTime()) / 1000),
      done: batch.counts.completed + batch.counts.failed,
      inFlight: batch.counts.processing + batch.counts.retrying,
    };
  }, [batch, live, now]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Upload" meta="Add your documents, choose what kind they are (or let the system detect it), then watch each one finish.">
        <Button variant="ghost" disabled={recent.length === 0 || live} title={live ? 'Wait for the running upload to finish' : undefined} onClick={() => setConfirmClear(true)}>
          <Trash2 /> Clear history
        </Button>
      </PageHeader>
      <ConfirmDialog
        open={confirmClear}
        title="Clear all upload history?"
        confirmLabel="Clear history"
        tone="danger"
        busy={clearing}
        onConfirm={runClear}
        onCancel={() => setConfirmClear(false)}
      >
        <p>This permanently removes every upload, its documents, extracted results and anything still waiting in Review. It cannot be undone.</p>
        <p>Values you already checked in Review are kept, with the results they belong to, because they are the evidence the system has of its own accuracy.</p>
      </ConfirmDialog>
      {notice && (
        <p role="status" className="border-b border-border bg-surface-sunken px-6 py-2 text-sm text-foreground-secondary">
          {notice}
        </p>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[320px_minmax(0,1fr)] xl:grid-cols-[384px_minmax(0,1fr)] lg:overflow-visible">
        <section aria-label="Upload documents" className="scroll-thin flex min-h-0 flex-col gap-4 border-border bg-surface p-6 lg:overflow-y-auto lg:border-r">
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
            className={cn(
              'flex flex-col items-center gap-3 rounded-lg border-2 border-dashed px-6 py-8 text-center transition-colors',
              dragging ? 'border-primary bg-primary-surface' : 'border-border-strong bg-background',
            )}
          >
            <div className="grid size-10 place-items-center rounded-md bg-surface-sunken">
              <Upload className="size-5 text-foreground-secondary" aria-hidden />
            </div>
            <div>
              <p className="text-sm font-medium text-foreground">Drag documents here</p>
              <p className="text-xs text-foreground-secondary">PDF, JPEG or PNG · up to 50 at a time</p>
            </div>
            <Button variant="outline" onClick={() => inputRef.current?.click()}>
              Choose files
            </Button>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept="application/pdf,image/jpeg,image/png"
              className="hidden"
              aria-label="Choose documents to upload"
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = '';
              }}
            />
          </div>

          {files.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Ready to upload · {files.length}</CardTitle>
                <Button variant="ghost" size="sm" onClick={() => setFiles([])}>
                  Clear all
                </Button>
              </CardHeader>
              <ul className="scroll-thin max-h-56 overflow-y-auto">
                {files.map((f) => (
                  <li key={f.name + f.size} className="flex items-center gap-3 border-b border-border px-4 py-2 last:border-b-0">
                    {/\.pdf$/i.test(f.name) ? (
                      <FileText className="size-4 shrink-0 text-foreground-subtle" aria-hidden />
                    ) : (
                      <FileImage className="size-4 shrink-0 text-foreground-subtle" aria-hidden />
                    )}
                    <span className="min-w-0 flex-1 truncate text-sm">{f.name}</span>
                    <span className="text-xs text-foreground-subtle">{formatBytes(f.size)}</span>
                    <Button variant="ghost" size="icon" onClick={() => setFiles((p) => p.filter((x) => x !== f))} aria-label={`Remove ${f.name}`}>
                      <X />
                    </Button>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Label>
            Document type
            <NativeSelect value={typeHint} onChange={(e) => setTypeHint(e.target.value)}>
              <option value="">Detect automatically (recommended)</option>
              {templates.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </NativeSelect>
          </Label>

          <Button variant="primary" size="lg" disabled={!files.length || submitting} onClick={handleSubmit}>
            <Play />
            {submitting ? 'Uploading…' : files.length ? `Process ${files.length} document${files.length > 1 ? 's' : ''}` : 'Process documents'}
          </Button>
          {error && (
            <p role="alert" className="rounded-md border border-danger-border bg-danger-surface px-3 py-2 text-sm text-danger">
              {error}
            </p>
          )}

          <Card>
            <CardHeader>
              <CardTitle>Recent uploads</CardTitle>
            </CardHeader>
            {recent.length === 0 ? (
              <p className="px-4 py-3 text-sm text-foreground-secondary">Nothing uploaded yet.</p>
            ) : (
              <ul>
                {recent.map((b) => (
                  <li key={b.batch_id} className="border-b border-border last:border-b-0">
                    <a
                      href={`#/batch/${b.batch_id}`}
                      aria-current={b.batch_id === batchId ? 'true' : undefined}
                      className={cn(
                        'flex items-center gap-3 border-l-4 px-4 py-2 text-sm transition-colors',
                        b.batch_id === batchId ? 'border-primary bg-primary-surface' : 'border-transparent hover:bg-surface-sunken',
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block text-foreground">
                          {b.document_count} document{b.document_count === 1 ? '' : 's'}
                        </span>
                        <span className="block text-xs text-foreground-secondary">{timeAgo(b.created_at)}</span>
                      </span>
                      <Badge tone={BATCH_TONE[b.status] ?? 'primary'} size="sm">
                        {batchStatusLabel(b.status)}
                      </Badge>
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </section>

        <section aria-label="Progress" className="scroll-thin min-h-0 lg:overflow-y-auto">
          {!batch ? (
            <EmptyState icon={Upload} title={batchId ? 'Loading…' : 'No uploads yet'}>
              Add documents on the left. Each one is read separately, and you can follow its progress here.
            </EmptyState>
          ) : (
            <div className="flex flex-col gap-4 p-6">
              <div className="flex flex-wrap items-center gap-3">
                <h2 className="text-lg text-foreground">Upload from {timeAgo(batch.created_at)}</h2>
                <Badge tone={live ? 'primary' : (BATCH_TONE[batch.status] ?? 'neutral')}>{batchStatusLabel(batch.status)}</Badge>
                {batch.type_hint && <span className="text-sm text-foreground-secondary">Type: {batch.type_hint}</span>}
                {finished && waiting != null && (
                  <div className="ml-auto flex flex-wrap items-center gap-2">
                    <Button asChild variant={waiting > 0 ? 'primary' : 'outline'} size="sm">
                      <a href={`#/review/${batch.batch_id}`}>
                        <Inbox /> {waiting > 0 ? `Review ${waiting} value${waiting === 1 ? '' : 's'}` : 'Nothing to review'}
                      </a>
                    </Button>
                    <Button asChild variant="outline" size="sm">
                      <a href={`/documents/batch/${batch.batch_id}/export`} download={`batch-${batch.batch_id.slice(0, 8)}.json`}>
                        <Download /> Download JSON
                      </a>
                    </Button>
                  </div>
                )}
              </div>

              <Flow batch={batch} stats={stats} live={live} finished={finished} waiting={waiting} />

              <Card className="flex flex-col gap-4 p-4">
                <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                  <Readout label="Documents" value={batch.documents.length} />
                  <Readout label="Finished" value={stats.done} sub={`of ${batch.documents.length}`} />
                  <Readout label="In progress" value={stats.inFlight} />
                  <Readout label="Time taken" value={`${stats.elapsed.toFixed(1)}s`} />
                </div>
                <div>
                  <div className="flex h-2 gap-1" role="img" aria-label={`${stats.done} of ${batch.documents.length} documents finished`}>
                    {batch.documents.map((d) => (
                      <div key={d.index} title={`${d.filename}: ${SEGMENT_LABEL[d.status] ?? d.status}`} className={cn('h-full flex-1 rounded-sm', SEGMENT[d.status])} />
                    ))}
                  </div>
                  <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-foreground-secondary">
                    {Object.entries(batch.counts).map(([k, n]) => (
                      <li key={k} className="flex items-center gap-2">
                        <span aria-hidden className={cn('inline-block size-2 rounded-sm', SEGMENT[k])} />
                        {SEGMENT_LABEL[k] ?? k}: {n}
                      </li>
                    ))}
                  </ul>
                </div>
                {batch.summary?.pages > 0 && (
                  <div className="border-t border-border pt-3">
                    <h3 className="label mb-2">Where the pages went ({batch.summary.pages})</h3>
                    <ul className="flex flex-wrap gap-2 text-xs">
                      {Object.entries(batch.summary.by_template).map(([name, n]) => (
                        <li key={name}>
                          <Badge tone="primary">
                            {name}: {n}
                          </Badge>
                        </li>
                      ))}
                      {Object.entries(batch.summary.unmatched).map(([reason, n]) => (
                        <li key={reason}>
                          <Badge tone={reason === 'ambiguous_match' ? 'danger' : 'warning'}>
                            {UNMATCHED_LABEL[reason] ?? reason}: {n}
                          </Badge>
                        </li>
                      ))}
                    </ul>
                    {Object.keys(batch.summary.engines_down ?? {}).length > 0 && (
                      <p role="alert" className="mt-2 rounded-md border border-warning-border bg-warning-surface px-3 py-2 text-xs text-foreground" data-testid="engines-down">
                        <strong>A reading engine did not answer</strong> ({Object.entries(batch.summary.engines_down).map(([name, n]) => `${name}: ${n} page${n === 1 ? '' : 's'}`).join(', ')}).
                        The remaining engines and the standard reader read those pages instead; fewer values are approved automatically and more wait for a person.
                      </p>
                    )}
                    {Object.keys(batch.summary.by_template).length > 1 && (
                      <p className="mt-2 text-xs text-foreground-secondary">
                        More than one template took pages in this upload. Each page is read as exactly one template; check the split matches what you expected.
                      </p>
                    )}
                  </div>
                )}
              </Card>

              <Card className="overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full min-w-xl table-fixed text-left text-sm">
                    <caption className="sr-only">Documents in this upload</caption>
                    <thead>
                      <tr className="border-b border-border bg-surface-sunken">
                        <th scope="col" className="label w-2/5 px-4 py-2">Document</th>
                        <th scope="col" className="label w-1/4 px-4 py-2">Status</th>
                        <th scope="col" className="label w-1/5 px-4 py-2">Fields</th>
                        <th scope="col" className="px-4 py-2"><span className="sr-only">Open</span></th>
                      </tr>
                    </thead>
                    <tbody>
                      {batch.documents.map((d) => (
                        <DocRow key={d.index} doc={d} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

/**
 * Where this upload stands, as four plain steps. The current step is the only one set in ink and weight; finished steps carry a
 * check. It says what is left, in the numbers the person will meet next (values waiting, what the JSON will and will not contain).
 */
function Flow({ batch, stats, live, finished, waiting }) {
  const total = batch.documents.length;
  const steps = [
    { name: 'Upload', note: `${total} document${total === 1 ? '' : 's'}`, done: true },
    { name: 'Read', note: live ? `${stats.done} of ${total} read` : 'All read', done: !live },
    {
      name: 'Review',
      note: !finished || waiting == null ? 'After reading' : waiting > 0 ? `${waiting} waiting` : 'Nothing waiting',
      done: finished && waiting === 0,
      current: finished && waiting > 0,
    },
    { name: 'Export', note: finished ? 'Values nobody has settled are left blank' : 'After review', done: false },
  ];
  const current = steps.findIndex((s) => s.current || !s.done);
  return (
    <ol aria-label="Progress" className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {steps.map((s, i) => (
        <li key={s.name} aria-current={i === current ? 'step' : undefined} className="flex items-start gap-3">
          <span
            aria-hidden
            className={cn(
              'mt-0.5 grid size-5 shrink-0 place-items-center rounded-full border text-xs',
              s.done ? 'border-foreground bg-foreground text-surface' : i === current ? 'border-primary bg-primary-surface text-primary-text' : 'border-border-strong text-foreground-subtle',
            )}
          >
            {s.done ? <Check className="size-3" /> : i + 1}
          </span>
          <span className="min-w-0">
            <span className={cn('block text-sm', i === current ? 'font-semibold text-foreground' : 'font-medium text-foreground-secondary')}>{s.name}</span>
            <span className="block text-xs text-foreground-secondary">{s.note}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function DocRow({ doc }) {
  const { counts, resultId, template, unconfirmed } = docSummary(doc);
  const time = seconds(doc.started_at, doc.finished_at);
  const hasFields = doc.status === 'completed' && counts.auto + counts.spot_check + counts.review > 0;
  const note = docNote(doc, template, unconfirmed);
  return (
    <tr className={cn('border-b border-border last:border-b-0', doc.status === 'failed' && 'bg-danger-surface')}>
      <th scope="row" className="px-4 py-3 text-left font-normal">
        <span className="block truncate font-medium text-foreground">{doc.filename}</span>
        {note && <span className={cn('block truncate text-xs', doc.status === 'failed' ? 'text-danger' : 'text-foreground-secondary')}>{note}</span>}
        {doc.attempts > 1 && <span className="block text-xs text-warning">Tried {doc.attempts} times</span>}
      </th>
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          {doc.status === 'completed' && unconfirmed > 0 && !resultId ? (
            <Badge tone="warning">
              <TriangleAlert className="size-3.5" aria-hidden /> Not recognized
            </Badge>
          ) : (
            <StatusPill status={doc.status} />
          )}
          {time != null && <span className="text-xs text-foreground-subtle">{time.toFixed(1)}s</span>}
        </div>
      </td>
      <td className="px-4 py-3">
        {hasFields ? (
          <div className="flex flex-col gap-1">
            <TierBar counts={counts} />
            <span className="flex flex-wrap gap-x-2 text-xs text-foreground-secondary">
              <span className="whitespace-nowrap">{counts.auto} auto</span>
              <span className="whitespace-nowrap">{counts.spot_check} spot check</span>
              <span className="whitespace-nowrap">{counts.review} review</span>
            </span>
          </div>
        ) : (
          <span className="text-foreground-subtle">—</span>
        )}
      </td>
      <td className="px-4 py-3 text-right">
        {resultId && (
          <Button asChild variant="ghost" size="sm">
            <a href={`#/results/${resultId}`}>
              View <ArrowUpRight />
            </a>
          </Button>
        )}
      </td>
    </tr>
  );
}
