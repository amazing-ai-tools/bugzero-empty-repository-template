#!/usr/bin/env python3
"""Deploy one Rust app on its VPS runner; never removes persistent data."""
from __future__ import annotations

import dataclasses
import fcntl
import json
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parent


class RollbackFailed(RuntimeError):
    """Routing may still reference the candidate; keep both slots alive for recovery."""


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


NETWORK_ROLES = ('database', 'blue', 'green')
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def network_pool(value):
    try:
        pool = ipaddress.ip_network(value, strict=True)
        if (pool.version != 4 or str(pool) != value or pool.prefixlen > 26
                or not any(pool.subnet_of(private) for private in PRIVATE_NETWORKS)):
            raise ValueError()
        return pool
    except (ValueError, TypeError):
        raise ValueError('Network pool must be a canonical RFC1918 IPv4 subnet with room for three /28 networks') from None


def choose_network_subnets(pool, occupied):
    """Pure allocation; no Docker or host changes are performed here."""
    pool = network_pool(pool)
    occupied = [ipaddress.ip_network(value, strict=False) for value in occupied]
    occupied = [subnet for subnet in occupied if subnet.version == 4]
    available = []
    for candidate in pool.subnets(new_prefix=28):
        if not any(candidate.overlaps(other) for other in occupied):
            available.append(str(candidate))
            if len(available) == len(NETWORK_ROLES):
                return dict(zip(NETWORK_ROLES, available))
    raise RuntimeError('App network subnet pool is exhausted; choose another unused RFC1918 pool')


@dataclasses.dataclass(frozen=True)
class Config:
    slug: str
    domain: str
    display_name: str
    revision: str
    state_root: Path = Path('/opt/bugzero-apps')
    sites_dir: Path = Path('/etc/caddy/sites')
    caddy_config: Path = Path('/etc/caddy/Caddyfile')
    bugzero_app_key: str = ''
    bugzero_widget_url: str = ''
    app_id: str = ''
    repo_url: str = ''
    network_pool: str = '10.240.0.0/16'
    ingress_http_port: int | None = None

    def __post_init__(self):
        network_pool(self.network_pool)
        if self.ingress_http_port is not None and not 1 <= self.ingress_http_port <= 65535:
            raise ValueError('The app ingress HTTP port must be between 1 and 65535')
        if self.app_id or self.repo_url:
            try:
                if str(uuid.UUID(self.app_id)) != self.app_id:
                    raise ValueError('Noncanonical app identity')
            except (ValueError, AttributeError):
                raise ValueError('APP_ID must be a canonical app UUID') from None
            if not re.fullmatch(r'https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', self.repo_url):
                raise ValueError('APP_REPOSITORY must be a canonical GitHub repository URL')
            if self.repo_url.rsplit('/', 1)[1] in {'.', '..'} or self.repo_url.endswith('.git'):
                raise ValueError('APP_REPOSITORY must be a canonical GitHub repository URL')
            object.__setattr__(self, 'repo_url', self.repo_url.lower())
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?', self.slug):
            raise ValueError('APP_NAME must be a lowercase app slug (1-48 characters)')
        if len(self.domain) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', self.domain):
            raise ValueError('APP_DOMAIN must be a plain lowercase DNS name')
        if not re.fullmatch(r'[0-9a-f]{40}', self.revision):
            raise ValueError('APP_REVISION must be an exact lowercase Git commit SHA')
        if not self.display_name or len(self.display_name) > 255 or any(ord(c) < 32 for c in self.display_name):
            raise ValueError('APP_DISPLAY_NAME must be a nonempty display name')
        if self.bugzero_app_key or self.bugzero_widget_url:
            script = urllib.parse.urlsplit(self.bugzero_widget_url)
            if not self.bugzero_app_key or len(self.bugzero_app_key) > 256 or script.scheme != 'https' or not script.hostname or script.username or script.password:
                raise ValueError('BugZero widget requires a public app key and HTTPS script URL')

    @property
    def app_dir(self):
        return self.state_root / self.slug

    @property
    def database_env(self):
        return self.app_dir / 'database.env'

    @property
    def recovery_marker(self):
        return self.app_dir / 'recovery-required.json'

    @property
    def site_path(self):
        return self.sites_dir / f'bugzero-{self.slug}.caddy'

    @property
    def ownership_marker(self):
        return f'# Managed by BugZero Rust template: {self.slug}'


class Deployment:
    def __init__(self, config: Config, runner=None, reload_caddy=None, verify_public=None, health_timeout=120):
        self.config = config
        self.secret_values: list[str] = []
        self.runner = runner or self.run
        self.reload_caddy = reload_caddy or self.system_reload
        self.health_timeout = health_timeout
        self.verify_public = verify_public or self.check_public

    def run(self, command, *, stdout=None, text=True):
        result = subprocess.run(command, check=False, stdout=stdout if stdout is not None else subprocess.PIPE,
                                stderr=subprocess.PIPE, text=text)
        if result.returncode:
            error = result.stderr if text else result.stderr.decode(errors='replace')
            for secret in self.secret_values:
                error = error.replace(secret, '[redacted]')
            raise RuntimeError(f'{command[0]} failed ({result.returncode}): {error[-2000:]}')
        return result

    def preflight(self):
        c = self.config
        identity_file = c.app_dir / 'ownership.json'
        retirement_file = c.app_dir / 'retirement.json'
        for path in [c.state_root, c.app_dir, c.database_env, c.sites_dir, c.site_path, c.recovery_marker,
                     identity_file, retirement_file, c.app_dir / 'deploy.lock']:
            if path.is_symlink():
                raise RuntimeError(f'Refusing symlink path: {path}')
        if retirement_file.exists():
            raise RuntimeError('App retirement has started; deployment is permanently disabled for this identity')
        if c.app_id or identity_file.exists():
            try:
                stat = identity_file.stat()
                owner = json.loads(identity_file.read_text())
            except (OSError, ValueError):
                raise RuntimeError('Missing or invalid host ownership identity; provision the app before deployment') from None
            expected = {'version': 1, 'stack_version': 'rust-axum-leptos-v1', 'app_id': c.app_id,
                        'app_name': c.slug, 'domain': c.domain, 'repo_url': c.repo_url}
            if (owner != expected or stat.st_nlink != 1 or stat.st_uid != os.geteuid()
                    or stat.st_mode & 0o022):
                raise RuntimeError('Host ownership identity does not match this deployment')
        if c.recovery_marker.exists():
            raise RuntimeError('Recovery required: reconcile active Caddy routing and current.json, then explicitly clear recovery-required.json before retrying')
        if c.site_path.exists() and c.site_path.read_text().splitlines()[0:1] != [c.ownership_marker]:
            raise RuntimeError('Existing Caddy site has no matching app ownership marker')
        c.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        c.app_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(c.app_dir, 0o700)
        c.sites_dir.mkdir(parents=True, exist_ok=True)

    def checked_network_path(self, path, *, missing=False):
        path = Path(path)
        for ancestor in [self.config.state_root, path.parent]:
            info = ancestor.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise RuntimeError('Untrusted app network reservation directory or symlink')
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise RuntimeError('Missing app network reservation') from None
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise RuntimeError('Untrusted app network reservation or lock: require a private owned regular file without links')
        return info

    def read_network_reservation(self, directory):
        path = directory / 'network-subnets.json'
        if self.checked_network_path(path, missing=True) is None:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            with os.fdopen(fd, 'r') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
                    raise ValueError()
                record = json.load(source)
            if set(record) != {'version', 'app_name', 'app_id', 'pool', 'subnets'} or record['version'] != 1 or record['app_name'] != directory.name:
                raise ValueError()
            if not isinstance(record['app_id'], str) or (record['app_id'] and str(uuid.UUID(record['app_id'])) != record['app_id']):
                raise ValueError()
            pool = network_pool(record['pool'])
            if set(record['subnets']) != set(NETWORK_ROLES):
                raise ValueError()
            subnets = [ipaddress.ip_network(record['subnets'][role], strict=True) for role in NETWORK_ROLES]
            if any(subnet.version != 4 or subnet.prefixlen != 28 or not subnet.subnet_of(pool)
                   or str(subnet) != record['subnets'][role] for role, subnet in zip(NETWORK_ROLES, subnets)):
                raise ValueError()
            if len(set(subnets)) != 3:
                raise ValueError()
            return record
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError('Invalid persisted app network reservation; manual repair required') from None

    def prepared_network_subnets(self):
        record = self.read_network_reservation(self.config.app_dir)
        if record is None:
            return {}
        if record['app_id'] != self.config.app_id or record['pool'] != self.config.network_pool:
            raise RuntimeError('Persisted app network reservation identity or pool changed')
        return record['subnets']

    def allocate_networks(self):
        """Called under deploy.lock; reserve before Compose creates any network.

        Every app shares this kernel lock. Persisted reservations survive crashes,
        absent Docker resources, so concurrent apps never race for
        the same small subnet. Docker's daemon-wide address pool is unchanged.
        """
        c = self.config
        lock_path = c.state_root / '.network-allocation.lock'
        self.checked_network_path(lock_path, missing=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            self.checked_network_path(lock_path)
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = self.prepared_network_subnets()
            occupied = []
            for directory in c.state_root.iterdir():
                if directory.is_symlink():
                    raise RuntimeError('Refusing symlink network reservation directory')
                if not directory.is_dir() or directory == c.app_dir:
                    continue
                record = self.read_network_reservation(directory)
                if record is not None:
                    occupied.extend(record['subnets'].values())
            identifiers = self.runner(['docker', 'network', 'ls', '--quiet', '--no-trunc']).stdout.split()
            networks = json.loads(self.runner(['docker', 'network', 'inspect', *identifiers]).stdout) if identifiers else []
            routes = json.loads(self.runner(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout)
            if not isinstance(networks, list) or not isinstance(routes, list):
                raise RuntimeError('Invalid Docker network or host route inventory')
            prefix = f'bugzero-{c.slug}'
            expected = {f'{prefix}-database': ('database', f'{prefix}-data', 'database'),
                        **{f'{prefix}-{slot}_default': (slot, f'{prefix}-{slot}', 'default') for slot in ('blue', 'green')}}
            projects = {value[1] for value in expected.values()}
            owned = []
            for network in networks:
                labels = network.get('Labels') or {}
                wanted = expected.get(network.get('Name'))
                subnets = [ipaddress.ip_network(entry['Subnet'], strict=False)
                           for entry in (network.get('IPAM') or {}).get('Config') or [] if entry.get('Subnet')]
                ipv4 = [subnet for subnet in subnets if subnet.version == 4]
                if wanted is not None or labels.get('com.docker.compose.project') in projects:
                    if (wanted is None or network.get('Driver') != 'bridge'
                            or (labels.get('com.docker.compose.project'), labels.get('com.docker.compose.network')) != wanted[1:]):
                        raise RuntimeError('Existing app network has foreign ownership labels or name')
                    if not existing:
                        raise RuntimeError('Existing app network has no persisted subnet reservation')
                    if len(ipv4) != 1 or str(ipv4[0]) != existing[wanted[0]]:
                        raise RuntimeError('Existing app network subnet does not match its persisted reservation')
                    bridge = (network.get('Options') or {}).get('com.docker.network.bridge.name') or 'br-' + network['Id'][:12]
                    owned.append((ipv4[0], bridge))
                else:
                    occupied.extend(str(subnet) for subnet in ipv4)
            for route in routes:
                destination = route.get('dst', 'default')
                if destination in ('default', '0.0.0.0/0'):
                    continue
                subnet = ipaddress.ip_network(destination, strict=False)
                if subnet.version != 4 or subnet.prefixlen == 0:
                    continue
                # Docker installs connected/local routes for each existing app
                # bridge. Only routes within that exact owned subnet are safe.
                if any(route.get('dev') == bridge and subnet.subnet_of(own) for own, bridge in owned):
                    continue
                occupied.append(str(subnet))
            if existing:
                if any(ipaddress.ip_network(value).overlaps(ipaddress.ip_network(other))
                       for value in existing.values() for other in occupied):
                    raise RuntimeError('Persisted app network subnet overlaps another reservation, Docker network or host route')
                return existing
            subnets = choose_network_subnets(c.network_pool, occupied)
            atomic_write(c.app_dir / 'network-subnets.json', json.dumps({
                'version': 1, 'app_name': c.slug, 'app_id': c.app_id, 'pool': c.network_pool,
                'subnets': subnets}, sort_keys=True) + '\n')
            return subnets

    def database_environment(self):
        c = self.config
        if c.database_env.exists():
            values = dict(line.split('=', 1) for line in c.database_env.read_text().splitlines() if line and not line.startswith('#'))
            if values.get('SLUG') != c.slug or not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', values.get('POSTGRES_PASSWORD', '')):
                raise RuntimeError('Invalid persisted database environment; manual repair required')
            if values.get('DATABASE_URL') != f"postgres://app:{values['POSTGRES_PASSWORD']}@db:5432/app":
                raise RuntimeError('Persisted database URL does not match this app database')
            os.chmod(c.database_env, 0o600)
        else:
            password = secrets.token_urlsafe(48)
            values = {'SLUG': c.slug, 'POSTGRES_PASSWORD': password,
                      'DATABASE_URL': f'postgres://app:{password}@db:5432/app'}
            atomic_write(c.database_env, ''.join(f'{key}={value}\n' for key, value in values.items()))
        subnets = self.prepared_network_subnets()
        if subnets:
            if values.get('APP_DATABASE_SUBNET', subnets['database']) != subnets['database']:
                raise RuntimeError('Persisted database subnet changed; manual repair required')
            values['APP_DATABASE_SUBNET'] = subnets['database']
            atomic_write(c.database_env, ''.join(f'{key}={value}\n' for key, value in values.items()))
        self.secret_values = [values['POSTGRES_PASSWORD'], values['DATABASE_URL']]
        return values

    def slot_environment(self, slot):
        if slot not in {'blue', 'green'}:
            raise ValueError('Invalid deployment slot')
        c = self.config
        directory = c.app_dir / slot
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise RuntimeError('Refusing symlink slot directory')
        config_file = directory / 'app-config.json'
        manifest = directory / 'qa-release-manifest.json'
        runtime = {'display_name': c.display_name, 'domain': c.domain, 'revision': c.revision}
        if c.bugzero_app_key:
            runtime.update({'bugzero_app_key': c.bugzero_app_key, 'bugzero_widget_url': c.bugzero_widget_url})
        atomic_write(config_file, json.dumps(runtime) + '\n', 0o644)
        atomic_write(manifest, json.dumps({'revision': c.revision, 'stack': 'rust-axum-leptos'}) + '\n', 0o644)
        values = self.database_environment() | {
            'APP_REVISION': c.revision, 'API_IMAGE': f'bugzero-{c.slug}-api:{c.revision}',
            'WEB_IMAGE': f'bugzero-{c.slug}-web:{c.revision}',
            'APP_CONFIG_PATH': str(config_file), 'RELEASE_MANIFEST_PATH': str(manifest),
        }
        subnets = self.prepared_network_subnets()
        if subnets:
            values['APP_SLOT_SUBNET'] = subnets[slot]
        atomic_write(directory / 'runtime.env', ''.join(f'{key}={value}\n' for key, value in values.items()))
        return values

    def compose(self, kind, *arguments, slot=None, stdout=None, text=True):
        c = self.config
        env_file = c.database_env if kind == 'data' else c.app_dir / slot / 'runtime.env'
        project = f'bugzero-{c.slug}-data' if kind == 'data' else f'bugzero-{c.slug}-{slot}'
        command = ['docker', 'compose', '--env-file', str(env_file), '--project-name', project,
                   '--file', str(ROOT / f'compose.{kind}.yml'), *arguments]
        return self.runner(command, stdout=stdout, text=text)

    def backup_database(self):
        directory = self.config.app_dir / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        backup = directory / f'{time.time_ns()}-before-{self.config.revision[:12]}.dump'
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as output:
                self.compose('data', 'exec', '-T', 'db', 'pg_dump', '-U', 'app', '-d', 'app', '--format=custom', stdout=output, text=False)
            if backup.stat().st_size == 0:
                raise RuntimeError('Database backup was empty; migration cancelled')
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        return backup

    def assert_health(self, payload):
        if payload.get('status') != 'healthy' or payload.get('revision') != self.config.revision:
            raise RuntimeError('Candidate database health or revision did not match')

    def check_candidate(self, port):
        deadline = time.monotonic() + self.health_timeout
        while True:
            try:
                def read(path):
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=3) as response:
                        return json.load(response)
                self.assert_health(read('/api/health/db'))
                for path in ['/app-config.json', '/qa-release-manifest.json']:
                    if read(path).get('revision') != self.config.revision:
                        raise RuntimeError('Candidate frontend revision did not match')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=3) as response:
                    if response.status != 200 or b'<html' not in response.read().lower():
                        raise RuntimeError('Candidate frontend did not serve HTML')
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Candidate failed frontend/API/database/revision health checks') from None
                time.sleep(1)

    def check_public(self):
        deadline = time.monotonic() + self.health_timeout
        while True:
            try:
                for route in ['/api/health/db', '/app-config.json', '/qa-release-manifest.json']:
                    with urllib.request.urlopen(f'https://{self.config.domain}{route}', timeout=5) as response:
                        payload = json.load(response)
                    if payload.get('revision') != self.config.revision:
                        raise RuntimeError('Public release revision did not match candidate')
                    if route == '/api/health/db':
                        self.assert_health(payload)
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Public HTTPS frontend/API/database/revision checks failed') from None
                time.sleep(2)

    def system_reload(self):
        self.runner(['systemctl', 'reload', 'caddy'])

    def switch_site(self, port, commit=lambda: None):
        # App releases share one Caddy process. Serialize site validation and reload globally.
        with open(self.config.sites_dir / '.bugzero-deploy.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.preflight()
            self._switch_site(port, commit)

    def _switch_site(self, port, commit):
        c = self.config
        previous = c.site_path.read_text() if c.site_path.exists() else None
        address = c.domain if c.ingress_http_port is None else f'http://{c.domain}:{c.ingress_http_port}'
        candidate = f'{c.ownership_marker}\n{address} {{\n    encode zstd gzip\n    reverse_proxy 127.0.0.1:{int(port)}\n}}\n'
        # Persist before touching routing so even a later disk failure or process crash blocks retries.
        atomic_write(c.recovery_marker, json.dumps({'reason': 'promotion_in_progress', 'candidate_revision': c.revision,
                                                  'candidate_port': int(port)}) + '\n')
        commit_started = False
        try:
            atomic_write(c.site_path, candidate, 0o644)
            self.runner(['caddy', 'validate', '--adapter', 'caddyfile', '--config', str(c.caddy_config)])
            self.reload_caddy()
            self.verify_public()
            commit_started = True
            commit()
        except BaseException as promotion_error:
            try:
                if previous is None:
                    c.site_path.unlink(missing_ok=True)
                else:
                    atomic_write(c.site_path, previous, 0o644)
                self.reload_caddy()
            except Exception:
                raise RollbackFailed('Candidate promotion failed and Caddy rollback failed; both slots preserved for recovery') from promotion_error
            if commit_started:
                raise RollbackFailed('Release state commit failed; routing restored but both slots preserved for recovery') from promotion_error
            c.recovery_marker.unlink()
            raise
        try:
            c.recovery_marker.unlink()
        except OSError as error:
            raise RollbackFailed('Healthy release committed but recovery marker could not be cleared; both slots preserved') from error

    def deploy(self):
        c = self.config
        self.preflight()
        with open(c.app_dir / 'deploy.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Recheck ownership after acquiring the per-app deployment lock.
            self.preflight()
            self.allocate_networks()
            self.database_environment()
            current_file = c.app_dir / 'current.json'
            previous = json.loads(current_file.read_text()) if current_file.exists() else None
            if previous and previous.get('slot') not in {'blue', 'green'}:
                raise RuntimeError('Invalid active deployment slot')
            slot = 'green' if previous and previous['slot'] == 'blue' else 'blue'
            self.slot_environment(slot)
            self.compose('data', 'up', '-d', '--wait', '--wait-timeout', '120', 'db')
            backup = self.backup_database()
            print(f'Database backup completed: {backup.name}', flush=True)
            # Migrations must remain compatible with the prior live revision.
            self.compose('slot', 'run', '--rm', '--no-deps', 'api', '--migrate-only', slot=slot)
            try:
                self.compose('slot', 'up', '-d', '--remove-orphans', slot=slot)
                result = self.compose('slot', 'port', 'web', '8080', slot=slot)
                address = result.stdout.strip()
                if not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', address):
                    raise RuntimeError('Candidate frontend must publish only on IPv4 loopback')
                port = int(address.rsplit(':', 1)[1])
                self.check_candidate(port)
                self.switch_site(port, commit=lambda: atomic_write(current_file, json.dumps(
                    {'slot': slot, 'revision': c.revision, 'port': port, 'domain': c.domain, 'backup': str(backup)}) + '\n'))
            except RollbackFailed:
                # Caddy may still route the candidate after a failed rollback reload.
                raise
            except BaseException:
                # The previously active slot and its data are never stopped on failure.
                try:
                    self.compose('slot', 'stop', slot=slot)
                except Exception:
                    print('Candidate cleanup failed; active release and database are preserved', file=sys.stderr)
                raise
            if previous:
                # Preserve its containers/images/config for manual rollback; stop only after promotion.
                try:
                    self.compose('slot', 'stop', slot=previous['slot'])
                except Exception:
                    print('Promoted healthy candidate; prior slot could not be stopped', file=sys.stderr)
            print(f'Deployed {c.slug} revision {c.revision} on {slot}: https://{c.domain}', flush=True)
            return {'slot': slot, 'revision': c.revision, 'port': port}


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run on the VPS with sudo -n; app state and Caddy configuration require root ownership')
    os.umask(0o077)
    ingress_file = Path('/etc/bugzero/app-ingress-http-port')
    ingress_port = int(ingress_file.read_text().strip()) if ingress_file.exists() else None
    c = Config(slug=os.environ.get('APP_NAME', ''), domain=os.environ.get('APP_DOMAIN', ''),
               display_name=os.environ.get('APP_DISPLAY_NAME', ''), revision=os.environ.get('APP_REVISION', ''),
               bugzero_app_key=os.environ.get('BUGZERO_APP_KEY', ''), bugzero_widget_url=os.environ.get('BUGZERO_WIDGET_URL', ''),
               app_id=os.environ.get('APP_ID', ''), repo_url=os.environ.get('APP_REPOSITORY', ''),
               network_pool=os.environ.get('BUGZERO_NETWORK_POOL', '10.240.0.0/16'),
               ingress_http_port=ingress_port)
    if not c.app_id or not c.repo_url:
        raise RuntimeError('Managed deployment requires APP_ID and APP_REPOSITORY from BugZero')
    if 'import /etc/caddy/sites/*.caddy' not in c.caddy_config.read_text():
        raise RuntimeError('VPS Caddyfile must import /etc/caddy/sites/*.caddy before app deployment')
    deployment = Deployment(c)
    if sys.argv[1:] == ['--preflight-only']:
        deployment.preflight()
    elif sys.argv[1:]:
        raise RuntimeError('Unknown deployment arguments')
    else:
        deployment.deploy()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Deployment failed: {error}', file=sys.stderr)
        sys.exit(1)
