"""Isolated billing tests: never import the production db module."""
import hashlib
import hmac
import json
import sys
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from bson import ObjectId

fake_db = types.ModuleType("db")
enable_gridfs_integration()
fake_db.db = mongomock.MongoClient().test_billing
fake_db.client = fake_db.db.client
sys.modules["db"] = fake_db
fake_istdb = types.ModuleType("istdb")
fake_istdb.client = mongomock.MongoClient()
fake_istdb.db = fake_istdb.client.kingollies
sys.modules["istdb"] = fake_istdb
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import subscriptions as billing
from app import create_app


class BillingTests(unittest.TestCase):
    def setUp(self):
        for name in fake_db.db.list_collection_names():
            fake_db.db[name].delete_many({})
        self.uid = ObjectId()
        billing.users.insert_one({"_id": self.uid, "role": "customer", "status": "active", "email": "agent@example.com",
                                  "subscription_initial_due_at": datetime.utcnow() - timedelta(days=2)})
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user_id=str(self.uid), role="customer", subscription_csrf="test-token")
        self.keys = patch.object(billing, "get_paystack_keys", return_value={"secret_key": "sk_test_mock"})
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def test_ist_login_is_independent_of_unpaid_speedlink_session(self):
        response = self.client.post('/api/ist/auth/login', json={
            'username': 'admin', 'password': '1234',
        }, headers={'Origin': 'https://ist-record-keeping.onrender.com'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.get_json())
        self.assertEqual(response.headers['Access-Control-Allow-Origin'],
                         'https://ist-record-keeping.onrender.com')
        self.assertIsNone(fake_db.db.users.find_one({'_id': 'admin'}))
        self.assertIsNotNone(fake_istdb.db.users.find_one({'_id': 'admin'}))
    def payment(self, suffix="one"):
        payment = {"_id": billing.REFERENCE_PREFIX + suffix, "user_id": self.uid,
                   "email": "agent@example.com", "status": "pending", "created_at": datetime.utcnow()}
        billing.payments.insert_one(payment)
        data = {"status": "success", "reference": payment["_id"], "amount": 1500, "currency": "GHS",
                "metadata": {"purpose": "agent_subscription", "user_id": str(self.uid)},
                "customer": {"email": payment["email"]}}
        return payment, data

    def test_unpaid_dashboard_and_api_are_blocked(self):
        response = self.client.get("/customer/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/subscription"))
        response = self.client.post("/checkout", json={"cart": []})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json["code"], "subscription_required")

    def test_subscription_page_and_both_sidebar_links(self):
        response = self.client.get("/subscription")
        self.assertEqual(response.status_code, 200)
        self.assertIn("GH₵15".encode(), response.data)
        self.assertGreaterEqual(response.data.count(b'href="/subscription"'), 2)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_public_index_purchase_does_not_require_subscription(self):
        anonymous = self.app.test_client()
        for client in (anonymous, self.client):
            self.assertEqual(client.get("/").status_code, 200)
            response = client.get("/api/phone-numbers/eligibility?phone=0241234567")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["success"])
            self.assertNotIn("code", response.json)
            # Invalid purchase input must reach public checkout validation.
            for path in ("/public-payment-session", "/public-checkout"):
                response = client.post(path, json={"cart": []})
                self.assertEqual(response.status_code, 400)
                self.assertNotEqual(response.json.get("code"), "subscription_required")

    def test_public_phone_check_does_not_start_subscription_grace(self):
        billing.users.update_one({"_id": self.uid}, {"$unset": {"subscription_initial_due_at": ""}})
        response = self.client.get("/api/phone-numbers/eligibility?phone=0241234567")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("subscription_initial_due_at", billing.get_user(self.uid))

    def test_admin_phone_controls_remain_subscription_gated(self):
        response = self.client.post("/admin/phone-numbers/block", data={"phone": "0241234567"})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json["code"], "subscription_required")

    def test_admin_exempt_and_expiry_boundary(self):
        now = datetime.utcnow()
        self.assertTrue(billing.is_active({"role": "admin"}))
        self.assertFalse(billing.is_active({"subscription_expires_at": now}, now))
        self.assertFalse(billing.is_active(None))

    def test_month_end_early_renewal_and_duplicate(self):
        expiry = datetime(2030, 1, 31, 12)
        billing.users.update_one({"_id": self.uid}, {"$set": {"subscription_expires_at": expiry}})
        payment, data = self.payment()
        billing.apply_payment(payment, data)
        billing.apply_payment(payment, data)
        user = billing.get_user(self.uid)
        self.assertEqual(user["subscription_expires_at"], datetime(2030, 2, 28, 12))
        self.assertEqual(len(user["subscription_payment_refs"]), 1)

    def test_expired_subscription_starts_now(self):
        billing.users.update_one({"_id": self.uid}, {"$set": {"subscription_expires_at": datetime(2020, 1, 1)}})
        billing.apply_payment(*self.payment())
        expiry = billing.get_user(self.uid)["subscription_expires_at"]
        self.assertGreater(expiry, datetime.utcnow() + timedelta(days=27))
        self.assertLess(expiry, datetime.utcnow() + timedelta(days=32))

    def test_concurrent_duplicate_and_distinct_payments(self):
        billing.users.update_one({"_id": self.uid}, {"$set": {"subscription_expires_at": datetime(2030, 1, 15)}})
        first = self.payment("first")
        second = self.payment("second")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda p: billing.apply_payment(*p), [first, first, second, second]))
        self.assertEqual(billing.get_user(self.uid)["subscription_expires_at"], datetime(2030, 3, 15))

    def test_invalid_payment_cannot_activate(self):
        payment, data = self.payment()
        for field, value in [("amount", 1499), ("currency", "USD"), ("reference", "another"),
                             ("status", "pending"), ("metadata", {}), ("customer", {"email": "other@example.com"})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                billing.apply_payment(payment, {**data, field: value})
        self.assertFalse(billing.is_active(billing.get_user(self.uid)))

    def test_signed_webhook_and_retries(self):
        payment, data = self.payment()
        body = json.dumps({"event": "charge.success", "data": data}).encode()
        self.assertEqual(self.client.post("/subscription/webhook", data=body).status_code, 401)
        signature = hmac.new(b"sk_test_mock", body, hashlib.sha512).hexdigest()
        for _ in range(2):
            response = self.client.post("/subscription/webhook", data=body,
                headers={"x-paystack-signature": signature, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 200)
        self.assertTrue(billing.is_active(billing.get_user(self.uid)))
        self.assertEqual(len(billing.get_user(self.uid)["subscription_payment_refs"]), 1)

    def test_retry_repairs_receipt_after_interrupted_activation(self):
        payment, data = self.payment()
        with patch.object(billing.payments, "update_one", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                billing.apply_payment(payment, data)
        expiry = billing.get_user(self.uid)["subscription_expires_at"]
        billing.apply_payment(payment, data)
        self.assertEqual(billing.get_user(self.uid)["subscription_expires_at"], expiry)
        self.assertEqual(billing.payments.find_one({"_id": payment["_id"]})["status"], "success")

    def test_cancelled_payment_can_be_replaced(self):
        payment, data = self.payment()
        response = Mock()
        response.json.return_value = {"status": True, "data": {**data, "status": "abandoned"}}
        with patch.object(billing.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                billing.verify_payment(payment)
        self.assertEqual(billing.payments.find_one({"_id": payment["_id"]})["status"], "abandoned")

    def test_payment_owner_and_csrf(self):
        payment, _ = self.payment()
        self.assertEqual(self.client.post("/subscription/pay").status_code, 400)
        with self.client.session_transaction() as sess:
            other = ObjectId()
            billing.users.insert_one({"_id": other, "role": "customer"})
            sess["user_id"] = str(other)
        self.assertEqual(self.client.get("/subscription/callback?reference=" + payment["_id"]).status_code, 404)

    def test_initialize_server_controls_amount_and_metadata(self):
        response = Mock()
        response.json.return_value = {"status": True, "data": {"authorization_url": "https://checkout.paystack.com/test"}}
        with patch.object(billing.requests, "post", return_value=response) as post:
            result = self.client.post("/subscription/pay", data={"csrf_token": "test-token", "amount": "1"})
        self.assertEqual(result.status_code, 303)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["amount"], 1500)
        self.assertEqual(sent["metadata"]["user_id"], str(self.uid))

    def test_store_modal_and_checkout_block_for_anonymous_visitors(self):
        fake_db.db.stores.insert_one({"slug": "test-shop", "owner_id": self.uid, "status": "published"})
        anonymous = self.app.test_client()
        result = anonymous.get("/s/test-shop")
        self.assertEqual(result.status_code, 503)
        self.assertIn(b"Unavailable Now, Contact Store Owner", result.data)
        result = anonymous.post("/store-checkout/test-shop", json={"cart": [{}]})
        self.assertEqual(result.status_code, 503)
        self.assertFalse(anonymous.get("/api/store-availability/test-shop").json["available"])
        billing.apply_payment(*self.payment())
        self.assertTrue(billing.store_is_active(fake_db.db.stores.find_one({"slug": "test-shop"})))
        self.assertTrue(anonymous.get("/api/store-availability/test-shop").json["available"])

    def test_verified_callback_restores_access(self):
        payment, data = self.payment()
        response = Mock()
        response.json.return_value = {"status": True, "data": data}
        with patch.object(billing.requests, "get", return_value=response):
            self.assertEqual(self.client.get("/subscription/callback?reference=" + payment["_id"]).status_code, 302)
        self.assertIn(b"Active", self.client.get("/subscription").data)
        # Checkout now reaches the existing cart validation instead of billing denial.
        self.assertEqual(self.client.post("/checkout", json={"cart": []}).status_code, 400)

    def test_subscription_payment_cannot_credit_wallet_or_public_order(self):
        from index import _verify_paystack
        self.assertFalse(_verify_paystack("SL-SUB-test")[0])
        result = self.client.get("/verify_transaction?reference=SL-SUB-test")
        self.assertTrue(result.location.endswith("/subscription"))
        self.assertEqual(fake_db.db.balances.count_documents({}), 0)

    def test_grace_boundary_and_store_access(self):
        now = datetime.utcnow()
        user = {"subscription_expires_at": now - timedelta(hours=23)}
        self.assertFalse(billing.is_active(user, now))
        self.assertTrue(billing.has_access(user, now))
        user["subscription_expires_at"] = now - timedelta(hours=24)
        self.assertFalse(billing.has_access(user, now))
        billing.users.update_one({"_id": self.uid}, {"$set": {"subscription_expires_at": now - timedelta(hours=1)}})
        self.assertTrue(billing.store_is_active({"owner_id": self.uid}))
        self.assertEqual(self.client.post("/checkout", json={"cart": []}).status_code, 400)
        self.assertIn(b"24-hour grace period", self.client.get("/subscription").data)

    def test_initial_grace_starts_once(self):
        billing.users.update_one({"_id": self.uid}, {"$unset": {"subscription_initial_due_at": ""}})
        first = billing.ensure_initial_due(billing.get_user(self.uid))
        second = billing.ensure_initial_due(billing.get_user(self.uid))
        self.assertEqual(first["subscription_initial_due_at"], second["subscription_initial_due_at"])
        self.assertTrue(billing.has_access(first))
        old = datetime.utcnow() - timedelta(hours=25)
        billing.users.update_one({"_id": self.uid}, {"$set": {"subscription_initial_due_at": old}})
        self.assertFalse(billing.has_access(billing.ensure_initial_due(billing.get_user(self.uid))))

    def test_announcement_once_per_login_session(self):
        with self.client.session_transaction() as sess:
            sess["subscription_announcement"] = True
        self.assertIn(b'id="subscription-announcement"', self.client.get("/subscription").data)
        self.assertNotIn(b'id="subscription-announcement"', self.client.get("/subscription").data)

    def test_admin_month_totals_filters_and_authorization(self):
        billing.users.update_one({"_id": self.uid}, {"$set": {"subscription_expires_at": datetime(2030, 1, 1)}})
        self.assertEqual(self.client.get("/admin/subscriptions").status_code, 403)
        admin_id = ObjectId()
        billing.users.insert_one({"_id": admin_id, "role": "admin"})
        with self.client.session_transaction() as sess:
            sess.update(user_id=str(admin_id), role="admin")
        for suffix, verified_at in [("a", datetime(2026, 9, 1)), ("b", datetime(2026, 9, 30)), ("c", datetime(2026, 10, 1))]:
            payment, _ = self.payment(suffix)
            billing.payments.update_one({"_id": payment["_id"]}, {"$set": {"status": "success", "verified_at": verified_at, "amount": 1500}})
        billing.users.insert_one({"role": "customer", "email": "unpaid@example.com"})
        billing.users.insert_one({"role": "customer", "email": "future@example.com", "created_at": datetime(2026, 10, 2)})
        result = self.client.get("/admin/subscriptions?month=2026-09")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'30.00', result.data)
        self.assertIn(b'across 2 agents', result.data)
        self.assertIn(b'all 2 successful payments', result.data)
        paid = self.client.get("/admin/subscriptions?month=2026-09&status=paid").data
        self.assertIn(b'agent@example.com', paid)
        self.assertNotIn(b'unpaid@example.com', paid)
        unpaid = self.client.get("/admin/subscriptions?month=2026-09&status=unpaid").data
        self.assertIn(b'unpaid@example.com', unpaid)
        self.assertNotIn(b'agent@example.com', unpaid)
        self.assertEqual(self.client.get("/admin/subscriptions?month=bad").status_code, 400)

    def test_receipt_month_does_not_change_on_retry(self):
        payment, data = self.payment()
        billing.apply_payment(payment, data)
        original = datetime(2026, 8, 31, 23, 59)
        billing.payments.update_one({"_id": payment["_id"]}, {"$set": {"verified_at": original}})
        billing.apply_payment(payment, data)
        self.assertEqual(billing.payments.find_one({"_id": payment["_id"]})["verified_at"], original)


if __name__ == "__main__":
    unittest.main()
