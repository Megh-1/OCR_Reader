import { FlaskConical, Lightbulb, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { errorLines, suggestChecks, testCheck } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input, Label, NativeSelect } from '@/components/ui/input';

const KINDS = [
  ['product', 'A x B = total'],
  ['sum', 'A + B (+ C) = total'],
  ['equal', 'two fields agree'],
  ['one_of', 'a field is one of a list'],
];

const slug = (text) =>
  text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 48) || 'rule';

function describe(c) {
  const names = c.inputs.join(c.kind === 'sum' ? ' + ' : c.kind === 'product' ? ' x ' : ', ');
  if (c.kind === 'product' || c.kind === 'sum') return `${names} = ${c.target}`;
  if (c.kind === 'equal') return `${c.inputs[0]} agrees with ${c.inputs[1]}`;
  if (c.kind === 'one_of') return `${c.inputs[0]} is ${c.lookup ? 'a currency code' : `one of ${(c.values ?? []).join(', ')}`}`;
  return `${c.inputs[0]} maps to ${c.target}`;
}

function TestResult({ result }) {
  if (!result) return null;
  if (result.loading) return <p className="text-xs text-foreground-secondary">Testing…</p>;
  if (result.error) return <p className="text-xs text-danger">{result.error}</p>;
  return (
    <p className="flex flex-wrap items-center gap-2 text-xs text-foreground-secondary">
      <Badge tone="success" size="sm">
        {result.passed} passed
      </Badge>
      <Badge tone={result.failed ? 'danger' : 'neutral'} size="sm">
        {result.failed} failed
      </Badge>
      <Badge size="sm">{result.skipped} skipped</Badge>
      <span>of {result.documents} processed documents</span>
      {result.failures?.map((f) => (
        <span key={f.result_id} className="basis-full font-mono">
          #{f.result_id}: {f.detail}
        </span>
      ))}
    </p>
  );
}

/**
 * Rules between fields. A rule only ever ADDS caution: when it fails, every field it names goes to Review. They are
 * written here by a person, or proposed from what the form's fields hold across already-processed documents (a
 * proposal is only that - nothing is added until you press Add).
 */
export default function CheckEditor({ templateId, fieldIds, checks, setChecks }) {
  const [tests, setTests] = useState({});
  const [suggestions, setSuggestions] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [draft, setDraft] = useState({ kind: 'product', a: '', b: '', c: '', target: '', label: '', values: '', iso: false });

  const runTest = async (check, key) => {
    setTests((t) => ({ ...t, [key]: { loading: true } }));
    try {
      const result = await testCheck(templateId, check);
      setTests((t) => ({ ...t, [key]: result }));
    } catch (err) {
      setTests((t) => ({ ...t, [key]: { error: errorLines(err).join(' ') } }));
    }
  };

  const suggest = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const out = await suggestChecks(templateId);
      const have = new Set(checks.map((c) => c.id));
      setSuggestions(out.suggestions.filter((s) => !have.has(s.id)));
      if (out.documents < 2) setMessage(`Only ${out.documents} processed document${out.documents === 1 ? '' : 's'} of this template: process at least two to get suggestions.`);
      else if (out.suggestions.length === 0) setMessage(`No rule held on every one of ${out.documents} documents.`);
    } catch (err) {
      setMessage(errorLines(err).join(' '));
    } finally {
      setBusy(false);
    }
  };

  const add = (check) => {
    let id = check.id;
    for (let n = 2; checks.some((c) => c.id === id); n++) id = `${check.id}_${n}`.slice(0, 60);
    const { documents: _documents, ...clean } = check;
    setChecks((prev) => [...prev, { ...clean, id }]);
    setSuggestions((prev) => (prev ? prev.filter((s) => s.id !== check.id) : null));
  };

  const set = (patch) => setDraft((d) => ({ ...d, ...patch }));

  const draftCheck = (() => {
    const { kind, a, b, c, target, label, values, iso } = draft;
    const inputs = kind === 'one_of' ? [a] : kind === 'sum' ? [a, b, c].filter(Boolean) : [a, b];
    if (inputs.some((v) => !v) || new Set(inputs).size !== inputs.length) return null;
    if (kind === 'sum' && inputs.length < 2) return null;
    if ((kind === 'product' || kind === 'sum') && (!target || inputs.includes(target))) return null;
    const check = {
      id: slug(label || `${kind}_${inputs.join('_')}`),
      label: label.trim() || `${kind} ${inputs.join(', ')}`,
      kind,
      inputs,
      tolerance: 0.01,
      patterns: {},
      values: [],
      table: {},
    };
    if (kind === 'product' || kind === 'sum') check.target = target;
    if (kind === 'one_of') {
      if (iso) check.lookup = 'iso4217';
      else {
        check.values = values
          .split(',')
          .map((v) => v.trim())
          .filter(Boolean);
        if (check.values.length === 0) return null;
      }
    }
    return check;
  })();

  const fieldSelect = (key, label) => (
    <Label key={key}>
      {label}
      <NativeSelect value={draft[key]} onChange={(e) => set({ [key]: e.target.value })}>
        <option value="">Choose a field…</option>
        {fieldIds.map((f) => (
          <option key={f} value={f}>
            {f}
          </option>
        ))}
      </NativeSelect>
    </Label>
  );

  return (
    <div className="flex flex-col gap-4 p-4">
      <p className="text-xs text-foreground-secondary">
        A rule compares fields with each other. A value that fails one is sent to Review; a rule never changes a value. Rules are kept when you save.
      </p>

      <section aria-label="Rules in this template" className="flex flex-col gap-3">
        <h3 className="label">In this template ({checks.length})</h3>
        {checks.length === 0 && <p className="text-sm text-foreground-secondary">None yet.</p>}
        {checks.map((c) => (
          <div key={c.id} className="flex flex-col gap-1 rounded-md border border-border p-3">
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-foreground">{c.label}</p>
                <p className="font-mono text-xs text-foreground-secondary">
                  {describe(c)}
                  {c.when ? ` · only when ${c.when.field} is ${c.when.equals}` : ''}
                </p>
              </div>
              <Button variant="outline" size="sm" disabled={!templateId} onClick={() => runTest(c, c.id)} title="Run on this template's processed documents">
                <FlaskConical /> Test
              </Button>
              <Button variant="outline" size="sm" aria-label={`Remove ${c.label}`} onClick={() => setChecks((prev) => prev.filter((x) => x.id !== c.id))}>
                <Trash2 />
              </Button>
            </div>
            <TestResult result={tests[c.id]} />
          </div>
        ))}
      </section>

      <section aria-label="Suggested rules" className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <h3 className="label flex-1">Suggested</h3>
          <Button variant="outline" size="sm" disabled={!templateId || busy} onClick={suggest} title="Looks for relationships that held on every processed document of this template">
            <Lightbulb /> {busy ? 'Looking…' : 'Suggest rules'}
          </Button>
        </div>
        {!templateId && <p className="text-xs text-foreground-secondary">Save the template and process a few documents with it first: suggestions come from what its fields hold.</p>}
        {message && (
          <p role="status" className="text-xs text-foreground-secondary">
            {message}
          </p>
        )}
        {suggestions?.map((s) => (
          <div key={s.id} className="flex items-start gap-2 rounded-md border border-dashed border-border-strong p-3">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-foreground">{s.label}</p>
              <p className="text-xs text-foreground-secondary">Held on all {s.documents} documents where its fields were read. Check it makes sense for the form before adding.</p>
            </div>
            <Button variant="primary" size="sm" onClick={() => add(s)}>
              <Plus /> Add
            </Button>
          </div>
        ))}
      </section>

      <section aria-label="Write a rule" className="flex flex-col gap-3 rounded-md border border-border-strong p-3">
        <h3 className="label">Write a rule</h3>
        <Label>
          Kind
          <NativeSelect value={draft.kind} onChange={(e) => set({ kind: e.target.value })}>
            {KINDS.map(([k, name]) => (
              <option key={k} value={k}>
                {name}
              </option>
            ))}
          </NativeSelect>
        </Label>
        <div className="grid grid-cols-2 gap-3">
          {fieldSelect('a', draft.kind === 'one_of' ? 'Field' : draft.kind === 'equal' ? 'Field 1' : 'A')}
          {draft.kind !== 'one_of' && fieldSelect('b', draft.kind === 'equal' ? 'Field 2' : 'B')}
          {draft.kind === 'sum' && fieldSelect('c', 'C (optional)')}
          {(draft.kind === 'product' || draft.kind === 'sum') && fieldSelect('target', 'Total')}
        </div>
        {draft.kind === 'one_of' && (
          <div className="flex flex-col gap-2">
            <label className="flex items-center gap-2 text-xs text-foreground-secondary">
              <input type="checkbox" className="size-4 accent-primary" checked={draft.iso} onChange={(e) => set({ iso: e.target.checked })} /> Any ISO 4217 currency code
            </label>
            {!draft.iso && (
              <Label>
                Allowed values (comma separated)
                <Input value={draft.values} onChange={(e) => set({ values: e.target.value })} placeholder="e.g. P, C" />
              </Label>
            )}
          </div>
        )}
        <Label>
          Name (shown when a document fails it)
          <Input value={draft.label} onChange={(e) => set({ label: e.target.value })} placeholder="e.g. total = quantity x price" />
        </Label>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            size="sm"
            disabled={!draftCheck}
            onClick={() => {
              add(draftCheck);
              set({ label: '' });
            }}
          >
            <Plus /> Add rule
          </Button>
          <Button variant="outline" size="sm" disabled={!draftCheck || !templateId} onClick={() => runTest(draftCheck, 'draft')}>
            <FlaskConical /> Test first
          </Button>
        </div>
        <TestResult result={tests.draft} />
      </section>
    </div>
  );
}
