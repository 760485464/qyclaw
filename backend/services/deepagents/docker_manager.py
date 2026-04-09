from __future__ import annotations

import atexit
import json
import logging
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import threading
from typing import Any, Callable

import docker
from docker.errors import APIError, NotFound

logger = logging.getLogger(__name__)


class DockerExecutor:
    def __init__(self, config: dict[str, Any], conversation_id: str) -> None:
        self.config_enabled = bool(config.get("enabled", False))
        self.enabled = self.config_enabled
        self.strict_mode = bool(config.get("strict_mode", True))
        self.image = config.get("image", "python:3.12-slim")
        self.container_name_prefix = config.get("container_name_prefix", "qyclaw-agent")
        self.workdir = config.get("workdir", "/workspace")
        self.auto_remove = bool(config.get("auto_remove", True))
        self.timeout = int(config.get("timeout", 300))
        self.readonly_workdir = bool(config.get("readonly_workdir", False))
        self.extra_volumes = config.get("volumes", []) or []
        self.environment = config.get("environment", []) or []
        self.daemon_host = str(
            config.get("host") or config.get("daemon_host") or config.get("docker_host") or ""
        ).strip()
        self.remote_enabled = bool(self.daemon_host)
        self.tls_config = config.get("tls", {}) or {}
        self.client_timeout = int(config.get("client_timeout", 10))
        workspace_root_cfg = config.get("workspace_root", "./workspaces")
        workspace_root_path = Path(str(workspace_root_cfg)).expanduser()
        if not workspace_root_path.is_absolute():
            workspace_root_path = (Path.cwd() / workspace_root_path).resolve()
        self.workspace_root = workspace_root_path
        daemon_root_cfg = config.get("daemon_workspace_root")
        if self.remote_enabled:
            daemon_root_value = str(daemon_root_cfg or "/workspaces").strip() or "/workspaces"
            self.daemon_workspace_root = daemon_root_value
            self.host_workspace_dir = str(PurePosixPath(daemon_root_value) / conversation_id)
        else:
            if daemon_root_cfg:
                daemon_root_path = Path(str(daemon_root_cfg)).expanduser()
                if not daemon_root_path.is_absolute():
                    daemon_root_path = (Path.cwd() / daemon_root_path).resolve()
            else:
                daemon_root_path = self.workspace_root
            self.daemon_workspace_root = daemon_root_path
            self.host_workspace_dir = (self.daemon_workspace_root / conversation_id).resolve()

        self.conversation_id = conversation_id
        self.client = None
        self.container = None
        self.container_name = None
        self.init_error: str | None = None

        if self.enabled:
            self._initialize()

    def _initialize(self) -> None:
        try:
            self.client = self._create_docker_client()
            self.client.ping()
            self.init_error = None
            self.enabled = True
            suffix = self.conversation_id.replace("-", "")[:12]
            self.container_name = f"{self.container_name_prefix}-{suffix}"

            mount_mode = "ro" if self.readonly_workdir else "rw"
            if not self.remote_enabled:
                self.host_workspace_dir.mkdir(parents=True, exist_ok=True)
            workspace_mount = f"{self.host_workspace_dir}:{self.workdir}:{mount_mode}"
            if self.remote_enabled:
                workspace_skills_dir = str(PurePosixPath(str(self.host_workspace_dir)) / "skills")
            else:
                workspace_skills_dir = (self.host_workspace_dir / "skills").resolve()
                workspace_skills_dir.mkdir(parents=True, exist_ok=True)
            skills_mount = f"{workspace_skills_dir}:{self.workdir}/skills:ro"

            processed_volumes: list[str] = []
            for vol in self.extra_volumes:
                vol = vol.replace("${PWD}", str(Path.cwd()))
                vol = vol.replace("${WORKDIR}", self.workdir)
                workspace_root_value = self.daemon_workspace_root if self.remote_enabled else self.workspace_root
                vol = vol.replace("${WORKSPACE_ROOT}", str(workspace_root_value))
                vol = vol.replace("${CONVERSATION_WORKSPACE}", str(self.host_workspace_dir))
                processed_volumes.append(vol)

            required_targets: set[str] = set()
            for vol in [workspace_mount, skills_mount] + processed_volumes:
                parts = str(vol).split(":")
                if len(parts) >= 2 and parts[1]:
                    required_targets.add(parts[1])

            def has_required_mounts(container: docker.models.containers.Container) -> bool:
                mounts = container.attrs.get("Mounts") or []
                existing = {m.get("Destination") for m in mounts if m.get("Destination")}
                return required_targets.issubset(existing)

            required_labels = {
                "qyclaw.managed": "true",
                "qyclaw.conversation_id": self.conversation_id,
                "qyclaw.container_prefix": self.container_name_prefix,
            }

            def has_required_labels(container: docker.models.containers.Container) -> bool:
                labels = container.labels or {}
                if not labels:
                    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
                for key, value in required_labels.items():
                    if labels.get(key) != value:
                        return False
                return True

            # Reuse existing named container when possible to avoid 409 conflicts.
            try:
                existing = self.client.containers.get(self.container_name)
                existing.reload()
                if has_required_mounts(existing) and has_required_labels(existing):
                    if existing.status == "running":
                        self.container = existing
                        return
                    try:
                        existing.start()
                        existing.reload()
                        if existing.status == "running" and has_required_mounts(existing) and has_required_labels(existing):
                            self.container = existing
                            return
                    except Exception:
                        # If it cannot be started, remove stale container and recreate.
                        try:
                            existing.remove(force=True)
                        except Exception:
                            pass
                else:
                    try:
                        existing.remove(force=True)
                    except Exception:
                        pass
            except NotFound:
                pass

            self.container = self.client.containers.run(
                self.image,
                command="tail -f /dev/null",
                name=self.container_name,
                detach=True,
                working_dir=self.workdir,
                volumes=[workspace_mount, skills_mount] + processed_volumes,
                labels={
                    "qyclaw.managed": "true",
                    "qyclaw.conversation_id": self.conversation_id,
                    "qyclaw.container_prefix": self.container_name_prefix,
                },
                environment=self.environment,
                auto_remove=self.auto_remove,
            )
        except APIError as exc:
            # Extra safeguard for rare race: container created between get() and run().
            if "Conflict" in str(exc) and self.client and self.container_name:
                try:
                    existing = self.client.containers.get(self.container_name)
                    existing.reload()
                    if existing.status != "running":
                        existing.start()
                        existing.reload()
                    if existing.status == "running":
                        self.container = existing
                        return
                except Exception:
                    pass
            self.init_error = str(exc)
            self.enabled = False
        except Exception as exc:
            self.init_error = str(exc)
            self.enabled = False

    def _create_docker_client(self) -> docker.DockerClient:
        if not self.remote_enabled:
            return docker.from_env(timeout=self.client_timeout)

        tls_enabled = bool(self.tls_config.get("enabled", False))
        tls_obj = None
        if tls_enabled:
            certs_dir = Path(str(self.tls_config.get("certs_dir", "./certs/client"))).expanduser()
            if not certs_dir.is_absolute():
                certs_dir = (Path.cwd() / certs_dir).resolve()
            ca_cert = str(self.tls_config.get("ca_cert") or (certs_dir / "ca.pem"))
            client_cert = str(self.tls_config.get("client_cert") or (certs_dir / "cert.pem"))
            client_key = str(self.tls_config.get("client_key") or (certs_dir / "key.pem"))
            verify = bool(self.tls_config.get("verify", True))
            tls_obj = docker.tls.TLSConfig(
                client_cert=(client_cert, client_key),
                ca_cert=ca_cert,
                verify=verify,
            )

        return docker.DockerClient(base_url=self.daemon_host, tls=tls_obj, timeout=self.client_timeout)

    def _ensure_container_ready(self) -> bool:
        if not self.config_enabled:
            return False

        if self.client is None or self.container_name is None:
            self.enabled = True
            self._initialize()
            return self.enabled and self.container is not None

        if self.container is not None:
            try:
                self.container.reload()
                if self.container.status != "running":
                    self.container.start()
                    self.container.reload()
                if self.container.status == "running":
                    self.enabled = True
                    self.init_error = None
                    return True
            except NotFound:
                self.container = None
            except Exception:
                self.container = None

        if self.client and self.container_name:
            try:
                existing = self.client.containers.get(self.container_name)
                existing.reload()
                if existing.status != "running":
                    existing.start()
                    existing.reload()
                if existing.status == "running":
                    self.container = existing
                    self.enabled = True
                    self.init_error = None
                    return True
            except NotFound:
                pass
            except Exception:
                pass

        self.enabled = True
        self._initialize()
        return self.enabled and self.container is not None

    def _exec_in_container(self, command: str) -> str:
        if self.container is None:
            raise RuntimeError("container is None")
        result = self.container.exec_run(
            f"bash -lc {json.dumps(command)}",
            workdir=self.workdir,
            demux=True,
        )
        stdout = result.output[0].decode("utf-8") if result.output and result.output[0] else ""
        stderr = result.output[1].decode("utf-8") if result.output and result.output[1] else ""
        if result.exit_code != 0:
            return f"Command failed with exit code {result.exit_code}\n{stderr or stdout}"
        return (stdout + ("\n" + stderr if stderr else "")).strip()

    def execute(self, command: str) -> str:
        if self.config_enabled and not self._ensure_container_ready():
            if self.strict_mode:
                if not self.config_enabled:
                    return "Error: Docker execution is disabled by config; local fallback is forbidden."
                return f"Error: Docker is not available (init failed: {self.init_error}); local fallback is forbidden."
            return self._execute_local(command)
        if not self.config_enabled:
            if self.strict_mode:
                return "Error: Docker execution is disabled by config; local fallback is forbidden."
            return self._execute_local(command)

        if self.container is None:
            if self.strict_mode:
                return f"Error: Docker container is unavailable; local fallback is forbidden. init_error={self.init_error}"
            return self._execute_local(command)

        try:
            return self._exec_in_container(command)
        except NotFound:
            if self._ensure_container_ready():
                try:
                    return self._exec_in_container(command)
                except Exception as retry_exc:  # noqa: BLE001
                    if self.strict_mode:
                        return f"Error executing in docker after reinit: {retry_exc}. local fallback is forbidden."
                    return self._execute_local(command)
            if self.strict_mode:
                return "Error executing in docker: container not found and reinit failed; local fallback is forbidden."
            return self._execute_local(command)
        except APIError as exc:
            err_text = str(exc)
            if "No such container" in err_text and self._ensure_container_ready():
                try:
                    return self._exec_in_container(command)
                except Exception as retry_exc:  # noqa: BLE001
                    if self.strict_mode:
                        return f"Error executing in docker after reinit: {retry_exc}. local fallback is forbidden."
                    return self._execute_local(command)
            if self.strict_mode:
                return f"Error executing in docker: {exc}. local fallback is forbidden."
            return self._execute_local(command)
        except Exception as exc:  # noqa: BLE001
            if self.strict_mode:
                return f"Error executing in docker: {exc}. local fallback is forbidden."
            return self._execute_local(command)

    def _execute_local(self, command: str) -> str:
        try:
            result = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=self.timeout)
            if result.returncode != 0:
                stdout = (result.stdout or "").strip()
                stderr = (result.stderr or "").strip()
                detail = stderr or stdout or "no output"
                return f"Command failed with exit code {result.returncode}\nReason: {detail}"
            return (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip()
        except Exception as exc:  # noqa: BLE001
            return f"Error executing locally: {exc}"

    def cleanup(self) -> None:
        if self.container is None:
            return
        try:
            self.container.reload()
            self.container.stop(timeout=5)
            self.container.remove(force=True)
        except Exception:
            pass


class DockerExecutionManager:
    def __init__(self, config: dict[str, Any], daemon_resolver: Callable[[str], dict[str, Any] | None] | None = None) -> None:
        self.config = config
        self.daemon_resolver = daemon_resolver
        self.executors: dict[str, DockerExecutor] = {}
        self.extra_volumes_by_conversation: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        atexit.register(self.cleanup_all)

    def _effective_config(self, conversation_id: str) -> dict[str, Any]:
        cfg = dict(self.config)
        if not self.daemon_resolver:
            return cfg
        override = self.daemon_resolver(conversation_id) or {}
        if not isinstance(override, dict):
            return cfg
        if "tls" in override and isinstance(override.get("tls"), dict):
            base_tls = dict(cfg.get("tls") or {})
            base_tls.update(override["tls"] or {})
            cfg["tls"] = base_tls
        for key, value in override.items():
            if key == "tls":
                continue
            cfg[key] = value
        volumes = list(cfg.get("volumes") or [])
        extra = self.extra_volumes_by_conversation.get(conversation_id) or []
        if extra:
            volumes.extend(extra)
        cfg["volumes"] = volumes
        return cfg

    def set_conversation_volumes(self, conversation_id: str, volumes: list[str]) -> None:
        normalized: list[str] = []
        for entry in volumes:
            if entry and entry not in normalized:
                normalized.append(entry)
        existing = self.extra_volumes_by_conversation.get(conversation_id)
        if existing == normalized:
            return
        self.extra_volumes_by_conversation[conversation_id] = normalized
        with self.lock:
            executor = self.executors.pop(conversation_id, None)
        if executor:
            executor.cleanup()

    def clear_conversation_volumes(self, conversation_id: str) -> None:
        self.extra_volumes_by_conversation.pop(conversation_id, None)

    def _get_or_create_executor(self, conversation_id: str) -> DockerExecutor:
        with self.lock:
            executor = self.executors.get(conversation_id)
            if executor is None:
                executor = DockerExecutor(self._effective_config(conversation_id), conversation_id)
                self.executors[conversation_id] = executor
            return executor

    def execute(self, conversation_id: str, command: str) -> str:
        return self._get_or_create_executor(conversation_id).execute(command)

    def status(self, conversation_id: str) -> dict[str, Any]:
        executor = self._get_or_create_executor(conversation_id)
        return {
            "enabled": executor.enabled,
            "container_name": executor.container_name,
            "image": executor.image,
            "workdir": executor.workdir,
            "host_workspace_dir": str(executor.host_workspace_dir),
            "init_error": executor.init_error,
        }

    def cleanup_conversation(self, conversation_id: str) -> None:
        with self.lock:
            executor = self.executors.pop(conversation_id, None)
        if executor:
            executor.cleanup()

    def cleanup_all(self) -> None:
        with self.lock:
            items = list(self.executors.items())
            self.executors.clear()
        if items:
            targets = [
                {
                    "conversation_id": conv_id,
                    "daemon": executor.daemon_host or "local",
                    "container": executor.container_name or "unknown",
                }
                for conv_id, executor in items
            ]
            logger.info("Shutting down docker containers: %s", targets)
        for conv_id, executor in items:
            daemon = executor.daemon_host or "local"
            container = executor.container_name or "unknown"
            logger.info(
                "Stopping docker container (conversation=%s, daemon=%s, container=%s)",
                conv_id,
                daemon,
                container,
            )
            print(f"[shutdown] stopping container conversation={conv_id} daemon={daemon} container={container}")
            try:
                executor.cleanup()
                logger.info(
                    "Stopped docker container (conversation=%s, daemon=%s, container=%s)",
                    conv_id,
                    daemon,
                    container,
                )
                print(f"[shutdown] stopped container conversation={conv_id} daemon={daemon} container={container}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to stop docker container (conversation=%s, daemon=%s, container=%s): %s",
                    conv_id,
                    daemon,
                    container,
                    exc,
                )
                print(
                    f"[shutdown] failed container conversation={conv_id} daemon={daemon} "
                    f"container={container} error={exc}"
                )

