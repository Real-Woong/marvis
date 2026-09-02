"""shadow 기간에 쌓인 두 라우터의 판단 차이를 사람이 읽을 수 있게 정리합니다.

    python -m marvis.shadow_report                  # 요약 + 불일치 유형
    python -m marvis.shadow_report --detail         # 발화까지 전부
    python -m marvis.shadow_report --since 7        # 최근 7일만
    python -m marvis.shadow_report --export         # 골든셋 후보 파일 생성

같은 불일치가 반복되기 때문에(예: 정규식은 chat인데 LLM은 save_memory) 낱개
행을 훑는 대신 유형별로 묶어서 보여줍니다. 12건짜리 유형 하나를 한 번 판단하면
12건이 정리됩니다.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from .db import get_connection, init_db
from .settings import BASE_DIR
from .time_utils import now_kst

CANDIDATES_FILE = BASE_DIR / "evals" / "candidates.jsonl"

# 정규식 경로를 사람이 읽을 수 있게.
ROUTE_LABELS = {
    "save": "저장",
    "query": "조회",
    "chat": "대화(도구 없음)",
    "project": "프로젝트",
    "command.schedule": "!스케쥴",
    "command.projects": "!프로젝트",
}


def _rows(kinds: tuple[str, ...], since_days: int | None):
    clauses = ["kind IN (%s)" % ",".join("?" * len(kinds))]
    params: list = list(kinds)
    if since_days:
        cutoff = (now_kst() - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")
        clauses.append("at >= ?")
        params.append(cutoff)
    return get_connection().execute(
        f"SELECT at, kind, payload FROM events WHERE {' AND '.join(clauses)} ORDER BY at",
        params,
    ).fetchall()


def _load(since_days: int | None):
    agreed, disagreed = [], []
    for row in _rows(("router.agreed", "router.disagreed"), since_days):
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        payload["at"] = row["at"]
        (agreed if row["kind"] == "router.agreed" else disagreed).append(payload)
    return agreed, disagreed


def _label(route: str | None) -> str:
    return ROUTE_LABELS.get(route, route or "—")


def _tool(name: str | None) -> str:
    return name or "도구 없음"


def print_summary(agreed: list, disagreed: list, since_days: int | None) -> None:
    total = len(agreed) + len(disagreed)
    window = f"최근 {since_days}일" if since_days else "전체 기간"
    print(f"\n{'=' * 66}")
    print(f"shadow 리포트 · {window}")
    print("=" * 66)

    if not total:
        print("\n비교 기록이 아직 없습니다.")
        print("MARVIS_ROUTER=shadow 로 돌고 있는지, 텔레그램 메시지를 보냈는지 확인하세요.")
        return

    rate = 100 * len(agreed) / total
    print(f"\n  비교한 발화   {total}건")
    print(f"  일치         {len(agreed)}건  ({rate:.1f}%)")
    print(f"  불일치       {len(disagreed)}건  ({100 - rate:.1f}%)")


def print_patterns(disagreed: list) -> None:
    """불일치를 (정규식 판단 → LLM 판단) 유형으로 묶습니다."""
    if not disagreed:
        return

    groups: dict[tuple, list] = defaultdict(list)
    for item in disagreed:
        groups[(item.get("regex_route"), item.get("llm_tool"))].append(item)

    print(f"\n{'-' * 66}")
    print("불일치 유형 (많은 순)")
    print("-" * 66)

    for (route, tool), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  [{len(items):>3}건]  정규식: {_label(route):<16} →  LLM: {_tool(tool)}")
        for item in items[:3]:
            print(f"          \"{item.get('text', '')[:52]}\"")
        if len(items) > 3:
            print(f"          … 외 {len(items) - 3}건")


def print_detail(disagreed: list) -> None:
    if not disagreed:
        return
    print(f"\n{'-' * 66}")
    print("불일치 전체")
    print("-" * 66)
    for index, item in enumerate(disagreed, start=1):
        print(f"\n{index}. [{item.get('at', '')}] \"{item.get('text', '')}\"")
        print(f"   정규식  {_label(item.get('regex_route'))}")
        print(f"   LLM     {_tool(item.get('llm_tool'))}")
        if item.get("llm_args"):
            print(f"           인자 {json.dumps(item['llm_args'], ensure_ascii=False)}")
        if item.get("llm_reply"):
            print(f"           답변 \"{item['llm_reply'][:70]}\"")


def print_traces(since_days: int | None) -> None:
    """LLM 경로의 지연과 토큰. 전환 전에 비용·속도를 가늠하는 용도입니다."""
    latencies, input_tokens, output_tokens, errors = [], 0, 0, Counter()
    steps = Counter()

    for row in _rows(("turn.trace",), since_days):
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("elapsed_ms"):
            latencies.append(payload["elapsed_ms"])
        input_tokens += payload.get("input_tokens") or 0
        output_tokens += payload.get("output_tokens") or 0
        if payload.get("error"):
            errors[payload["error"].split(":")[0]] += 1
        steps[payload.get("steps", 0)] += 1

    if not latencies:
        return

    latencies.sort()
    median = latencies[len(latencies) // 2]
    p90 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))]

    print(f"\n{'-' * 66}")
    print("LLM 경로 실측")
    print("-" * 66)
    print(f"\n  턴 수         {len(latencies)}")
    print(f"  지연 중앙값    {median:,}ms")
    print(f"  지연 p90      {p90:,}ms")
    print(f"  입력 토큰     {input_tokens:,}")
    print(f"  출력 토큰     {output_tokens:,}")
    print(f"  단계 분포     {dict(sorted(steps.items()))}")
    if errors:
        print(f"  오류          {dict(errors)}")


def export_candidates(disagreed: list) -> Path:
    """골든셋 후보를 별도 파일로 씁니다.

    golden.jsonl에 직접 쓰지 않습니다. LLM의 판단을 그대로 정답으로 넣으면
    모델을 자기 답으로 채점하게 되고, 그건 점수가 아니라 착시입니다.
    사람이 _review.confirmed를 채우고 옮겨 붙여야 합니다.
    """
    CANDIDATES_FILE.parent.mkdir(exist_ok=True)
    seen: set[str] = set()
    lines = []

    for item in disagreed:
        text = (item.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)

        at = item.get("at") or ""
        case = {
            "id": f"shadow-{len(lines) + 1:03d}",
            "utterance": text,
            "today": at[:10],
            "now": at,
            "expect": {"tool": item.get("llm_tool")},
            "tags": [],
            "_review": {
                "confirmed": False,
                "regex_said": item.get("regex_route"),
                "llm_said": item.get("llm_tool"),
                "llm_args": item.get("llm_args"),
                "note": "expect를 직접 확인해서 고치고 tags를 채운 뒤 golden.jsonl로 옮기세요.",
            },
        }
        lines.append(json.dumps(case, ensure_ascii=False))

    with CANDIDATES_FILE.open("w", encoding="utf-8") as file:
        file.write(
            "// shadow 불일치에서 뽑은 골든셋 후보. 아직 정답이 아닙니다.\n"
            "// expect.tool은 LLM이 고른 것을 그대로 넣어 둔 것뿐입니다. 사람이\n"
            "// 확인해서 고치고, _review를 지운 뒤 golden.jsonl로 옮기세요.\n"
        )
        for line in lines:
            file.write(line + "\n")
    return CANDIDATES_FILE


def main() -> int:
    parser = argparse.ArgumentParser(description="shadow 라우터 비교 리포트")
    parser.add_argument("--detail", action="store_true", help="불일치를 전부 나열")
    parser.add_argument("--since", type=int, metavar="일", help="최근 N일만")
    parser.add_argument("--export", action="store_true", help="골든셋 후보 파일 생성")
    options = parser.parse_args()

    init_db()
    agreed, disagreed = _load(options.since)

    print_summary(agreed, disagreed, options.since)
    if not agreed and not disagreed:
        return 0

    print_patterns(disagreed)
    if options.detail:
        print_detail(disagreed)
    print_traces(options.since)

    if options.export:
        path = export_candidates(disagreed)
        unique = sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                     if line and not line.startswith("//"))
        print(f"\n{'-' * 66}")
        print(f"골든셋 후보 {unique}건 → {path}")
        print("expect를 직접 확인해서 고친 뒤 evals/golden.jsonl로 옮기세요.")
        print("LLM 판단을 그대로 정답으로 쓰면 자기 답으로 채점하는 셈이 됩니다.")

    print(f"\n{'=' * 66}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
