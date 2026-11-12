---
name: update
description: Brings a newer original of this plugin into the project's own copy, keeping every file the project added or changed. Use when the user types "<copy> update", such as "phro-project_e-call update", or asks to take the plugin's update into this project ("플러그인 업데이트 반영", "원본 업데이트 가져와", "사본 업데이트").
---

# update

Output: the copy holding the original's latest published files, the project's own files untouched, and the user told to reload.

Updates flow one way, from the original into the copy. Never copy anything from the copy back into the original: the copy holds this project's own additions, which are not part of the plugin's skeleton.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py sync`.

1. Run it with `--dry-run`. If it prints `FAILED this is the original`, say that the original needs no update and stop.
2. Show the user, in their language, what would change: `update`, `add` and `remove` lines, then each `keep` line. A `keep` file is one the project changed; it stays as the project has it, and the original's change to it is not taken. Name those files.
3. Run it without `--dry-run`, and tell the user to type `/reload-plugins`.

Report the counts, the kept files and the original's version or commit from the `SYNCED` line.
