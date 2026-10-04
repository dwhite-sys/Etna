import contextlib
import io
import json
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from etna import chatgpt_compat as compat
from etna import cli


class ChatGPTCompatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.path = self.home / '.agents/plugins/marketplace.json'
        self.config = {'kits': {'web_kit': {'kit_name': 'Web'}, 'web-kit': {'kit_name': 'Web'}}}

    def read(self):
        return json.loads(self.path.read_text())

    def test_layout_identity_and_removal(self):
        compat.sync(self.config, self.home)
        first = self.read()['plugins']
        self.assertEqual(len({entry['name'] for entry in first}), 2)
        for entry in first:
            directory = self.home / entry['source']['path']
            manifest = json.loads((directory / 'plugin.json').read_text())
            self.assertIn('com.openai', manifest['extensions'])
            self.assertEqual(manifest['version'], compat.PLUGIN_VERSION)
            server = next(iter(json.loads((directory / 'mcp.json').read_text())['mcpServers'].values()))
            self.assertEqual(server['type'], 'stdio')
            self.assertTrue(server['command'].startswith('./'))
            self.assertNotIn('args', server)
            launcher = directory / server['command']
            self.assertTrue(launcher.is_file())
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn('start stdio', launcher.read_text())
        self.config['kits']['web_kit']['kit_name'] = 'Renamed'
        compat.sync(self.config, self.home)
        self.assertEqual([e['name'] for e in first], [e['name'] for e in self.read()['plugins']])
        self.config['kits'] = {}
        compat.sync(self.config, self.home)
        self.assertEqual(self.read()['plugins'], [])
        self.assertEqual(list((self.home / '.codex/plugins').iterdir()), [])

    def test_absolute_etna_path_is_kept_inside_contained_launcher(self):
        executable = '/opt/Etna Folder/bin/etna'
        with patch.object(compat.shutil, 'which', return_value=executable):
            compat.sync({'kits': {'web': {'kit_name': 'Web'}}}, self.home)
        entry = self.read()['plugins'][0]
        directory = self.home / entry['source']['path']
        server = next(iter(json.loads((directory / 'mcp.json').read_text())['mcpServers'].values()))
        launcher_name = 'etna-stdio.cmd' if os.name == 'nt' else 'etna-stdio'
        self.assertEqual(server['command'], './' + launcher_name)
        launcher = (directory / launcher_name).read_text()
        expected = '"/opt/Etna Folder/bin/etna" start stdio web' if os.name == 'nt' else "'/opt/Etna Folder/bin/etna' start stdio web"
        self.assertIn(expected, launcher)

    def test_preserves_unrelated_and_dedupes_owned(self):
        compat.sync(self.config, self.home)
        market = self.read()
        unrelated = {'name': 'other', 'source': {'source': 'local', 'path': './other'}, 'custom': True}
        market['plugins'] += [market['plugins'][0], unrelated]
        market['custom'] = {'keep': True}
        self.path.write_text(json.dumps(market))
        compat.sync(self.config, self.home)
        self.assertEqual(len(self.read()['plugins']), 3)
        self.assertEqual(self.read()['plugins'][0], unrelated)
        self.assertEqual(self.read()['custom'], {'keep': True})

    def test_malformed_aborts_without_mutation(self):
        for value in ('{broken', '[]', '{"name":"local","plugins":{}}', '{"name":"local","plugins":[null]}'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                compat.sync(self.config, self.home)
            self.assertEqual(self.path.read_text(), value)
            self.assertFalse((self.home / '.codex').exists())

    def test_collision_aborts_before_creating_any_packages(self):
        directory = self.home / '.codex/plugins' / compat._plugin_name('web-kit')
        directory.mkdir(parents=True)
        (directory / 'mine').write_text('keep')
        with self.assertRaises(ValueError):
            compat.sync(self.config, self.home)
        self.assertEqual((directory / 'mine').read_text(), 'keep')
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(directory.parent.iterdir())), 1)

    def test_dispatch_and_registered_sync(self):
        with patch.object(cli.cfg, 'load', return_value=self.config), patch.object(cli.cfg, 'register_client') as register, patch.object(compat, 'sync') as sync, contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_compat(['chatgpt'])
            register.assert_called_once_with('chatgpt', {})
            sync.assert_called_once_with(self.config)
        with patch.object(cli.cfg, 'load_clients', return_value={'chatgpt': {}}), patch.object(compat, 'sync') as sync, contextlib.redirect_stdout(io.StringIO()):
            cli._sync_clients(self.config)
            sync.assert_called_once_with(self.config)

    def test_advertised_dispatchers(self):
        for target, writer in [('cursor', '_compat_write_stdio_config'), ('windsurf', '_compat_write_stdio_config'), ('vscode', '_compat_write_vscode_config'), ('continue', '_compat_write_continue_config')]:
            with patch.object(cli, writer) as method:
                cli.cmd_compat([target])
                self.assertEqual(method.call_args.args[2], target)

    def test_lifecycle_sync(self):
        with patch.object(cli.cfg, 'load', return_value=self.config), patch.object(cli.cfg, 'save'), patch.object(cli.cfg, 'kits_dir', return_value=self.home), patch.object(cli, '_sync_clients') as sync, patch.object(cli.km, 'install_kit'), patch.object(cli.km, 'remove_kit'), patch.object(cli.km, 'install_kit_from_repo'):
            cli.cmd_install(['sample.py'])
            cli.cmd_update(['sample.py'])
            cli.cmd_update(['--all'])
            cli.cmd_remove(['web_kit'])
            self.assertEqual(sync.call_count, 4)

    def test_duplicate_owned_directories_removed(self):
        compat.sync(self.config, self.home)
        duplicate = self.home / '.codex/plugins/old-name'
        duplicate.mkdir()
        (duplicate / compat.MARKER).write_text(json.dumps({'owner': 'etna', 'kit_stem': 'web_kit'}))
        market = self.read()
        market['plugins'].append({'name': 'old-name', 'source': {'source': 'local', 'path': './.codex/plugins/old-name'}})
        self.path.write_text(json.dumps(market))
        compat.sync(self.config, self.home)
        self.assertFalse(duplicate.exists())
        self.assertEqual(len(self.read()['plugins']), 2)

    def test_auto_detects_all_local_clients(self):
        directories = ['.config/Claude', '.lmstudio', '.cursor',
                       '.codeium/windsurf', '.config/Code/User', '.continue', '.codex']
        for directory in directories:
            (self.home / directory).mkdir(parents=True)
        with patch.object(cli.Path, 'home', return_value=self.home), patch.object(cli.sys, 'platform', 'linux'), patch('builtins.input', return_value='y'), patch.object(cli, 'cmd_compat') as dispatch, contextlib.redirect_stdout(io.StringIO()):
            cli._compat_auto()
        self.assertEqual([call.args[0] for call in dispatch.call_args_list],
                         [[name] for name in ['claude', 'lmstudio', 'cursor', 'windsurf', 'vscode', 'continue', 'chatgpt']])

    def test_auto_individual_selection(self):
        (self.home / '.codex').mkdir()
        (self.home / '.continue').mkdir()
        with patch.object(cli.Path, 'home', return_value=self.home), patch('builtins.input', side_effect=['n', 'n', 'y']), patch.object(cli, 'cmd_compat') as dispatch, contextlib.redirect_stdout(io.StringIO()):
            cli._compat_auto()
        dispatch.assert_called_once_with(['chatgpt'])

    def test_auto_no_clients_creates_no_config(self):
        with patch.object(cli.Path, 'home', return_value=self.home), patch.object(cli, 'cmd_compat') as dispatch, patch('builtins.input') as prompt, contextlib.redirect_stdout(io.StringIO()):
            cli._compat_auto()
        dispatch.assert_not_called()
        prompt.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_failed_compat_does_not_register(self):
        with patch.object(cli.cfg, 'load', return_value=self.config), patch.object(cli.cfg, 'register_client') as register, patch.object(compat, 'sync', side_effect=ValueError('bad config')), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                cli.cmd_compat(['chatgpt'])
            self.assertEqual(result.exception.code, 1)
            register.assert_not_called()


if __name__ == '__main__':
    unittest.main()
