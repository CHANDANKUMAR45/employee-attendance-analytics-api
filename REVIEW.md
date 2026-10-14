# REVIEW.md

These are the problems I found in the starter `app/main.py`, and how I fixed each one.

| # | Where | What is wrong | How you would notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `/health` | It always says `ok`. It never asks MongoDB. | Stop MongoDB. Health still returns 200, but it should return 503. | It now pings MongoDB. If the ping fails, it returns 503. |
| 2 | `MongoClient(...)` | PyMongo gives back datetimes without a time zone. Mixing them with datetimes that have a time zone causes a `TypeError`. | Subtracting a stored `punch_out` from a normal datetime crashes with a 500. | The client uses `tz_aware=True`. Every conversion goes through `ms_to_dt` and `dt_to_ms`. |
| 3 | `compute_late_minutes` | It uses the punch-in's own day and time zone (server time), not IST. | On a UTC server, a 10:05 IST punch-in looks like 04:35, so late time is wrong. | The shift start is built as an IST time for the attendance date (`shift_window`). |
| 4 | `compute_late_minutes` | It rounds to minutes first, then checks `> 10`. The rule says "more than 10:00 after the start". | On a 09:30 shift, 09:40:01 gives 10 minutes, and `10 > 10` is false, so it returns 0. It should return 10. | It compares in seconds (`secs > 600`), then divides by 60. |
| 5 | `compute_late_minutes` | It ignores overnight shifts and does not cut the time to whole seconds. | A 22:00-06:00 shift with a punch-in at 02:00 gets the wrong date and start. | `attendance_date` follows rule R1. `ms_to_dt` cuts to whole seconds. |
| 6 | `compute_work_hours` | `round()` rounds half to even, and floats are not exact. | Rule R4 needs half-up rounding. Some values round the wrong way. | I use `Decimal` with `ROUND_HALF_UP`. |
| 7 | `compute_overtime` | There is no 30-minute minimum (R3). The end of an overnight shift is not moved to the next day. It mixes datetimes with and without a time zone. | Punch-out at 18:40 on an 18:30 shift gives 10. It should give 0. A punch-out read from MongoDB gives a `TypeError`. | `shift_window` gives the real shift end. Overtime counts only if it is 30 minutes or more. |
| 8 | `EmployeeIn` | There is no validation at all. | `POST /employees` with `shift_start: "25:99"` or `joined_on: "abc"` returns 201. | I added patterns and checks. Bad input now gives 422. |
| 9 | `create_employee` | It checks first, then inserts, and there is no unique index. Two requests at the same time can both succeed. `created_at` is local time, not epoch milliseconds. | Send the same `emp_code` twice at the same time. You can get two 201 answers. | A unique index on `emp_code`. `DuplicateKeyError` becomes 409. `created_at` is stored in UTC and returned in milliseconds. |
| 10 | `list_employees` | `skip = page * page_size` skips the first page. `total` ignores the department filter. There is no sort and no limit on `page` or `page_size`. | `?page=1` shows the second page. `?department=Sales` shows the total for everyone. | `skip = (page - 1) * page_size`. `total` uses the same filter. Sorted by `emp_code`. `page >= 1` and `page_size` from 1 to 100. |
| 11 | `punch_in` | If the employee does not exist, the code crashes with a 500. | Send `emp_code: "NOPE"`. | It now returns 404. |
| 12 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` uses the server's time zone and keeps milliseconds. `if body.punched_at` treats `0` as missing. | The same request gives a different date on a UTC server and on an IST laptop. | It uses `ms_to_dt`. It checks `is not None`. The date comes from rule R1 in IST. |
| 13 | `punch_in` | It checks first, then inserts. | Send 10 identical requests at once. Several can return 201. | A unique index on `(emp_code, date)`. It inserts and turns `DuplicateKeyError` into 409. |
| 14 | `PunchInIn` | `status` can be any text. `punched_at` accepts values that look like seconds. | `{"status": "LEAVE"}` returns 201. | `status` is limited to `PRESENT`, `WFH`, `ON_DUTY`. `punched_at` must be a whole number of milliseconds in the allowed range. |
| 15 | `punch_in` response | It returns an `id` (the MongoDB `_id`) and datetimes instead of milliseconds. | The response contains `id`. | `record_out` builds the response. It never includes `_id`. |
| 16 | `list_attendance` | It loads the whole collection into memory, then sorts and pages in Python. | It is slow and uses a lot of memory with 100,000 records. | MongoDB does the `find`, `sort`, `skip` and `limit`, using indexes. |
| 17 | `list_attendance` | It sorts only by `date`, so pages can change order. It does not check `date_from > date_to`. It does not validate dates or `status`. It returns `id`. | `?date_from=2026-07-10&date_to=2026-07-01` returns 200. It should return 422. | Sort by `date` (newest first), then `emp_code`. It returns 422 for bad input. It uses `record_out`. |
| 18 | Startup | No indexes are created. | The explain output shows `COLLSCAN`. | `ensure_indexes()` runs at startup and is safe to run many times. |
| 19 | Old records | Some records have no `history` or `half_day`. The code would break on them. | The sample record `EMP0002 / 2026-07-14` has neither field. | `record_out` uses `.get()` with defaults (empty list and `false`). |
| 20 | `list_employees`, `list_attendance` | Filters use `if department:` and `if emp_code:`. An empty value means "no filter". | `GET /employees?department=` returns every employee. | The code now checks `is not None`, so an empty value is used as a real filter. |

## Things I checked and decided were fine

- `load_dotenv()` does not replace environment variables that already exist. So the real `MONGO_URI` and `MONGO_DB` win over `.env`. I kept it.
- The endpoints are normal `def` functions. FastAPI runs them in a thread pool, and the PyMongo client is safe to share between threads. I kept PyMongo and did not switch to Motor.
- The default shift times `09:30` and `18:30` match the contract.
- The default `MONGO_URI` of `localhost` is only a fallback for local work. The real value always comes from the environment.
