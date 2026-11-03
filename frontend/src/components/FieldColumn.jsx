import { Check, EyeOff, Layers } from 'lucide-react';
import { useMemo, useState } from 'react';
import { bulkAccept, clearReviewQueue, errorLines, resolveFieldReview, resultImageUrl } from '@/api';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { FieldCrop } from '@/components/FieldCrop';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { reasonText } from '@/lib/tiers';
import { cn, fmtConf } from '@/lib/utils';

const WHY = { no_validator: 'No format rule', blank_optional: 'Not found', below_auto_threshold: 'Not confirmed' };

/**
 * One field across every document of the upload: the same crop, one value per document, in a column. Scanning a column is
 * the fastest way for a person to check many values (one kind of text, one place on the page): fix the ones that are wrong
 * in place, then accept the rest in one click. Values Module 7 rejected cannot be accepted as read; each needs its own
 * Accept (the value is right after all) or Save (it was corrected).
 */
export function FieldColumn({ group: g, field, batchId, onChanged }) {
  const [edits, setEdits] = useState({});
  const [confirmBlank, setConfirmBlank] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const valueOf = (e) => edits[e.id] ?? e.value;
  const changed = (e) => valueOf(e) !== e.value;

  const unchangedOk = useMemo(() => g.entries.filter((e) => e.bulk_ok && !(edits[e.id] != null && edits[e.id] !== e.value)), [g.entries, edits]);
  const edited = g.entries.filter(changed);

  async function run(fn, note) {
    setBusy(true);
    setError(null);
    try {
      const message = await fn();
      await onChanged(message ?? note);
      setEdits({});
    } catch (err) {
      setError(errorLines(err).join('; '));
    } finally {
      setBusy(false);
    }
  }

  const acceptRest = () =>
    run(async () => {
      const out = await bulkAccept(unchangedOk.map((e) => e.id));
      return `Accepted ${out.accepted.length} ${field?.label ?? g.field_id} value${out.accepted.length === 1 ? '' : 's'}. ${out.held_for_audit.length} chosen at random stay for a proper look.`;
    });
  const saveEdited = () =>
    run(async () => {
      for (const e of edited) await resolveFieldReview(e.id, valueOf(e));
      return `Saved ${edited.length} correction${edited.length === 1 ? '' : 's'} to ${field?.label ?? g.field_id}.`;
    });
  const leaveBlank = () =>
    run(async () => {
      const out = await clearReviewQueue(batchId, g.field_id);
      setConfirmBlank(false);
      return `${field?.label ?? g.field_id} will be left blank in the JSON for ${out.deleted} document${out.deleted === 1 ? '' : 's'}.`;
    });
  const saveOne = (e) => run(() => resolveFieldReview(e.id, valueOf(e)).then(() => null), null);

  return (
    <Card className="overflow-hidden">
      <CardHeader>
        <CardTitle>
          {field?.label ?? g.field_id}
          <span className="ml-2 font-normal text-foreground-secondary">{g.template_id}</span>
        </CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          {g.needs_person > 0 && (
            <Badge tone="danger" size="sm">
              {g.needs_person} failed a check
            </Badge>
          )}
          <Badge size="sm">{g.count} waiting</Badge>
        </div>
      </CardHeader>

      <ul>
        {g.entries.map((e) => {
          const rejected = e.entry_type === 'field_review';
          return (
            <li key={e.id} className="grid grid-cols-1 gap-3 border-t border-border p-4 first:border-t-0 md:grid-cols-[240px_minmax(0,1fr)] lg:grid-cols-[280px_minmax(0,1fr)]">
              <div className="overflow-hidden rounded-sm border border-border bg-white">
                {field ? <FieldCrop src={resultImageUrl(e.result_id)} bbox={field.bbox} tier={rejected ? 'review' : 'spot_check'} /> : <div className="h-16 bg-surface-sunken" />}
              </div>
              <div className="flex min-w-0 flex-col justify-center gap-2">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="text-foreground-secondary">Document #{e.result_id}</span>
                  <span className={cn('tabular-nums', rejected ? 'text-danger' : 'text-warning')}>{fmtConf(e.confidence)}</span>
                  {e.audit && <Badge size="sm" title="Held back from an unseen accept so a person checks it">Audit sample</Badge>}
                  {rejected ? (
                    e.reasons.map((r) => (
                      <Badge key={r} tone="danger" size="sm">
                        {reasonText(r)}
                      </Badge>
                    ))
                  ) : (
                    <Badge tone="warning" size="sm">
                      {WHY[e.why] ?? e.why}
                    </Badge>
                  )}
                </div>
                <div className="flex gap-2">
                  <Input
                    mono
                    aria-label={`${field?.label ?? g.field_id}, document ${e.result_id}`}
                    value={valueOf(e)}
                    placeholder={e.value ? undefined : 'Nothing was read'}
                    onChange={(ev) => setEdits((m) => ({ ...m, [e.id]: ev.target.value }))}
                  />
                  {(changed(e) || !e.bulk_ok) && (
                    <Button variant={changed(e) ? 'primary' : 'outline'} disabled={busy || (changed(e) && !valueOf(e).trim())} onClick={() => saveOne(e)}>
                      {changed(e) ? 'Save' : 'Accept'}
                    </Button>
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-3 border-t border-border bg-surface-sunken p-4">
        <Button variant="primary" disabled={busy || unchangedOk.length === 0} onClick={acceptRest}>
          <Layers /> Accept {unchangedOk.length} unchanged
        </Button>
        {edited.length > 0 && (
          <Button variant="outline" disabled={busy || edited.some((e) => !valueOf(e).trim())} onClick={saveEdited}>
            <Check /> Save {edited.length} edited
          </Button>
        )}
        <Button variant="ghost" disabled={busy} onClick={() => setConfirmBlank(true)}>
          <EyeOff /> Leave blank
        </Button>
        <span className="text-xs text-foreground-secondary">
          Fix any value that does not match its crop, then accept the rest. A random 10% of what you accept stays for a proper look
          {g.needs_person > 0 ? '; values that failed a check are accepted or corrected one by one' : ''}.
        </span>
      </div>
      {error && (
        <p role="alert" className="px-4 pb-4 text-xs text-danger">
          {error}
        </p>
      )}
      <ConfirmDialog
        open={confirmBlank}
        title={`Leave ${field?.label ?? g.field_id} blank?`}
        confirmLabel={`Leave ${g.count} blank`}
        tone="danger"
        busy={busy}
        onConfirm={leaveBlank}
        onCancel={() => setConfirmBlank(false)}
      >
        <p>
          Nobody will check these {g.count} values. They are removed from the queue, and the downloaded JSON leaves this field empty
          for those documents (the machine’s reading stays beside it as <code>suggested</code>). Use this for a field you do not need.
        </p>
        <p>It cannot be undone, but the reading itself is not deleted: it is still on the Results screen.</p>
      </ConfirmDialog>
    </Card>
  );
}
