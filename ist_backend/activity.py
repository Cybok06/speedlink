"""Accountability summaries and read-only activity history."""
import secrets
from flask import jsonify, request
from ist_backend.settings import read_settings
from ist_backend.tracking import cents, student_with_items, payment_rows


def audit_event(actor, action, module, entity_id, details, now, severity='info', event_id=None):
    return {'id': event_id or secrets.token_hex(16), 'action': action, 'module': module,
            'entityId': str(entity_id), 'entityType': module, 'performedBy': actor.get('name') or actor.get('username', 'Unknown'),
            'actorId': str(actor.get('_id', '')), 'timestamp': now().isoformat(),
            'details': details, 'ipAddress': request.remote_addr or '', 'severity': severity}


def register_activity_routes(bp, database, authenticated_session, public_user, now):
    def actor():
        auth = authenticated_session()
        user = database.users.find_one({'_id': auth['user_id']}) if auth else None
        return user if user and user.get('status', 'Active') == 'Active' else None

    @bp.route('/search', methods=['GET'])
    def global_search():
        if not actor():
            return jsonify(message='Please sign in again.'), 401
        query = request.args.get('q', '').strip().casefold()
        if len(query) > 150:
            return jsonify(message='Search must be at most 150 characters.'), 400
        if not query:
            return jsonify(students=[], payments=[])
        students, payments = [], []
        for student in database.students.find():
            if query in ' '.join(str(student.get(k, '')) for k in
                                ('firstName', 'lastName', 'indexNumber', 'email', 'phone', 'program')).casefold():
                students.append({k: student.get(k, '') for k in
                                 ('id', 'firstName', 'lastName', 'indexNumber', 'program')})
            for payment in payment_rows(student):
                if query in ' '.join(str(payment.get(k, '')) for k in
                                    ('studentName', 'indexNumber', 'reference', 'method', 'amount', 'date', 'notes')).casefold():
                    payments.append(payment)
        payments.sort(key=lambda p: (p['date'], p['id']), reverse=True)
        students.sort(key=lambda s: (s['firstName'], s['lastName'], s['id']))
        return jsonify(students=students[:10], payments=payments[:10])

    @bp.route('/notifications', methods=['GET', 'POST'])
    def notifications():
        user = actor()
        if not user:
            return jsonify(message='Please sign in again.'), 401
        user_id = user['_id']
        if request.method == 'POST':
            payload = request.get_json(silent=True)
            ids = payload.get('ids') if isinstance(payload, dict) else None
            if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) or len(i) > 250 for i in ids):
                return jsonify(message='Select valid notifications.'), 400
            database.notification_reads.update_one({'_id': user_id},
                                                   {'$addToSet': {'ids': {'$each': ids}}}, upsert=True)
            return jsonify(success=True)
        read_ids = set((database.notification_reads.find_one({'_id': user_id}) or {}).get('ids', []))
        items = []
        for student in database.students.find():
            name = f"{student['firstName']} {student['lastName']}"
            items.append({'id': 'student-' + student['id'], 'studentId': student['id'],
                          'title': 'Student registered', 'details': f"{name} · {student['indexNumber']}",
                          'timestamp': student['createdAt']})
            for payment in payment_rows(student):
                items.append({'id': 'payment-' + student['id'] + '-' + payment['id'],
                              'studentId': student['id'], 'title': 'Payment recorded',
                              'details': f"{name} · GHS {payment['amount']:.2f} · {payment['reference']}",
                              'timestamp': payment.get('createdAt') or student['createdAt'], 'payment': payment})
        items.sort(key=lambda item: (item['timestamp'], item['id']), reverse=True)
        items = [dict(item, read=item['id'] in read_ids) for item in items[:50]]
        return jsonify(notifications=items, unreadCount=sum(not item['read'] for item in items))

    @bp.route('/accountability', methods=['GET'])
    def accountability():
        if not actor():
            return jsonify(message='Please sign in again.'), 401
        config = read_settings(database)['config']
        documents = list(database.students.find())
        years = sorted({s['academicYear'] for s in documents} | {config['academicYear']}, reverse=True)
        year = request.args.get('year', 'All')
        students = [student_with_items(database, s) for s in documents if year == 'All' or s['academicYear'] == year]
        summary = []
        for programme in sorted({s['program'] for s in students}):
            group = [s for s in students if s['program'] == programme]
            fees = sum(cents(s['totalFees']) for s in group)
            collected = sum(cents(s['amountPaid']) for s in group)
            summary.append({'program': programme, 'enrolled': len(group),
                            'paid': sum(s['paymentStatus'] == 'Paid' for s in group),
                            'partial': sum(s['paymentStatus'] == 'Partial' for s in group),
                            'unpaid': sum(s['paymentStatus'] == 'Unpaid' for s in group),
                            'fees': fees / 100, 'collected': collected / 100, 'balance': (fees - collected) / 100,
                            'souvenirIssued': sum(s['souvenirStatus'] == 'Received' for s in group)})
        fees = sum(cents(s['totalFees']) for s in students)
        collected = sum(cents(s['amountPaid']) for s in students)
        return jsonify(students=[{k: v for k, v in s.items() if k != '_id'} for s in students],
                       programSummary=summary, totalFees=fees / 100, totalCollected=collected / 100,
                       totalOutstanding=(fees - collected) / 100, collectionRate=round(collected / fees * 100) if fees else 0,
                       souvenirReceived=sum(s['souvenirStatus'] == 'Received' for s in students),
                       years=years, generatedAt=now().isoformat())

    @bp.route('/accountability/export', methods=['POST'])
    def export_accountability():
        user = actor()
        if not user:
            return jsonify(message='Please sign in again.'), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('format') not in ('CSV', 'Print/PDF'):
            return jsonify(message='Select a supported export format.'), 400
        year = payload.get('year', 'All')
        if not isinstance(year, str) or len(year) > 20:
            return jsonify(message='Invalid academic year.'), 400
        event = audit_event(user, 'Accountability Export Requested', 'Accountability', year,
                            f"Requested {payload['format']} export for {year} academic years.", now)
        database.audit_logs.insert_one(dict(event, _id=event['id']))
        return jsonify(success=True)

    @bp.route('/audit-logs', methods=['GET'])
    def audit_logs():
        user = actor()
        if not user:
            return jsonify(message='Please sign in again.'), 401
        if public_user(user)['role'] not in ('Admin', 'Super Admin'):
            return jsonify(message='Only Admin and Super Admin accounts may view audit logs.'), 403
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            return jsonify(message='Invalid page.'), 400
        events = [{k: v for k, v in e.items() if k != '_id'} for e in database.audit_logs.find()]
        for collection in (database.users, database.settings):
            for document in collection.find({}, {'auditEvents': 1}):
                events.extend(document.get('auditEvents', []))
        for student in database.students.find():
            events.append({'id': 'registration-' + student['id'], 'timestamp': student['createdAt'],
                           'action': 'Student Registered', 'module': 'Students', 'entityId': student['indexNumber'],
                           'entityType': 'Student', 'performedBy': student.get('registeredBy', ''), 'severity': 'info',
                           'ipAddress': student.get('registrationIp', ''), 'details': f"Registered {student['firstName']} {student['lastName']} in {student['program']}."})
            for payment in payment_rows(student):
                events.append({'id': 'payment-' + payment['id'], 'timestamp': payment.get('createdAt') or student['createdAt'],
                               'action': 'Payment Recorded', 'module': 'Payments', 'entityId': student['indexNumber'],
                               'entityType': 'Student', 'performedBy': payment['recordedBy'], 'severity': 'info',
                               'ipAddress': payment.get('ipAddress', student.get('registrationIp', '')),
                               'details': f"Recorded GHS {payment['amount']:.2f} via {payment['method']}; reference {payment['reference']}."})
            for item in student.get('souvenirItems', []):
                if item.get('received'):
                    events.append({'id': 'souvenir-' + student['id'] + '-' + item['id'],
                                   'timestamp': item.get('issuedDate', student['createdAt']), 'action': 'Souvenir Issued',
                                   'module': 'Souvenirs', 'entityId': student['indexNumber'], 'entityType': 'Student',
                                   'performedBy': item.get('issuedBy', student.get('registeredBy', '')), 'severity': 'info',
                                   'ipAddress': item.get('ipAddress', student.get('registrationIp', '')),
                                   'details': f"Issued {item['label']} ({item['qty']})."})
        modules = sorted({e['module'] for e in events})
        query = request.args.get('search', '').lower()
        severity, module = request.args.get('severity', 'All'), request.args.get('module', 'All')
        events = [e for e in events if (severity == 'All' or e['severity'] == severity)
                  and (module == 'All' or e['module'] == module)
                  and (not query or query in ' '.join(str(e.get(key, '')) for key in ('action', 'details', 'performedBy', 'entityId')).lower())]
        events.sort(key=lambda e: (e['timestamp'], e['id']), reverse=True)
        total = len(events)
        counts = {severity: sum(e['severity'] == severity for e in events) for severity in ('info', 'warning', 'critical')}
        return jsonify(logs=events[(page - 1) * 50:page * 50], total=total, page=page, pageSize=50,
                       modules=modules, counts=counts)
