---
name: daily
description: The weekday morning report of a project's copy, written by itself into the copy's docs/daily as <date>(<weekday>).md with yesterday's finished work and today's work in short Korean list items. Off by default; once turned on, a hook starts it at the first session or prompt after 08:00 KST, Monday to Friday. Use this skill only when the user asks to see, rewrite or regenerate that report, or to turn it on or off ("일일 정리 보여줘", "오늘 일일 문서 다시 써", "daily 켜줘", "daily report").
---

# daily

Output: `<copy>/docs/daily/<YYYY-MM-DD>(<요일>).md`, one per weekday.

How it runs: off until the user turns it on. `scripts/daily.py hook` runs at every session start and prompt in a project's copy (never in the original plugin) and returns at once when the copy's `origin.json` holds `"daily": false` (new copies are written that way; copies made before the switch have no key and keep running). When on, on a weekday after 08:00 KST, when today's file is missing and no other session is writing it, it starts `daily.py run` in the background and returns at once. `run` gathers, for the period since the previous weekday 00:00 KST until today 00:00 KST, every session of the project (each fork's typed prompts and last replies, read from the transcript tails), the project's commits, and the queue's closed and open tickets, then asks `claude -p` to write the report in the style below, checks the form and writes the file. A failure is kept in `docs/daily/.state/<date>.error`; the next session after 30 minutes tries again.

When the user asks:
- To turn it on or off: run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/daily.py on` (or `off`). The setting survives `update`.
- To see it: read today's file (or the date they name) and show it as it is.
- To rewrite it: run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/daily.py run --force [--date YYYY-MM-DD]` and show the new file.
- To change the style: edit the Style section below; `run` reads it from this file.

## Style

팀 채팅방에 아침마다 올리는 짧은 업무 공유처럼 쓴다. 읽는 사람은 작업 과정을 전혀 모르는 동료다.

- 제목은 `어제 완료:`와 `오늘 할 일:` 두 개뿐이고, 각각 한 번만 쓴다. 제목을 나누거나 덧붙이지 않는다(`어제 완료 - 재난:` 금지).
- 내용은 `* ` 항목으로만 쓴다. 문단과 서술문은 쓰지 않는다.
- 한 항목에는 일 하나를 `[대상] [작업]` 순서로 쓴다. 예: `e-call 홈페이지 api 연결`
- 항목은 명사나 명사구로 끝낸다(기획, 연동, 게시, 설계, 수집, 개선, 배포 등). `~했다`, `~합니다`, `~해요`로 끝내지 않는다.
- 항목은 40자 안쪽으로 짧게 쓴다. 덧붙일 말이 꼭 필요하면 ` - ` 뒤에 몇 단어만 쓴다. 괄호는 쓰지 않는다. 예: `detect 단계 파이프라인 설계 - 촬영 중 실시간`
- 같은 대상의 일이 여럿이면 쉼표로 묶어 한 항목으로 만든다. 예: `스테이 프로모션 - 시스템 어드민, 앱 구현`, `ecall 홍수,열원,연기,탄 자국 분석 모델 개선`
- 제목마다 항목은 다섯 개까지, 중요한 것을 위에 둔다. 넘치면 작은 일을 묶거나 뺀다.
- 누구나 아는 쉬운 말로 쓴다. 결과가 무엇인지만 쓰고 과정은 쓰지 않는다.
- 러프하게 쓴다. 무엇을 다뤘는지(대상)와 어떤 종류의 일인지(작업)만 남기고, 어디서·어디로·어떤 도구로 했는지는 뺀다. 세부가 궁금한 사람은 따로 묻는다.
- 쓰지 않는 것: 저장소·폴더·서버·도구 이름(SAT-GUARD, JDI, GCP, MLflow, n8n 등), 옮긴 곳이나 경로, 내부 실험 이름과 번호(IDEA-095, r6, v3.28), 판정 등급(high, confirmed), 포트·버전·파일명, 기호로 줄인 말(`+`, `?`), 화면 문구 인용, 수치와 기준 설명, 지역 이름, 대기 중인 결정.
- 항목에 숫자, `+`, `?`, 괄호를 쓰지 않는다. 검사에서 걸린다.
- 판단 기준: 이 일에 참여하지 않은 팀원이 그 단어를 보고 무엇인지 모를 것 같으면 빼거나 일반 명사(모델, 화면, 앱, 서버, 데이터)로 바꾼다.
- 인사, 서론, 이유 설명, 맺음말은 쓰지 않는다.

나쁜 예와 고친 예:
- `화재 복합 판독 실험 - 열원+연기(high) 단계 불통과로 종결, 열원+탄 자국(confirmed) 후보는 2024 봄 오탐 기준 통과` -> `화재 감지 모델 조합 실험`
- `sVLM - NO RESPONSE? 시 sVLM 호출 연결, 포트 충돌 해결(8095)` -> `신고자 무응답 시 차내 영상 분석 연결`
- `산사태 IDEA-095(곡성 기준 한국 빈 시계열 학습) 개시 여부 및 봉화 한국 라벨 결정 대기` -> `산사태 감지 모델 학습 준비`
- `재난 분석 모델 SAT-GUARD 폴더 정리, 이전` -> `재난 분석 모델 폴더 구조 정리`
- `시뮬레이터 OBD 데이터 전환, SAT-GUARD 이전` -> `차량 시뮬레이터 데이터 교체`
- `홍수 부산 지역 라벨 작업` -> `홍수 학습 데이터 라벨링`

Example:

```
어제 완료:
* ecall 홍수,열원,연기,탄 자국 분석 모델 개선
* 차내 촬영 수집 앱 배포

오늘 할 일:
* 산사태 감지 모델 학습 준비
* 졸음운전 판정 모델 학습
```
