import { useEffect, useRef, useState } from 'react';
import { Rect, Shadow } from 'fabric';
import { relativeBBoxToRect } from './geometry';
import { useDocumentCanvas } from './useDocumentCanvas';
import {
  ApiValidationError,
  formatErrorDetail,
  getExtractionResult,
  getTemplateVersion,
  listReviewQueue,
  resolveFieldReview,
  resolveNewTemplateNeeded,
} from './api';

const CANVAS_W = 700;
const CANVAS_H = 560;
// Module 8 stores a review-sized render of the page each extraction ran
// against, so the reviewer sees the actual document. Results written before
// that column existed have no image and fall back to the sample.
const FALLBACK_IMAGE_URL = '/sample-document.png';
const resultImageUrl = (resultId) => `/results/${resultId}/image`;

const TYPE_META = {
  field_review: { label: 'Field Review', color: '#14b8a6' },
  new_template_needed: { label: 'New Template', color: '#8b5cf6' },
};

// Deliberately distinct from Module 3's dashed anchor/field draw-box styling
// (which means "you are drawing this") - solid, higher-contrast, with a
// glow, so this reads as "look here" instead.
const HIGHLIGHT_STYLE = { stroke: '#f43f5e', strokeWidth: 3, fill: 'rgba(244,63,94,0.16)' };

function timeAgo(isoString) {
  const diffSec = Math.max(0, Math.floor((Date.now() - new Date(isoString).getTime()) / 1000));
  if (diffSec < 60) return 'just now';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

function errorMessages(err) {
  if (err instanceof ApiValidationError) return formatErrorDetail(err.detail);
  return [String(err)];
}

export default function ReviewQueue() {
  const [entries, setEntries] = useState([]);
  const [loadState, setLoadState] = useState('loading'); // loading | ready | error
  const [selectedId, setSelectedId] = useState(null);
  const [toast, setToast] = useState(null);

  useEffect(() => {
    (async () => {
      setLoadState('loading');
      try {
        const list = await listReviewQueue('pending');
        setEntries(list);
        setSelectedId(list.length > 0 ? list[0].id : null);
        setLoadState('ready');
      } catch {
        setLoadState('error');
      }
    })();
  }, []);

  function handleResolved(resolvedId) {
    const idx = entries.findIndex((e) => e.id === resolvedId);
    const next = entries.filter((e) => e.id !== resolvedId);
    setEntries(next);
    // Don't dump the reviewer back to an empty pane between every
    // resolution - land on whatever took this item's place in the list,
    // falling back to the last remaining item if this one was at the end.
    setSelectedId(next.length === 0 ? null : (next[idx] ?? next[next.length - 1]).id);
    setToast('Resolved.');
    setTimeout(() => setToast(null), 2500);
  }

  const selectedEntry = entries.find((e) => e.id === selectedId) ?? null;

  return (
    <div className="review">
      <div className="review-queue-list">
        {loadState === 'loading' && <p className="hint">Loading…</p>}
        {loadState === 'error' && <p className="hint">Couldn't load the review queue.</p>}
        {loadState === 'ready' && entries.length === 0 && <EmptyState />}
        {entries.map((entry) => (
          <QueueListItem
            key={entry.id}
            entry={entry}
            selected={entry.id === selectedId}
            onClick={() => setSelectedId(entry.id)}
          />
        ))}
      </div>

      <div className="review-detail-pane">
        {toast && <div className="toast">{toast}</div>}
        {loadState === 'ready' && entries.length === 0 && <EmptyState large />}
        {loadState === 'ready' && entries.length > 0 && !selectedEntry && (
          <p className="hint">Select an item from the queue.</p>
        )}
        {selectedEntry && (
          <ReviewDetail key={selectedEntry.id} entry={selectedEntry} onResolved={() => handleResolved(selectedEntry.id)} />
        )}
      </div>
    </div>
  );
}

function EmptyState({ large }) {
  return (
    <div className={large ? 'empty-state empty-state-large' : 'empty-state'}>
      <div className="empty-icon">✓</div>
      <p className="empty-title">Queue is empty</p>
      <p className="hint">Nothing needs review right now.</p>
    </div>
  );
}

function QueueListItem({ entry, selected, onClick }) {
  const meta = TYPE_META[entry.entry_type];
  const label =
    entry.entry_type === 'field_review'
      ? entry.payload.field_id
      : (entry.payload.attempted_type_hint ?? '(no type hint)');

  return (
    <button className={`queue-item${selected ? ' selected' : ''}`} onClick={onClick}>
      <span className="type-badge" style={{ background: meta.color }}>
        {meta.label}
      </span>
      <span className="queue-item-label">{label}</span>
      <span className="queue-item-time">{timeAgo(entry.created_at)}</span>
    </button>
  );
}

function ReviewDetail({ entry, onResolved }) {
  if (entry.entry_type === 'field_review') {
    return <FieldReviewDetail entry={entry} onResolved={onResolved} />;
  }
  return <NewTemplateNeededDetail entry={entry} onResolved={onResolved} />;
}

function FieldReviewDetail({ entry, onResolved }) {
  const { payload } = entry;
  const [confirmedValue, setConfirmedValue] = useState(payload.original_value);
  const [bboxState, setBboxState] = useState({ status: 'loading' });
  const [submitState, setSubmitState] = useState({ status: 'idle' });

  const [imageUrl, setImageUrl] = useState(() => resultImageUrl(payload.extraction_result_id));
  const { canvasElRef, fabricCanvasRef, imageRef, imageReady, imageFailed } = useDocumentCanvas(
    imageUrl,
    CANVAS_W,
    CANVAS_H,
  );
  const highlightRectRef = useRef(null);

  useEffect(() => {
    if (imageFailed && imageUrl !== FALLBACK_IMAGE_URL) setImageUrl(FALLBACK_IMAGE_URL);
  }, [imageFailed, imageUrl]);

  useEffect(() => {
    let cancelled = false;
    setBboxState({ status: 'loading' });
    (async () => {
      try {
        const result = await getExtractionResult(payload.extraction_result_id);
        const template = await getTemplateVersion(result.template_id, result.version);
        const field = template.fields.find((f) => f.id === payload.field_id);
        if (cancelled) return;
        if (!field) {
          setBboxState({
            status: 'error',
            message: `Field '${payload.field_id}' not found in ${result.template_id} v${result.version}`,
          });
          return;
        }
        setBboxState({ status: 'ready', bbox: field.bbox });
      } catch (err) {
        if (!cancelled) setBboxState({ status: 'error', message: errorMessages(err).join('; ') });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [payload.extraction_result_id, payload.field_id]);

  useEffect(() => {
    const canvas = fabricCanvasRef.current;
    const image = imageRef.current;
    if (!canvas || !imageReady || bboxState.status !== 'ready') return;

    if (highlightRectRef.current) {
      canvas.remove(highlightRectRef.current);
    }
    const box = relativeBBoxToRect(bboxState.bbox, image);
    const rect = new Rect({
      left: box.left,
      top: box.top,
      width: box.width,
      height: box.height,
      originX: 'left',
      originY: 'top',
      selectable: false,
      evented: false,
      ...HIGHLIGHT_STYLE,
      shadow: new Shadow({ color: 'rgba(244,63,94,0.65)', blur: 18 }),
    });
    canvas.add(rect);
    highlightRectRef.current = rect;
    canvas.requestRenderAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imageReady, bboxState]);

  async function handleSubmit() {
    setSubmitState({ status: 'submitting' });
    try {
      await resolveFieldReview(entry.id, confirmedValue);
      onResolved();
    } catch (err) {
      setSubmitState({ status: 'error', messages: errorMessages(err) });
    }
  }

  return (
    <div className="detail">
      <div className="detail-header">
        <span className="type-badge" style={{ background: TYPE_META.field_review.color }}>
          Field Review
        </span>
        <h2 className="mono-value">{payload.field_id}</h2>
      </div>

      <div className="detail-body">
        <div className="detail-canvas-pane">
          <canvas ref={canvasElRef} />
          {bboxState.status === 'error' && <p className="hint error-text">Couldn't locate this field's box: {bboxState.message}</p>}
        </div>

        <div className="detail-form">
          <div className="field-block">
            <span className="field-label">Original value</span>
            <code className="mono-value block">{payload.original_value}</code>
          </div>

          <div className="field-block">
            <span className="field-label">Reasons flagged</span>
            <ul className="reason-list">
              {payload.reasons.map((r) => (
                <li key={r} className="mono-value">
                  {r}
                </li>
              ))}
            </ul>
          </div>

          <label className="field-block">
            <span className="field-label">Confirmed value</span>
            <input
              type="text"
              className="mono-input"
              value={confirmedValue}
              onChange={(e) => setConfirmedValue(e.target.value)}
            />
          </label>

          <button className="submit" disabled={submitState.status === 'submitting'} onClick={handleSubmit}>
            {submitState.status === 'submitting' ? 'Saving…' : 'Confirm value'}
          </button>

          {submitState.status === 'error' && (
            <div className="banner error">
              <strong>Resolve failed:</strong>
              <ul>
                {submitState.messages.map((m, i) => (
                  <li key={i}>{m}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function NewTemplateNeededDetail({ entry, onResolved }) {
  const { payload } = entry;
  const [createdTemplateId, setCreatedTemplateId] = useState('');
  const [submitState, setSubmitState] = useState({ status: 'idle' });

  async function handleSubmit() {
    if (!createdTemplateId.trim()) {
      setSubmitState({ status: 'error', messages: ['Template ID is required.'] });
      return;
    }
    setSubmitState({ status: 'submitting' });
    try {
      await resolveNewTemplateNeeded(entry.id, createdTemplateId.trim());
      onResolved();
    } catch (err) {
      setSubmitState({ status: 'error', messages: errorMessages(err) });
    }
  }

  return (
    <div className="detail">
      <div className="detail-header">
        <span className="type-badge" style={{ background: TYPE_META.new_template_needed.color }}>
          New Template Needed
        </span>
        <h2 className="mono-value">{payload.attempted_type_hint ?? '(no type hint)'}</h2>
      </div>

      <div className="detail-form wide">
        <div className="field-block">
          <span className="field-label">Attempted type hint</span>
          <code className="mono-value block">{payload.attempted_type_hint ?? '(none given)'}</code>
        </div>

        <div className="field-block">
          <span className="field-label">Rejection reason</span>
          <code className="mono-value block">{payload.rejection_reason}</code>
        </div>

        <p className="hint note">
          No document preview here - there's no confirmed template yet. Use the Template Designer to create the
          right template for this document type first, then paste its template ID below to close this out.
        </p>

        <label className="field-block">
          <span className="field-label">Created template ID</span>
          <input
            type="text"
            className="mono-input"
            value={createdTemplateId}
            onChange={(e) => setCreatedTemplateId(e.target.value)}
            placeholder="e.g. bits_goa_id_card"
          />
        </label>

        <button className="submit" disabled={submitState.status === 'submitting'} onClick={handleSubmit}>
          {submitState.status === 'submitting' ? 'Saving…' : 'Confirm template'}
        </button>

        {submitState.status === 'error' && (
          <div className="banner error">
            <strong>Resolve failed:</strong>
            <ul>
              {submitState.messages.map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
