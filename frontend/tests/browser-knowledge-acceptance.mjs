import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";


const APP_URL = process.env.BROWSER_AUDIT_URL || "http://127.0.0.1:5173";
const EDGE_PATH =
  process.env.EDGE_PATH
  || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const FRONTEND_ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
);
const OUTPUT_DIR =
  process.env.BROWSER_AUDIT_OUT
  || resolve(
    FRONTEND_ROOT,
    "..",
    "maintenance",
    "backups",
    "v0.5.0-final-acceptance",
  );
const DEBUG_PORT = 9600 + Math.floor(Math.random() * 200);
const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));


async function waitForDebugger() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
      if (response.ok) return await response.json();
    } catch {
      // Edge is still starting.
    }
    await sleep(200);
  }
  throw new Error("Edge debugger did not become available");
}


function connect(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  const pending = new Map();
  const listeners = new Map();
  let nextId = 1;
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
      return;
    }
    for (const listener of listeners.get(message.method) || []) {
      listener(message.params);
    }
  });
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  return {
    opened,
    close: () => socket.close(),
    on(method, listener) {
      const current = listeners.get(method) || [];
      listeners.set(method, [...current, listener]);
    },
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = nextId;
        nextId += 1;
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}


async function run() {
  await mkdir(OUTPUT_DIR, { recursive: true });
  const profileDir = await mkdtemp(join(tmpdir(), "netresearch-kg-edge-"));
  const edge = spawn(
    EDGE_PATH,
    [
      "--headless=new",
      `--remote-debugging-port=${DEBUG_PORT}`,
      "--remote-allow-origins=*",
      `--user-data-dir=${profileDir}`,
      "--no-first-run",
      "--disable-extensions",
      "--disable-gpu",
      "--hide-scrollbars",
      "about:blank",
    ],
    { stdio: "ignore", windowsHide: true },
  );
  let client;
  const browserErrors = [];
  try {
    const targets = await waitForDebugger();
    const target = targets.find((item) => item.type === "page");
    if (!target) throw new Error("No browser page target found");
    client = connect(target.webSocketDebuggerUrl);
    await client.opened;
    await Promise.all([
      client.send("Page.enable"),
      client.send("Runtime.enable"),
      client.send("Log.enable"),
      client.send("Emulation.setDeviceMetricsOverride", {
        width: 1440,
        height: 1000,
        deviceScaleFactor: 1,
        mobile: false,
      }),
    ]);
    client.on("Runtime.exceptionThrown", ({ exceptionDetails }) => {
      browserErrors.push(
        exceptionDetails.exception?.description
        || exceptionDetails.text
        || "Runtime exception",
      );
    });
    client.on("Runtime.consoleAPICalled", ({ type, args }) => {
      if (type === "error") {
        browserErrors.push(
          args.map((arg) => arg.value || arg.description || "").join(" "),
        );
      }
    });
    client.on("Log.entryAdded", ({ entry }) => {
      if (entry.level === "error") browserErrors.push(entry.text);
    });
    const evaluate = async (expression) => {
      const result = await client.send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      });
      if (result.exceptionDetails) {
        throw new Error(
          result.exceptionDetails.exception?.description
          || result.exceptionDetails.text,
        );
      }
      return result.result.value;
    };
    const waitFor = async (expression, timeout = 15_000) => {
      const started = Date.now();
      while (Date.now() - started < timeout) {
        try {
          if (await evaluate(expression)) return;
        } catch {
          // Navigation can temporarily invalidate the execution context.
        }
        await sleep(150);
      }
      throw new Error(`Timed out waiting for: ${expression}`);
    };
    const capture = async (name) => {
      const { data } = await client.send("Page.captureScreenshot", {
        format: "png",
        fromSurface: true,
        captureBeyondViewport: false,
      });
      const path = join(OUTPUT_DIR, `${name}.png`);
      await writeFile(path, Buffer.from(data, "base64"));
      return path;
    };
    await client.send("Page.navigate", {
      url: `${APP_URL}/knowledge-graph`,
    });
    await waitFor(
      'document.readyState === "complete" && Boolean(document.querySelector(".knowledge-page"))',
    );
    await waitFor(
      'Boolean(document.querySelector(".graph-canvas canvas"))',
      20_000,
    );
    const initial = await evaluate(`(() => ({
      title: document.title,
      toolbar: document.querySelector(".graph-toolbar")?.innerText.trim(),
      stats: Array.from(document.querySelectorAll(".stat")).map((item) => item.innerText.trim()),
      canvasCount: document.querySelectorAll(".graph-canvas canvas").length,
      health: null,
      missingProvenance: document.querySelector(".stats-grid")?.innerText.includes("缺失溯源"),
    }))()`);
    initial.health = await evaluate(
      `fetch("/api/health").then((response) => response.json())`,
    );
    const screenshotInitial = await capture("knowledge-graph-initial");

    await evaluate(`(() => {
      const input = document.querySelector(".filter-panel .el-input__inner");
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      ).set;
      setter.call(input, "NANOGPT");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      const button = Array.from(document.querySelectorAll(".filter-panel button"))
        .find((item) => item.innerText.trim() === "查询");
      button.click();
      return true;
    })()`);
    await sleep(1200);
    const filtered = await evaluate(`({
      toolbar: document.querySelector(".graph-toolbar")?.innerText.trim(),
      hasCanvas: Boolean(document.querySelector(".graph-canvas canvas")),
    })`);
    await evaluate(`(() => {
      const input = document.querySelector(".filter-panel .el-input__inner");
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      ).set;
      setter.call(input, "");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      Array.from(document.querySelectorAll(".filter-panel button"))
        .find((item) => item.innerText.trim() === "查询").click();
      return true;
    })()`);
    await sleep(1200);

    await evaluate(`(() => {
      const shortcut = document.querySelector(".node-shortcut");
      shortcut.click();
      return shortcut.innerText.trim();
    })()`);
    await waitFor('Boolean(document.querySelector(".detail-head"))', 20_000);
    await sleep(1200);
    const detailDebug = await evaluate(`({
      relationCards: document.querySelectorAll(".relation-card").length,
      detailText: document.querySelector(".detail-panel")?.innerText.slice(0, 1000),
      empty: document.querySelector(".detail-panel .el-empty")?.innerText.trim(),
    })`);
    if (!detailDebug.relationCards) {
      throw new Error(`Node detail rendered without relations: ${JSON.stringify(detailDebug)}`);
    }
    const nodeDetail = await evaluate(`(() => {
      const panel = document.querySelector(".detail-panel");
      const relation = panel?.querySelector(".relation-card");
      return {
        title: panel?.querySelector(".detail-head h3")?.innerText.trim(),
        relationCount: panel?.querySelectorAll(".relation-card").length,
        relationType: relation?.querySelector(".el-tag")?.innerText.trim(),
        source: relation?.querySelector(".relation-source")?.innerText.trim(),
        evidence: relation?.querySelector("blockquote")?.innerText.trim(),
      };
    })()`);
    const screenshotNode = await capture("knowledge-graph-node-detail");

    const clickTab = (label) => evaluate(`(() => {
      const tab = Array.from(document.querySelectorAll(".el-tabs__item"))
        .find((item) => item.innerText.trim() === ${JSON.stringify(label)});
      tab.click();
      return true;
    })()`);
    const clickButton = (label) => evaluate(`(() => {
      const button = Array.from(document.querySelectorAll("button"))
        .find((item) => item.innerText.trim() === ${JSON.stringify(label)} && item.offsetParent !== null);
      if (!button) return false;
      button.click();
      return true;
    })()`);

    await clickTab("核心论文");
    await waitFor('Boolean(document.querySelector(".core-card"))', 15_000);
    const corePapers = await evaluate(
      'document.querySelectorAll(".core-card").length',
    );

    await clickTab("方法比较");
    await waitFor('Boolean(document.querySelector(".analysis-form"))');
    await clickButton("比较方法");
    await waitFor('Boolean(document.querySelector(".method-card"))', 15_000);
    const methodComparison = await evaluate(`({
      methodCount: document.querySelectorAll(".method-card").length,
      summary: document.querySelector(".method-summary")?.innerText.trim(),
      hasEvidence: Boolean(document.querySelector(".method-card blockquote")),
    })`);

    await clickTab("关系路径");
    await waitFor('Boolean(document.querySelector(".path-form"))');
    const pathSelects = await evaluate(`(() => {
      const selects = document.querySelectorAll(".path-form .el-select__wrapper");
      if (selects.length !== 2) return 0;
      selects[0].click();
      return 1;
    })()`);
    if (!pathSelects) throw new Error("Path paper selectors not found");
    await sleep(350);
    await evaluate(`(() => {
      const options = Array.from(document.querySelectorAll(".el-select-dropdown__item"))
        .filter((item) => item.offsetParent !== null);
      options[Math.min(2, options.length - 1)].click();
      return true;
    })()`);
    await evaluate(
      'document.querySelectorAll(".path-form .el-select__wrapper")[1].click(); true',
    );
    await sleep(350);
    await evaluate(`(() => {
      const options = Array.from(document.querySelectorAll(".el-select-dropdown__item"))
        .filter((item) => item.offsetParent !== null);
      options[Math.min(7, options.length - 1)].click();
      return true;
    })()`);
    await clickButton("查找路径");
    await waitFor('Boolean(document.querySelector(".path-result"))', 15_000);
    const path = await evaluate(`({
      found: Boolean(document.querySelector(".path-chain")),
      depth: Array.from(document.querySelectorAll(".path-result p"))
        .find((item) => item.innerText.includes("最短路径深度"))?.innerText.trim(),
      evidenceCount: document.querySelectorAll(".path-evidence").length,
      fallbackMessage: document.querySelector(".path-result .el-alert")?.innerText.trim(),
    })`);

    await clickTab("研究空白");
    await waitFor(
      'Boolean(document.querySelector(".gap-card")) || Boolean(document.querySelector(".gap-grid .el-empty"))',
      15_000,
    );
    await clickButton("探索空白");
    await sleep(900);
    const gaps = await evaluate(`({
      gapCount: document.querySelectorAll(".gap-card").length,
      evidenceCount: document.querySelectorAll(".gap-card blockquote").length,
      types: Array.from(document.querySelectorAll(".gap-card .el-tag"))
        .map((item) => item.innerText.trim()),
    })`);
    const screenshotAnalysis = await capture("knowledge-graph-analysis");

    const report = {
      generatedAt: new Date().toISOString(),
      appUrl: APP_URL,
      initial,
      filtered,
      nodeDetail,
      corePapers,
      methodComparison,
      path,
      gaps,
      screenshotInitial,
      screenshotNode,
      screenshotAnalysis,
      browserErrors,
      valid: (
        browserErrors.length === 0
        && initial.health?.success === true
        && initial.health?.data?.version === "0.5.0"
        && initial.canvasCount > 0
        && Boolean(filtered.hasCanvas)
        && Boolean(nodeDetail.title)
        && nodeDetail.relationCount > 0
        && Boolean(nodeDetail.evidence)
        && corePapers > 0
        && methodComparison.methodCount > 0
        && (path.found || Boolean(path.fallbackMessage))
        && gaps.gapCount > 0
      ),
    };
    const reportPath = join(
      OUTPUT_DIR,
      "browser-knowledge-acceptance.json",
    );
    await writeFile(reportPath, JSON.stringify(report, null, 2), "utf8");
    console.log(JSON.stringify(report, null, 2));
    if (!report.valid) process.exitCode = 1;
  } finally {
    client?.close();
    edge.kill();
    await sleep(300);
    await rm(profileDir, { recursive: true, force: true });
  }
}


run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
