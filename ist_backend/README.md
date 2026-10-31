# IST API

This module is registered in the existing Speedlink Flask application at
`/api/ist`; it does not require a separate backend service.

It imports `istdb.db`, using a dedicated Atlas connection and the `kingollies`
database. Set `IST_MONGODB_URI` on the Speedlink backend to the supplied full Atlas
URI before deploying. Speedlink's existing `db.py` remains unchanged.
Run `python istdb.py` to explicitly verify connectivity; requests handle database
unavailability through the IST API's existing error handler. Existing records
are not migrated automatically when switching clusters.

Endpoints:

- `GET /api/ist/search?q=...`: authenticated global search; returns up to ten
  student matches and ten payment matches, including payment details.
- `GET /api/ist/notifications`: latest fifty registration/payment notifications
  with per-user read state and unread count.
- `POST /api/ist/notifications`: JSON `{ "ids": ["notification-id"] }` marks
  the selected notifications read for the signed-in user in `notification_reads`.
- `POST /api/ist/auth/login`: JSON `{ "username": "admin", "password": "1234", "remember": false }`.
- `GET /api/ist/auth/me`: `Authorization: Bearer <token>`.
- `POST /api/ist/auth/logout`: same authorization header; revokes the token.
- `GET /api/ist/admins`: lists accounts and returns the current administrator.
- `POST /api/ist/admins`: creates an account with `username`, `password`, `name`,
  `email`, `phone`, and `role` (optional `status`, defaults to `Active`).
- `PATCH /api/ist/admins/<username>`: edits profile, role, optional password, and
  status; also accepts just `{ "status": "Inactive" }` or `Active`.

Admin management requires an active Admin or Super Admin bearer session. Only
Super Admin may create or modify Super Admin accounts. Self-deactivation and
self-role changes are blocked. The initial `admin` account retains its Super
Admin role. Username is immutable and normalized to lowercase; MongoDB's unique
`_id` prevents duplicate accounts, including concurrent creation requests.
Deactivation and password changes revoke that account's existing sessions.

Passwords use Werkzeug password hashing. Session tokens are random and only
their SHA-256 digests are stored. Database expiry indexes clean up sessions and
login attempts; authentication also checks expiry immediately.

Configuration:

- `IST_ALLOWED_ORIGINS`: comma-separated browser origins; defaults to
  `https://ist-record-keeping.onrender.com`. No trailing slashes.
- `IST_ADMIN_PASSWORD`: optional initial admin password; defaults to the requested
  `1234`. This only applies when the admin document does not yet exist.

Deploy Speedlink's existing `app:app` entrypoint and redeploy the IST frontend.
The database account must be able to write and create indexes in `ist_records`.
MongoDB creates the database on the first write; no manual SQL schema is needed.

The API uses bearer authorization independently of Speedlink cookies. The
Speedlink subscription guard exempts only the `ist` blueprint. Every future
IST data endpoint must validate `authenticated_session()` before returning data.

Tests use mongomock and do not access the live database:

```
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p test_ist_auth.py -v
```

Login and Admin Management are integrated. Other pages retain their frontend data.

Settings endpoints:

- `GET /api/ist/settings`: authenticated active users receive config, defaults,
  version, and `canEdit`.
- `PATCH /api/ist/settings`: Admin/Super Admin submit `{config, version}`.
  Validates fields and uses an atomic version check to reject stale writes (409).
- `POST /api/ist/auth/password`: `{currentPassword, newPassword}` changes the
  caller's password and revokes other sessions.

Configuration is stored in `ist_records.settings` and initialized only once.
Security policy applies to new login sessions and new passwords, including admin
password resets. Existing passwords and sessions retain their original validity.
Remember me changes browser storage only; all sessions use the configured duration.

Student endpoints:

- `GET /api/ist/students`: real saved student records, newest registrations first.
- `POST /api/ist/students`: validated registration including stable `requestId`,
  required manually entered `indexNumber`, student details, configured programme/level/year, `amountPaid`, initial payment
  details, `consentChecked`, and souvenir selections by configured item ID.
- `GET /api/ist/students/<id>`: saved profile.

Registration requires Admin, Super Admin, or Registrar; reads require an active
session. Fees, payment status, and registrar identity are assigned on the server.
Index numbers are entered by staff, trimmed, and validated (maximum 100 characters,
no spaces). Registration does not advance the legacy settings sequence.
Initial payment and souvenir data are part of the student record. A unique
index and request ID prevent duplicate indexes and repeated registration inserts.
Duplicate index numbers return HTTP 409 so staff can correct the entered value.

Tracking endpoints:

- `GET /api/ist/payments`: ledger including initial registration payments and
  current student payment status; returns `canRecord` for the caller.
- `POST /api/ist/payments`: `studentId`, stable UUID `requestId`, `amount`,
  `method`, `date` (YYYY-MM-DD), `reference`, optional `notes`.
- `GET /api/ist/souvenirs`: one record per student/item and `canIssue`.
- `POST /api/ist/souvenirs/issue`: `studentId`, `itemId`.

Payments are stored as events in each student document and updated atomically
with the balance using a compare-and-set retry. No cross-collection transaction
is required. Duplicate request IDs and references per student are rejected or
returned idempotently. Integer cents prevent balance rounding errors. Noncash
payments require a reference; zero, negative, fractional-cent, future-date and
excess payments are rejected. Issuance uses a compare-and-set of item snapshots,
retaining original issuer/date on repeated calls. Newly configured items become
available to existing students; historical issued items remain available when
removed from configuration.

Accountability and audit endpoints:

- `GET /api/ist/accountability?year=All`: signed-in users receive student and
  programme summaries, collection totals, year options, and generation time.
- `POST /api/ist/accountability/export`: `{format: "CSV" | "Print/PDF", year}`
  records an export request; the browser performs the download or print.
- `GET /api/ist/audit-logs`: Admin/Super Admin only. Parameters: `search`,
  `module`, `severity`, `page`; responses include filtered counts and pagination.

Registration, payment and issuance events derive from the same atomic student
writes. Administrator/settings/password audit events are embedded in the same
MongoDB update as the corresponding change. Authentication/export activity is
stored in `audit_logs` with no TTL. Secrets and tokens are never audit details.
Audit endpoints provide no modification/deletion API. History is not a claim of
immutable storage against database administrators or a specific retention policy.

`GET /api/ist/dashboard?year=All` returns stats, recent students/payments/activity,
programme counts, 30-day daily registration and collection series, payment method
counts, current-user identity, and quick-action permissions. Requires an active
bearer session; selected years must be present in records or the configured year.
Balances use integer cents and the same snapshot as the payment ledger. Initial
registration payments are included. No enrolments produces zero totals and empty
recent-record lists rather than seeded records. Chart series use UTC/Ghana dates
and full ISO dates internally, keeping different years separate.
