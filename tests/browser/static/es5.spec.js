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
      parseInt: 'every browser', parseFloat: 'every browser', isNaN: 'every browser',
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

  test(`static/${name} only calls methods and reads properties that the oldest phones and TVs have`, () => {
    // The same idea one level down: every name after a dot. ES5 syntax does not stop a script
    // from calling something an old engine never had (element.closest, array.includes,
    // promise.finally, element.remove, string.padStart): there it is "undefined is not a
    // function" in the middle of the script, and everything after that line never runs.
    // Each name is listed with the oldest target that has it; a new one is a decision.
    const MEMBERS = {
      // the DOM of the 1990s and 2000s
      getElementById: 'every browser', getAttribute: 'every browser', addEventListener: 'every browser',
      querySelector: 'Chrome 1, Safari 3.1', preventDefault: 'every browser', focus: 'every browser',
      textContent: 'every browser', target: 'every browser', keyCode: 'every browser (deprecated, and the only one every TV remote sets)',
      name: 'every browser', value: 'every browser', checked: 'every browser', reload: 'every browser',
      split: 'ES3', toFixed: 'ES3', getTime: 'ES1',
      hidden: 'Chrome 6, Safari 5.1: the hidden attribute as a property',
      activeElement: 'Chrome 2, Safari 4, Firefox 3',
      altKey: 'every browser', ctrlKey: 'every browser', metaKey: 'every browser',
      getBoundingClientRect: 'every browser', bottom: 'every browser (a rectangle from getBoundingClientRect)',
      innerHeight: 'every browser', documentElement: 'every browser', clientHeight: 'every browser',
      scrollIntoView: 'every browser, in the true/false form used here',
      onLine: 'every browser; only a plain false is believed',
      originalTarget: 'Firefox only. Everywhere else it is undefined, which the script reads as "the key was pressed on the video itself"',
      // full screen: each name is tested for before it is called, so a browser without it skips the line
      fullscreenElement: 'Chrome 71, Safari 16.4; tested for first', exitFullscreen: 'Chrome 71, Safari 16.4; tested for first',
      webkitFullscreenElement: 'Chrome 15, Safari 5.1; tested for first', webkitExitFullscreen: 'Chrome 15, Safari 5.1, and the iPhone player; tested for first',
      webkitDisplayingFullscreen: 'Safari and iOS only (the iPhone player); undefined elsewhere, which skips the line',
      // the media element
      play: 'every browser with <video>; returns a promise from Chrome 50 and Safari 10, undefined before (the script checks)',
      pause: 'every browser with <video>', load: 'every browser with <video>', src: 'every browser with <video>',
      error: 'every browser with <video>', paused: 'every browser with <video>', ended: 'every browser with <video>',
      currentTime: 'every browser with <video>', readyState: 'every browser with <video>', seeking: 'every browser with <video>',
      // fetch() and its promise: Chrome 42, Safari and iOS 10.1 (every target)
      then: 'with fetch', ok: 'with fetch',
    };
    const ast = acorn.parse(code, { ecmaVersion: 5, sourceType: 'script' });
    const members = new Set();
    (function walk(node) {
      if (!node || typeof node.type !== 'string') return;
      if (node.type === 'MemberExpression' && !node.computed) members.add(node.property.name);
      for (const k of Object.keys(node)) {
        const child = node[k];
        if (Array.isArray(child)) child.forEach(walk);
        else if (child && typeof child.type === 'string') walk(child);
      }
    }(ast));
    const unknown = [...members].filter((n) => !(n in MEMBERS)).sort();
    expect(unknown, `static/${name} uses .${unknown.join(', .')}: check that iOS 12 Safari and Chromium 53 have it (caniuse.com, MDN), then add it to MEMBERS in this test`).toEqual([]);
    const unused = Object.keys(MEMBERS).filter((n) => !members.has(n)).sort();
    expect(unused, `MEMBERS in this test lists names static/${name} no longer uses: delete them`).toEqual([]);
  });

  test(`static/${name} never names a reserved word after a dot, and never trusts play() to return a promise`, () => {
    // Two traps for the oldest engines that an ES5 parser lets through:
    //   * x.catch(...) / x.finally(...) / x.delete(...): a reserved word as a property name is a
    //     syntax error in the JavaScriptCore of old Samsung and LG TVs. Write x["catch"](...).
    //   * video.play().catch(...): before Chrome 50 and Safari 10 play() returns nothing, and
    //     calling .catch on nothing stops the script. Keep the result and test it first.
    expect(code, 'a reserved word after a dot').not.toMatch(/\.(catch|finally|delete|default|class|new|in|do|if|for|try|var|void|while|with)\s*\(/);
    expect(code, 'play() used as if it always returned a promise').not.toMatch(/\.play\(\)\s*(\.|\[)/);
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
