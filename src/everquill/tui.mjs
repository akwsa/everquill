import { spawn } from 'node:child_process';
import { createInterface, emitKeypressEvents } from 'node:readline';
import { fileURLToPath } from 'node:url';

const ESC = '\u001b[';
const noColor = process.env.NO_COLOR !== undefined || !process.stdout.isTTY;
const styles = {
  title: noColor ? '' : `${ESC}1;36m`,
  accent: noColor ? '' : `${ESC}36m`,
  dim: noColor ? '' : `${ESC}2m`,
  warn: noColor ? '' : `${ESC}1;33m`,
  reset: noColor ? '' : `${ESC}0m`,
};
const state = {
  root: process.cwd(), mode: 'ask', session: '', lines: [], input: '', cursor: 0,
  history: [], historyIndex: null, scroll: 0, busy: false, approval: false,
  running: true, worker: null, approvalPurpose: '',
};

export function displayWidth(value) {
  let width = 0;
  for (const char of String(value).replace(/\u001b\[[0-9;]*m/gu, '')) {
    const cp = char.codePointAt(0);
    if (cp === 0 || cp < 32 || (cp >= 0x7f && cp < 0xa0)) continue;
    if (/\p{Mark}/u.test(char) || cp === 0x200d || (cp >= 0xfe00 && cp <= 0xfe0f)) continue;
    width += cp >= 0x1100 && (
      cp <= 0x115f || cp === 0x2329 || cp === 0x232a ||
      (cp >= 0x2e80 && cp <= 0xa4cf) || (cp >= 0xac00 && cp <= 0xd7a3) ||
      (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xfe10 && cp <= 0xfe19) ||
      (cp >= 0xfe30 && cp <= 0xfe6f) || (cp >= 0xff00 && cp <= 0xff60) ||
      (cp >= 0xffe0 && cp <= 0xffe6) || (cp >= 0x1f300 && cp <= 0x1faff)
    ) ? 2 : 1;
  }
  return width;
}

export function isApproved(value) {
  return value === 'y' || value === 'yes';
}

function approvalPrompt(view) {
  if (!view.approval) return 'everquill> ';
  const provider = (view.provider || process.env.EVERQUILL_PROVIDER || 'meta') === 'muse' ? 'Muse.ai' : 'Meta';
  if (view.approvalPurpose === 'tool_execution') return 'Execute read-only recovery tools? [y/N]: ';
  if (view.approvalPurpose === 'tool_results') return `Send tool results to ${provider}? [y/N]: `;
  return view.approvalPurpose === 'context' ? `Send to ${provider}? [y/N]: ` : 'Apply proposal? [y/N]: ';
}

export function inputWindow(view, innerWidth) {
  const prompt = approvalPrompt(view);
  const chars = Array.from(view.input || '');
  const cursor = Math.min(view.cursor || 0, chars.length);
  const capacity = Math.max(0, innerWidth - displayWidth(prompt) - 3);
  let start = cursor;
  while (start > 0 && displayWidth(chars.slice(start - 1, cursor).join('')) <= capacity) start -= 1;
  return {
    text: chars.slice(start).join(''),
    column: 3 + displayWidth(prompt) + displayWidth(chars.slice(start, cursor).join('')),
  };
}

function fit(value, width) {
  let result = '';
  let used = 0;
  const tokens = String(value).match(/\u001b\[[0-9;]*m|[\s\S]/gu) || [];
  for (const char of tokens) {
    if (char.startsWith('\u001b[')) {
      result += char;
      continue;
    }
    const size = displayWidth(char);
    if (used + size > width) break;
    result += char;
    used += size;
  }
  if (String(value).includes('\u001b[') && !result.endsWith(styles.reset)) result += styles.reset;
  return result + ' '.repeat(Math.max(0, width - used));
}

function wrap(value, width) {
  const output = [];
  for (const paragraph of String(value).split('\n')) {
    let line = '';
    for (const char of paragraph.replace(/\t/gu, '    ')) {
      if (displayWidth(line + char) > width) {
        output.push(line);
        line = '';
      }
      line += char;
    }
    output.push(line);
  }
  return output.length ? output : [''];
}

function border(mainWidth, sideWidth, left, middle, right) {
  return left + '─'.repeat(mainWidth) + middle + '─'.repeat(sideWidth) + right;
}

function panelLine(text, width) {
  return '│' + fit(text, width - 2) + '│';
}

function sideLines(width) {
  const stateLabel = state.approval ? 'approval needed' : state.busy ? 'working' : 'ready';
  const content = [
    `Project  ${state.root.split(/[\\/]/u).filter(Boolean).at(-1) || state.root}`,
    '',
    `${styles.accent}SESSION${styles.reset}`,
    state.session || 'main chat',
    '',
    `${styles.accent}MODE${styles.reset}`,
    state.mode,
    '',
    `${styles.accent}STATUS${styles.reset}`,
    `${state.approval ? styles.warn : ''}${stateLabel}${state.approval ? styles.reset : ''}`,
    '',
    `${styles.dim}/help  /mode  /session${styles.reset}`,
    `${styles.dim}PgUp/PgDn scroll${styles.reset}`,
    `${styles.dim}Ctrl+C cancel / exit${styles.reset}`,
  ];
  return content.map((line) => fit(line, width - 2));
}

function plain(value) {
  return value.replace(/\u001b\[[0-9;]*m/gu, '');
}

export function composeFrame({ width, height, state: view = state }) {
  const previous = { ...state };
  Object.assign(state, view);
  try {
    if (width < 40 || height < 10) {
      return [
        `Everquill needs 40 columns × 10 rows (current ${width} × ${height}).`,
        'Resize the terminal; Ctrl+C exits.',
      ].map((line) => line.slice(0, width));
    }
    const wide = width >= 88;
    const sideWidth = wide ? Math.min(30, Math.max(24, Math.floor(width / 4))) : 0;
    const mainWidth = wide ? width - sideWidth - 3 : width - 2;
    const bodyHeight = Math.max(1, height - 8);
    const wrapped = (view.lines || []).flatMap((line) => wrap(line, Math.max(1, mainWidth - 2)));
    if (!wrapped.length) wrapped.push(`${styles.dim}No messages yet. Type a request or /help.${styles.reset}`);
    const maxScroll = Math.max(0, wrapped.length - bodyHeight);
    const scroll = Math.min(view.scroll || 0, maxScroll);
    const end = wrapped.length - scroll;
    const visible = wrapped.slice(Math.max(0, end - bodyHeight), end);
    const title = ` ${styles.title}EVERQUILL${styles.reset}  ${view.root.split(/[\\/]/u).filter(Boolean).at(-1) || view.root} `;
    const session = (view.session || 'main').slice(0, 16);
    const status = view.approval ? 'approval' : view.busy ? 'working' : 'ready';
    const meta = `mode: ${view.mode}   session: ${session}   ${status}`;
    const prompt = approvalPrompt(view);
    const footer = view.approval
      ? 'Only y or yes approves · Enter, Esc or Ctrl+C rejects'
      : 'Enter send · PgUp/PgDn scroll · /help commands · Ctrl+C cancel/exit';
    const result = [];
    if (wide) {
      result.push(border(mainWidth, sideWidth, '╭', '┬', '╮'));
      result.push(`│${fit(` ${title}`, mainWidth)}│${fit(` ${styles.accent}WORKSPACE${styles.reset}`, sideWidth)}│`);
      result.push(`│${fit(` ${meta}`, mainWidth)}│${fit('', sideWidth)}│`);
      result.push(border(mainWidth, sideWidth, '├', '┼', '┤'));
      const side = sideLines(sideWidth);
      for (let row = 0; row < bodyHeight; row += 1) {
        result.push(`│${fit(` ${visible[row] || ''}`, mainWidth)}│${fit(` ${plain(side[row] || '')}`, sideWidth)}│`);
      }
      result.push(border(mainWidth, sideWidth, '├', '┼', '┤'));
      result.push(`│${fit(` ${styles.dim}${footer}${styles.reset}`, mainWidth)}│${fit(` mode: ${view.mode}`, sideWidth)}│`);
      const inputText = inputWindow(view, mainWidth).text;
      result.push(`│${fit(` ${view.approval ? styles.warn : styles.accent}${prompt}${styles.reset}${inputText}`, mainWidth)}│${fit('', sideWidth)}│`);
      result.push(border(mainWidth, sideWidth, '╰', '┴', '╯'));
    } else {
      result.push('╭' + '─'.repeat(width - 2) + '╮');
      result.push(panelLine(` ${title}`, width));
      result.push(panelLine(` ${meta}`, width));
      result.push('├' + '─'.repeat(width - 2) + '┤');
      for (let row = 0; row < bodyHeight; row += 1) result.push(panelLine(` ${visible[row] || ''}`, width));
      result.push('├' + '─'.repeat(width - 2) + '┤');
      result.push(panelLine(` ${styles.dim}${footer}${styles.reset}`, width));
      const shown = inputWindow(view, mainWidth).text;
      result.push(panelLine(` ${view.approval ? styles.warn : styles.accent}${prompt}${styles.reset}${shown}`, width));
      result.push('╰' + '─'.repeat(width - 2) + '╯');
    }
    return result.map((line) => {
      const raw = plain(line);
      const visibleWidth = displayWidth(raw);
      return visibleWidth > width ? fit(raw, width) : line;
    });
  } finally {
    Object.assign(state, previous);
  }
}

function send(value) {
  if (state.worker?.stdin.writable) state.worker.stdin.write(JSON.stringify(value) + '\n');
}

function append(value) {
  state.lines.push(String(value).replace(/\r/gu, ''));
  if (state.lines.length > 2000) state.lines = state.lines.slice(-2000);
  render();
}

function render() {
  if (!state.running) return;
  const width = process.stdout.columns || 80;
  const height = process.stdout.rows || 24;
  const frame = composeFrame({ width, height, state });
  process.stdout.write(`${ESC}H${ESC}2J${frame.join('\r\n')}`);
  if (width >= 40 && height >= 10) {
    const sideWidth = width >= 88 ? Math.min(30, Math.max(24, Math.floor(width / 4))) : 0;
    const innerWidth = sideWidth ? width - sideWidth - 3 : width - 2;
    process.stdout.write(`${ESC}${height - 1};${inputWindow(state, innerWidth).column}H`);
  }
}

function finish() {
  if (!state.running) return;
  state.running = false;
  send({ type: 'shutdown' });
  if (process.stdin.isTTY) process.stdin.setRawMode(false);
  process.stdout.write(`${ESC}?25h${ESC}?1049l`);
  process.exit(0);
}

function command(value) {
  const [name, ...args] = value.split(/\s+/u);
  if (name === '/quit' || name === '/exit') {
    if (state.busy) append('Everquill is still working. Press Ctrl+C to cancel first.');
    else finish();
  } else if (name === '/help') {
    append('/mode ask|proposal|iterate   /session new|continue   /clear   /quit');
    append('Enter sends · Up/Down recall input · PgUp/PgDn scroll · Ctrl+C cancels.');
  } else if (name === '/mode') {
    if (args.length !== 1 || !['ask', 'proposal', 'iterate'].includes(args[0])) append('Choose: /mode ask, /mode proposal, or /mode iterate');
    else { state.mode = args[0]; append(`Mode: ${state.mode}`); }
  } else if (name === '/session') {
    if (args.length === 1 && args[0] === 'new') {
      append('Creating Muse session...');
      state.busy = true;
      send({ type: 'command', action: 'session_new' });
    } else if (args.length === 1 && args[0] === 'continue') {
      state.busy = true;
      send({ type: 'command', action: 'session_continue' });
    } else if (args.length === 0) append(`Session: ${state.session || 'main chat'}`);
    else append('Choose: /session new or /session continue');
  } else if (name === '/clear') {
    state.lines = [];
    state.scroll = 0;
    render();
  } else append(`Unknown command: ${name}. Type /help.`);
}

function submit() {
  const value = state.input.trim();
  if (state.approval) {
    const approved = isApproved(value);
    send({ type: 'approval_response', approved });
    state.approval = false;
    state.input = '';
    state.cursor = 0;
    append(`${state.approvalPurpose === 'context' ? 'Context send' : 'Proposal'} approval: ${approved ? 'yes' : 'no'}`);
    state.approvalPurpose = '';
    return;
  }
  if (state.busy) { append('Everquill is still working. Wait or press Ctrl+C to cancel.'); return; }
  state.input = '';
  state.cursor = 0;
  state.historyIndex = null;
  if (!value) { render(); return; }
  if (value.startsWith('/')) { command(value); return; }
  state.history.push(value);
  append(`you> ${value}`);
  state.busy = true;
  send({ type: 'command', action: 'request', prompt: value, mode: state.mode, session_id: state.session });
}

function handleKey(text, key) {
  if (key.ctrl && key.name === 'c') {
    if (state.approval) {
      send({ type: 'approval_response', approved: false });
      state.approval = false;
      append('Approval cancelled.');
    } else if (state.busy) {
      send({ type: 'cancel' });
      append('Cancelling after the current Muse response...');
    } else finish();
    return;
  }
  if (key.name === 'return' || key.name === 'enter') submit();
  else if (key.name === 'escape') {
    if (state.approval) {
      send({ type: 'approval_response', approved: false });
      state.approval = false;
      append('Approval cancelled.');
    } else if (!state.busy) finish();
  } else if (key.name === 'backspace') {
    if (state.cursor > 0) {
      const chars = Array.from(state.input);
      chars.splice(state.cursor - 1, 1);
      state.input = chars.join(''); state.cursor -= 1; render();
    }
  } else if (key.name === 'delete') {
    const chars = Array.from(state.input); chars.splice(state.cursor, 1); state.input = chars.join(''); render();
  } else if (key.name === 'left') { state.cursor = Math.max(0, state.cursor - 1); render(); }
  else if (key.name === 'right') { state.cursor = Math.min(Array.from(state.input).length, state.cursor + 1); render(); }
  else if (key.name === 'home') { state.cursor = 0; render(); }
  else if (key.name === 'end') { state.cursor = Array.from(state.input).length; render(); }
  else if (key.name === 'up' && state.history.length && !state.approval) {
    if (state.historyIndex === null) state.historyIndex = state.history.length;
    state.historyIndex = Math.max(0, state.historyIndex - 1);
    state.input = state.history[state.historyIndex]; state.cursor = Array.from(state.input).length; render();
  } else if (key.name === 'down' && state.historyIndex !== null && !state.approval) {
    state.historyIndex = Math.min(state.history.length, state.historyIndex + 1);
    state.input = state.history[state.historyIndex] || ''; state.cursor = Array.from(state.input).length; render();
  } else if (key.name === 'pageup') { state.scroll += 5; render(); }
  else if (key.name === 'pagedown') { state.scroll = Math.max(0, state.scroll - 5); render(); }
  else if (text && !key.ctrl && !key.meta && text.length && text >= ' ') {
    const chars = Array.from(state.input); chars.splice(state.cursor, 0, text);
    state.input = chars.join(''); state.cursor += Array.from(text).length; render();
  }
}

export function readArgs(args) {
  const parsed = { mode: 'ask', session: '', newSession: false };
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--mode') parsed.mode = args[++i] || 'ask';
    else if (args[i] === '--session') parsed.session = args[++i] || '';
    else if (args[i] === '--new-session') parsed.newSession = true;
  }
  return parsed;
}

function run() {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    process.stderr.write('[everquill] Node TUI requires an interactive terminal.\n');
    process.exitCode = 2;
    return;
  }
  if (!process.env.EVERQUILL_PYTHON) {
    process.stderr.write('[everquill] Python runtime path was not provided.\n');
    process.exitCode = 2;
    return;
  }
  Object.assign(state, readArgs(process.argv.slice(2)));
  state.worker = spawn(process.env.EVERQUILL_PYTHON, ['-m', 'everquill.worker'], {
    cwd: state.root,
    env: process.env,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const events = createInterface({ input: state.worker.stdout });
  events.on('line', (line) => {
    let event;
    try { event = JSON.parse(line); } catch { append(`Invalid worker response: ${line}`); return; }
    if (event.type === 'line') append(event.text || '');
    else if (event.type === 'approval') { state.approval = true; state.approvalPurpose = event.purpose || 'proposal'; state.input = ''; state.cursor = 0; render(); }
    else if (event.type === 'session') {
      state.session = event.session_id || '';
      append(event.pending ? 'New session pending until context approval.' :
        state.session ? `Session: ${state.session}` : 'No saved session for this project. Use /session new.');
    } else if (event.type === 'result') append(`Result: ${event.status}`);
    else if (event.type === 'error') append(`Error: ${event.message}`);
    else if (event.type === 'done') { state.busy = false; state.approval = false; state.approvalPurpose = ''; render(); }
  });
  if (state.newSession) {
    state.busy = true;
    send({ type: 'command', action: 'session_new' });
  }
  state.worker.stderr.on('data', () => {});
  state.worker.on('close', (code) => {
    if (code && state.running) append(`Worker exited with code ${code}. Check session and connection.`);
  });
  emitKeypressEvents(process.stdin);
  process.stdin.setRawMode(true);
  process.stdin.setEncoding('utf8');
  process.stdin.on('keypress', handleKey);
  process.stdout.on('resize', render);
  process.stdout.write(`${ESC}?1049h${ESC}?25h`);
  render();
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) run();
