"""2026-09-04 일정 기능 버그 리포트(B1~B6)의 회귀 테스트.

각 테스트는 그날 실제로 관측된 입력을 그대로 씁니다. 리포트가 제시한
검증 순서(반복 생성 → 지시문이 새 레코드를 만들지 않는지 → 삭제가 진짜
됐는지 → 긴 리터럴이 안 잘렸는지)를 그대로 따라갑니다.
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = tempfile.TemporaryDirectory()
os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "bugs.db")
os.environ["MARVIS_SECRETARY_DIR"] = str(Path(_TMP_DIR.name) / "no-secretary")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ["MARVIS_ROUTER"] = "regex"  # shadow 스레드가 뜨지 않게

_genai = types.ModuleType("google.generativeai")
_genai.configure = lambda **kwargs: None
_genai.GenerativeModel = lambda name: None
_google = types.ModuleType("google")
_google.generativeai = _genai
sys.modules.setdefault("google", _google)

from marvis import core, memory  # noqa: E402
from marvis.db import init_db  # noqa: E402
from marvis.llm import tools  # noqa: E402
from marvis.schedule_parser import (  # noqa: E402
    detect_instruction,
    detect_message_intent,
    parse_item_refs,
    parse_recurrence_request,
)
from marvis.settings import KST  # noqa: E402
from marvis.voice import split_for_telegram  # noqa: E402


def _reset_database() -> None:
    """테스트 사이에 저장소를 비웁니다."""
    init_db()
    from marvis.db import get_connection

    conn = get_connection()
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM recurrences")
    conn.execute("DELETE FROM events")
    conn.commit()


def _item_count() -> int:
    from marvis.db import get_connection

    return get_connection().execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


# 리포트에 실린 원문 그대로입니다. 요약하면 테스트가 지키는 것이 달라집니다.
INSTRUCTION_FIX_TWO = """Marvis, 두 가지만 고쳐줘.

1) 서버 경로가 잘렸어. 정확한 경로는:
   ubuntu@invest-agent → ~/toss-api/apps/toss-ai-agent
   (앞으로 알림에 이 경로로 적어줘)

2) "2026-11-01 05:10" 알림이 뭔지 확인해줘.
   11/1은 일요일이라 보고서가 안 와.
   내가 원한 건 단발 알림이 아니라,
   평일 반복 알림 시각을 11/2(월)부터 05:10 → 06:10으로
   바꾸는 거야."""

INSTRUCTION_SHOW_RAW = """Marvis, 정리 말고 저장된 원본을 그대로 보여줘.

TAPIoca 관련 알림 전부 나열해줘 —
반복 알림 규칙(요일·시각·변경 예정 포함)과 단발 알림을
저장된 그대로, 날짜·시각까지 적어서."""

INSTRUCTION_LAST_THREE = """Marvis, 마지막 세 개만.

1) 반복 알림 두 개가 목록에 안 보여.
   실제로 저장돼 있으면 그 규칙을 그대로 보여줘
   (요일·시각·시작일·종료일). 없으면 지금 만들어줘.

2) [12] "Marvis, 두 가지만 고쳐줘" (11/1) 지워줘.
   지웠다고 했는데 아직 있어.

3) [14] "Marvis, 세 가지 남았어" (10/30) 지워줘."""


class B1InstructionsAreNotSchedulesTest(unittest.TestCase):
    """B1. 지시 메시지가 일정으로 저장된다 (오탐 등록)."""

    def setUp(self):
        _reset_database()

    def test_fix_request_with_a_date_is_not_saved(self):
        """본문에 '11/1'이 있어도 '고쳐줘'는 일정이 아닙니다."""
        self.assertEqual(detect_message_intent(INSTRUCTION_FIX_TWO), "instruction")
        list(core.handle_message(INSTRUCTION_FIX_TWO, source="telegram"))
        self.assertEqual(_item_count(), 0)

    def test_show_raw_request_is_not_saved(self):
        self.assertEqual(detect_message_intent(INSTRUCTION_SHOW_RAW), "instruction")
        list(core.handle_message(INSTRUCTION_SHOW_RAW, source="telegram"))
        self.assertEqual(_item_count(), 0)

    def test_delete_request_is_not_saved(self):
        self.assertEqual(detect_message_intent(INSTRUCTION_LAST_THREE), "instruction")
        list(core.handle_message(INSTRUCTION_LAST_THREE, source="telegram"))
        self.assertEqual(_item_count(), 0)

    def test_a_message_holding_several_items_is_not_saved_whole(self):
        """분해할 수 없으면 통째로 한 건 만들지 말고 되묻습니다."""
        text = (
            "📌 TAPIoca 일정 등록 부탁해.\n"
            "1) 9/10 (목) 아침 램프 종료 점검\n"
            "2) 10/1 (목) 아침 실거래 판정\n"
            "3) 10/31 뉴스감성 60거래일 도달\n"
        )
        self.assertEqual(detect_message_intent(text), "ambiguous")
        replies = list(core.handle_message(text, source="telegram"))
        self.assertEqual(_item_count(), 0)
        self.assertIn("저장하지 않았습니다", replies[0].text)

    def test_an_ordinary_one_line_schedule_still_saves(self):
        """오탐을 막느라 정상 입력까지 막으면 안 됩니다."""
        self.assertEqual(detect_message_intent("5.25 09:00 GitHub README 정리하기"), "save")
        self.assertEqual(detect_message_intent("내일 9시에 통신사 전화 알려줘"), "save")
        list(core.handle_message("5.25 09:00 GitHub README 정리하기", source="telegram"))
        self.assertEqual(_item_count(), 1)

    def test_ordinary_sentences_with_numbers_are_still_saved(self):
        """오탐을 막는 규칙이 정탐까지 막으면 안 됩니다.

        '12번'을 그 자체로 항목 번호 신호로 삼았더니 "12번 버스 타야 해"가
        지시문으로 분류돼 저장되지 않았습니다. 대괄호 형태만 신호로 씁니다.
        """
        for text in ("12번 버스 타야 해",
                     "내일 3번 출구에서 만나기로 했어",
                     "drawing 연습 내일 할거야",
                     "내일 2시 회의 있어"):
            self.assertIsNone(detect_instruction(text), text)
            self.assertNotEqual(detect_message_intent(text), "instruction", text)

    def test_numbers_with_a_verb_are_still_read_as_refs(self):
        """지시 동사와 함께 나오면 '14번'도 항목 번호로 읽힙니다."""
        self.assertEqual(detect_instruction("14번 지워줘"), "delete")
        self.assertEqual(parse_item_refs("14번 지워줘"), [14])
        self.assertEqual(detect_instruction("[12] 지워줘"), "delete")

    def test_item_refs_ignore_bare_numbers(self):
        """'세 가지 남았어'의 3을 항목 번호로 읽으면 엉뚱한 걸 지웁니다."""
        self.assertEqual(parse_item_refs("[12] 지워줘"), [12])
        self.assertEqual(parse_item_refs("14번 지워줘"), [14])
        self.assertEqual(parse_item_refs("세 가지 남았어 3 4 5"), [])


class B2NoFalseCompletionReportsTest(unittest.TestCase):
    """B2. 하지 않은 쓰기를 했다고 답한다."""

    def setUp(self):
        _reset_database()

    def test_delete_actually_removes_and_is_verified(self):
        item = memory.create_item(content="지울 항목", kind="schedule",
                                  schedule_date="2026-12-01")
        result = memory.delete_item(item["seq"])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["verified"])
        # 다시 읽어서 실제로 조회에서 빠졌는지 봅니다.
        self.assertNotIn(item["seq"], [i["seq"] for i in memory.get_active_schedules()])

    def test_deleting_a_missing_item_reports_failure(self):
        result = memory.delete_item(9999)
        self.assertFalse(result["deleted"])
        self.assertFalse(result["verified"])

    def test_delete_instruction_only_claims_what_it_verified(self):
        kept = memory.create_item(content="남을 항목", kind="schedule",
                                  schedule_date="2026-12-01")
        gone = memory.create_item(content="사라질 항목", kind="schedule",
                                  schedule_date="2026-12-02")

        replies = list(core.handle_message(
            f"[{gone['seq']}] 지워줘, 그리고 [9999]번도 지워줘", source="telegram"
        ))
        text = replies[0].text

        self.assertIn(f"[{gone['seq']}] 지웠습니다", text)
        self.assertIn("[9999] 찾지 못했습니다", text)
        # 없는 번호를 지웠다고 말하지 않습니다.
        self.assertNotIn("[9999] 지웠습니다", text)
        # 지시 메시지 자체가 새 레코드가 되지 않았습니다.
        remaining = [i["seq"] for i in memory.get_active_schedules()]
        self.assertEqual(remaining, [kept["seq"]])

    def test_complete_item_tool_does_not_call_itself_a_deletion(self):
        """완료 처리는 삭제가 아닙니다. 결과가 그 사실을 말해야 합니다."""
        item = memory.create_item(content="완료할 항목", kind="schedule",
                                  schedule_date="2026-12-01")
        result = tools.execute("complete_item", {"seq": item["seq"]})
        self.assertTrue(result["completed"])
        self.assertTrue(result["still_stored"])
        self.assertIn("삭제가 아닙니다", result["note"])
        # 항목은 그대로 남아 있습니다.
        self.assertIsNotNone(memory.get_item(item["seq"]))

    def test_edit_instruction_says_it_changed_nothing(self):
        replies = list(core.handle_message("[7] 경로 고쳐줘", source="telegram"))
        self.assertIn("아무것도 바꾸지 않았습니다", replies[0].text)


class B3RecurringSchedulesAreFirstClassTest(unittest.TestCase):
    """B3. 반복 일정을 못 만들면서 만든 것처럼 답한다."""

    def setUp(self):
        _reset_database()

    def test_weekday_recurrence_is_parsed(self):
        parsed = parse_recurrence_request("평일 아침 05:10 반복 알림")
        self.assertEqual(parsed["weekdays"], [0, 1, 2, 3, 4])
        self.assertEqual(parsed["at_time"], "05:10")

    def test_recurrence_request_creates_a_rule_not_a_one_off(self):
        replies = list(core.handle_message(
            "평일 06:10 TAPIoca 보고서 확인 반복 알림 2026-11-02부터", source="telegram"
        ))
        rules = memory.list_recurrences()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["weekdays"], "0,1,2,3,4")
        self.assertEqual(rules[0]["at_time"], "06:10")
        self.assertEqual(rules[0]["starts_on"], "2026-11-02")
        # 단발 레코드는 하나도 만들지 않았습니다.
        self.assertEqual(_item_count(), 0)
        # 저장된 값을 그대로 되돌려 보여줍니다.
        self.assertIn("월~금", replies[0].text)
        self.assertIn("06:10", replies[0].text)

    def test_the_rule_survives_a_read_back(self):
        """리포트의 검증 ①: 만들고 → 원본 조회로 반복 규칙 필드 확인."""
        core_replies = list(core.handle_message(
            "평일 05:10 보고서 확인 반복 2026-10-30까지", source="telegram"
        ))
        self.assertTrue(core_replies)
        raw = memory.format_schedule_raw()
        self.assertIn("recurrence", raw)
        self.assertIn("월~금", raw)
        self.assertIn("05:10", raw)
        self.assertIn("2026-10-30", raw)

    def test_occurrences_respect_weekday_and_bounds(self):
        rule = memory.create_recurrence(
            content="보고서 확인", weekdays=[0, 1, 2, 3, 4], at_time="06:10",
            starts_on="2026-11-02", ends_on="2026-11-06",
        )
        stored = memory.get_recurrence(rule["seq"])
        # 2026-11-02 는 월요일, 11-07 은 토요일, 11-09 는 종료일 이후입니다.
        self.assertTrue(memory._occurs_on(stored, datetime(2026, 11, 2).date()))
        self.assertFalse(memory._occurs_on(stored, datetime(2026, 11, 7).date()))
        self.assertFalse(memory._occurs_on(stored, datetime(2026, 11, 9).date()))
        # 시작일 이전에도 울리지 않습니다.
        self.assertFalse(memory._occurs_on(stored, datetime(2026, 10, 30).date()))

    def test_due_recurrence_fires_once_per_day(self):
        rule = memory.create_recurrence(
            content="보고서 확인", weekdays=[0, 1, 2, 3, 4], at_time="06:10",
            starts_on="2026-11-02",
        )
        monday_morning = datetime(2026, 11, 2, 6, 15, tzinfo=KST)
        due = memory.get_due_recurrences(monday_morning)
        self.assertEqual([r["seq"] for r in due], [rule["seq"]])

        self.assertTrue(memory.mark_recurrence_fired(rule["id"], "2026-11-02"))
        # 같은 날 다시 검사해도 나오지 않습니다.
        self.assertEqual(memory.get_due_recurrences(monday_morning), [])
        # 두 번째 표시는 실패합니다(자물쇠가 걸려 있습니다).
        self.assertFalse(memory.mark_recurrence_fired(rule["id"], "2026-11-02"))

    def test_a_long_dead_occurrence_does_not_fire(self):
        """오후 3시에 05:10 알림이 오는 것은 알림이 아니라 소음입니다."""
        memory.create_recurrence(
            content="보고서 확인", weekdays=[0, 1, 2, 3, 4], at_time="06:10",
            starts_on="2026-11-02",
        )
        monday_afternoon = datetime(2026, 11, 2, 15, 0, tzinfo=KST)
        self.assertEqual(memory.get_due_recurrences(monday_afternoon), [])

    def test_deleting_a_rule_is_verified(self):
        rule = memory.create_recurrence(
            content="보고서 확인", weekdays=[0], at_time="06:10",
            starts_on="2026-11-02",
        )
        result = memory.delete_recurrence(rule["seq"])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["verified"])
        self.assertEqual(memory.list_recurrences(), [])

    def test_updating_a_rule_reads_back_the_stored_value(self):
        rule = memory.create_recurrence(
            content="보고서 확인", weekdays=[0, 1, 2, 3, 4], at_time="05:10",
            starts_on="2026-09-07",
        )
        result = memory.update_recurrence(rule["seq"], at_time="06:10")
        self.assertTrue(result["verified"])
        self.assertEqual(memory.get_recurrence(rule["seq"])["at_time"], "06:10")


class B4RawOutputMatchesRecordsTest(unittest.TestCase):
    """B4. 요약 출력이 실제 레코드와 다르다."""

    def setUp(self):
        _reset_database()

    def test_a_one_off_is_never_rendered_as_a_recurrence(self):
        memory.create_item(content="단발 하나", kind="schedule",
                           schedule_date="2026-11-01",
                           reminder_at="2026-11-01 05:10:00")
        raw = memory.format_schedule_raw()
        self.assertIn("단발 일정 1건", raw)
        self.assertIn("반복 규칙 0건", raw)
        self.assertIn("저장된 반복 규칙이 없습니다.", raw)

    def test_raw_output_shows_the_record_fields(self):
        item = memory.create_item(content="필드 확인용", kind="schedule",
                                  schedule_date="2026-11-01",
                                  reminder_at="2026-11-01 05:10:00")
        raw = memory.format_schedule_raw()
        self.assertIn(f"[{item['seq']}] schedule", raw)
        self.assertIn("일정일: 2026-11-01", raw)
        self.assertIn("알림: 2026-11-01 05:10:00", raw)

    def test_the_raw_command_bypasses_the_llm(self):
        memory.create_item(content="단발 하나", kind="schedule",
                           schedule_date="2026-11-01")
        replies = list(core.handle_message("!원본", source="telegram"))
        self.assertEqual(len(replies), 1)
        self.assertIn("단발 일정 1건", replies[0].text)

    def test_show_raw_instruction_is_recognised(self):
        self.assertEqual(detect_instruction(INSTRUCTION_SHOW_RAW), "show_raw")


class B5LiteralsArePreservedTest(unittest.TestCase):
    """B5. 사용자가 준 리터럴 문자열을 임의로 자른다."""

    def setUp(self):
        _reset_database()

    def test_a_long_path_survives_a_round_trip(self):
        """리포트의 검증 ④: 긴 경로를 넣고 한 글자도 안 잘렸는지 확인."""
        path = "ubuntu@invest-agent → ~/toss-api/apps/toss-ai-agent"
        command = "cd ~/toss-api/apps/toss-ai-agent && npm run paper:alpha"
        content = f"TAPIoca 램프 종료 점검. {path} / {command}"

        result = tools.execute(
            "save_memory",
            {"content": content, "kind": "schedule", "schedule_date": "2026-12-10"},
        )
        self.assertTrue(result["saved"])
        stored = memory.get_item(result["seq"])["content"]
        self.assertEqual(stored, content)
        self.assertIn("~/toss-api/apps/toss-ai-agent", stored)
        self.assertNotIn("~/toss-api/app ", stored)

    def test_the_tool_contract_forbids_shortening_literals(self):
        """계약이 사라지면 모델은 다시 요약하기 시작합니다."""
        description = tools.REGISTRY["save_memory"].parameters["properties"]["content"]
        self.assertIn("한 글자도", description["description"])

    def test_recurrence_content_is_stored_verbatim(self):
        content = "보고서 확인 — cd ~/toss-api/apps/toss-ai-agent && npm run paper:alpha"
        rule = memory.create_recurrence(
            content=content, weekdays=[0, 1, 2, 3, 4], at_time="06:10",
            starts_on="2026-11-02",
        )
        self.assertEqual(memory.get_recurrence(rule["seq"])["content"], content)


class B6LongRepliesAndQuotaTest(unittest.TestCase):
    """B6. 다중 작업 메시지에서 난 오류 두 가지."""

    def setUp(self):
        _reset_database()

    def test_a_reply_over_the_telegram_limit_is_split_not_dropped(self):
        """4096자를 넘으면 예전에는 전송이 실패하고 답변이 통째로 사라졌습니다."""
        text = "\n".join(f"[{i}] 어떤 일정 내용입니다" for i in range(500))
        self.assertGreater(len(text), 4096)
        chunks = split_for_telegram(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        # 한 글자도 잃지 않습니다.
        self.assertEqual("\n".join(chunks), text)

    def test_a_line_longer_than_the_limit_is_still_sent(self):
        text = "x" * 9000
        chunks = split_for_telegram(text)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_quota_errors_tell_the_user_what_still_works(self):
        error = RuntimeError(
            "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
            "generate_content_free_tier_requests"
        )
        message = core._answer_failure_message(error)
        self.assertIn("한도", message)
        self.assertIn("!원본", message)

    def test_other_errors_say_the_data_is_intact(self):
        message = core._answer_failure_message(RuntimeError("boom"))
        self.assertIn("저장된 내용은 그대로입니다", message)

    def test_the_multi_task_message_creates_nothing(self):
        """리포트 B6의 그 메시지입니다. 두 번 보내도 레코드가 생기지 않습니다."""
        list(core.handle_message(INSTRUCTION_LAST_THREE, source="telegram"))
        list(core.handle_message(INSTRUCTION_LAST_THREE, source="telegram"))
        self.assertEqual(_item_count(), 0)


class ShadowModeIsReadOnlyTest(unittest.TestCase):
    """리포트에는 없지만 B1·B3·B5의 실제 뿌리입니다.

    shadow 는 "로그에만 남는다"고 문서화돼 있었지만 도구를 진짜로 실행했고,
    그래서 모든 메시지가 두 번 저장됐습니다.
    """

    def setUp(self):
        _reset_database()

    def test_dry_run_blocks_writes(self):
        before = _item_count()
        result = tools.execute(
            "save_memory",
            {"content": "shadow 가 쓰면 안 되는 것", "kind": "schedule",
             "schedule_date": "2026-12-01"},
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_call"], "save_memory")
        self.assertEqual(_item_count(), before)

    def test_dry_run_still_validates_arguments(self):
        """무엇을 하려 했는지뿐 아니라 그게 통했을지도 기록에 남아야 합니다."""
        result = tools.execute(
            "save_memory", {"kind": "schedule"}, dry_run=True
        )
        self.assertEqual(result["error"], "missing_argument")

    def test_dry_run_lets_reads_through(self):
        memory.create_item(content="읽기는 됩니다", kind="schedule",
                           schedule_date="2026-12-01")
        result = tools.execute("list_schedule", {}, dry_run=True)
        self.assertEqual(result["count"], 1)
        self.assertNotIn("dry_run", result)

    def test_dry_run_blocks_every_writing_tool(self):
        before = _item_count()
        rule_count = len(memory.list_recurrences())
        for name, spec in tools.REGISTRY.items():
            if not spec.writes:
                continue
            result = tools.execute(name, {}, dry_run=True)
            # 인자가 없어 검증에서 걸리거나, dry_run 으로 막히거나 둘 중 하나.
            # 어느 쪽이든 실행되지는 않았습니다.
            self.assertTrue("dry_run" in result or "error" in result, name)
        self.assertEqual(_item_count(), before)
        self.assertEqual(len(memory.list_recurrences()), rule_count)


class AgentLimitsTest(unittest.TestCase):
    """한 턴이 저장소에 남길 수 있는 양에 상한이 있어야 합니다."""

    def test_tool_call_cap_is_below_the_runaway_that_happened(self):
        from marvis.agent import MAX_TOOL_CALLS

        # 실제로 한 턴에 89번 불렸고 84건이 저장됐습니다.
        self.assertLess(MAX_TOOL_CALLS, 89)


if __name__ == "__main__":
    unittest.main()
