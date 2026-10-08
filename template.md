---
title: "Routines"
description: "One window showing everything in your workspace that runs on its own. Every scheduled task is picked up automatically, with nothing to set up, and shows its schedule, whether it is working, when it last ran, and whether that run was automatic or manual -- with fix, pause, and reschedule controls."
thumbnail: "template.svg"
version: v1
format: v2
---

# Routines

This file is the manifest for the **Routines** template (slug:
`routines`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

One window showing everything in your workspace that runs on its own. Every scheduled task is picked up automatically, with nothing to set up, and shows its schedule, whether it is working, when it last ran, and whether that run was automatic or manual -- with fix, pause, and reschedule controls.

A workspace quietly accumulates things that run without being asked: a nightly
sync, an hourly inbox pull, a weekly check-up, a backup, a service that
refreshes its data every few minutes. Each one lives somewhere different (a
cron file, a supervisord program, a loop inside an app), so when one stops
working nobody notices until its output goes stale. Routines puts all of them
in one window. Each row says in plain English what the routine does, how often
it runs and when it runs next, whether it is working, failing, paused, stopped
or running on sample data, when it last ran, and whether that last run was
automatic or started by hand. From the same row the user can run it now, pause
or resume it, change how often it runs (a number and a unit, plus an hour of the day for
daily or slower jobs), read its latest output and log, view the raw schedule entry it
came from, and press "Fix it" to open a chat that already has the failure
evidence in hand. Nothing has to be registered: a job the user or an agent
adds later shows up on the next page load.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/routines`
- `system/supervisord.conf.d/routines.conf`
- `AGENTS.md`

- `system/apps/routines` -- the Routines app itself, a Flask web app (Python
  package `routines`, entry point `routines`) listening on port 8082. Its
  modules:
  - `discovery.py` finds everything that exists, on every request: scheduled
    jobs from `/etc/cron.d/` (plus the durable copies under
    `data/.state/cron.d/` for entries not yet installed live), job health from
    `run_job.sh`'s own state in `data/.state/jobs/<id>/`, and long-running
    services from `supervisorctl status`.
  - `catalog.toml` is optional wording: a plain-English title and sentence per
    routine, and the one thing that cannot be discovered -- a `[[service]]`
    entry for a program that refreshes on its own internal timer (its cadence,
    and optionally `demo_env`, the env var that puts it in sample-data mode,
    which the app reads from the live process so it can say "Sample data"
    instead of "Working"). It ships with only the built-in workspace routines
    (weekly check-up, update recovery guard, workspace backup).
  - `bin/record_run.py` is the run recorder: a standard-library-only wrapper
    that runs a command, echoes its output, returns its exit code unchanged,
    and appends one JSON line per run (start, finish, exit code, trigger
    `scheduled` or `manual`, output tail) to `data/.apps/routines/runs.jsonl`.
    Cron entries wrap their command in it; the app's "Run now" calls it with
    `--trigger manual --update-job-state`.
  - `ledger.py` reads that run log (and reaps runs interrupted mid-flight),
    `schedules.py` turns cron lines and `run_job.sh --every` intervals into
    readable schedules and next-run times, `schedule_edit.py` pauses, resumes
    and re-times a job by rewriting its entry (durable copy in
    `data/.state/cron.d/` first, then the live `/etc/cron.d/` file), `view.py`
    turns all of it into rows with a health verdict, and `diagnose.py` builds
    the "Fix it" brief.
  - `assets/index.html` is the single-page frontend; `runner.py` serves it and
    the JSON API (`/api/routines`, and per routine `/runs`, `/run`, `/pause`,
    `/cadence`, `/diagnose`, `/log`, `/definition`).
- `system/supervisord.conf.d/routines.conf` -- the supervisord program. It
  registers the app with the shell through `system/scripts/forward_port.py
  --manifest system/apps/routines/app.toml --url http://localhost:8082` and
  then starts `routines`. The app's `app.toml` gives it a desktop icon on every
  new desktop and lets the shell stop it when no window shows it.
- `AGENTS.md` -- the base instructions plus one section, "Anything that runs on
  its own is shown in the Routines app", which tells every agent in the
  workspace to wrap new cron jobs in `record_run.py` and to declare any
  internal-timer service in `catalog.toml`. That section is what keeps the
  view complete as the workspace grows.

The "Fix it" button does not call a model itself: it hands a ready-made
`/assist` brief (the routine, its command, schedule, failure count, the files
to look in, and the recent runs' output) to `system/scripts/message_chat.py
--create`, which opens an ordinary chat in the workspace with that brief as
its first message. The app stores nothing of its own beyond the run log under
`data/.apps/routines/`.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


No activation requirements -- the app needs no external permissions, no
secrets, and makes no LLM calls of its own. It reads local files, the local
supervisord, and the workspace's own cron directories. Its "Fix it" button
opens a regular workspace chat through the workspace's chat script, so it uses
whatever model access the workspace's chats already have; no `requires_llm:`
line applies.

Adaptation:

- **Describe your own routines.** `catalog.toml` ships with wording for the
  built-in workspace routines only. Every other scheduled job in the adopting
  workspace will appear labelled from its id and tagged "Not described yet".
  Add a `[[job]]` entry (id = the job id in its cron line, plus `title`,
  `what`, and optionally `app`) for each routine the user cares about, in
  words the user would use.
- **Declare internal-timer services.** A supervisord program that refreshes
  something from a loop inside its own process leaves no trace on disk, so it
  is invisible until declared. For each such service in the adopting
  workspace, add a `[[service]]` entry with `every_seconds`, `every`,
  `only_while_open`, `app`, and `demo_env` if it has a sample-data switch.
- **Wrap existing cron jobs in the run recorder.** Jobs created before
  adoption still appear (discovery reads `/etc/cron.d` directly), but without
  run history or the automatic/manual distinction. Rewrite each one's command
  as `python3 /home/user/workspace/system/apps/routines/bin/record_run.py
  --job <id> --trigger scheduled -- <the real command>` (through the
  `manage-scheduled-tasks` skill, so the durable copy under
  `data/.state/cron.d/` is updated too).
- **Port 8082.** The app listens on 8082 (overridable with `ROUTINES_PORT`).
  If the adopting workspace already has a program on that port, pick another
  and change both `routines.conf`'s `--url` and the environment it runs with.
- **Merge the AGENTS.md section.** If the adopting workspace's AGENTS.md has
  drifted from the stock one, carry over just the "Anything that runs on its
  own is shown in the Routines app" section rather than replacing the file.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment. It shells out only
to `supervisorctl` (already part of every workspace) and to the workspace's
own `run_job.sh` and `message_chat.py` scripts, and its Python dependencies
(Flask, flask-sock, loguru, pydantic, imbue-common) install from its own
`pyproject.toml` like any other app.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-10-07) -- First version: one window listing every scheduled job and background refresher, with health, run history, automatic/manual tagging, and run-now, pause, reschedule and fix-it controls.

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
