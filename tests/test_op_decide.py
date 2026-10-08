"""Human helper, deferred authority, and safe admission scaffolding."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import contextlib
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import op_fixtures as fx
from op_fixtures import op_runner, op_spec
import smith_decide
import smith_op
from test_op_authority import AuthorityCase, REPO


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name).resolve()
        payload = {'changes': [fx.change('a'), fx.change('b')]}
        self.pending, _, _ = op_runner.write_pending(self.run, 'run-1', 'choose', payload)
        (self.run / 'track.json').write_text(json.dumps({'run_id': 'run-1', 'status': 'paused',
            'steps': [{'id': 'choose', 'status': 'awaiting-decision'}]}))

    def test_exact_explicit_choices_fill_digest_and_timestamp(self):
        decision = smith_decide.prepare(self.run, approve=['a'], defer_rest=True, by='learner')
        applied = op_runner.apply_decision(self.pending, decision)
        self.assertEqual([item['change_id'] for item in applied['approved']], ['a'])
        self.assertEqual(applied['deferred'], ['b'])
        self.assertEqual(applied['rejected'], [])
        self.assertEqual(decision['payload_sha256'], self.pending['payload_sha256'])
        self.assertEqual([h['verdict'] for h in applied['history']], ['approve', 'defer'])

    def test_no_implicit_choice_unknown_ids_or_duplicate_selection(self):
        for options in ({'approve': ['a']}, {'approve': ['wrong'], 'reject_rest': True},
                        {'approve': ['a'], 'reject': ['a'], 'reject_rest': True}):
            with self.subTest(options=options), self.assertRaises(op_spec.OpSpecError):
                smith_decide.prepare(self.run, by='learner', **options)

    def test_changed_pending_content_is_not_silently_reapproved(self):
        path = self.run / 'pending/choose.json'
        doc = json.loads(path.read_text()); doc['payload']['changes'][0]['summary'] = 'changed'
        path.write_text(json.dumps(doc))
        with self.assertRaisesRegex(op_spec.OpSpecError, 'changed'):
            smith_decide.prepare(self.run, approve=['a'], reject_rest=True, by='learner')

    def test_saved_decision_never_overwrites_an_existing_input(self):
        doc = smith_decide.prepare(self.run, reject_rest=True, by='learner')
        path = smith_decide.save(self.run, doc)
        with self.assertRaises(FileExistsError):
            smith_decide.save(self.run, doc, path)
        self.assertEqual(json.loads(path.read_text()), doc)

    def test_artifact_helper_binds_both_digests_and_requires_rejection_reason(self):
        artifact = {'kind': 'cog-contract', 'id': 'contract-a', 'summary': 'A contract',
                    'digests': {'contract': 'a' * 64}, 'detail': {}}
        pending, _, _ = op_runner.write_pending(self.run, 'run-1', 'choose', {'contract': {}}, artifact)
        decision = smith_decide.prepare(self.run, verdict='accept', by='learner')
        self.assertEqual(decision['artifact_sha256'], pending['artifact_sha256'])
        self.assertEqual(op_runner.apply_decision(pending, decision)['verdict'], 'accept')
        with self.assertRaises(op_spec.OpSpecError):
            smith_decide.prepare(self.run, verdict='reject', by='learner')
        self.assertEqual(smith_decide.prepare(self.run, verdict='reject', reason='Wrong scope', by='learner')['reason'], 'Wrong scope')
        with self.assertRaises(op_spec.OpSpecError):
            smith_decide.prepare(self.run, defer_rest=True, by='learner')

    def test_cli_resume_uses_the_selected_packages_own_runtime(self):
        import cogsmith_cli
        package = self.run / 'package'
        package.mkdir()
        (package / 'op.yaml').write_text('schema: fixture')
        output = self.run / 'input.json'
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()), mock.patch('cogsmith_cli.subprocess.run') as run:
            run.return_value.returncode = 3
            with mock.patch.object(sys, 'argv', ['cogsmith', 'op', 'decide', str(self.run), '--approve', 'a', '--defer-rest', '--by', 'learner', '--output', str(output), '--resume', '--package', str(package)]):
                code = cogsmith_cli.main()
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(stdout.getvalue()), json.loads(output.read_text()))
        self.assertEqual(run.call_args.args[0][1:], [str(package / 'src/op_runner.py'), '--resume', str(self.run), '--decision', str(output)])
        self.assertEqual(run.call_args.kwargs['cwd'], package)


class DeferredAuthorityTests(AuthorityCase):
    def test_deferred_change_is_not_rejected_and_never_enters_write_grant(self):
        code, output, track = self.start(answers={'ask': [fx.envelope(payload={'items': [1]}),
            fx.envelope(payload={'changes': [fx.change('a'), fx.change('b')]}),
            fx.envelope(payload={'applied': ['a']})]})
        self.assertEqual(code, 3)
        decision = smith_decide.prepare(output['run_dir'], approve=['a'], defer_rest=True, by='learner')
        path = smith_decide.save(output['run_dir'], decision)
        code, output, track = self.resume(output, path)
        self.assertEqual(code, 0)
        compose = self.step(track, 'compose')['decision']['value']
        self.assertEqual(compose['deferred'], ['b'])
        self.assertEqual(compose['rejected'], [])
        writer = self.step(track, 'example-writer')
        grant = json.loads(Path(writer['grant']).read_text())
        self.assertEqual([entry['change_id'] for entry in grant['operations'][0]['changes']], ['a'])


class AdmissionScaffoldTests(unittest.TestCase):
    def test_scaffold_has_required_resources_actions_but_no_authorized_targets(self):
        from test_op_authority import spec_doc
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'spec.json'; path.write_text(json.dumps(spec_doc()))
            spec, files = smith_op.plan(path, Path(directory) / 'op')
            doc = json.loads(files['examples/admission.json'])
            self.assertEqual([(op['resource'], op['action'], op['targets']) for op in doc['operations']], [('github', 'read', []), ('github', 'write', [])])
            authority = Path(directory) / 'admission.json'; authority.write_text(json.dumps(doc))
            self.assertEqual(op_runner.admitted_targets(op_runner.load_authority(authority), 'github', 'read'), set())

    def test_no_admission_is_generated_for_an_op_that_reaches_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'spec.json'; path.write_text(json.dumps(fx.spec_doc()))
            _, files = smith_op.plan(path, Path(directory) / 'op')
            self.assertNotIn('examples/admission.json', files)
