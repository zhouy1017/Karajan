import copy
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / '.cache/dispatch-planning-parser'
sys.path.insert(0, str(CANDIDATE / 'backend'))
spec = importlib.util.spec_from_file_location('author_examples', CANDIDATE / 'tests/runs/test_planning_output.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from karajan.runs.planning_output import MAX_OUTPUT_BYTES, PlanningOutputError, parse_planning_output

observed = []
def reject(label, content, expected, version='v2'):
    try:
        parse_planning_output(content, version=version)
    except PlanningOutputError as error:
        assert error.code == expected, (label, error.code)
        assert str(error) == expected and error.args == (expected,)
        observed.append({'case': label, 'result': 'passed', 'reason': error.code})
    else:
        raise AssertionError(label)

plan = module.document(v2=True)
plan['summary'] = ' preserved text \" [ { \\ 中文 '
plan['authorization']['currency_limits'] = {'USD': '0001.230000'}
body = json.dumps(plan, ensure_ascii=False, separators=(',', ':'))
assert parse_planning_output(body, version='v2').model_dump() == plan
observed.append({'case': 'preserve text, escaped brackets and original amount', 'result': 'passed'})
wire = body.encode('utf-8')
at_limit = wire + b' ' * (MAX_OUTPUT_BYTES - len(wire))
assert parse_planning_output(at_limit, version='v2').model_dump() == plan
observed.append({'case': 'valid multibyte plan at exact byte limit', 'result': 'passed'})
reject('byte limit +1', at_limit + b' ', 'PLANNING_OUTPUT_LIMIT_EXCEEDED')
for depth in (16, 17):
    nested = '[' * depth + '0' + ']' * depth
    reject(f'valid JSON container depth {depth}', nested,
           'PLANNING_OUTPUT_SCHEMA_INVALID' if depth == 16 else 'PLANNING_OUTPUT_LIMIT_EXCEEDED')
for target in ('revision', 'required', 'context_tokens', 'duration_seconds'):
    for value in ('1', 1.0, None):
        changed = copy.deepcopy(plan)
        changed['tasks'][0][target] = value
        reject(f'strict task {target} {value!r}', json.dumps(changed), 'PLANNING_OUTPUT_SCHEMA_INVALID')
for value in (1, True, 1.0, 'NaN', 'Infinity'):
    changed = copy.deepcopy(plan)
    changed['authorization']['currency_limits']['USD'] = value
    reject(f'strict amount {value!r}', json.dumps(changed), 'PLANNING_OUTPUT_SCHEMA_INVALID')
for target in ('run_id', 'intent_id', 'term', 'principal', 'receipt', 'admitted', 'authority', 'source', 'digest', 'approval'):
    for location in ('root', 'authorization', 'task'):
        changed = copy.deepcopy(plan)
        destination = changed if location == 'root' else changed['authorization'] if location == 'authorization' else changed['tasks'][0]
        destination[target] = 'SENSITIVE_MARKER'
        reject(f'identity {target} at {location}', json.dumps(changed), 'PLANNING_OUTPUT_SCHEMA_INVALID')
duplicate = body.replace('"USD":"0001.230000"', '"USD":"0001.230000","\\u0055SD":"2"')
reject('nested escaped duplicate currency', duplicate, 'PLANNING_OUTPUT_JSON_INVALID')
reject('decoded surrogate in nested key', body[:-1] + ',"\\ud800":0}', 'PLANNING_OUTPUT_INPUT_INVALID')
reject('escaped surrogate nested value', body.replace('"worker"', '"\\ud800"'), 'PLANNING_OUTPUT_INPUT_INVALID')
reject('huge JSON integer stable rejection', body.replace('"revision":1', '"revision":' + '9' * 5000), 'PLANNING_OUTPUT_JSON_INVALID')
print(json.dumps({'candidate': '447319708801927c0063700d1d294b6c4b2bebfe', 'cases': observed, 'total': len(observed)}, ensure_ascii=False, indent=2))
