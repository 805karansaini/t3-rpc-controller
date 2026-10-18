"""Regression checks for CLI usage and local configuration failures."""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from t3_controller.cli import build_parser, main, selected_environments
from t3_controller.state import EnvironmentStore, normalize_url, write_private
from t3_controller.transport import websocket_url


class CliTests(unittest.TestCase):
    def test_nonfinite_timeouts_and_poll_intervals_are_usage_errors(self):
        for option in ('--timeout', '--poll-interval'):
            for value in ('nan', 'inf', '-inf'):
                with self.subTest(option=option, value=value):
                    output = io.StringIO()
                    with redirect_stderr(output), self.assertRaises(SystemExit) as caught:
                        main(['thread', 'wait', 'mac', 'thread-id', f'{option}={value}'])
                    self.assertEqual(caught.exception.code, 2)
                    self.assertEqual(json.loads(output.getvalue())['error']['category'], 'usage')

    def test_home_is_resolved_when_called(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'T3_CONTROLLER_HOME': home}):
            self.assertEqual(EnvironmentStore().home, Path(home))
            self.assertEqual(build_parser().parse_args(['environments']).home, Path(home))

    def test_duplicate_environments_are_read_once(self):
        self.assertEqual(selected_environments(EnvironmentStore(), ['mac', 'mac', 'i7']), ['mac', 'i7'])

    def test_legacy_launcher_works_from_another_directory(self):
        launcher = Path(__file__).parent / 't3_rpc_controller.py'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(launcher), '--home', directory, 'environments'],
                cwd=directory, capture_output=True, text=True, check=True,
            )
        self.assertEqual(json.loads(result.stdout), {
            'schemaVersion': 1, 'ok': True, 'count': 0, 'environments': {},
        })
        self.assertEqual(result.stderr, '')


class ConfigurationTests(unittest.TestCase):
    def test_only_plain_origins_are_accepted(self):
        for url in (
            'http://user:password@localhost:3773', 'http://localhost:3773/path',
            'http://localhost:3773?token=private', 'http://localhost:3773#fragment',
            'http://localhost:invalid', 'http://localhost:65536', 'http://localhost:0',
            'http://local host:3773',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)
        self.assertEqual(normalize_url(' https://localhost:3773/ '), 'https://localhost:3773')
        self.assertEqual(normalize_url('http://[::1]:3773'), 'http://[::1]:3773')

    def test_dot_names_and_blank_tokens_do_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EnvironmentStore(Path(directory) / 'state')
            for name in ('.', '..', '../escape', 'has/slash'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    store.save(name, 'http://localhost', 'secret')
            with self.assertRaises(ValueError):
                store.save('test', 'http://localhost', '  ')
            self.assertFalse(store.home.exists())

    def test_invalid_config_is_rejected_before_network_access(self):
        for value in ([], {'test': None}, {'test': {'url': 42}}, {'..': {'url': 'http://localhost'}}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                store = EnvironmentStore(Path(directory))
                store.config_path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    store.load_config()

    def test_failed_replacement_preserves_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            write_private(path, 'original')
            with patch.object(Path, 'replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    write_private(path, 'replacement')
            self.assertEqual(path.read_text(), 'original')
            self.assertEqual(list(Path(directory).iterdir()), [path])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_config_save_restores_existing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EnvironmentStore(Path(directory))
            store.save('test', 'http://localhost:3773', 'original')
            def fail_config(path, text):
                if path == store.config_path:
                    raise OSError('disk failure')
                write_private(path, text)
            with patch('t3_controller.state.write_private', side_effect=fail_config):
                with self.assertRaises(OSError):
                    store.save('test', 'http://localhost:3774', 'replacement')
                with self.assertRaises(OSError):
                    store.remove('test')
            self.assertEqual(store.load('test'), ('http://localhost:3773', 'original'))

    def test_empty_saved_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EnvironmentStore(Path(directory))
            store.save('test', 'http://localhost', 'secret')
            (store.token_dir / 'test').write_text(' ')
            with self.assertRaises(ValueError):
                store.load('test')

    def test_websocket_ticket_is_encoded_as_one_query_value(self):
        from urllib.parse import parse_qs, urlparse
        url = websocket_url('https://localhost', 'a+b&c=?')
        self.assertEqual(parse_qs(urlparse(url).query), {'wsTicket': ['a+b&c=?']})
