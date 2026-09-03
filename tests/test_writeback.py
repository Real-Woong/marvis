"""_STATUS.md write-back 회귀 테스트.

두 층으로 나눕니다.

* 텍스트 수술(_set_scalar 등)은 어디서든 돕니다. 부수효과가 없습니다.
* 전체 루프는 진짜 SECRETARY/render.py 를 임시 트리에 복사해 돌립니다.
  "우리가 고친 텍스트를 그 파서가 같은 뜻으로 읽는가"는 진짜 파서로만
  확인할 수 있기 때문입니다. render.py 가 없는 기계(우분투 등)에서는
  건너뜁니다.

    python -m unittest discover -s tests
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = tempfile.TemporaryDirectory()
_ROOT = Path(_TMP_DIR.name) / "project"
_SECRETARY = _ROOT / "SECRETARY"
_SECRETARY.mkdir(parents=True, exist_ok=True)

os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "writeback.db")
os.environ["MARVIS_SECRETARY_DIR"] = str(_SECRETARY)
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from marvis import secretary  # noqa: E402
from marvis.db import get_connection, init_db  # noqa: E402
from marvis.projects import (  # noqa: E402
    STATUS_IN_PROGRESS,
    STATUS_STOPPED,
    add_project,
    get_briefing_projects,
    load_projects,
    update_project,
)
from marvis.secretary import (  # noqa: E402
    WriteBackError,
    _set_list,
    _set_scalar,
    _set_summary,
    _to_secretary_status,
)
from marvis.time_utils import today_kst_date  # noqa: E402

# 이 저장소가 클론된 기계에는 SECRETARY 가 없을 수 있습니다.
REAL_RENDER = Path.home() / "Desktop/project/SECRETARY/render.py"

STATUS_MD = """---
name: Alpha
status: 진행중          # 진행중 | 관찰중 | 멈춤 | 종료
role: 개인
updated: 2026-01-01
stack: [python, sqlite]
links: { repo: https://example.com/alpha, deploy: }
next:
  - 첫 번째 할 일
  - 두 번째 할 일
blockers:
  -
---
## 한 줄 요약
원래 요약입니다.

## 최근 진행
- 뭔가 했다

## 자주 쓰는 명령 / 접속
`make run`
"""


class TextSurgeryTest(unittest.TestCase):
    """프론트매터를 고치는 문자열 조작만 떼어 봅니다."""

    def test_scalar_replaces_value_and_keeps_the_comment(self):
        result = _set_scalar("status: 진행중          # 진행중 | 멈춤\n", "status", "멈춤")
        self.assertIn("# 진행중 | 멈춤", result)
        self.assertTrue(result.startswith("status: 멈춤"))

    def test_scalar_appends_when_the_key_is_missing(self):
        self.assertEqual(_set_scalar("name: A\n", "updated", "2026-09-03"),
                         "name: A\nupdated: 2026-09-03\n")

    def test_scalar_touches_only_the_named_key(self):
        result = _set_scalar("name: A\nstatus: 진행중\n", "status", "종료")
        self.assertEqual(result, "name: A\nstatus: 종료\n")

    def test_list_replaces_the_whole_block(self):
        result = _set_list("next:\n  - 하나\n  - 둘\nblockers:\n  - 막힘\n", "next", ["새것"])
        self.assertEqual(result, "next:\n  - 새것\nblockers:\n  - 막힘\n")

    def test_empty_list_leaves_the_key_with_no_items(self):
        result = _set_list("blockers:\n  - 막힘\n", "blockers", [])
        self.assertEqual(result, "blockers:\n")

    def test_list_appends_when_the_key_is_missing(self):
        self.assertEqual(_set_list("name: A\n", "next", ["할 일"]),
                         "name: A\nnext:\n  - 할 일\n")

    def test_summary_replaces_only_its_own_section(self):
        body = "## 한 줄 요약\n예전 요약\n\n## 최근 진행\n- 남아야 한다\n"
        result = _set_summary(body, "새 요약")
        self.assertIn("새 요약", result)
        self.assertNotIn("예전 요약", result)
        self.assertIn("## 최근 진행\n- 남아야 한다", result)

    def test_summary_inserts_the_heading_when_missing(self):
        result = _set_summary("## 최근 진행\n- 뭔가\n", "새 요약")
        self.assertTrue(result.startswith("## 한 줄 요약\n새 요약"))
        self.assertIn("## 최근 진행", result)

    def test_stopped_maps_back_to_a_secretary_state(self):
        self.assertEqual(_to_secretary_status(STATUS_IN_PROGRESS, None), "진행중")
        self.assertEqual(_to_secretary_status(STATUS_STOPPED, "관찰중"), "관찰중")
        self.assertEqual(_to_secretary_status(STATUS_STOPPED, "종료"), "종료")

    def test_marvis_only_tags_fall_back_to_dormant(self):
        """'일시정지'는 SECRETARY에 없는 Marvis 고유 태그입니다."""
        self.assertEqual(_to_secretary_status(STATUS_STOPPED, "일시정지"), "멈춤")
        self.assertEqual(_to_secretary_status(STATUS_STOPPED, None), "멈춤")


@unittest.skipUnless(REAL_RENDER.is_file(), f"SECRETARY/render.py 없음: {REAL_RENDER}")
class WriteBackLoopTest(unittest.TestCase):
    """고친다 → render.py 로 다시 읽는다 → DB 를 맞춘다, 전 구간."""

    @classmethod
    def setUpClass(cls):
        # 파서는 하나뿐이어야 하므로 흉내내지 않고 진짜를 복사해 씁니다.
        shutil.copy2(REAL_RENDER, _SECRETARY / "render.py")

        # settings.SECRETARY_DIR은 최초 import 때 한 번 정해집니다. 한 프로세스에서
        # 테스트 모듈을 여러 개 돌리면 먼저 import된 쪽의 환경 변수가 이깁니다.
        # 이 테스트는 특정 트리를 봐야 하므로 모듈 상수를 직접 갈아끼웁니다.
        cls._saved = (secretary.SECRETARY_DIR, secretary.INDEX_FILE,
                      secretary.RENDER_SCRIPT)
        secretary.SECRETARY_DIR = _SECRETARY
        secretary.INDEX_FILE = _SECRETARY / "_index" / "all_status.json"
        secretary.RENDER_SCRIPT = _SECRETARY / "render.py"

    @classmethod
    def tearDownClass(cls):
        (secretary.SECRETARY_DIR, secretary.INDEX_FILE,
         secretary.RENDER_SCRIPT) = cls._saved

    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM events")

        self.project_dir = _ROOT / "Alpha"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.status_md = self.project_dir / "_STATUS.md"
        self.status_md.write_text(STATUS_MD, encoding="utf-8")

        secretary._last_refresh_at = None
        secretary.sync(force=True)
        self.seq = load_projects()[0]["seq"]

    def _file(self) -> str:
        return self.status_md.read_text(encoding="utf-8")

    def _row(self) -> dict:
        return load_projects()[0]

    # ------------------------------------------------------------ 쓰기

    def test_next_steps_lands_in_the_file_not_just_the_database(self):
        update_project(self.seq, next_steps="새 할 일")

        self.assertIn("next:\n  - 새 할 일\n", self._file())
        self.assertEqual(self._row()["next_steps"], "새 할 일")

    def test_writing_next_steps_replaces_the_whole_list(self):
        update_project(self.seq, next_steps="하나만 남는다")

        self.assertNotIn("두 번째 할 일", self._file())

    def test_the_edit_survives_the_next_sync(self):
        """이게 write-back 이 존재하는 이유입니다.

        예전에는 DB만 고쳐서, 다음 동기화가 파일 내용으로 덮어썼습니다.
        """
        update_project(self.seq, next_steps="살아남아야 한다")
        secretary.sync(force=True)

        self.assertEqual(self._row()["next_steps"], "살아남아야 한다")

    def test_writing_bumps_the_updated_field(self):
        update_project(self.seq, next_steps="아무거나")

        self.assertIn(f"updated: {today_kst_date().isoformat()}", self._file())

    def test_the_status_comment_survives(self):
        update_project(self.seq, status=STATUS_STOPPED)

        self.assertIn("# 진행중 | 관찰중 | 멈춤 | 종료", self._file())

    def test_stopping_writes_a_state_the_parser_knows(self):
        update_project(self.seq, status=STATUS_STOPPED)

        self.assertIn("status: 멈춤", self._file())
        row = self._row()
        self.assertEqual((row["status"], row["sub_status"]), (STATUS_STOPPED, "멈춤"))

    def test_resuming_clears_the_sub_status(self):
        update_project(self.seq, status=STATUS_STOPPED)
        update_project(self.seq, status=STATUS_IN_PROGRESS)

        self.assertIn("status: 진행중", self._file())
        row = self._row()
        self.assertEqual((row["status"], row["sub_status"]), (STATUS_IN_PROGRESS, None))

    def test_an_existing_sub_status_is_kept_when_not_given_again(self):
        """'관찰중'인 프로젝트의 다음 할 일만 고쳤다고 '멈춤'이 되면 안 됩니다."""
        update_project(self.seq, status=STATUS_STOPPED, sub_status="관찰중")
        update_project(self.seq, next_steps="상태는 그대로", status=STATUS_STOPPED)

        self.assertIn("status: 관찰중", self._file())
        self.assertEqual(self._row()["sub_status"], "관찰중")

    def test_note_replaces_the_summary_section_only(self):
        update_project(self.seq, note="새 한 줄 요약")

        text = self._file()
        self.assertIn("## 한 줄 요약\n새 한 줄 요약", text)
        self.assertNotIn("원래 요약입니다", text)
        self.assertIn("## 최근 진행\n- 뭔가 했다", text)
        self.assertIn("`make run`", text)
        self.assertEqual(self._row()["note"], "새 한 줄 요약")

    # ------------------------------------------------------------ 막힌 것

    def test_a_blocker_takes_over_the_briefing_line(self):
        update_project(self.seq, blockers=["열쇠가 없다"])

        self.assertIn("blockers:\n  - 열쇠가 없다\n", self._file())
        self.assertEqual(self._row()["next_steps"], "[막힘] 열쇠가 없다")

    def test_clearing_blockers_gives_the_next_step_back(self):
        update_project(self.seq, blockers=["열쇠가 없다"])
        update_project(self.seq, blockers=[])

        self.assertEqual(self._row()["next_steps"], "첫 번째 할 일")
        self.assertNotIn("열쇠가 없다", self._file())

    def test_a_blocked_project_still_reaches_the_briefing(self):
        update_project(self.seq, blockers=["막혔다"])

        briefed = [p["next_steps"] for p in get_briefing_projects()]
        self.assertEqual(briefed, ["[막힘] 막혔다"])

    # ------------------------------------------------------------ 실패

    def test_a_missing_file_changes_nothing(self):
        self.status_md.unlink()
        before = self._row()["next_steps"]

        with self.assertRaises(WriteBackError):
            update_project(self.seq, next_steps="쓰이면 안 된다")

        self.assertEqual(self._row()["next_steps"], before)

    def test_a_file_without_frontmatter_is_left_alone(self):
        self.status_md.write_text("프론트매터가 없습니다.\n", encoding="utf-8")

        with self.assertRaises(WriteBackError):
            update_project(self.seq, next_steps="쓰이면 안 된다")

        self.assertEqual(self._file(), "프론트매터가 없습니다.\n")

    def test_a_failed_write_leaves_no_temporary_file(self):
        self.status_md.write_text("프론트매터가 없습니다.\n", encoding="utf-8")
        with self.assertRaises(WriteBackError):
            update_project(self.seq, next_steps="쓰이면 안 된다")

        leftovers = list(self.project_dir.glob("*.marvis-tmp"))
        self.assertEqual(leftovers, [])

    # ------------------------------------------------------------ 경계

    def test_muting_never_touches_the_file(self):
        """브리핑 제외는 SECRETARY가 모르는 Marvis 쪽 취향입니다."""
        before = self._file()
        update_project(self.seq, muted_from_briefing=True)

        self.assertEqual(self._file(), before)
        self.assertTrue(self._row()["muted_from_briefing"])

    def test_manually_added_projects_still_go_straight_to_the_database(self):
        manual = add_project("손으로 만든 것")
        self.assertTrue(update_project(manual["seq"], next_steps="DB에만 남는다"))

        row = next(p for p in load_projects() if p["seq"] == manual["seq"])
        self.assertEqual(row["next_steps"], "DB에만 남는다")
        self.assertIsNone(row["status_path"])

    def test_updating_a_missing_project_returns_false(self):
        self.assertFalse(update_project(9999, next_steps="아무거나"))

    def test_one_event_per_update(self):
        update_project(self.seq, next_steps="한 번")
        rows = get_connection().execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'project.updated'"
        ).fetchone()["n"]
        self.assertEqual(rows, 1)


if __name__ == "__main__":
    unittest.main()
