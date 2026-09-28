import asyncio
import hashlib
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import database
from app.api.v1 import auth_info, city_access_fast, login
from app.core import auth
from app.models.auth import AuthenticatedUser


class EmptyQueries:
    def table(self, *args):
        return self

    def select(self, *args):
        return self

    def eq(self, *args):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class AuthSecurityTests(unittest.TestCase):
    def setUp(self):
        auth.clear_auth_cache()
        self.configuration = patch.multiple(
            auth.settings, supabase_url=None, supabase_service_role_key=None
        )
        self.configuration.start()
        self.addCleanup(self.configuration.stop)
        self.addCleanup(auth.clear_auth_cache)
        self.endpoint_calls = 0
        app = FastAPI()

        @app.get('/private')
        async def private(user: AuthenticatedUser = Depends(auth.authenticate_request)):
            self.endpoint_calls += 1
            return {'tenant_id': user.tenant_id}

        app.include_router(login.router, prefix='/api/v1')
        app.include_router(auth_info.router, prefix='/api/v1')
        app.include_router(city_access_fast.router, prefix='/api/v1')
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def claims(self, **overrides):
        payload = {
            'id': 'user-candidate',
            'email': 'candidate@propertyflow.com',
            'app_metadata': {'role': 'user', 'tenant_id': 'tenant-a'},
            'aud': 'authenticated',
            'exp': int(time.time()) + 3600,
        }
        payload.update(overrides)
        return payload

    def token(self, **overrides):
        return jwt.encode(self.claims(**overrides), auth.settings.secret_key, algorithm='HS256')

    def request(self, token, path='/private', **headers):
        return self.client.get(path, headers={'Authorization': f'Bearer {token}', **headers})

    def remote_client(self, tenant_id='tenant-b'):
        user = SimpleNamespace(
            id='remote-user', email='remote@example.com',
            app_metadata={'role': 'user', 'tenant_id': tenant_id},
            user_metadata={'tenant_id': 'tenant-a'},
        )
        client = SimpleNamespace(
            auth=SimpleNamespace(get_user=Mock(return_value=SimpleNamespace(user=user))),
            service=EmptyQueries(),
        )
        return client, user

    def test_missing_auth_is_rejected_before_endpoint(self):
        response = self.client.get('/private')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.endpoint_calls, 0)

    def test_invalid_static_unsigned_and_expired_tokens_are_rejected(self):
        tokens = [
            'not-a-token',
            'mock-token-123',
            jwt.encode(self.claims(), '', algorithm='none'),
            jwt.encode(self.claims(), 'wrong-secret', algorithm='HS256'),
            self.token(exp=int(time.time()) - 60),
            self.token(aud='someone-else'),
        ]
        for token in tokens:
            with self.subTest(token=token[:15]):
                self.assertEqual(self.request(token).status_code, 401)
        self.assertEqual(self.endpoint_calls, 0)

    def test_missing_expiry_is_rejected(self):
        payload = self.claims()
        del payload['exp']
        token = jwt.encode(payload, auth.settings.secret_key, algorithm='HS256')
        self.assertEqual(self.request(token).status_code, 401)
        self.assertEqual(self.endpoint_calls, 0)

    def test_missing_or_user_editable_tenant_is_rejected(self):
        for metadata in ({}, {'tenant_id': 'tenant-a'}):
            with self.subTest(user_metadata=metadata):
                token = self.token(app_metadata={}, user_metadata=metadata)
                self.assertEqual(self.request(token).status_code, 403)
        self.assertEqual(self.endpoint_calls, 0)

    def test_invalid_tenant_identity_is_rejected(self):
        for tenant in (None, '', ' ', ' tenant-a', 1, [], {}):
            with self.subTest(tenant=tenant):
                token = self.token(app_metadata={'tenant_id': tenant})
                self.assertEqual(self.request(token).status_code, 403)
        self.assertEqual(self.endpoint_calls, 0)

    def test_authoritative_tenant_ignores_email_metadata_and_header(self):
        token = self.token(
            email='sunset@propertyflow.com',
            app_metadata={'tenant_id': 'tenant-b'},
            user_metadata={'tenant_id': 'tenant-a'},
        )
        response = self.request(token, **{'X-Tenant-ID': 'tenant-a'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tenant_id'], 'tenant-b')

    def test_signed_root_tenant_claim_is_supported(self):
        response = self.request(self.token(app_metadata={}, tenant_id='tenant-b'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tenant_id'], 'tenant-b')

    def test_auth_me_reuses_authenticated_tenant(self):
        token = self.token(email='sunset@propertyflow.com', app_metadata={'tenant_id': 'tenant-b'})
        response = self.request(token, path='/api/v1/auth/me')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tenant_id'], 'tenant-b')

    def test_city_access_reuses_authenticated_tenant(self):
        cache = SimpleNamespace(get_city_access=AsyncMock(return_value=['test-city']))
        with patch.object(city_access_fast, 'tenant_cache', cache):
            response = self.request(self.token(app_metadata={'tenant_id': 'tenant-b'}), path='/api/v1/fast/city-access')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tenant_id'], 'tenant-b')
        self.assertEqual(response.json()['cities'], ['test-city'])
        cache.get_city_access.assert_awaited_once_with('tenant-b', 'user-candidate')

    def test_websocket_uses_verified_local_app_and_root_claims(self):
        for claims in (
            {'app_metadata': {'tenant_id': 'tenant-b'}, 'user_metadata': {'tenant_id': 'tenant-a'}},
            {'app_metadata': {}, 'tenant_id': 'tenant-b'},
        ):
            with self.subTest(claims=claims):
                user = asyncio.run(auth.verify_token_ws(self.token(**claims)))
                self.assertIsNotNone(user)
                self.assertEqual(user.tenant_id, 'tenant-b')

    def test_websocket_rejects_missing_and_user_editable_tenant(self):
        for claims in ({'app_metadata': {}}, {'app_metadata': {}, 'user_metadata': {'tenant_id': 'tenant-a'}}):
            with self.subTest(claims=claims):
                self.assertIsNone(asyncio.run(auth.verify_token_ws(self.token(**claims))))

    def test_websocket_uses_verified_supabase_app_metadata(self):
        remote, _ = self.remote_client()
        token = jwt.encode(self.claims(), 'remote-secret', algorithm='HS256')
        with patch.object(auth, 'supabase', remote):
            user = asyncio.run(auth.verify_token_ws(token))
        self.assertIsNotNone(user)
        self.assertEqual(user.tenant_id, 'tenant-b')
        remote.auth.get_user.assert_called_once_with(token)

    def test_websocket_rejects_invalid_and_expired_local_tokens(self):
        for token in (
            'mock-token-123', jwt.encode(self.claims(), '', algorithm='none'),
            jwt.encode(self.claims(), 'wrong-secret', algorithm='HS256'),
            self.token(exp=int(time.time()) - 60),
        ):
            with self.subTest(token=token[:15]):
                self.assertIsNone(asyncio.run(auth.verify_token_ws(token)))

    def test_warm_auth_cache_cannot_outlive_token(self):
        expires_at = int(time.time()) + 60
        token = self.token(exp=expires_at)
        self.assertEqual(self.request(token).status_code, 200)
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        self.assertEqual(auth.auth_cache[token_hash]['expires_at'], expires_at)
        with patch('app.core.auth.datetime') as clock:
            clock.now.return_value = datetime.fromtimestamp(expires_at + 1)
            self.assertEqual(self.request(token).status_code, 401)
        self.assertEqual(self.endpoint_calls, 1)
        self.assertNotIn(token_hash, auth.auth_cache)

    def test_configured_supabase_fallback_uses_verified_user_metadata(self):
        remote, _ = self.remote_client()
        token = jwt.encode(self.claims(), 'remote-secret', algorithm='HS256')
        with patch.multiple(auth.settings, supabase_url='https://auth.example', supabase_service_role_key='test'), \
                patch.object(auth, 'supabase', remote):
            response = self.request(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tenant_id'], 'tenant-b')
        remote.auth.get_user.assert_called_once_with(token)

    def test_failed_supabase_verification_is_rejected(self):
        remote, _ = self.remote_client()
        remote.auth.get_user.side_effect = RuntimeError('invalid token')
        token = jwt.encode(self.claims(), 'remote-secret', algorithm='HS256')
        with patch.multiple(auth.settings, supabase_url='https://auth.example', supabase_service_role_key='test'), \
                patch.object(auth, 'supabase', remote):
            self.assertEqual(self.request(token).status_code, 401)
        self.assertEqual(self.endpoint_calls, 0)

    def test_challenge_auth_rejects_unsigned_static_wrong_signature_and_expired(self):
        for token in (
            'mock-token-123', jwt.encode(self.claims(), '', algorithm='none'),
            jwt.encode(self.claims(), 'wrong-secret', algorithm='HS256'),
            self.token(exp=int(time.time()) - 60),
        ):
            with self.subTest(token=token[:15]):
                self.assertIsNone(database.supabase.auth.get_user(token).user)

    def test_supplied_accounts_still_login_and_authenticate(self):
        for email, password, tenant in (
            ('sunset@propertyflow.com', 'client_a_2024', 'tenant-a'),
            ('ocean@propertyflow.com', 'client_b_2024', 'tenant-b'),
        ):
            with self.subTest(email=email):
                response = self.client.post('/api/v1/auth/login', json={'email': email, 'password': password})
                self.assertEqual(response.status_code, 200)
                identity = self.request(response.json()['access_token'])
                self.assertEqual(identity.status_code, 200)
                self.assertEqual(identity.json()['tenant_id'], tenant)

    def test_wrong_password_and_unconfigured_accounts_are_rejected(self):
        for email in ('sunset@propertyflow.com', 'ocean@propertyflow.com', 'candidate@propertyflow.com'):
            with self.subTest(email=email):
                response = self.client.post('/api/v1/auth/login', json={'email': email, 'password': 'wrong'})
                self.assertEqual(response.status_code, 401)

    def test_configured_login_verifies_password_and_returns_verified_session(self):
        remote, user = self.remote_client()
        remote.auth.sign_in_with_password = Mock(return_value=SimpleNamespace(
            user=user, session=SimpleNamespace(access_token='verified-session-token')
        ))
        credentials = {'email': 'remote@example.com', 'password': 'password'}
        with patch.multiple(auth.settings, supabase_url='https://auth.example', supabase_service_role_key='test'), \
                patch.object(login, 'supabase', remote):
            response = self.client.post('/api/v1/auth/login', json=credentials)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['access_token'], 'verified-session-token')
        self.assertEqual(response.json()['user']['tenant_id'], 'tenant-b')
        remote.auth.sign_in_with_password.assert_called_once_with(credentials)

    def test_configured_login_rejects_failed_password_verification(self):
        remote, _ = self.remote_client()
        remote.auth.sign_in_with_password = Mock(side_effect=RuntimeError('invalid credentials'))
        with patch.multiple(auth.settings, supabase_url='https://auth.example', supabase_service_role_key='test'), \
                patch.object(login, 'supabase', remote):
            response = self.client.post('/api/v1/auth/login', json={'email': 'remote@example.com', 'password': 'wrong'})
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
