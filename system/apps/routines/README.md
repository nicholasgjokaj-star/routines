# routines

Every scheduled job and background refresher in this workspace, with its
schedule, health, and run history.

## Why it needs no list of what exists

The app stores no inventory of routines. Every request re-reads the files that
actually define them, so a routine someone adds, re-times, pauses or deletes is
reflected immediately with no edit here:

| What | Read from | Gives |
| --- | --- | --- |
| Scheduled jobs | `/etc/cron.d/`, plus `data/.state/cron.d/` for an entry not yet installed live | the cadence, the command, the log path, whether it is paused |
| Job health | `data/.state/jobs/<id>/` (`run_job.sh`'s own state) | last attempt, last success, consecutive failures |
| Services | supervisord's RPC socket | whether a long-running refresher is up |
| Run history | `data/.apps/routines/runs.jsonl` | every run, with what triggered it |

`src/routines/catalog.toml` supplies only *wording*, as an override. A routine
with no entry there is still discovered and still shown -- labelled from its id
and tagged "Not described yet". The single exception is a service whose refresh
loop lives inside its own process: that cadence exists nowhere on disk, so the
catalog is where it is declared.

## The run log, and automatic vs manual

`run_job.sh` tracks two timestamps and a failure count, which cannot say whether
the last run was scheduled or someone pressing "Run now". So both paths go
through `bin/record_run.py`, which runs the command and appends one JSON line
per run -- start, finish, exit code, trigger, and the tail of the output.

```
cron entry:  ... run_job.sh <id> --every 6h python3 .../record_run.py --job <id> --trigger scheduled -- <cmd>
Run now:     .../record_run.py --job <id> --trigger manual --update-job-state -- <cmd>
```

The recorder is transparent: it echoes the command's output and returns its
exit code unchanged, so `run_job.sh`'s catch-up and retry semantics are
untouched. It is standard-library only and imports nothing from this package,
so a scheduled job keeps running while this app's environment is being
reinstalled.

`--update-job-state` exists for the manual path only. A manual run has no
`run_job.sh` above it, so the recorder writes `last_attempt` / `last_success`
itself -- otherwise a pull the user just did by hand would not count toward the
schedule, and the scheduled run would repeat it minutes later.

## Adding a routine

Nothing needs registering. Write the cron entry (see the
`manage-scheduled-tasks` skill), wrapping the command in `record_run.py` per
the line above, and it appears. See the "Anything that runs on its own is shown
in the Routines app" section of `AGENTS.md` for the two obligations in full.
