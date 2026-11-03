import { CheckCheck, Layers, Pencil, ShieldCheck } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { bulkAccept, bulkCorrect, errorLines, getReviewEvidence, getReviewGroups, getTemplateVersion, resolveFieldReview, resultImageUrl } from '@/api';
import { EmptyState } from '@/components/bits';
import { FieldCrop } from '@/components/FieldCrop';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { reasonText } from '@/lib/tiers';
import { cn, fmtConf } from '@/lib/utils';

const SHEET = 12; // crops shown per group before "show all"
const SHEET_MAX = 60;

const TABS = [
  ['value', 'Same value'],
  ['document', 'By document'],
  ['evidence', 'Evidence'],
];

/** One template version per (id, version), fetched once, shared by every card. */
function useTemplates() {
  const [templates, setTemplates] = useState({});
  const asked = useRef(new Set());
  const need = useCallback((groups) => {
    for (const g of groups) {
      const key = `${g.template_id}@${g.template_version}`;
      if (asked.current.has(key)) continue;
      asked.current.add(key);
      getTemplateVersion(g.template_id, g.template_version)
        .then((t) => setTemplates((m) => ({ ...m, [key]: t })))
        .catch(() => setTemplates((m) => ({ ...m, [key]: { error: true } })));
    }
  }, []);
  return [templates, need];
}

const fieldOf = (templates, g, fieldId) => templates[`${g.template_id}@${g.template_version}`]?.fields?.find((f) => f.id === fieldId);

export default function GroupedReview({ batchId = null, onQueueChange }) {
  const [tab, setTab] = useState('value');
  const [data, setData] = useState({ state: 'loading', by: 'value', groups: [], total: 0 });
  const [evidence, setEvidence] = useState(null);
  const [templates, needTemplates] = useTemplates();
  const [note, setNote] = useState(null);
  const groups = data.by === tab ? data.groups : []; // the first render after a tab change still holds the previous tab's groups

  const load = useCallback(async () => {
    try {
      if (tab === 'evidence') {
        setEvidence(await getReviewEvidence());
        return;
      }
      // The value view also lists what Module 7 rejected: a repeated wrong read is what one correction should fix.
      const out = await getReviewGroups(tab, { batchId, includeRejected: tab === 'value' });
      setData({ state: 'ready', by: tab, groups: out.groups, total: out.total });
      needTemplates(out.groups);
    } catch {
      setData({ state: 'error', by: tab, groups: [], total: 0 });
    }
  }, [tab, batchId, needTemplates]);

  useEffect(() => {
    setNote(null);
    setData({ state: 'loading', by: tab, groups: [], total: 0 }); // never show one tab's groups under another
    load();
  }, [load, tab]);

  const changed = useCallback(
    async (message) => {
      if (message) setNote(message);
      await load();
      onQueueChange?.();
    },
    [load, onQueueChange],
  );

  return (
    <div className="scroll-thin flex h-full min-h-0 flex-col overflow-y-auto">
      <div role="tablist" aria-label="Group by" className="flex gap-1 border-b border-border bg-surface px-6 py-2">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={cn(
              'h-8 rounded-sm px-3 text-sm font-medium transition-colors',
              tab === key ? 'bg-primary-surface text-primary-text' : 'text-foreground-secondary hover:bg-surface-sunken hover:text-foreground',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {note && (
        <p role="status" className="mx-6 mt-4 rounded-md bg-surface-sunken px-3 py-2 text-sm text-foreground-secondary">
          {note}
        </p>
      )}

      {tab === 'evidence' ? (
        <EvidencePanel evidence={evidence} />
      ) : (
        <div className="flex flex-col gap-4 p-6">
          {data.state === 'loading' && <p className="text-sm text-foreground-secondary">Loading…</p>}
          {data.state === 'error' && (
            <p role="alert" className="text-sm text-danger">
              Couldn't reach the server.
            </p>
          )}
          {data.state === 'ready' && data.by === tab && groups.length === 0 && (
            <EmptyState icon={CheckCheck} title={tab === 'value' ? 'No repeated values waiting' : 'No documents waiting'}>
              {tab === 'value'
                ? 'Groups appear when the same value was read in the same field on two or more documents and only needs a quick look.'
                : 'Nothing is waiting for a person.'}
            </EmptyState>
          )}
          {data.state === 'ready' && data.total > data.groups.length && (
            <p className="text-xs text-foreground-secondary">
              Showing the first {data.groups.length} of {data.total}.
            </p>
          )}
          {tab === 'value' && groups.map((g) => <ValueGroup key={`${g.template_id}@${g.template_version}|${g.field_id}|${g.value}`} group={g} templates={templates} onChanged={changed} />)}
          {tab === 'document' && groups.map((g) => <DocumentGroup key={g.result_id} group={g} templates={templates} onChanged={changed} />)}
        </div>
      )}
    </div>
  );
}

function ValueGroup({ group: g, templates, onChanged }) {
  const [showAll, setShowAll] = useState(false);
  const [fix, setFix] = useState(g.value);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const field = fieldOf(templates, g, g.field_id);
  const shown = g.entry_ids.slice(0, showAll ? SHEET_MAX : SHEET).map((id, i) => ({ id, resultId: g.result_ids[i] }));
  const audit = Math.max(1, Math.ceil(0.1 * g.count));

  async function acceptAll() {
    setBusy(true);
    setError(null);
    try {
      const out = await bulkAccept(g.entry_ids);
      await onChanged(
        `Accepted ${out.accepted.length} × ${g.value} for ${field?.label ?? g.field_id}. ${out.held_for_audit.length} chosen at random stay in the queue as an audit sample: check those one by one.`,
      );
    } catch (err) {
      setError(errorLines(err).join('; '));
      setBusy(false);
    }
  }

  async function correctAll() {
    setBusy(true);
    setError(null);
    try {
      const out = await bulkCorrect(g.entry_ids, fix.trim());
      await onChanged(
        `Set ${field?.label ?? g.field_id} to “${out.value}” on ${out.corrected.length} documents. ${out.held_for_audit.length} chosen at random stay in the queue unchanged: check those one by one.`,
      );
    } catch (err) {
      setError(errorLines(err).join('; '));
      setBusy(false);
    }
  }

  return (
    <Card className="overflow-hidden">
      <CardHeader>
        <CardTitle>
          {field?.label ?? g.field_id}
          <span className="ml-2 font-normal text-foreground-secondary">{g.template_id}</span>
        </CardTitle>
        <Badge tone={g.accept_ok ? 'warning' : 'danger'} size="sm">
          {g.count} documents · {g.accept_ok ? `lowest confidence ${fmtConf(g.min_confidence)}` : 'includes values that failed a check'}
        </Badge>
      </CardHeader>
      <div className="flex flex-col gap-4 p-4">
        <p className="break-words rounded-md border border-border-strong bg-surface-sunken px-3 py-3 font-mono text-lg text-foreground">{g.value}</p>
        <p className="text-xs text-foreground-secondary">
          The same reading appeared in all {g.count} documents. Look at the crops: if any does not show this value, open that document under By document instead of accepting the group.
        </p>
        <ul className="grid grid-cols-2 gap-3 lg:grid-cols-3 2xl:grid-cols-4">
          {shown.map((s) => (
            <li key={s.id} className="overflow-hidden rounded-sm border border-border bg-white">
              {field ? <FieldCrop src={resultImageUrl(s.resultId)} bbox={field.bbox} tier="spot_check" /> : <div className="h-16 animate-pulse bg-surface-sunken" />}
            </li>
          ))}
        </ul>
        {g.count > SHEET && (
          <Button variant="ghost" size="sm" className="self-start" onClick={() => setShowAll((v) => !v)}>
            {showAll ? 'Show fewer' : `Show ${Math.min(g.count, SHEET_MAX)} of ${g.count}`}
          </Button>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="primary" disabled={busy || !field || !g.accept_ok} onClick={acceptAll}>
            <Layers /> Accept all {g.count}
          </Button>
          <span className="text-xs text-foreground-secondary">
            {g.accept_ok
              ? `${audit} chosen at random stay in the queue as an audit sample, so what is accepted here is still measured.`
              : 'Some of these failed a check, so they cannot be accepted as they are. Correct them together below.'}
          </span>
        </div>
        <form
          className="flex flex-col gap-2 border-t border-border pt-4"
          onSubmit={(e) => {
            e.preventDefault();
            correctAll();
          }}
        >
          <label className="label flex flex-col gap-1" htmlFor={`fix-${g.entry_ids[0]}`}>
            Not what the crops show? Set the right value for all {g.count}
          </label>
          <div className="flex gap-2">
            <Input id={`fix-${g.entry_ids[0]}`} mono value={fix} onChange={(e) => setFix(e.target.value)} />
            <Button type="submit" variant="outline" disabled={busy || !field || !fix.trim() || fix.trim() === g.value}>
              <Pencil /> Correct all {g.count}
            </Button>
          </div>
          <p className="text-xs text-foreground-secondary">
            The value must match this field’s format rule. A random {audit} stay unchanged for a proper look, and corrections made together do not count as checks of the system.
          </p>
        </form>
        {error && (
          <p role="alert" className="text-xs text-danger">
            {error}
          </p>
        )}
      </div>
    </Card>
  );
}

function DocumentGroup({ group: g, templates, onChanged }) {
  const [values, setValues] = useState(() => Object.fromEntries(g.entries.map((e) => [e.id, e.original_value])));
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState(null);
  const bulkIds = g.entries.filter((e) => e.bulk_ok).map((e) => e.id);

  async function resolveOne(entry) {
    setBusyId(entry.id);
    setError(null);
    try {
      await resolveFieldReview(entry.id, values[entry.id]);
      await onChanged();
    } catch (err) {
      setError(errorLines(err).join('; '));
      setBusyId(null);
    }
  }

  async function acceptSpotChecks() {
    setBusyId('bulk');
    setError(null);
    try {
      const out = await bulkAccept(bulkIds);
      await onChanged(
        `Accepted ${out.accepted.length} spot check${out.accepted.length === 1 ? '' : 's'} in document #${g.result_id}.${out.held_for_audit.length ? ` ${out.held_for_audit.length} kept back for a proper look.` : ''}`,
      );
    } catch (err) {
      setError(errorLines(err).join('; '));
      setBusyId(null);
    }
  }

  return (
    <Card className="overflow-hidden">
      <CardHeader>
        <CardTitle>
          Document #{g.result_id}
          <span className="ml-2 font-normal text-foreground-secondary">{g.template_id}</span>
        </CardTitle>
        <div className="flex items-center gap-2">
          {g.needs_person > 0 && (
            <Badge tone="danger" size="sm">
              {g.needs_person} need{g.needs_person === 1 ? 's' : ''} a person
            </Badge>
          )}
          <Badge size="sm">{g.count} waiting</Badge>
        </div>
      </CardHeader>
      <ul>
        {g.entries.map((e) => {
          const field = fieldOf(templates, g, e.field_id);
          const rejected = e.entry_type === 'field_review';
          return (
            <li key={e.id} className="grid grid-cols-1 gap-3 border-t border-border p-4 first:border-t-0 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <div className="overflow-hidden rounded-sm border border-border bg-white">
                {field ? <FieldCrop src={resultImageUrl(g.result_id)} bbox={field.bbox} tier={rejected ? 'review' : 'spot_check'} /> : <div className="h-16 animate-pulse bg-surface-sunken" />}
              </div>
              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-foreground">{field?.label ?? e.field_id}</span>
                  <span className={cn('text-xs tabular-nums', rejected ? 'text-danger' : 'text-warning')}>{fmtConf(e.confidence)}</span>
                  {e.audit && (
                    <Badge size="sm" title="Held back from a bulk accept so a person checks it">
                      Audit sample
                    </Badge>
                  )}
                  {rejected ? (
                    e.reasons.map((r) => (
                      <Badge key={r} tone="danger" size="sm">
                        {reasonText(r)}
                      </Badge>
                    ))
                  ) : (
                    <Badge tone="warning" size="sm">
                      {e.why === 'no_validator' ? 'No format rule' : e.why === 'blank_optional' ? 'Not found' : 'Low confidence'}
                    </Badge>
                  )}
                </div>
                <div className="flex gap-2">
                  <Input mono aria-label={`Value of ${field?.label ?? e.field_id}`} value={values[e.id]} onChange={(ev) => setValues((v) => ({ ...v, [e.id]: ev.target.value }))} />
                  <Button variant={values[e.id] === e.original_value ? 'outline' : 'primary'} disabled={busyId !== null} onClick={() => resolveOne(e)}>
                    {values[e.id] === e.original_value ? 'Accept' : 'Save'}
                  </Button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      <div className="flex flex-wrap items-center gap-3 border-t border-border bg-surface-sunken p-4">
        <Button variant="primary" disabled={busyId !== null || bulkIds.length === 0} onClick={acceptSpotChecks}>
          <Layers /> Accept {bulkIds.length} spot check{bulkIds.length === 1 ? '' : 's'}
        </Button>
        <span className="text-xs text-foreground-secondary">
          {g.needs_person > 0
            ? 'Values that failed a check, were not found, or are audit samples stay for you to confirm or correct one by one.'
            : bulkIds.length > 1
              ? 'A random one is kept back for a proper look.'
              : ''}
        </span>
      </div>
      {error && (
        <p role="alert" className="px-4 pb-4 text-xs text-danger">
          {error}
        </p>
      )}
    </Card>
  );
}

const pct = (x) => (x >= 0.01 ? `${(100 * x).toFixed(1)}%` : `${(100 * x).toFixed(3)}%`);

function EvidencePanel({ evidence }) {
  if (!evidence) return <p className="p-6 text-sm text-foreground-secondary">Loading…</p>;
  const target = `1 in ${Math.round(1 / evidence.target).toLocaleString()}`;
  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex max-w-prose items-start gap-3 text-sm text-foreground-secondary">
        <ShieldCheck className="mt-0.5 size-4 shrink-0" aria-hidden />
        <p>
          How often people had to change what the system read, field by field, counting only values checked one at a time (a bulk accept is not a check). The
          bound is the highest error rate still consistent with what was seen, at {Math.round(evidence.confidence * 100)}% confidence. To show a field is at
          {' '}{target} or better, <strong>{evidence.clean_checks_needed.toLocaleString()}</strong> values must have been checked with none wrong: fewer checks cannot show it, however many were right.
        </p>
      </div>
      <p className="max-w-prose text-sm text-foreground-secondary">
        {evidence.audited_auto_approval ? (
          <>
            <strong className="text-foreground">Audited auto-approval is on.</strong> A field marked Trusted below has its spot checks approved as auto without a person, except a random{' '}
            {Math.round(evidence.audit_fraction * 100)}% that still come here as audit samples. One wrong value found raises the bound and takes the trust away until enough clean
            checks pile up again.
          </>
        ) : (
          'Audited auto-approval is off: every spot check still goes to a person.'
        )}
      </p>
      {evidence.fields.length === 0 ? (
        <EmptyState title="Nothing checked yet">Evidence builds up as people accept or correct values one at a time.</EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full table-fixed text-sm">
            <thead className="bg-surface-sunken text-left text-xs text-foreground-secondary">
              <tr>
                <th scope="col" className="w-[26%] px-3 py-2 font-medium">Template</th>
                <th scope="col" className="w-[22%] px-3 py-2 font-medium">Field</th>
                <th scope="col" className="w-[10%] px-3 py-2 text-right font-medium">Checked</th>
                <th scope="col" className="w-[10%] px-3 py-2 text-right font-medium">Changed</th>
                <th scope="col" className="w-[14%] px-3 py-2 text-right font-medium">Error rate is at most</th>
                <th scope="col" className="w-[18%] px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {evidence.fields.map((f) => (
                <tr key={`${f.template_id}@${f.version}|${f.field_id}`} className="border-t border-border">
                  <td className="truncate px-3 py-2 font-mono text-xs">
                    {f.template_id} v{f.version}
                  </td>
                  <td className="truncate px-3 py-2 font-mono text-xs">{f.field_id}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{f.checked}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{f.changed}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{pct(f.upper_bound)}</td>
                  <td className="px-3 py-2">
                    {f.meets_target ? (
                      <Badge tone="success" size="sm">
                        Trusted · {target}
                      </Badge>
                    ) : f.more_clean_checks_needed != null ? (
                      <Badge size="sm">{f.more_clean_checks_needed.toLocaleString()} more clean checks</Badge>
                    ) : (
                      <Badge tone="warning" size="sm">
                        Has errors
                      </Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
