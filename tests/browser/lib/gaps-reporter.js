// Prints, at the end of a run, every known gap the run touched (see known-gaps.js) and on
// which projects, and writes the same list to report/known-gaps.txt. A green run with gaps
// listed here means "nothing NEW is wrong", not "nothing is wrong".

const fs = require('fs');
const path = require('path');

class GapsReporter {
  constructor() {
    this.gaps = new Map(); // id -> { what, projects:Set, seen:Set }
    this.how = new Map(); // "play pressed by" -> Set of projects
    this.confirm = new Map(); // "to confirm" line -> Set of projects
    this.varies = new Map(); // "varies by device" line -> Set of projects
  }

  onTestEnd(test, result) {
    const project = test.parent.project() ? test.parent.project().name : '';
    for (const note of [...test.annotations, ...(result.annotations || [])]) {
      if (note.type === 'known gap') {
        const text = note.description || '';
        const id = text.split(':')[0];
        const seen = (text.match(/\[seen: (.*)\]$/) || [])[1];
        const what = text.slice(id.length + 1).replace(/\s*\[seen: .*\]$/, '').trim();
        if (!this.gaps.has(id)) this.gaps.set(id, { what, projects: new Set(), seen: new Set() });
        this.gaps.get(id).projects.add(project);
        if (seen) this.gaps.get(id).seen.add(seen);
      }
      if (note.type === 'varies by device') {
        const line = (note.description || '').replace(/ \[.*\]$/, '');
        if (!this.varies.has(line)) this.varies.set(line, new Set());
        this.varies.get(line).add(project);
      }
      if (note.type === 'to confirm') {
        const line = (note.description || '').replace(/ \[.*\]$/, '').replace(/ \((?:[^()]|\([^()]*\))*\)\./, '.');
        if (!this.confirm.has(line)) this.confirm.set(line, new Set());
        this.confirm.get(line).add(project);
      }
      if (note.type === 'play pressed by') {
        if (!this.how.has(note.description)) this.how.set(note.description, new Set());
        this.how.get(note.description).add(project);
      }
    }
  }

  onEnd() {
    const lines = [];
    if (this.gaps.size) {
      lines.push('', `KNOWN GAPS still open (${this.gaps.size}). Product bugs excused in lib/known-gaps.js, not fixed:`);
      for (const [id, gap] of this.gaps) {
        lines.push(`  - ${id}`);
        lines.push(`      ${gap.what}`);
        lines.push(`      on: ${[...gap.projects].sort().join(', ')}`);
        const seen = [...gap.seen];
        if (seen.length) lines.push(`      seen: ${seen.slice(0, 3).join(' || ')}${seen.length > 3 ? ` (+${seen.length - 3} more)` : ''}`);
      }
    }
    if (this.confirm.size) {
      lines.push('', `TO CONFIRM (${this.confirm.size}). Written without this engine at hand; this run is the first evidence:`);
      for (const [line, projects] of this.confirm) lines.push(`  - ${line}`, `      on: ${[...projects].sort().join(', ')}`);
    }
    if (this.varies.size) {
      lines.push('', `DEPENDS ON THE DEVICE (${this.varies.size}). Listed in lib/known-gaps.js as 'varies'; this is what THIS machine's fonts gave:`);
      for (const [line, projects] of this.varies) lines.push(`  - ${line}`, `      on: ${[...projects].sort().join(', ')}`);
    }
    if (this.how.size) {
      lines.push('', 'How "play" was pressed:');
      for (const [how, projects] of this.how) lines.push(`  - ${how}: ${[...projects].sort().join(', ')}`);
    }
    if (!lines.length) return;
    lines.push('');
    const text = lines.join('\n');
    console.log(text);
    try {
      const folder = path.join(__dirname, '..', 'report');
      fs.mkdirSync(folder, { recursive: true });
      // A live run and the macOS probe write beside the main run's list, not over it.
      const name = `${process.env.LIVE_WATCH_URL ? 'live-' : ''}${process.env.GAPS_REPORT || 'known-gaps.txt'}`;
      fs.writeFileSync(path.join(folder, name), text);
    } catch (e) { /* the list was printed; the file is a convenience */ }
  }
}

module.exports = GapsReporter;
