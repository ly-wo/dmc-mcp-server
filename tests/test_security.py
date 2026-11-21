import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dmc_mcp_server.client import DMCClient, InstanceCredentials, InstanceSession
from dmc_mcp_server.credentials import load_credentials
from dmc_mcp_server.sql_policy import validate_read_only
from dmc_mcp_server import main


class SQLPolicyTests(unittest.TestCase):
    def test_normal_queries(self):
        for sql in (
            'SELECT 1;', 'SELECT COUNT(*), MAX(id) FROM events WHERE status = 3',
            'WITH x AS (SELECT 1 AS id) SELECT id FROM x',
            'SELECT a.id FROM a JOIN b ON a.id=b.id ORDER BY a.id LIMIT 10',
            'SELECT 1 UNION ALL SELECT 2',
            "SELECT * FROM t WHERE a=1 AND (b=2 OR c=3)",
            "SELECT 'a''b', CASE WHEN x=1 THEN 2 ELSE 3 END FROM t",
            "SELECT NOW(), DATE_FORMAT(NOW(), '%Y'), COALESCE(NULL, 1)",
            "SELECT '; DELETE FROM t' AS harmless_string",
            'SELECT 1 /* ordinary comment */',
        ):
            with self.subTest(sql=sql):
                self.assertTrue(validate_read_only(sql))

    def test_unsafe_queries(self):
        for sql in (
            '', 'DELETE FROM t', 'SELECT 1; UPDATE t SET a=1',
            'SELECT 1; SELECT 2', 'SELECT 1;;',
            "SELECT 'x' INTO OUTFILE '/tmp/x'", "SELECT 1 INTO DUMPFILE '/tmp/x'",
            'SELECT * FROM t FOR UPDATE', 'SELECT * FROM t LOCK IN SHARE MODE',
            "SELECT GET_LOCK('x', 60)", "SELECT RELEASE_LOCK('x')",
            "SELECT LOAD_FILE('/etc/passwd')", 'SELECT SLEEP(100)',
            'SELECT BENCHMARK(1000000, 1)', 'SELECT custom_function()',
            'SELECT db.abs(1)', 'SELECT `abs`(1)',
            'SELECT @x:=1', 'SELECT @@global.version',
            'SELECT 1 /*!50000 INTO OUTFILE "/tmp/x" */',
            'SELECT 1 /*M! INTO OUTFILE "/tmp/x" */',
            'WITH x AS (DELETE FROM t RETURNING id) SELECT * FROM x',
            "SELECT 'back\\slash'", 'SELECT /*+ SET_VAR(x=1) */ SLEEP(1)',
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    validate_read_only(sql)

    def test_validation_precedes_session_and_network(self):
        client = DMCClient('fake')
        self.addCleanup(client._http.close)
        with patch.object(client, '_get_valid_session') as session:
            with self.assertRaises(ValueError):
                client.execute_sql('fixture', 'SELECT 1; DELETE FROM t', 'db')
            session.assert_not_called()

    def test_metadata_uses_same_boundary(self):
        client = DMCClient('fake')
        self.addCleanup(client._http.close)
        with patch.object(client, '_get_valid_session') as session:
            for method, args in (
                (client.list_tables, ('fixture', "db' OR 1=1 --")),
                (client.list_tables, ('fixture', 'db', "\\' OR 1=1 --")),
                (client.get_table_detail, ('fixture', 'db', 't`; DROP TABLE t; --')),
            ):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    method(*args)
            session.assert_not_called()
        self.assertEqual(validate_read_only('SHOW DATABASES', allow_metadata=True), 'SHOW DATABASES')
        self.assertEqual(validate_read_only('SHOW CREATE TABLE `db`.`t`', allow_metadata=True), 'SHOW CREATE TABLE `db`.`t`')
        with self.assertRaises(ValueError):
            validate_read_only('SHOW PROCESSLIST', allow_metadata=True)

    def test_http_payload_contains_only_checked_sql(self):
        client = DMCClient('fake')
        self.addCleanup(client._http.close)
        creds = InstanceCredentials('fixture', 'readonly', 'fake-password')
        client._sessions['fixture'] = InstanceSession(creds, 'fake-token')
        response = Mock()
        response.json.return_value = {'code': 0, 'data': {'items': []}}
        with patch.object(client._http, 'post', return_value=response) as post:
            client.execute_sql('fixture', 'SELECT 1 /* comment */', 'db')
            self.assertEqual(post.call_args.kwargs['json']['sql'], 'SELECT 1')
            client.get_table_detail('fixture', 'db', 't', include_ddl=True)
            self.assertEqual(post.call_args.kwargs['json']['sql'], 'SHOW CREATE TABLE `db`.`t`')


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.client = DMCClient('old-cookie', 123)
        self.addCleanup(self.client._http.close)
        self.creds = InstanceCredentials('fixture', 'admin', 'old-password')
        self.old = InstanceSession(self.creds, 'old-secret-token')
        self.client._sessions['fixture'] = self.old
        self.client._creds_registry['fixture'] = self.creds

    def test_same_identity_reuses_session(self):
        with patch.object(self.client, '_ping', return_value=True), patch.object(self.client, '_do_login') as login:
            self.assertIs(self.client.ensure_login('fixture', 'admin', 'old-password'), self.old)
            login.assert_not_called()

    def test_changed_identity_never_reuses_old_session(self):
        for kwargs in ({'user': 'readonly'}, {'password': 'new-password'}, {'region_id': 7}, {'db_type': 'tdsql'}):
            with self.subTest(kwargs=kwargs):
                self.client._sessions['fixture'] = self.old
                args = dict(instance_id='fixture', user='admin', password='old-password')
                args.update(kwargs)
                with patch.object(self.client, '_do_login', return_value='new-session') as login, patch.object(self.client, '_ping') as ping:
                    self.assertEqual(self.client.ensure_login(**args), 'new-session')
                    login.assert_called_once()
                    ping.assert_not_called()
                    self.assertNotIn('fixture', self.client._sessions)
                    self.assertNotIn('fixture', self.client._creds_registry)

    def test_failed_account_switch_cannot_fall_back(self):
        with patch.object(self.client, '_do_login', side_effect=RuntimeError('login failed')):
            with self.assertRaises(RuntimeError):
                self.client.ensure_login('fixture', 'readonly', 'new-password')
        self.assertFalse(self.client.active_instances)
        with self.assertRaises(RuntimeError):
            self.client._get_valid_session('fixture')

    def test_cookie_change_invalidates_sessions_and_credentials(self):
        self.client.update_cookie('new-cookie', 0)
        self.assertEqual(self.client._mc_gtk, 0)
        self.assertFalse(self.client._sessions)
        self.assertFalse(self.client._creds_registry)

    def test_secret_repr(self):
        self.assertNotIn('old-password', repr(self.creds))
        self.assertNotIn('old-secret-token', repr(self.old))


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'credentials.json'
        self.path.write_text(json.dumps({'cookie': 'fake-cookie', 'mc_gtk': 123, 'profiles': {
            'readonly': {'instance_id': 'fixture', 'user': 'readonly', 'password': 'fake-password'}
        }}))
        self.path.chmod(0o600)
        self.env = patch.dict(os.environ, {'DMC_CREDENTIALS_FILE': str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_private_local_credentials(self):
        cookie, csrf, profiles = load_credentials()
        self.assertEqual(cookie, 'fake-cookie')
        self.assertEqual(profiles['readonly'].user, 'readonly')

    def test_public_file_and_symlink_are_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_credentials()
        link = self.path.parent / 'link.json'
        link.symlink_to(self.path)
        with patch.dict(os.environ, {'DMC_CREDENTIALS_FILE': str(link)}), self.assertRaises(OSError):
            load_credentials()

    def test_tool_schema_has_no_secret_input(self):
        tools = asyncio.run(main.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertNotIn('set_cookie', names)
        for tool in tools:
            self.assertFalse({'password', 'cookie', 'token'} & set(tool.inputSchema.get('properties', {})))

    def test_failed_reload_revokes_old_state(self):
        with patch.object(main, '_client', DMCClient('fake')), patch.object(main, '_profiles', {'old': object()}):
            self.path.chmod(0o644)
            with self.assertRaises(ValueError):
                main.reload_credentials()
            self.assertIsNone(main._client)
            self.assertFalse(main._profiles)

    def test_login_output_omits_token(self):
        fake = Mock()
        fake.ensure_login.return_value = InstanceSession(InstanceCredentials('fixture', 'ro', 'fake'), 'SECRET_TOKEN')
        with patch.object(main, '_get_client', return_value=fake), patch.object(main, '_profiles', {'ro': InstanceCredentials('fixture', 'ro', 'fake')}):
            result = main.login_instance('ro')
            self.assertNotIn('SECRET_TOKEN', result)
            self.assertNotIn('Token', result)


if __name__ == '__main__':
    unittest.main()
