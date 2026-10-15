**# Employee Attendance & Analytics API**

A robust, production-ready RESTful service built with FastAPI and MongoDB for managing employee attendance, handling manual regularizations with immutable audit trails, and executing high-performance analytical queries across 100,000+ attendance records.

\---

**## 1. Prerequisites**

\- **\*\*Python\*\***: 3.11 or newer (tested with Python 3.11+)

\- **\*\*MongoDB\*\***: 6.0 or newer (tested with MongoDB 7.0+ features including \`$densify\` and \`$setWindowFields\`)

\- **\*\*Package Manager\*\***: \`pip\`

\---

**## 2. Installation Steps**

Clone the repository and set up a virtual environment:

\`\`\`bash

\# Create and activate a virtual environment

python -m venv .venv

\# On Linux / macOS:

source .venv/bin/activate

\# On Windows:

.\\.venv\Scripts\activate

\# Install dependencies

pip install -r requirements.txt

\`\`\`

\---

**## 3. Environment Variable Configuration**

The application reads configuration from environment variables. If a local \`.env\` file is present, it is loaded without overriding existing system environment variables (\`override=False\`), ensuring containerized runners and automated graders can reliably inject credentials.

\| Variable | Description | Default Value |

\|---|---|---|

\| \`MONGO_URI\` | MongoDB connection string | \`mongodb://localhost:27017\` |

\| \`MONGO_DB\` | MongoDB database name | \`attendance_db\` |

Example \`.env\`:

\`\`\`env

MONGO_URI=mongodb://localhost:27017

MONGO_DB=attendance_db

\`\`\`

\---

**## 4. MongoDB Setup & Seeding**

1\. **\*\*Start MongoDB (Local or Docker)\*\***:

   \`\`\`bash

   # Using Docker:

   docker run -d -p 27017:27017 --name mongo-attendance mongo:7.0

   \`\`\`

   *\*Alternatively, MongoDB Atlas free tier (M0) connection strings can be supplied via \`MONGO_URI\`.\**

2\. **\*\*Seed Sample Data (Optional)\*\***:

   \`\`\`bash

   python sample_seed.py

   \`\`\`

*\*Note: All required indexes are automatically and idempotently created at application startup by the FastAPI lifespan handler.\**

\---

**## 5. Running the Application**

Start the FastAPI application with Uvicorn:

\`\`\`bash

uvicorn app.main\:app --port 8000

\`\`\`

To enable live reloading during development:

\`\`\`bash

uvicorn app.main\:app --port 8000 --reload

\`\`\`

\---

**## 6. Interactive API Documentation**

Once the server is running, the interactive OpenAPI documentation can be accessed in any web browser:

\- **\*\*Swagger UI\*\***: [[http://localhost:8000/docs\](http://localhost:8000/docs)](http://localhost:8000/docs]\(http://localhost:8000/docs\))

\- **\*\*ReDoc UI\*\***: [[http://localhost:8000/redoc\](http://localhost:8000/redoc)](http://localhost:8000/redoc]\(http://localhost:8000/redoc\))

\- **\*\*OpenAPI JSON\*\***: [[http://localhost:8000/openapi.json\](http://localhost:8000/openapi.json)](http://localhost:8000/openapi.json]\(http://localhost:8000/openapi.json\))

\---

**## 7. Running Automated Tests**

Run the test suite using \`pytest\`:

\`\`\`bash

\# Run all tests with verbose output

pytest tests/test_api.py -v

\`\`\`

The test suite covers:

\- Core business rules R1 through R10 (IST time zones, overnight shift dates, late minutes grace thresholds, round half-up work hours, overtime boundaries, working days calculation, half-day presence weights).

\- Concurrency and race safety (atomic inserts with unique constraints, optimistic lock checks on regularization history).

\- Request payload validation (epoch millisecond validation, email regex, emp_code pattern).

\- Status codes (200, 201, 404, 409, 422, 503).

\- Analytics aggregation correctness and parameter validation.

\---

**## 8. Implemented Endpoints Overview**

**### System**

\- \`GET /health\`: Readiness probe. Returns HTTP 200 \`{"status": "ok"}\` if MongoDB answers a ping; returns HTTP 503 if unreachable.

**### Employees**

\- \`POST /employees\`: Creates an employee with a customer-assigned \`emp_code\` (validated against \`^EMP\d{4,6}$\`), name, email, department, shift times, and \`joined_on\`. Concurrently safe via a unique database index on \`emp_code\`.

\- \`GET /employees\`: Lists employees sorted by \`emp_code\` ascending with pagination (\`page\`, \`page_size\`) and optional department filtering. \`total\` accurately reflects the filtered count.

**### Attendance**

\- \`POST /attendance/punch-in\`: Records a punch-in event. Attributions strictly follow IST time zone and overnight shift rollover rules (R1). Computes \`late_minutes\` according to the 10-minute grace rule (R2). Protected against duplicate punches by a unique compound index on \`(emp_code, date)\`.

\- \`POST /attendance/punch-out\`: Closes the employee's most recent open attendance record whose \`punch_in <= punched_at\`. Computes \`work_hours\` (rounded half-up to 2 decimals), \`overtime_minutes\` (minimum 30 minutes threshold), and \`half_day\` flag (< 4.50 hours). Atomic update prevents simultaneous punch-out races.

\- \`GET /attendance\`: High-performance listing of attendance records sorted by \`date\` descending, then \`emp_code\` ascending. Supports filters for \`emp_code\`, \`date_from\`, \`date_to\`, and \`status\`. Backed by compound database index for fast execution at 100k+ documents.

\- \`PATCH /attendance/{emp_code}/{date}\`: Regularizes one day's record. Recomputes derived fields (\`late_minutes\`, \`work_hours\`, \`overtime_minutes\`, \`half_day\`) and atomically appends an immutable entry to \`history\` tracking changes. Uses optimistic concurrency controls to avoid lost audit history.

**### Analytics**

\- \`GET /analytics/employees/{emp_code}/monthly\`: Computes monthly attendance metrics for an employee, including working days from \`joined_on\`, present days (Mon-Fri only, half-day = 0.5), leave days, late count, total late minutes, total overtime, and attendance percentage.

\- \`GET /analytics/departments/summary\`: Department-level monthly summary aggregating headcount (including employees with zero logs), present days, mean work hours across present records, late metrics, leave counts, and on-duty counts.

\- \`GET /analytics/leaderboard/late\`: Ranks employees by total late minutes for a given month using standard competition ranking (\`$rank\`, e.g., 1, 2, 2, 4). The \`limit\` parameter applies after ranking so ties at the boundary are preserved.

\- \`GET /analytics/departments/{department}/trend\`: Generates a daily department series across a date range (up to 92 days) using \`$densify\` for gap filling. Computes daily working day flags, headcount, present count, attendance rate, and a 7-day moving average over preceding non-null rates.

**### Administration**

\- \`GET /admin/explain/{endpoint}\`: Executes MongoDB \`explain\` with \`executionStats\` verbosity on the query or aggregation pipeline of the requested endpoint (\`attendance_list\`, \`employee_monthly\`, \`department_summary\`, \`late_leaderboard\`, \`department_trend\`). Verifies \`IXSCAN\` usage without collection scans.

\---

**## 9. Assumptions and Design Decisions**

1\. **\*\*Storage of Derived Values\*\***: As mandated by \`DATA_MODEL.md\`, derived fields (\`work_hours\`, \`late_minutes\`, \`overtime_minutes\`, \`half_day\`) are computed and written by the API at the time of punch or regularization. Analytics pipelines read these stored values directly.

2\. **\*\*Instant Timestamps\*\***: All API timestamps (\`punched_at\`, \`punch_in\`, \`punch_out\`, \`created_at\`, \`history[].at\`) are strictly epoch milliseconds integers truncated to whole seconds. In MongoDB, these are stored as BSON UTC datetimes.

3\. **\*\*Optimistic Locking\*\***: To ensure concurrent regularizations do not overwrite audit logs, \`find_one_and_update\` verifies the length of \`history\` array before appending new changes.

4\. **\*\*No Secrets / Clean Submission\*\***: No \`.env\`, database dump, credentials, or Dockerfile are included in the repository submission.   this is read.me
