"""Exercise restricted OpenSSH forwarding against a disposable local sshd."""

import getpass
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health/live":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alive")

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen[bytes]) -> None:
    for _ in range(50):
        if process.poll() is not None:
            pytest.fail("disposable sshd exited before listening")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail("disposable sshd did not listen")


def test_ssh_account_allows_only_authorized_loopback_forward(tmp_path: Path) -> None:
    try:
        http = ThreadingHTTPServer(("127.0.0.1", 8000), HealthHandler)
    except OSError as error:
        pytest.skip(f"isolated loopback port 8000 unavailable: {error}")
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()
    sshd: subprocess.Popen[bytes] | None = None
    tunnel: subprocess.Popen[bytes] | None = None
    try:
        client_key = tmp_path / "client_key"
        host_key = tmp_path / "host_key"
        for path in (client_key, host_key):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                check=True,
                capture_output=True,
                timeout=10,
            )
        authorized = tmp_path / "authorized_keys"
        authorized.write_text(
            'from="127.0.0.1",restrict,port-forwarding,'
            'permitopen="127.0.0.1:8000" '
            + client_key.with_suffix(".pub").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        ssh_port = free_port()
        config = tmp_path / "sshd_config"
        config.write_text(
            f"ListenAddress 127.0.0.1\nPort {ssh_port}\n"
            f"HostKey {host_key}\nAuthorizedKeysFile {authorized}\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            "PubkeyAuthentication yes\nUsePAM no\nStrictModes no\n"
            "Subsystem sftp internal-sftp\n"
            f"Match User {getpass.getuser()}\n"
            "    AuthenticationMethods publickey\n"
            "    MaxSessions 0\n"
            "    AllowTcpForwarding local\n"
            "    AllowStreamLocalForwarding no\n"
            "    PermitOpen 127.0.0.1:8000\n"
            "    PermitTTY no\n"
            "    PermitUserRC no\n"
            "    AllowAgentForwarding no\n"
            "    X11Forwarding no\n"
            "    PermitTunnel no\n",
            encoding="utf-8",
        )
        subprocess.run(["/usr/sbin/sshd", "-t", "-f", str(config)], check=True, timeout=5)
        effective = subprocess.run(
            [
                "/usr/sbin/sshd",
                "-T",
                "-f",
                str(config),
                "-C",
                f"user={getpass.getuser()},host=127.0.0.1,addr=127.0.0.1",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for setting in (
            "maxsessions 0",
            "allowtcpforwarding local",
            "permitopen 127.0.0.1:8000",
            "allowstreamlocalforwarding no",
            "passwordauthentication no",
        ):
            assert setting in effective
        sshd = subprocess.Popen(
            ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for_port(ssh_port, sshd)
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text(
            f"[127.0.0.1]:{ssh_port} "
            + host_key.with_suffix(".pub").read_text(encoding="utf-8").split(" ", 2)[0]
            + " "
            + host_key.with_suffix(".pub").read_text(encoding="utf-8").split(" ", 2)[1]
            + "\n",
            encoding="utf-8",
        )
        base = [
            "ssh",
            "-F",
            "/dev/null",
            "-p",
            str(ssh_port),
            "-i",
            str(client_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ExitOnForwardFailure=yes",
        ]
        target = f"{getpass.getuser()}@127.0.0.1"
        local_port = free_port()
        tunnel = subprocess.Popen(
            [*base, "-N", "-T", "-L", f"127.0.0.1:{local_port}:127.0.0.1:8000", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for_port(local_port, tunnel)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{local_port}/health/live", timeout=2
        ) as response:
            assert response.read() == b"alive"
        assert (
            subprocess.run(
                [*base, target, "echo", "forbidden"], capture_output=True, timeout=5
            ).returncode
            != 0
        )
        assert (
            subprocess.run(
                [
                    "sftp",
                    "-F",
                    "/dev/null",
                    "-P",
                    str(ssh_port),
                    "-i",
                    str(client_key),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={known_hosts}",
                    "-b",
                    "/dev/null",
                    target,
                ],
                capture_output=True,
                timeout=5,
            ).returncode
            != 0
        )
        assert (
            subprocess.run(
                [*base, "-R", "127.0.0.1:0:127.0.0.1:8000", target, "true"],
                capture_output=True,
                timeout=5,
            ).returncode
            != 0
        )
        denied_port = free_port()
        denied = subprocess.Popen(
            [*base, "-N", "-T", "-L", f"127.0.0.1:{denied_port}:127.0.0.1:8001", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_port(denied_port, denied)
            with pytest.raises((OSError, urllib.error.URLError)):
                urllib.request.urlopen(f"http://127.0.0.1:{denied_port}/health/live", timeout=2)
        finally:
            denied.terminate()
            denied.wait(timeout=5)
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait(timeout=5)
        if sshd is not None:
            sshd.terminate()
            sshd.wait(timeout=5)
        http.shutdown()
        http.server_close()


@pytest.mark.docker
def test_container_tunnel_uses_ssh_and_reaches_only_isolated_target(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker unavailable")
    if subprocess.run(
        ["docker", "image", "inspect", "ip-proxy-pool:local"],
        capture_output=True,
        timeout=10,
    ).returncode:
        pytest.skip("build ip-proxy-pool:local before container tunnel acceptance")

    def docker(*arguments: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            pytest.fail(f"Docker peer tunnel step failed: {arguments[0]}")
        return result

    suffix = uuid4().hex[:10]
    network = f"peer-test-{suffix}"
    target = f"peer-target-{suffix}"
    tunnel_name = f"peer-tunnel-{suffix}"
    image_name = f"peer-sshd-test:{suffix}"
    root = Path(__file__).parents[2]
    docker("build", "-f", "tests/fixtures/peer-sshd.Dockerfile", "-t", image_name, ".", timeout=180)
    docker("network", "create", "--internal", network)
    try:
        key = tmp_path / "id_ed25519"
        host_key = tmp_path / "host_key"
        for path in (key, host_key):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                check=True,
                capture_output=True,
                timeout=10,
            )
        (tmp_path / "authorized_keys").write_text(
            'restrict,port-forwarding,permitopen="127.0.0.1:8000" '
            + key.with_suffix(".pub").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (tmp_path / "sshd_config").write_text(
            "ListenAddress 0.0.0.0\nPort 22\n"
            "HostKey /run/peer-test/host_key\n"
            "AuthorizedKeysFile /run/peer-test/authorized_keys\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            "PubkeyAuthentication yes\nUsePAM no\nStrictModes no\n"
            "Subsystem sftp internal-sftp\n"
            "Match User proxy-peer\n"
            "    AuthenticationMethods publickey\n"
            "    MaxSessions 0\n"
            "    AllowTcpForwarding local\n"
            "    AllowStreamLocalForwarding no\n"
            "    PermitOpen 127.0.0.1:8000\n"
            "    PermitTTY no\n",
            encoding="utf-8",
        )
        (tmp_path / "client.conf").write_text(
            "Host peer-export\n"
            f"    HostName {target}\n"
            "    User proxy-peer\n"
            "    IdentityFile /run/peer-ssh/id_ed25519\n"
            "    UserKnownHostsFile /run/peer-ssh/known_hosts\n"
            "    GlobalKnownHostsFile /dev/null\n"
            "    UpdateHostKeys no\n",
            encoding="utf-8",
        )
        host_fields = host_key.with_suffix(".pub").read_text(encoding="utf-8").split()
        (tmp_path / "known_hosts").write_text(
            f"{target} {host_fields[0]} {host_fields[1]}\n", encoding="utf-8"
        )
        web_root = tmp_path / "www" / "health"
        web_root.mkdir(parents=True)
        (web_root / "live").write_text("alive", encoding="utf-8")

        docker(
            "run",
            "-d",
            "--name",
            target,
            "--network",
            network,
            "-v",
            f"{tmp_path}:/run/peer-test:ro",
            "--entrypoint",
            "/bin/sh",
            image_name,
            "-c",
            "python -m http.server 8000 --bind 127.0.0.1 --directory /run/peer-test/www "
            ">/dev/null 2>&1 & exec /usr/sbin/sshd -D -e -f /run/peer-test/sshd_config",
        )
        docker(
            "run",
            "-d",
            "--name",
            tunnel_name,
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{tmp_path / 'client.conf'}:/run/peer-ssh/config:ro",
            "-v",
            f"{key}:/run/peer-ssh/id_ed25519:ro",
            "-v",
            f"{tmp_path / 'known_hosts'}:/run/peer-ssh/known_hosts:ro",
            "--entrypoint",
            "/usr/bin/ssh",
            "ip-proxy-pool:local",
            "-N",
            "-T",
            "-F",
            "/run/peer-ssh/config",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            "0.0.0.0:8000:127.0.0.1:8000",
            "peer-export",
        )
        for _ in range(25):
            probe = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    network,
                    "--entrypoint",
                    "python",
                    "ip-proxy-pool:local",
                    "-c",
                    "import urllib.request; assert urllib.request.urlopen("
                    f"'http://{tunnel_name}:8000/health/live', timeout=2).read()==b'alive'",
                ],
                capture_output=True,
                timeout=8,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.2)
        else:
            pytest.fail("isolated container tunnel never became healthy")
        assert docker("inspect", tunnel_name, "--format", "{{.Path}}").stdout.strip() == (
            "/usr/bin/ssh"
        )
        assert docker("port", tunnel_name).stdout.strip() == ""
    finally:
        subprocess.run(["docker", "rm", "-f", tunnel_name, target], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
        subprocess.run(["docker", "image", "rm", image_name], capture_output=True)
