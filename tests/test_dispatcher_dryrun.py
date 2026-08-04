from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from core import dispatcher


class DispatcherDryRunTests(unittest.TestCase):
    def test_dry_run_fires_observably_without_touching_persistent_latches(self):
        cycle = "2026-08-04T12:00"
        for stage in ("live", "push"):
            with self.subTest(stage=stage):
                result: list[str] = []
                fire = mock.Mock(return_value=f"dry-{stage}-key")
                with (
                    mock.patch.dict(
                        os.environ, {"OKX_TRIGGER_DRYRUN": "1"}, clear=False
                    ),
                    mock.patch.object(
                        dispatcher.ledger,
                        "stage_dispatched",
                        return_value=False,
                    ) as stage_dispatched,
                    mock.patch.object(
                        dispatcher.ledger, "try_profile_lease"
                    ) as try_profile_lease,
                    mock.patch.object(
                        dispatcher.ledger, "try_stage"
                    ) as try_stage,
                    mock.patch.object(
                        dispatcher.ledger, "connect"
                    ) as connect,
                    mock.patch.object(
                        dispatcher.ledger, "release_stage"
                    ) as release_stage,
                    mock.patch.object(
                        dispatcher.ledger, "release_profile_lease"
                    ) as release_profile_lease,
                    mock.patch.object(dispatcher, "log") as write_log,
                ):
                    dispatcher._fire_stage(
                        Path("isolated") / "ledger.db",
                        cycle,
                        stage,
                        "full",
                        fire,
                        result,
                    )

                stage_dispatched.assert_called_once()
                fire.assert_called_once_with(stage, cycle, "full")
                try_profile_lease.assert_not_called()
                try_stage.assert_not_called()
                connect.assert_not_called()
                release_stage.assert_not_called()
                release_profile_lease.assert_not_called()
                self.assertEqual(
                    result,
                    [f"dry_run fired {stage} {cycle} (key=dry-{stage}-key)"],
                )
                self.assertIn("no latch written", write_log.call_args.args[0])

    def test_dry_run_failure_reports_error_without_releasing_unowned_latches(self):
        cycle = "2026-08-04T12:15"
        result: list[str] = []
        fire = mock.Mock(side_effect=RuntimeError("synthetic launch error"))
        with (
            mock.patch.dict(
                os.environ, {"OKX_TRIGGER_DRYRUN": "1"}, clear=False
            ),
            mock.patch.object(
                dispatcher.ledger, "stage_dispatched", return_value=False
            ),
            mock.patch.object(dispatcher.ledger, "try_stage") as try_stage,
            mock.patch.object(
                dispatcher.ledger, "try_profile_lease"
            ) as try_profile_lease,
            mock.patch.object(dispatcher.ledger, "connect") as connect,
            mock.patch.object(
                dispatcher.ledger, "release_stage"
            ) as release_stage,
            mock.patch.object(
                dispatcher.ledger, "release_profile_lease"
            ) as release_profile_lease,
            mock.patch.object(dispatcher, "log") as write_log,
        ):
            dispatcher._fire_stage(
                Path("isolated") / "ledger.db",
                cycle,
                "demo",
                "full",
                fire,
                result,
            )

        try_stage.assert_not_called()
        try_profile_lease.assert_not_called()
        connect.assert_not_called()
        release_stage.assert_not_called()
        release_profile_lease.assert_not_called()
        self.assertEqual(
            result,
            [f"fire_failed demo {cycle}: synthetic launch error"],
        )
        self.assertIn("no latch written", write_log.call_args.args[0])

    def test_non_dry_run_keeps_latch_then_fire_behavior(self):
        cycle = "2026-08-04T12:30"
        result: list[str] = []
        events: list[str] = []
        fire = mock.Mock(
            side_effect=lambda *_args: events.append("fire") or "session-key"
        )
        connection = mock.Mock()

        def acquire_profile(*_args):
            events.append("profile")
            return True

        def acquire_stage(*_args):
            events.append("stage")
            return True

        with (
            mock.patch.dict(
                os.environ, {"OKX_TRIGGER_DRYRUN": "0"}, clear=False
            ),
            mock.patch.object(
                dispatcher.ledger, "stage_dispatched", return_value=False
            ),
            mock.patch.object(
                dispatcher.ledger,
                "try_profile_lease",
                side_effect=acquire_profile,
            ),
            mock.patch.object(
                dispatcher.ledger, "try_stage", side_effect=acquire_stage
            ),
            mock.patch.object(
                dispatcher.ledger, "connect", return_value=connection
            ),
            mock.patch.object(
                dispatcher.ledger, "release_stage"
            ) as release_stage,
            mock.patch.object(
                dispatcher.ledger, "release_profile_lease"
            ) as release_profile_lease,
            mock.patch.object(dispatcher, "log"),
        ):
            dispatcher._fire_stage(
                Path("isolated") / "ledger.db",
                cycle,
                "live",
                "full",
                fire,
                result,
            )

        self.assertEqual(events, ["profile", "stage", "fire"])
        self.assertEqual(
            result, [f"fired live {cycle} (key=session-key)"]
        )
        connection.execute.assert_called_once()
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()
        release_stage.assert_not_called()
        release_profile_lease.assert_not_called()


if __name__ == "__main__":
    unittest.main()
