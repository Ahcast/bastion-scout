from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from common import HN


class ManualPipelineTests(unittest.TestCase):
    def test_manual_runs_all_stages_outside_schedule_with_distinct_cycle_ids(self):
        moment = datetime(2026,10,24,16,0,tzinfo=HN)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            master = Path(directory)/'maestro.csv'
            master.write_text('synthetic')
            cfg = {'start_hour':19, 'end_hour':13, 'scout':{'modalidad':'2','tipo':'1'}}
            with patch.object(main,'now',return_value=moment), patch.object(main,'data_path',side_effect=lambda cfg,name:Path(directory)/name), patch.object(main,'read_json',return_value={}), patch.object(main,'write_json'), patch.object(main,'finish_main',side_effect=lambda cfg,state,status,reason,**kw:dict(state,status=status,**kw)), patch.object(main.scout,'run',return_value={'status':'exitoso','raw_sha256':'synthetic'}) as scan, patch.object(main.combiner,'run',return_value={'status':'exitoso','total':0}) as combine, patch.object(main.clasificador,'run',return_value={'status':'exitoso'}) as classify:
                normal=main.run(cfg)
                self.assertTrue(normal['skipped'])
                scan.assert_not_called()
                first=main.run(cfg,manual=True)
                second=main.run(cfg,manual=True)
                self.assertEqual(first['trigger'],'manual')
                self.assertTrue(first['output_ready'])
                self.assertNotEqual(first['run_id'],second['run_id'])
                self.assertEqual(datetime.fromisoformat(first['deadline']),moment+timedelta(hours=18))
                self.assertEqual(scan.call_count,2)
                self.assertEqual(combine.call_count,2)
                self.assertEqual(classify.call_count,2)


if __name__=='__main__':
    unittest.main()
