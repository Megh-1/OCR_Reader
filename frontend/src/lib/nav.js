// Hash routes: #/templates, #/batch/<batch_id>, #/review, #/results/<result_id>.
export function navigate(key, param) {
  window.location.hash = param ? `/${key}/${param}` : `/${key}`;
}
