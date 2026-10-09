# Design decisions

1. **Indexes.** `employees.emp_code` is unique. `(department, emp_code)` serves employee lists; `(department, joined_on)` and `(joined_on, department)` serve headcount queries with and without a department filter. Attendance `(emp_code, date)` is unique. `(date, emp_code)` supports list ordering; status/date and employee/status/date support filtered lists; employee/punch-in supports punch-out; date/late-minutes supports the leaderboard. I rejected a `work_hours` index because no query filters or sorts on it.

2. **Punch-in race.** A request normalizes its timestamp, derives the attendance date, then attempts one insert. MongoDB's unique `(emp_code, date)` index accepts one request. Duplicate-key errors from the others become 409; there is no check-then-insert race.

3. **Ties.** The pipeline uses `$rank` on total late minutes, then filters for `rank <= limit`. Ties at the cutoff share a rank and all remain in the response, ordered by employee code.

4. **Headcount.** The summary starts with employees who joined by month end, then looks up logs. An employee remains in the outer pipeline when that lookup is empty, so zero-log employees still count.

5. **100x data.** I would measure the workload, then add a daily rollup for analytics while retaining raw attendance as the audit source. I would review index storage and write cost before adding indexes.
