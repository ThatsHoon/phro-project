---
name: audit
description: Reads a plugin's current structure and proposes merges, deletions and repairs, without editing it.
disable-model-invocation: true
---

# audit

Output: a short ranked proposal list. "No change" is a normal result. Do not edit the plugin during an audit.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py <command>`. It rebuilds the graph from the files on every call, so never rely on an earlier reading.

1. Run `summary <plugin> --usage <transcripts_dir>` and `check <plugin> --usage <transcripts_dir>`. Omit `--usage` if there are no transcripts.
2. Read `<plugin>/.phro-project/rejected.md` if it exists. Do not raise a rejected proposal again unless its reason no longer holds.
3. Verify each finding by opening the file. The script finds candidates, and some are false:
   - `duplicate-file`: may be deliberate, such as a template copied into examples or one rule file per platform.
   - `orphan`: may be a tool run by hand or a file other platforms read.
   - `overlap-candidate`: word overlap only. Read both descriptions, and ask which real requests would fit both.
   - `never-called`: means nothing if the transcripts predate the skill or come from another project. `unmeasured` means no transcripts were found, so use is unknown.
4. Find what the script cannot: skills that do the same job in different words, a rule that no longer matches the code, and steps a script could do deterministically. Use `sql` and `impact <plugin> <node>` for structure questions.
5. Write proposals, deletions and merges first:

| # | Proposal | Evidence (file:line, finding or count) | Breaks if done (`impact`) | Gain | How to test |
|---|---|---|---|---|---|

Rules for the list:
- Every proposal carries evidence from this run. Without any, drop it.
- A proposal that adds something must name what it removes, or why nothing can go.
- At most 5 proposals. Mention the rest in one line.
- If findings are noise or the plugin is fine, say so and stop.

6. Ask which to apply. Apply only those, on a copy or a branch, then read `${CLAUDE_PLUGIN_ROOT}/skills/route-test/SKILL.md` and follow it on both versions, and report what was gained and what broke. Record refused proposals in `<plugin>/.phro-project/rejected.md`, one line each with the reason.

Report in the user's language.
