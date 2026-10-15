import assert from 'node:assert/strict';
import test from 'node:test';
import { composeFrame } from '../src/everquill/tui.mjs';

const view = { root: 'demo', mode: 'ask', lines: [], input: '', cursor: 0,
  approval: true, scroll: 0, busy: false };

test('Meta context approval does not identify Muse', () => {
  const frame = composeFrame({ width: 100, height: 24, state: {
    ...view, approvalPurpose: 'context', provider: 'meta',
  } }).join('\n');
  assert.ok(frame.includes('Send to Meta? [y/N]'));
  assert.equal(frame.includes('Send to Muse.ai?'), false);
});

test('recovery execution approval is distinct from result sending and writes', () => {
  const frame = composeFrame({ width: 100, height: 24, state: {
    ...view, approvalPurpose: 'tool_execution',
  } }).join('\n');
  assert.ok(frame.includes('Execute read-only recovery tools? [y/N]'));
  assert.equal(frame.includes('Apply proposal?'), false);
  assert.equal(frame.includes('Send to'), false);
});

test('tool-result approval uses the selected provider', () => {
  const frame = composeFrame({ width: 100, height: 24, state: {
    ...view, approvalPurpose: 'tool_results', provider: 'muse',
  } }).join('\n');
  assert.ok(frame.includes('Send tool results to Muse.ai? [y/N]'));
  assert.equal(frame.includes('Apply proposal?'), false);
});
