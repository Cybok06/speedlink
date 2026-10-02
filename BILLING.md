# Agent subscriptions

Customer/agent accounts pay GHS 15 (1,500 pesewas) per calendar month through the existing Paystack configuration. Administrators are exempt. Renewals are manual, with no automatic debit. A 24-hour grace period keeps the dashboard and store available after expiry. At the exact end of grace, access is blocked until payment is verified.

Accounts that have never subscribed receive a one-time 24-hour introductory grace period starting on their next login or authenticated dashboard/billing use. Its start is stored in `users.subscription_initial_due_at` using an atomic conditional update. Repeated logins and anonymous store visits cannot reset or start this period. Agents see a subscription announcement once each login session and a persistent reminder with their grace deadline while grace is active.

## Admin reporting

The admin sidebar links to `/admin/subscriptions`. Month selection uses GMT calendar months and the first successful verification date. Paid agents means unique agents with a successful receipt in that month; unpaid means agents without a receipt in that month. These are payment activity counts, not historical coverage or outstanding debt: early renewals may cover months with no new payment. Current access and grace deadlines are displayed separately. Accounts created after the selected month and deleted accounts are excluded from agent counts; revenue retains receipts for removed accounts. Search, status filters, and pagination narrow the list without changing the summary totals. Failed and pending payments are excluded from revenue.

An early payment adds a calendar month to the current expiry; an expired account starts a new month at verification time. Month-end dates clamp to the last valid day of the following month. Dates are stored as UTC and displayed as GMT.

## Deployment

- Deploy the code and templates together. No account migration or scheduled expiry job is required; access checks compare the stored expiry with the current time on every request.
- Keep the existing Paystack keys configured. Test in a staging database with Paystack test keys first.
- Set the Paystack dashboard webhook URL to `https://YOUR-DOMAIN/subscription/webhook`. This accepts signed events and restores access even if the payer closes the browser. If another deployment has an existing webhook handler, forward subscription events to this handler rather than replacing that integration.
- Ensure the externally generated callback URL uses your public HTTPS domain when behind a reverse proxy.
- Add an operational MongoDB index on `subscription_payments` for `{user_id: 1, created_at: -1}` as the history grows. Payment reference uniqueness uses MongoDB's built-in `_id` index.

Payments live in `subscription_payments`, separate from wallet deposits and purchases. Entitlements use `users.subscription_expires_at` and `users.subscription_payment_refs`. The expiry and reference are changed together in one conditional, atomic user update. Duplicate callbacks, webhook retries, and overlapping renewals cannot apply the same payment twice. If receipt recording fails after activation, retrying verification repairs the payment record without extending access again.

Agents can use **Check payment** in their history to recover payments after a lost callback. Unpaid dashboard requests redirect to Subscription; API/mutation requests receive HTTP 402 with `redirect_url`. Unpaid public stores show an unavailable dialog and reject checkout on the server.

## Validation

Run `python -m unittest discover -s tests -v`. Tests use `mongomock` (development dependency) and mocked Paystack responses; they do not contact the production database or make real payments.

Before launch, exercise a Paystack test checkout, signed webhook, browser return, cancelled payment, early renewal, and expired store on the deployed HTTPS staging URL. Local tests do not establish that dashboard webhook configuration or live credentials are correct.
