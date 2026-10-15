import assert from 'node:assert/strict';
import test from 'node:test';
import { composeFrame, displayWidth, isApproved, inputWindow, readArgs } from '../src/everquill/tui.mjs';

const view = {
  root: 'C:/workspace/demo-project',
  mode: 'proposal',
  session: 'session-123456789',
  lines: ['Inspect the login handler', 'Muse found two files to change.'],
  input: '',
  cursor: 0,
  busy: false,
  approval: true,
  scroll: 0,
};

test('wide layout uses continuous Unicode borders and a workspace panel', () => {
  const frame = composeFrame({ width: 100, height: 24, state: view });
  assert.ok(frame.some((line) => line.includes('WORKSPACE')));
  assert.ok(frame.some((line) => line.includes('Apply proposal?')));
  assert.ok(frame.some((line) => line.includes('Inspect the login handler')));
  assert.equal(frame[0].startsWith('╭'), true);
  assert.equal(frame.at(-1).startsWith('╰'), true);
  assert.equal(frame.some((line) => /-{3,}/u.test(line)), false);
  assert.ok(frame.every((line) => displayWidth(line) === 100));
  assert.equal(frame[0].indexOf('┬'), frame[2].indexOf('│', 1));
  assert.equal(frame[0].indexOf('┬'), frame.at(-1).indexOf('┴'));
});

test('narrow layout retains chat controls without a sidebar', () => {
  const frame = composeFrame({ width: 60, height: 18, state: view });
  assert.ok(frame.some((line) => line.includes('mode: proposal')));
  assert.ok(frame.some((line) => line.includes('everquill>') || line.includes('Apply proposal?')));
  assert.equal(frame.some((line) => line.includes('WORKSPACE')), false);
  assert.ok(frame.every((line) => displayWidth(line) === 60));
});

test('context confirmation clearly asks to send rather than apply', () => {
  const frame = composeFrame({ width: 100, height: 24, state: {
    ...view, approval: true, approvalPurpose: 'context',
  } });
  assert.ok(frame.some((line) => line.includes('Send to Meta? [y/N]')));
  assert.equal(frame.some((line) => line.includes('Apply proposal?')), false);
});

test('new session flag remains pending before the first TUI request', () => {
  assert.deepEqual(readArgs(['--mode', 'ask', '--new-session']),
    { mode: 'ask', session: '', newSession: true });
});

test('short terminals show a resize message instead of a malformed frame', () => {
  const frame = composeFrame({ width: 30, height: 8, state: view });
  assert.ok(frame[0].includes('needs 40 columns'));
});

test('display width handles wide glyphs and combining marks', () => {
  assert.equal(displayWidth('a界e\u0301'), 4);
});

test('proposal approval accepts only lowercase y or yes', () => {
  assert.equal(isApproved('y'), true);
  assert.equal(isApproved('yes'), true);
  for (const value of ['Y', 'YES', '', 'no', 'maybe']) assert.equal(isApproved(value), false);
});

test('long input keeps the cursor inside the conversation panel', () => {
  const window = inputWindow({ input: 'x'.repeat(100) + 'END', cursor: 103 }, 72);
  assert.ok(window.text.endsWith('END'));
  assert.ok(window.column <= 72);
});

test('code indentation survives rendering', () => {
  const frame = composeFrame({ width: 100, height: 24, state: {
    ...view, lines: ['    return value'],
  } });
  assert.ok(frame.some((line) => line.includes('│     return value')));
});
