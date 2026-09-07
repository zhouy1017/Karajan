from pathlib import Path, PurePosixPath
import hashlib,json,subprocess,xml.etree.ElementTree as ET
root=Path.cwd(); archive=root/'examples/go-readonly-reviewer-qualification-20260907'; out=root/'.cache/reviewer-publication-independent'
def sha(data): return hashlib.sha256(data).hexdigest()
def git(*args): return subprocess.check_output(['git',*args],cwd=root)
inputs=['docs/implementation/go-readonly-reviewer-qualification.md','examples/go-readonly-reviewer-qualification-20260907/README.md','examples/go-readonly-reviewer-qualification-20260907/publication-map.json','examples/go-readonly-reviewer-qualification-20260907/source-to-code.json']
result={'scope':'Independent documentation/archive Standards and Spec only; not Suite code review or new execution','head':git('rev-parse','HEAD').decode().strip(),'inputs':{p:sha((root/p).read_bytes()) for p in inputs},'map':{},'source':{},'xml':{}}
entries=json.loads((archive/'publication-map.json').read_text(encoding='utf-8'))
seen=set(); errors=[]
for row in entries:
    rel=PurePosixPath(row['published']); original=(root/row['original']).resolve(); published=(archive/Path(*rel.parts)).resolve()
    if rel.is_absolute() or '..' in rel.parts or not published.is_relative_to(archive.resolve()) or not original.is_relative_to((root/'.cache').resolve()):
        errors.append({'published':row['published'],'reason':'path-outside-boundary'}); continue
    if row['published'] in seen: errors.append({'published':row['published'],'reason':'duplicate-path'})
    seen.add(row['published'])
    if original.is_symlink() or published.is_symlink() or original.suffix.lower() in {'.key','.sqlite','.db','.sqlite3'}:
        errors.append({'published':row['published'],'reason':'unsafe-source'}); continue
    a,b=original.read_bytes(),published.read_bytes()
    if a!=b or sha(a)!=row['sha256'] or len(a)!=row['bytes']:
        errors.append({'published':row['published'],'reason':'bytes-digest-or-size'})
result['map']={'count':len(entries),'unique_published_paths':len(seen),'all_original_and_published_bytes_sha_size_match':not errors,'errors':errors}
manifest=json.loads((archive/'source-to-code.json').read_text(encoding='utf-8'))
p_source=json.loads((archive/'suite/source-map.json').read_text(encoding='utf-8'))
code=git('rev-parse',manifest['code_commit']).decode().strip(); mods=manifest['modules']; changes=[]; invalid=[]
for m in mods:
    path=m['path']; assert path.startswith('backend/') and path.endswith('.py') and '..' not in PurePosixPath(path).parts
    blob=git('show',code+':'+path); local=(root/path).read_bytes(); raw_equal=sha(blob)==m['local_sha256']
    normalized=(local.replace(b'\r\n',b'\n')==blob and sha(local)==m['local_sha256'])
    if sha(blob)!=m['git_blob_sha256'] or p_source[path]!=m['local_sha256'] or raw_equal!=m['equal'] or bool(not raw_equal and normalized)!=m['only_line_endings']:
        invalid.append(path)
    if not raw_equal: changes.append({'path':path,'local_sha256':sha(local),'git_sha256':sha(blob),'only_crlf_to_lf':normalized,'local_crlf_count':local.count(b'\r\n'),'git_crlf_count':blob.count(b'\r\n')})
result['source']={'code_commit':code,'module_count':len(mods),'P_source_count':len(p_source),'same_path_set':set(p_source)=={m['path'] for m in mods},'non_exact':changes,'invalid':invalid}
merge=git('rev-parse','a453aa9080a084f935985627f7608fa8b500fa6f').decode().strip()
result['integration']={'commit':merge,'parents':git('show','-s','--format=%P',merge).decode().strip().split(),'code_is_ancestor':subprocess.run(['git','merge-base','--is-ancestor',code,merge],cwd=root).returncode==0,'behavior_paths_changed':git('diff','--name-only',code,merge,'--','backend','tests','frontend').decode().splitlines(),'trees':{p:{'code':git('rev-parse',code+':'+p).decode().strip(),'integration':git('rev-parse',merge+':'+p).decode().strip()} for p in ['backend','tests','frontend']}}
for name in ['root/root-current-guard.xml','suite/linux.xml','reviewer-root-independent/final-six.xml','reviewer-observer-author/final-negative-linux.xml','root/root-final-combination-windows-pass.xml','root/root-final-factory-assets.xml']:
    doc=ET.parse(archive/name).getroot(); suites=list(doc) if doc.tag=='testsuites' else [doc]
    result['xml'][name]={k:sum(float(s.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped','time']}
result['journal_relay_xml']={}
for path in (archive/'journal-relay').rglob('*.xml'):
    doc=ET.parse(path).getroot(); suites=list(doc) if doc.tag=='testsuites' else [doc]
    result['journal_relay_xml'][str(path.relative_to(archive)).replace('\\','/')]={k:sum(float(s.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped','time']}
result['root_log_paths']=[str(p.relative_to(archive)).replace('\\','/') for p in sorted((archive/'root').iterdir()) if p.is_file()]
(out/'checks.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8',newline='\n')
print(json.dumps(result))
