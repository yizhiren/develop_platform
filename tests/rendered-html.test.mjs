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
  assert.match(html, /需求澄清者/);
  assert.match(html, /系统架构师/);
  assert.match(html, /开发工程师/);
  assert.match(html, /验收工程师/);
  assert.match(html, /src="\/huaban-logo\.png"/);
  assert.match(html, /alt="画板 Logo"/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("keeps the product metadata and security copy", async () => {
  const [page, layout, packageJson, logo] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../public/huaban-logo.png", import.meta.url)),
  ]);
  assert.match(page, /Agent 沙箱/);
  assert.match(page, /发布需求/);
  assert.match(layout, /lang="zh-CN"/);
  assert.match(layout, /画板/);
  assert.match(layout, /huaban-logo\.png/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.deepEqual([...logo.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
});
