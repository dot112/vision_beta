# Prompts for the five sessions

Start one new session per section, **in order**. Paste the prompt as the session's first message.

Sections 3 to 5 have a `<BRANCH>` placeholder. Replace it with the branch the previous session reports when it finishes; it is also written in the table in `docs/progress/README.md` on that branch. If you forget, the session finds it with the command in the prompt. Session 2's prompt already names its branch.

**If a section's branch was merged into `main` with "Squash and merge"**, start the next session from `main` instead (`git fetch origin main && git checkout -B <your session branch> origin/main`). Starting from the old branch brings the same changes back as separate commits, and `main` then reports conflicts (this happened once: see the merge commit `78c51a7` on `claude/tender-cerf-qzfcle`).

---

## Session 1: Logic fixes (rest of Section 1)

**Done** on `claude/tender-cerf-qzfcle`. Its prompt is not needed any more.

---

## Session 2: Camera cards on Line setup (up to 8 cameras)

```text
Start from the branch where Section 1 was finished: claude/tender-cerf-qzfcle
  git fetch origin claude/tender-cerf-qzfcle && git checkout -B <your session branch> origin/claude/tender-cerf-qzfcle
(If that branch has been merged into main, start from main instead: git fetch origin main && git checkout -B <your session branch> origin/main.)

Read docs/progress/README.md, then docs/progress/section-1-logic-fixes.md ("Notes for the next section"), then docs/progress/section-2-camera-cards.md, and do Section 2:
- the camera cards on Line setup, with every camera setting inside its card;
- up to 8 cameras per line;
- per-camera counting settings in the UI;
- the v8 upgrade copying the line's count lines into each camera;
- the camera strip on the Line dashboard;
- the docs.

Rules: follow the rules in docs/progress/README.md. Upgraded lines must count exactly as before. Add the tests listed in the section file. Run `pytest` and `ruff check .` until both are green. Check the UI with Playwright and take screenshots at 1440 px and 375 px.

When done:
1. In docs/progress/section-2-camera-cards.md, fill in "Status" and "Notes for the next section".
2. In the table in docs/progress/README.md, set Section 2 to Done and write your branch name.
3. Commit and push your branch.
4. Reply with your branch name and a short summary.
Do not start Section 3.
```

---

## Session 3: Several vision cameras: own station or joined result

```text
Start from the branch where Section 2 was finished: <BRANCH>
  git fetch origin <BRANCH> && git checkout -B <your session branch> origin/<BRANCH>
(If you do not know the branch: `git fetch origin '+refs/heads/claude/*:refs/remotes/origin/claude/*'`, then use the newest branch whose docs/progress/README.md marks Section 2 as Done.)

Read docs/progress/README.md, then docs/progress/section-3-joined-cameras.md, and do Section 3. Each extra vision camera is either an "Own station" or "Joins the product result":
- joined cameras are matched to the counting camera's products by travel time (before or after it on the belt);
- one result per product, with reason station_no_result when a joined camera sees nothing and is set to reject;
- the travel-delay warnings, the UI fields on the camera card, and the docs.

Rules: follow the rules in docs/progress/README.md. Lines without joined cameras must behave exactly as before. Add the tests listed in the section file. Run `pytest` and `ruff check .` until both are green. Take screenshots at 1440 px and 375 px.

When done:
1. In docs/progress/section-3-joined-cameras.md, fill in "Status" and "Notes for the next section" (include the exact shape of `stations`).
2. In the table in docs/progress/README.md, set Section 3 to Done and write your branch name.
3. Commit and push your branch.
4. Reply with your branch name and a short summary.
Do not start Section 4.
```

---

## Session 4: Product records, Records page, CSV and Excel export

```text
Start from the branch where Section 3 was finished: <BRANCH>
  git fetch origin <BRANCH> && git checkout -B <your session branch> origin/<BRANCH>
(If you do not know the branch: `git fetch origin '+refs/heads/claude/*:refs/remotes/origin/claude/*'`, then use the newest branch whose docs/progress/README.md marks Section 3 as Done.)

Read docs/progress/README.md, then docs/progress/section-4-product-records.md, and do Section 4:
- the product_records table and its migration;
- the non-blocking recorder with retention;
- line counters that survive a restart;
- the records API with filters and a summary;
- CSV and Excel (.xlsx, no new library) export for a chosen start and end time;
- the "Production records" dashboard page, the records:read API key scope, and the docs.

Rules: follow the rules in docs/progress/README.md (a new table goes in _LATER_TABLES of revision 0001 and gets its own revision). Add the tests listed in the section file. Run `pytest` and `ruff check .` until both are green. Run the server from a temporary copy of the repository (never from the repo folder), count some products with a fake camera, export CSV and XLSX, and check them. Take screenshots at 1440 px and 375 px.

When done:
1. In docs/progress/section-4-product-records.md, fill in "Status" and "Notes for the next section".
2. In the table in docs/progress/README.md, set Section 4 to Done and write your branch name.
3. Commit and push your branch.
4. Reply with your branch name and a short summary.
Do not start Section 5.
```

---

## Session 5: PLC inputs, batch, trigger inspection, reject confirmation

```text
Start from the branch where Section 4 was finished: <BRANCH>
  git fetch origin <BRANCH> && git checkout -B <your session branch> origin/<BRANCH>
(If you do not know the branch: `git fetch origin '+refs/heads/claude/*:refs/remotes/origin/claude/*'`, then use the newest branch whose docs/progress/README.md marks Section 4 as Done.)

Read docs/progress/README.md, then docs/progress/section-5-plc-inputs.md, and do Section 5:
- read() for the PLC drivers;
- PLC signal cards per line: start/stop, run while on, reset counters, product sensor and PLC counter, capture, set batch, confirm a card, user alarm;
- the PLC input polling service;
- the batch number in events, records, messages and PLC values;
- trigger-mode inspection for vision cameras (/control/trigger becomes a real trigger);
- real reject confirmation for PLC action cards;
- the UI and the docs.

Rules: follow the rules in docs/progress/README.md. Lines without signal cards must behave exactly as before. Add the tests listed in the section file, including an end-to-end run with "test codes/plc_simulator.py" from a temporary copy of the server. Run `pytest` and `ruff check .` until both are green. Take screenshots at 1440 px and 375 px.

This is the last section:
1. Make README.md's "Production lines" part read as one story.
2. Update FUTURE_FIXES.md (what was done; what still needs a real line or PLC).
3. In docs/progress/section-5-plc-inputs.md and the README table, mark Section 5 as Done with your branch.
4. Commit and push, and make sure GitHub Actions (lint, tests, Docker) are green.
5. Reply with your branch name and a summary of all five sections.
```
