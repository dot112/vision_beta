"""The optional Mosquitto service in docker-compose.yml: opt-in, and never open to everyone."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_the_broker_service_is_opt_in_and_needs_a_login():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    mqtt = compose["services"]["mqtt"]
    # `docker compose up -d` without the profile starts what it started before.
    assert mqtt["profiles"] == ["broker"] and "profiles" not in compose["services"]["vision"]
    assert "depends_on" not in compose["services"]["vision"]
    assert mqtt["image"].startswith("eclipse-mosquitto:2") and mqtt["restart"] == "unless-stopped"
    assert mqtt["ports"] == ["${BROKER_BIND_ADDRESS:-0.0.0.0}:${BROKER_PORT:-1883}:1883"]
    assert "vision-broker" in compose["volumes"] and "vision-broker:/mosquitto/data" in mqtt["volumes"]
    assert "no-new-privileges:true" in mqtt["security_opt"] and mqtt["mem_limit"] and mqtt["logging"]["options"]["max-size"]
    assert mqtt["healthcheck"]["test"]
    # Started the way the image starts itself: as root, which can always read the
    # two mounted files and the volume, and the broker then gives root up.
    assert "user" not in mqtt and "cap_drop" not in mqtt
    start = (ROOT / "docker" / "mosquitto" / "start.sh").read_text(encoding="utf-8")
    assert "chown -R mosquitto:mosquitto /mosquitto/data" in start
    assert mqtt["environment"]["BROKER_PASSWORD"] == "${BROKER_PASSWORD:-}"

    conf = (ROOT / "docker" / "mosquitto" / "mosquitto.conf").read_text(encoding="utf-8")
    settings = [line.strip() for line in conf.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert "allow_anonymous false" in settings and "listener 1883" in settings
    assert "password_file /mosquitto/data/passwd" in settings and "persistence true" in settings

    # A shell script with Windows line endings does not run in the container.
    assert b"\r" not in (ROOT / "docker" / "mosquitto" / "start.sh").read_bytes()
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")

    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert all(name in env for name in ("BROKER_USERNAME", "BROKER_PASSWORD", "BROKER_PORT", "BROKER_BIND_ADDRESS"))


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_the_start_script_refuses_an_empty_password():
    script = "docker/mosquitto/start.sh"
    for password in ("", None):
        env = {**os.environ, "BROKER_USERNAME": "vision"}
        env.pop("BROKER_PASSWORD", None)
        if password is not None:
            env["BROKER_PASSWORD"] = password
        run = subprocess.run(["sh", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
        assert run.returncode != 0 and "BROKER_PASSWORD" in run.stderr, (run.returncode, run.stderr)
