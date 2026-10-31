"""Authentication contract tests without accessing the production database."""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import mongomock
from flask import Flask
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ISTAuthTests(unittest.TestCase):
    def setUp(self):
        mock_db = types.ModuleType('istdb')
        mock_db.client = mongomock.MongoClient()
        mock_db.db = mock_db.client['kingollies']
        source = Path(__file__).resolve().parents[1] / 'ist_backend' / 'auth.py'
        spec = importlib.util.spec_from_file_location('ist_auth_test_module', source)
        self.auth = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'istdb': mock_db}):
            spec.loader.exec_module(self.auth)
        app = Flask(__name__)
        app.register_blueprint(self.auth.ist_bp)
        self.client = app.test_client()
        self.db = mock_db.db

    def login(self, **changes):
        return self.client.post('/api/ist/auth/login', json={
            'username': 'admin', 'password': '1234', **changes,
        }, headers={'Origin': 'https://ist-record-keeping.onrender.com'})

    def test_login_me_logout_and_password_hash(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'],
                         'https://ist-record-keeping.onrender.com')
        token = response.json['token']
        headers = {'Authorization': f'Bearer {token}'}
        self.assertEqual(self.client.get('/api/ist/auth/me', headers=headers).status_code, 200)
        self.assertNotEqual(self.db.users.find_one()['password_hash'], '1234')
        self.assertNotEqual(self.db.auth_sessions.find_one()['_id'], token)
        self.client.post('/api/ist/auth/logout', headers=headers)
        self.assertEqual(self.client.get('/api/ist/auth/me', headers=headers).status_code, 401)

    def test_invalid_credentials_and_input(self):
        self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.login(username='other').status_code, 401)
        self.assertEqual(self.login(username=['admin']).status_code, 400)
        self.assertEqual(self.login(remember='yes').status_code, 400)
        self.assertEqual(self.client.get('/api/ist/auth/me').status_code, 401)

    def test_expiration_and_remember_me(self):
        response = self.login(remember=True)
        expiry = datetime.fromisoformat(response.json['expires_at'])
        self.assertGreater(expiry, datetime.now(timezone.utc) + timedelta(hours=7))
        self.db.auth_sessions.update_one({}, {'$set': {
            'expires_at': datetime.now(timezone.utc) - timedelta(seconds=1),
        }})
        headers = {'Authorization': 'Bearer ' + response.json['token']}
        self.assertEqual(self.client.get('/api/ist/auth/me', headers=headers).status_code, 401)

    def test_preflight_and_disallowed_origin(self):
        response = self.client.options('/api/ist/auth/login', headers={
            'Origin': 'https://ist-record-keeping.onrender.com',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'Content-Type,Authorization',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('Authorization', response.headers['Access-Control-Allow-Headers'])
        response = self.client.get('/api/ist/auth/me', headers={'Origin': 'https://example.com'})
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_rate_limit_and_existing_password_preserved(self):
        self.login()
        stored = self.db.users.find_one()['password_hash']
        for _ in range(9):
            self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.login().status_code, 429)
        self.assertEqual(self.db.users.find_one()['password_hash'], stored)

    def management_headers(self):
        return {'Authorization': 'Bearer ' + self.login().json['token']}

    def create_account(self, headers, **changes):
        return self.client.post('/api/ist/admins', headers=headers, json={
            'username': 'ama', 'password': 'secret123', 'name': 'Ama Boateng',
            'email': 'ama@example.com', 'phone': '+233240000000', 'role': 'Admin', **changes,
        })

    def test_create_account_login_and_list_without_secrets(self):
        headers = self.management_headers()
        created = self.create_account(headers)
        self.assertEqual(created.status_code, 201)
        self.assertNotIn('password_hash', created.json['admin'])
        self.assertEqual(self.login(username='AMA', password='secret123').status_code, 200)
        listed = self.client.get('/api/ist/admins', headers=headers)
        self.assertEqual(len(listed.json['admins']), 2)
        self.assertNotIn('password_hash', str(listed.json))
        self.assertEqual(self.create_account(headers, username='AMA').status_code, 409)
        self.assertEqual(self.create_account(headers, username='other', password='x').status_code, 400)
        self.assertEqual(self.client.post('/api/ist/admins', json={}).status_code, 401)

    def test_deactivate_reactivate_and_reset_password(self):
        headers = self.management_headers()
        self.create_account(headers)
        token = self.login(username='ama', password='secret123').json['token']
        own_headers = {'Authorization': 'Bearer ' + token}
        response = self.client.patch('/api/ist/admins/ama', headers=headers, json={'status': 'Inactive'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.login(username='ama', password='secret123').status_code, 401)
        self.assertEqual(self.client.get('/api/ist/auth/me', headers=own_headers).status_code, 401)
        self.client.patch('/api/ist/admins/ama', headers=headers, json={'status': 'Active'})
        self.assertEqual(self.login(username='ama', password='secret123').status_code, 200)
        response = self.client.patch('/api/ist/admins/ama', headers=headers, json={
            'name': 'Ama B', 'email': 'ama@example.com', 'phone': '12345',
            'role': 'Admin', 'status': 'Active', 'password': 'new-password',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.login(username='ama', password='secret123').status_code, 401)
        self.assertEqual(self.login(username='ama', password='new-password').status_code, 200)

    def test_management_permissions_and_self_protection(self):
        headers = self.management_headers()
        self.assertEqual(self.client.patch('/api/ist/admins/admin', headers=headers,
                                          json={'status': 'Inactive'}).status_code, 400)
        self.create_account(headers, role='Registrar')
        token = self.login(username='ama', password='secret123').json['token']
        limited = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.get('/api/ist/admins', headers=limited).status_code, 403)
        self.assertEqual(self.create_account(limited, username='other').status_code, 403)
        self.db.users.update_one({'_id': 'ama'}, {'$set': {'role': 'Admin'}})
        self.assertEqual(self.create_account(limited, username='other', role='Super Admin').status_code, 403)
        self.assertEqual(self.client.patch('/api/ist/admins/admin', headers=limited,
                                          json={'status': 'Inactive'}).status_code, 403)

    def test_settings_persist_validate_and_detect_conflicts(self):
        headers = self.management_headers()
        self.assertEqual(self.client.get('/api/ist/settings').status_code, 401)
        settings = self.client.get('/api/ist/settings', headers=headers).json
        self.assertEqual(settings['config']['programmes'], [])
        self.assertEqual(settings['config']['souvenirItems'], [])
        self.assertEqual(settings['defaults']['programmes'], [])
        self.assertEqual(settings['defaults']['souvenirItems'], [])
        config = settings['config']
        config['programmes'].append({'id': 'new', 'name': 'New Programme', 'type': 'Diploma', 'fee': 100})
        config['souvenirItems'].append({'id': 'bag', 'label': 'Bag', 'qty': '1 pc', 'icon': 'gift'})
        config['academicYear'] = '2026/2027'
        payload = {'config': config, 'version': settings['version']}
        response = self.client.patch('/api/ist/settings', headers=headers, json=payload)
        self.assertEqual(response.status_code, 200)
        loaded = self.client.get('/api/ist/settings', headers=headers).json
        self.assertEqual(loaded['config'], config)
        self.assertEqual(self.client.patch('/api/ist/settings', headers=headers, json=payload).status_code, 409)
        payload['version'] = loaded['version']
        config['programmes'].append(dict(config['programmes'][0]))
        self.assertEqual(self.client.patch('/api/ist/settings', headers=headers, json=payload).status_code, 400)
        self.assertEqual(self.client.get('/api/ist/settings', headers=headers).json['config'], loaded['config'])

    def test_old_sample_cleanup_preserves_real_and_edited_entries(self):
        headers = self.management_headers()
        config = self.client.get('/api/ist/settings', headers=headers).json['config']
        sample = {'id': 'bsc-it', 'name': 'BSc. Information Technology', 'type': 'BSc', 'fee': 2800}
        edited = {'id': 'hnd-it', 'name': 'HND Information Technology', 'type': 'HND', 'fee': 3000}
        custom = {'id': 'custom', 'name': 'Admin Programme', 'type': 'Diploma', 'fee': 150}
        config['programmes'] = [sample, edited, custom]
        config['souvenirItems'] = [
            {'id': 'books', 'label': 'Exercise Books', 'qty': '2 pcs', 'icon': 'book'},
            {'id': 'custom-bag', 'label': 'Student Bag', 'qty': '1 pc', 'icon': 'gift'},
        ]
        self.db.settings.replace_one({'_id': 'config'}, {'_id': 'config', 'config': config, 'version': 5})
        loaded = self.client.get('/api/ist/settings', headers=headers).json
        self.assertEqual(loaded['config']['programmes'], [edited, custom])
        self.assertEqual(len(loaded['config']['souvenirItems']), 1)
        self.assertEqual(loaded['version'], 6)
        self.assertEqual(self.client.get('/api/ist/settings', headers=headers).json, loaded)

    def test_security_policy_and_password_change(self):
        headers = self.management_headers()
        settings = self.client.get('/api/ist/settings', headers=headers).json
        settings['config'].update(sessionMinutes=30, passwordMinLength=8)
        response = self.client.patch('/api/ist/settings', headers=headers, json={
            'config': settings['config'], 'version': settings['version'],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.create_account(headers, password='1234').status_code, 400)
        login = self.login(remember=True)
        expiry = datetime.fromisoformat(login.json['expires_at'])
        self.assertLess(expiry, datetime.now(timezone.utc) + timedelta(minutes=31))
        self.assertEqual(self.client.post('/api/ist/auth/password', headers=headers, json={
            'currentPassword': 'wrong', 'newPassword': 'new-password',
        }).status_code, 400)
        self.assertEqual(self.client.post('/api/ist/auth/password', headers=headers, json={
            'currentPassword': '1234', 'newPassword': 'new-password',
        }).status_code, 200)
        self.assertEqual(self.login(password='1234').status_code, 401)
        self.assertEqual(self.login(password='new-password').status_code, 200)
        other = {'Authorization': 'Bearer ' + login.json['token']}
        self.assertEqual(self.client.get('/api/ist/auth/me', headers=other).status_code, 401)
        self.assertEqual(self.client.get('/api/ist/auth/me', headers=headers).status_code, 200)

    def test_read_only_settings_role(self):
        headers = self.management_headers()
        self.create_account(headers, role='Registrar')
        token = self.login(username='ama', password='secret123').json['token']
        limited = {'Authorization': 'Bearer ' + token}
        settings = self.client.get('/api/ist/settings', headers=limited)
        self.assertEqual(settings.status_code, 200)
        self.assertFalse(settings.json['canEdit'])
        self.assertEqual(self.client.patch('/api/ist/settings', headers=limited, json={
            'config': settings.json['config'], 'version': settings.json['version'],
        }).status_code, 403)

    def student_setup(self):
        headers = self.management_headers()
        settings = self.client.get('/api/ist/settings', headers=headers).json
        settings['config']['programmes'] = [{'id': 'prog', 'name': 'Programme', 'type': 'BSc', 'fee': 100}]
        settings['config']['souvenirItems'] = [{'id': 'bag', 'label': 'Bag', 'qty': '1 pc', 'icon': 'gift'}]
        self.client.patch('/api/ist/settings', headers=headers, json={
            'config': settings['config'], 'version': settings['version'],
        })
        return headers, {
            'requestId': 'registration-request-0001', 'indexNumber': 'IST/2026/001', 'firstName': 'Ama', 'lastName': 'Boateng',
            'phone': '0240000000', 'gender': 'Female', 'program': 'Programme', 'level': '100',
            'academicYear': settings['config']['academicYear'], 'amountPaid': 50,
            'paymentMethod': 'Cash', 'consentChecked': True, 'souvenirs': {'bag': True},
        }

    def test_student_registration_list_profile_and_retry(self):
        headers, payload = self.student_setup()
        self.assertEqual(self.client.get('/api/ist/students', headers=headers).json['students'], [])
        created = self.client.post('/api/ist/students', headers=headers, json=payload)
        self.assertEqual(created.status_code, 201)
        student = created.json['student']
        self.assertEqual(student['indexNumber'], payload['indexNumber'])
        self.assertEqual(self.db.settings.find_one({'_id': 'config'})['config']['nextSequence'], 1)
        self.assertEqual(student['totalFees'], 100)
        self.assertEqual(student['paymentStatus'], 'Partial')
        self.assertEqual(student['souvenirStatus'], 'Received')
        self.assertEqual(student['registeredBy'], 'Administrator')
        retry = self.client.post('/api/ist/students', headers=headers, json=payload)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json['student']['indexNumber'], student['indexNumber'])
        self.assertEqual(self.db.students.count_documents({}), 1)
        payload['requestId'] = 'registration-request-0002'
        self.assertEqual(self.client.post('/api/ist/students', headers=headers, json=payload).status_code, 409)
        payload['indexNumber'] = 'IST/2026/002'
        second = self.client.post('/api/ist/students', headers=headers, json=payload)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(second.json['student']['indexNumber'], student['indexNumber'])
        self.assertEqual(len(self.client.get('/api/ist/students', headers=headers).json['students']), 2)
        profile = self.client.get('/api/ist/students/' + student['id'], headers=headers)
        self.assertEqual(profile.json['student'], student)

    def test_student_validation_and_access(self):
        headers, payload = self.student_setup()
        self.assertEqual(self.client.post('/api/ist/students', json=payload).status_code, 401)
        for changes in [{'indexNumber': ''}, {'indexNumber': '   '}, {'indexNumber': 'A' * 101}, {'indexNumber': 'A B'}, {'program': 'Unknown'}, {'level': '500'}, {'amountPaid': -1},
                        {'amountPaid': 101}, {'consentChecked': False}, {'souvenirs': {'fake': True}},
                        {'academicYear': '1999/2000'}, {'email': 'invalid'}]:
            response = self.client.post('/api/ist/students', headers=headers, json={**payload, **changes})
            self.assertEqual(response.status_code, 400, changes)
        self.assertEqual(self.db.students.count_documents({}), 0)
        self.create_account(headers, role='Records Officer')
        token = self.login(username='ama', password='secret123').json['token']
        limited = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.get('/api/ist/students', headers=limited).status_code, 200)
        self.assertEqual(self.client.post('/api/ist/students', headers=limited, json=payload).status_code, 403)
        self.assertEqual(self.client.get('/api/ist/students/missing', headers=headers).status_code, 404)

    def test_navbar_search_and_notification_read_state(self):
        headers, registration = self.student_setup()
        student = self.client.post('/api/ist/students', headers=headers, json=registration).json['student']
        for path in ('/search?q=test', '/notifications'):
            self.assertEqual(self.client.get('/api/ist' + path).status_code, 401)
        response = self.client.get('/api/ist/search', query_string={'q': student['indexNumber'].lower()}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['students'][0]['id'], student['id'])
        self.assertEqual(response.json['payments'][0]['studentId'], student['id'])
        reference = response.json['payments'][0]['reference']
        self.assertEqual(len(self.client.get('/api/ist/search', query_string={'q': reference}, headers=headers).json['payments']), 1)
        self.assertEqual(self.client.get('/api/ist/search?q=unmatched-value', headers=headers).json,
                         {'students': [], 'payments': []})
        self.assertEqual(self.client.get('/api/ist/search', query_string={'q': 'x' * 151}, headers=headers).status_code, 400)
        notices = self.client.get('/api/ist/notifications', headers=headers).json
        self.assertEqual(notices['unreadCount'], 2)
        ids = [item['id'] for item in notices['notifications']]
        self.assertEqual(self.client.post('/api/ist/notifications', headers=headers, json={'ids': ids}).status_code, 200)
        self.assertEqual(self.client.get('/api/ist/notifications', headers=headers).json['unreadCount'], 0)
        self.assertEqual(self.client.post('/api/ist/notifications', headers=headers, json={'ids': 'bad'}).status_code, 400)
        self.create_account(headers, role='Records Officer')
        token = self.login(username='ama', password='secret123').json['token']
        self.assertEqual(self.client.get('/api/ist/notifications', headers={'Authorization': 'Bearer ' + token}).json['unreadCount'], 2)

    def test_payment_ledger_initial_payment_retries_and_balances(self):
        headers, registration = self.student_setup()
        student = self.client.post('/api/ist/students', headers=headers, json=registration).json['student']
        rows = self.client.get('/api/ist/payments', headers=headers).json['payments']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['amount'], 50)
        payload = {'studentId': student['id'], 'requestId': 'payment-request-0001', 'amount': 25,
                   'method': 'Cash', 'reference': 'receipt-1', 'date': '2026-01-01', 'notes': 'Top up'}
        response = self.client.post('/api/ist/payments', headers=headers, json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.post('/api/ist/payments', headers=headers, json=payload).status_code, 200)
        balance = self.client.get('/api/ist/students/' + student['id'], headers=headers).json['student']
        self.assertEqual(balance['amountPaid'], 75)
        self.assertEqual(balance['paymentStatus'], 'Partial')
        self.assertEqual(self.client.post('/api/ist/payments', headers=headers, json={
            **payload, 'requestId': 'payment-request-0002',
        }).status_code, 409)
        self.assertEqual(self.client.post('/api/ist/payments', headers=headers, json={
            **payload, 'requestId': 'payment-request-0002', 'reference': 'receipt-2', 'amount': 26,
        }).status_code, 400)
        self.assertEqual(self.client.post('/api/ist/payments', headers=headers, json={
            **payload, 'requestId': 'payment-request-0002', 'reference': 'receipt-2',
        }).status_code, 201)
        balance = self.client.get('/api/ist/students/' + student['id'], headers=headers).json['student']
        self.assertEqual(balance['amountPaid'], 100)
        self.assertEqual(balance['paymentStatus'], 'Paid')
        rows = self.client.get('/api/ist/payments', headers=headers).json['payments']
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row['amount'] for row in rows), 100)

    def test_souvenir_issuance_is_repeat_safe_and_updates_student(self):
        headers, registration = self.student_setup()
        registration['souvenirs'] = {'bag': False}
        student = self.client.post('/api/ist/students', headers=headers, json=registration).json['student']
        rows = self.client.get('/api/ist/souvenirs', headers=headers).json['souvenirs']
        self.assertEqual(rows[0]['status'], 'Not Issued')
        payload = {'studentId': student['id'], 'itemId': 'bag'}
        response = self.client.post('/api/ist/souvenirs/issue', headers=headers, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post('/api/ist/souvenirs/issue', headers=headers, json=payload).status_code, 200)
        rows = self.client.get('/api/ist/souvenirs', headers=headers).json['souvenirs']
        self.assertEqual(rows[0]['status'], 'Received')
        self.assertEqual(rows[0]['issuedBy'], 'Administrator')
        self.assertIsNotNone(rows[0]['issuedDate'])
        profile = self.client.get('/api/ist/students/' + student['id'], headers=headers).json['student']
        self.assertEqual(profile['souvenirStatus'], 'Received')
        self.assertEqual(len(profile['souvenirItems']), 1)
        self.assertEqual(self.client.post('/api/ist/souvenirs/issue', headers=headers,
                                        json={**payload, 'itemId': 'fake'}).status_code, 404)

    def test_tracking_permissions_validation_and_concurrent_payments(self):
        from concurrent.futures import ThreadPoolExecutor
        headers, registration = self.student_setup()
        self.client.post('/api/ist/students', headers=headers, json=registration)
        payload = {'studentId': registration['requestId'], 'requestId': 'payment-request-0001',
                   'amount': 30, 'method': 'Cash', 'date': '2026-01-01'}
        self.assertEqual(self.client.get('/api/ist/payments').status_code, 401)
        self.assertEqual(self.client.post('/api/ist/souvenirs/issue', json={}).status_code, 401)
        for changes in [{'amount': -1}, {'amount': 0}, {'amount': 0.001},
                        {'method': 'Fake'}, {'method': 'Mobile Money'}, {'date': 'invalid'}]:
            self.assertEqual(self.client.post('/api/ist/payments', headers=headers,
                                             json={**payload, **changes}).status_code, 400)
        def record(number):
            with self.client.application.test_client() as client:
                return client.post('/api/ist/payments', headers=headers, json={
                    **payload, 'requestId': f'payment-request-000{number}',
                }).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(record, [1, 2]))
        self.assertEqual(sorted(statuses), [201, 400])
        self.assertEqual(self.db.students.find_one()['amountPaid'], 80)
        self.create_account(headers, role='Records Officer')
        limited = {'Authorization': 'Bearer ' + self.login(username='ama', password='secret123').json['token']}
        self.assertEqual(self.client.post('/api/ist/payments', headers=limited, json=payload).status_code, 403)
        self.db.users.update_one({'_id': 'ama'}, {'$set': {'role': 'Finance Officer'}})
        self.assertEqual(self.client.post('/api/ist/souvenirs/issue', headers=limited,
                                         json={'studentId': registration['requestId'], 'itemId': 'bag'}).status_code, 403)

    def test_accountability_totals_year_filters_and_empty_state(self):
        headers, registration = self.student_setup()
        empty = self.client.get('/api/ist/accountability', headers=headers).json
        self.assertEqual(empty['totalFees'], 0)
        self.assertEqual(empty['collectionRate'], 0)
        self.assertEqual(empty['students'], [])
        self.client.post('/api/ist/students', headers=headers, json=registration)
        self.client.post('/api/ist/payments', headers=headers, json={
            'studentId': registration['requestId'], 'requestId': 'payment-request-0001',
            'amount': 25, 'method': 'Cash', 'date': '2026-01-01',
        })
        summary = self.client.get('/api/ist/accountability', headers=headers).json
        self.assertEqual(summary['totalFees'], 100)
        self.assertEqual(summary['totalCollected'], 75)
        self.assertEqual(summary['totalOutstanding'], 25)
        self.assertEqual(summary['collectionRate'], 75)
        self.assertEqual(summary['programSummary'][0]['partial'], 1)
        self.assertEqual(summary['souvenirReceived'], 1)
        selected = self.client.get('/api/ist/accountability?year=1999/2000', headers=headers).json
        self.assertEqual(selected['totalFees'], 0)
        self.assertEqual(selected['programSummary'], [])
        self.assertEqual(self.client.get('/api/ist/accountability').status_code, 401)

    def test_audit_history_permissions_no_secrets_and_retries(self):
        headers, registration = self.student_setup()
        self.create_account(headers)
        student = self.client.post('/api/ist/students', headers=headers, json=registration).json['student']
        payload = {'studentId': student['id'], 'requestId': 'payment-request-0001',
                   'amount': 10, 'method': 'Cash', 'date': '2026-01-01'}
        self.client.post('/api/ist/payments', headers=headers, json=payload)
        self.client.post('/api/ist/payments', headers=headers, json=payload)
        self.login(password='wrong')
        response = self.client.get('/api/ist/audit-logs', headers=headers)
        self.assertEqual(response.status_code, 200)
        actions = [event['action'] for event in response.json['logs']]
        for action in ['Login Successful', 'Login Failed', 'Settings Updated', 'Administrator Created',
                       'Student Registered', 'Payment Recorded', 'Souvenir Issued']:
            self.assertIn(action, actions)
        self.assertEqual(actions.count('Payment Recorded'), 2)
        self.assertNotIn('secret123', str(response.json))
        self.assertNotIn('password_hash', str(response.json))
        self.assertEqual(self.client.get('/api/ist/audit-logs').status_code, 401)
        token = self.login(username='ama', password='secret123').json['token']
        self.db.users.update_one({'_id': 'ama'}, {'$set': {'role': 'Records Officer'}})
        limited = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.get('/api/ist/audit-logs', headers=limited).status_code, 403)
        self.assertEqual(self.client.post('/api/ist/accountability/export', headers=headers,
                                         json={'format': 'CSV', 'year': 'All'}).status_code, 200)
        export = self.client.get('/api/ist/audit-logs?module=Accountability', headers=headers).json
        self.assertEqual(export['total'], 1)

    def test_audit_updates_passwords_pagination_and_read_only_routes(self):
        headers = self.management_headers()
        self.create_account(headers)
        self.client.patch('/api/ist/admins/ama', headers=headers, json={'status': 'Inactive'})
        self.client.post('/api/ist/auth/password', headers=headers, json={
            'currentPassword': '1234', 'newPassword': 'new-password',
        })
        logs = self.client.get('/api/ist/audit-logs', headers=headers).json['logs']
        self.assertIn('Administrator Updated', [e['action'] for e in logs])
        self.assertIn('Password Changed', [e['action'] for e in logs])
        self.assertNotIn('new-password', str(logs))
        for index in range(55):
            self.db.audit_logs.insert_one({'_id': f'page-{index}', 'id': f'page-{index}',
                'timestamp': '2026-01-01T00:00:00+00:00', 'severity': 'critical', 'module': 'Test',
                'action': 'Test Event', 'entityId': str(index), 'details': 'Pagination', 'performedBy': 'Tester', 'ipAddress': ''})
        first = self.client.get('/api/ist/audit-logs?module=Test&severity=critical', headers=headers).json
        second = self.client.get('/api/ist/audit-logs?module=Test&severity=critical&page=2', headers=headers).json
        self.assertEqual(first['total'], 55)
        self.assertEqual(first['counts']['critical'], 55)
        self.assertEqual(len(first['logs']), 50)
        self.assertEqual(len(second['logs']), 5)
        self.assertEqual(self.client.post('/api/ist/audit-logs', headers=headers, json={}).status_code, 405)

    def test_student_lists_and_dashboard_read_settings_once(self):
        headers, registration = self.student_setup()
        response = self.client.post('/api/ist/students', headers=headers, json=registration)
        self.assertEqual(response.status_code, 201)
        student = self.db.students.find_one()
        for i in range(20):
            duplicate = dict(student, _id=f'copy-{i}', id=f'copy-{i}', indexNumber=f'COPY/{i}')
            self.db.students.insert_one(duplicate)
        for path in ('/api/ist/students', '/api/ist/dashboard'):
            with self.subTest(path=path), patch.object(
                self.db.settings, 'find_one', wraps=self.db.settings.find_one
            ) as reads, patch.object(
                self.db.settings, 'update_one', wraps=self.db.settings.update_one
            ) as writes:
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(reads.call_count, 1)
                self.assertEqual(writes.call_count, 0)

    def test_dashboard_empty_state_authentication_and_permissions(self):
        self.assertEqual(self.client.get('/api/ist/dashboard').status_code, 401)
        headers = self.management_headers()
        response = self.client.get('/api/ist/dashboard', headers=headers)
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data['stats']['totalStudents'], 0)
        self.assertEqual(data['stats']['collectionRate'], 0)
        self.assertEqual(data['stats']['souvenirTotal'], 0)
        self.assertEqual(data['recentStudents'], [])
        self.assertEqual(data['recentPayments'], [])
        self.assertEqual(data['recentActivity'], [])
        self.assertEqual(len(data['registrationChart']), 30)
        self.assertEqual(len(data['paymentChart']), 30)
        self.assertTrue(all(point['value'] == 0 for point in data['registrationChart']))
        self.assertTrue(data['permissions']['manageAdmins'])
        self.create_account(headers, role='Records Officer')
        token = self.login(username='ama', password='secret123').json['token']
        limited = {'Authorization': 'Bearer ' + token}
        data = self.client.get('/api/ist/dashboard', headers=limited).json
        self.assertTrue(data['permissions']['souvenirs'])
        self.assertFalse(data['permissions']['payments'])
        self.assertFalse(data['permissions']['register'])
        self.assertFalse(data['permissions']['audit'])
        self.db.users.update_one({'_id': 'ama'}, {'$set': {'status': 'Inactive'}})
        self.assertEqual(self.client.get('/api/ist/dashboard', headers=limited).status_code, 401)

    def test_programme_without_type_saves_and_supports_registration(self):
        headers, registration = self.student_setup()
        settings = self.client.get('/api/ist/settings', headers=headers).json
        settings['config']['programmes'][0].pop('type')
        saved = self.client.patch('/api/ist/settings', headers=headers, json={
            'config': settings['config'], 'version': settings['version'],
        })
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn('type', saved.json['config']['programmes'][0])
        response = self.client.post('/api/ist/students', headers=headers, json=registration)
        self.assertEqual(response.status_code, 201)

    def test_dashboard_matches_accountability_and_tracking(self):
        headers, registration = self.student_setup()
        self.client.post('/api/ist/students', headers=headers, json=registration)
        initial = self.client.get('/api/ist/dashboard', headers=headers).json
        today = initial['generatedAt'][:10]
        self.client.post('/api/ist/payments', headers=headers, json={
            'studentId': registration['requestId'], 'requestId': 'payment-request-0001',
            'amount': 25, 'method': 'Cash', 'date': today,
        })
        data = self.client.get('/api/ist/dashboard', headers=headers).json
        summary = self.client.get('/api/ist/accountability', headers=headers).json
        self.assertEqual(data['stats']['totalCollected'], summary['totalCollected'])
        self.assertEqual(data['stats']['totalFees'], summary['totalFees'])
        self.assertEqual(data['stats']['outstanding'], summary['totalOutstanding'])
        self.assertEqual(data['stats']['collectionRate'], 75)
        self.assertEqual(data['stats']['totalStudents'], 1)
        self.assertEqual(data['stats']['partialCount'], 1)
        self.assertEqual(data['stats']['recentCollected'], 75)
        self.assertEqual(data['registrationChart'][-1]['value'], 1)
        self.assertEqual(data['paymentChart'][-1]['value'], 75)
        self.assertEqual(data['paymentMethods'][2]['value'], 2)
        self.assertEqual(data['programmes'][0]['full'], 'Programme')
        self.assertEqual(data['recentStudents'][0]['id'], registration['requestId'])
        self.assertEqual(len(data['recentPayments']), 2)
        self.assertEqual(len(data['recentActivity']), 4)
        items = self.client.get('/api/ist/souvenirs', headers=headers).json['souvenirs']
        self.assertEqual(data['stats']['souvenirTotal'], len(items))
        self.assertEqual(data['stats']['souvenirReceived'], 1)

    def test_dashboard_year_filter_historical_data_and_new_items(self):
        headers, registration = self.student_setup()
        self.client.post('/api/ist/students', headers=headers, json=registration)
        historical = self.db.students.find_one()
        historical.update(_id='historic-student', id='historic-student', indexNumber='OLD/001',
                          academicYear='2025/2026', program='Historical Programme',
                          amountPaid=0, paymentStatus='Unpaid', enrollmentDate='2025-01-01',
                          createdAt='2025-01-01T00:00:00+00:00', souvenirItems=[], souvenirs={})
        self.db.students.insert_one(historical)
        data = self.client.get('/api/ist/dashboard?year=2025/2026', headers=headers).json
        self.assertEqual(data['stats']['totalStudents'], 1)
        self.assertEqual(data['stats']['unpaidCount'], 1)
        self.assertEqual(data['stats']['recentRegistrations'], 0)
        self.assertEqual(data['programmes'][0]['full'], 'Historical Programme')
        self.assertEqual(data['recentStudents'][0]['id'], 'historic-student')
        self.assertEqual(data['stats']['souvenirPending'], 1)
        all_years = self.client.get('/api/ist/dashboard', headers=headers).json
        self.assertEqual(all_years['stats']['totalStudents'], 2)
        self.assertEqual(all_years['stats']['outstanding'], 150)
        self.assertEqual(all_years['stats']['souvenirTotal'], 2)
        self.assertEqual(self.client.get('/api/ist/dashboard?year=invalid', headers=headers).status_code, 400)


if __name__ == '__main__':
    unittest.main()
