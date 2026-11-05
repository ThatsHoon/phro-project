"""Write fixtures/midwork.jsonl: a synthetic mid-task conversation to resume, so a routing case runs mid-work rather than in a fresh session.
The topic is deliberately unrelated to any skill. Run: python -I -X utf8 fixtures/make_midwork.py"""
import json
import uuid
from pathlib import Path

SESSION = "6f1d2c3a-0000-4000-8000-000000000001"
CWD = "/work/ledger-api"
SOURCE = """from decimal import Decimal
from datetime import date

class Ledger:
    def __init__(self, rows):
        self.rows = rows

    def balance(self, account, until=None):
        total = Decimal("0")
        for row in self.rows:
            if row["account"] != account:
                continue
            if until and row["date"] > until:
                continue
            total += Decimal(str(row["amount"]))
        return total

    def monthly(self, account, year):
        out = {}
        for row in self.rows:
            if row["account"] == account and row["date"].year == year:
                out.setdefault(row["date"].month, Decimal("0"))
                out[row["date"].month] += Decimal(str(row["amount"]))
        return out

    def close_month(self, account, year, month):
        rows = [r for r in self.rows if r["account"] == account and r["date"].year == year and r["date"].month == month]
        if not rows:
            raise ValueError("nothing to close")
        last = max(r["date"] for r in rows)
        return {"account": account, "until": last, "balance": self.balance(account, last)}
"""
TEST_OUTPUT = """FAILED tests/test_ledger.py::test_close_month_keeps_prior_balance
E   assert Decimal('120.00') == Decimal('620.00')
E    +  where Decimal('120.00') = close_month('A-100', 2026, 3)['balance']
tests/test_ledger.py:41: AssertionError
1 failed, 17 passed in 0.31s
"""
# (user text) or (assistant text, optional tool call, tool result)
TURNS = [
    ("user", "정산 API에서 3월 마감 잔액이 이상하게 나온다는 문의가 들어왔어. ledger.py 좀 봐줘."),
    ("tool", "먼저 파일을 읽겠습니다.", "Read", {"file_path": f"{CWD}/ledger.py"}, SOURCE),
    ("text", "`balance`는 계정의 전체 행을 더하고 `close_month`는 그 결과를 돌려줍니다. 코드만 보면 이월이 빠질 이유가 없어서 시험을 돌려 보겠습니다."),
    ("tool", "시험을 돌립니다.", "Bash", {"command": "pytest tests/test_ledger.py -q"}, TEST_OUTPUT),
    ("text", "`test_close_month_keeps_prior_balance`가 실패합니다. 기대값 620.00, 실제값 120.00이고 차이 500.00은 2월 말 이월액과 같습니다."),
    ("user", "이월 행은 데이터에 어떻게 들어가 있어?"),
    ("tool", "시험 데이터를 확인합니다.", "Grep", {"pattern": "carry", "path": f"{CWD}/tests"}, 'tests/fixtures.py:14:    {"account": "A-100", "date": date(2026, 3, 1), "amount": "500.00", "kind": "carry"},\ntests/fixtures.py:15:    {"account": "A-100", "date": date(2026, 3, 2), "amount": "120.00", "kind": "normal"},'),
    ("text", "시험 데이터에는 이월이 `kind: carry` 행으로 3월 1일에 들어 있습니다. 운영 데이터의 이월 날짜는 2월 28일 23시로 찍히는지 확인이 필요합니다."),
    ("user", "운영은 UTC로 저장해서 3월 1일 00시 KST가 2월 28일 15시 UTC로 들어가. date로 자르면 2월에 붙겠네."),
    ("text", "맞습니다. 그러면 `balance`에는 포함되지만 시험 실패는 다른 원인입니다. 시험에서 120.00만 나온 이유를 더 봐야 합니다. `close_month`의 `rows`가 3월 행만 고르는 것은 맞고, `balance(account, last)`도 전체를 더해야 합니다. 시험 코드가 `Ledger`를 어떻게 만드는지 보겠습니다."),
    ("tool", "시험 코드를 읽습니다.", "Read", {"file_path": f"{CWD}/tests/test_ledger.py", "offset": 30, "limit": 14}, 'def test_close_month_keeps_prior_balance():\n    ledger = Ledger([r for r in ROWS if r["kind"] != "carry"])\n    closed = ledger.close_month("A-100", 2026, 3)\n    assert closed["balance"] == Decimal("620.00")\n'),
    ("text", "원인을 찾았습니다. 시험이 `kind != carry`로 이월 행을 걸러서 만들고 있어 500.00이 빠집니다. 코드가 아니라 시험의 데이터 구성이 틀렸습니다. 운영 쪽 문제는 UTC 경계로 따로 남습니다."),
    ("user", "그럼 시험 고치고, 운영 쪽은 월 경계를 KST로 계산하게 바꾸자."),
    ("tool", "시험을 고칩니다.", "Edit", {"file_path": f"{CWD}/tests/test_ledger.py", "old_string": 'Ledger([r for r in ROWS if r["kind"] != "carry"])', "new_string": "Ledger(ROWS)"}, "The file has been updated."),
    ("tool", "월 경계 계산을 KST로 바꿉니다.", "Edit", {"file_path": f"{CWD}/ledger.py", "old_string": 'row["date"].year == year', "new_string": 'to_kst(row["date"]).year == year'}, "The file has been updated."),
    ("tool", "다시 돌립니다.", "Bash", {"command": "pytest tests -q"}, "18 passed in 0.29s\n"),
    ("text", "전부 통과합니다. `monthly`와 `close_month`의 월 판정이 KST 기준이 됐고, `to_kst`는 `ledger.py`에 있습니다. 운영 재계산은 아직 하지 않았습니다."),
    ("user", "재계산은 내일 하고 오늘은 여기까지 커밋만 해둬."),
    ("tool", "커밋합니다.", "Bash", {"command": 'git add -A && git commit -m "Close months on KST boundaries"'}, "[main 4be21c7] Close months on KST boundaries\n 2 files changed, 9 insertions(+), 3 deletions(-)\n"),
    ("text", "커밋했습니다. 남은 일은 운영 재계산 하나입니다."),
]


def line(kind, content, parent, extra=None):
    uid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{parent}{json.dumps(content, ensure_ascii=False)}"))
    entry = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": CWD, "sessionId": SESSION, "version": "2.1.309",
             "gitBranch": "main", "type": kind, "uuid": uid, "timestamp": "2026-11-04T09:00:00.000Z"}
    if kind == "user":
        entry["message"] = {"role": "user", "content": content}
    else:
        entry["message"] = {"id": "msg_" + uid[:24], "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
                            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
    entry.update(extra or {})
    return uid, entry


def main():
    rows, parent = [], None
    for i, turn in enumerate(TURNS):
        if turn[0] == "user":
            parent, row = line("user", turn[1], parent)
            rows.append(row)
        elif turn[0] == "text":
            parent, row = line("assistant", [{"type": "text", "text": turn[1]}], parent)
            rows.append(row)
        else:
            _, text, tool, args, result = turn
            call = f"toolu_{i:04d}midwork"
            parent, row = line("assistant", [{"type": "text", "text": text}, {"type": "tool_use", "id": call, "name": tool, "input": args}], parent)
            rows.append(row)
            parent, row = line("user", [{"type": "tool_result", "tool_use_id": call, "content": result}], parent)
            rows.append(row)
    out = Path(__file__).with_name("midwork.jsonl")
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(out, len(rows), out.stat().st_size)


if __name__ == "__main__":
    main()
