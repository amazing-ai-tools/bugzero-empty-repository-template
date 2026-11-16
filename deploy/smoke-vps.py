#!/usr/bin/env python3
"""Isolated Docker integration: real Rust/PG/WASM, two promotions and rollback.

Build the two production Dockerfiles first, then:
SMOKE_API_IMAGE=app-api:test SMOKE_WEB_IMAGE=app-web:test python3 deploy/smoke-vps.py
This never reads/writes the production Caddy configuration or reloads Caddy.
"""
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import urllib.request

spec = importlib.util.spec_from_file_location('deploy_vps', Path(__file__).with_name('deploy-vps.py'))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, capture_output=True, **kwargs)


def main():
    api_source = os.environ['SMOKE_API_IMAGE']
    web_source = os.environ['SMOKE_WEB_IMAGE']
    slug = 'rust-smoke-' + secrets.token_hex(5)
    tags = []
    results = {'slug': slug, 'production_caddy_touched': False}
    revision_a, revision_b, revision_bad = 'a' * 40, 'b' * 40, 'c' * 40
    with tempfile.TemporaryDirectory(prefix='bugzero-rust-smoke-') as directory:
        root = Path(directory)
        config = module.Config(slug=slug, domain=f'{slug}.invalid', display_name='Rust deployment smoke',
                               revision=revision_a, state_root=root / 'apps', sites_dir=root / 'sites',
                               caddy_config=root / 'Caddyfile')
        config.caddy_config.write_text('{\n admin off\n auto_https off\n}\nimport ' + str(config.sites_dir) + '/*.caddy\n')
        deployment = module.Deployment(config, reload_caddy=lambda: None, verify_public=lambda: None, health_timeout=15)
        try:
            for revision in [revision_a, revision_b, revision_bad]:
                for kind, source in [('api', api_source), ('web', web_source)]:
                    tag = f'bugzero-{slug}-{kind}:{revision}'
                    tags.append(tag)
                    docker('tag', source, tag)
            # Migration succeeds, but the broken candidate never starts an HTTP API.
            broken_dockerfile = root / 'Dockerfile.broken'
            # json.dumps avoids quoting or shell interpretation of the static entrypoint.
            entrypoint = ['/bin/sh', '-c', 'if [ "$1" = --migrate-only ]; then exec /usr/local/bin/app-api --migrate-only; else exec sleep 120; fi', 'sh']
            broken_dockerfile.write_text(f'FROM {api_source}\nENTRYPOINT ' + json.dumps(entrypoint) + '\n')
            docker('build', '--file', str(broken_dockerfile), '--tag', f'bugzero-{slug}-api:{revision_bad}', str(root))
            first = deployment.deploy()
            sql = deployment.compose('data', 'exec', '-T', 'db', 'psql', '-U', 'app', '-d', 'app', '-Atc',
                                     'SELECT count(*) FROM _sqlx_migrations WHERE success = true;')
            assert int(sql.stdout.strip()) >= 1, 'Embedded SQLx migrations were not applied'
            deployment.compose('data', 'exec', '-T', 'db', 'psql', '-U', 'app', '-d', 'app', '-c',
                               "CREATE TABLE smoke_persistence (value text); INSERT INTO smoke_persistence VALUES ('survives-promotion');")
            results['first_revision'] = first['revision']
            deployment = module.Deployment(dataclasses.replace(config, revision=revision_b), reload_caddy=lambda: None, verify_public=lambda: None, health_timeout=15)
            second = deployment.deploy()
            assert second['slot'] != first['slot'], 'Blue/green slot did not change'
            results['second_revision'] = second['revision']
            current_path = config.app_dir / 'current.json'
            before_state, before_site = current_path.read_bytes(), config.site_path.read_bytes()
            failing = module.Deployment(dataclasses.replace(config, revision=revision_bad), reload_caddy=lambda: None, verify_public=lambda: None, health_timeout=5)
            try:
                failing.deploy()
            except RuntimeError as error:
                assert 'health checks' in str(error), f'Unexpected failure: {error}'
            else:
                raise AssertionError('Broken candidate was promoted')
            assert current_path.read_bytes() == before_state, 'Failed candidate changed active state'
            assert config.site_path.read_bytes() == before_site, 'Failed candidate changed the Caddy route'
            deployment.check_candidate(second['port'])
            persisted = deployment.compose('data', 'exec', '-T', 'db', 'psql', '-U', 'app', '-d', 'app', '-Atc',
                                           'SELECT value FROM smoke_persistence;')
            assert persisted.stdout.strip() == 'survives-promotion', 'Database did not survive slot changes'
            with urllib.request.urlopen(f"http://127.0.0.1:{second['port']}/", timeout=3) as response:
                html = response.read().decode()
            import re
            wasm_paths = re.findall(r'(?:href|src)="([^"]+\.wasm)"', html)
            if not wasm_paths:
                # Trunk emits the binary URL in the module script, not always a link tag.
                wasm_paths = re.findall(r"['\"]([^'\"]+_bg\.wasm)['\"]", html)
            assert wasm_paths, 'Production frontend HTML did not reference a WASM binary'
            for wasm_path in wasm_paths:
                with urllib.request.urlopen(f"http://127.0.0.1:{second['port']}/" + wasm_path.lstrip('/'), timeout=3) as response:
                    assert response.read(4) == b'\x00asm', 'Frontend asset was not WebAssembly'
            backups = list((config.app_dir / 'backups').glob('*.dump'))
            assert len(backups) == 3, 'Every migration attempt must have a backup'
            for backup in backups:
                docker('run', '--rm', '--volume', f'{backup}:/backup.dump:ro', 'postgres:17-alpine', 'pg_restore', '--list', '/backup.dump')
            inspect = docker('inspect', f'bugzero-{slug}-data-db-1', '--format', '{{json .HostConfig.PortBindings}}', text=True)
            assert json.loads(inspect.stdout) in [None, {}], 'PostgreSQL has a public host port'
            for service in ['api', 'web']:
                user = docker('inspect', f"bugzero-{slug}-{second['slot']}-{service}-1", '--format', '{{.Config.User}}', text=True).stdout.strip()
                assert user not in ['', '0', 'root'], f'{service} runtime runs as root'
            results.update({'nonroot_api_and_web': True, 'sqlx_migrations': True, 'persistent_database': True, 'backups_verified': len(backups),
                            'failed_candidate_rollback': True, 'webassembly_binary': True, 'database_public_ports': False})
            print(json.dumps(results, indent=2))
        finally:
            # Only this randomly named smoke app is removed; production deployment never deletes data.
            assert slug.startswith('rust-smoke-') and len(slug) == 21
            for slot in ['blue', 'green']:
                if (config.app_dir / slot / 'runtime.env').exists():
                    deployment.compose('slot', 'down', '--remove-orphans', slot=slot)
            if config.database_env.exists():
                deployment.compose('data', 'down', '--remove-orphans')
                docker('volume', 'rm', f'bugzero-{slug}-postgres')
            for tag in tags:
                docker('image', 'rm', tag)


if __name__ == '__main__':
    main()
