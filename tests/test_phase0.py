"""Phase 0 회귀 테스트.

핵심은 "예전 구조에서 실제로 틀렸던 시나리오"를 그대로 재현해서 이제는 맞게
동작하는지 보는 것입니다. 외부 의존성(Gemini, Telegram, gTTS)은 import 시점에
스텁으로 대체하므로 네트워크 없이 실행됩니다.

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# marvis.settings를 처음 import 하기 전에 테스트용 DB 경로를 잡아야 합니다.
_TMP_DIR = tempfile.TemporaryDirectory()
os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "test.db")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

# ai.py가 import하는 레거시 SDK를 스텁으로 대체합니다.
_genai = types.ModuleType("google.generativeai")
_genai.configure = lambda **kwargs: None
_genai.GenerativeModel = lambda name: None
_google = types.ModuleType("google")
_google.generativeai = _genai
sys.modules.setdefault("google", _google)
sys.modules["google.generativeai"] = _genai

from marvis import core, memory, projects  # noqa: E402
from marvis.db import get_connection, init_db  # noqa: E402
from marvis.reminders import briefing_time_for, send_morning_briefing_if_due  # noqa: E402
from marvis.time_utils import now_kst  # noqa: E402


def _reset_db():
    init_db()
    conn = get_connection()
    for table in ("items", "projects", "events", "config"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


class StableNumberingTest(unittest.TestCase):
    """B1: 저장할 때마다 번호가 다시 매겨지던 문제."""

    def setUp(self):
        _reset_db()

    def test_seq_survives_archiving(self):
        first = memory.add_memory("2020-01-01 지난 일정 확인해야 해")
        second = memory.add_memory("내일 병원 예약 확인해야 해")
        third = memory.add_memory("책 읽기 아이디어가 생각났어")

        self.assertEqual([first["seq"], second["seq"], third["seq"]], [1, 2, 3])

        # 지난 일정 하나가 보관 처리돼도 뒤 항목의 번호는 그대로여야 합니다.
        archived = memory.archive_past_schedules()
        self.assertEqual(archived, 1)

        self.assertTrue(memory.mark_done(3))
        row = get_connection().execute(
            "SELECT content, done FROM items WHERE seq = 3"
        ).fetchone()
        self.assertEqual(row["content"], third["content"])
        self.assertEqual(row["done"], 1)

    def test_seq_is_never_reused(self):
        memory.add_memory("첫 번째 메모")
        memory.archive_all_memories()
        fresh = memory.add_memory("두 번째 메모")
        self.assertEqual(fresh["seq"], 2)


class ReminderTargetingTest(unittest.TestCase):
    """B1의 가장 나쁜 증상: 엉뚱한 항목에 '알림 보냄'이 찍히던 문제."""

    def setUp(self):
        _reset_db()

    def test_mark_reminded_hits_the_same_item_after_archiving(self):
        stale = memory.add_memory("2020-05-01 09:00 지난 일정")
        target = memory.add_memory("오늘 08:00 약 먹기")
        other = memory.add_memory("오늘 09:00 스트레칭")

        # 예전 코드에서 번호가 밀리게 만들던 바로 그 상황입니다.
        memory.archive_past_schedules()

        self.assertTrue(memory.mark_reminded(target["id"]))

        rows = {
            row["id"]: row["reminded"]
            for row in get_connection().execute("SELECT id, reminded FROM items")
        }
        self.assertEqual(rows[target["id"]], 1)
        self.assertEqual(rows[other["id"]], 0)
        self.assertEqual(rows[stale["id"]], 0)

    def test_due_reminders_exclude_done_and_archived(self):
        due = memory.add_memory("오늘 07:00 물 마시기")
        memory.add_memory("2020-01-01 08:00 지난 일정")
        memory.archive_past_schedules()

        ids = [item["id"] for item in memory.get_due_reminders("2099-01-01 00:00:00")]
        self.assertIn(due["id"], ids)
        self.assertEqual(len(ids), 1)


class ArchiveNotDeleteTest(unittest.TestCase):
    """B2: 지난 일정을 지워서 히스토리가 사라지던 문제."""

    def setUp(self):
        _reset_db()

    def test_past_schedule_is_kept_in_the_database(self):
        memory.add_memory("2020-03-03 세미나 참석해야 해")
        memory.archive_past_schedules()

        self.assertEqual(len(memory.get_active_schedules()), 0)
        total = get_connection().execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        self.assertEqual(total, 1, "보관 처리된 항목이 DB에 남아 있어야 합니다")

    def test_forget_all_archives_rather_than_deletes(self):
        memory.add_memory("메모 하나")
        memory.add_memory("메모 둘")
        count = memory.archive_all_memories()

        self.assertEqual(count, 2)
        self.assertEqual(len(memory.get_recent_memories()), 0)
        total = get_connection().execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        self.assertEqual(total, 2)


class SingleEntryPointTest(unittest.TestCase):
    """B3: 텔레그램과 Siri 경로가 서로 어긋나 있던 문제."""

    def setUp(self):
        _reset_db()
        # LLM 호출은 결정적인 문자열로 대체합니다.
        self._real_ask = core.ask_gemini
        core.ask_gemini = lambda text: f"[답변] {text}"

    def tearDown(self):
        core.ask_gemini = self._real_ask

    def test_schedule_command_works_from_both_sources(self):
        memory.add_memory("내일 오후 3시에 병원 예약 확인해야 해")

        for source in (core.SOURCE_TELEGRAM, core.SOURCE_SIRI):
            replies = list(core.handle_message("!스케쥴", source=source))
            self.assertEqual(len(replies), 1, source)
            self.assertIn("병원", replies[0].text, source)
            self.assertTrue(replies[0].speak, source)

    def test_project_action_works_from_siri(self):
        replies = list(
            core.handle_message("테스트프로젝트 새로 추가해줘", source=core.SOURCE_SIRI)
        )
        self.assertEqual(len(replies), 1)
        self.assertIn("등록했습니다", replies[0].text)
        self.assertEqual(len(projects.load_projects()), 1)

    def test_save_flow_yields_ack_then_answer(self):
        replies = list(core.handle_message("내일 병원 예약 확인해야 해"))
        self.assertEqual(len(replies), 2)
        self.assertTrue(replies[0].ack)
        self.assertFalse(replies[1].ack)
        self.assertTrue(replies[1].speak)

    def test_every_turn_is_logged(self):
        core_replies = list(core.handle_message("내일 병원 예약 확인해야 해"))
        self.assertTrue(core_replies)

        row = get_connection().execute(
            "SELECT payload FROM events WHERE kind = 'turn.handled'"
        ).fetchone()
        self.assertIsNotNone(row, "발화가 이벤트 로그에 남아야 합니다")
        self.assertIn("병원", row["payload"])


class ProjectStorageTest(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_update_by_seq_and_status_reset(self):
        created = projects.add_project("Marvis", status=projects.STATUS_IN_PROGRESS)
        self.assertTrue(
            projects.update_project(
                created["seq"], status=projects.STATUS_STOPPED, sub_status="일시정지"
            )
        )
        stopped = projects.get_project(created["seq"])
        self.assertEqual(stopped["status"], projects.STATUS_STOPPED)
        self.assertEqual(stopped["sub_status"], "일시정지")

        # 다시 진행중이 되면 중단 사유 태그는 지워집니다.
        projects.update_project(created["seq"], status=projects.STATUS_IN_PROGRESS)
        resumed = projects.get_project(created["seq"])
        self.assertIsNone(resumed["sub_status"])

    def test_update_missing_project_returns_false(self):
        self.assertFalse(projects.update_project(999, status=projects.STATUS_STOPPED))


class BriefingWindowTest(unittest.TestCase):
    """봇이 아침에 꺼져 있다가 오후에 켜지면 '좋은 아침'을 보내던 문제."""

    def setUp(self):
        _reset_db()
        self.sent = []
        import marvis.reminders as reminders

        self.reminders = reminders
        self._real_send = reminders.send_proactive_telegram_message
        self._real_briefing = reminders.generate_morning_briefing
        reminders.send_proactive_telegram_message = lambda text: self.sent.append(text) or True
        reminders.generate_morning_briefing = lambda: "오늘 아침 일정은 없습니다."

    def tearDown(self):
        self.reminders.send_proactive_telegram_message = self._real_send
        self.reminders.generate_morning_briefing = self._real_briefing

    def _weekday_at(self, hour, minute):
        current = now_kst().replace(hour=hour, minute=minute, second=0, microsecond=0)
        while briefing_time_for(current) != (8, 30):
            current = current.replace(day=current.day + 1)
        return current

    def test_no_briefing_long_after_the_scheduled_time(self):
        send_morning_briefing_if_due(self._weekday_at(14, 0))
        self.assertEqual(self.sent, [], "오후에 '좋은 아침'이 나가면 안 됩니다")

    def test_briefing_inside_the_window(self):
        send_morning_briefing_if_due(self._weekday_at(8, 35))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("좋은 아침", self.sent[0])

    def test_sunday_has_no_briefing(self):
        current = now_kst()
        while current.weekday() != 6:
            current = current.replace(day=current.day + 1)
        self.assertIsNone(briefing_time_for(current))


if __name__ == "__main__":
    unittest.main(verbosity=2)
