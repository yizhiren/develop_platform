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


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forgeflow.git_worker")


def build_provider(provider_name: str) -> GitProvider:
    settings = get_settings()
    if provider_name == "github":
        if not settings.github_token:
            raise GitProviderError("github.missing_token", "GitHub token is not configured")
        return GitHubProvider(settings.github_token, settings.github_webhook_secret)
    if provider_name == "gitlab":
        if not settings.gitlab_token:
            raise GitProviderError("gitlab.missing_token", "GitLab token is not configured")
        return GitLabProvider(
            settings.gitlab_token,
            settings.gitlab_webhook_secret,
            f"{settings.gitlab_base_url.rstrip('/')}/api/v4",
        )
    raise GitProviderError("git.unsupported_provider", f"unsupported provider {provider_name}")


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
                )
                if not await lease.acquire():
                    await redis.xack(stream, group, message_id)
                    continue
                try:
                    result = await execute_task(envelope)
                except GitProviderError as exc:
                    result = {"task_id": envelope["task_id"], "status": "failed", "error_code": exc.code, "retryable": exc.retryable}
                except Exception:
                    logger.exception("unhandled git task failure")
                    result = {"task_id": envelope["task_id"], "status": "failed", "error_code": "git.internal", "retryable": True}
                await redis.xadd("forgeflow:results", {"payload": json.dumps(result)}, maxlen=10_000, approximate=True)
                await redis.xack(stream, group, message_id)
                await lease.complete()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
