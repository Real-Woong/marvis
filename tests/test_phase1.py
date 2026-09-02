"""Phase 1 회귀 테스트 — 도구 표면과 에이전트 루프.

모델 호출은 FakeClient로 대체합니다. 네트워크도 API 키도 필요 없고,
"모델이 이렇게 답했을 때 우리 루프가 어떻게 행동하는가"만 봅니다.
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = tempfile.TemporaryDirectory()
os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "phase1.db")
# 테스트가 실제 SECRETARY를 읽거나 render.py를 실행하면 안 됩니다.
# 없는 경로를 가리키면 secretary.sync()가 조용히 건너뜁니다.
os.environ["MARVIS_SECRETARY_DIR"] = str(Path(_TMP_DIR.name) / "no-secretary")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ["MARVIS_ROUTER"] = "regex"  # shadow 스레드가 뜨지 않게

_genai = types.ModuleType("google.generativeai")
_genai.configure = lambda **kwargs: None
_genai.GenerativeModel = lambda name: None
_google = types.ModuleType("google")
_google.generativeai = _genai
sys.modules.setdefault("google", _google)
sys.modules["google.generativeai"] = _genai

from marvis import agent, core, memory, projects  # noqa: E402
from marvis.db import get_connection, init_db  # noqa: E402
from marvis.llm.base import LLMResponse, ToolCall, Usage  # noqa: E402
from marvis.llm.tools import REGISTRY, execute  # noqa: E402
from marvis.time_utils import now_kst, today_kst_date  # noqa: E402


def _reset_db():
    init_db()
    conn = get_connection()
    for table in ("items", "projects", "events", "config"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


class FakeClient:
    """미리 정해 둔 응답을 순서대로 내놓습니다."""

    name = "fake"
    model = "fake-1"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, messages, tools):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if not self.responses:
            return LLMResponse(text="(응답 없음)", usage=Usage())
        return self.responses.pop(0)

    def generate_text(self, prompt):
        return "[단발 답변]"


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(name=name, args=args, call_id=name)], usage=Usage())


def say(text):
    return LLMResponse(text=text, usage=Usage())


class ToolValidationTest(unittest.TestCase):
    """검증 실패가 예외가 아니라 모델이 읽을 수 있는 오류로 돌아오는지."""

    def setUp(self):
        _reset_db()

    def test_past_reminder_is_rejected_with_a_hint(self):
        yesterday = (now_kst() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        result = execute("save_memory", {
            "content": "약 먹기", "kind": "schedule", "reminder_at": yesterday,
        })
        self.assertEqual(result["error"], "reminder_at_in_past")
        self.assertIn("hint", result)
        self.assertEqual(
            get_connection().execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"], 0
        )

    def test_unparseable_date_is_rejected(self):
        result = execute("save_memory", {
            "content": "치과", "kind": "schedule", "schedule_date": "다음주 화요일",
        })
        self.assertEqual(result["error"], "invalid_date")

    def test_reminder_on_a_different_day_is_rejected(self):
        result = execute("save_memory", {
            "content": "회의", "kind": "schedule",
            "schedule_date": "2099-01-10", "reminder_at": "2099-01-11 09:00:00",
        })
        self.assertEqual(result["error"], "reminder_outside_schedule_date")

    def test_missing_required_argument(self):
        self.assertEqual(execute("save_memory", {"content": "뭔가"})["error"], "missing_argument")

    def test_invalid_enum(self):
        result = execute("save_memory", {"content": "x", "kind": "일정"})
        self.assertEqual(result["error"], "invalid_enum")

    def test_unknown_tool(self):
        self.assertEqual(execute("nope", {})["error"], "unknown_tool")

    def test_valid_save_writes_one_row(self):
        result = execute("save_memory", {
            "content": "치과 가기", "kind": "schedule", "schedule_date": "2099-01-10",
        })
        self.assertTrue(result["saved"])
        row = get_connection().execute("SELECT content, schedule_date FROM items").fetchone()
        self.assertEqual(row["schedule_date"], "2099-01-10")


class AmbiguityTest(unittest.TestCase):
    """모호할 때 정보를 버리지 않고 후보를 돌려주는지."""

    def setUp(self):
        _reset_db()
        projects.add_project("Agora")
        projects.add_project("AI-Spoc")

    def test_ambiguous_project_returns_candidates(self):
        result = execute("update_project", {"name": "A", "status": "중단"})
        self.assertEqual(result["error"], "project_ambiguous")
        self.assertCountEqual(result["candidates"], ["Agora", "AI-Spoc"])

    def test_exact_match_wins_over_partial(self):
        result = execute("update_project", {"name": "Agora", "next_steps": "검증"})
        self.assertTrue(result["updated"])
        self.assertEqual(projects.find_projects_by_name("Agora")[0]["next_steps"], "검증")

    def test_duplicate_project_is_refused(self):
        self.assertEqual(execute("add_project", {"name": "Agora"})["error"], "project_exists")


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_tool_then_final_answer_is_one_reply(self):
        client = FakeClient(
            call("save_memory", content="치과 가기", kind="schedule", schedule_date="2099-01-10"),
            say("1월 10일 치과 일정으로 저장했습니다."),
        )
        result = agent.run_turn(client, "2099년 1월 10일에 치과 가야 해")

        self.assertEqual(result.primary_tool, "save_memory")
        self.assertEqual(result.text, "1월 10일 치과 일정으로 저장했습니다.")
        self.assertEqual(result.steps, 2)

    def test_plain_chat_calls_no_tool(self):
        result = agent.run_turn(FakeClient(say("안녕하세요.")), "안녕")
        self.assertIsNone(result.primary_tool)
        self.assertEqual(result.text, "안녕하세요.")

    def test_validation_error_is_retried_once_then_succeeds(self):
        client = FakeClient(
            call("save_memory", content="치과", kind="schedule", schedule_date="내일"),
            call("save_memory", content="치과", kind="schedule", schedule_date="2099-01-10"),
            say("저장했습니다."),
        )
        result = agent.run_turn(client, "내일 치과")

        self.assertEqual(len(result.tool_calls), 2)
        self.assertFalse(result.tool_calls[0]["ok"])
        self.assertTrue(result.tool_calls[1]["ok"])
        self.assertEqual(result.text, "저장했습니다.")

    def test_repeated_failure_falls_back_to_clarify(self):
        bad = call("save_memory", content="치과", kind="schedule", schedule_date="내일")
        result = agent.run_turn(FakeClient(bad, bad, bad), "내일 치과")

        self.assertIsNotNone(result.clarify)
        self.assertIn("해석할 수 없습니다", result.text)

    def test_clarify_tool_ends_the_turn_with_options(self):
        client = FakeClient(
            call("clarify", question="어떤 프로젝트요?", options=["Agora", "AI-Spoc"]),
            say("이 문장은 나오면 안 됩니다."),
        )
        result = agent.run_turn(client, "프로젝트 중단해줘")

        self.assertEqual(result.primary_tool, "clarify")
        self.assertIn("어떤 프로젝트요?", result.text)
        self.assertIn("- Agora", result.text)
        self.assertEqual(result.steps, 1)

    def test_llm_failure_is_reported_not_raised(self):
        class Broken:
            name = "broken"
            model = "x"

            def complete(self, *a, **k):
                raise RuntimeError("boom")

        result = agent.run_turn(Broken(), "안녕")
        self.assertIsNotNone(result.error)
        self.assertIn("llm_call_failed", result.error)

    def test_every_turn_writes_a_trace(self):
        agent.run_turn(FakeClient(say("네.")), "고마워")
        row = get_connection().execute(
            "SELECT payload FROM events WHERE kind = 'turn.trace'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("elapsed_ms", row["payload"])


class SystemPromptTest(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_prompt_carries_names_and_counts_but_not_contents(self):
        projects.add_project("Agora")
        memory.create_item("아주 비밀스러운 메모 내용", kind="note")

        prompt = agent.build_system_prompt()

        self.assertIn(today_kst_date().isoformat(), prompt)
        self.assertIn("Agora", prompt)
        self.assertNotIn("아주 비밀스러운 메모 내용", prompt)


class RouterModeTest(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self._mode = os.environ.get("MARVIS_ROUTER")

    def tearDown(self):
        if self._mode is None:
            os.environ.pop("MARVIS_ROUTER", None)
        else:
            os.environ["MARVIS_ROUTER"] = self._mode

    def test_regex_mode_never_touches_the_llm(self):
        os.environ["MARVIS_ROUTER"] = "regex"
        core.ask_gemini = lambda text: "[정규식 경로 답변]"
        replies = list(core.handle_message("내일 병원 예약 확인해야 해"))
        self.assertEqual(len(replies), 2)
        self.assertTrue(replies[0].ack)

    def test_llm_mode_yields_ack_then_single_answer(self):
        os.environ["MARVIS_ROUTER"] = "llm"
        client = FakeClient(
            call("save_memory", content="병원 예약 확인", kind="schedule",
                 schedule_date="2099-01-10"),
            say("병원 일정으로 저장했습니다."),
        )
        import marvis.llm.factory as factory

        factory._client = client
        try:
            replies = list(core.handle_message("2099년 1월 10일 병원 예약 확인해야 해"))
        finally:
            factory.reset_client()

        self.assertEqual(len(replies), 2)
        self.assertTrue(replies[0].ack)
        self.assertEqual(replies[1].text, "병원 일정으로 저장했습니다.")

    def test_route_comparison_table(self):
        self.assertTrue(core._routes_agree("save", "save_memory"))
        self.assertTrue(core._routes_agree("chat", None))
        self.assertTrue(core._routes_agree("query", "list_memories"))
        self.assertTrue(core._routes_agree("project", "add_project"))
        self.assertFalse(core._routes_agree("save", None))
        self.assertFalse(core._routes_agree("chat", "save_memory"))


class DependencyTest(unittest.TestCase):
    """의존성이 requirements.txt와 실제로 맞는지."""

    def test_no_module_imports_the_deprecated_sdk(self):
        """레거시 google.generativeai는 requirements에서 빠졌다.

        ai.py가 이걸 계속 import하고 있어서, 옛 SDK가 남아 있던 서버에서는
        돌지만 깨끗한 환경에서는 기동 자체가 실패했다.
        """
        root = Path(__file__).resolve().parent.parent
        offenders = [
            path.relative_to(root)
            for path in (root / "marvis").rglob("*.py")
            if "import google.generativeai" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], f"레거시 SDK를 import하는 모듈: {offenders}")

    def test_requirements_match_what_the_code_imports(self):
        requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("google-genai", requirements)
        self.assertNotIn("google-generativeai", requirements)


class ToolSurfaceTest(unittest.TestCase):
    def test_all_tools_have_a_schema_and_description(self):
        expected = {
            "save_memory", "list_schedule", "list_memories", "complete_item",
            "list_projects", "add_project", "update_project", "clarify",
        }
        self.assertEqual(set(REGISTRY), expected)
        for name, spec in REGISTRY.items():
            self.assertTrue(spec.description.strip(), name)
            self.assertEqual(spec.parameters["type"], "object", name)

    def test_forget_all_is_not_a_tool(self):
        """모델이 전체 보관을 부를 수 있는 경로가 있으면 안 됩니다."""
        for name in REGISTRY:
            self.assertNotIn("forget", name)
            self.assertNotIn("archive_all", name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
