"""Status sync tests: in-memory MongoDB, mocked HTTP and no live scheduler."""
import importlib.util
import os
from datetime import datetime, timedelta
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import mongomock
import requests


class OrderStatusTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test_order_status
        fake_db = types.ModuleType('db')
        fake_db.db = self.db
        self.env = patch.dict(os.environ, {'EMMA_DATA_API_KEY':'test-emma-key', 'EMMA_DATA_TIMEOUT':'45'})
        self.env.start()
        self.addCleanup(self.env.stop)
        path = Path(__file__).resolve().parents[1] / 'order_status.py'
        spec = importlib.util.spec_from_file_location('test_status_module', path)
        self.module = importlib.util.module_from_spec(spec)
        self.scheduler = Mock()
        with patch.dict(sys.modules, {'db':fake_db}), \
             patch('apscheduler.schedulers.background.BackgroundScheduler', return_value=self.scheduler):
            spec.loader.exec_module(self.module)
        self.module.PROVIDER_API_KEY = ''

    def item(self, **overrides):
        return {'provider':'emma_data', 'provider_request_order_id':'SL-original-ref',
                'provider_order_id':'EDK-original', 'provider_reference':'EDK-original',
                'phone':'0541234567', 'line_status':'processing', 'api_status':'submitted',
                'api_response':{'request':{'ref':'SL-original-ref'}}, **overrides}

    def insert(self, items=None, status='pending'):
        return self.db.orders.insert_one({'status':status, 'items':items or [self.item()],
                                          'updated_at':datetime.utcnow() - timedelta(minutes=5)}).inserted_id

    def response(self, status='COMPLETED', code=200, **data):
        response = Mock(status_code=code)
        response.json.return_value = {'status':'success', 'data':{
            'id':'EDK-8F3A2B1C', 'reference':'SL-original-ref', 'status':status,
            'transaction_code':'txn-test', 'amount':20, 'network':'YELLO',
            'delivered_at':'January 28, 2026 10:01 AM', **data}}
        return response

    def test_sync_uses_original_ref_and_updates_database_without_campus_key(self):
        oid = self.insert()
        with patch.object(self.module.requests, 'get', return_value=self.response()) as get:
            summary = self.module._run_order_status_sync()
        self.assertEqual(summary['updated_orders'], 1)
        args, kwargs = get.call_args
        self.assertEqual(args[0], 'https://emmadataking.com/api/v1/order-status/SL-original-ref')
        self.assertEqual(kwargs['headers']['X-API-Key'], 'test-emma-key')
        self.assertNotIn('Authorization', kwargs['headers'])
        self.assertFalse(kwargs['allow_redirects'])
        order = self.db.orders.find_one({'_id':oid})
        self.assertEqual(order['status'], 'delivered')
        line = order['items'][0]
        self.assertEqual(line['line_status'], 'delivered')
        self.assertEqual(line['provider_order_id'], 'EDK-8F3A2B1C')
        self.assertEqual(line['provider_request_order_id'], 'SL-original-ref')
        self.assertEqual(line['api_response']['request']['ref'], 'SL-original-ref')
        self.assertEqual(line['provider_amount'], 20)
        self.assertFalse(line['provider_follow_up_required'])
        with patch.object(self.module.requests, 'get') as get:
            self.module._run_order_status_sync()
            get.assert_not_called()

    def test_pending_processing_and_failed_mapping(self):
        for status, expected in [('PENDING','pending'), ('PROCESSING','processing'), ('FAILED','failed')]:
            with self.subTest(status=status):
                self.db.orders.delete_many({})
                oid = self.insert()
                with patch.object(self.module.requests, 'get', return_value=self.response(status)):
                    self.module._run_order_status_sync()
                order = self.db.orders.find_one({'_id':oid})
                self.assertEqual(order['status'], expected)
                self.assertEqual(order['items'][0]['line_status'], expected)

    def test_unconfirmed_purchase_without_edk_id_is_recovered(self):
        oid = self.insert([self.item(provider_order_id=None, provider_reference=None,
                                     line_status='pending', api_status='submission_unknown')])
        with patch.object(self.module.requests, 'get', return_value=self.response()):
            self.module._run_order_status_sync()
        self.assertEqual(self.db.orders.find_one({'_id':oid})['status'], 'delivered')

    def test_bad_responses_preserve_status(self):
        invalid_json = self.response()
        invalid_json.json.side_effect = ValueError('bad json')
        responses = [self.response(code=401), self.response(code=500), self.response(reference='wrong'),
                     self.response(status='UNKNOWN'), self.response(status=''), invalid_json]
        for response in responses:
            with self.subTest(response=response):
                self.db.orders.delete_many({})
                oid = self.insert()
                with patch.object(self.module.requests, 'get', return_value=response):
                    self.module._run_order_status_sync()
                line = self.db.orders.find_one({'_id':oid})['items'][0]
                self.assertEqual(line['line_status'], 'processing')
                self.assertEqual(line['api_status'], 'submitted')
                self.assertIn('provider_status_checked_at', line)
        self.db.orders.delete_many({})
        oid = self.insert()
        with patch.object(self.module.requests, 'get', side_effect=requests.Timeout):
            self.module._run_order_status_sync()
        self.assertEqual(self.db.orders.find_one({'_id':oid})['items'][0]['line_status'], 'processing')

    def test_stale_missing_refs_and_terminal_lines_are_skipped(self):
        self.insert([self.item(line_status='delivered')], status='delivered')
        self.insert([self.item(line_status='failed')], status='failed')
        self.insert([self.item(provider_request_order_id=None)])
        self.insert([self.item(provider_status_checked_at=datetime.utcnow())])
        with patch.object(self.module.requests, 'get') as get:
            self.module._run_order_status_sync()
            get.assert_not_called()

    def test_mixed_provider_order_and_skipped_lines(self):
        oid = self.insert([self.item(), {'provider':'skplug', 'line_status':'delivered'},
                           {'line_status':'skipped_duplicate_in_cart'}])
        with patch.object(self.module.requests, 'get', return_value=self.response()) as get:
            self.module._run_order_status_sync()
            get.assert_called_once()
        self.assertEqual(self.db.orders.find_one({'_id':oid})['status'], 'delivered')

    def test_campus_status_contract_still_works(self):
        self.module.PROVIDER_API_KEY = 'campus-test-key'
        oid = self.insert([{'provider':'skplug', 'provider_order_id':'campus-id', 'line_status':'processing'}])
        response = Mock(status_code=200, text='json')
        response.json.return_value = {'success':True, 'status':'completed', 'order_id':'campus-id'}
        with patch.object(self.module.requests, 'get', return_value=response) as get:
            self.module._run_order_status_sync()
        self.assertIn('/api/external/orders/campus-id', get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer campus-test-key')
        self.assertEqual(self.db.orders.find_one({'_id':oid})['status'], 'delivered')

    def test_rate_limit_stops_remaining_emma_checks(self):
        self.insert([self.item(), self.item(provider_request_order_id='another-ref')])
        with patch.object(self.module.requests, 'get', return_value=self.response(code=429)) as get:
            self.module._run_order_status_sync()
            get.assert_called_once()

    def test_three_minute_scheduler_and_no_missing_key_calls(self):
        self.assertEqual(self.scheduler.add_job.call_args.kwargs['minutes'], self.module.SYNC_INTERVAL_MINUTES)
        self.assertEqual(self.module.SYNC_INTERVAL_MINUTES, 3)
        self.insert()
        with patch.dict(os.environ, {'EMMA_DATA_API_KEY':''}), patch.object(self.module.requests, 'get') as get:
            self.module._run_order_status_sync()
            get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
