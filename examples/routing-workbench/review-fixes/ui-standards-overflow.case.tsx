import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {readFileSync} from 'node:fs';
import {RoutingSimulation} from '../../src/RoutingSimulation';

afterEach(() => {cleanup();vi.unstubAllGlobals();});
it('does not turn an overflowing imported cooldown into a null cooldown',async () => {
  const source=readFileSync('../examples/routing-workbench/review-fixes/ui-standards-overflow.input.json','utf8');
  const baseline=JSON.parse(readFileSync('../examples/routing/fixed-input.json','utf8'));
  const sent:RequestInit[]=[];
  vi.stubGlobal('fetch',vi.fn(async (_path:string,options?:RequestInit) => {
    if(options?.method==='POST')sent.push(options);
    return Response.json({reason_code:'TEST_RECEIVER'},{status:422});
  }));
  render(<RoutingSimulation project={{id:'project-1',name:'Review'}} csrf='review-csrf' draft={baseline.policy.rulebook} onSessionExpired={vi.fn()}/>);
  fireEvent.click(screen.getByText('路由模拟 · 使用固定快照演练'));
  const file=new File([source],'overflow.json',{type:'application/json'});
  Object.defineProperty(file,'text',{value:async()=>source});
  await userEvent.upload(screen.getByLabelText('导入模拟快照'),file);
  const button=screen.queryByRole('button',{name:'模拟当前编辑'});
  if(button)await userEvent.click(button);
  expect(sent.map(call=>JSON.parse(String(call.body)).capacity.accounts[0].cooldown_until)).toEqual([]);
  expect(screen.getByRole('alert')).toBeTruthy();
});
