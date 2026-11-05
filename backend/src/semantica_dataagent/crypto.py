"""Encryption for provider secrets stored in the application database."""

import os

from cryptography.fernet import Fernet


def encrypt(plaintext: str) -> str:
    cipher = Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
    return cipher.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    cipher = Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
    return cipher.decrypt(ciphertext.encode()).decode()
