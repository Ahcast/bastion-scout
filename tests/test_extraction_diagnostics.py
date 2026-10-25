import unittest

import test_pipeline as f
import common
import scout


class ExtractionDiagnosticsTests(unittest.TestCase):
    setUp = f.PipelineTests.setUp

    def test_resume_reports_only_details_fetched_in_this_invocation(self):
        values = [f.detail(i) for i in range(1, 5)]

        class Site(f.FakeSite):
            broken = True

            def advance(self, page, number):
                if self.broken and number == 3:
                    raise RuntimeError('HonduCompras HTTP 500')
                return super().advance(page, number)

        self.cfg['scout']['page_retry_attempts'] = 0
        site = Site([[row['url']] for row in values], {row['url']: row for row in values})
        first = scout.run(self.cfg, '2026-10-21', client=site)
        self.assertEqual(first['invocation']['last_saved_page'], 2)
        self.assertEqual(first['invocation']['last_failed_page'], 3)
        self.assertEqual(first['invocation']['last_error'], 'HonduCompras HTTP 500')
        self.assertEqual(first['invocation']['details_attempted'], 2)
        self.assertEqual(first['invocation']['extracted'], 2)
        self.assertEqual(first['phases']['discovery']['error'], 'HonduCompras HTTP 500')
        site.broken = False
        second = scout.run(self.cfg, '2026-10-21', client=site)
        self.assertEqual(second['extracted'], 4)  # Entire round, including earlier invocation.
        self.assertEqual(second['invocation']['extracted'], 2)
        self.assertEqual(second['invocation']['details_attempted'], 2)
        self.assertIsNone(second['invocation']['last_failed_page'])
        self.assertIsNone(second['phases']['discovery']['error'])
        third = scout.run(self.cfg, '2026-10-21', client=site)
        self.assertEqual(third['extracted'], 4)
        self.assertEqual(third['invocation']['details_attempted'], 0)
        self.assertEqual(third['invocation']['extracted'], 0)
        self.assertIsNone(third['invocation']['last_saved_page'])

    def test_detail_outcomes_are_separate_from_listing_budget(self):
        values = [f.detail(1), f.detail(2, description='Compra Menor', complete=False), f.detail(3)]

        class Site(f.FakeSite):
            def advance(self, page, number):
                raise common.DeadlineReached('discovery budget')

        site = Site([[row['url'] for row in values], []], {row['url']: row for row in values},
                    fail_once=[values[2]['url']])
        result = scout.run(self.cfg, '2026-10-21', client=site)
        counts = result['invocation']
        self.assertEqual(result['error'], 'presupuesto_descubrimiento')
        self.assertEqual(counts['last_attempted_page'], 2)
        self.assertEqual(counts['last_saved_page'], 1)
        self.assertEqual(counts['details_attempted'], 3)
        self.assertEqual(counts['extracted'], 1)
        self.assertEqual(counts['incomplete'], 1)
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['interrupted'], 0)


if __name__ == '__main__':
    unittest.main()
