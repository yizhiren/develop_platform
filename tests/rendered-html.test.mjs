import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the 画板 application shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>画板 · AI 开发平台<\/title>/i);
  assert.match(html, /让每个需求/);
  assert.match(html, /需求澄清师/);
  assert.match(html, /系统架构师/);
  assert.match(html, /开发工程师/);
  assert.match(html, /验收工程师/);
  assert.match(html, /src="\/huaban-logo\.png"/);
  assert.match(html, /alt="画板 Logo"/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("keeps the product metadata and security copy", async () => {
  const [page, layout, styles, packageJson, logo] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../public/huaban-logo.png", import.meta.url)),
  ]);
  assert.match(page, /Agent 沙箱/);
  assert.match(page, /发布需求/);
  assert.match(page, /粘贴截图/);
  assert.match(page, /onPaste=\{pasteImages\}/);
  assert.match(page, /data_base64: attachment\.data_base64/);
  assert.match(page, /\/api\/v1\/requirement-attachments\/\$\{attachment\.id\}\/content/);
  assert.match(page, /添加仓库/);
  assert.match(page, /平台设置/);
  assert.match(page, /Provider 凭据/);
  assert.match(page, /Token 只保存到受限 Secret Volume/);
  assert.match(page, /\/api\/v1\/admin\/provider-credentials/);
  assert.match(page, /type="password"/);
  assert.match(page, /保存后不会再次显示/);
  assert.match(page, /编辑代码仓库/);
  assert.match(page, /保存仓库修改/);
  assert.match(page, /method: repository \? "PUT" : "POST"/);
  assert.match(page, /请先为.*添加仓库/);
  assert.match(page, /仓库只会出现在当前项目中/);
  assert.match(page, /git@github\.com:organization\/repository\.git/);
  assert.match(page, /与需求澄清师对话/);
  assert.match(page, /需要你回答的问题/);
  assert.match(page, /发送回答给需求澄清师/);
  assert.match(page, /COLLABORATION SWIMLANE/);
  assert.match(page, /角色协作与任务交接/);
  assert.match(page, /需求方/);
  assert.match(page, /画板 \/ Git/);
  assert.match(page, /handoffLabel/);
  assert.match(page, /Code Review 意见 · 返工/);
  assert.match(page, /workflowRunOutcomeLabels/);
  assert.match(page, /代码评审 · 未通过/);
  assert.match(page, /entry\.actor_id === run\.id/);
  assert.match(page, /workflowTaskLabels/);
  assert.match(page, /task\.agent_run_id === null/);
  assert.match(page, /画板 \/ Git 正在执行/);
  assert.match(page, /analysis_ready: "读取仓库现状"/);
  assert.match(
    page,
    /const platformTimelineEvents = new Set\(\[[\s\S]*?"analysis_ready"[\s\S]*?\]\);/,
  );
  assert.match(page, /\/api\/v1\/requirements\/\$\{item\.id\}\/tasks/);
  assert.match(page, /完整审计日志/);
  assert.match(page, /aria-label="需求协作泳道图"/);
  assert.match(page, /SWIMLANE_REFRESH_INTERVAL_MS = 1500/);
  assert.match(page, /refreshSwimlane/);
  assert.match(page, /运行期间每 1\.5 秒自动刷新/);
  assert.match(page, /className="swimlane-error"/);
  assert.match(styles, /\.workflow-swimlane/);
  assert.match(styles, /grid-template-columns: repeat\(6/);
  assert.match(styles, /\.swimlane-scroll[\s\S]*overflow-x: auto/);
  assert.match(styles, /width: min\(1180px, 96vw\)/);
  assert.match(styles, /\.swimlane-error code[\s\S]*max-height: 150px/);
  assert.match(styles, /\.run-state\.running/);
  assert.match(styles, /\.swimlane-event\.rejected/);
  assert.match(styles, /\.requirement-image-previews/);
  assert.match(styles, /\.requirement-attachment-grid/);
  assert.match(page, /open_questions/);
  assert.match(page, /澄清已完成，正在启动系统架构师/);
  assert.doesNotMatch(page, />确认需求规格</);
  assert.match(page, /实现方案评审/);
  assert.match(page, /方案置信度/);
  assert.match(page, /需人工审核/);
  assert.match(page, /本方案未达到自动批准条件/);
  assert.match(page, /自动批准实现方案/);
  assert.match(
    page,
    /const platformTimelineEvents = new Set\(\[[\s\S]*?"confirm_plan"[\s\S]*?\]\);/,
  );
  assert.match(page, /批准方案并开始开发/);
  assert.match(page, /要求系统架构师调整/);
  assert.match(page, /目标方案/);
  assert.match(page, /回滚方案/);
  assert.match(page, /阻塞原因/);
  assert.match(page, /完整错误详情/);
  assert.match(page, /开发工程师未能在步骤上限内完成/);
  assert.match(page, /开发工程师连续返回了不可执行的动作/);
  assert.match(page, /代码评审输出未通过平台校验/);
  assert.match(page, /重新执行代码评审，无需重跑开发/);
  assert.match(page, /\["retry_review", "重试代码评审"\]/);
  assert.match(page, /模型 API Key 未配置/);
  assert.match(page, /加载 \.env\.local 重启 Agent Worker/);
  assert.match(page, /需求澄清师未在轮次上限内提交结论/);
  assert.match(page, /run\.diagnostics\?\.max_turns \?\? 32/);
  assert.match(page, /正在执行 · 上限 32 轮/);
  assert.match(page, /\["retry_clarification", "重试需求澄清"\]/);
  assert.match(page, /重新获取仓库信息并继续需求澄清/);
  assert.match(
    page,
    /blockedDiagnostic\?\.recoveryEvent ===\s*"retry_clarification"/,
  );
  assert.match(page, /parseApiTimestamp/);
  assert.match(page, /从开发重试，复用当前开发分支/);
  assert.match(page, /run\.error_message/);
  assert.match(page, /timeline-reason/);
  assert.match(page, /分支已推送，由画板创建 PR/);
  assert.match(page, /由画板创建 PR/);
  assert.match(page, /git\.create_pull_request|\/pull-request/);
  assert.match(page, /Git Worker 创建 PR 失败/);
  assert.match(page, /手工方式（备用）/);
  assert.match(page, /登记 PR/);
  assert.match(page, /PR 已登记，自动合并凭据尚未配置/);
  assert.match(page, /providerCapabilities\?\.github_api_enabled/);
  assert.match(page, /providerApiEnabled/);
  assert.match(page, /event !== "begin_merge" \|\| canBeginMerge/);
  assert.match(page, /payload\.error\?\.message \|\| payload\.detail/);
  assert.doesNotMatch(page, /PR #\{link\.pull_request_number\}/);
  assert.match(layout, /lang="zh-CN"/);
  assert.match(layout, /画板/);
  assert.match(layout, /huaban-logo\.png/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.deepEqual([...logo.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
});
