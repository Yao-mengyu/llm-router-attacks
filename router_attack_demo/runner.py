"""Run the normal route and three controlled router attacks."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import tempfile
from pathlib import Path

from attacks import ATTACKS
from .backends import BackendError
from .routing import select_question
from .util import model_messages
from .workloads import evaluate

TRACE_SCHEMA = 'router-attack-demo-trace-v1'


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.router-trace-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_experiment(cases, model_pool, scenarios, out: Path, mode: str,
                   backend_info: dict, model_claim='approved', *, routing_policy):
    if out.exists():
        raise ValueError('Output already exists; choose a new --out path.')
    if model_claim not in {'approved', 'actual'}:
        raise ValueError('Model claim must be approved or actual.')
    decisions = {case['id']: select_question(case['prompt'], routing_policy) for case in cases}
    for case in cases:
        route = decisions[case['id']]
        if route['selected_model'] not in model_pool:
            raise ValueError('Selected model is absent from the execution pool.')
        if 'model_selection' in scenarios and case['kind'] == 'quality':
            if not route['attack_model'] or route['attack_model'] not in model_pool:
                raise ValueError('Model-selection attack needs a model outside this question\'s eligible set.')
    payload = {'schema': TRACE_SCHEMA, 'provenance': {
        'mode': mode, 'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'backend': backend_info,
    }, 'cases': copy.deepcopy(cases), 'routing_policy': copy.deepcopy(routing_policy),
        'runs': [], 'errors': [], 'model_calls_attempted': 0}
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError('Output already exists; choose a new --out path.') from None
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    for case in cases:
        route = decisions[case['id']]
        normal_model = route['selected_model']
        scenarios_for_case = ['honest'] + [name for name in scenarios if ATTACKS[name].TARGET == case['kind']]
        normal = None
        for scenario in scenarios_for_case:
            record_id = f"{case['id']}/{scenario}"
            clients = [{'role': 'user', 'content': case['prompt']}]
            plan = {'messages': model_messages(clients), 'backend': 'selected',
                    'effect': 'Forward the original request to the selected model.'}
            if scenario in ATTACKS:
                ATTACKS[scenario].mutate(plan, case)
            actual_model = route['attack_model'] if plan['backend'] == 'alternate' else normal_model
            reused = None
            if plan.get('reuse') == 'honest':
                if normal is None:
                    payload['errors'].append({'id': record_id, 'error': 'Normal execution failed; response-tampering branch skipped without another call.'})
                    write_json(out, payload)
                    continue
                generation = copy.deepcopy(normal['generation'])
                plan['messages'] = copy.deepcopy(normal['backend_messages'])
                reused = normal['id']
            else:
                payload['model_calls_attempted'] += 1
                write_json(out, payload)
                try:
                    generation = model_pool[actual_model].generate(plan['messages'])
                except BackendError as exc:
                    payload['errors'].append({'id': record_id, 'error': str(exc)})
                    print(f'{record_id}: {exc}', flush=True)
                    write_json(out, payload)
                    continue
            text = generation['text']
            delivered = ATTACKS[scenario].deliver(text) if scenario == 'response_tampering' else text
            claimed = normal_model if scenario == 'model_selection' and model_claim == 'approved' else actual_model
            effect = plan['effect']
            if scenario == 'model_selection' and model_claim == 'approved':
                effect += " The router labels this as the selected model's answer."
            record = {'id': record_id, 'scenario': scenario, 'case_id': case['id'],
                'case_title': case['title'], 'case_kind': case['kind'],
                'client_messages': clients, 'backend_messages': copy.deepcopy(plan['messages']),
                'returned_model': claimed, 'actual_model': actual_model,
                'backend_response': text, 'delivered_response': delivered,
                'generation': generation, 'routing_decision': copy.deepcopy(route),
                'eligible_models': copy.deepcopy(route['eligible_models']), 'effect': effect,
                'reused_execution': reused, 'evaluation': evaluate(case, delivered),
                'backend_evaluation': evaluate(case, text)}
            if scenario == 'honest':
                normal = record
            payload['runs'].append(record)
            print(f'{record_id:48} completed | actual model: {actual_model}', flush=True)
            write_json(out, payload)
    return payload
