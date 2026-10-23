# Beat trigger tasks

Celery beat sends a single task, not a chain. Both chains in
`app/jobs/tasks.py` (`form4_chain()`, `thirteen_f_poll()`) are built and tested,
but nothing sends them on a clock. Each needs a small task of its own that
builds the chain and applies it, plus a beat entry that sends that task.

Both tickets follow the same rules:

- **The trigger only enqueues.** It calls `.apply_async()` on the chain and
  returns at once. It never calls `.get()` on the result: waiting on a subtask
  from inside a task can deadlock the worker, and Celery raises
  `RuntimeError` if you try.
- **No lock, no `ingestion_run` row.** The trigger is not a job. Each link
  takes its own job's lock and writes its own row. If a chain is enqueued
  while yesterday's is still running, the links that overlap return
  `{"skipped": "locked"}`, and every link after them skips too. So a
  double-fire is a non-event without the trigger doing anything.
- **On `compute`.** The trigger runs in milliseconds. Each link still goes to
  its own queue (ingestion or compute), as set in its decorator.
- **Returns what it enqueued**: `{"chain": "<name>", "task_id": "<id of the
  last link>"}`, so Flower and the result backend show where the chain went.
- **Beat entries expire**, as `heartbeat` does. If the worker is stopped, a
  trigger nobody consumed should not pile up and fire all at once later. Use
  `options: {"expires": ...}`, shorter than the interval between sends.
- **Crontabs are New York time.** The app's `timezone` is already
  `America/New_York`.

---

## Daily Form 4 trigger

**Why.** Insider trades are push-based, one day at a time. If nobody runs
`crawl-form4 --catch-up` and `ingest-form4`, the insider feed silently stops
at the last day someone ran them by hand.

**AC**

- `daily_form4` task in `app/jobs/tasks.py` that applies `form4_chain()` and
  returns `{"chain": "form4", "task_id": ...}`
- Beat entry `daily-form4` in `app/jobs/schedule.py`, sent once a day after
  EDGAR publishes that day's index
- The entry expires in less than 24 hours
- Test: calling the task directly with `apply_async` patched enqueues a chain
  of `crawl_form4_daily` → `ingest_form4`, and calls `.get()` on nothing
- Test: the beat entry names `app.jobs.tasks.daily_form4`, and its crontab is
  in the window below
- `make` target or README line showing how to fire it once by hand
  (`celery call app.jobs.tasks.daily_form4`)

**Notes.** EDGAR publishes the daily form index at about 10pm New York time.
Run at about 23:30. If the crawl reaches a day that is not published yet, it
stops there without failing, and the watermark only moves past days it read
in full, so a run that fires too early just picks that day up the next night.
06:00 the next morning works as well; pick one and say why in the schedule's
comment.

The crawl and its ingest are both on the ingestion worker, which runs one
task at a time. A long 13F backfill will delay the Form 4 crawl. That is
acceptable; the lock means the two never run the same job twice.

---

## 13F poll trigger, with a filing-season cadence

**Why.** New 13Fs only arrive in bursts around each quarter's deadline.
Polling a hundred filers' submissions JSON every day of the year is roughly
400 EDGAR requests a day, nearly all of which find nothing. Polling weekly
all year misses a whole week of the busiest period.

**AC**

- `poll_13f` task in `app/jobs/tasks.py` that applies `thirteen_f_poll()`
  (discover → ingest → recompute → refresh) and returns
  `{"chain": "13f-poll", "task_id": ...}`
- `in_filing_season(today)` in `app/core/periods.py`: true inside the window
  around any quarter's `filing_deadline()`
- Beat entry `poll-13f`, sent daily. Inside the season it applies the chain;
  outside it, it applies the chain only on one fixed weekday, and on other
  days returns `{"chain": "13f-poll", "skipped": "out of season"}`
- The entry expires in less than 24 hours
- Tests for `in_filing_season` at the edges of each quarter's window, across
  a year boundary (2025Q4's deadline falls in February 2026)
- Test: in season, the task enqueues the four-link chain; out of season on a
  non-poll weekday, it enqueues nothing

**Notes.** One daily entry with the season check inside the task is better
than two beat entries (a daily one and a weekly one). With two, every
season-day that is also the weekly day fires the chain twice. The lock makes
that harmless, but it is noise in Flower.

`filing_deadline()` is up to three days early, because it does not roll
forward over weekends and holidays. Widen the window on the late side by at
least that much.

**Open question: which window?** The spec says "outside the ~three weeks
after each due date this finds nothing". But most 13F-HRs arrive in the two
weeks *before* the deadline, and what comes after it is mostly amendments and
late filers. Suggested window: deadline − 14 days to deadline + 21 days.
Confirm before building.

Pass `ingest` options (`concurrency`, `limit`) through the beat entry's
`kwargs` only if there is a reason to. The task defaults are what `backfill`
uses by hand.
