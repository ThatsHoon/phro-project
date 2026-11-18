---
name: grill
description: Interviews the user one design-tree round at a time until both sides share the same understanding of a plan, decision or idea, with a recommended answer and its reason for every question. Use when the user wants a plan or design stress-tested or reviewed by questions, or says "grill me", "grill this", "stress-test this plan", "interview me about this design", "그릴해", "검수해줘 질문으로", "빠진 결정 없는지 물어봐".
---

# grill

Interview the user until you both hold the same understanding. Map it as a **design tree**: each decision branches into the decisions that depend on it.

## Rounds

The **frontier** is every decision whose prerequisites are settled: what you can ask now without guessing at answers you have not heard. A question that depends on another question still open belongs to a later round.

Ask the frontier as one round, at most 5 questions, the most blocking first; the rest wait for the next round. Format:

```
❓ **Q1** - **<title>**: <question, with the choices when there are some>

➡️ <recommended answer>
<one line of rationale: a reason drawn from facts you found, not a restatement>

---

❓ **Q2** - ...
```

Word each question so "yes" accepts the recommended answer. Each answer reshapes the tree: recompute the frontier and ask the next round.

When at most 2 decisions remain and both are cheap to reverse, ask them in one line each, without the round format.

When the user accepts a whole round with one word and a decision in it is costly to reverse, restate that decision's cost in one line before acting on it.

## Facts and decisions

Finding facts is your job, never the user's. When a question needs a fact from the files, tools or the web, look it up, or send a sub-agent, and do not ask the user for it. A question that a quick run or prototype can settle is a fact too: settle it that way and give the result in the round. A running lookup blocks only the questions downstream of it; ask the rest of the frontier now. Decisions belong to the user: put each to them and wait.

## The tree file

Keep the tree in `grill-<topic>.md`, in the session scratchpad when the system prompt names one, else in the OS temp dir. It holds the settled decisions with their answers, the open questions, and the running fact lookups. Rewrite it after every round. Name its path in chat once. After a compaction, read it before asking the next round.

## Done

The interview is done when the frontier is empty: every branch visited, nothing silently assumed. Do not act on the result until the user confirms you have reached a shared understanding.
