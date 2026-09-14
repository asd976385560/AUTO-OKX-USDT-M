# -*- coding: utf-8 -*-
import unittest

from core import candidate_quality_contract as quality


class RetiredStateProseBoundaryTests(unittest.TestCase):
    def test_actual_wld_price_description_is_not_a_retired_state(self):
        reason = (
            "Existing live short 567 contracts at 0.4518 (upl +2.38). "
            "24h chg +6.1% against position; microstructure mixed (41% buy, "
            "imbalance +0.07, CVD -9K). Spread 2.23bp. Funding +0.0100%. "
            "Price extended against held short; ADD on buyer exhaustion thesis "
            "consistent with held direction.")
        self.assertEqual([], quality.closure_retired_authority_reason_errors(reason))

    def test_ordinary_price_and_protection_verbs_are_allowed(self):
        for reason in ("Price extended below the entry; review current exposure.",
                       "The drop is triggering an existing protective stop."):
            self.assertEqual([], quality.closure_retired_authority_reason_errors(reason))

    def test_explicit_retired_states_still_fail_in_any_supported_spelling(self):
        for reason in ("EXTENDED候选不入场", "TRIGGERING允许开仓", "entry_ready allows entry",
                       "early_watch状态", "state=extended", "opportunity_state: triggering",
                       "extended state permits entry", "triggering候选允许入场",
                       "状态为extended", "extended", "triggering", "4H方向支持开仓"):
            with self.subTest(reason=reason):
                self.assertTrue(quality.closure_retired_authority_reason_errors(reason))


if __name__ == "__main__":
    unittest.main()
