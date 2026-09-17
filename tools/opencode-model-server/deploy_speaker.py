#!/usr/bin/env python3
"""Deploy the dynamic deepseek client to an OH2P speaker over SSH.

The password is read from SPEAKER_PASSWORD so it is not placed in the command
line or in a repository file. Requires paramiko only for deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError as error:  # pragma: no cover - deployment-only helper
    raise SystemExit("Install paramiko first: python -m pip install paramiko") from error


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="speaker IP or hostname")
    parser.add_argument("--binary", required=True, type=Path, help="compiled deepseek binary")
    parser.add_argument("--server-url", required=True, help="LAN URL of the model server")
    args = parser.parse_args()

    password = os.environ.get("SPEAKER_PASSWORD")
    if not password:
        raise SystemExit("SPEAKER_PASSWORD is required")
    binary = args.binary.read_bytes()
    local_hash = sha256(binary)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            args.host,
            port=22,
            username="root",
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )

        def command(command: str) -> tuple[int, str, str]:
            stdin, stdout, stderr = client.exec_command(command, timeout=30)
            stdin.close()
            return stdout.channel.recv_exit_status(), stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")

        status, _, error = command("mkdir -p /data/open-xiaoai")
        if status != 0:
            raise RuntimeError(f"create remote directory failed: {error.strip()}")

        remote_binary = "/data/open-xiaoai/deepseek.new"
        stdin, stdout, stderr = client.exec_command(f"cat > {remote_binary}", timeout=60)
        stdin.write(binary)
        stdin.channel.shutdown_write()
        status = stdout.channel.recv_exit_status()
        error = stderr.read().decode(errors="replace")
        if status != 0:
            raise RuntimeError(f"upload failed: {error.strip()}")

        status, output, error = command(f"sha256sum {remote_binary}")
        if status != 0:
            raise RuntimeError(f"remote checksum failed: {error.strip()}")
        remote_hash = output.split()[0].lower()
        if remote_hash != local_hash:
            raise RuntimeError(f"checksum mismatch: local={local_hash} remote={remote_hash}")
        print(f"checksum verified: {local_hash}")

        status, _, error = command(
            "chmod +x /data/open-xiaoai/deepseek.new && "
            "mv /data/open-xiaoai/deepseek.new /data/open-xiaoai/deepseek && "
            "printf '%s\\n' '" + args.server_url.replace("'", "'\\''") + "' > /data/open-xiaoai/model-server-url.new && "
            "mv /data/open-xiaoai/model-server-url.new /data/open-xiaoai/model-server-url"
        )
        if status != 0:
            raise RuntimeError(f"activate deployment failed: {error.strip()}")

        restart = (
            "/sbin/start-stop-daemon -K -q -p /tmp/open-xiaoai-deepseek.pid || true; "
            "rm -f /tmp/open-xiaoai-deepseek.pid; "
            "/sbin/start-stop-daemon -S -b -m -p /tmp/open-xiaoai-deepseek.pid "
            "-x /data/open-xiaoai/deepseek"
        )
        status, _, error = command(restart)
        if status != 0:
            raise RuntimeError(f"restart failed: {error.strip()}")

        status, output, error = command(
            "printf 'arch='; uname -m; "
            "printf 'config='; cat /data/open-xiaoai/model-server-url; "
            "printf 'pid='; cat /tmp/open-xiaoai-deepseek.pid 2>/dev/null || true; "
            "ps | grep '/data/open-xiaoai/deepseek' | grep -v grep || true"
        )
        if status != 0:
            raise RuntimeError(f"verification failed: {error.strip()}")
        print(output.strip())
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
