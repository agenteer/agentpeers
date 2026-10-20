"""Existing external mailboxes survive a plugin/package rename."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentpeers.config import setting
from agentpeers.core import Bridge, current_actor, state_dir


class RenameMigration(unittest.TestCase):
    def test_existing_mailbox_is_reused_without_moving_data(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), \
                patch('agentpeers.core.Path.home', return_value=Path(folder)):
            legacy = Path(folder) / '.local/state/peerbridge'
            legacy.mkdir(parents=True, mode=0o700)
            with Bridge() as original:
                original.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    ('saved', 'codex:sender', 'claude:recipient', 'keep me', 'scope',
                                     None, 1, 'unavailable', None, '{}'))
                original.db.commit()
            with Bridge() as replacement:
                self.assertEqual(replacement.get('saved')['body'], 'keep me')
            self.assertEqual(state_dir(), legacy)
            self.assertFalse((legacy.parent / 'agentpeers').exists())

    def test_fresh_install_and_explicit_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), \
                patch('agentpeers.core.Path.home', return_value=Path(folder)):
            self.assertEqual(state_dir(), Path(folder) / '.local/state/agentpeers')
            os.environ['PEERBRIDGE_HOME'] = folder + '/former'
            self.assertEqual(state_dir(), Path(folder) / 'former')
            os.environ['AGENTPEERS_HOME'] = folder + '/chosen'
            self.assertEqual(state_dir(), Path(folder) / 'chosen')

    def test_legacy_actor_and_codex_executable_remain_usable(self):
        with patch.dict(os.environ, {'PEERBRIDGE_SESSION': 'codex:existing',
                                    'PEERBRIDGE_CODEX': '/fixture/codex'}, clear=True):
            self.assertEqual(current_actor(), 'codex:existing')
            self.assertEqual(setting('CODEX'), '/fixture/codex')
            os.environ['AGENTPEERS_SESSION'] = 'codex:replacement'
            self.assertEqual(current_actor(), 'codex:replacement')
