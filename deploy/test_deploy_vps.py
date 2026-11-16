import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('deploy_vps', Path(__file__).with_name('deploy-vps.py'))
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class DeploymentTests(unittest.TestCase):
    def config(self, root, **kwargs):
        return module.Config(slug=kwargs.pop('slug', 'sample-app'), domain=kwargs.pop('domain', 'sample.example.com'),
                             display_name='Sample App', revision='a' * 40, state_root=Path(root) / 'apps',
                             sites_dir=Path(root) / 'sites', caddy_config=Path(root) / 'Caddyfile', **kwargs)

    def test_rejects_unsafe_identifiers_before_writing_files(self):
        with tempfile.TemporaryDirectory() as root:
            for slug in ['../chat', 'A', 'a;id', '-app', 'a' * 49]:
                with self.assertRaises(ValueError):
                    self.config(root, slug=slug)
            for domain in ['a.com\nimport /etc/shadow', '*.example.com', 'https://example.com', 'a.com:99']:
                with self.assertRaises(ValueError):
                    self.config(root, domain=domain)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_never_replaces_unowned_caddy_site(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            config.sites_dir.mkdir()
            config.site_path.write_text('chat.example.com { respond "production" }\n')
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                module.Deployment(config).preflight()
            self.assertEqual(config.site_path.read_text(), 'chat.example.com { respond "production" }\n')
            self.assertFalse(config.app_dir.exists())

    def test_rejects_symlink_state_directory(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            config.state_root.mkdir()
            other = Path(root) / 'outside'
            other.mkdir()
            config.app_dir.symlink_to(other, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                module.Deployment(config).preflight()

    def test_database_secret_is_stable_and_private(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            deploy = module.Deployment(config)
            deploy.preflight()
            first = deploy.database_environment()
            second = deploy.database_environment()
            self.assertEqual(first, second)
            self.assertNotIn('password', first['POSTGRES_PASSWORD'].lower())
            self.assertEqual(config.database_env.stat().st_mode & 0o777, 0o600)

    def test_failed_caddy_reload_restores_previous_site(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            attempts = []
            def reload_once_failed():
                attempts.append(True)
                if len(attempts) == 1:
                    raise RuntimeError('reload failed')
            deploy = module.Deployment(config, runner=lambda command, **kwargs: None,
                                       reload_caddy=reload_once_failed, verify_public=lambda: None)
            deploy.preflight()
            old_site = config.ownership_marker + '\nsample.example.com { reverse_proxy 127.0.0.1:10000 }\n'
            config.site_path.write_text(old_site)
            with self.assertRaisesRegex(RuntimeError, 'reload failed'):
                deploy.switch_site(10001)
            self.assertEqual(config.site_path.read_text(), old_site)

    def test_public_revision_failure_restores_site_before_committing_state(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            def wrong_public():
                raise RuntimeError('public revision mismatch')
            committed = []
            deploy = module.Deployment(config, runner=lambda command, **kwargs: None,
                                       reload_caddy=lambda: None, verify_public=wrong_public)
            deploy.preflight()
            old_site = config.ownership_marker + '\nsample.example.com { reverse_proxy 127.0.0.1:10000 }\n'
            config.site_path.write_text(old_site)
            with self.assertRaisesRegex(RuntimeError, 'public revision mismatch'):
                deploy.switch_site(10001, commit=lambda: committed.append(True))
            self.assertEqual(config.site_path.read_text(), old_site)
            self.assertEqual(committed, [])

    def test_failed_rollback_reload_preserves_candidate_and_prior_slot(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            stops = []
            class FailedRollback(module.Deployment):
                def compose(self, kind, *arguments, **kwargs):
                    if 'stop' in arguments:
                        stops.append(kwargs['slot'])
                    if 'pg_dump' in arguments:
                        kwargs['stdout'].write(b'backup archive')
                    return SimpleNamespace(stdout='127.0.0.1:12345')
                def check_candidate(self, port):
                    pass
                def switch_site(self, port, commit):
                    raise module.RollbackFailed('Caddy rollback reload failed')
            deploy = FailedRollback(config)
            deploy.preflight()
            (config.app_dir / 'current.json').write_text(json.dumps({'slot': 'blue', 'revision': 'b' * 40}))
            with self.assertRaises(module.RollbackFailed):
                deploy.deploy()
            self.assertEqual(stops, [])
            self.assertEqual(json.loads((config.app_dir / 'current.json').read_text())['slot'], 'blue')

    def test_recovery_marker_blocks_next_deploy_before_docker_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            deploy = module.Deployment(config, runner=lambda command, **kwargs: None,
                                       reload_caddy=lambda: (_ for _ in ()).throw(RuntimeError('reload failed')),
                                       verify_public=lambda: None)
            deploy.preflight()
            with self.assertRaises(module.RollbackFailed):
                deploy.switch_site(10001)
            self.assertTrue(config.recovery_marker.exists())
            commands = []
            retry = module.Deployment(config, runner=lambda command, **kwargs: commands.append(command))
            with self.assertRaisesRegex(RuntimeError, 'Recovery required'):
                retry.deploy()
            self.assertEqual(commands, [])

    def test_failed_rollback_file_restore_preserves_candidate(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            deploy = module.Deployment(config, runner=lambda command, **kwargs: None,
                                       reload_caddy=lambda: None,
                                       verify_public=lambda: (_ for _ in ()).throw(RuntimeError('public failed')))
            deploy.preflight()
            config.site_path.write_text(config.ownership_marker + '\nold.example.com {}\n')
            original = module.atomic_write
            attempts = []
            def fail_restore(path, *args, **kwargs):
                if path == config.site_path:
                    attempts.append(path)
                if path == config.site_path and len(attempts) == 2:
                    raise OSError('disk failed restoring route')
                return original(path, *args, **kwargs)
            with patch.object(module, 'atomic_write', side_effect=fail_restore):
                with self.assertRaises(module.RollbackFailed):
                    deploy.switch_site(10001)

    def test_failed_rollback_reload_is_reported_explicitly(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            def failed_reload():
                raise RuntimeError('reload failed')
            deploy = module.Deployment(config, runner=lambda command, **kwargs: None,
                                       reload_caddy=failed_reload, verify_public=lambda: None)
            deploy.preflight()
            with self.assertRaises(module.RollbackFailed):
                deploy.switch_site(10001)

    def test_failed_state_commit_restores_previous_site(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            deploy = module.Deployment(config, runner=lambda command, **kwargs: None, reload_caddy=lambda: None, verify_public=lambda: None)
            deploy.preflight()
            old_site = config.ownership_marker + '\nsample.example.com { reverse_proxy 127.0.0.1:10000 }\n'
            config.site_path.write_text(old_site)
            def failed_commit():
                raise RuntimeError('state disk failed')
            with self.assertRaisesRegex(module.RollbackFailed, 'Release state commit failed'):
                deploy.switch_site(10001, commit=failed_commit)
            self.assertEqual(config.site_path.read_text(), old_site)

    def test_failed_first_site_validation_removes_new_site(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            def invalid(command, **kwargs):
                raise RuntimeError('config invalid')
            deploy = module.Deployment(config, runner=invalid, reload_caddy=lambda: None)
            deploy.preflight()
            with self.assertRaisesRegex(RuntimeError, 'config invalid'):
                deploy.switch_site(10001)
            self.assertFalse(config.site_path.exists())

    def test_health_requires_candidate_revision_and_database(self):
        with tempfile.TemporaryDirectory() as root:
            deploy = module.Deployment(self.config(root))
            for body in [{'status': 'unhealthy', 'revision': 'a' * 40}, {'status': 'healthy', 'revision': 'b' * 40}]:
                with self.assertRaises(RuntimeError):
                    deploy.assert_health(body)
            deploy.assert_health({'status': 'healthy', 'revision': 'a' * 40})

    def test_widget_configuration_uses_public_key_and_https_script(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root, bugzero_app_key='bz_public_key', bugzero_widget_url='https://bugzero.app/widget.js')
            deploy = module.Deployment(config)
            deploy.preflight()
            runtime = json.loads(Path(deploy.slot_environment('blue')['APP_CONFIG_PATH']).read_text())
            self.assertEqual(runtime['bugzero_app_key'], 'bz_public_key')
            self.assertEqual(runtime['bugzero_widget_url'], 'https://bugzero.app/widget.js')
            with self.assertRaises(ValueError):
                self.config(root, bugzero_app_key='key', bugzero_widget_url='javascript:alert(1)')

    def test_runtime_config_has_no_database_secret(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config(root)
            deploy = module.Deployment(config)
            deploy.preflight()
            env = deploy.slot_environment('blue')
            runtime = json.loads(Path(env['APP_CONFIG_PATH']).read_text())
            self.assertEqual(runtime, {'display_name': 'Sample App', 'domain': 'sample.example.com', 'revision': 'a' * 40})
            self.assertNotIn('DATABASE_URL', runtime)
            self.assertEqual(json.loads(Path(env['RELEASE_MANIFEST_PATH']).read_text())['revision'], 'a' * 40)


if __name__ == '__main__':
    unittest.main()
