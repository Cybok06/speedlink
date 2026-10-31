Run the Emma Data package report
==============================

1. Install the runner's dependencies if needed:

   ```powershell
   python -m pip install requests python-dotenv reportlab
   ```

2. Add your own API key to the `.env` file in this project:

   ```dotenv
   EMMA_DATA_API_KEY=your_api_key_here
   ```

   Alternatively, set it for the current PowerShell session:

   ```powershell
   $env:EMMA_DATA_API_KEY = "your_api_key_here"
   ```

3. Run from the project folder:

   ```powershell
   python fetch_emma_packages.py
   ```

The report is saved to `output/pdf/emma_data_packages.pdf`. To choose a different path:

```powershell
python fetch_emma_packages.py --output output/pdf/emma_prices.pdf
```

The runner makes one GET request to `https://emmadataking.com/api/v1/packages`.
It sends `Content-Type: application/json` and `X-API-Key`; the HTTP client sets
the Host header from the URL. It does not retry automatically or follow redirects.
API errors leave an existing report intact. The key is never included in the PDF.

Packages are grouped using the network names returned by the API, with package ID,
capacity, capacity value, volume in MB, currency, price, and stock status.
Each successful run replaces the previous report. This runner only reads the
catalogue; it does not submit orders or change the site's provider settings.

Site provider switching
=======================

Set `EMMA_DATA_API_KEY` on the server (the same variable as the runner), then restart
the app. Optionally set `EMMA_DATA_TIMEOUT=45` for the purchase response timeout.
On Admin > Services, the supported service cards show a Provider selector with
Default and Emma Data. The selection saves immediately for future orders across
the dashboard, public checkout, and storefront checkout. Existing orders keep
their original provider. The API/OFF setting still controls automatic submission.

| Service | Emma Data network |
| --- | --- |
| MTN Normal (or MTN) | YELLO |
| MTN Express | EXPRESS_YELLO |
| AT iShare | at |
| AT Bigtime | AT_PREMIUM |
| Telecel | TELECEL |

Default preserves the service's existing Campus Data routing configuration.
The switch is stored separately as `fulfillment_provider`; no data migration is
needed. Offer volumes are converted from decimal MB to a numeric GB capacity:
5000 MB sends `"capacity": "5"`. Existing retail prices are unchanged.

Emma Data purchases use `POST /api/v1/purchase`, an X-API-Key header, and a unique
reference saved on the order line. Completed responses mark the line delivered;
an order is delivered immediately when all non-skipped lines are delivered.
Errors remain visible in the saved API response under the existing charged-order
workflow, so administrators can review them. Purchases are never automatically
retried or routed to another provider after an error. HTTP 429 (60 requests/minute)
is saved as a submission failure for review.

Pending and processing Emma Data lines are checked by `order_status.py` every
three minutes by default using `GET /api/v1/order-status/:reference`. The reference
is the original purchase `ref`, saved as `provider_request_order_id`, not the EDK
transaction ID. This also checks unconfirmed purchases that have a reference but
no EDK ID. The worker runs independently of the Campus Data API key.

The returned `data.status` updates the line and overall order in MongoDB;
`COMPLETED` becomes `delivered`. Delivered lines are not polled again. Failed API
checks preserve delivery status and save diagnostics for a later check. Failed
purchases still require administrator review. No status check resubmits a purchase.
Each run checks at most 200 Emma Data lines and stops Emma requests on HTTP 429.
The existing order batch limit defaults to 25 orders per cycle; larger backlogs
continue in later cycles. The app must remain running for scheduled checks.
