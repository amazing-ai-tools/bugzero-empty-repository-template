import concurrent.futures
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('network_deploy_vps', Path(__file__).with_name('deploy-vps.py'))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Inventory:
    def __init__(self, networks=None, routes=None):
        self.networks = networks or []
        self.routes = routes or []
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[:3] == ['docker', 'network', 'ls']:
            return SimpleNamespace(stdout='\n'.join(network['Id'] for network in self.networks))
        if command[:3] == ['docker', 'network', 'inspect']:
            return SimpleNamespace(stdout=json.dumps(self.networks))
        if command[:2] == ['ip', '-j']:
            return SimpleNamespace(stdout=json.dumps(self.routes))
        raise AssertionError(command)


def network(name, subnet, project=None, key=None, identifier='a' * 64):
    return {'Id': identifier, 'Name': name, 'Driver': 'bridge', 'Options': {},
            'Labels': {'com.docker.compose.project': project, 'com.docker.compose.network': key} if project else {},
            'IPAM': {'Config': [{'Subnet': subnet}]}}


class SubnetTests(unittest.TestCase):
    def config(self, root, slug='sample-app', **kwargs):
        return module.Config(slug=slug, domain=f'{slug}.example.com', display_name='Sample', revision='a' * 40,
                             state_root=Path(root) / 'apps', sites_dir=Path(root) / 'sites',
                             caddy_config=Path(root) / 'Caddyfile', **kwargs)

    def deployment(self, root, *, slug='sample-app', networks=None, routes=None, **kwargs):
        config = self.config(root, slug=slug, **kwargs)
        inventory = Inventory(networks, routes)
        deploy = module.Deployment(config, runner=inventory)
        deploy.preflight()
        return deploy, inventory

    def test_pure_allocator_avoids_existing_subnets_and_individual_host_routes(self):
        result = module.choose_network_subnets('10.240.0.0/16', ['10.240.0.0/28', '10.240.0.16/32'])
        self.assertEqual(result, {'database': '10.240.0.32/28', 'blue': '10.240.0.48/28', 'green': '10.240.0.64/28'})

    def test_exhausted_small_pool_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, 'exhausted'):
            module.choose_network_subnets('192.168.5.0/26', ['192.168.5.0/27'])

    def test_only_canonical_rfc1918_pools_with_three_small_networks_are_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            for pool in ['8.8.0.0/16', '127.0.0.0/8', '100.64.0.0/10', '10.240.0.0/27', '10.240.0.1/16', 'fd00::/64']:
                with self.subTest(pool=pool), self.assertRaises(ValueError):
                    self.config(root, network_pool=pool)

    def test_full_docker_default_pool_does_not_prevent_small_app_networks(self):
        with tempfile.TemporaryDirectory() as root:
            networks = [network(f'old-{index}', f'172.{index}.0.0/16', identifier=str(index)) for index in range(17, 32)]
            networks += [network(f'old-small-{index}', f'192.168.{index * 16}.0/20', identifier=f'small-{index}') for index in range(14)]
            deploy, inventory = self.deployment(root, networks=networks, routes=[{'dst': 'default'}, {'dst': '0.0.0.0/0'}, {'dst': '10.240.0.0/28', 'dev': 'vpn0'}])
            result = deploy.allocate_networks()
            self.assertEqual(result['database'], '10.240.0.16/28')
            self.assertTrue(all(ipaddress.ip_network(value).prefixlen == 28 for value in result.values()))
            saved = deploy.config.app_dir / 'network-subnets.json'
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(saved.read_text())['subnets'], result)
            self.assertTrue(any(command[0] == 'ip' for command in inventory.calls))

    def test_other_app_reservations_survive_without_docker_networks(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.deployment(root)
            a = first.allocate_networks()
            second, _ = self.deployment(root, slug='second-app')
            b = second.allocate_networks()
            self.assertTrue(set(a.values()).isdisjoint(b.values()))
            self.assertEqual(first.allocate_networks(), a)

    def test_concurrent_apps_reserve_distinct_subnets_with_kernel_lock(self):
        with tempfile.TemporaryDirectory() as root:
            apps = [self.deployment(root, slug=f'app-{index}')[0] for index in range(4)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda deployment: deployment.allocate_networks(), apps))
            self.assertEqual(len({value for result in results for value in result.values()}), 12)
            self.assertEqual((apps[0].config.state_root / '.network-allocation.lock').stat().st_mode & 0o777, 0o600)

    def test_matching_existing_network_and_its_connected_host_routes_are_reused(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, inventory = self.deployment(root)
            result = deploy.allocate_networks()
            inventory.networks = [network('bugzero-sample-app-database', result['database'], 'bugzero-sample-app-data', 'database')]
            inventory.routes = [{'dst': result['database'], 'dev': 'br-aaaaaaaaaaaa'}, {'dst': '10.240.0.1/32', 'dev': 'br-aaaaaaaaaaaa', 'type': 'local'}]
            self.assertEqual(deploy.allocate_networks(), result)

    def test_existing_network_without_reservation_or_with_foreign_labels_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            existing = network('bugzero-sample-app-database', '10.240.0.0/28', 'foreign', 'database')
            deploy, inventory = self.deployment(root, networks=[existing])
            with self.assertRaisesRegex(RuntimeError, 'network'):
                deploy.allocate_networks()
            self.assertFalse((deploy.config.app_dir / 'network-subnets.json').exists())
            inventory.networks = []
            deploy.allocate_networks()
            inventory.networks = [existing]
            with self.assertRaisesRegex(RuntimeError, 'network'):
                deploy.allocate_networks()

    def test_saved_subnet_cannot_change_or_overlap_a_foreign_network_or_route(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, inventory = self.deployment(root)
            saved = deploy.allocate_networks()
            path = deploy.config.app_dir / 'network-subnets.json'
            original = path.read_bytes()
            for networks, routes in [([network('bugzero-sample-app-database', '10.240.2.0/28', 'bugzero-sample-app-data', 'database')], []),
                                     ([network('foreign', saved['blue'])], []),
                                     ([], [{'dst': '10.240.0.1/32', 'dev': 'vpn0'}])]:
                inventory.networks, inventory.routes = networks, routes
                with self.assertRaisesRegex(RuntimeError, 'subnet|overlap'):
                    deploy.allocate_networks()
                self.assertEqual(path.read_bytes(), original)

    def test_untrusted_reservation_or_lock_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, _ = self.deployment(root)
            deploy.allocate_networks()
            saved = deploy.config.app_dir / 'network-subnets.json'
            target = deploy.config.app_dir / 'other.json'
            saved.rename(target)
            saved.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'reservation|symlink'):
                deploy.allocate_networks()
            saved.unlink(); os.link(target, saved)
            with self.assertRaisesRegex(RuntimeError, 'reservation|link'):
                deploy.allocate_networks()

    def test_network_fields_are_added_without_changing_existing_database_password(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, inventory = self.deployment(root)
            first = deploy.database_environment()
            self.assertEqual(inventory.calls, [])
            reservations = deploy.allocate_networks()
            database = deploy.database_environment()
            self.assertEqual(database['POSTGRES_PASSWORD'], first['POSTGRES_PASSWORD'])
            self.assertEqual(database['DATABASE_URL'], first['DATABASE_URL'])
            self.assertEqual(database['APP_DATABASE_SUBNET'], reservations['database'])
            self.assertEqual(deploy.slot_environment('blue')['APP_SLOT_SUBNET'], reservations['blue'])
            self.assertEqual(deploy.slot_environment('green')['APP_SLOT_SUBNET'], reservations['green'])
            calls = list(inventory.calls)
            self.assertEqual(deploy.database_environment(), database)
            self.assertEqual(inventory.calls, calls)

    def test_exhaustion_does_not_persist_a_partial_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, _ = self.deployment(root, network_pool='10.244.0.0/26', routes=[{'dst': '10.244.0.0/27', 'dev': 'vpn0'}])
            with self.assertRaisesRegex(RuntimeError, 'exhausted'):
                deploy.allocate_networks()
            self.assertFalse((deploy.config.app_dir / 'network-subnets.json').exists())

    def test_group_writable_reservations_and_locks_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, _ = self.deployment(root)
            deploy.allocate_networks()
            for path in [deploy.config.app_dir / 'network-subnets.json', deploy.config.state_root / '.network-allocation.lock']:
                path.chmod(0o660)
                with self.assertRaisesRegex(RuntimeError, 'reservation|lock'):
                    deploy.allocate_networks()
                path.chmod(0o600)

    def test_saved_pool_or_app_identity_cannot_be_reassigned(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as root:
            deploy, inventory = self.deployment(root)
            deploy.allocate_networks()
            for config in [dataclasses.replace(deploy.config, network_pool='10.241.0.0/16'),
                           dataclasses.replace(deploy.config, app_id='a064c533-4947-45e5-a228-47074ca7ad33', repo_url='https://github.com/example/sample-app')]:
                with self.assertRaisesRegex(RuntimeError, 'identity|pool'):
                    module.Deployment(config, runner=inventory).allocate_networks()

    def test_workflow_passes_configurable_pool_through_sudo(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/deploy.yml').read_text()
        self.assertIn("BUGZERO_NETWORK_POOL: ${{ vars.BUGZERO_NETWORK_POOL || '10.240.0.0/16' }}", workflow)
        self.assertEqual(workflow.count('BUGZERO_NETWORK_POOL="$BUGZERO_NETWORK_POOL"'), 2)

    def test_compose_files_require_explicit_small_network_subnets(self):
        directory = Path(__file__).parent
        data, slot = ((directory / name).read_text() for name in ['compose.data.yml', 'compose.slot.yml'])
        self.assertIn('subnet: ${APP_DATABASE_SUBNET:', data)
        self.assertIn('subnet: ${APP_SLOT_SUBNET:', slot)


if __name__ == '__main__':
    unittest.main()
