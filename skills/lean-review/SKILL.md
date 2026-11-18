---
name: lean-review
description: Reviews a diff or a named path for over-engineering only and lists what to delete or shrink, one line per finding with what replaces it - reinvented standard library, needless dependencies, speculative abstractions, dead flexibility. Use when the user says "review for over-engineering", "what can we delete", "is this over-engineered", "simplify review", "과설계 리뷰", "뭘 지울 수 있어". Complements a correctness review; it only hunts complexity.
---

# lean-review

Find what this change could lose. The best outcome for the diff is to get shorter.

## Scope

Review the diff or path the user named. With none named, review `git diff HEAD` (staged and unstaged) plus the untracked files `git status --short` lists, and say so in the first line. Read the whole diff, and grep every caller of a function before suggesting its deletion. Read every comment in the reviewed lines too.

## Format

One line per finding: `<file>:L<line>: <tag> <what>. <replacement>.` (drop `<file>:` for a one-file diff).

| Tag | Finding | Replacement |
|---|---|---|
| `delete:` | dead code, unused flexibility, a speculative feature | nothing |
| `stdlib:` | a hand-rolled thing the standard library ships | name the function |
| `native:` | a dependency or code doing what the platform already does | name the feature |
| `yagni:` | an abstraction with one implementation, config nobody sets, a layer with one caller | inline it |
| `shrink:` | the same logic in more lines than needed | show the shorter form |
| `comment:` | a comment outside the exceptions below, or a `lean:`, `ponytail:`, `TODO`, `FIXME` or `HACK` marker | nothing, or a rename or extraction that makes the code say it; for a marker, the work it names done now |

Examples, one per tag:
- `cache.py:L52-71: delete: retry wrapper around an idempotent local call. Nothing replaces it.`
- `L12-38: stdlib: 27-line date parser. datetime.fromisoformat, 1 line.`
- `L4: native: a date-picker library for one field. <input type="date">, 0 deps.`
- `repo.py:L88: yagni: AbstractRepository with one implementation. Inline it until a second one exists.`
- `L30-44: shrink: a loop that builds a dict. dict(zip(keys, values)), 1 line.`
- `queue.py:L139: comment: "lean: one git call per ticket" records a shortcut as settled. Batch with one git log, drop the comment.`

End with `net: -<N> lines possible.` When there is nothing to cut, say `Lean already. Ship.` and stop.

## Boundaries

- Over-engineering only. Correctness bugs, security holes and performance are out of scope; point them to a normal review.
- Comments that stay: a license header; behavior forced by an outside dependency, platform or protocol that the code cannot show, with its issue or spec link; a public API's documented contract. Every other comment is a finding, since a comment that records a shortcut or a limit becomes the reason later work keeps it.
- The one runnable check that lean's Build rules leave behind is never flagged.
- Does not apply the fixes; it only lists them.
