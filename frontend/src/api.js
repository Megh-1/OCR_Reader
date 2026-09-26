export class ApiValidationError extends Error {
  constructor(status, detail) {
    super(`Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Normalizes whatever shape the backend's error body actually has into
 * readable lines. FastAPI/Pydantic validation failures (bad bbox range,
 * x1<x2, zero anchors) come back as `{"detail": [{"loc":[...], "msg":...}]}`
 * - a list, not a single message - so that's handled explicitly rather
 * than just stringifying the whole body.
 */
export function formatErrorDetail(detail) {
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      const loc = Array.isArray(item?.loc) ? item.loc.join('.') : String(item?.loc ?? '');
      return `${loc}: ${item?.msg ?? JSON.stringify(item)}`;
    });
  }
  if (typeof detail === 'string') return [detail];
  if (detail && typeof detail === 'object') {
    return [detail.message ?? JSON.stringify(detail)];
  }
  return ['Unknown error'];
}

export function errorLines(err) {
  if (err instanceof ApiValidationError) return formatErrorDetail(err.detail);
  return [String(err?.message ?? err)];
}

async function requestJson(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => null);

  if (!res.ok) {
    throw new ApiValidationError(res.status, body?.detail ?? body);
  }
  return body;
}

// --- templates (Modules 3/4) ---------------------------------------------------------
export function createTemplate(payload) {
  return requestJson('/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}
export const listTemplates = () => requestJson('/templates');
export const getTemplate = (templateId) => requestJson(`/templates/${encodeURIComponent(templateId)}`);
export const listTemplateVersions = (templateId) => requestJson(`/templates/${encodeURIComponent(templateId)}/versions`);
export const getTemplateVersion = (templateId, version) =>
  requestJson(`/templates/${encodeURIComponent(templateId)}/versions/${version}`);

// --- results (Module 8) --------------------------------------------------------------
export const listResults = (limit = 60) => requestJson(`/results?limit=${limit}`);
export const getExtractionResult = (resultId) => requestJson(`/results/${resultId}`);
export const resultImageUrl = (resultId) => `/results/${resultId}/image`;

// --- review queue (Module 9) ---------------------------------------------------------
export const listReviewQueue = (status = 'pending') => requestJson(`/review?status=${encodeURIComponent(status)}`);

export function resolveFieldReview(queueId, confirmedValue) {
  return requestJson(`/review/${queueId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmed_value: confirmedValue }),
  });
}

export function resolveNewTemplateNeeded(queueId, createdTemplateId) {
  return requestJson(`/review/${queueId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ created_template_id: createdTemplateId }),
  });
}

// --- batch ---------------------------------------------------------------------------
/** Multipart, not JSON - no Content-Type header on purpose, so the browser sets the boundary. */
export function submitBatch(files, typeHint) {
  const formData = new FormData();
  for (const file of files) formData.append('files', file);
  if (typeHint) formData.append('type_hint', typeHint);
  return requestJson('/documents/batch', { method: 'POST', body: formData });
}
export const getBatchStatus = (batchId) => requestJson(`/documents/batch/${batchId}/status`);
export const listBatches = (limit = 12) => requestJson(`/documents/batch?limit=${limit}`);
