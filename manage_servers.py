#!/usr/bin/env python3
"""Build, start, and stop the ResourceUpload Docker server stack."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Server:
    key: str
    label: str
    compose_dir: Path
    default_port: int
    port_env: str
    health_env: str
    build_services: tuple[str, ...] = ()

    def health_url(self) -> str:
        configured = os.environ.get(self.health_env, "").strip()
        if configured:
            return configured

        host = os.environ.get("SERVERCTL_HEALTH_HOST", "localhost").strip() or "localhost"
        port = os.environ.get(self.port_env, str(self.default_port)).strip() or str(self.default_port)
        return f"http://{host}:{port}/health"


SERVERS: tuple[Server, ...] = (
    Server(
        key="search",
        label="SearchServer",
        compose_dir=REPO_ROOT / "SearchServer",
        default_port=8000,
        port_env="SEARCH_SERVER_PORT",
        health_env="SEARCH_SERVER_HEALTH_URL",
        build_services=("postgres", "reranker", "api"),
    ),
    Server(
        key="renderer",
        label="preview_renderer",
        compose_dir=REPO_ROOT / "preview_renderer",
        default_port=8200,
        port_env="PR_PORT",
        health_env="PREVIEW_RENDERER_HEALTH_URL",
    ),
    Server(
        key="processor",
        label="resource_processing_server",
        compose_dir=REPO_ROOT / "resource_processing_server",
        default_port=8100,
        port_env="RP_PORT",
        health_env="RESOURCE_PROCESSING_SERVER_HEALTH_URL",
    ),
)
SERVER_BY_KEY = {server.key: server for server in SERVERS}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build/start/stop the three ResourceUpload server compose projects.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "action",
        choices=("build", "compile", "start", "up", "stop", "down", "restart", "status"),
        help="Operation to run for the selected services.",
    )
    parser.add_argument(
        "services",
        nargs="*",
        choices=tuple(SERVER_BY_KEY),
        help="Optional subset: search, renderer, processor. Defaults to all three.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="For start/restart, build images before starting containers.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="For start/restart, do not wait for /health endpoints.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Seconds to wait for each selected service health endpoint.",
    )
    parser.add_argument(
        "--volumes",
        "-v",
        action="store_true",
        help="For stop/restart, also remove compose volumes with docker compose down -v.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print docker commands without running them.",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.build and args.no_build:
        parser.error("--build and --no-build cannot be used together")
    return args


def selected_servers(keys: list[str]) -> list[Server]:
    if not keys:
        return list(SERVERS)
    selected = {key for key in keys}
    return [server for server in SERVERS if server.key in selected]


def detect_compose_command() -> list[str]:
    candidates = (["docker", "compose"], ["docker-compose"])
    for candidate in candidates:
        try:
            subprocess.run(
                [*candidate, "version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
        return candidate

    raise RuntimeError("Docker Compose was not found. Install Docker with the compose plugin first.")


def command_text(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def compose_env(server: Server) -> dict[str, str]:
    env = os.environ.copy()
    if server.key == "search":
        env.setdefault("COMPOSE_PARALLEL_LIMIT", "1")
    return env


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    dry_run: bool,
) -> None:
    print(f"$ {command_text(command)}")
    print(f"  cwd: {cwd}")
    if dry_run:
        return
    subprocess.run(command, cwd=str(cwd), env=env, check=True)


def build_server(server: Server, compose_cmd: list[str], *, dry_run: bool) -> None:
    print(f"\n== Build {server.label} ==")
    env = compose_env(server)
    if server.build_services:
        for service in server.build_services:
            run_command([*compose_cmd, "build", service], cwd=server.compose_dir, env=env, dry_run=dry_run)
    else:
        run_command([*compose_cmd, "build"], cwd=server.compose_dir, env=env, dry_run=dry_run)


def start_server(server: Server, compose_cmd: list[str], *, dry_run: bool) -> None:
    print(f"\n== Start {server.label} ==")
    run_command([*compose_cmd, "up", "-d", "--no-build"], cwd=server.compose_dir, env=compose_env(server), dry_run=dry_run)


def stop_server(server: Server, compose_cmd: list[str], *, volumes: bool, dry_run: bool) -> None:
    print(f"\n== Stop {server.label} ==")
    args = ["down", "-v"] if volumes else ["down"]
    run_command([*compose_cmd, *args], cwd=server.compose_dir, env=compose_env(server), dry_run=dry_run)


def status_server(server: Server, compose_cmd: list[str], *, dry_run: bool) -> None:
    print(f"\n== Status {server.label} ==")
    run_command([*compose_cmd, "ps"], cwd=server.compose_dir, env=compose_env(server), dry_run=dry_run)


def wait_for_health(server: Server, *, timeout: int, dry_run: bool) -> None:
    url = server.health_url()
    print(f"\n== Wait {server.label}: {url} ==")
    if dry_run:
        return

    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 300:
                    body = response.read()
                    try:
                        payload = json.loads(body.decode("utf-8"))
                        status = str(payload.get("status", "")).lower()
                    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                        status = ""
                    if status in {"ok", "healthy"}:
                        print(f"{server.label} is healthy.")
                        return
                    last_error = f"health status={status or 'missing'}"
                else:
                    last_error = f"HTTP {response.status}"
        except (error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)

        remaining = max(0, int(deadline - time.monotonic()))
        print(f"Waiting for {server.label} ({remaining}s left): {last_error}")
        time.sleep(5)

    raise TimeoutError(f"{server.label} did not become healthy within {timeout}s: {last_error}")


def build_all(servers: list[Server], compose_cmd: list[str], *, dry_run: bool) -> None:
    for server in servers:
        build_server(server, compose_cmd, dry_run=dry_run)


def start_all(
    servers: list[Server],
    compose_cmd: list[str],
    *,
    build: bool,
    no_wait: bool,
    timeout: int,
    dry_run: bool,
) -> None:
    if build:
        build_all(servers, compose_cmd, dry_run=dry_run)
    for server in servers:
        start_server(server, compose_cmd, dry_run=dry_run)
    if not no_wait:
        for server in servers:
            wait_for_health(server, timeout=timeout, dry_run=dry_run)


def stop_all(servers: list[Server], compose_cmd: list[str], *, volumes: bool, dry_run: bool) -> None:
    for server in reversed(servers):
        stop_server(server, compose_cmd, volumes=volumes, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    servers = selected_servers(args.services)

    try:
        compose_cmd = ["docker", "compose"] if args.dry_run else detect_compose_command()
        action = args.action
        if action in {"build", "compile"}:
            build_all(servers, compose_cmd, dry_run=args.dry_run)
        elif action in {"start", "up"}:
            start_all(
                servers,
                compose_cmd,
                build=args.build,
                no_wait=args.no_wait,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
        elif action in {"stop", "down"}:
            stop_all(servers, compose_cmd, volumes=args.volumes, dry_run=args.dry_run)
        elif action == "restart":
            stop_all(servers, compose_cmd, volumes=args.volumes, dry_run=args.dry_run)
            start_all(
                servers,
                compose_cmd,
                build=args.build,
                no_wait=args.no_wait,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
        elif action == "status":
            for server in servers:
                status_server(server, compose_cmd, dry_run=args.dry_run)
    except (RuntimeError, subprocess.CalledProcessError, TimeoutError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            return exc.returncode
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
