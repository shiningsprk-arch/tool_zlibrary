# -*- coding: utf-8 -*-
"""账号密码的本地加密存储：AES-GCM，密钥与工具配置分文件保存。"""
import base64
import os
import secrets
import threading

_KEY_BYTES = 32
_NONCE_BYTES = 12
_PREFIX = "v1:"
_AAD = b"mybooks-zlibrary-account-password-v1"
_KEY_LOCK = threading.RLock()


class CredentialStoreError(Exception):
    pass


def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _load_key(path):
    with _KEY_LOCK:
        try:
            with open(path, "rb") as handle:
                raw = handle.read().strip()
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except FileNotFoundError:
            key = secrets.token_bytes(_KEY_BYTES)
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            try:
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return _load_key(path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_b64encode(key).encode("ascii"))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return key
    try:
        key = _b64decode(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as err:
        raise CredentialStoreError("invalid credential key file") from err
    if len(key) != _KEY_BYTES:
        raise CredentialStoreError("invalid credential key length")
    return key


def _aesgcm_encrypt(key, nonce, plaintext):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).encrypt(nonce, plaintext, _AAD)
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES
    except ImportError as err:
        raise CredentialStoreError("AES-GCM provider unavailable") from err
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(_AAD)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return ciphertext + tag


def _aesgcm_decrypt(key, nonce, blob):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        AESGCM = None
    if AESGCM is not None:
        try:
            return AESGCM(key).decrypt(nonce, blob, _AAD)
        except Exception as err:
            raise CredentialStoreError("invalid encrypted password") from err
    from Crypto.Cipher import AES
    ciphertext, tag = blob[:-16], blob[-16:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(_AAD)
    try:
        return cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as err:
        raise CredentialStoreError("invalid encrypted password") from err


def encrypt_password(password, key_path):
    if not password:
        raise CredentialStoreError("empty password")
    key = _load_key(key_path)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    try:
        blob = _aesgcm_encrypt(key, nonce, password.encode("utf-8"))
    except Exception as err:
        if isinstance(err, CredentialStoreError):
            raise
        raise CredentialStoreError("failed to encrypt password") from err
    return _PREFIX + _b64encode(nonce + blob)


def decrypt_password(token, key_path):
    token = str(token or "")
    if not token:
        return ""
    if not token.startswith(_PREFIX):
        raise CredentialStoreError("unsupported credential token")
    raw = _b64decode(token[len(_PREFIX):])
    if len(raw) <= _NONCE_BYTES:
        raise CredentialStoreError("invalid credential token")
    key = _load_key(key_path)
    plaintext = _aesgcm_decrypt(
        key, raw[:_NONCE_BYTES], raw[_NONCE_BYTES:])
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as err:
        raise CredentialStoreError("invalid credential text") from err
