"""IST authentication, independent of Speedlink's website sessions."""
import hashlib
import os
import secrets
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from flask_cors import CORS
from pymongo.errors import PyMongoError, DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash

from istdb import db as ist_db
from ist_backend.activity import audit_event, register_activity_routes
from ist_backend.settings import DEFAULTS, read_settings, validate_settings

ist_bp = Blueprint('ist', __name__, url_prefix='/api/ist')
CORS(ist_bp, origins=[origin.strip() for origin in os.getenv(
    'IST_ALLOWED_ORIGINS', 'https://ist-record-keeping.onrender.com'
).split(',') if origin.strip()], methods=['GET', 'POST', 'PATCH', 'OPTIONS'],
     allow_headers=['Content-Type', 'Authorization'], supports_credentials=False)


def now():
    return datetime.now(timezone.utc)


@ist_bp.before_request
def prepare_storage():
    if request.method == 'OPTIONS':
        return None
    # Index creation is idempotent and also works across Gunicorn workers.
    ist_db.auth_sessions.create_index('expires_at', expireAfterSeconds=0)
    ist_db.auth_attempts.create_index('expires_at', expireAfterSeconds=0)


@ist_bp.errorhandler(PyMongoError)
def database_unavailable(error):
    current_app.logger.error('IST database operation failed (%s)', type(error).__name__)
    return jsonify(message='IST login is temporarily unavailable. Please try again.'), 503


@ist_bp.after_request
def prevent_caching(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def authenticated_session():
    scheme, _, token = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not token or len(token) > 256:
        return None
    return ist_db.auth_sessions.find_one({
        '_id': hashlib.sha256(token.encode()).hexdigest(),
        'expires_at': {'$gt': now()},
    })


@ist_bp.route('/auth/login', methods=['POST'])
def login():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(message='A JSON login request is required.'), 400
    username, password = payload.get('username'), payload.get('password')
    remember = payload.get('remember', False)
    if (not isinstance(username, str) or not isinstance(password, str)
            or not username.strip() or not password or len(username) > 100
            or len(password) > 256 or not isinstance(remember, bool)):
        return jsonify(message='Please enter a valid username and password.'), 400
    username = username.strip().lower()
    timestamp = now()
    # Limit attempts per source address in fixed 15-minute windows, in MongoDB
    # so the limit is shared by workers. Do not trust client-supplied IP headers.
    bucket = int(timestamp.timestamp()) // 900
    attempt_id = hashlib.sha256(f'{request.remote_addr}:{bucket}'.encode()).hexdigest()
    attempt = ist_db.auth_attempts.find_one_and_update(
        {'_id': attempt_id},
        {'$inc': {'count': 1}, '$setOnInsert': {'expires_at': timestamp + timedelta(minutes=30)}},
        upsert=True, return_document=True,
    )
    if attempt['count'] > 10:
        response = jsonify(message='Too many login attempts. Please try again in 15 minutes.')
        response.headers['Retry-After'] = str(900 - int(timestamp.timestamp()) % 900)
        return response, 429
    # Create the requested initial admin once; never overwrite a stored password.
    user = ist_db.users.find_one({'_id': 'admin'})
    if user is None:
        ist_db.users.update_one({'_id': 'admin'}, {'$setOnInsert': {
            'username': 'admin', 'password_hash': generate_password_hash(
                os.getenv('IST_ADMIN_PASSWORD', '1234')),
            'role': 'admin', 'created_at': timestamp,
        }}, upsert=True)
    user = ist_db.users.find_one({'_id': username})
    valid_password = check_password_hash(user['password_hash'], password) if user else False
    if not valid_password or user.get('status', 'Active') != 'Active':
        event = audit_event({'username': username}, 'Login Failed', 'Authentication', username, 'Invalid credentials or inactive account.', now, 'warning')
        ist_db.audit_logs.insert_one(dict(event, _id=event['id']))
        return jsonify(message='Invalid username or password.'), 401
    token = secrets.token_urlsafe(48)
    config = read_settings(ist_db)['config']
    expires_at = timestamp + timedelta(minutes=config['sessionMinutes'])
    ist_db.auth_sessions.insert_one({
        '_id': hashlib.sha256(token.encode()).hexdigest(), 'user_id': user['_id'],
        'created_at': timestamp, 'expires_at': expires_at,
    })
    ist_db.users.update_one({'_id': user['_id']}, {'$set': {'last_login': timestamp}})
    event = audit_event(user, 'Login Successful', 'Authentication', user['_id'], 'Signed in to IST.', now)
    ist_db.audit_logs.insert_one(dict(event, _id=event['id']))
    return jsonify(token=token, expires_at=expires_at.isoformat(),
                   user=public_user(user))


@ist_bp.route('/auth/me', methods=['GET'])
def me():
    auth_session = authenticated_session()
    if not auth_session:
        return jsonify(message='Your session has expired. Please sign in again.'), 401
    user = ist_db.users.find_one({'_id': auth_session['user_id']})
    if not user or user.get('status', 'Active') != 'Active':
        return jsonify(message='Please sign in again.'), 401
    return jsonify(user=public_user(user))


@ist_bp.route('/auth/logout', methods=['POST'])
def logout():
    auth_session = authenticated_session()
    if auth_session:
        user = ist_db.users.find_one({'_id': auth_session['user_id']})
        if user:
            event = audit_event(user, 'Logout', 'Authentication', user['_id'], 'Signed out of IST.', now)
            ist_db.audit_logs.insert_one(dict(event, _id=event['id']))
        ist_db.auth_sessions.delete_one({'_id': auth_session['_id']})
    return jsonify(success=True)


ROLES = {'Super Admin', 'Admin', 'Registrar', 'Finance Officer', 'Records Officer'}


def public_user(user):
    def date(value):
        return value.isoformat() if isinstance(value, datetime) else None
    return {
        'id': str(user['_id']), 'username': user['username'],
        'name': user.get('name', 'Administrator'), 'email': user.get('email', ''),
        'phone': user.get('phone', ''),
        'role': 'Super Admin' if user.get('role') == 'admin' else user['role'],
        'status': user.get('status', 'Active'),
        'lastLogin': date(user.get('last_login')), 'createdAt': date(user.get('created_at')),
    }


def management_user():
    auth_session = authenticated_session()
    user = ist_db.users.find_one({'_id': auth_session['user_id']}) if auth_session else None
    if not user or user.get('status', 'Active') != 'Active':
        return None, (jsonify(message='Please sign in again.'), 401)
    if public_user(user)['role'] not in {'Super Admin', 'Admin'}:
        return None, (jsonify(message='You do not have permission to manage administrators.'), 403)
    return user, None


def admin_fields(payload, creating=False):
    if not isinstance(payload, dict):
        raise ValueError('A JSON account request is required.')
    result = {}
    for field, limit in [('name', 150), ('email', 254), ('phone', 40)]:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f'Please enter a valid {field}.')
        result[field] = value.strip()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', result['email']):
        raise ValueError('Please enter a valid email address.')
    role = payload.get('role')
    status = payload.get('status', 'Active')
    if not isinstance(role, str) or role not in ROLES or status not in ('Active', 'Inactive'):
        raise ValueError('Please select a valid role and status.')
    result.update(role=role, status=status)
    password = payload.get('password', '')
    minimum = read_settings(ist_db)['config']['passwordMinLength']
    if not isinstance(password, str) or len(password) > 256 or ((creating or password) and len(password) < minimum):
        raise ValueError(f'The password must contain between {minimum} and 256 characters.')
    if password:
        result['password_hash'] = generate_password_hash(password)
    return result


@ist_bp.route('/admins', methods=['GET', 'POST'])
def admins():
    actor, error = management_user()
    if error:
        return error
    if request.method == 'GET':
        return jsonify(admins=[public_user(user) for user in ist_db.users.find().sort('created_at', 1)],
                       currentUser=public_user(actor))
    payload = request.get_json(silent=True)
    try:
        fields = admin_fields(payload, creating=True)
        username = payload.get('username')
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,100}', username.strip()):
            raise ValueError('Username must be 3–100 letters, numbers, dots, underscores or hyphens.')
        username = username.strip().lower()
        if public_user(actor)['role'] != 'Super Admin' and fields['role'] == 'Super Admin':
            return jsonify(message='Only a Super Admin can assign the Super Admin role.'), 403
        document = dict(fields, _id=username, username=username, created_at=now(), created_by=actor['_id'])
        document['auditEvents'] = [audit_event(actor, 'Administrator Created', 'Administrators', username, f"Created {username} with role {fields['role']}.", now)]
        ist_db.users.insert_one(document)
    except ValueError as error:
        return jsonify(message=str(error)), 400
    except DuplicateKeyError:
        return jsonify(message='That username already exists.'), 409
    return jsonify(admin=public_user(document)), 201


@ist_bp.route('/admins/<username>', methods=['PATCH'])
def update_admin(username):
    actor, error = management_user()
    if error:
        return error
    target = ist_db.users.find_one({'_id': username})
    if not target:
        return jsonify(message='Administrator not found.'), 404
    payload = request.get_json(silent=True)
    try:
        if isinstance(payload, dict) and set(payload) == {'status'}:
            if payload['status'] not in ('Active', 'Inactive'):
                raise ValueError('Please select a valid status.')
            fields = {'status': payload['status']}
        else:
            fields = admin_fields(payload)
    except ValueError as error:
        return jsonify(message=str(error)), 400
    actor_role, target_role = public_user(actor)['role'], public_user(target)['role']
    if actor_role != 'Super Admin' and (target_role == 'Super Admin' or fields.get('role') == 'Super Admin'):
        return jsonify(message='Only a Super Admin can change Super Admin accounts.'), 403
    if actor['_id'] == target['_id'] and (fields.get('status') == 'Inactive'
                                         or fields.get('role', actor_role) != actor_role):
        return jsonify(message='You cannot deactivate your account or change your own role.'), 400
    if target['_id'] == 'admin' and fields.get('role', target_role) != 'Super Admin':
        return jsonify(message='The initial administrator must remain a Super Admin.'), 400
    fields['updated_at'] = now()
    changed_fields = ', '.join('password' if field == 'password_hash' else field for field in fields if field != 'updated_at')
    event = audit_event(actor, 'Administrator Updated', 'Administrators', username, f'Updated fields: {changed_fields}.', now, 'warning' if fields.get('status') == 'Inactive' or 'password_hash' in fields else 'info')
    ist_db.users.update_one({'_id': username}, {'$set': fields, '$push': {'auditEvents': event}})
    if fields.get('status') == 'Inactive' or 'password_hash' in fields:
        ist_db.auth_sessions.delete_many({'user_id': username})
    return jsonify(admin=public_user(ist_db.users.find_one({'_id': username})))


@ist_bp.route('/settings', methods=['GET', 'PATCH'])
def settings():
    auth_session = authenticated_session()
    actor = ist_db.users.find_one({'_id': auth_session['user_id']}) if auth_session else None
    if not actor or actor.get('status', 'Active') != 'Active':
        return jsonify(message='Please sign in again.'), 401
    can_edit = public_user(actor)['role'] in {'Admin', 'Super Admin'}
    if request.method == 'GET':
        document = read_settings(ist_db)
        return jsonify(config=document['config'], version=document['version'], canEdit=can_edit,
                       defaults=DEFAULTS)
    if not can_edit:
        return jsonify(message='You do not have permission to change settings.'), 403
    payload = request.get_json(silent=True)
    try:
        config = validate_settings(payload.get('config') if isinstance(payload, dict) else None)
        version = payload.get('version')
        if type(version) is not int:
            raise ValueError('A settings version is required.')
    except ValueError as error:
        return jsonify(message=str(error)), 400
    read_settings(ist_db)
    result = ist_db.settings.update_one({'_id': 'config', 'version': version}, {
        '$set': {'config': config, 'updated_at': now(), 'updated_by': actor['_id']},
        '$inc': {'version': 1},
        '$push': {'auditEvents': audit_event(actor, 'Settings Updated', 'Settings', 'config',
            f"Saved settings version {version + 1}; {len(config['programmes'])} programmes and {len(config['souvenirItems'])} souvenir items.", now)},
    })
    if not result.modified_count:
        return jsonify(message='Settings changed in another session. Reload settings before saving.'), 409
    return jsonify(config=config, version=version + 1)


@ist_bp.route('/auth/password', methods=['POST'])
def change_password():
    auth_session = authenticated_session()
    user = ist_db.users.find_one({'_id': auth_session['user_id']}) if auth_session else None
    if not user or user.get('status', 'Active') != 'Active':
        return jsonify(message='Please sign in again.'), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(message='A JSON password request is required.'), 400
    current, password = payload.get('currentPassword'), payload.get('newPassword')
    minimum = read_settings(ist_db)['config']['passwordMinLength']
    if not isinstance(current, str) or len(current) > 256 or not check_password_hash(user['password_hash'], current):
        return jsonify(message='Current password is incorrect.'), 400
    if not isinstance(password, str) or not minimum <= len(password) <= 256:
        return jsonify(message=f'New password must contain {minimum}–256 characters.'), 400
    ist_db.users.update_one({'_id': user['_id']}, {'$set': {
        'password_hash': generate_password_hash(password), 'updated_at': now(),
    }, '$push': {'auditEvents': audit_event(user, 'Password Changed', 'Authentication', user['_id'], 'Changed own password; other sessions revoked.', now, 'warning')}})
    ist_db.auth_sessions.delete_many({'user_id': user['_id'], '_id': {'$ne': auth_session['_id']}})
    return jsonify(success=True)


from ist_backend.students import register_student_routes
register_student_routes(ist_bp, ist_db, authenticated_session, public_user, now)

from ist_backend.tracking import register_tracking_routes
register_tracking_routes(ist_bp, ist_db, authenticated_session, public_user, now)

register_activity_routes(ist_bp, ist_db, authenticated_session, public_user, now)

from ist_backend.dashboard import register_dashboard_routes
register_dashboard_routes(ist_bp, ist_db, authenticated_session, public_user, now)
