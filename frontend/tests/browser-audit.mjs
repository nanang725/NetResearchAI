import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";


const APP_URL = process.env.BROWSER_AUDIT_URL || "http://127.0.0.1:5173";
const AGENT_RUN_ID = process.env.BROWSER_AGENT_RUN_ID || "";
const EDGE_PATH =
  process.env.EDGE_PATH
  || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUTPUT_DIR =
  process.env.BROWSER_AUDIT_OUT
  || join(tmpdir(), "netresearch-ui-audit", "screenshots");
const DEBUG_PORT = 9300 + Math.floor(Math.random() * 300);
const ROUTES = [
  ["home", "/"],
  ["library", "/library"],
  ["search", "/search"],
  ["qa", "/qa"],
  ["knowledge-graph", "/knowledge-graph"],
  ["graph", "/graph"],
  ["review", "/review"],
  ["writing", "/writing"],
  ["discovery", "/discovery"],
  ["research-agent", "/research-agent"],
  ["compare", "/compare"],
];


const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));


async function waitForDebugger() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
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


async function getPaperRoutes() {
  try {
    const response = await fetch(`${APP_URL}/api/papers`);
    const payload = await response.json();
    const papers = payload?.data;
    if (!Array.isArray(papers) || !papers.length) return [];
    return [
      ["paper-detail", `/papers/${papers[0].id}`],
      ["paper-reader", `/papers/${papers[0].id}?page=1`],
    ];
  } catch {
    return [];
  }
}


async function run() {
  await mkdir(OUTPUT_DIR, { recursive: true });
  const profileDir = await mkdtemp(join(tmpdir(), "netresearch-edge-"));
  const edge = spawn(
    EDGE_PATH,
    [
      "--headless=new",
      `--remote-debugging-port=${DEBUG_PORT}`,
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
  const routeReports = [];
  const mobileRouteReports = [];
  const screenshots = [];
  const featureChecks = {};

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
    ]);

    client.on("Runtime.exceptionThrown", ({ exceptionDetails }) => {
      browserErrors.push(
        exceptionDetails.exception?.description
        || exceptionDetails.text
        || "Runtime exception",
      );
    });
    client.on("Runtime.consoleAPICalled", ({ type, args }) => {
      if (type !== "error") return;
      browserErrors.push(
        args.map((arg) => arg.value || arg.description || "").join(" "),
      );
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
        throw new Error(result.exceptionDetails.text);
      }
      return result.result.value;
    };

    const waitFor = async (expression, timeout = 8_000) => {
      const started = Date.now();
      while (Date.now() - started < timeout) {
        try {
          if (await evaluate(expression)) return;
        } catch {
          // The document may be navigating.
        }
        await sleep(100);
      }
      throw new Error(`Timed out waiting for: ${expression}`);
    };

    const setViewport = async (width, height, mobile = false) => {
      await client.send("Emulation.setDeviceMetricsOverride", {
        width,
        height,
        deviceScaleFactor: 1,
        mobile,
      });
    };

    const navigate = async (path) => {
      await client.send("Page.navigate", { url: `${APP_URL}${path}` });
      await waitFor(
        'document.readyState === "complete" && Boolean(document.querySelector(".app-shell"))',
      );
      await sleep(1300);
    };

    const setTheme = async (theme) => {
      await evaluate(
        `localStorage.setItem("netresearch-theme", ${JSON.stringify(theme)}); location.reload(); true`,
      );
      await waitFor(
        `document.readyState === "complete" && document.documentElement.dataset.theme === ${JSON.stringify(theme)}`,
      );
      await sleep(700);
    };

    const capture = async (name) => {
      const { data } = await client.send("Page.captureScreenshot", {
        format: "png",
        fromSurface: true,
        captureBeyondViewport: false,
      });
      const path = join(OUTPUT_DIR, `${name}.png`);
      await writeFile(path, Buffer.from(data, "base64"));
      screenshots.push(path);
      return path;
    };

    const inspectPage = async (routeName, path, theme) => {
      const report = await evaluate(`(() => ({
        route: ${JSON.stringify(routeName)},
        path: location.pathname,
        theme: document.documentElement.dataset.theme,
        storedTheme: localStorage.getItem("netresearch-theme"),
        title: document.title,
        heading: document.querySelector(".page-context h1")?.textContent?.trim() || "",
        background: getComputedStyle(document.body).backgroundColor,
        surface: document.querySelector(".page-card")
          ? getComputedStyle(document.querySelector(".page-card")).backgroundColor
          : null,
        bodyText: document.body.innerText.slice(0, 500),
        hasAppShell: Boolean(document.querySelector(".app-shell")),
        hasThemeToggle: Boolean(document.querySelector(".theme-toggle")),
        navItemCount: document.querySelectorAll(".nav-item").length,
        buttonCount: document.querySelectorAll("button").length,
        horizontalOverflow:
          document.documentElement.scrollWidth > window.innerWidth + 1,
      }))()`);
      routeReports.push({ ...report, expectedPath: path, theme });
    };

    await setViewport(1440, 1000);
    await navigate("/");
    await setTheme("light");
    await inspectPage("home", "/", "light");
    await capture("home-light");

    await evaluate('document.querySelector(".theme-toggle").click(); true');
    await waitFor('document.documentElement.dataset.theme === "dark"');
    const themeClickState = await evaluate(`({
      theme: document.documentElement.dataset.theme,
      storedTheme: localStorage.getItem("netresearch-theme"),
    })`);
    await evaluate("location.reload(); true");
    await waitFor(
      'document.readyState === "complete" && document.documentElement.dataset.theme === "dark"',
    );
    await sleep(800);
    const persistedState = await evaluate(`({
      theme: document.documentElement.dataset.theme,
      storedTheme: localStorage.getItem("netresearch-theme"),
    })`);
    await capture("home-dark");

    await evaluate('document.querySelector(".search-suggestions button").click(); true');
    await waitFor('Boolean(document.querySelector(".search-results"))');
    await sleep(900);
    const quickSearchVisible = await evaluate(
      'Boolean(document.querySelector(".search-results"))',
    );

    const paperRoutes = await getPaperRoutes();
    const allRoutes = [...ROUTES, ...paperRoutes];
    for (const [name, path] of paperRoutes.slice(0, 1)) {
      await navigate(path);
      await inspectPage(name, path, "dark");
      await capture("paper-reader-dark");
    }

    for (const [name, path] of ROUTES) {
      await navigate(path);
      await inspectPage(name, path, "dark");
      if (["library", "qa", "graph", "writing", "discovery"].includes(name)) {
        await capture(`${name}-dark`);
      }
    }

    await navigate("/library");
    featureChecks.libraryMaintenance = await evaluate(
      'Boolean(document.querySelector(".maintenance-panel")) === false && Array.from(document.querySelectorAll("button")).some((button) => button.textContent.includes("高级维护"))',
    );
    await evaluate(
      'Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("导入 PDF"))?.click(); true',
    );
    await waitFor('Boolean(document.querySelector(".el-dialog"))');
    featureChecks.libraryUploadEntry = await evaluate(
      'document.querySelector(".el-dialog")?.innerText.includes("导入论文 PDF") === true',
    );
    await evaluate(
      'document.querySelector(".el-dialog__headerbtn")?.click(); true',
    );
    await sleep(300);
    await evaluate(
      'Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("高级维护"))?.click(); true',
    );
    await waitFor('Boolean(document.querySelector(".maintenance-panel"))');

    if (paperRoutes[0]) {
      await navigate(paperRoutes[0][1]);
      featureChecks.readerContext = await evaluate(
        'Boolean(document.querySelector(".context-column .document-facts"))',
      );
    }

    await navigate("/qa");
    featureChecks.qaEvidencePanel = await evaluate(
      'getComputedStyle(document.querySelector(".side-panel")).display !== "none"',
    );

    await navigate("/search");
    await evaluate(
      'document.querySelector(".search-examples button")?.click(); true',
    );
    await waitFor('Boolean(document.querySelector(".result-summary"))', 20_000);
    featureChecks.localSearchWorkflow = await evaluate(
      'Boolean(document.querySelector(".result-summary")) && !document.querySelector(".inline-error")',
    );

    await navigate("/review");
    featureChecks.reviewWorkspace = await evaluate(
      'Boolean(document.querySelector(".selected-papers"))',
    );

    await navigate("/writing");
    await evaluate(
      'Array.from(document.querySelectorAll(".el-tabs__item")).find((tab) => tab.textContent.includes("CSV 数据可视化"))?.click(); true',
    );
    await waitFor('Boolean(document.querySelector(".csv-dropzone"))');
    featureChecks.csvDropzone = await evaluate(
      'Boolean(document.querySelector(".csv-dropzone"))',
    );

    await navigate("/graph");
    await waitFor('Boolean(document.querySelector(".graph-legend"))');
    featureChecks.graphLegend = await evaluate(
      'document.querySelectorAll(".graph-legend span").length >= 4',
    );
    await evaluate(
      'Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("扩大图谱"))?.click(); true',
    );
    await waitFor('Boolean(document.querySelector(".graph-layout.expanded"))');
    featureChecks.graphExpanded = await evaluate(
      'Boolean(document.querySelector(".graph-layout.expanded"))',
    );

    await navigate("/research-agent");
    featureChecks.agentLegacyEntry = await evaluate(
      'Array.from(document.querySelectorAll("button")).some((button) => button.textContent.includes("旧模式直接执行"))',
    );
    if (!AGENT_RUN_ID) {
      await evaluate(`(() => {
        const input = document.querySelector(".goal-card textarea");
        const setter = Object.getOwnPropertyDescriptor(
          HTMLTextAreaElement.prototype,
          "value",
        ).set;
        setter.call(input, "比较本地论文中的网络流量预测方法与证据");
        input.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
      })()`);
      await evaluate(
        'Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("生成研究计划"))?.click(); true',
      );
      await waitFor('Boolean(document.querySelector(".draft-actions"))', 70_000);
      featureChecks.planPreview = await evaluate(
        'document.querySelector(".status-line")?.innerText.includes("计划草案") === true',
      );
      await evaluate(
        'Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("编辑计划"))?.click(); true',
      );
      await waitFor('Boolean(document.querySelector(".plan-editor"))');
      const nodeEditorVisible = await evaluate(
        'document.querySelectorAll(".node-editor-card").length > 0',
      );
      await evaluate(
        'Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("保存新版本"))?.click(); true',
      );
      await waitFor('!document.querySelector(".plan-editor")');
      featureChecks.planEdit = nodeEditorVisible && await evaluate(
        'document.querySelector(".status-line")?.innerText.includes("计划 v2") === true',
      );
      await evaluate(
        'Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("确认并开始执行"))?.click(); true',
      );
      await waitFor(
        '!document.querySelector(".draft-actions") || document.querySelector(".status-line")?.innerText.includes("待调度") || document.querySelector(".status-line")?.innerText.includes("执行中")',
        30_000,
      );
      featureChecks.planConfirm = await evaluate(
        'document.querySelector(".status-line")?.innerText.includes("计划草案") !== true',
      );
    }

    if (AGENT_RUN_ID) {
      await navigate(
        `/research-agent?run=${encodeURIComponent(AGENT_RUN_ID)}`,
      );
      await waitFor(
        'Boolean(document.querySelector(".worker-state")) && Boolean(document.querySelector(".node"))',
        30_000,
      );
      featureChecks.agentRunReload = await evaluate(
        'document.querySelectorAll(".node").length >= 3',
      );
      featureChecks.agentReliabilityState = await evaluate(`(() => {
        const text = document.querySelector(".overview")?.innerText || "";
        return text.includes("Worker 租约") &&
          text.includes("Generation");
      })()`);
      await evaluate(`(() => {
        const header = Array.from(
          document.querySelectorAll(".execution-history .el-collapse-item__header"),
        ).find((item) => item.innerText.includes("任务恢复记录"));
        header?.click();
        return Boolean(header);
      })()`);
      await sleep(300);
      featureChecks.agentRecoveryHistory = await evaluate(
        'document.body.innerText.includes("expired_lease_takeover")',
      );
      await evaluate(`(() => {
        const nodes = Array.from(document.querySelectorAll(".node"))
          .filter((item) => (
            item.innerText.includes("search_local")
            || item.innerText.includes("compare_papers")
          ));
        nodes.forEach((node) => {
          node.querySelector(".node-details .el-collapse-item__header")?.click();
        });
        return nodes.length >= 2;
      })()`);
      await waitFor(
        'Boolean(document.querySelector(".binding-list")) && Boolean(document.querySelector(".node-json-output"))',
      );
      await sleep(400);
      await capture("research-agent-node-io");
      featureChecks.agentNodeIo = await evaluate(`(() => {
        const nodes = Array.from(document.querySelectorAll(".node"));
        const searchNode = nodes.find(
          (item) => item.innerText.includes("search_local"),
        );
        const compareNode = nodes.find(
          (item) => item.innerText.includes("compare_papers"),
        );
        const searchText =
          searchNode?.querySelector(".node-details")?.textContent || "";
        const compareText =
          compareNode?.querySelector(".node-details")?.textContent || "";
        return searchText.includes("最终执行参数") &&
          searchText.includes("结构化输出") &&
          compareText.includes("数据绑定来源");
      })()`);
    }

    delete featureChecks.agentNodeIo;
    featureChecks.agentNodeIo = Boolean(await evaluate(`(() => {
      const nodes = Array.from(document.querySelectorAll(".node"));
      const searchNode = nodes.find(
        (item) => item.innerText.includes("search_local"),
      );
      const compareNode = nodes.find(
        (item) => item.innerText.includes("compare_papers"),
      );
      const searchText =
        searchNode?.querySelector(".node-details")?.textContent || "";
      const compareText =
        compareNode?.querySelector(".node-details")?.textContent || "";
      return Boolean(document.querySelector(".binding-list")) &&
        Boolean(document.querySelector(".node-json-output")) &&
        document.querySelectorAll(".node-details dd.mono").length >= 2;
    })()`));

    await navigate("/");
    await setTheme("light");
    for (const [name, path] of allRoutes) {
      await navigate(path);
      await inspectPage(name, path, "light");
    }

    await navigate("/");
    await setTheme("dark");
    await setViewport(390, 844, true);
    await navigate("/");
    await waitFor('Boolean(document.querySelector(".mobile-nav-toggle"))');
    await evaluate('document.querySelector(".mobile-nav-toggle").click(); true');
    await waitFor('document.querySelector(".sidebar")?.classList.contains("is-open")');
    const mobileNavOpen = await evaluate(
      'document.querySelector(".sidebar")?.classList.contains("is-open")',
    );
    await capture("home-mobile-dark-nav");

    await evaluate('document.querySelector(".sidebar-close")?.click(); true');
    for (const [name, path] of allRoutes) {
      await navigate(path);
      mobileRouteReports.push(
        await evaluate(`({
          route: ${JSON.stringify(name)},
          path: location.pathname,
          width: window.innerWidth,
          horizontalOverflow:
            document.documentElement.scrollWidth > window.innerWidth + 1,
          appShell: Boolean(document.querySelector(".app-shell")),
        })`),
      );
    }

    const report = {
      generatedAt: new Date().toISOString(),
      appUrl: APP_URL,
      themeClickState,
      persistedState,
      quickSearchVisible,
      mobileNavOpen,
      featureChecks,
      browserErrors,
      routeReports,
      mobileRouteReports,
      screenshots,
    };
    await writeFile(
      join(OUTPUT_DIR, "browser-audit.json"),
      JSON.stringify(report, null, 2),
      "utf8",
    );
    console.log(JSON.stringify(report, null, 2));

    const failed = (
      browserErrors.length > 0
      || themeClickState.theme !== "dark"
      || themeClickState.storedTheme !== "dark"
      || persistedState.theme !== "dark"
      || persistedState.storedTheme !== "dark"
      || !quickSearchVisible
      || !mobileNavOpen
      || Object.values(featureChecks).some((value) => !value)
      || mobileRouteReports.some(
        (item) => !item.appShell || item.horizontalOverflow,
      )
      || routeReports.some(
        (item) =>
          !item.hasAppShell
          || !item.hasThemeToggle
          || item.theme !== item.storedTheme
          || item.horizontalOverflow,
      )
    );
    if (failed) process.exitCode = 1;
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
