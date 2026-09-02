"""SECRETARY 색인 → projects 테이블 동기화 회귀 테스트.

render.py는 실행하지 않습니다. 색인 JSON을 직접 써 두고 sync(refresh=False)로
읽게 해서, 동기화 규칙만 떼어 놓고 봅니다.

    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = tempfile.TemporaryDirectory()
os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "secretary.db")
os.environ["MARVIS_SECRETARY_DIR"] = str(Path(_TMP_DIR.name) / "SECRETARY")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from marvis import secretary  # noqa: E402
from marvis.db import get_connection, init_db  # noqa: E402
from marvis.projects import (  # noqa: E402
    add_project,
    get_briefing_projects,
    load_projects,
    update_project,
)


def _record(path, name, status="진행중", next_=None, blockers=None, summary=""):
    return {
        "path": path,
        "file": f"{path}/_STATUS.md",
        "name": name,
        "status": status,
        "role": "개인",
        "updated": "2026-09-01",
        "summary": summary,
        "next": next_ or [],
        "blockers": blockers or [],
        "stack": [],
        "repo": "",
        "mtime": "2026-09-01 10:00:00",
    }


def _write_index(records):
    secretary.INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    secretary.INDEX_FILE.write_text(
        json.dumps({
            "generated_at": "2026-09-01 10:00:00",
            "root": "/tmp/project",
            "projects": records,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


class SecretarySyncTest(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM events")
        if secretary.INDEX_FILE.exists():
            secretary.INDEX_FILE.unlink()

    def test_missing_index_is_skipped_not_an_error(self):
        """SECRETARY가 없어도 브리핑까지 같이 죽으면 안 됩니다."""
        result = secretary.sync(refresh=False)
        self.assertIn("skipped", result)
        self.assertEqual(result["added"], 0)

    def test_first_sync_adds_every_project(self):
        _write_index([
            _record("a/Alpha", "Alpha"),
            _record("b/Beta", "Beta", status="종료"),
        ])
        result = secretary.sync(refresh=False)
        self.assertEqual((result["added"], result["total"]), (2, 2))
        self.assertEqual(len(load_projects()), 2)

    def test_second_sync_changes_nothing(self):
        _write_index([_record("a/Alpha", "Alpha")])
        secretary.sync(refresh=False)
        result = secretary.sync(refresh=False)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual((result["added"], result["updated"]), (0, 0))

    def test_renaming_a_project_updates_instead_of_duplicating(self):
        """이름이 아니라 경로가 조인 키라서, 이름을 바꿔도 같은 행입니다."""
        _write_index([_record("a/Alpha", "Alpha")])
        secretary.sync(refresh=False)
        _write_index([_record("a/Alpha", "Alpha (개편)")])
        result = secretary.sync(refresh=False)

        self.assertEqual((result["added"], result["updated"]), (0, 1))
        projects = load_projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "Alpha (개편)")

    def test_disappearing_status_file_archives_instead_of_deleting(self):
        _write_index([_record("a/Alpha", "Alpha"), _record("b/Beta", "Beta")])
        secretary.sync(refresh=False)

        _write_index([_record("a/Alpha", "Alpha")])
        result = secretary.sync(refresh=False)

        self.assertEqual(result["archived"], 1)
        self.assertEqual([p["name"] for p in load_projects()], ["Alpha"])
        # 지운 게 아니라 보관한 것이므로 행은 남아 있어야 합니다.
        total = get_connection().execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        self.assertEqual(total, 2)

    def test_reappearing_status_file_unarchives(self):
        _write_index([_record("a/Alpha", "Alpha")])
        secretary.sync(refresh=False)
        _write_index([])
        secretary.sync(refresh=False)
        _write_index([_record("a/Alpha", "Alpha")])
        secretary.sync(refresh=False)

        self.assertEqual([p["name"] for p in load_projects()], ["Alpha"])

    def test_sync_never_resurrects_a_muted_project(self):
        """'브리핑에서 빼줘'는 SECRETARY가 모르는 Marvis 쪽 취향입니다."""
        _write_index([_record("a/Alpha", "Alpha")])
        secretary.sync(refresh=False)
        update_project(load_projects()[0]["seq"], muted_from_briefing=True)

        # 내용이 바뀌어 UPDATE 경로를 확실히 타게 합니다.
        _write_index([_record("a/Alpha", "Alpha", next_=["새 할 일"])])
        secretary.sync(refresh=False)

        self.assertEqual(get_briefing_projects(), [])
        self.assertTrue(load_projects()[0]["muted_from_briefing"])

    def test_manually_added_projects_are_left_alone(self):
        """status_path가 NULL인 행은 SECRETARY 소관이 아닙니다."""
        add_project("손으로 만든 것")
        _write_index([_record("a/Alpha", "Alpha")])
        secretary.sync(refresh=False)

        names = sorted(p["name"] for p in load_projects())
        self.assertEqual(names, ["Alpha", "손으로 만든 것"])

        _write_index([])
        secretary.sync(refresh=False)
        self.assertEqual([p["name"] for p in load_projects()], ["손으로 만든 것"])

    def test_only_in_progress_reaches_the_morning_briefing(self):
        _write_index([
            _record("a/Alpha", "Alpha", status="진행중"),
            _record("b/Beta", "Beta", status="관찰중"),
            _record("c/Gamma", "Gamma", status="멈춤"),
            _record("d/Delta", "Delta", status="종료"),
        ])
        secretary.sync(refresh=False)

        self.assertEqual([p["name"] for p in get_briefing_projects()], ["Alpha"])

    def test_original_secretary_state_survives_in_sub_status(self):
        """4단계를 2단계로 좁히되, 물어보면 답할 수 있게 남겨 둡니다."""
        _write_index([_record("b/Beta", "Beta", status="관찰중")])
        secretary.sync(refresh=False)

        beta = load_projects()[0]
        self.assertEqual((beta["status"], beta["sub_status"]), ("중단", "관찰중"))

    def test_blocker_wins_over_next_step(self):
        _write_index([_record("a/Alpha", "Alpha",
                              next_=["다음 할 일"], blockers=["막힌 것"])])
        secretary.sync(refresh=False)

        self.assertEqual(load_projects()[0]["next_steps"], "[막힘] 막힌 것")

    def test_first_next_step_is_used_when_nothing_is_blocked(self):
        _write_index([_record("a/Alpha", "Alpha", next_=["첫 번째", "두 번째"])])
        secretary.sync(refresh=False)

        self.assertEqual(load_projects()[0]["next_steps"], "첫 번째")

    def test_empty_frontmatter_lists_do_not_become_empty_strings(self):
        _write_index([_record("a/Alpha", "Alpha", next_=["", "  "], blockers=[""])])
        secretary.sync(refresh=False)

        self.assertIsNone(load_projects()[0]["next_steps"])

    def test_records_without_a_path_are_ignored(self):
        _write_index([_record("", "이름만 있는 것"), _record("a/Alpha", "Alpha")])
        result = secretary.sync(refresh=False)

        self.assertEqual(result["total"], 1)
        self.assertEqual([p["name"] for p in load_projects()], ["Alpha"])

    def test_malformed_index_is_skipped_not_raised(self):
        secretary.INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        secretary.INDEX_FILE.write_text("{ 이건 JSON이 아닙니다", encoding="utf-8")

        result = secretary.sync(refresh=False)
        self.assertIn("skipped", result)

    def test_sync_logs_one_event_not_one_per_project(self):
        """이벤트 로그는 eval 데이터셋이라 동기화 로그로 덮이면 안 됩니다."""
        _write_index([_record(f"p/{i}", f"P{i}") for i in range(10)])
        secretary.sync(refresh=False)

        rows = get_connection().execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'projects.synced'"
        ).fetchone()["n"]
        self.assertEqual(rows, 1)

    def test_sync_without_changes_logs_nothing(self):
        _write_index([_record("a/Alpha", "Alpha")])
        secretary.sync(refresh=False)
        before = get_connection().execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

        secretary.sync(refresh=False)
        after = get_connection().execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
