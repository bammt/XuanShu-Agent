import base64, hashlib
from cryptography.fernet import Fernet
from .config import settings

def _fernet():
    key=base64.urlsafe_b64encode(hashlib.sha256(settings.encryption_key.encode()).digest())
    return Fernet(key)
def encrypt_secret(value:str)->str: return _fernet().encrypt(value.encode()).decode() if value else ''
def decrypt_secret(value:str)->str: return _fernet().decrypt(value.encode()).decode() if value else ''
