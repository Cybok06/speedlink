"""Dashboard summaries derived from the same student and payment snapshot."""
from collections import Counter
from datetime import timedelta
from flask import jsonify, request
from ist_backend.settings import read_settings
from ist_backend.tracking import cents, payment_rows, student_with_items


def register_dashboard_routes(bp, database, authenticated_session, public_user, now):
    @bp.route('/dashboard', methods=['GET'])
    def dashboard():
        auth = authenticated_session()
        actor = database.users.find_one({'_id': auth['user_id']}) if auth else None
        if not actor or actor.get('status', 'Active') != 'Active':
            return jsonify(message='Please sign in again.'), 401
        config = read_settings(database)['config']
        documents = list(database.students.find())
        years = sorted({s['academicYear'] for s in documents} | {config['academicYear']}, reverse=True)
        year = request.args.get('year', 'All')
        if year != 'All' and year not in years:
            return jsonify(message='Select a valid academic year.'), 400
        students = [student_with_items(database, s, config) for s in documents if year == 'All' or s['academicYear'] == year]
        payments = [row for s in students for row in payment_rows(s)]
        fees = sum(cents(s['totalFees']) for s in students)
        collected = sum(cents(s['amountPaid']) for s in students)
        souvenir_items = [item for s in students for item in s['souvenirItems']]
        received = sum(bool(item.get('received')) for item in souvenir_items)
        timestamp = now()
        today = timestamp.date()
        days = [(today - timedelta(days=29 - i)).isoformat() for i in range(30)]
        registrations = Counter(s['enrollmentDate'] for s in students)
        paid = Counter()
        for payment in payments:
            paid[payment['date'][:10]] += cents(payment['amount'])
        methods = Counter(p['method'] for p in payments)
        role = public_user(actor)['role']
        programmes = Counter(s['program'] for s in students)
        activity = []
        for student in students:
            activity.append({'id': 'registration-' + student['id'], 'studentId': student['id'],
                'timestamp': student['createdAt'], 'action': 'Student registered',
                'details': f"{student['firstName']} {student['lastName']} ? {student['indexNumber']}",
                'performedBy': student.get('registeredBy', '')})
            for payment in payment_rows(student):
                activity.append({'id': 'payment-' + payment['id'], 'studentId': student['id'],
                    'timestamp': payment.get('createdAt') or student['createdAt'], 'action': 'Payment recorded',
                    'details': f"{student['indexNumber']} ? GHS {payment['amount']:.2f}",
                    'performedBy': payment['recordedBy']})
            for item in student['souvenirItems']:
                if item.get('received'):
                    activity.append({'id': 'souvenir-' + student['id'] + '-' + item['id'], 'studentId': student['id'],
                        'timestamp': item.get('issuedDate') or student['createdAt'], 'action': 'Souvenir issued',
                        'details': f"{student['indexNumber']} ? {item['label']}",
                        'performedBy': item.get('issuedBy') or student.get('registeredBy', '')})
        activity.sort(key=lambda event: (event['timestamp'], event['id']), reverse=True)
        recent_students = sorted(students, key=lambda s: (s['createdAt'], s['id']), reverse=True)[:6]
        # Backdated payments are ordered by payment date, then recording time.
        recent_payments = sorted(payments, key=lambda p: (p['date'], p.get('createdAt', ''), p['id']), reverse=True)[:6]
        return jsonify(
            user=public_user(actor), academicYear=config['academicYear'], years=years, selectedYear=year,
            departmentName=config['departmentName'], generatedAt=timestamp.isoformat(),
            stats={'totalStudents': len(students), 'activeStudents': sum(s['studentStatus'] == 'Active' for s in students),
                   'paidCount': sum(s['paymentStatus'] == 'Paid' for s in students),
                   'partialCount': sum(s['paymentStatus'] == 'Partial' for s in students),
                   'unpaidCount': sum(s['paymentStatus'] == 'Unpaid' for s in students),
                   'totalFees': fees / 100, 'totalCollected': collected / 100, 'outstanding': (fees - collected) / 100,
                   'collectionRate': round(collected / fees * 100) if fees else 0,
                   'souvenirReceived': received, 'souvenirPending': len(souvenir_items) - received,
                   'souvenirTotal': len(souvenir_items),
                   'activeAdmins': database.users.count_documents({'status': {'$ne': 'Inactive'}}),
                   'recentRegistrations': sum(registrations[day] for day in days),
                   'recentCollected': sum(paid[day] for day in days) / 100},
            recentStudents=[{key: value for key, value in s.items() if key != '_id'} for s in recent_students],
            recentPayments=recent_payments, recentActivity=activity[:6],
            registrationChart=[{'label': day[5:], 'date': day, 'value': registrations[day]} for day in days],
            paymentChart=[{'label': day[5:], 'date': day, 'value': paid[day] / 100} for day in days],
            paymentMethods=[{'label': method, 'value': methods[method]} for method in
                            ('Mobile Money', 'Bank Transfer', 'Cash', 'Cheque')],
            programmes=[{'label': name, 'full': name, 'count': count} for name, count in sorted(programmes.items())],
            permissions={'register': role in ('Super Admin', 'Admin', 'Registrar'),
                         'payments': role in ('Super Admin', 'Admin', 'Registrar', 'Finance Officer'),
                         'souvenirs': role in ('Super Admin', 'Admin', 'Registrar', 'Records Officer'),
                         'manageAdmins': role in ('Super Admin', 'Admin'), 'audit': role in ('Super Admin', 'Admin')})
