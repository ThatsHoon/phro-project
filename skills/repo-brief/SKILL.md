---
name: repo-brief
description: Reads one GitHub repo and writes a practical brief to a local Markdown file, then briefs it in chat as tables. Purpose, basics, core features, alternatives, when to use it, install commands, risks (security, terms, self-reported numbers, inflated stars) and the link to the user's own work. Runs only when you type it, with a repo link.
disable-model-invocation: true
---

# repo-brief

Read one repository so the user can decide at once whether to use it and where. The full brief goes to a local Markdown file. The chat answer is a short briefing in tables. No HTML page, no Artifact.

Input: a repo link or `owner/repo`. Without one, ask for it and stop.

## 1. Read

Take `owner/repo` from the link. Before any GitHub call, follow the account rules in the user's CLAUDE.md, if any.

Read in parallel:

```bash
gh repo view OWNER/REPO --json description,primaryLanguage,languages,stargazerCount,forkCount,createdAt,pushedAt,licenseInfo,repositoryTopics,isFork,isArchived,homepageUrl,owner
gh api repos/OWNER/REPO/readme -H "Accept: application/vnd.github.raw" | head -c 25000
gh api repos/OWNER/REPO/contents --jq '.[].name'
gh api repos/OWNER/REPO/releases/latest --jq '.tag_name+" "+.published_at'
```

If the README is in another language, find an English one (README_EN.md and the like) in the listing and read it too.

If the quick start runs a script or a package hook (`install.sh`, `setup.sh`, `postinstall`, `curl | sh`), read that file before writing the brief: what it downloads, what it installs, whether it needs root, where it sends anything. This step is not optional. If the file cannot be read, say so in the risks.

## 2. Risk signals

Look for these in the README, the scripts from step 1 and the metadata. Write only what you found.

- Stars as a gate for features, or stars far beyond the repo's age (compare stars, forks and created date)
- Bypassing another service's free quota or accounts, posing as an official client, rotating IPs to dodge limits
- Remote notices, auto-update or remote config that let the maintainer reach installed machines
- Where credentials, API keys, prompts and target data go, and whether they are logged
- Benchmarks the project ran on itself, with or without a way to reproduce them; inflated comparisons
- A same-name fake package, archived, stale, a fork, or a re-upload of another repo that is not a GitHub fork
- Offensive or dual-use tools: note that they are for authorized targets only

## 3. Alternatives

Name the field in one line and run one or two web searches ("<field> alternatives", "<name> vs"). Put only what the results confirm in the comparison, without detailed figures for the alternatives. If none turn up, say so. Check the names by search even when the field is familiar.

## 4. Link to the user's work

Write one or two sentences on where it would help the user, from this conversation, memory and the current project. If nothing fits, say so in one line; do not force it.

## 5. The Markdown file

Write `~/research/repo-brief/<owner>__<repo>-<YYYY-MM-DD>.md` (create the folder if missing; a second run on the same day overwrites). Write it in the user's language. Use this layout:

```markdown
# <owner>/<repo>

> <What it is, one line.> <Worth using or not, one line.>

| 항목 | 내용 |
|---|---|
| 링크 | https://github.com/<owner>/<repo> |
| 언어 | |
| 라이선스 | |
| 스타 / 포크 | |
| 생성일 / 최근 푸시 | |
| 최신 릴리스 | |
| 만든 곳 | 개인 / 회사 / 재단, 원본이 따로 있으면 그것도 |
| 성숙도 | README의 상태 표기 |

## 핵심 기능
| 기능 | 푸는 문제 |

## 유사 도구와 차별점
| 도구 | 방식 | 이걸 고를 때 |

## 언제 쓰나 / 안 맞는 경우
| 쓸 때 | 안 맞을 때 |

## 바로 써보기
README에 있는 그대로, bash 코드 블록 하나에 명령 하나.

## 주의점
| 신호 | 근거 | 영향 |

## 내 작업과의 연결

## 읽은 범위와 출처
읽은 것(README, 메타데이터, 파일 목록, 열어 본 스크립트, 검색 횟수)과 읽지 않은 것. 검색 결과 링크.
```

Rules:
- Base every claim on the README or a file you read. Mark anything else "추정".
- Attach who measured each number (benchmarks, user counts).
- Drop a section that has nothing to say.
- A link that is not GitHub (GitLab, Hugging Face and the like) uses the same layout, with step 1 replaced by that site's API or page.

## 6. The chat briefing

After the file is written, answer in chat in the user's language:

1. The conclusion, two lines: what it is, and whether it is worth using.
2. One table that carries the brief at a glance:

| 항목 | 내용 |
|---|---|
| 정체 | |
| 기본 | 언어 · 라이선스 · 스타/포크 · 최근 푸시 |
| 핵심 기능 | 가장 중요한 2~3개 |
| 대안 | 이름만, 2~3개 |
| 쓸 때 | |
| 주의 | 가장 중요한 1~2개 |
| 내 작업 | |

3. A link to the file, and one line on what was read and what was not.

Keep the chat part to about one screen. The detail lives in the file.
