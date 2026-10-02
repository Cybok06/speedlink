"""Shared IST configuration and validation."""
import math
import json
import hashlib
import re
from copy import deepcopy

DEFAULTS = {
    'institutionName': 'Accra Technical University',
    'departmentName': 'Information Systems & Technology',
    'academicYear': '2026/2027', 'indexPrefix': 'ATU/IST', 'indexYear': '26',
    'nextSequence': 1, 'sessionMinutes': 480, 'passwordMinLength': 4,
    'programmes': [],
    'souvenirItems': [],
}


# Fingerprints identify only the unchanged demonstration entries from the old
# release. Admin-created and edited entries are preserved during migration.
LEGACY_SAMPLE_FINGERPRINTS = {'programmes': ['4dd61c3c4a14efd120ae63046995968d36f9a7102c840ee9033c1c079d3d6b11', '473e1ef9f0bbe2d437a0518f041bef533ca806ca461ecb434249084440d9c163', '545ac7ce189fc6444c59b8d13b2a14f2f4a6ac97d49efcd5cf9bc13aae62b89e', 'a2ff80ceb2f52767683bc85a36435777247c0a495aac1e2b1a36b043c7ad06f4', '5e20663301011ce43956cb531231e0b0622a12e8129447478c1a7fd403813a96'], 'souvenirItems': ['4b76714bbd79cf97f4c8abc484abb148416ed05bc3e064405741596485931309', '282705e16268899d2124c0052c9ae2f2672ba3a3940099d28c69a908b61a901b', 'f59cc0c6debf8a1c3209133b31efd78317d6fd02085e620a4d68f398dd1a707b', '434955789b823bec6dde470ec92de4a203f766c69b0af72a6522cf2f40c93574']}


def read_settings(database):
    database.settings.update_one({'_id': 'config'}, {'$setOnInsert': {
        'config': deepcopy(DEFAULTS), 'version': 1, 'sample_cleanup_done': True,
    }}, upsert=True)
    while True:
        document = database.settings.find_one({'_id': 'config'})
        if document.get('sample_cleanup_done'):
            return document
        config = deepcopy(document['config'])
        for field, fingerprints in LEGACY_SAMPLE_FINGERPRINTS.items():
            config[field] = [item for item in config.get(field, []) if
                hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                not in fingerprints]
        result = database.settings.update_one({
            '_id': 'config', 'version': document['version'],
            'sample_cleanup_done': {'$ne': True},
        }, {'$set': {'config': config, 'sample_cleanup_done': True}, '$inc': {'version': 1}})
        if result.modified_count:
            return database.settings.find_one({'_id': 'config'})


def validate_settings(config):
    if not isinstance(config, dict) or set(config) != set(DEFAULTS):
        raise ValueError('Please provide all settings fields.')
    result = deepcopy(config)
    for field in ['institutionName', 'departmentName', 'academicYear', 'indexPrefix', 'indexYear']:
        value = result[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 150:
            raise ValueError(f'Invalid {field}.')
        result[field] = value.strip()
    if not re.fullmatch(r'[0-9]{4}/[0-9]{4}', result['academicYear']):
        raise ValueError('Academic year must use YYYY/YYYY.')
    first, second = map(int, result['academicYear'].split('/'))
    if second != first + 1:
        raise ValueError('Academic year must contain consecutive years.')
    for field, low, high in [('nextSequence', 1, 999999999), ('sessionMinutes', 15, 43200), ('passwordMinLength', 4, 128)]:
        if type(result[field]) is not int or not low <= result[field] <= high:
            raise ValueError(f'{field} must be between {low} and {high}.')
    for field, label_field in [('programmes', 'name'), ('souvenirItems', 'label')]:
        items = result[field]
        if not isinstance(items, list) or len(items) > 200:
            raise ValueError('Provide at most 200 items per list.')
        ids, labels = set(), set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Invalid configuration item.')
            for key in ['id', label_field] + (['qty'] if field == 'souvenirItems' else []):
                value = item.get(key)
                if not isinstance(value, str) or not value.strip() or len(value) > 150:
                    raise ValueError(f'Invalid item {key}.')
                item[key] = value.strip()
            if item['id'] in ids or item[label_field].lower() in labels:
                raise ValueError('Duplicate item name or ID.')
            ids.add(item['id']); labels.add(item[label_field].lower())
            if field == 'programmes':
                if 'type' in item and item['type'] not in ('BSc', 'HND', 'Diploma', 'Certificate'):
                    raise ValueError('Invalid programme type.')
                fee = item.get('fee')
                if type(fee) not in (int, float) or not math.isfinite(fee) or not 0 < fee <= 10000000:
                    raise ValueError('Programme fees must be positive valid amounts.')
            elif item.get('icon') not in ('book', 'shirt', 'award', 'usb', 'gift'):
                raise ValueError('Invalid souvenir icon.')
    return result
