"""Dedicated MongoDB connection for IST, separate from Speedlink storage."""
import os

from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import ConfigurationError, PyMongoError

uri = os.environ.get('IST_MONGODB_URI', '').strip()
if not uri:
    raise RuntimeError('Set IST_MONGODB_URI to the IST MongoDB Atlas connection string.')

try:
    client = MongoClient(
        uri,
        server_api=ServerApi('1'),
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=10000,
    )
except ConfigurationError:
    raise RuntimeError('IST MongoDB configuration failed. Check IST_MONGODB_URI.') from None

db = client['kingollies']


def verify_connection():
    """Check connectivity explicitly without exposing credentials in errors."""
    try:
        client.admin.command('ping')
    except PyMongoError:
        raise RuntimeError('IST MongoDB connection failed. Check Atlas access and credentials.') from None


if __name__ == '__main__':
    verify_connection()
    print('Successfully connected to IST MongoDB!')
