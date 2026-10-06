"""Payment and souvenir updates are atomic within each student document."""
import re
from decimal import Decimal, InvalidOperation
from datetime import date
from flask import jsonify, request
from ist_backend.settings import read_settings


def cents(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Enter a valid amount.')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number * 100 != (number * 100).to_integral_value():
            raise ValueError('Amounts must be nonnegative with at most two decimal places.')
        return int(number * 100)
    except InvalidOperation:
        raise ValueError('Enter a valid amount.')


def payment_rows(student):
    events = student.get('paymentEvents', [])
    initial = cents(student['amountPaid']) - sum(cents(event['amount']) for event in events)
    rows = list(events)
    if initial > 0:
        rows.insert(0, {'id': 'registration-' + student['id'], 'amount': initial / 100,
                       'date': student['enrollmentDate'], 'method': 'Mobile Money' if student.get('paymentMethod') == 'MoMo' else student.get('paymentMethod', 'Cash'),
                       'reference': student.get('transactionRef') or student.get('receiptNumber') or 'Registration payment',
                       'recordedBy': student.get('registeredBy', ''), 'notes': student.get('paymentNotes', '')})
    return [dict(event, studentId=student['id'], studentName=f"{student['firstName']} {student['lastName']}",
                 indexNumber=student['indexNumber'], status=student['paymentStatus']) for event in rows]


def student_with_items(database, student, config=None):
    result = dict(student)
    items = [dict(item) for item in student.get('souvenirItems', [])]
    ids = {item['id'] for item in items}
    if config is None:
        config = read_settings(database)['config']
    for item in config['souvenirItems']:
        if item['id'] not in ids:
            items.append(dict(item, received=False))
    received = sum(bool(item.get('received')) for item in items)
    result['souvenirItems'] = items
    result['souvenirStatus'] = 'Received' if items and received == len(items) else ('Pending' if received else 'Not Issued')
    return result


def register_tracking_routes(bp, database, authenticated_session, public_user, now):
    def user():
        auth = authenticated_session()
        actor = database.users.find_one({'_id': auth['user_id']}) if auth else None
        return actor if actor and actor.get('status', 'Active') == 'Active' else None

    def items_for(student):
        return student_with_items(database, student)['souvenirItems']

    def souvenir_rows(student):
        items = items_for(student)
        partial = any(item.get('received') for item in items)
        return [{
            'id': student['id'] + ':' + item['id'], 'itemId': item['id'], 'studentId': student['id'],
            'studentName': f"{student['firstName']} {student['lastName']}", 'indexNumber': student['indexNumber'],
            'program': student['program'], 'item': item['label'], 'quantity': item['qty'],
            'status': 'Received' if item.get('received') else ('Pending' if partial else 'Not Issued'),
            'issuedDate': item.get('issuedDate') or (student['createdAt'] if item.get('received') else None),
            'issuedBy': item.get('issuedBy') or (student.get('registeredBy') if item.get('received') else None),
        } for item in items]

    @bp.route('/payments', methods=['GET', 'POST'])
    def payments():
        actor = user()
        if not actor:
            return jsonify(message='Please sign in again.'), 401
        if request.method == 'GET':
            students = list(database.students.find())
            rows = [row for student in students for row in payment_rows(student)]
            return jsonify(payments=sorted(rows, key=lambda row: row['date'], reverse=True),
                           canRecord=public_user(actor)['role'] in {'Super Admin', 'Admin', 'Finance Officer', 'Registrar'})
        if public_user(actor)['role'] not in {'Super Admin', 'Admin', 'Finance Officer', 'Registrar'}:
            return jsonify(message='You do not have permission to record payments.'), 403
        payload = request.get_json(silent=True)
        try:
            if not isinstance(payload, dict):
                raise ValueError('A JSON payment is required.')
            student_id, event_id = payload.get('studentId'), payload.get('requestId')
            if not isinstance(student_id, str) or not isinstance(event_id, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,100}', event_id):
                raise ValueError('A student and payment request ID are required.')
            amount = cents(payload.get('amount'))
            if amount <= 0:
                raise ValueError('Payment must be greater than zero.')
            method = payload.get('method')
            if method not in ('Cash', 'Mobile Money', 'Bank Transfer', 'Cheque'):
                raise ValueError('Select a valid payment method.')
            reference, notes, paid_date = payload.get('reference', ''), payload.get('notes', ''), payload.get('date')
            if not isinstance(reference, str) or len(reference) > 150 or not isinstance(notes, str) or len(notes) > 2000:
                raise ValueError('Invalid reference or notes.')
            if method != 'Cash' and not reference.strip():
                raise ValueError('A transaction reference is required for this payment method.')
            if not isinstance(paid_date, str) or date.fromisoformat(paid_date) > now().date():
                raise ValueError('Payment date cannot be in the future.')
        except (ValueError, TypeError) as error:
            return jsonify(message=str(error)), 400
        for _ in range(10):
            student = database.students.find_one({'_id': student_id})
            if not student:
                return jsonify(message='Student not found.'), 404
            events = student.get('paymentEvents', [])
            existing = next((event for event in events if event['id'] == event_id), None)
            if existing:
                return jsonify(success=True, payment=existing), 200
            if reference.strip() and any(row['reference'].strip().lower() == reference.strip().lower() for row in payment_rows(student)):
                return jsonify(message='That payment reference is already recorded for this student.'), 409
            total = cents(student['amountPaid']) + amount
            if total > cents(student['totalFees']):
                return jsonify(message='Payment exceeds the outstanding balance.'), 400
            status = 'Paid' if total == cents(student['totalFees']) else 'Partial'
            event = {'id': event_id, 'amount': amount / 100, 'date': paid_date, 'method': method,
                     'reference': reference.strip() or event_id, 'notes': notes.strip(),
                     'recordedBy': public_user(actor)['name'], 'recordedById': actor['_id'], 'createdAt': now().isoformat(), 'ipAddress': request.remote_addr or ''}
            result = database.students.update_one({'_id': student_id, 'amountPaid': student['amountPaid'],
                                                   'paymentEvents': student.get('paymentEvents')},
                {'$set': {'amountPaid': total / 100, 'paymentStatus': status, 'paymentEvents': events + [event]}})
            if result.modified_count:
                return jsonify(success=True, payment=event), 201
        return jsonify(message='Student payment was updated concurrently. Please retry.'), 409

    @bp.route('/souvenirs', methods=['GET'])
    def souvenirs():
        actor = user()
        if not actor:
            return jsonify(message='Please sign in again.'), 401
        return jsonify(souvenirs=[row for student in database.students.find() for row in souvenir_rows(student)],
                       canIssue=public_user(actor)['role'] in {'Super Admin', 'Admin', 'Registrar', 'Records Officer'})

    @bp.route('/souvenirs/issue', methods=['POST'])
    def issue_souvenir():
        actor = user()
        if not actor:
            return jsonify(message='Please sign in again.'), 401
        if public_user(actor)['role'] not in {'Super Admin', 'Admin', 'Registrar', 'Records Officer'}:
            return jsonify(message='You do not have permission to issue souvenirs.'), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get('studentId'), str) or not isinstance(payload.get('itemId'), str):
            return jsonify(message='A student and item are required.'), 400
        for _ in range(10):
            student = database.students.find_one({'_id': payload['studentId']})
            if not student:
                return jsonify(message='Student not found.'), 404
            items = items_for(student)
            item = next((item for item in items if item['id'] == payload['itemId']), None)
            if not item:
                return jsonify(message='Souvenir item not found.'), 404
            if item.get('received'):
                return jsonify(success=True, message='This item has already been issued.'), 200
            item.update(received=True, issuedDate=now().isoformat(), issuedBy=public_user(actor)['name'], issuedById=actor['_id'], ipAddress=request.remote_addr or '')
            status = 'Received' if all(item.get('received') for item in items) else 'Pending'
            result = database.students.update_one({'_id': student['_id'], 'souvenirItems': student.get('souvenirItems')},
                {'$set': {'souvenirItems': items, 'souvenirs': {item['id']: bool(item.get('received')) for item in items},
                          'souvenirStatus': status}})
            if result.modified_count:
                return jsonify(success=True, message='Souvenir issued successfully.'), 200
        return jsonify(message='Souvenirs changed concurrently. Please retry.'), 409
