"""초기화 명령 테스트.

지우는 코드라 특히 '지우면 안 되는 것을 안 지키는가'를 중심으로 봅니다.
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
os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "reset.db")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ["MARVIS_ROUTER"] = "regex"

_genai = types.ModuleType("google.generativeai")
_genai.configure = lambda **kwargs: None
_genai.GenerativeModel = lambda name: None
_google = types.ModuleType("google")
_google.generativeai = _genai
sys.modules.setdefault("google", _google)
sys.modules["google.generativeai"] = _genai

from marvis import memory, projects, reset as reset_module  # noqa: E402
from marvis.db import get_connection, init_db  # noqa: E402
from marvis.storage import get_setting, set_setting  # noqa: E402


def _count(table: str) -> int:
    return get_connection().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


class ResetTest(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_connection()
        for table in ("items", "projects", "events", "config"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

        for index in range(5):
            memory.create_item(f"항목 {index}", kind="schedule", schedule_date="2099-01-01")
        projects.add_project("Agora")
        projects.add_project("Marvis")
        set_setting("telegram_chat_id", "12345")

        # 백업은 임시 디렉터리로 보냅니다.
        self._backup_dir = reset_module.BACKUP_DIR
        reset_module.BACKUP_DIR = Path(_TMP_DIR.name) / "backups"

    def tearDown(self):
        reset_module.BACKUP_DIR = self._backup_dir

    def test_default_scope_keeps_projects_events_and_config(self):
        reset_module.reset(["items"])

        self.assertEqual(_count("items"), 0)
        self.assertEqual(_count("projects"), 2)
        self.assertEqual(get_setting("telegram_chat_id"), "12345")
        self.assertGreater(_count("events"), 0, "이벤트 로그는 기본으로 남아야 합니다")

    def test_reset_is_recorded_when_the_event_log_survives(self):
        reset_module.reset(["items"])
        row = get_connection().execute(
            "SELECT payload FROM events WHERE kind = 'system.reset'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["payload"])["items"], 5)

    def test_seq_restarts_from_one(self):
        reset_module.reset(["items"])
        self.assertEqual(memory.create_item("새 항목", kind="note")["seq"], 1)

    def test_full_reset_clears_everything(self):
        reset_module.reset(["items", "projects", "events", "config"])
        for table in ("items", "projects", "events", "config"):
            self.assertEqual(_count(table), 0, table)
        self.assertEqual(projects.add_project("새 프로젝트")["seq"], 1)

    def test_backup_is_written_before_deleting(self):
        path = reset_module.write_backup(["items", "projects"])
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["tables"]["items"]), 5)
        self.assertEqual(len(payload["tables"]["projects"]), 2)
        self.assertIn("created_at", payload)
        # 백업은 지우기 전 상태여야 하므로 데이터는 아직 그대로입니다.
        self.assertEqual(_count("items"), 5)

    def test_backup_content_is_enough_to_restore(self):
        path = reset_module.write_backup(["projects"])
        reset_module.reset(["projects"])
        self.assertEqual(_count("projects"), 0)

        saved = json.loads(path.read_text(encoding="utf-8"))["tables"]["projects"]
        names = sorted(row["name"] for row in saved)
        self.assertEqual(names, ["Agora", "Marvis"])
        self.assertIn("status", saved[0])
        self.assertIn("next_steps", saved[0])

    def test_forget_all_command_still_only_archives(self):
        """텔레그램 경로는 여전히 되돌릴 수 있어야 합니다."""
        memory.archive_all_memories()
        self.assertEqual(len(memory.get_recent_memories()), 0)
        self.assertEqual(_count("items"), 5, "보관은 행을 지우지 않습니다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
