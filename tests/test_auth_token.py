import unittest
from unittest.mock import patch
import json
import base64
import time
from auth_token import create_auth_token, verify_auth_token

class TestAuthToken(unittest.TestCase):
    # Sample key pair
    public_key = "4852b69364572b52efa1b6bb3e6d0abed4f389a1cbfbb60a9bba2cce649caf0e"
    private_key = "18469d6140447f77de13cd8d761e605431f52269fbff43b0925752ed9e6745435dc6a86d2568af8b70d3365db3f88234760c8ecc645ce469829bc45b65f1d5d5"
    fixed_now = 1776541102  # Captured from previous run
    reference_token = "eyJhbGciOiJFZDI1NTE5IiwidHlwIjoiSldUIn0.eyJwdWJsaWNLZXkiOiI0ODUyQjY5MzY0NTcyQjUyRUZBMUI2QkIzRTZEMEFCRUQ0RjM4OUExQ0JGQkI2MEE5QkJBMkNDRTY0OUNBRjBFIiwiaWF0IjoxNzc2NTQxMTAyLCJleHAiOjE3NzY1NDQ3MDIsInN1YiI6InRlc3QtdXNlciJ9.ADEC2AD6CD692EE266CDD83956104ADA1F742CDD19E60C3CA0A7860BC26683C8F8FB47958BB13FC82DBCF84F76B06E644D69BC715497FD1457B012534072700A"

    @patch('time.time')
    def test_reference_token_verification(self, mock_time):
        mock_time.return_value = self.fixed_now
        # Verify the reference token captured from meshcore-decoder,
        # to show it verifies with our implementation as well.
        try:
            payload = verify_auth_token(self.reference_token, self.public_key)
            self.assertEqual(payload['sub'], "test-user")
            self.assertEqual(payload['publicKey'], self.public_key.upper())
            self.assertEqual(payload['iat'], self.fixed_now)
        except Exception as e:
            self.fail(f"Reference token verification failed: {e}")

    @patch('time.time')
    def test_native_implementation_matches_reference(self, mock_time):
        mock_time.return_value = self.fixed_now
        try:
            native_token = create_auth_token(self.public_key, self.private_key, expiry_seconds=3600, sub="test-user")
            self.assertEqual(native_token, self.reference_token)
        except Exception as e:
            self.fail(f"Native token generation failed: {e}")

    def test_native_token_with_custom_claims(self):
        # Create token with custom claims
        claims = {
            "aud": "mqtt.example.com",
            "role": "admin",
            "permissions": ["read", "write"]
        }
        token = create_auth_token(self.public_key, self.private_key, **claims)

        # Verify it
        payload = verify_auth_token(token, self.public_key)
        self.assertEqual(payload['aud'], "mqtt.example.com")
        self.assertEqual(payload['role'], "admin")
        self.assertEqual(payload['permissions'], ["read", "write"])

    @patch('time.time')
    def test_token_expiration(self, mock_time):
        mock_time.return_value = self.fixed_now

        # Create token that expires in 10 seconds
        token = create_auth_token(self.public_key, self.private_key, expiry_seconds=10)

        # Verify it now (should be valid)
        payload = verify_auth_token(token, self.public_key)
        self.assertIsNotNone(payload)

        # Advance time by 11 seconds
        mock_time.return_value = self.fixed_now + 11

        # Verify it again (should fail)
        with self.assertRaisesRegex(Exception, "Token has expired"):
            verify_auth_token(token, self.public_key)

    def test_tampered_token_fails_verification(self):
        token = create_auth_token(self.public_key, self.private_key, sub="test-user")
        parts = token.split('.')

        # 1. Tamper with payload
        payload_dict = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
        payload_dict['sub'] = 'attacker'
        tampered_payload = base64.urlsafe_b64encode(json.dumps(payload_dict, separators=(',', ':')).encode()).decode().rstrip('=')
        tampered_token_1 = f"{parts[0]}.{tampered_payload}.{parts[2]}"

        with self.assertRaisesRegex(Exception, "Invalid signature"):
            verify_auth_token(tampered_token_1, self.public_key)

        # 2. Tamper with signature
        tampered_sig = parts[2][:-4] + "0000"
        tampered_token_2 = f"{parts[0]}.{parts[1]}.{tampered_sig}"

        with self.assertRaisesRegex(Exception, "Invalid signature"):
            verify_auth_token(tampered_token_2, self.public_key)

    def test_wrong_public_key_fails_verification(self):
        token = create_auth_token(self.public_key, self.private_key, sub="test-user")

        # Use a different public key for verification
        wrong_pubkey = "00" * 32
        with self.assertRaisesRegex(Exception, "Token public key does not match expected public key"):
            verify_auth_token(token, wrong_pubkey)

if __name__ == '__main__':
    unittest.main()
