#!/usr/bin/env python3
"""Run a non-destructive local Compose smoke test against a fresh project."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar


BASE_URL = os.getenv("FORGEFLOW_API_URL", "http://127.0.0.1:8000")
EMAIL = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "change-this-password")


def main() -> None:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            BASE_URL + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(req, timeout=10) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()}") from exc

    request("POST", "/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD})
    suffix = str(int(time.time()))[-8:]
    project = request(
        "POST",
        "/api/v1/projects",
        {"key": f"SMK{suffix}", "name": f"Smoke {suffix}", "description": "自动化容器冒烟测试"},
    )
    repo = request(
        "POST",
        f"/api/v1/projects/{project['id']}/repositories",
        {
            "provider": "github",
            "external_id": f"smoke/repo-{suffix}",
            "full_name": f"smoke/repo-{suffix}",
            "clone_url": f"https://github.com/smoke/repo-{suffix}.git",
            "web_url": f"https://github.com/smoke/repo-{suffix}",
            "default_branch": "main",
        },
    )
    requirement = request(
        "POST",
        f"/api/v1/projects/{project['id']}/requirements",
        {
            "title": "验证四 Agent 持久化交付链路",
            "description": "从发布开始，依次验证澄清、方案、开发、评审和验收任务均由 Redis Streams 驱动并写回 SQLite。",
            "priority": "high",
            "repositories": [{"repository_id": repo["id"], "target_branch": "main", "merge_order": 0}],
        },
    )

    requirement = transition(request, requirement, "publish")
    requirement = wait_for(request, requirement["id"], "awaiting_clarification")
    requirement = transition(request, requirement, "confirm_clarification")
    requirement = wait_for(request, requirement["id"], "awaiting_plan")
    requirement = transition(request, requirement, "confirm_plan")
    requirement = wait_for(request, requirement["id"], "awaiting_merge", timeout=30)
    artifacts = request("GET", f"/api/v1/requirements/{requirement['id']}/artifacts")
    expected = {
        "clarification_spec",
        "architecture_plan",
        "development_report",
        "code_review_report",
        "acceptance_report",
    }
    actual = {item["kind"] for item in artifacts}
    missing = expected - actual
    if missing:
        raise AssertionError(f"missing agent artifacts: {sorted(missing)}")
    print(
        json.dumps(
            {
                "ok": True,
                "project_key": project["key"],
                "requirement_number": requirement["number"],
                "status": requirement["status"],
                "artifact_kinds": sorted(actual),
            },
            ensure_ascii=False,
        )
    )


def transition(request, requirement: dict, event: str) -> dict:
    response = request(
        "POST",
        f"/api/v1/requirements/{requirement['id']}/transitions",
        {"event": event, "expected_version": requirement["version"], "reason": "local smoke test"},
    )
    return response["requirement"]


def wait_for(request, requirement_id: str, expected_status: str, timeout: int = 20) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = request("GET", f"/api/v1/requirements/{requirement_id}")
        if last["status"] == expected_status:
            return last
        if last["status"] in {"blocked", "cancelled"}:
            break
        time.sleep(0.25)
    raise AssertionError(f"expected {expected_status}, got {last and last['status']}")


if __name__ == "__main__":
    main()
