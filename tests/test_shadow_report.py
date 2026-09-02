"""shadow 리포트 테스트.

가장 중요한 건 "미검토 후보가 골든셋에 섞이지 않는가"입니다. LLM 판단을 그대로
정답으로 쓰면 모델을 자기 답으로 채점하게 되고, 그건 점수가 아니라 착시입니다.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = tempfile.TemporaryDirectory()
os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "shadow.db")
# 테스트가 실제 SECRETARY를 읽거나 render.py를 실행하면 안 됩니다.
# 없는 경로를 가리키면 secretary.sync()가 조용히 건너뜁니다.
os.environ["MARVIS_SECRETARY_DIR"] = str(Path(_TMP_DIR.name) / "no-secretary")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ["MARVIS_ROUTER"] = "regex"

_genai = types.ModuleType("google.generativeai")
_genai.configure = lambda **kwargs: None
_genai.GenerativeModel = lambda name: None
_google = types.ModuleType("google")
_google.generativeai = _genai
sys.modules.setdefault("google", _google)
sys.modules["google.generativeai"] = _genai

from marvis import core, shadow_report  # noqa: E402
from marvis.db import get_connection, init_db, log_event, transaction  # noqa: E402


def _seed(rows):
    with transaction() as tx:
        for kind, payload in rows:
            log_event(tx, kind, entity="turn", source="telegram", payload=payload)


class ShadowReportTest(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_connection()
        conn.execute("DELETE FROM events")
        conn.commit()
        self._candidates = shadow_report.CANDIDATES_FILE
        shadow_report.CANDIDATES_FILE = Path(_TMP_DIR.name) / "candidates.jsonl"

    def tearDown(self):
        shadow_report.CANDIDATES_FILE = self._candidates

    def test_loads_and_splits_agreed_from_disagreed(self):
        _seed([
            ("router.agreed", {"text": "안녕", "regex_route": "chat", "llm_tool": None}),
            ("router.disagreed", {"text": "다음주 화요일 치과", "regex_route": "chat",
                                  "llm_tool": "save_memory"}),
            ("turn.trace", {"text": "x", "elapsed_ms": 100}),
        ])
        agreed, disagreed = shadow_report._load(None)
        self.assertEqual(len(agreed), 1)
        self.assertEqual(len(disagreed), 1)
        self.assertEqual(disagreed[0]["llm_tool"], "save_memory")

    def test_export_marks_every_case_unconfirmed(self):
        _seed([
            ("router.disagreed", {"text": "다음주 화요일 치과", "regex_route": "chat",
                                  "llm_tool": "save_memory", "llm_args": {"kind": "schedule"},
                                  "at": "2026-09-02 09:00:00"}),
        ])
        _, disagreed = shadow_report._load(None)
        path = shadow_report.export_candidates(disagreed)

        cases = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("//")
        ]
        self.assertEqual(len(cases), 1)
        self.assertFalse(cases[0]["_review"]["confirmed"])
        self.assertEqual(cases[0]["_review"]["regex_said"], "chat")

    def test_export_never_writes_to_the_golden_set(self):
        """후보는 반드시 별도 파일로 나가야 합니다."""
        self.assertNotEqual(shadow_report.CANDIDATES_FILE.name, "golden.jsonl")
        _seed([("router.disagreed", {"text": "x", "regex_route": "chat", "llm_tool": "clarify"})])
        _, disagreed = shadow_report._load(None)
        path = shadow_report.export_candidates(disagreed)
        self.assertNotEqual(path.name, "golden.jsonl")

    def test_export_deduplicates_repeated_utterances(self):
        payload = {"text": "같은 말", "regex_route": "chat", "llm_tool": "save_memory"}
        _seed([("router.disagreed", payload)] * 4)
        _, disagreed = shadow_report._load(None)
        path = shadow_report.export_candidates(disagreed)

        cases = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("//")
        ]
        self.assertEqual(len(cases), 1, "같은 발화는 한 번만 후보가 돼야 합니다")

    def test_empty_database_does_not_crash(self):
        agreed, disagreed = shadow_report._load(None)
        self.assertEqual((agreed, disagreed), ([], []))
        shadow_report.print_summary([], [], None)  # 예외 없이 안내만 출력


class RegexRouteClassificationTest(unittest.TestCase):
    """리포트가 견주는 대상인 정규식 경로 판정 자체를 고정해 둡니다."""

    def test_known_routes(self):
        self.assertEqual(core._classify_regex_route("!스케쥴"), "command.schedule")
        self.assertEqual(core._classify_regex_route("!프로젝트"), "command.projects")
        self.assertEqual(core._classify_regex_route("안녕"), "chat")

    def test_relative_weekday_is_currently_dropped(self):
        """현행 정규식은 '다음주 화요일' 약속을 저장조차 하지 않습니다.

        날짜를 못 잡는 정도가 아니라 chat으로 흘려보냅니다. shadow가 잡아내야 할
        대표 사례라, 고쳐질 때까지 여기에 기록해 둡니다.
        """
        self.assertEqual(core._classify_regex_route("다음주 화요일 오전에 치과 가야 해"), "chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
