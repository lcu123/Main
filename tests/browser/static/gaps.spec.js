// lib/known-gaps.js is the one list of product bugs this suite excuses. Keep it honest:
// every entry is used by a spec, every id a spec asks about exists, and each says what happens.

const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const GAPS = require('../lib/known-gaps');

test('known-gaps.js: every entry is used by a spec and says what happens', () => {
  const specs = fs.readdirSync(path.join(__dirname, '..', 'specs')).map((f) => fs.readFileSync(path.join(__dirname, '..', 'specs', f), 'utf8')).join('\n');
  const ids = GAPS.map((g) => g.id);
  expect(new Set(ids).size, 'two entries share an id').toBe(ids.length);
  for (const gap of GAPS) {
    expect(specs.includes(`'${gap.id}'`), `"${gap.id}" is not used by any spec: delete it`).toBe(true);
    expect(typeof gap.where, `"${gap.id}" needs a where()`).toBe('function');
    expect(gap.what.length, `"${gap.id}" must say what the customer gets`).toBeGreaterThan(40);
  }
});
