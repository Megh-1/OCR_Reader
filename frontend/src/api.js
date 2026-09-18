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

async function requestJson(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => null);

  if (!res.ok) {
    throw new ApiValidationError(res.status, body?.detail ?? body);
  }
  return body;
}

export function createTemplate(payload) {
  return requestJson('/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function listTemplates() {
  return requestJson('/templates');
}

export function getTemplate(templateId) {
  return requestJson(`/templates/${encodeURIComponent(templateId)}`);
}

export function getTemplateVersion(templateId, version) {
  return requestJson(`/templates/${encodeURIComponent(templateId)}/versions/${version}`);
}

export function getExtractionResult(resultId) {
  return requestJson(`/results/${resultId}`);
}

export function listReviewQueue(status = 'pending') {
  return requestJson(`/review?status=${encodeURIComponent(status)}`);
}

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

/**
 * Multipart, not JSON - no Content-Type header is set here on purpose, so
 * the browser fills in the multipart boundary itself.
 */
export function processDocument(file, typeHint) {
  const formData = new FormData();
  formData.append('file', file);
  if (typeHint) formData.append('type_hint', typeHint);
  return requestJson('/documents/process', { method: 'POST', body: formData });
}
