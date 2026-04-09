from __future__ import annotations

import argparse
import importlib
import json
import os
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CheckResult:
    name: str
    status: str
    detail: str
    data: dict[str, Any] | None = None


def _result(name: str, status: str, detail: str, data: dict[str, Any] | None = None) -> CheckResult:
    return CheckResult(name=name, status=status, detail=detail, data=data)


def _resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _import_check(name: str, module_name: str) -> CheckResult:
    try:
        module = importlib.import_module(module_name)
        version = getattr(module, "__version__", None)
        detail = f"import ok: {module_name}"
        if version:
            detail += f" ({version})"
        return _result(name, "ok", detail)
    except Exception as exc:  # noqa: BLE001
        return _result(name, "fail", f"import failed: {module_name}: {exc.__class__.__name__}: {exc}")


def _load_settings(config_path: Path) -> tuple[Any | None, CheckResult]:
    try:
        os.environ["QYCLAW_CONFIG"] = str(config_path)
        from backend.core.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        return settings, _result("config", "ok", f"loaded config: {config_path}")
    except Exception as exc:  # noqa: BLE001
        return None, _result("config", "fail", f"config load failed: {exc.__class__.__name__}: {exc}")


def _database_check(settings: Any) -> CheckResult:
    try:
        import psycopg

        conn = psycopg.connect(
            host=settings.database.host,
            port=settings.database.port,
            user=settings.database.user,
            password=settings.database.password,
            dbname=settings.database.name,
            connect_timeout=3,
        )
        with conn.cursor() as cur:
            cur.execute("select 1")
            row = cur.fetchone()
        conn.close()
        return _result(
            "database",
            "ok",
            "database connection ok",
            {"host": settings.database.host, "port": settings.database.port, "probe": row[0] if row else None},
        )
    except Exception as exc:  # noqa: BLE001
        return _result(
            "database",
            "fail",
            f"database connection failed: {exc.__class__.__name__}: {exc}",
            {"host": settings.database.host, "port": settings.database.port},
        )


def _preferred_daemon_host(settings: Any) -> str | None:
    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_hosts = docker_cfg.get("daemon_hosts")
    if isinstance(daemon_hosts, list):
        for entry in daemon_hosts:
            if isinstance(entry, dict) and entry.get("host"):
                return str(entry["host"])
    host = docker_cfg.get("daemon_host")
    return str(host) if host else None


def _prepare_smoke_conversation(settings: Any, user_id: str, conversation_id: str) -> CheckResult:
    try:
        from backend.core.database import SessionLocal
        from backend.core.models import Conversation, User
    except Exception as exc:  # noqa: BLE001
        return _result(
            "smoke_fixture",
            "fail",
            f"fixture import failed: {exc.__class__.__name__}: {exc}",
        )

    daemon_host = _preferred_daemon_host(settings)
    try:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if user is None:
                user = User(
                    id=user_id,
                    username=f"{user_id}-user",
                    display_name="Smoke Check User",
                    email=f"{user_id}@example.local",
                    password_hash="not-used",
                    is_admin=False,
                    is_blocked=False,
                )
                db.add(user)

            conversation = db.get(Conversation, conversation_id)
            if conversation is None:
                conversation = Conversation(
                    id=conversation_id,
                    user_id=user_id,
                    title="Smoke Check Conversation",
                    model_name="smoke-model",
                    execution_backend="deepagents",
                    container_status="stopped",
                    daemon_host=daemon_host,
                )
                db.add(conversation)
            elif daemon_host and conversation.daemon_host != daemon_host:
                conversation.daemon_host = daemon_host
                conversation.updated_at = datetime.utcnow()
            db.commit()
        detail = "smoke user/conversation prepared"
        data = {"conversation_id": conversation_id, "daemon_host": daemon_host}
        return _result("smoke_fixture", "ok", detail, data)
    except Exception as exc:  # noqa: BLE001
        return _result(
            "smoke_fixture",
            "fail",
            f"failed to prepare smoke conversation: {exc.__class__.__name__}: {exc}",
            {"conversation_id": conversation_id, "daemon_host": daemon_host},
        )


def _docker_check(settings: Any, config_path: Path) -> CheckResult:
    try:
        import docker
    except Exception as exc:  # noqa: BLE001
        return _result("docker", "fail", f"docker import failed: {exc.__class__.__name__}: {exc}")

    docker_cfg = (settings.model_extra or {}).get("docker", {}) or {}
    daemon_hosts = docker_cfg.get("daemon_hosts")
    targets: list[dict[str, Any]] = []
    if isinstance(daemon_hosts, list) and daemon_hosts:
        for entry in daemon_hosts:
            if isinstance(entry, dict):
                targets.append(entry)
    elif docker_cfg.get("daemon_host"):
        targets.append({"host": docker_cfg.get("daemon_host")})
    else:
        targets.append({})

    results: list[dict[str, Any]] = []
    failures = 0
    config_dir = config_path.parent
    for target in targets:
        host = str(target.get("host") or "local")
        try:
            client_timeout = int(target.get("client_timeout") or docker_cfg.get("client_timeout") or 10)
            if host == "local":
                client = docker.from_env(timeout=client_timeout)
            else:
                tls_cfg = docker_cfg.get("tls", {}) or {}
                tls_obj = None
                if bool(tls_cfg.get("enabled", False)):
                    certs_dir = _resolve_path(str(tls_cfg.get("certs_dir", "./certs")), config_dir)
                    ca_cert = _resolve_path(str(tls_cfg.get("ca_cert", certs_dir / "ca.crt")), config_dir)
                    client_cert = _resolve_path(str(tls_cfg.get("client_cert", certs_dir / "tls.crt")), config_dir)
                    client_key = _resolve_path(str(tls_cfg.get("client_key", certs_dir / "tls.key")), config_dir)
                    tls_obj = docker.tls.TLSConfig(
                        client_cert=(str(client_cert), str(client_key)),
                        ca_cert=str(ca_cert),
                        verify=bool(tls_cfg.get("verify", True)),
                    )
                client = docker.DockerClient(base_url=host, tls=tls_obj, timeout=client_timeout)
            client.ping()
            results.append({"host": host, "status": "ok"})
        except Exception as exc:  # noqa: BLE001
            failures += 1
            results.append({"host": host, "status": "fail", "error": f"{exc.__class__.__name__}: {exc}"})

    if failures == len(results):
        return _result("docker", "fail", "all docker targets failed", {"targets": results})
    if failures:
        return _result("docker", "warn", "some docker targets failed", {"targets": results})
    return _result("docker", "ok", "docker targets reachable", {"targets": results})


def _backend_check(settings: Any, conversation_id: str, db_ok: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        from backend.agent_backends import get_backend
    except Exception as exc:  # noqa: BLE001
        return [_result("backends", "fail", f"backend import failed: {exc.__class__.__name__}: {exc}")]

    for backend_name in ("deepagents", "claude"):
        if backend_name == "deepagents" and not db_ok:
            results.append(
                _result(
                    f"backend:{backend_name}",
                    "fail",
                    "skipped because database prerequisite failed",
                )
            )
            continue
        try:
            backend = get_backend(backend_name)
            ready = backend.ensure_ready(conversation_id)
            status = "ok"
            if ready.get("enabled") is False or ready.get("init_error"):
                status = "warn"
            results.append(
                _result(
                    f"backend:{backend_name}",
                    status,
                    f"ensure_ready completed for {backend_name}",
                    {"ready": ready},
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                _result(
                    f"backend:{backend_name}",
                    "fail",
                    f"ensure_ready failed: {exc.__class__.__name__}: {exc}",
                )
            )
    return results


def _routing_check(settings: Any, user_id: str) -> CheckResult:
    try:
        from backend.runtime.backend_router import resolve_backend_name, resolve_fallback_backend_name

        chosen = resolve_backend_name(user_id, None)
        fallback = resolve_fallback_backend_name("claude")
        return _result(
            "backend_routing",
            "ok",
            "backend routing loaded",
            {
                "default_backend": settings.backend_routing.default_backend,
                "rollout_backend": settings.backend_routing.rollout_backend,
                "rollout_percent": settings.backend_routing.rollout_percent,
                "resolved_for_user": chosen,
                "fallback_from_claude": fallback,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _result("backend_routing", "fail", f"backend routing failed: {exc.__class__.__name__}: {exc}")


def _runtime_imports() -> list[CheckResult]:
    checks = [
        ("module:psycopg", "psycopg"),
        ("module:langgraph", "langgraph"),
        ("module:deepagents", "deepagents"),
        ("module:claude_agent_sdk", "claude_agent_sdk"),
        ("module:docker", "docker"),
    ]
    return [_import_check(name, module_name) for name, module_name in checks]


def main() -> int:
    parser = argparse.ArgumentParser(description="Qyclaw runtime smoke check")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "config.yaml"),
        help="Path to Qyclaw config.yaml",
    )
    parser.add_argument("--user-id", default="smoke-user", help="Synthetic user id for routing check")
    parser.add_argument("--conversation-id", default="smoke-conversation", help="Synthetic conversation id for ensure_ready")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    results: list[CheckResult] = []
    results.extend(_runtime_imports())

    config_path = Path(args.config).expanduser().resolve()
    os.chdir(config_path.parent)
    settings, config_result = _load_settings(config_path)
    results.append(config_result)

    if settings is not None:
        db_result = _database_check(settings)
        results.append(
            _result(
                "settings_summary",
                "ok",
                "settings parsed",
                {
                    "db_host": settings.database.host,
                    "db_port": settings.database.port,
                    "frontend_base_url": settings.app.frontend_base_url,
                    "default_backend": settings.backend_routing.default_backend,
                    "rollout_backend": settings.backend_routing.rollout_backend,
                    "rollout_percent": settings.backend_routing.rollout_percent,
                },
            )
        )
        results.append(db_result)
        if db_result.status == "ok":
            results.append(_prepare_smoke_conversation(settings, args.user_id, args.conversation_id))
        results.append(_docker_check(settings, config_path))
        results.append(_routing_check(settings, args.user_id))
        results.extend(_backend_check(settings, args.conversation_id, db_ok=db_result.status == "ok"))

    if args.json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(f"[{item.status.upper()}] {item.name}: {item.detail}")
            if item.data:
                print(json.dumps(item.data, ensure_ascii=False, indent=2))

    failed = any(item.status == "fail" for item in results)
    warned = any(item.status == "warn" for item in results)
    if failed:
        return 2
    if warned:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
