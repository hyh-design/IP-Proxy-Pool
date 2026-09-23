"""Exercise restricted OpenSSH forwarding against a disposable local sshd."""

import getpass
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
