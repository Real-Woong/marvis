"""골든셋을 재생해서 라우터 정확도를 잰다.

    python -m evals.run_eval                    # 기본 프로바이더
    python -m evals.run_eval --provider gemini
    python -m evals.run_eval --tag 상대날짜      # 특정 카테고리만
    python -m evals.run_eval --no-cache         # 캐시 무시하고 실제 호출

케이스마다 깨끗한 임시 DB에서 실행하므로 진짜 데이터를 건드리지 않습니다.
응답은 (프로바이더, 모델, 프롬프트 해시)로 캐시되어, 프롬프트가 바뀌지 않는
한 재실행은 무료입니다.

채점 대상은 '어떤 도구를 어떤 인자로 불렀는가'입니다. 답변 문장은 채점하지
않습니다 — 표현은 흔들리고, 중요한 건 행동이기 때문입니다.
"""

import argparse
import hashlib
import json
import os
import pickle
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLDEN_FILE = Path(__file__).parent / "golden.jsonl"
CACHE_DIR = Path(__file__).parent / "cache"


def load_cases(tag: str | None = None) -> list[dict]:
    cases = []
    with GOLDEN_FILE.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as error:
                raise SystemExit(f"{GOLDEN_FILE}:{line_number} 파싱 실패: {error}")
            if tag and tag not in case.get("tags", []):
                continue
            cases.append(case)
    return cases


class CachingClient:
    """실제 클라이언트를 감싸 응답을 디스크에 캐시합니다."""

    def __init__(self, inner, enabled: bool = True):
        self.inner = inner
        self.enabled = enabled
        self.name = inner.name
        self.model = inner.model
        self.hits = 0
        self.misses = 0
        CACHE_DIR.mkdir(exist_ok=True)

    def _key(self, system, messages, tools) -> str:
        blob = json.dumps(
            {
                "provider": self.name,
                "model": self.model,
                "system": system,
                "messages": [
                    {
                        "role": m.role,
                        "text": m.text,
                        "calls": [(c.name, c.args) for c in m.tool_calls],
                        "results": [(r.name, r.content) for r in m.tool_results],
                    }
                    for m in messages
                ],
                "tools": [t["name"] for t in tools],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    def complete(self, system, messages, tools):
        if not self.enabled:
            self.misses += 1
            return self.inner.complete(system, messages, tools)

        path = CACHE_DIR / f"{self.name}-{self._key(system, messages, tools)}.pkl"
        if path.exists():
            self.hits += 1
            return pickle.loads(path.read_bytes())

        self.misses += 1
        response = self.inner.complete(system, messages, tools)
        path.write_bytes(pickle.dumps(response))
        return response


def args_match(expected: dict, actual: dict) -> tuple[bool, list[str]]:
    """부분 일치. 정답에 적힌 키만 봅니다."""
    misses = []
    for key, want in (expected or {}).items():
        got = (actual or {}).get(key)
        if str(got) != str(want):
            misses.append(f"{key}: 기대={want} 실제={got}")
    return not misses, misses


def run_case(client, case: dict) -> dict:
    from datetime import datetime

    from marvis import agent
    from marvis.db import get_connection, init_db
    from marvis.time_utils import freeze_clock

    # 케이스가 '오늘'을 지정하면 그 시각으로 고정합니다. 그래야 정답에
    # 절대 날짜를 적어도 시간이 지나며 썩지 않습니다.
    if case.get("today"):
        at = case.get("now") or f"{case['today']} 09:00:00"
        freeze_clock(datetime.strptime(at, "%Y-%m-%d %H:%M:%S"))
    else:
        freeze_clock(None)

    init_db()
    conn = get_connection()
    for table in ("items", "projects", "events"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    # 케이스가 사전 상태를 요구하면 만들어 둡니다.
    for name in case.get("given_projects", []):
        from marvis.projects import add_project

        add_project(name)
    for item in case.get("given_items", []):
        from marvis.memory import create_item

        create_item(
            content=item["content"],
            kind=item.get("kind", "schedule"),
            schedule_date=item.get("schedule_date"),
        )

    try:
        result = agent.run_turn(client, case["utterance"], source="eval")
    finally:
        freeze_clock(None)

    expect = case.get("expect", {})
    want_tool = expect.get("tool")
    got_tool = result.primary_tool

    tool_ok = want_tool == got_tool
    arg_ok, arg_misses = True, []
    if tool_ok and expect.get("args"):
        actual = result.tool_calls[0]["args"] if result.tool_calls else {}
        arg_ok, arg_misses = args_match(expect["args"], actual)

    return {
        "id": case["id"],
        "utterance": case["utterance"],
        "tags": case.get("tags", []),
        "want_tool": want_tool,
        "got_tool": got_tool,
        "tool_ok": tool_ok,
        "arg_ok": arg_ok,
        "arg_misses": arg_misses,
        "reply": result.text,
        "note": case.get("note", ""),
    }


def summarize(results: list[dict]) -> None:
    total = len(results)
    if not total:
        print("실행할 케이스가 없습니다.")
        return

    tool_ok = sum(r["tool_ok"] for r in results)
    scored_args = [r for r in results if r["tool_ok"] and r["arg_misses"] or (r["tool_ok"] and r["arg_ok"])]
    with_args = [r for r in results if r["want_tool"] and r["tool_ok"]]
    arg_ok = sum(r["arg_ok"] for r in with_args)

    # 오탐: 도구를 부르면 안 되는데 부른 것 / 미탐: 불러야 하는데 안 부른 것
    false_positive = sum(1 for r in results if r["want_tool"] is None and r["got_tool"] is not None)
    no_tool_cases = sum(1 for r in results if r["want_tool"] is None)
    false_negative = sum(1 for r in results if r["want_tool"] is not None and r["got_tool"] is None)
    tool_cases = sum(1 for r in results if r["want_tool"] is not None)

    def pct(n, d):
        return f"{100 * n / d:5.1f}%" if d else "    - "

    print("\n" + "=" * 62)
    print(f"{'전체':10} {total}건")
    print(f"{'도구 선택':10} {pct(tool_ok, total)}  ({tool_ok}/{total})")
    print(f"{'인자 정확도':10} {pct(arg_ok, len(with_args))}  ({arg_ok}/{len(with_args)})")
    print(f"{'오탐률':10} {pct(false_positive, no_tool_cases)}  ({false_positive}/{no_tool_cases})")
    print(f"{'미탐률':10} {pct(false_negative, tool_cases)}  ({false_negative}/{tool_cases})")

    by_tag = defaultdict(lambda: [0, 0])
    for r in results:
        for tag in r["tags"]:
            by_tag[tag][1] += 1
            if r["tool_ok"] and r["arg_ok"]:
                by_tag[tag][0] += 1

    print("\n카테고리별 (통과/전체)")
    for tag, (ok, n) in sorted(by_tag.items()):
        warn = "  ← 케이스 부족, 점수 신뢰 못 함" if n < 5 else ""
        print(f"  {tag:12} {pct(ok, n)}  ({ok}/{n}){warn}")

    failures = [r for r in results if not (r["tool_ok"] and r["arg_ok"])]
    if failures:
        print(f"\n실패 {len(failures)}건")
        for r in failures:
            print(f"\n  [{r['id']}] {r['utterance']}")
            if not r["tool_ok"]:
                print(f"    도구: 기대={r['want_tool']} 실제={r['got_tool']}")
            for miss in r["arg_misses"]:
                print(f"    인자: {miss}")
            if r["note"]:
                print(f"    비고: {r['note']}")
    print("=" * 62)

    # 스펙의 전환 기준
    tool_rate = tool_ok / total
    arg_rate = arg_ok / len(with_args) if with_args else 1.0
    passed = tool_rate >= 0.95 and arg_rate >= 0.90
    print(f"\n전환 기준(도구 95% / 인자 90%): {'충족' if passed else '미달'}")
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--no-cache", action="store_true")
    options = parser.parse_args()

    # 진짜 DB를 절대 건드리지 않도록 임시 파일을 씁니다.
    os.environ["MARVIS_DB_FILE"] = tempfile.mktemp(suffix="-eval.db")
    os.environ["MARVIS_ROUTER"] = "regex"

    from marvis.llm.factory import get_client

    client = CachingClient(get_client(options.provider), enabled=not options.no_cache)

    cases = load_cases(options.tag)
    print(f"{client.name}/{client.model} · {len(cases)}건 실행")

    results = [run_case(client, case) for case in cases]
    print(f"\n캐시 적중 {client.hits} · 실제 호출 {client.misses}")
    return summarize(results)


if __name__ == "__main__":
    raise SystemExit(main())
