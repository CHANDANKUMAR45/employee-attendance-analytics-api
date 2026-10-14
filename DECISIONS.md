# DECISIONS.md

1. **Indexes.**
   - `employees`: unique `emp_code` (a duplicate gives 409), `(department, emp_code)` (department list and trend), `joined_on` (headcount).
   - `attendance_logs`: unique `(emp_code, date)` (stops double punch-in, serves monthly and per-employee reads), `(date desc, emp_code)` (list order and the month range for the leaderboard), `(status, date, emp_code)` (status filter), `(emp_code, punch_in desc)` (find the record to punch out).
   - Rejected: copying `department` into every log. It saves a join but becomes wrong when an employee changes department.

2. **Punch-in race.** Both requests work out the same date and call `insert_one`. The unique `(emp_code, date)` index lets only one insert succeed. The other one gets `DuplicateKeyError`, and I return 409. I never check first and insert later.

3. **Ties.** `$rank` gives 1, 2, 2, 4. I keep rows where `rank <= limit` after ranking, so every tied employee at the cutoff is returned. The list can be longer than `limit`.

4. **Headcount.** The summary starts from `employees` who joined on or before the month end, then joins their logs. An employee with no logs stays as one row, so they are still counted.

5. **At 100x data.** Keep daily rollups per department, updated on each punch, instead of scanning raw logs on every request. Also use cursor paging instead of `skip`.