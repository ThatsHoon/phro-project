---
name: update
description: Brings a newer original of this plugin into the project's own copy, keeping every file the project added or changed. Use when the user types "<copy> update", such as "phro-project_e-call update", or asks to take the plugin's update into this project ("플러그인 업데이트 반영", "원본 업데이트 가져와", "사본 업데이트").
---

# update

Output: the copy holding the original's latest published files, the project's own files untouched, every change of the original the copy did not take named, and the user told to reload.

Updates flow one way, from the original into the copy. Never copy anything from the copy back into the original: the copy holds this project's own additions, which are not part of the plugin's skeleton.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py sync`.

1. Run it with `--dry-run`. If it prints `FAILED this is the original`, say that the original needs no update and stop.
2. Show the user, in their language, what would change: `update`, `add` and `remove` lines, then the kept files. A kept file is one the project changed; it stays as the project has it.
   - `keep`: the original has not changed it since. Nothing is missed.
   - `keep-changed`: the original changed it too, and that change will not arrive. When the line ends in `<base>..<new>`, run `git -C <origin_repo> diff <base>..<new> -- <path>` (`origin_repo` is in the copy's `origin.json`) and say in one line per file what the original changed.
   - `keep-removed`: the original deleted it; the copy keeps it as its own.
3. Run it without `--dry-run`.
4. For each `kept-changed` file, ask the user whether to bring the original's change into the project's version. On a yes, edit the copy's file so it holds both, run the copy's test for that file if there is one, then run `clone.py merged <path>`. Without a yes, leave it: the next update names it again.
5. Tell the user to type `/reload-plugins`.

Report the counts, each kept file by kind, what was merged, and the original's version or commit from the `SYNCED` line.
