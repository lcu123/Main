// The customer-side source files the static checks read. Paths are from the repo root.
const fs = require('fs');
const path = require('path');

const REPO = path.join(__dirname, '..', '..', '..');
const TEMPLATES = path.join(REPO, 'src', 'zest_video', 'templates');
const STATIC = path.join(REPO, 'src', 'zest_video', 'static');

// Every page a customer can land on. The office pages and the emails are not in this list.
const CUSTOMER_TEMPLATES = ['_base.html', 'watch.html', 'gone.html', 'unsubscribe.html', 'info.html'];

const read = (file) => fs.readFileSync(file, 'utf8');
const template = (name) => read(path.join(TEMPLATES, name));
const scripts = () => fs.readdirSync(STATIC).filter((f) => f.endsWith('.js')).map((f) => ({ name: f, code: read(path.join(STATIC, f)) }));

/** All the CSS the customer pages carry: <style> blocks, {% block style %} bodies and style="" attributes. */
function customerCss() {
  const parts = [];
  for (const name of CUSTOMER_TEMPLATES) {
    const html = template(name);
    for (const m of html.matchAll(/<style>([\s\S]*?)<\/style>/g)) parts.push({ from: `${name} <style>`, css: m[1].replace(/\{%[\s\S]*?%\}/g, '') });
    for (const m of html.matchAll(/\{% block style %\}([\s\S]*?)\{% endblock %\}/g)) if (m[1].trim()) parts.push({ from: `${name} {% block style %}`, css: m[1] });
    let n = 0;
    for (const m of html.matchAll(/\sstyle="([^"]*)"/g)) { n += 1; parts.push({ from: `${name} style="" #${n}`, css: `.inline-${n}{${m[1]}}` }); }
  }
  return parts;
}

module.exports = { REPO, TEMPLATES, STATIC, CUSTOMER_TEMPLATES, template, scripts, customerCss };
