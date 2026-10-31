import ast
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from performance.observations import Mailbox, DISABLED, notify, capture_environment, observe_order_call
from performance.runtime import start_performance
from tests.performance_fixtures import Offline
from tests import test_accounting as accounting
from tests import test_monitoring_bot as monitor_bot

ROOT = Path(__file__).resolve().parents[1]


class ObservationTests(Offline):
    def test_default_off_no_thread_db_or_get(self):
        with patch("performance.runtime.Runtime") as factory:
            self.assertIs(start_performance({}, self.path.parent), DISABLED)
            self.assertIs(start_performance({"PERFORMANCE_ENABLED": "false"}, self.path.parent), DISABLED)
        factory.assert_not_called()

    def test_environment_before_dotenv_and_no_trading_key_fallback(self):
        env = {"PERFORMANCE_ENABLED": "true", "BYBIT_API_KEY": "SECRET", "UNRELATED": "SECRET"}
        captured = capture_environment(env)
        env["PERFORMANCE_ENABLED"] = "false"
        self.assertEqual(captured["PERFORMANCE_ENABLED"], "true")
        self.assertNotIn("BYBIT_API_KEY", captured)
        self.assertNotIn("UNRELATED", captured)

    def test_mailbox_copies_actual_inputs_and_is_bounded(self):
        mailbox = Mailbox(capacity=2)
        row = {"symbol": "TESTUSDT", "stall": 4, "ready": True, "api_key": "SECRET"}
        mailbox.emit("strategy_snapshot", {"rows": [row]})
        row["stall"] = 99
        mailbox.emit("decision", {"symbol": "TESTUSDT", "reason": "ready"})
        mailbox.emit("decision", {"symbol": "TESTUSDT", "reason": "ready"})
        values = mailbox.take()
        self.assertEqual(values[0]["body"]["rows"][0]["stall"], "4")
        self.assertNotIn("SECRET", str(values))
        self.assertEqual(mailbox.dropped, 1)

    def test_busy_observer_lock_never_waits(self):
        mailbox = Mailbox()
        with mailbox.lock:
            mailbox.emit("decision", {"symbol": "TESTUSDT", "reason": "ready"})
        self.assertEqual(mailbox.dropped, 1)

    def test_broken_observer_cannot_escape_or_expose_error(self):
        notify(SimpleNamespace(enabled=True, emit=Mock(side_effect=RuntimeError("SECRET"))), "entry", symbol="TESTUSDT")

    def test_wrapper_preserves_args_identity_result_and_exception(self):
        observer = Mailbox()
        result = {"retCode": 0}
        call = Mock(return_value=result)
        self.assertIs(observe_order_call(observer, "place_order", "TESTUSDT", call, 1, symbol="TESTUSDT", qty="1"), result)
        call.assert_called_once_with(1, symbol="TESTUSDT", qty="1")
        error = TimeoutError("SECRET")
        call = Mock(side_effect=error)
        with self.assertRaises(TimeoutError) as caught:
            observe_order_call(observer, "place_order", "TESTUSDT", call, symbol="TESTUSDT")
        self.assertIs(caught.exception, error)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(observer.take()[0]["body"]["reason"], "order_error")

    def test_unicode_symbols_are_valid(self):
        mailbox = Mailbox()
        mailbox.emit("decision", {"symbol": "龙虾USDT", "reason": "candidate", "pump_pct": 30})
        self.assertEqual(mailbox.take()[0]["body"]["symbol"], "龙虾USDT")

    def test_risk_reason_classifies_existing_result_without_free_text(self):
        mailbox = Mailbox()
        mailbox.emit("decision", {"symbol": "TESTUSDT", "reason": "risk", "risk_detail": "Просадка 24.5% > 20%"})
        row = mailbox.take()[0]
        self.assertEqual(row["body"]["risk_reason"], "drawdown")
        self.assertNotIn("24.5", str(row))

    def test_trade_methods_ast_preserved_after_removing_observation_only_calls(self):
        """Compare against accepted production base, not a copied implementation fixture."""
        baseline = ast.parse(subprocess.check_output(["git", "show", "f928be3:bot.py"], cwd=ROOT).decode("utf-8-sig"))
        current = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8-sig"))

        class RemoveObservations(ast.NodeTransformer):
            def visit_Expr(self, node):
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and node.value.func.attr.startswith("_performance"):
                    return None
                return self.generic_visit(node)
        old = next(x for x in baseline.body if isinstance(x, ast.ClassDef) and x.name == "ShortBot")
        new = next(x for x in current.body if isinstance(x, ast.ClassDef) and x.name == "ShortBot")
        names = ("check_pump_candidate", "update_watchlist", "check_stall", "cancel_all_orders_for_symbol",
                 "reload_from_file", "open_position", "check_positions", "refresh_balance", "send_daily_stats")
        for name in names:
            a = next(x for x in old.body if isinstance(x, ast.FunctionDef) and x.name == name)
            b = next(x for x in new.body if isinstance(x, ast.FunctionDef) and x.name == name)
            self.assertEqual(ast.dump(a), ast.dump(RemoveObservations().visit(b)), name)


class EnabledTradingTests(accounting.BotAccountingTests):
    def setUp(self):
        super().setUp()
        self.bot.performance = Mailbox()


class BrokenTradingTests(accounting.BotAccountingTests):
    def setUp(self):
        super().setUp()
        self.bot.performance = SimpleNamespace(enabled=True, emit=Mock(side_effect=RuntimeError("synthetic")))


class EnabledClientTests(accounting.ClientTests):
    def setUp(self):
        super().setUp()
        self.client.performance = Mailbox()


class EnabledLoopTests(monitor_bot.LoopTests):
    def make_bot(self, **kwargs):
        instance = super().make_bot(**kwargs)
        instance.performance = Mailbox()
        return instance
