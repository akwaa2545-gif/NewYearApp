"""PBKDF2-SHA256 for Python Workers lacking hashlib's native implementation.

The fallback follows PBKDF2's HMAC block derivation and preserves the caller's
iteration count, so existing password hashes require no migration.
"""
import hashlib
import hmac
import operator


def _derive_block(prf, salt, iterations, block_number):
    copy_prf = prf.copy
    from_bytes = int.from_bytes
    first = copy_prf()
    first.update(salt + block_number.to_bytes(4, 'big'))
    previous = first.digest()
    combined = from_bytes(previous, 'big')
    for _ in range(1, iterations):
        current = copy_prf()
        current.update(previous)
        previous = current.digest()
        combined ^= from_bytes(previous, 'big')
    return combined.to_bytes(32, 'big')


def pbkdf2_hmac(hash_name, password, salt, iterations, dklen=None):
    """Derive bytes with hashlib's PBKDF2 call shape, supporting SHA256 only."""
    if not isinstance(hash_name, str):
        raise TypeError('hash_name must be a string')
    if hash_name.lower() not in ('sha256', 'sha-256'):
        raise ValueError('Only SHA256 is supported by the PBKDF2 fallback.')
    iterations = operator.index(iterations)
    dklen = 32 if dklen is None else operator.index(dklen)
    if iterations <= 0:
        raise ValueError('iteration value must be greater than 0.')
    if dklen <= 0:
        raise ValueError('key length must be greater than 0.')
    if iterations > 2147483647 or dklen > 2147483647:
        raise OverflowError('iteration value or key length is too large.')
    password = memoryview(password).tobytes()
    salt = memoryview(salt).tobytes()
    prf = hmac.new(password, digestmod=hashlib.sha256)
    block_count = (dklen + 31) // 32
    return b''.join(
        _derive_block(prf, salt, iterations, block_number)
        for block_number in range(1, block_count + 1)
    )[:dklen]


def install():
    """Provide the fallback only when the runtime omits native PBKDF2."""
    if not hasattr(hashlib, 'pbkdf2_hmac'):
        hashlib.pbkdf2_hmac = pbkdf2_hmac
