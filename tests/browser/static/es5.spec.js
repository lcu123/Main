// The oldest engines a customer may open the link in (iOS 12 Safari, the Chromium 53 inside
// an LG webOS 4 TV, the Chromium 56 inside a 2017 Samsung TV) stop at the FIRST piece of
// syntax they do not know and run none of the file. So the customer-side script must be
// plain ES5: no arrow functions, no let/const, no template strings, no optional chaining.

const { test, expect } = require('@playwright/test');
const acorn = require('acorn');
const { scripts, CUSTOMER_TEMPLATES, template } = require('./sources');

for (const { name, code } of scripts()) {
  test(`static/${name} is ES5: the oldest phones and TVs can run it`, () => {
    let problem = '';
    try {
      acorn.parse(code, { ecmaVersion: 5, sourceType: 'script' });
    } catch (error) {
      const line = code.split('\n')[error.loc.line - 1] || '';
      problem = `static/${name} line ${error.loc.line}: ${error.message}\n    ${line.trim()}\n`
        + '  This is syntax newer than ES5. Old iOS and smart-TV browsers will not run ANY of the file.';
    }
    expect(problem).toBe('');
  });

  test(`static/${name} only uses browser features that are on the list`, () => {
    // Anything a script reaches for that is not plain ES5 or the 1990s DOM has to be named
    // here with the first versions that have it, so adding one is a decision, not an accident.
    const KNOWN = {
      document: 'every browser', window: 'every browser', navigator: 'every browser', location: 'every browser',
      setTimeout: 'every browser', clearTimeout: 'every browser', Date: 'every browser', Math: 'every browser', JSON: 'ES5',
      encodeURIComponent: 'every browser', undefined: 'ES5', Object: 'ES5', Array: 'ES5', String: 'ES5', Number: 'ES5', Error: 'ES5',
      // Chrome 42, Safari and iOS 10.1, Firefox 39, Samsung 4: every target has it. `keepalive` is ignored where unknown.
      fetch: 'Chrome 42, Safari/iOS 10.1, Firefox 39, Samsung 4',
      XMLHttpRequest: 'every browser',
    };
    const ast = acorn.parse(code, { ecmaVersion: 5, sourceType: 'script' });
    const declared = new Set();
    const used = new Set();
    (function walk(node, parent, key) {
      if (!node || typeof node.type !== 'string') return;
      if (node.type === 'FunctionDeclaration' || node.type === 'FunctionExpression') {
        if (node.id) declared.add(node.id.name);
        node.params.forEach((p) => declared.add(p.name));
      }
      if (node.type === 'VariableDeclarator') declared.add(node.id.name);
      if (node.type === 'CatchClause' && node.param) declared.add(node.param.name);
      if (node.type === 'Identifier') {
        const isProperty = parent && ((parent.type === 'MemberExpression' && key === 'property' && !parent.computed) || (parent.type === 'Property' && key === 'key'));
        if (!isProperty) used.add(node.name);
      }
      for (const k of Object.keys(node)) {
        const child = node[k];
        if (Array.isArray(child)) child.forEach((c) => walk(c, node, k));
        else if (child && typeof child.type === 'string') walk(child, node, k);
      }
    }(ast, null, ''));
    const globals = [...used].filter((n) => !declared.has(n));
    const unknown = globals.filter((n) => !(n in KNOWN));
    expect(unknown, `static/${name} uses ${unknown.join(', ')}: check which browsers have it (caniuse.com), then add it to KNOWN in this test`).toEqual([]);
  });
}

test('customer pages carry no inline script and no on...= handlers (the page\'s own CSP would silently drop them)', () => {
  const found = [];
  for (const name of CUSTOMER_TEMPLATES) {
    const html = template(name);
    for (const m of html.matchAll(/<script\b([^>]*)>/g)) {
      if (!/\ssrc="\/static\//.test(m[1])) found.push(`${name}: <script${m[1]}> is not a /static/ file`);
    }
    for (const m of html.matchAll(/\son[a-z]+="/g)) found.push(`${name}: inline handler${m[0]}...`);
    for (const m of html.matchAll(/href="javascript:/g)) found.push(`${name}: ${m[0]}`);
  }
  expect(found).toEqual([]);
});
