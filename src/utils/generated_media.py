"""Project-bound scene, generation and budget records with durable retry semantics."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time
import uuid

from src.utils.generated_assets import WorkflowError, require, ingest, digest, elevenlabs_result

ACTIONS = ('capabilities', 'init', 'scene_put', 'records', 'reserve', 'generation_update',
           'budget', 'budget_set', 'estimate', 'attach', 'download', 'import_asset', 'plan_storyboard',
           'build_storyboard', 'plan_replace', 'replace_shot', 'review', 'commit_review')


def money(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise WorkflowError('invalid_cost', 'Credits must be a non-negative finite decimal.') from exc
    require(number.is_finite() and number >= 0, 'invalid_cost', 'Credits must be non-negative and finite.')
    return str(number)


def identifier(value, name):
    require(isinstance(value, str) and 0 < len(value.strip()) <= 200, 'invalid_params', f'{name} is required (1..200 characters).')
    return value.strip()


def budget(state):
    actual = sum((Decimal(g['actual_credits']) for g in state['generations'].values()
                  if g.get('actual_credits') is not None), Decimal(0))
    reserved = sum((Decimal(g['estimated_credits']) for g in state['generations'].values()
                    if g.get('actual_credits') is None), Decimal(0))
    limit = Decimal(state['budget_credits'])
    return {'unit': 'ElevenLabs credits', 'budget_credits': str(limit), 'actual_credits': str(actual),
            'reserved_credits': str(reserved), 'remaining_credits': str(limit - actual - reserved),
            'over_budget': actual + reserved > limit,
            'note': 'Local project accounting, not provider account balance. Unknown actual charges retain their reservation.'}


class Store:
    def __init__(self, workspace, create=False):
        require(workspace and Path(workspace).expanduser().is_absolute(), 'invalid_workspace', 'workspace must be an absolute project directory.')
        self.root = Path(workspace).expanduser().resolve() / '.generated-media'
        require(create or (self.root / 'records.sqlite3').is_file(), 'not_initialized', 'Call init for this workspace first.')
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.root / 'records.sqlite3', timeout=120)
        self.conn.execute('PRAGMA busy_timeout=120000')
        self.conn.execute('CREATE TABLE IF NOT EXISTS workflow (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')

    def save(self, state):
        self.conn.execute('INSERT OR REPLACE INTO workflow VALUES(1, ?)', (json.dumps(state, allow_nan=False),))

    @contextmanager
    def transaction(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('SELECT data FROM workflow WHERE id=1').fetchone()
            state = json.loads(row[0]) if row else None
            yield state
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self.conn.close()


def project(resolve, state=None):
    require(resolve is not None, 'resolve_unavailable', 'Resolve connection unavailable; on Free 20.3.2 run Workspace > Scripts > resolve_bridge.')
    value = resolve.GetProjectManager().GetCurrentProject()
    require(value is not None, 'no_project', 'Open a Resolve project first.')
    if state:
        require(str(value.GetUniqueId()) == state['project_id'], 'wrong_project',
                f'This workspace belongs to {state["project_name"]}; open that project before editing.')
    return value


def generation(state, key):
    require(key in state['generations'], 'unknown_generation', 'Unknown local generation record ID.')
    return state['generations'][key]


def verified_asset(g):
    asset = g.get('asset')
    require(asset, 'asset_not_ready', 'Download or attach the existing generation first; do not generate again.')
    path = Path(asset['local_path'])
    require(path.is_file() and digest(path) == asset['sha256'], 'asset_changed', 'Managed asset is missing or changed; retry download/attach.')
    return asset


def reserve(state, p):
    scene_id = p['scene_id']
    require(scene_id in state['scenes'], 'unknown_scene', 'Create the scene first.')
    request_key = identifier(p['request_key'], 'request_key')
    estimate = money(p['estimated_credits'])
    scene = state['scenes'][scene_id]
    spec = {'scene_id': scene_id, 'provider': p.get('provider', 'elevenlabs'),
            'model': identifier(p['model'], 'model'), 'prompt': p.get('prompt', scene['prompt']),
            'reference_images': p.get('reference_images', scene.get('reference_images', [])),
            'settings': p.get('settings', {}), 'estimated_credits': estimate,
            'price_source': identifier(p['price_source'], 'price_source')}
    for g in state['generations'].values():
        if g['request_key'] == request_key:
            require(all(g[k] == v for k, v in spec.items()), 'request_key_conflict', 'Existing request_key has different generation parameters.')
            return {'generation': g, 'may_submit_once': False, 'next_step': 'Resume this record; reconcile any ambiguous provider request. Never submit again on retry.'}
    if p.get('regenerate_from'):
        prior = generation(state, p['regenerate_from'])
        require(prior['scene_id'] == scene_id, 'wrong_scene', 'regenerate_from must belong to this scene.')
    else:
        matching = next((g for g in reversed(list(state['generations'].values()))
                         if all(g[k] == v for k, v in spec.items())), None)
        if matching:
            return {'generation': matching, 'may_submit_once': False,
                    'next_step': 'Matching generation already exists. Resume it. An intentional new take requires regenerate_from=its record ID.'}
    require(Decimal(estimate) <= Decimal(budget(state)['remaining_credits']), 'budget_exceeded', 'Estimate exceeds remaining project credit budget.')
    key = uuid.uuid4().hex
    g = dict(spec, id=key, request_key=request_key, created_at=time.time(), status='reserved', actual_credits=None,
             generation_id=None, regenerate_from=p.get('regenerate_from'), version=1 + sum(v['scene_id'] == scene_id for v in state['generations'].values()))
    state['generations'][key] = g
    return {'generation': g, 'may_submit_once': True,
            'next_step': 'Reservation only. If user authorized spending, submit once externally and record its generation ID immediately. An uncertain submission must be reconciled, not retried.'}


def update_generation(state, p):
    g = generation(state, p['record_id'])
    gid = p.get('generation_id', g['generation_id'])
    if gid:
        identifier(gid, 'generation_id')
        require(not g['generation_id'] or g['generation_id'] == gid, 'generation_id_conflict', 'A record cannot be rebound to a different paid generation.')
        require(not any(v['id'] != g['id'] and v['provider'] == g['provider'] and v['generation_id'] == gid
                        for v in state['generations'].values()), 'duplicate_generation', 'This provider generation already has a record; reuse it.')
    status = p.get('status', g['status'])
    allowed = {'reserved': {'reserved', 'submitted', 'completed', 'failed'},
               'submitted': {'submitted', 'completed', 'failed'}, 'completed': {'completed'}, 'failed': {'failed'}}
    require(status in allowed[g['status']], 'invalid_transition', 'Generation status cannot move backwards or restart a failed job.')
    require(status not in ('submitted', 'completed') or gid, 'missing_generation_id', 'Record the provider generation ID first.')
    g.update(generation_id=gid, status=status)
    if 'actual_credits' in p:
        cost = money(p['actual_credits'])
        require(g['actual_credits'] is None or g['actual_credits'] == cost, 'cost_conflict', 'Actual cost already recorded with another value.')
        g['actual_credits'] = cost
        g['cost_evidence'] = identifier(p['cost_evidence'], 'cost_evidence')
    return {'generation': g, 'budget': budget(state)}


def dispatch(action, params, resolve_provider):
    """The only public boundary; errors are retryable by phase, never by regeneration."""
    p = params or {}
    try:
        require(action in ACTIONS, 'unknown_action', f'Actions: {", ".join(ACTIONS)}')
        from src.utils import generated_resolve as live
        from src.utils import generated_review as review
        if action == 'capabilities':
            return {'success': True, **live.capabilities(resolve_provider())}
        store = Store(p.get('workspace'), create=action == 'init')
        with store.transaction() as state:
            if action == 'init':
                proj = project(resolve_provider())
                if state:
                    require(state['project_id'] == str(proj.GetUniqueId()), 'wrong_project', 'Workspace is already bound to another Resolve project.')
                else:
                    state = {'schema': 1, 'project_id': str(proj.GetUniqueId()), 'project_name': proj.GetName(),
                             'budget_credits': money(p.get('budget_credits', 0)), 'scenes': {}, 'generations': {},
                             'plans': {}, 'reviews': {}}
                result = {'project_id': state['project_id'], 'project_name': state['project_name'], 'budget': budget(state)}
            else:
                require(state is not None, 'not_initialized', 'Call init first.')
                if action == 'records':
                    result = {'records': state}
                elif action == 'estimate':
                    rate, quantity = money(p['unit_credits']), money(p['quantity'])
                    variants = p.get('variants', 1)
                    require(type(variants) is int and 1 <= variants <= 1000, 'invalid_params', 'variants must be 1..1000.')
                    total = Decimal(rate) * Decimal(quantity) * variants
                    result = {'estimated_credits': str(total), 'unit_credits': rate, 'quantity': quantity,
                              'variants': variants, 'price_source': identifier(p['price_source'], 'price_source'),
                              'fits_budget': total <= Decimal(budget(state)['remaining_credits']),
                              'note': 'Uses the supplied current provider quote; model pricing is not hardcoded. Reserve before submission.'}
                elif action == 'budget':
                    result = budget(state)
                elif action == 'budget_set':
                    state['budget_credits'] = money(p['budget_credits'])
                    result = budget(state)
                elif action == 'scene_put':
                    key = identifier(p['scene_id'], 'scene_id')
                    from fractions import Fraction
                    duration = float(Fraction(str(p['duration_seconds'])))
                    require(0 < duration <= 86400, 'invalid_duration', 'Scene duration must be positive and at most one day.')
                    old = state['scenes'].get(key, {})
                    scene = {'id': key, 'prompt': p.get('prompt', ''), 'duration_seconds': duration,
                             'reference_images': p.get('reference_images', []), 'revision': old.get('revision', 0) + 1}
                    require(isinstance(scene['prompt'], str) and isinstance(scene['reference_images'], list), 'invalid_params', 'prompt must be text; reference_images must be a list.')
                    state['scenes'][key] = scene
                    result = {'scene': scene}
                elif action == 'reserve':
                    result = reserve(state, p)
                    if result['may_submit_once']:
                        g = result['generation']
                        refs = g['reference_images']
                        require(isinstance(refs, list), 'invalid_references', 'reference_images must be a list of local image paths.')
                        g['reference_assets'] = []
                        for reference in refs:
                            require(isinstance(reference, str) and Path(reference).is_absolute(),
                                    'invalid_references', 'Reference images must be absolute local paths; download remote references first.')
                            reference_asset = ingest(store.root, local_path=reference)
                            require(reference_asset['kind'] == 'image', 'invalid_references', 'Reference file must be a still image.')
                            g['reference_assets'].append(dict(reference_asset, original_path=reference))
                elif action == 'generation_update':
                    result = update_generation(state, p)
                elif action in ('attach', 'download'):
                    g = generation(state, p['record_id'])
                    require(g['status'] == 'completed', 'generation_not_completed', 'Record a completed generation ID before ingest.')
                    if g.get('asset') and Path(g['asset']['local_path']).is_file() and digest(g['asset']['local_path']) == g['asset']['sha256']:
                        require(not p.get('expected_sha256') or p['expected_sha256'].lower() == g['asset']['sha256'],
                                'checksum_mismatch', 'Expected SHA-256 conflicts with the existing generation asset.')
                        result = {'generation': g, 'reused': True}
                    else:
                        url = p.get('url')
                        if action == 'download' and not url:
                            require(g['provider'] == 'elevenlabs', 'unsupported_provider', 'Supply a completed URL for this provider.')
                            url = elevenlabs_result(g['generation_id'], p['kind'])['content_url']
                        asset = ingest(store.root, local_path=p.get('local_path') if action == 'attach' else None,
                                       url=url if action == 'download' else None, expected_sha256=p.get('expected_sha256'),
                                       max_bytes=p.get('max_bytes', 1024**3))
                        require(not g.get('asset') or g['asset']['sha256'] == asset['sha256'], 'asset_conflict', 'This generation previously produced different bytes; use a new record.')
                        g['asset'] = asset
                        result = {'generation': g, 'reused': False}
                elif action == 'import_asset':
                    proj = project(resolve_provider(), state)
                    g = generation(state, p['record_id'])
                    clip, reused = live.import_asset(proj, verified_asset(g), g['scene_id'])
                    g['resolve_clip_id'] = str(clip.GetUniqueId())
                    result = {'clip_id': g['resolve_clip_id'], 'reused': reused, 'requested_bin': live.bin_path(g['scene_id']),
                              'note': 'Shared content reuses its existing Media Pool item and bin.' if reused else 'Imported into requested bin.'}
                elif action == 'plan_storyboard':
                    result = live.plan_storyboard(state, p)
                elif action == 'build_storyboard':
                    proj = project(resolve_provider(), state)
                    result = live.build_storyboard(proj, state, p, store.root)
                elif action == 'plan_replace':
                    proj = project(resolve_provider(), state)
                    result = live.plan_replace(proj, state, p)
                elif action == 'replace_shot':
                    proj = project(resolve_provider(), state)
                    result = live.replace_shot(proj, state, p)
                elif action == 'review':
                    result = review.prepare(state, p, store.root)
                elif action == 'commit_review':
                    result = review.commit(state, p)
            store.save(state)
            return {'success': True, **result}
    except WorkflowError as exc:
        return {'success': False, 'error': {'code': exc.code, 'message': str(exc)}, 'generation_submitted': False}
    except (KeyError, ValueError, TypeError) as exc:
        return {'success': False, 'error': {'code': 'invalid_params', 'message': str(exc)}, 'generation_submitted': False}
    except Exception as exc:
        # Never echo signed URLs or provider secrets in exception text.
        return {'success': False, 'error': {'code': 'workflow_failed', 'message': type(exc).__name__,
                'remediation': 'Retry the same phase/plan after checking state. Never submit a new paid generation to fix download/import failure.'},
                'generation_submitted': False}
