"""Compatibility checks against CPython's native password derivation."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloudflare import crypto_compat


class Pbkdf2CompatibilityTests(unittest.TestCase):
    def test_matches_native_password_hashes_without_reducing_iterations(self):
        cases = [
            (b'password', b'salt', 1, None),
            (b'password', b'salt', 2, None),
            (b'password\x00bytes', b'salt\x00bytes', 1000, 77),
            ('Staff password \u0e44\u0e17\u0e22 \U0001f511'.encode(), bytes.fromhex('ab' * 16), 350000, None),
        ]
        for password, salt, iterations, dklen in cases:
            with self.subTest(iterations=iterations, dklen=dklen):
                self.assertEqual(
                    crypto_compat.pbkdf2_hmac('sha256', password, salt, iterations, dklen),
                    hashlib.pbkdf2_hmac('sha256', password, salt, iterations, dklen),
                )

    def test_handles_empty_values_long_password_and_truncated_output(self):
        for password, salt, dklen in [(b'', b'', 1), (b'long-password' * 20, b'salt', 33)]:
            with self.subTest(dklen=dklen):
                self.assertEqual(
                    crypto_compat.pbkdf2_hmac('sha256', password, salt, 2, dklen),
                    hashlib.pbkdf2_hmac('sha256', password, salt, 2, dklen),
                )

    def test_accepts_bytearray_and_memoryview_inputs(self):
        actual = crypto_compat.pbkdf2_hmac('sha256', bytearray(b'password'), memoryview(b'salt'), 2)
        self.assertEqual(actual, hashlib.pbkdf2_hmac('sha256', b'password', b'salt', 2))

    def test_accepts_sha256_hash_name_aliases(self):
        for name in ('SHA256', 'sha-256'):
            with self.subTest(name=name):
                self.assertEqual(
                    crypto_compat.pbkdf2_hmac(name, b'password', b'salt', 2),
                    hashlib.pbkdf2_hmac(name, b'password', b'salt', 2),
                )

    def test_rejects_non_string_hash_name_and_overflowing_parameters(self):
        with self.assertRaises(TypeError):
            crypto_compat.pbkdf2_hmac(None, b'password', b'salt', 1)
        for iterations, dklen in [(2147483648, 32), (1, 2147483648)]:
            with self.subTest(iterations=iterations, dklen=dklen), self.assertRaises(OverflowError):
                crypto_compat.pbkdf2_hmac('sha256', b'password', b'salt', iterations, dklen)

    def test_rejects_invalid_iterations_output_lengths_and_hashes(self):
        for iterations, dklen in [(0, None), (-1, None), (1, 0), (1, -1)]:
            with self.subTest(iterations=iterations, dklen=dklen), self.assertRaises(ValueError):
                crypto_compat.pbkdf2_hmac('sha256', b'password', b'salt', iterations, dklen)
        with self.assertRaises(ValueError):
            crypto_compat.pbkdf2_hmac('sha1', b'password', b'salt', 1)

    def test_rejects_non_byte_password_and_non_integer_parameters(self):
        for password, salt, iterations, dklen in [
            ('password', b'salt', 1, None),
            (b'password', 'salt', 1, None),
            (b'password', b'salt', 1.5, None),
            (b'password', b'salt', 1, 2.5),
        ]:
            with self.subTest(iterations=iterations, dklen=dklen), self.assertRaises(TypeError):
                crypto_compat.pbkdf2_hmac('sha256', password, salt, iterations, dklen)

    def test_install_preserves_native_implementation(self):
        native = hashlib.pbkdf2_hmac
        crypto_compat.install()
        self.assertIs(hashlib.pbkdf2_hmac, native)

    def test_install_adds_fallback_only_when_native_is_absent(self):
        native = hashlib.pbkdf2_hmac
        expected = native('sha256', b'password', b'salt', 2)
        try:
            del hashlib.pbkdf2_hmac
            crypto_compat.install()
            self.assertIs(hashlib.pbkdf2_hmac, crypto_compat.pbkdf2_hmac)
            self.assertEqual(hashlib.pbkdf2_hmac('sha256', b'password', b'salt', 2), expected)
            crypto_compat.install()
            self.assertIs(hashlib.pbkdf2_hmac, crypto_compat.pbkdf2_hmac)
        finally:
            hashlib.pbkdf2_hmac = native


if __name__ == '__main__':
    unittest.main()
