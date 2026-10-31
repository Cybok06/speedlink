"""Provider contract tests with in-memory MongoDB and mocked HTTP only."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import mongomock
import requests
from bson import ObjectId
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import emma_data


def load_module(name, filename, database):
    fake_db = types.ModuleType("db")
    fake_db.db = database
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"db": fake_db}):
        spec.loader.exec_module(module)
    return module


class EmmaDataTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test_emma
        # Avoid importing the production DB through the phone-number guard, too.
        guard = types.ModuleType("phone_number_guard")
        guard.evaluate_phone_order_eligibility = lambda phone: {"allowed": True}
        with patch.dict(sys.modules, {"phone_number_guard": guard}):
            self.checkout = load_module("emma_test_checkout", "checkout.py", self.db)
        self.admin = load_module("emma_test_admin", "admin_services.py", self.db)
        self.app = Flask(__name__, template_folder=str(ROOT / "templates"))
        self.app.secret_key = "test"
        self.app.config["TESTING"] = True
        self.app.register_blueprint(self.checkout.checkout_bp)
        self.app.register_blueprint(self.admin.admin_services_bp)
        self.app.add_url_rule('/brand/<filename>', endpoint='brand_images', view_func=lambda filename: '')
        self.app.jinja_loader = ChoiceLoader([DictLoader({'admin_sidebar.html': ''}), self.app.jinja_loader])
        self.client = self.app.test_client()
        self.service = {"_id": ObjectId(), "name": "MTN Normal", "type": "API", "provider": "skplug",
                        "fulfillment_provider": "emma_data", "offers": [{"value": "{'id': 1, 'volume': 5000}", "amount": 25}]}
        self.db.services.insert_one(self.service)
        self.key_patch = patch.dict(os.environ, {"EMMA_DATA_API_KEY": "test-key"})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def reply(self, status="COMPLETED", http_status=200):
        response = Mock(status_code=http_status)
        response.json.return_value = {"status": "success", "data": {
            "id": "EDK-test", "status": status, "transaction_code": "txn-test", "amount": 20}}
        return response

    def dispatch(self, service=None, item=None):
        return self.checkout._dispatch_service_order(
            order_id="SPD_123456", idx=1, item=item or {"value_obj": {"id": 1, "volume": 5000}},
            svc_doc=service if service is not None else self.service,
            svc_type=(service or self.service).get("type"), service_id_raw=str(self.service['_id']),
            phone="+233541234567", base_amount=25, resolved_network="mtn")

    def admin_session(self):
        with self.client.session_transaction() as session:
            session['role'] = 'admin'

    def test_networks_and_exact_purchase_contract(self):
        for name, network in [("MTN Normal", "YELLO"), ("MTN EXPRESS", "EXPRESS_YELLO"),
                              ("AT ishare", "at"), ("AT_Bigtime", "AT_PREMIUM"), ("TELECEL", "TELECEL")]:
            with self.subTest(name=name), patch.object(emma_data.requests, 'post', return_value=self.reply()) as post:
                result = self.dispatch({**self.service, "name": name})
                args, kwargs = post.call_args
                self.assertEqual(args[0], 'https://emmadataking.com/api/v1/purchase')
                self.assertEqual(kwargs['json'], {'network': network, 'capacity': '5',
                    'phoneNumber': '0541234567', 'ref': result['provider_request_order_id']})
                self.assertEqual(kwargs['headers']['X-API-Key'], 'test-key')
                self.assertFalse(kwargs['allow_redirects'])
                self.assertEqual(result['line_status'], 'delivered')
                self.assertEqual(result['provider_order_id'], 'EDK-test')

    def test_capacity_conversion_and_invalid_values(self):
        self.assertEqual(emma_data.capacity_in_gb({'volume': 500}, None), '0.5')
        self.assertEqual(emma_data.capacity_in_gb({}, '1.5GB'), '1.5')
        self.assertEqual(emma_data.capacity_in_gb({}, '500MB'), '0.5')
        for volume in ['NaN', 'Infinity', 0, -1, 'bad']:
            self.assertIsNone(emma_data.capacity_in_gb({'volume': volume}, None))
        with patch.object(emma_data.requests, 'post') as post:
            self.assertEqual(self.dispatch(item={'value': 'bad'})['api_status'], 'submit_failed')
            post.assert_not_called()

    def test_default_still_calls_campus_and_off_stays_manual(self):
        response = {'ok': True, 'response': {'id': 'campus-test'}}
        for selection in [None, 'default']:
            service = {**self.service, 'fulfillment_provider': selection}
            with patch.object(self.checkout, '_submit_external_data_order', return_value=response) as campus:
                result = self.dispatch(service)
                campus.assert_called_once()
                self.assertEqual(result['provider'], 'skplug')
        with patch.object(emma_data.requests, 'post') as post:
            result = self.dispatch({**self.service, 'type': 'OFF'})
            self.assertEqual(result['provider'], 'manual')
            post.assert_not_called()

    def test_cart_cannot_select_provider(self):
        service = {**self.service, 'fulfillment_provider': 'default'}
        with patch.object(self.checkout, '_submit_external_data_order', return_value={'ok': True, 'response': {}}) as campus:
            result = self.dispatch(service, {'provider': 'emma_data', 'fulfillment_provider': 'emma_data', 'value': '5GB'})
            self.assertEqual(result['provider'], 'skplug')
            campus.assert_called_once()

    def test_statuses_and_no_fallback_or_retry(self):
        for status, expected in [('PENDING', 'processing'), ('FAILED', 'failed'), ('COMPLETED', 'delivered')]:
            with patch.object(emma_data.requests, 'post', return_value=self.reply(status)):
                self.assertEqual(self.dispatch()['line_status'], expected)
        with patch.object(emma_data.requests, 'post', side_effect=requests.Timeout) as post, \
             patch.object(self.checkout, '_submit_external_data_order') as campus:
            result = self.dispatch()
            self.assertEqual(result['api_status'], 'submission_unknown')
            self.assertTrue(result['provider_follow_up_required'])
            post.assert_called_once()
            campus.assert_not_called()
        for code in [401, 429, 500]:
            response = Mock(status_code=code)
            response.json.return_value = {'status': 'error', 'message': 'Rejected'}
            with patch.object(emma_data.requests, 'post', return_value=response) as post:
                result = self.dispatch()
                self.assertFalse(result['api_response']['ok'])
                self.assertEqual(result['api_response']['http_status'], code)
                post.assert_called_once()
        response = self.reply()
        response.json.side_effect = ValueError('invalid JSON')
        with patch.object(emma_data.requests, 'post', return_value=response):
            self.assertEqual(self.dispatch()['api_status'], 'submission_unknown')

    def test_missing_key_prevents_submission(self):
        with patch.dict(os.environ, {'EMMA_DATA_API_KEY': ''}), patch.object(emma_data.requests, 'post') as post:
            self.assertEqual(self.dispatch()['api_status'], 'submit_failed')
            post.assert_not_called()

    def test_switch_authorization_validation_and_persistence(self):
        url = f"/admin/services/{self.service['_id']}/provider"
        self.assertEqual(self.client.post(url, json={'provider': 'emma_data'}).status_code, 401)
        self.admin_session()
        for provider in ['default', 'emma_data', 'default']:
            response = self.client.post(url, json={'provider': provider})
            self.assertEqual(response.status_code, 200)
            saved = self.db.services.find_one({'_id': self.service['_id']})
            self.assertEqual(saved['fulfillment_provider'], provider)
            self.assertEqual(saved['provider'], 'skplug')
        self.assertEqual(self.client.post(url, json={'provider': 'other'}).status_code, 400)
        self.assertEqual(self.client.post('/admin/services/bad/provider', json={'provider': 'default'}).status_code, 400)
        self.assertEqual(self.client.post(f'/admin/services/{ObjectId()}/provider', json={'provider':'default'}).status_code, 404)
        self.db.services.update_one({'_id': self.service['_id']}, {'$set': {'name': 'AFA Talktime'}})
        self.assertEqual(self.client.post(url, json={'provider': 'emma_data'}).status_code, 400)

    def test_service_page_shows_switch_for_supported_services_only(self):
        self.db.services.insert_one({'name': 'AFA Talktime', 'offers': []})
        self.admin_session()
        response = self.client.get('/admin/services')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('onchange="switchProvider('), 1)
        self.assertIn('>Emma Data</option>', response.text)

    def test_wallet_checkout_saves_delivery_and_charges_once(self):
        uid = ObjectId()
        self.db.balances.insert_one({'user_id': uid, 'amount': 100})
        with self.client.session_transaction() as session:
            session['user_id'] = str(uid)
        cart = [{'serviceId': str(self.service['_id']), 'phone': '0541234567', 'amount': 25,
                 'value_obj': {'id': 1, 'volume': 5000}, 'value': '5GB'}]
        with patch.object(emma_data.requests, 'post', return_value=self.reply()) as post:
            response = self.client.post('/checkout', json={'cart': cart})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['status'], 'delivered')
        self.assertEqual(self.db.orders.find_one()['items'][0]['provider'], 'emma_data')
        self.assertEqual(self.db.balances.find_one()['amount'], 75)
        self.assertEqual(self.db.transactions.find_one()['meta']['api_delivered_amount'], 25)
        post.assert_called_once()

    def test_mixed_order_status(self):
        self.assertEqual(emma_data.initial_order_status([{'line_status':'delivered'}, {'line_status':'skipped_duplicate_in_cart'}]), 'delivered')
        self.assertEqual(emma_data.initial_order_status([{'line_status':'delivered'}, {'line_status':'processing'}]), 'pending')
        self.assertEqual(emma_data.initial_order_status([]), 'pending')

    def test_public_checkout_uses_saved_provider_and_delivery(self):
        fake_paystack = types.ModuleType('paystack_config')
        fake_paystack.get_paystack_keys = lambda: {}
        with patch.dict(sys.modules, {'checkout': self.checkout, 'paystack_config': fake_paystack}):
            public = load_module('emma_test_public', 'index.py', self.db)
        cart = [{'serviceId': str(self.service['_id']), 'phone':'0541234567',
                 'value_obj': {'id':1, 'volume':5000}, 'value':'5GB', 'amount':25}]
        with patch.object(emma_data.requests, 'post', return_value=self.reply()) as post:
            result = public._create_public_order_from_verified_payment('paid-test', {}, cart, 25)
        self.assertEqual(result['order_doc']['status'], 'delivered')
        self.assertEqual(result['order_doc']['items'][0]['provider'], 'emma_data')
        post.assert_called_once()

    def test_store_checkout_includes_saved_provider_in_service_projection(self):
        from mongomock.gridfs import enable_gridfs_integration
        enable_gridfs_integration()
        fake_paystack = types.ModuleType('paystack_config')
        fake_paystack.get_paystack_keys = lambda: {}
        fake_paystack.is_public_key = lambda key: True
        fake_paystack.is_secret_key = lambda key: True
        fake_subscriptions = types.ModuleType('subscriptions')
        fake_subscriptions.store_is_active = lambda store: True
        with patch.dict(sys.modules, {'checkout':self.checkout, 'paystack_config':fake_paystack}):
            store = load_module('emma_test_store', 'routes/store_page.py', self.db)
        self.app.register_blueprint(store.stores_bp)
        self.db.stores.insert_one({'slug':'test-shop', 'owner_id':ObjectId()})
        self.db.services.update_one({'_id':self.service['_id']}, {'$set':{'store_offers':self.service['offers']}})
        cart = [{'serviceId':str(self.service['_id']), 'phone':'0541234567',
                 'value_obj':{'id':1, 'volume':5000}, 'value':'5GB', 'amount':25}]
        with patch.dict(sys.modules, {'subscriptions':fake_subscriptions}), \
             patch.object(store, '_store_host_redirect', return_value=None), \
             patch.object(store, '_verify_paystack', return_value=(True, {'amount':2500,'currency':'GHS'}, '')), \
             patch.object(emma_data.requests, 'post', return_value=self.reply()) as post:
            response = self.client.post('/store-checkout/test-shop', json={'cart':cart,'paystack':{'reference':'paid-store-test'}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json['items'][0]['provider'], 'emma_data')
        self.assertEqual(response.json['status'], 'delivered')
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')
        post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
