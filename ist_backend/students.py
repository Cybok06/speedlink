"""Authenticated student registration and records."""
import math
import re
from datetime import date
from flask import jsonify, request
from pymongo.errors import DuplicateKeyError
from ist_backend.settings import read_settings
from ist_backend.tracking import cents, student_with_items

LEVELS = {'BSc': ['100', '200', '300', '400'], 'HND': ['HND 1', 'HND 2'],
          'Diploma': ['Year 1', 'Year 2'], 'Certificate': ['Year 1']}


def register_student_routes(bp, database, authenticated_session, public_user, now):
    def actor():
        auth = authenticated_session()
        user = database.users.find_one({'_id': auth['user_id']}) if auth else None
        return user if user and user.get('status', 'Active') == 'Active' else None

    def serialize(document, config=None):
        return {key: value for key, value in student_with_items(database, document, config).items() if key != '_id'}

    @bp.route('/students', methods=['GET', 'POST'])
    def student_records():
        user = actor()
        if not user:
            return jsonify(message='Please sign in again.'), 401
        if request.method == 'GET':
            config = read_settings(database)['config']
            return jsonify(students=[serialize(s, config) for s in database.students.find().sort('createdAt', -1)])
        if public_user(user)['role'] not in {'Super Admin', 'Admin', 'Registrar'}:
            return jsonify(message='You do not have permission to register students.'), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(message='A JSON registration is required.'), 400
        request_id = payload.get('requestId')
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,100}', request_id):
            return jsonify(message='A registration request ID is required.'), 400
        existing = database.students.find_one({'_id': request_id})
        if existing:
            return jsonify(student=serialize(existing)), 200
        config = read_settings(database)['config']
        try:
            fields = {}
            for key in ['indexNumber', 'firstName', 'lastName', 'gender', 'dateOfBirth', 'nationality', 'email', 'phone',
                        'address', 'emergencyContact', 'emergencyPhone', 'program', 'level', 'academicYear',
                        'paymentMethod', 'momoNetwork', 'transactionRef', 'receiptNumber', 'paymentNotes', 'notes']:
                value = payload.get(key, '')
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError(f'Invalid {key}.')
                fields[key] = value.strip()
            for key in ['indexNumber', 'firstName', 'lastName', 'phone', 'program', 'level', 'academicYear', 'gender']:
                if not fields[key]:
                    raise ValueError(f'{key} is required.')
            if len(fields['indexNumber']) > 100 or any(c.isspace() or ord(c) < 32 for c in fields['indexNumber']):
                raise ValueError('Index number must be at most 100 characters with no spaces.')
            if fields['gender'] not in ('Male', 'Female'):
                raise ValueError('Select a valid gender.')
            if fields['email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', fields['email']):
                raise ValueError('Enter a valid email address.')
            if fields['dateOfBirth'] and date.fromisoformat(fields['dateOfBirth']) > date.today():
                raise ValueError('Date of birth cannot be in the future.')
            programme = next((p for p in config['programmes'] if p['name'] == fields['program']), None)
            if not programme or fields['level'] not in ['100', '200', '300', '400']:
                raise ValueError('Choose a configured programme and a valid level.')
            if fields['academicYear'] != config['academicYear']:
                raise ValueError('The academic year has changed. Reload settings and try again.')
            if payload.get('consentChecked') is not True:
                raise ValueError('Registration consent is required.')
            amount = payload.get('amountPaid', 0)
            cents(amount)
            cents(programme['fee'])
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0 or amount > programme['fee']:
                raise ValueError('Payment must be between zero and the programme fee.')
            if amount and fields['paymentMethod'] not in ('Cash', 'MoMo'):
                raise ValueError('Select a payment method.')
            if amount and fields['paymentMethod'] == 'MoMo' and (not fields['momoNetwork'] or not fields['transactionRef']):
                raise ValueError('Mobile money network and transaction reference are required.')
            selections = payload.get('souvenirs', {})
            allowed = {item['id'] for item in config['souvenirItems']}
            if not isinstance(selections, dict) or set(selections) - allowed or any(type(v) is not bool for v in selections.values()):
                raise ValueError('Select valid configured souvenir items.')
        except (ValueError, TypeError) as error:
            return jsonify(message=str(error)), 400
        database.students.create_index('indexNumber', unique=True)
        timestamp = now().isoformat()
        issued = [dict(item, received=selections.get(item['id'], False)) for item in config['souvenirItems']]
        received = sum(item['received'] for item in issued)
        document = dict(fields, _id=request_id, id=request_id,
                        totalFees=programme['fee'], amountPaid=amount,
                        paymentStatus='Paid' if amount >= programme['fee'] else ('Partial' if amount else 'Unpaid'),
                        souvenirStatus='Received' if issued and received == len(issued) else ('Pending' if received else 'Not Issued'),
                        souvenirItems=issued, souvenirs=selections, studentStatus='Active', consentGiven=True,
                        registeredBy=public_user(user)['name'], registeredById=user['_id'],
                        createdAt=timestamp, enrollmentDate=timestamp[:10], registrationIp=request.remote_addr or '')
        try:
            database.students.insert_one(document)
        except DuplicateKeyError:
            existing = database.students.find_one({'_id': request_id})
            if existing:
                return jsonify(student=serialize(existing)), 200
            return jsonify(message='That index number already exists. Enter a different index number.'), 409
        return jsonify(student=serialize(document)), 201

    @bp.route('/students/<student_id>', methods=['GET'])
    def student_record(student_id):
        if not actor():
            return jsonify(message='Please sign in again.'), 401
        document = database.students.find_one({'_id': student_id})
        if not document:
            return jsonify(message='Student not found.'), 404
        return jsonify(student=serialize(document))
