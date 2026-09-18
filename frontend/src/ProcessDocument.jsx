import { useState } from 'react';
import { ApiValidationError, formatErrorDetail, processDocument } from './api';

export default function ProcessDocument() {
  const [file, setFile] = useState(null);
  const [typeHint, setTypeHint] = useState('');
  const [state, setState] = useState({ status: 'idle' });

  async function handleSubmit() {
    if (!file) return;
    setState({ status: 'submitting' });
    try {
      const result = await processDocument(file, typeHint.trim() || undefined);
      setState({ status: 'success', result });
    } catch (err) {
      if (err instanceof ApiValidationError) {
        setState({ status: 'error', messages: formatErrorDetail(err.detail) });
      } else {
        setState({ status: 'error', messages: [String(err)] });
      }
    }
  }

  return (
    <div className="process-document">
      <div className="side-panel">
        <label className="template-id-label">
          Document file (PDF, JPEG or PNG)
          <input type="file" accept="application/pdf,image/jpeg,image/png" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>

        <label className="template-id-label">
          Type hint (optional)
          <input
            type="text"
            value={typeHint}
            onChange={(e) => setTypeHint(e.target.value)}
            placeholder="e.g. bits_goa_id_card"
          />
        </label>

        <button className="submit" disabled={!file || state.status === 'submitting'} onClick={handleSubmit}>
          {state.status === 'submitting' ? 'Processing…' : 'Process document'}
        </button>

        {state.status === 'error' && (
          <div className="banner error">
            <strong>Processing failed:</strong>
            <ul>
              {state.messages.map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="process-results">
        {state.status !== 'success' && (
          <div className="empty-state empty-state-large">
            <p className="hint">Pick a file and click "Process document" to run it through the full pipeline.</p>
          </div>
        )}
        {state.status === 'success' &&
          state.result.pages.map((page) => <PageResultCard key={page.page_index} page={page} filename={state.result.filename} />)}
      </div>
    </div>
  );
}

function PageResultCard({ page, filename }) {
  return (
    <div className="review-detail-pane page-result-card">
      <div className="detail-header">
        <h2>
          {filename} — page {page.page_index + 1}
        </h2>
        <span className={`type-badge ${page.confirmed ? 'status-complete' : 'status-review'}`}>
          {page.confirmed ? 'confirmed' : page.reason}
        </span>
      </div>

      {!page.confirmed && (
        <p className="hint">
          Document type could not be confirmed ({page.reason}). Sent to the review queue as a new-template-needed
          entry (id {page.review_queue_id}).
        </p>
      )}

      {page.confirmed && page.result && (
        <>
          <p className="hint">
            Matched <strong>{page.result.template_id}</strong> v{page.result.version} — overall status:{' '}
            <strong>{page.result.overall_status}</strong>
          </p>
          <div className="field-list">
            {Object.values(page.result.fields).map((field) => (
              <div className="field-block" key={field.field_id}>
                <span className="field-label">
                  {field.field_id} {field.needs_review && <span className="flag-badge">flagged</span>}
                </span>
                <span className="mono-value block">{field.value || '(empty)'}</span>
                <span className="hint">
                  confidence {field.confidence.toFixed(1)}
                  {field.reasons.length > 0 ? ` — ${field.reasons.join(', ')}` : ''}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
