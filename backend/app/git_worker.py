from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from typing import Any

from redis.asyncio import Redis

from .core.config import get_settings
from .providers.git import GitProvider, GitProviderError
from .providers.github import GitHubProvider
from .providers.gitlab import GitLabProvider
from .services.git_workspace import GitWorkspaceManager
from .services.leases import TaskLease
from .services.task_progress import publish_task_running
from .services.diagnostics import safe_error_message
from .services.provider_secrets import ProviderSecretError, ProviderSecretStore


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forgeflow.git_worker")


def build_provider(provider_name: str) -> GitProvider | None:
    settings = get_settings()
    if provider_name not in {"github", "gitlab"}:
        raise GitProviderError("git.unsupported_provider", f"unsupported provider {provider_name}")
    try:
        managed_token = ProviderSecretStore(settings.provider_secret_root).read(provider_name)
    except ProviderSecretError as exc:
        raise GitProviderError("git.invalid_provider_secret", str(exc)) from exc
    if provider_name == "github":
        token = managed_token or settings.github_token
        if not token:
            return None
        return GitHubProvider(token, settings.github_webhook_secret)
    if provider_name == "gitlab":
        token = managed_token or settings.gitlab_token
        if not token:
            return None
        return GitLabProvider(
            token,
            settings.gitlab_webhook_secret,
            f"{settings.gitlab_base_url.rstrip('/')}/api/v4",
        )
    raise AssertionError("validated provider was not handled")


async def execute_task(
    envelope: dict[str, Any],
    provider: GitProvider | None = None,
    workspace_manager: GitWorkspaceManager | None = None,
) -> dict[str, Any]:
    task_type = envelope.get("task_type")
    context = envelope["payload"]["context"]
    if task_type == "git.prepare_workspaces":
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        output = await asyncio.to_thread(manager.prepare, context)
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type == "git.prepare_analysis":
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        output = await asyncio.to_thread(manager.prepare_analysis, context)
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type == "git.restore_workspaces":
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        output = await asyncio.to_thread(manager.restore, context)
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type == "git.restore_validation_workspace":
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        output = await asyncio.to_thread(manager.restore, context)
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type == "git.commit_changes":
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        output = await asyncio.to_thread(manager.commit, context)
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type == "git.publish_changes":
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        factory = (lambda _name: provider) if provider else build_provider
        output = await manager.publish(context, factory)  # type: ignore[arg-type]
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type in {"git.prepare_verification", "git.prepare_final_verification"}:
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        output = await asyncio.to_thread(
            manager.prepare_verification,
            context,
            task_type == "git.prepare_final_verification",
        )
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type == "git.prepare_incremental_verification":
        manager = workspace_manager or GitWorkspaceManager(get_settings())
        output = await asyncio.to_thread(manager.prepare_verification, context, False, True)
        return {"task_id": envelope["task_id"], "status": "completed", "output": output}
    if task_type == "git.create_pull_request":
        selected = provider or build_provider(context["provider"])
        if selected is None:
            variable = "GITHUB_TOKEN" if context["provider"] == "github" else "GITLAB_TOKEN"
            raise GitProviderError(
                "git.missing_provider_token",
                f"provider token is not configured; set {variable} and restart git-worker",
            )
        try:
            pull_request = await selected.create_or_update_pull_request(
                context["repository"],
                context["work_branch"],
                context["target_branch"],
                f"[画板] {context['title']}",
                "## 画板 delivery\n\n"
                f"{context.get('description') or 'Automated implementation produced by 画板.'}\n\n"
                f"Requirement: `{context['requirement_id']}`\n",
            )
            if pull_request.head_sha.lower() != context["head_sha"].lower():
                raise GitProviderError(
                    "git.pull_request_head_mismatch",
                    "created pull request head does not match reviewed head SHA",
                )
            return {
                "task_id": envelope["task_id"],
                "status": "completed",
                "output": {
                    "requirement_repository_id": context["requirement_repository_id"],
                    "pull_request_number": pull_request.number,
                    "pull_request_url": pull_request.url,
                    "head_sha": pull_request.head_sha,
                },
            }
        finally:
            if provider is None and hasattr(selected, "close"):
                await selected.close()  # type: ignore[attr-defined]
    if task_type != "git.merge_next":
        raise GitProviderError("git.unsupported_task", f"unsupported task {task_type}")
    selected = provider or build_provider(context["provider"])
    try:
        checks = await selected.get_checks(context["repository"], context["head_sha"])
        green_states = {"success", "skipped", "neutral"}
        failed = [item for item in checks if item.get("status") not in green_states and item.get("conclusion") not in green_states]
        if failed:
            raise GitProviderError("git.checks_not_green", "required checks are not green", True)
        merged_sha = await selected.merge(
            context["repository"], int(context["pull_request_number"]), context["head_sha"]
        )
        return {"task_id": envelope["task_id"], "status": "completed", "output": {"merged_sha": merged_sha, "checks": checks}}
    finally:
        if provider is None and hasattr(selected, "close"):
            await selected.close()  # type: ignore[attr-defined]


async def run_worker() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stream_name = "forgeflow:git-tasks"
    group = "forgeflow-git-workers"
    try:
        await redis.xgroup_create(stream_name, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    logger.info("git worker started: %s", worker_id)
    while True:
        claimed = await redis.xautoclaim(stream_name, group, worker_id, settings.task_lease_seconds * 1000, "0-0", count=1)
        messages = [(stream_name, claimed[1])] if claimed[1] else await redis.xreadgroup(group, worker_id, {stream_name: ">"}, count=1, block=5_000)
        for stream, entries in messages:
            for message_id, fields in entries:
                envelope = json.loads(fields["payload"])
                lease = TaskLease(
                    redis,
                    envelope["task_id"],
                    envelope["payload"]["requirement_id"],
                    worker_id,
                    settings.task_lease_seconds,
                    settings.max_parallel_requirements,
                    on_heartbeat=lambda task_id=envelope["task_id"]: publish_task_running(
                        redis,
                        task_id,
                        worker_id,
                        settings.task_lease_seconds,
                    ),
                )
                if not await lease.acquire():
                    await redis.xack(stream, group, message_id)
                    continue
                try:
                    result = await execute_task(envelope)
                except GitProviderError as exc:
                    message = safe_error_message(exc)
                    logger.error(
                        "git task failed: task_id=%s code=%s detail=%s",
                        envelope["task_id"],
                        exc.code,
                        message,
                    )
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "failed",
                        "error_code": exc.code,
                        "error_message": message,
                        "retryable": exc.retryable,
                    }
                except Exception as exc:
                    logger.exception("unhandled git task failure")
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "failed",
                        "error_code": "git.internal",
                        "error_message": safe_error_message(exc) or "unexpected Git worker failure",
                        "retryable": True,
                    }
                await redis.xadd("forgeflow:results", {"payload": json.dumps(result)}, maxlen=10_000, approximate=True)
                await redis.xack(stream, group, message_id)
                await lease.complete()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
