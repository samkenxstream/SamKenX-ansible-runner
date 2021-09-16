import os
import shutil
import time
import json
from glob import glob
from uuid import uuid4

import pytest

from ansible_runner.interface import run


def is_running(cli, container_runtime_installed, container_name):
    cmd = [container_runtime_installed, 'ps', '-aq', '--filter', 'name=ansible_runner_foo_bar']
    r = cli(cmd, bare=True)
    output = '{}{}'.format(r.stdout, r.stderr)
    print(' '.join(cmd))
    print(output)
    return output.strip()


class CancelStandIn:
    def __init__(self, runtime, cli, delay=0.2):
        self.runtime = runtime
        self.cli = cli
        self.delay = 0.2
        self.checked_running = False
        self.start_time = None

    def cancel(self):
        # Avoid checking for some initial delay to allow container startup
        if not self.start_time:
            self.start_time = time.time()
        if time.time() - self.start_time < self.delay:
            return False
        # guard against false passes by checking for running container
        if not self.checked_running:
            for i in range(5):
                if is_running(self.cli, self.runtime, 'ansible_runner_foo_bar'):
                    break
                time.sleep(0.2)
            else:
                print(self.cli([self.runtime, 'ps', '-a'], bare=True).stdout)
                raise Exception('Never spawned expected container')
            self.checked_running = True
        # Established that container was running, now we cancel job
        return True


@pytest.mark.serial
def test_cancel_will_remove_container(test_data_dir, container_runtime_installed, cli):
    private_data_dir = os.path.join(test_data_dir, 'sleep')

    env_dir = os.path.join(private_data_dir, 'env')
    if os.path.exists(env_dir):
        shutil.rmtree(env_dir)

    cancel_standin = CancelStandIn(container_runtime_installed, cli)

    res = run(
        private_data_dir=private_data_dir,
        playbook='sleep.yml',
        settings={
            'process_isolation_executable': container_runtime_installed,
            'process_isolation': True
        },
        cancel_callback=cancel_standin.cancel,
        ident='foo?bar'  # question mark invalid char, but should still work
    )
    assert res.rc == 254, res.stdout.read()
    assert res.status == 'canceled'

    assert not is_running(
        cli, container_runtime_installed, 'ansible_runner_foo_bar'
    ), 'Found a running container, they should have all been stopped'


@pytest.mark.parametrize('runtime', ['podman', 'docker'])
def test_invalid_registry_host(tmp_path, runtime):
    if shutil.which(runtime) is None:
        pytest.skip(f'{runtime} is unavaialble')
    pdd_path = tmp_path / 'private_data_dir'
    pdd_path.mkdir()
    private_data_dir = str(pdd_path)

    image_name = 'quay.io/kdelee/does-not-exist'

    res = run(
        private_data_dir=private_data_dir,
        playbook='ping.yml',
        settings={
            'process_isolation_executable': runtime,
            'process_isolation': True,
            'container_image': image_name,
            'container_options': ['--user=root', '--pull=always'],
        },
        container_auth_data={'host': 'somedomain.invalid', 'username': 'foouser', 'password': '349sk34', 'verify_ssl': False},
        ident='awx_123'
    )
    assert res.status == 'failed'
    assert res.rc > 0
    assert os.path.exists(res.config.registry_auth_path)

    result_stdout = res.stdout.read()
    auth_file_path = os.path.join(res.config.registry_auth_path, 'config.json')
    registry_conf = os.path.join(res.config.registry_auth_path, 'registries.conf')
    error_msg = 'access to the requested resource is not authorized'
    if runtime == 'podman':
        assert image_name in result_stdout
        error_msg = 'unauthorized'
        auth_file_path = res.config.registry_auth_path
        registry_conf = os.path.join(os.path.dirname(res.config.registry_auth_path), 'registries.conf')
        
    assert error_msg in result_stdout


    with open(auth_file_path, 'r') as f:
        content = f.read()
        assert res.config.container_auth_data['host'] in content
        assert 'Zm9vdXNlcjozNDlzazM0' in content  # the b64 encoded of username and password

    assert os.path.exists(registry_conf)
    with open(registry_conf, 'r') as f:
        assert f.read() == '\n'.join([
            '[[registry]]',
            'location = "somedomain.invalid"',
            'insecure = true'
        ])


@pytest.mark.parametrize('runtime', ['podman', 'docker'])
def test_registry_auth_file_cleanup(tmp_path, cli, runtime):
    if shutil.which(runtime) is None:
        pytest.skip(f'{runtime} is unavaialble')
    pdd_path = tmp_path / 'private_data_dir'
    pdd_path.mkdir()
    private_data_dir = str(pdd_path)

    auth_registry_glob = '/tmp/ansible_runner_registry_*'
    registry_files_before = set(glob(auth_registry_glob))

    settings_data = {
        'process_isolation_executable': runtime,
        'process_isolation': True,
        'container_image': 'quay.io/kdelee/does-not-exist',
        'container_options': ['--user=root', '--pull=always'],
        'container_auth_data': {'host': 'https://somedomain.invalid', 'username': 'foouser', 'password': '349sk34'},
    }

    os.mkdir(os.path.join(private_data_dir, 'env'))
    with open(os.path.join(private_data_dir, 'env', 'settings'), 'w') as f:
        f.write(json.dumps(settings_data, indent=2))

    this_ident = str(uuid4())[:5]

    cli(['run', private_data_dir, '--ident', this_ident, '-p', 'ping.yml'], check=False)

    discovered_registry_files = set(glob(auth_registry_glob)) - registry_files_before
    for file_name in discovered_registry_files:
        assert this_ident not in file_name
