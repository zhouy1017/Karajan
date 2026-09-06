import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach, expect, it, vi} from 'vitest';
import {readFileSync} from 'node:fs';
import {RoutingSimulation} from '../../src/RoutingSimulation';

afterEach(() => {cleanup(); vi.unstubAllGlobals();});

it.each([
  ['9007199254740991', true],
  ['-9007199254740991', true],
  ['9007199254740992', false],
  ['-9007199254740992', false],
] as const)('keeps the nested native setting integer boundary %s explicit', async (literal, accepted) => {
  const baseline = JSON.parse(readFileSync('../examples/routing/fixed-input.json', 'utf8'));
  const source = readFileSync('../examples/routing-workbench/review-fixes/ui-standards-unsafe-integer.input.json', 'utf8')
    .replace('9007199254740993', literal);
  const sent: RequestInit[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_path: string, options?: RequestInit) => {
    if(options?.method === 'POST') sent.push(options);
    return Response.json({reason_code: 'TEST_RECEIVER'}, {status: 422});
  }));
  render(<RoutingSimulation project={{id: 'boundary-project', name: 'Boundary Review'}} csrf='boundary-csrf' draft={baseline.policy.rulebook} onSessionExpired={vi.fn()}/>);
  fireEvent.click(screen.getByText('路由模拟 · 使用固定快照演练'));
  const file = new File([source], 'integer-boundary.json', {type: 'application/json'});
  Object.defineProperty(file, 'text', {value: async () => source});
  await userEvent.upload(screen.getByLabelText('导入模拟快照'), file);
  if(accepted) {
    await userEvent.click(screen.getByRole('button', {name: '模拟当前编辑'}));
    expect(sent).toHaveLength(1);
    expect(JSON.parse(String(sent[0].body)).policy.resources.profiles[0].profile.binding.native_settings.seed).toBe(Number(literal));
    expect(String(sent[0].body)).toContain('"seed":' + literal);
  } else {
    expect(sent).toHaveLength(0);
    expect(screen.getByRole('alert').textContent).toContain('数字超出可靠范围');
    expect(screen.queryByRole('button', {name: '模拟当前编辑'})).toBeNull();
  }
});
