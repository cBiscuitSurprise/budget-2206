"""Push deploy/ configs to the EC2 host over SSM and restart Nebula + the Actual stack."""

import base64
import json
import subprocess
import time
from pathlib import Path

import click

REPO = Path(__file__).resolve().parent.parent
FILES = {
    "/etc/nebula/config.yml": "deploy/nebula/lighthouse.yml",
    "/etc/systemd/system/nebula.service": "deploy/nebula/nebula.service",
    "/opt/actual/docker-compose.yml": "deploy/actual-server.compose.yml",
    "/opt/actual/caddy/Dockerfile": "deploy/caddy/Dockerfile",
    "/opt/actual/caddy/Caddyfile": "deploy/caddy/Caddyfile",
    "/opt/actual/caddy/aws-config": "deploy/caddy/aws-config",
}
CADDY_IMAGE = "local/caddy-route53:2.11.7"


def _script() -> str:
    lines = ["set -euo pipefail", "install -d -m 755 /opt/actual/data /opt/actual/caddy"]
    for dst, src in FILES.items():
        lines.append(f"echo {base64.b64encode((REPO / src).read_bytes()).decode()} | base64 -d > {dst}")
    lines += [
        "chmod 600 /etc/nebula/config.yml",
        "systemctl daemon-reload",
        "systemctl restart nebula",
        "cd /opt/actual",
        f"DOCKER_BUILDKIT=0 docker build -q -t {CADDY_IMAGE} caddy 2>&1 | tail -2",
        "docker compose up -d 2>&1",
        "docker compose restart caddy 2>&1",
        "sleep 20",
        "docker compose ps",
    ]
    return "\n".join(lines)


def _aws(profile: str, region: str, *args: str) -> str:
    cmd = ["aws", *args, "--profile", profile, "--region", region, "--output", "text"]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


@click.command()
@click.option("--instance", default="i-04b5b1d767b6a9bd5", show_default=True)
@click.option("--profile", default="ch-dev", show_default=True)
@click.option("--region", default="us-east-2", show_default=True)
def main(instance: str, profile: str, region: str) -> None:
    params = json.dumps({"commands": [_script()], "executionTimeout": ["1800"]})
    cid = _aws(
        profile, region, "ssm", "send-command", "--instance-ids", instance, "--document-name", "AWS-RunShellScript",
        "--timeout-seconds", "1800", "--parameters", params, "--query", "Command.CommandId",
    )
    click.echo(f"ssm command {cid}")
    invocation = ["ssm", "get-command-invocation", "--command-id", cid, "--instance-id", instance]
    status = "Pending"
    while status in ("Pending", "InProgress", "Delayed"):
        time.sleep(10)
        try:
            status = _aws(profile, region, *invocation, "--query", "Status")
        except subprocess.CalledProcessError:
            continue
    click.echo(_aws(profile, region, *invocation, "--query", "[StandardOutputContent,StandardErrorContent]"))
    if status != "Success":
        raise click.ClickException(status)


if __name__ == "__main__":
    main()
