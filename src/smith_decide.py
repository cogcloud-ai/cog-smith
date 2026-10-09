"""Prepare digest-bound human decisions without copying hashes by hand."""
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import op_runner
import op_spec


def prepare(run_dir, *, approve=(), reject=(), reject_rest=False,
            defer_rest=False, verdict=None, reason=None, by, step=None):
    run_dir = Path(run_dir).resolve()
    track = json.loads((run_dir / 'track.json').read_text())
    waiting = [record['id'] for record in track.get('steps', [])
               if record['status'] == 'awaiting-decision']
    if track.get('status') != 'paused' or len(waiting) != 1:
        raise op_spec.OpSpecError('This run is not waiting on one human Gate.')
    if step is not None and step != waiting[0]:
        raise op_spec.OpSpecError(f'The pending step is {waiting[0]!r}, not {step!r}.')
    pending = json.loads((run_dir / 'pending' / f'{waiting[0]}.json').read_text())
    if pending['run_id'] != track['run_id'] or pending['step'] != waiting[0]:
        raise op_spec.OpSpecError('Pending decision does not belong to this run and step.')
    if op_runner.canonical_sha256(pending['payload']) != pending['payload_sha256']:
        raise op_spec.OpSpecError('Pending payload changed; inspect the run before deciding.')
    document = {'schema': op_runner.DECISION_SCHEMA, 'run_id': pending['run_id'],
                'step': pending['step'], 'payload_sha256': pending['payload_sha256'],
                'decided_by': by, 'decided_at': datetime.now(timezone.utc).isoformat()}
    if pending.get('decides') == op_spec.DECIDES_ARTIFACT:
        if approve or reject or reject_rest or defer_rest or verdict is None:
            raise op_spec.OpSpecError('Artifact Gates need --accept or --reject-artifact, not change-id choices.')
        document.update(artifact_sha256=pending['artifact_sha256'], verdict=verdict)
        if reason is not None:
            document['reason'] = reason
    else:
        if verdict is not None:
            raise op_spec.OpSpecError('This Gate asks about changes; use --approve/--reject and an explicit remainder policy.')
        if reject_rest and defer_rest:
            raise op_spec.OpSpecError('Choose only one remainder policy.')
        ids = [change['change_id'] for change in op_runner.pending_changes(pending['payload'], pending['step'])]
        selections = list(approve) + list(reject)
        if len(selections) != len(set(selections)):
            raise op_spec.OpSpecError('A change cannot be selected twice or both approved and rejected.')
        unknown = set(selections) - set(ids)
        if unknown:
            raise op_spec.OpSpecError('Unknown change ids: ' + ', '.join(sorted(unknown)))
        remaining = set(ids) - set(selections)
        if remaining and not (reject_rest or defer_rest):
            raise op_spec.OpSpecError('Unmentioned changes require --reject-rest or --defer-rest; no implicit decisions.')
        document['decisions'] = [
            {'change_id': cid, 'verdict': 'approve' if cid in approve else 'reject' if cid in reject or reject_rest else 'defer'}
            for cid in ids]
    # Validate with the same shared semantics that resume will apply.
    op_runner.apply_decision(pending, document)
    return document


def save(run_dir, document, output=None):
    path = Path(output).resolve() if output else Path(run_dir).resolve() / 'decision-inputs' / f'{uuid.uuid4().hex}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(document, stream, indent=2)
        stream.write('\n')
    return path
