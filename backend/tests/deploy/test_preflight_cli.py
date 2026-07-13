import subprocess
import sys

from app.deploy.preflight import _port_available


def test_preflight_module_entrypoint_fails_closed_without_env() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.deploy.preflight", "startup"],
        capture_output=True,
        text=True,
        env={},
        check=False,
    )

    assert result.returncode == 10
    assert "ENV_FILE_NOT_CONFIGURED" in result.stdout


def test_port_probe_detects_an_actual_listener() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert _port_available("127.0.0.1", port) is False
