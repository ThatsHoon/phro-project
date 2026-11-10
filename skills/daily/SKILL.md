---
name: daily
description: The weekday morning report of a project's copy, written by itself into the copy's docs/daily as <date>(<weekday>).md with yesterday's finished work and today's work in short Korean list items. A hook starts it at the first session or prompt after 08:00 KST, Monday to Friday. Use this skill only when the user asks to see, rewrite or regenerate that report ("일일 정리 보여줘", "오늘 일일 문서 다시 써", "daily report").
---

# daily

Output: `<copy>/docs/daily/<YYYY-MM-DD>(<요일>).md`, one per weekday.

How it runs: `scripts/daily.py hook` runs at every session start and prompt in a project's copy (never in the original plugin). On a weekday after 08:00 KST, when today's file is missing and no other session is writing it, it starts `daily.py run` in the background and returns at once. `run` gathers, for the period since the previous weekday 00:00 KST until today 00:00 KST, every session of the project (each fork's typed prompts and last replies, read from the transcript tails), the project's commits, and the queue's closed and open tickets, then asks `claude -p` to write the report in the style below, checks the form and writes the file. A failure is kept in `docs/daily/.state/<date>.error`; the next session after 30 minutes tries again.

When the user asks:
- To see it: read today's file (or the date they name) and show it as it is.
- To rewrite it: run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/daily.py run --force [--date YYYY-MM-DD]` and show the new file.
- To change the style: edit the Style section below; `run` reads it from this file.

## Style

- 첫 줄은 제목 한 줄과 콜론으로 쓴다. 예: `어제 완료:`, `오늘 할 일:`
- 내용은 `* ` 항목으로만 쓴다. 문단과 서술문은 쓰지 않는다.
- 한 항목에는 일 하나를 `[대상] [작업]` 순서로 쓴다. 예: `e-call 홈페이지 api 연결`
- 항목은 명사나 명사구로 끝낸다(기획, 연동, 게시, 설계, 수집 등등). `~했다`, `~합니다`, `~해요`로 끝내지 않는다.
- 덧붙일 말은 같은 줄 안에서 ` - ` 뒤나 괄호 안에 짧게 쓴다. 예: `detect 단계 파이프라인 설계 (촬영 중 실시간)`
- 같은 대상의 일이 여럿이면 쉼표로 묶는다. 예: `스테이 프로모션 - 시스템 어드민, 앱 구현`
- 항목은 다섯 개까지 쓰고, 중요한 것을 위에 둔다. 더 많으면 제목을 나눠 묶는다. 예: `어제 완료 - 재난:`, `어제 완료 - sVLM:`
- 인사, 서론, 이유 설명, 맺음말은 쓰지 않는다.

Example:

```
어제 완료:
* 홍수 모델 파이프라인 구축

오늘 할 일:
* 홍수 모델 성능 개선
```
