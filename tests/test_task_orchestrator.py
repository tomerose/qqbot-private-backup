import asyncio
import sys
import unittest
from pathlib import Path


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
    / "astrbot"
    / "data"
    / "plugins"
    / "claude_code_agent"
)
sys.path.insert(0, str(PLUGIN_DIR))

from action_policy import ActionClass  # noqa: E402
from backend_router import BackendRoute  # noqa: E402
from step_policy import StepDecision  # noqa: E402
from task_orchestrator import StepExecution, TaskOrchestrator  # noqa: E402
from task_planner import ExecutionPlan, TaskStep  # noqa: E402
from task_verifier import VerificationEvidence  # noqa: E402


def make_plan(*steps):
    return ExecutionPlan("job123", "claude", tuple(steps))


def make_step(index, instruction="读取项目", action=ActionClass.READ_ONLY):
    return TaskStep("job123", index, instruction, action, False)


class TaskOrchestratorTests(unittest.TestCase):
    def test_executes_verifies_and_completes_steps_in_order(self):
        calls = []

        async def execute(step, route):
            calls.append((step.index, route.backend))
            return StepExecution(0, (), None, False)

        async def verify(_step, _execution):
            return VerificationEvidence(True, "verified")

        orchestrator = TaskOrchestrator(
            policy=lambda _step: StepDecision(True, False, "allowed"),
            router=lambda _step, _attempted: BackendRoute("claude", "selected"),
            executor=execute,
            verifier=verify,
            approval_check=lambda _step: False,
        )

        outcome = asyncio.run(orchestrator.run(make_plan(make_step(0), make_step(1))))

        self.assertEqual(calls, [(0, "claude"), (1, "claude")])
        self.assertEqual(
            [event.kind for event in outcome.events],
            ["started", "step_completed", "step_completed", "completed"],
        )
        self.assertTrue(outcome.verified)
        self.assertEqual(outcome.state, "completed")

    def test_unapproved_step_stops_before_executor(self):
        calls = []

        async def execute(step, _route):
            calls.append(step.index)
            return StepExecution(0, (), None, False)

        async def verify(_step, _execution):
            return VerificationEvidence(True, "verified")

        def policy(step):
            if step.index == 1:
                return StepDecision(False, True, "high_impact")
            return StepDecision(True, False, "allowed")

        orchestrator = TaskOrchestrator(
            policy=policy,
            router=lambda _step, _attempted: BackendRoute("claude", "selected"),
            executor=execute,
            verifier=verify,
            approval_check=lambda _step: False,
        )
        plan = make_plan(
            make_step(0),
            make_step(1, "发送报告", ActionClass.HIGH_IMPACT),
        )

        outcome = asyncio.run(orchestrator.run(plan))

        self.assertEqual(calls, [0])
        self.assertEqual(outcome.events[-1].kind, "approval_required")
        self.assertEqual(outcome.state, "awaiting_approval")

    def test_verification_failure_never_reports_completed(self):
        async def execute(_step, _route):
            return StepExecution(0, (), 1, True)

        async def verify(_step, _execution):
            return VerificationEvidence(False, "verification_failed")

        orchestrator = TaskOrchestrator(
            policy=lambda _step: StepDecision(True, False, "allowed"),
            router=lambda _step, _attempted: BackendRoute("codex", "selected"),
            executor=execute,
            verifier=verify,
            approval_check=lambda _step: False,
        )

        outcome = asyncio.run(
            orchestrator.run(
                make_plan(
                    make_step(0, "生成报告", ActionClass.WORKSPACE_WRITE)
                )
            )
        )

        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.state, "failed")
        self.assertEqual(outcome.events[-1].code, "verification_failed")

    def test_repeated_backend_route_fails_closed_instead_of_looping(self):
        route_calls = 0

        async def execute(_step, _route):
            return StepExecution(1, (), None, False)

        async def verify(_step, _execution):
            return VerificationEvidence(False, "execution_failed")

        def repeating_router(_step, _attempted):
            nonlocal route_calls
            route_calls += 1
            if route_calls > 2:
                raise AssertionError("orchestrator retried the same backend indefinitely")
            return BackendRoute("codex", "selected")

        orchestrator = TaskOrchestrator(
            policy=lambda _step: StepDecision(True, False, "allowed"),
            router=repeating_router,
            executor=execute,
            verifier=verify,
            approval_check=lambda _step: False,
        )

        outcome = asyncio.run(orchestrator.run(make_plan(make_step(0))))

        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.events[-1].code, "backend_repeated")

    def test_read_only_execution_can_switch_backend_once(self):
        calls = []

        async def execute(_step, route):
            calls.append(route.backend)
            return StepExecution(1 if len(calls) == 1 else 0, (), None, False)

        async def verify(_step, execution):
            return VerificationEvidence(
                execution.exit_code == 0,
                "verified" if execution.exit_code == 0 else "execution_failed",
            )

        def router(_step, attempted):
            backend = "codex" if "codex" not in attempted else "claude"
            return BackendRoute(backend, "selected")

        orchestrator = TaskOrchestrator(
            policy=lambda _step: StepDecision(True, False, "allowed"),
            router=router,
            executor=execute,
            verifier=verify,
            approval_check=lambda _step: False,
        )

        outcome = asyncio.run(orchestrator.run(make_plan(make_step(0))))

        self.assertEqual(calls, ["codex", "claude"])
        self.assertTrue(outcome.verified)

    def test_isolated_workspace_write_can_switch_backend_before_side_effect(self):
        calls = []

        async def execute(_step, route):
            calls.append(route.backend)
            return StepExecution(1 if len(calls) == 1 else 0, (), None, False)

        async def verify(_step, execution):
            return VerificationEvidence(
                execution.exit_code == 0,
                "verified" if execution.exit_code == 0 else "execution_failed",
            )

        def router(_step, attempted):
            return BackendRoute("claude" if not attempted else "codex", "selected")

        step = TaskStep(
            "job123", 0, "生成 Word", ActionClass.WORKSPACE_WRITE, True
        )
        orchestrator = TaskOrchestrator(
            policy=lambda _step: StepDecision(True, False, "allowed"),
            router=router,
            executor=execute,
            verifier=verify,
            approval_check=lambda _step: False,
        )

        outcome = asyncio.run(orchestrator.run(make_plan(step)))

        self.assertEqual(calls, ["claude", "codex"])
        self.assertTrue(outcome.verified)

    def test_resume_starts_at_cursor_and_preserves_step_response(self):
        calls = []

        async def execute(step, _route):
            calls.append(step.index)
            return StepExecution(0, (), None, False, response=f"step-{step.index}")

        async def verify(_step, _execution):
            return VerificationEvidence(True, "verified")

        orchestrator = TaskOrchestrator(
            policy=lambda _step: StepDecision(True, False, "allowed"),
            router=lambda _step, _attempted: BackendRoute("claude", "selected"),
            executor=execute,
            verifier=verify,
            approval_check=lambda _step: False,
        )

        outcome = asyncio.run(
            orchestrator.run(
                make_plan(make_step(0), make_step(1)), start_index=1
            )
        )

        self.assertEqual(calls, [1])
        self.assertEqual(outcome.responses, ("step-1",))


if __name__ == "__main__":
    unittest.main()
