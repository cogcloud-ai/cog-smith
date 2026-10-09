"""Bounded declarative cycles retain evidence and reuse accepted/paid work."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import op_fixtures as fx
from op_fixtures import op_runner, op_spec, op_track
import op_cycle


def cycle_spec():
    design = fx.cog_step('design', gate={'policy': 'human', 'decides': 'artifact', 'artifact': {'kind': 'cog-contract', 'digests': {'contract': {'$sha256': {'$from': 'steps.design.payload.contract'}}}}})
    design['input'] = {'operation': 'design'}
    author = fx.cog_step('author', depends_on=['design'])
    author['input'] = {'operation': 'author', 'contract': {'$from': 'steps.design.payload.contract'}}
    verify = fx.cog_step('verify', depends_on=['author'])
    verify['input'] = {'operation': 'verify', 'source': {'$from': 'steps.author.payload'}}
    review = fx.cog_step('review', depends_on=['design', 'author', 'verify'], gate={'policy': 'human', 'decides': 'artifact', 'artifact': {'kind': 'cog-candidate', 'digests': {'contract': {'$from': 'steps.design.decision.artifact.digests.contract'}, 'source': {'$sha256': {'$from': 'steps.author.payload'}}, 'evidence': {'$sha256': {'$from': 'steps.verify.payload'}}, 'assessment': {'$sha256': {'$from': 'steps.review.payload'}}}}})
    review['input'] = {'operation': 'review', 'evidence': {'$from': 'steps.verify.payload'}}
    prepare = fx.cog_step('prepare')
    prepare['input'] = {'operation': 'prepare', 'previous': {'$from': 'inputs.cycle_previous.steps.author.request'}}
    return fx.spec_doc([design, author, verify, review], inputs=[{'name': 'max_attempts', 'required': True}, {'name': 'max_cost_units', 'required': True}], outputs={'source': {'$from': 'steps.author.payload'}, 'accepted': {'$from': 'steps.review.decision.verdict'}}, cycle={'outcome_step': 'review', 'outcome_field': 'classification', 'max_attempts': {'$from': 'inputs.max_attempts'}, 'max_cost_units': {'$from': 'inputs.max_cost_units'}, 'costs': {'design': 1, 'author': 1, 'verify': 0, 'review': 1, 'prepare': 0}, 'transitions': {'revise': {'restart': 'author', 'prepare': [prepare], 'replace': {'author': {'input': {'$from': 'steps.prepare.payload.request'}, 'depends_on': ['design', 'prepare']}}}, 'insufficient_evidence': {'restart': 'verify'}}})


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.package = self.root / 'op'
        fx.write_package(self.package, cycle_spec())
        (self.package / 'src').mkdir()
        for name in op_cycle.MASTERS:
            shutil.copyfile(fx.OP_SRC / name, self.package / 'src' / name)
        for name in ['design', 'author', 'verify', 'review', 'prepare']:
            fx.write_cog(self.root, name)
        self.request = self.root / 'request.json'; self.request.write_text(json.dumps({'max_attempts': 3, 'max_cost_units': 20}))
        self.outcomes = ['revise', 'pass']; self.reviews = 0
        self.calls = []
        self.contract = {'purpose': 'Keep this contract', 'acceptance_criteria': ['also preserve unrelated requirements']}
        def invoke(cog_dir, task, request_path, **seam):
            request = json.loads(Path(request_path).read_text()); self.calls.append(request)
            operation = request['operation']
            if operation == 'design': payload = {'contract': self.contract}
            elif operation in ('author', 'revise'): payload = {'source': operation, 'contract': request['contract']}
            elif operation == 'prepare': payload = {'request': {**request['previous'], 'operation': 'revise'}}
            elif operation == 'verify': payload = request
            else:
                payload = {'classification': self.outcomes[min(self.reviews, len(self.outcomes)-1)], 'reason': 'Synthetic test review'}
                self.reviews += 1
            return fx.envelope(payload=payload)
        self.patch = patch.object(op_runner, 'invoke_cog', side_effect=invoke); self.patch.start(); self.addCleanup(self.patch.stop)

    def decide(self, output, verdict='accept'):
        pending = json.loads(Path(output['pending']).read_text())
        doc = {'schema': op_runner.DECISION_SCHEMA, 'run_id': pending['run_id'], 'step': pending['step'], 'payload_sha256': pending['payload_sha256'], 'artifact_sha256': pending['artifact_sha256'], 'verdict': verdict, 'decided_by': 'test-reviewer', 'decided_at': op_track.utc_now()}
        if verdict == 'reject': doc['reason'] = 'Rejected test candidate'
        path = self.root / ('decision-' + pending['run_id'] + '-' + pending['step'] + '.json'); path.write_text(json.dumps(doc)); return path

    def start_and_accept_contract(self):
        code, output = op_cycle.start(self.package, self.request)
        self.assertEqual(code, 3)
        self.assertEqual(output['step'], 'design')
        return op_cycle.resume(self.package, output['cycle_dir'], self.decide(output))

    def test_plain_child_resume_cannot_bypass_cycle(self):
        code, output = op_cycle.start(self.package, self.request)
        phase = json.loads(Path(output['cycle']).read_text())['phases'][-1]
        with self.assertRaisesRegex(op_spec.OpSpecError, 'bounded cycle'):
            op_runner.resume(phase['package'], output['run_dir'], decision_path=self.decide(output))
        self.assertEqual(len(self.calls), 1)

    def test_erased_reservations_are_refused_against_child_attempts(self):
        code, output = op_cycle.start(self.package, self.request)
        state = json.loads(Path(output['cycle']).read_text())
        state.update(reservations=[], cost_units_reserved=0)
        Path(output['cycle']).write_text(json.dumps(state))
        with self.assertRaisesRegex(op_spec.OpSpecError, 'child Track'):
            op_cycle.resume(self.package, output['cycle_dir'], self.decide(output))

    def test_repriced_reservations_cannot_lower_declared_costs(self):
        code, output = op_cycle.start(self.package, self.request)
        state=json.loads(Path(output['cycle']).read_text())
        state['reservations'][0]['units']=0;state['cost_units_reserved']=0
        Path(output['cycle']).write_text(json.dumps(state))
        with self.assertRaisesRegex(op_spec.OpSpecError,'declared policy'):
            op_cycle.resume(self.package,output['cycle_dir'],self.decide(output))

    def test_bad_decision_keeps_paused_cycle_status(self):
        code, output = op_cycle.start(self.package, self.request)
        path = self.decide(output); doc = json.loads(path.read_text())
        doc['payload_sha256'] = '0'*64; path.write_text(json.dumps(doc))
        with self.assertRaises(op_spec.OpSpecError):
            op_cycle.resume(self.package, output['cycle_dir'], path)
        self.assertEqual(json.loads(Path(output['cycle']).read_text())['status'], 'paused')

    def test_revision_preserves_contract_retains_rounds_and_needs_final_acceptance(self):
        code, output = self.start_and_accept_contract()
        self.assertEqual(code, 3); self.assertEqual(output['step'], 'review')
        self.assertEqual(output['attempts'], 2)
        self.assertEqual([r['operation'] for r in self.calls].count('design'), 1)
        self.assertEqual([r['operation'] for r in self.calls].count('author'), 1)
        self.assertEqual([r['operation'] for r in self.calls].count('revise'), 1)
        self.assertTrue(all(r['contract'] == self.contract for r in self.calls if r['operation'] in ('author', 'revise')))
        state = json.loads(Path(output['cycle']).read_text())
        self.assertEqual(state['cost_units_reserved'], 5)
        self.assertEqual(json.loads((Path(state['phases'][0]['run_dir'])/'track.json').read_text())['status'], 'cycle-transition')
        code, final = op_cycle.resume(self.package, output['cycle_dir'], self.decide(output))
        self.assertEqual(code, 0); self.assertEqual(final['outputs']['accepted'], 'accept')

    def test_missing_evidence_reuses_same_candidate_and_contract(self):
        self.outcomes = ['insufficient_evidence', 'pass']
        code, output = self.start_and_accept_contract(); self.assertEqual(code, 3)
        self.assertEqual([r['operation'] for r in self.calls].count('author'), 1)
        self.assertEqual([r['operation'] for r in self.calls].count('verify'), 2)
        self.assertNotIn('prepare', [r['operation'] for r in self.calls])
        self.assertEqual(output['cost_units_reserved'], 4)

    def test_attempt_exhaustion_keeps_last_review_and_does_not_restart(self):
        self.outcomes = ['revise']; self.request.write_text(json.dumps({'max_attempts': 1, 'max_cost_units': 20}))
        code, output = self.start_and_accept_contract()
        self.assertEqual(code, 1); self.assertEqual(output['status'], 'budget-exhausted')
        self.assertIn('Attempt budget', output['reason'])
        self.assertEqual(output['attempts'], 1)
        track=json.loads((Path(output['run_dir'])/'track.json').read_text())
        self.assertEqual(track['status'],'cycle-transition')
        before = len(self.calls)
        op_cycle.resume(self.package, output['cycle_dir'])
        self.assertEqual(len(self.calls), before)

    def test_cost_exhaustion_stops_before_next_paid_invocation(self):
        self.request.write_text(json.dumps({'max_attempts': 3, 'max_cost_units': 1}))
        code, output = self.start_and_accept_contract()
        self.assertEqual(code, 1); self.assertEqual(output['status'], 'budget-exhausted')
        self.assertEqual([r['operation'] for r in self.calls], ['design'])
        self.assertEqual(output['cost_units_reserved'], 1)
        track=json.loads((Path(output['run_dir'])/'track.json').read_text())
        self.assertEqual(track['status'],'budget-exhausted')
        self.assertEqual(next(row for row in track['steps'] if row['id']=='author')['attempts'], [])

    def test_interrupted_answer_is_recovered_without_another_paid_call(self):
        code, output = op_cycle.start(self.package, self.request)
        decision = self.decide(output)
        original = op_track.save
        def crash_after_answer(track, run_dir):
            if any(r['id'] == 'author' and r['status'] == 'passed' for r in track['steps']):
                raise RuntimeError('power loss after durable answer')
            return original(track, run_dir)
        with patch.object(op_track, 'save', side_effect=crash_after_answer), self.assertRaisesRegex(RuntimeError, 'power loss'):
            op_cycle.resume(self.package, output['cycle_dir'], decision)
        self.assertEqual([r['operation'] for r in self.calls].count('author'), 1)
        self.assertEqual(json.loads(Path(output['cycle']).read_text())['status'], 'running')
        self.outcomes = ['pass']
        code, resumed = op_cycle.resume(self.package, output['cycle_dir'])
        self.assertEqual(code, 3)
        self.assertEqual([r['operation'] for r in self.calls].count('design'), 1)
        self.assertEqual([r['operation'] for r in self.calls].count('author'), 1)
        self.assertEqual(resumed['cost_units_reserved'], 3)

    def test_completion_with_warnings_is_successful_on_initial_and_terminal_resume(self):
        self.outcomes = ['pass']
        # Inject a warning into the final review while retaining its valid payload.
        invoke = op_runner.invoke_cog.side_effect
        def warned(*args, **kwargs):
            result = invoke(*args, **kwargs)
            if json.loads(Path(args[2]).read_text())['operation'] == 'review':
                result['problems'] = [{'severity': 'warning', 'code': 'advisory', 'message': 'Synthetic warning'}]
            return result
        with patch.object(op_runner, 'invoke_cog', side_effect=warned):
            code, output = self.start_and_accept_contract()
        code, final = op_cycle.resume(self.package, output['cycle_dir'], self.decide(output))
        self.assertEqual(code, 0)
        self.assertEqual(final['status'], 'completed-with-problems')
        self.assertTrue(final['ok'])
        code, again = op_cycle.resume(self.package, output['cycle_dir'])
        self.assertEqual(code, 0); self.assertTrue(again['ok'])

    def test_declared_terminal_error_keeps_reason_and_does_not_repeat(self):
        doc = cycle_spec(); doc['cycle']['terminal_errors'] = {'prepare': ['invalid-revision']}
        (self.package/'op.yaml').write_text(json.dumps(doc))
        invoke = op_runner.invoke_cog.side_effect
        def refuse(*args, **kwargs):
            if json.loads(Path(args[2]).read_text())['operation'] == 'prepare':
                result = fx.envelope(ok=False)
                result['error'] = {'code': 'invalid-revision', 'detail': 'Finding is outside the accepted scope'}
                return result
            return invoke(*args, **kwargs)
        with patch.object(op_runner, 'invoke_cog', side_effect=refuse):
            code, output = self.start_and_accept_contract()
        self.assertEqual(code, 1); self.assertEqual(output['status'], 'refused')
        self.assertIn('outside', output['reason'])
        before = len(self.calls)
        code, again = op_cycle.resume(self.package, output['cycle_dir'])
        self.assertEqual(again['status'], 'refused'); self.assertEqual(len(self.calls), before)

    def test_rejection_finishes_cycle_and_changed_budgets_are_refused(self):
        self.outcomes = ['pass']
        code, output = self.start_and_accept_contract()
        code, rejected = op_cycle.resume(self.package, output['cycle_dir'], self.decide(output, 'reject'))
        self.assertEqual(code, 1); self.assertEqual(rejected['status'], 'rejected')
        state = json.loads(Path(output['cycle']).read_text()); state['max_attempts'] += 1
        Path(output['cycle']).write_text(json.dumps(state))
        with self.assertRaises(op_spec.OpSpecError):
            op_cycle.resume(self.package, output['cycle_dir'])

    def test_recoverable_failure_reason_disappears_after_retry_and_acceptance(self):
        self.outcomes = ['pass']
        code, output = op_cycle.start(self.package, self.request)
        invoke = op_runner.invoke_cog.side_effect
        def fail_once(*args, **kwargs):
            if json.loads(Path(args[2]).read_text())['operation'] == 'author':
                result = fx.envelope(ok=False)
                result['error'] = {'code':'model-unavailable','detail':'Synthetic endpoint unavailable'}
                return result
            return invoke(*args, **kwargs)
        with patch.object(op_runner,'invoke_cog',side_effect=fail_once):
            code, failed = op_cycle.resume(self.package,output['cycle_dir'],self.decide(output))
        self.assertEqual(failed['status'],'failed');self.assertIn('unavailable',failed['reason'])
        code, paused = op_cycle.resume(self.package,output['cycle_dir'])
        self.assertEqual(code,3);self.assertNotIn('reason',paused)
        self.assertNotIn('reason',json.loads(Path(paused['cycle']).read_text()))
        code, accepted = op_cycle.resume(self.package,output['cycle_dir'],self.decide(paused))
        self.assertEqual(code,0);self.assertNotIn('reason',accepted)

    def test_wrapped_process_failure_preserves_terminal_cog_error(self):
        doc=cycle_spec();doc['cycle']['terminal_errors']={'prepare':['invalid-revision']}
        (self.package/'op.yaml').write_text(json.dumps(doc))
        invoke=op_runner.invoke_cog.side_effect
        def refuse(*args, **kwargs):
            if json.loads(Path(args[2]).read_text())['operation']=='prepare':
                raw=fx.envelope(ok=False);raw['error']={'code':'invalid-revision','detail':'Immutable scope refusal'}
                return op_runner.failed_envelope(args[0],args[1],'Process exited 1',raw=raw,carried=raw,evidence={'returncode':1})
            return invoke(*args, **kwargs)
        with patch.object(op_runner,'invoke_cog',side_effect=refuse):
            code, refused=self.start_and_accept_contract()
        self.assertEqual(refused['status'],'refused');self.assertEqual(refused['reason'],'Immutable scope refusal')
        with patch.object(op_runner,'invoke_cog') as again:
            code, stopped=op_cycle.resume(self.package,refused['cycle_dir'])
        again.assert_not_called();self.assertEqual(stopped['reason'],'Immutable scope refusal')


class CycleDeclarationTests(unittest.TestCase):
    def test_unknown_replacement_and_effectful_phase_are_refused(self):
        doc = cycle_spec(); doc['cycle']['transitions']['revise']['replace']['author']['cog'] = {'id': 'different'}
        with self.assertRaises(op_spec.OpSpecError): op_spec.OpSpec(doc)
        doc = cycle_spec(); doc['steps'][0]['authority'] = {'requires': [{'resource': 'github', 'action': 'write', 'targets': []}]}
        with self.assertRaises(op_spec.OpSpecError): op_spec.OpSpec(doc)
