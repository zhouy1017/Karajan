import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach, expect, it, vi} from 'vitest';
import {readFileSync} from 'node:fs';
import {App} from '../../src/App';

afterEach(() => {cleanup();vi.unstubAllGlobals();});

it('retains an unresolved publication when navigating to resources and back', async () => {
  const configuration = JSON.parse(readFileSync('../examples/projects/offline-configuration.json','utf8'));
  const project = {id:'project-navigation',name:'Review project',revision:2,repository:{root:'/fixture',base_ref:'main',base_sha:'a'.repeat(40)},target_branch:'main',configuration:{status:'draft',revision:1,dispatch_eligible:false}};
  vi.stubGlobal('fetch',vi.fn(async (path:string, options?:RequestInit) => {
    if(path==='/v1/session')return Response.json({csrf_token:'session-review'});
    if(path==='/v1/projects')return Response.json({items:[project]});
    if(path==='/v1/resources')return Response.json({schema_version:'karajan.resources.view.v1',accounts:[],observed_at:1000,live_qualification:'not_run',activation_allowed:false});
    if(path.endsWith('/configuration'))return Response.json({project_revision:2,configuration_revision:1,configuration});
    if(path.endsWith('/preview'))return Response.json({preview_id:'preview-navigation',project_revision:2,can_save_draft:true,can_publish:true,issues:[],compile_issues:[],warnings:[],waiting_reasons:[],rulebook_sha256:'a'.repeat(64)});
    if(path.endsWith('/publish') && options?.method==='POST')throw new TypeError('response lost');
    return Response.json({items:[]});
  }));
  render(<App/>);
  await userEvent.click(await screen.findByRole('button',{name:'调度规则'}));
  await screen.findByLabelText('版本说明');
  fireEvent.change(screen.getByLabelText('版本说明'),{target:{value:'Unresolved owner edit'}});
  fireEvent.change(screen.getByLabelText('编辑版本号'),{target:{value:'2'}});
  await userEvent.click(screen.getByRole('button',{name:'预览规则变更'}));
  await userEvent.click(await screen.findByRole('button',{name:'确认发布此版本'}));
  await screen.findByRole('button',{name:'重试原发布请求'});
  await userEvent.click(screen.getByRole('button',{name:'资源与配额'}));
  await userEvent.click(screen.getByRole('button',{name:'项目工作台'}));
  await screen.findByLabelText('版本说明');
  expect((screen.getByLabelText('版本说明') as HTMLTextAreaElement).value).toBe('Unresolved owner edit');
  expect(screen.getByRole('button',{name:'重试原发布请求'})).toBeTruthy();
});
