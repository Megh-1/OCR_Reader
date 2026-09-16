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

export async function createTemplate(payload) {
  const res = await fetch('/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  const body = await res.json().catch(() => null);

  if (!res.ok) {
    throw new ApiValidationError(res.status, body?.detail ?? body);
  }
  return body;
}
