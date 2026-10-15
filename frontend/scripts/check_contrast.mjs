// WCAG contrast check for every text/background pair the design system uses.
// Run: node scripts/check_contrast.mjs   (exits 1 if any pair is under AA)
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const tokens = Object.fromEntries([...css.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})\s*;/gi)].map((m) => [m[1], m[2]]));
const resolveVar = (name) => {
  const m = css.match(new RegExp(`--${name}:\\s*var\\(--([a-z0-9-]+)\\)`));
  return m ? tokens[m[1]] : tokens[name];
};

const lum = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

// [text role, background role, minimum]
const PAIRS = [
  ['foreground', 'background', 4.5],
  ['foreground', 'surface', 4.5],
  ['foreground-secondary', 'background', 4.5],
  ['foreground-secondary', 'surface', 4.5],
  ['foreground-secondary', 'surface-sunken', 4.5],
  ['foreground-subtle', 'surface', 4.5],
  ['foreground-subtle', 'background', 4.5],
  ['primary-foreground', 'primary', 4.5],
  ['primary-foreground', 'primary-hover', 4.5],
  ['primary-foreground', 'primary-active', 4.5],
  ['primary', 'surface', 4.5],
  ['primary-text', 'primary-surface', 4.5],
  ['success', 'success-surface', 4.5],
  ['warning', 'warning-surface', 4.5],
  ['danger', 'danger-surface', 4.5],
  ['primary-foreground', 'danger', 4.5],
  ['primary-foreground', 'danger-hover', 4.5],
  ['foreground-secondary', 'primary-surface', 4.5],
  // non-text fills (bars, dots, markers) need 3:1 against the surfaces they sit on (WCAG 1.4.11)
  ['success-solid', 'surface', 3],
  ['warning-solid', 'surface', 3],
  ['danger-solid', 'surface', 3],
  ['success-solid', 'surface-sunken', 3],
  ['warning-solid', 'surface-sunken', 3],
  ['danger-solid', 'surface-sunken', 3],
];

let failed = 0;
for (const [fg, bg, min] of PAIRS) {
  const r = ratio(resolveVar(fg), resolveVar(bg));
  const ok = r >= min;
  if (!ok) failed += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${r.toFixed(2).padStart(5)}:1  ${fg} on ${bg}`);
}
process.exit(failed ? 1 : 0);
