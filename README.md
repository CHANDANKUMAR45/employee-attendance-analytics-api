# Employee Attendance & Analytics API

A small HR attendance backend built with FastAPI and MongoDB. All the code is in one file: `app/main.py`.

## How to run

1. Install Python 3.11 or newer.
2. Install the packages:
   ```
   pip install -r requirements.txt
   ```
3. Tell the app where MongoDB is. Create a file named `.env` in the project root (the folder that contains `app/`) with two lines:
   ```
   MONGO_URI=your MongoDB connection string
   MONGO_DB=attendance_db
   ```
   Real environment variables are used first. The `.env` file is only a shortcut for local work. Never commit it.
4. Start the server from the project root:
   ```
   uvicorn app.main:app --port 8000
   ```
   If your computer says `uvicorn` is not found, use this instead:
   ```
   python -m uvicorn app.main:app --port 8000
   ```
5. Open `http://localhost:8000/docs` to try every endpoint in the browser.

The app creates all the database indexes by itself when it starts. You do not need to do anything.

## What is built

Every endpoint in `openapi.yaml`:

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

The four analytics endpoints use MongoDB aggregation pipelines.

## What I tested

- I ran the app against MongoDB Atlas with the 9 sample records. All endpoints answered, and the leaderboard result matched what I worked out by hand.
- The explain endpoint showed `IXSCAN` and no `COLLSCAN` for all five endpoints on that small data.

## What I did not test

- I did not test with about 100,000 records, so I do not know the real speed at that size.
- I did not test with the hidden dataset.
- I did not run many requests at the same time against a real database. Punch-in, punch-out and PATCH are written so that the database decides the winner, but I only checked this on a small in-memory test database.

## Choices I made where the contract was not clear

- `moving_avg_7d` is the average of the rounded `attendance_rate` values.
- The trend range can be at most 92 days, counting both the first and last day. A 93-day range gives 422.
- Punch-out returns 409 (already punched out) before it checks the time (422).
- If two PATCH requests change the same record at the same time, one wins and the other gets 409.
- The analytics endpoints need MongoDB 5.0 or newer.