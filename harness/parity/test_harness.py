import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location('parity', Path(__file__).with_name('run.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class HarnessTests(unittest.TestCase):
    def test_missing_evidence_cannot_complete(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(module.completed(Path(d)), set())

    def test_incomplete_and_dependency_gates(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            for task in module.TASKS[:2]:
                data={'task_id':task['id'],'result':'passed','verification':'mocked','commit':'test-fixture','reviewer':'test-fixture','summary':'test-fixture','tests':[{'command':'fixture','exit_code':0,'log':'fixture'}], 'criteria':{a['id']:{'result':'passed','evidence':'fixture'} for a in task['acceptance']}}
                (folder/(task['id']+'.json')).write_text(json.dumps(data))
            self.assertEqual(module.completed(folder), {'P00','P01'})
            (folder/'P00.json').write_text('{}')
            self.assertEqual(module.completed(folder), set())

    def test_invalid_shape_and_live_claim(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d); path=folder/'P00.json'
            path.write_text('[]')
            self.assertTrue(module.read_evidence(folder,module.TASKS[0])[1])
            path.write_text('{"verification":"sandbox"}')
            self.assertIn('sandbox/production verification needs provider_evidence',module.read_evidence(folder,module.TASKS[0])[1])

    def test_manifest_dependencies_exist_and_prompts_include_criteria(self):
        ids={t['id'] for t in module.TASKS}
        for task in module.TASKS:
            self.assertTrue(set(task['depends_on']) <= ids)
            self.assertNotIn(task['id'],task['depends_on'])
            for a in task['acceptance']:
                self.assertIn(a['id'], module.prompt(task))

if __name__ == '__main__': unittest.main()
