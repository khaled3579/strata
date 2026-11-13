"use strict";

const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const {spawn} = require("node:child_process");
const {setTimeout: delay} = require("node:timers/promises");

async function smoke(input, work, port = 4173, browserPath) {
  const settings = JSON.parse(fs.readFileSync(path.join(input, "launch.json"), "utf8"));
  const origin = `http://127.0.0.1:${port}`;
  const url = new URL(settings.page, origin);
  const report = {status: "setup_failed", page: url.pathname, browser_errors: []};
  let browser, app, staticServer, startup = "";
  function error(message) {
    if (report.browser_errors.length < 5) report.browser_errors.push(String(message).slice(0, 600));
  }
  try {
    const {chromium} = require(path.join(input, "Playwright"));
    browser = await chromium.launch({headless: true, executablePath: browserPath, timeout: 12000, chromiumSandbox: true,
      args: ["--disable-background-networking", "--no-proxy-server"]});
    report.status = "failed";
    if (settings.static) {
      const root = path.resolve(work, settings.static);
      const types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
        ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
        ".jpg": "image/jpeg", ".webp": "image/webp", ".wasm": "application/wasm"};
      staticServer = http.createServer((req, res) => {
        try {
          const name = decodeURIComponent(new URL(req.url, origin).pathname);
          const filename = fs.realpathSync(path.resolve(root, "." + (name.endsWith("/") ? name + "index.html" : name)));
          const relative = path.relative(root, filename);
          if (relative.startsWith("..") || path.isAbsolute(relative) || !fs.statSync(filename).isFile()) throw Error();
          res.setHeader("Content-Type", types[path.extname(filename)] || "application/octet-stream");
          const stream = fs.createReadStream(filename);
          stream.on("error", () => res.destroy());
          stream.pipe(res);
        } catch {
          res.writeHead(404).end("Not found");
        }
      });
      await new Promise((resolve, reject) => {
        staticServer.once("error", reject);
        staticServer.listen(port, "127.0.0.1", resolve);
      });
    } else {
      const [tool, ...args] = settings.argv;
      const node = path.join(input, "Node/node.exe");
      const python = path.join(input, "Python/python.exe");
      const commands = {node: [node], npm: [node, path.join(input, "npm/bin/npm-cli.js")], python: [python, "-E", "-s"]};
      if (!Object.hasOwn(commands, tool)) throw Error("Unsupported launch tool.");
      const [program, ...prefix] = commands[tool];
      app = spawn(program, [...prefix, ...args.map(arg => arg.replaceAll("{port}", String(port)))],
        {cwd: path.join(work, settings.cwd), windowsHide: true,
        env: {...process.env, HOST: "127.0.0.1", PORT: String(port), CI: "true", npm_config_offline: "true",
          PATH: [path.dirname(node), path.dirname(python), process.env.PATH].join(path.delimiter)},
        stdio: ["ignore", "pipe", "pipe"]});
      app.on("error", err => { startup = err.message; });
      for (const stream of [app.stdout, app.stderr]) {
        stream.on("data", bytes => { startup = (startup + bytes.toString()).slice(-1600); });
      }
    }
    const page = await browser.newPage({serviceWorkers: "block"});
    await page.route("**/*", route => {
      const address = new URL(route.request().url());
      return address.origin === origin || ["data:", "blob:"].includes(address.protocol) ? route.continue() : route.abort();
    });
    const until = Date.now() + 15000;
    while (true) {
      try {
        await fetch(url, {method: "HEAD", redirect: "manual", signal: AbortSignal.timeout(800)});
        break;
      } catch {
        if (Date.now() >= until || (app && app.exitCode !== null)) {
          throw Error("The application did not start on " + origin + ". " + startup);
        }
        await delay(250);
      }
    }
    page.on("pageerror", err => error(err.message));
    page.on("console", msg => { if (msg.type() === "error") error(msg.text()); });
    page.on("response", res => {
      if (res.status() >= 400 && !res.request().isNavigationRequest()) error(`HTTP ${res.status()} ${new URL(res.url()).pathname}`);
    });
    const response = await page.goto(url.href, {waitUntil: "domcontentloaded", timeout: 10000});
    report.http = response?.status();
    if (!response?.ok()) throw Error(`The home page returned HTTP ${report.http ?? "unknown"}.`);
    await page.waitForFunction(() => {
      const body = document.body;
      return body && body.getBoundingClientRect().height > 0 && (
        body.innerText.trim() || [...body.querySelectorAll("img, svg, canvas, video, iframe")].some(el => {
          const bounds = el.getBoundingClientRect();
          return bounds.width > 0 && bounds.height > 0 && getComputedStyle(el).visibility === "visible";
        })
      );
    }, null, {timeout: 6000});
    await delay(750);
    report.title = await page.title();
    if (!report.browser_errors.length) report.status = "passed";
  } catch (err) {
    report.output = String(err.message || err).slice(0, 1800);
  } finally {
    if (app) app.kill();
    if (browser) await browser.close();
    if (staticServer) staticServer.close();
  }
  return report;
}

if (require.main === module) {
  const input = __dirname;
  const root = path.dirname(input);
  const work = path.join(root, "Work");
  (async () => {
    let report;
    try {
      fs.cpSync(path.join(input, "Project"), work, {recursive: true});
      const settings = JSON.parse(fs.readFileSync(path.join(input, "launch.json"), "utf8"));
      const bases = [process.env["ProgramFiles(x86)"], process.env.ProgramFiles].filter(Boolean);
      const browser = [settings.browser && path.join(input, "Browser", settings.browser),
        ...bases.map(base => path.join(base, "Microsoft/Edge/Application/msedge.exe"))].filter(Boolean).find(fs.existsSync);
      report = browser ? await smoke(input, work, 4173, browser) :
        {status: "setup_failed", output: "Microsoft Edge is unavailable in Windows Sandbox."};
    } catch (err) {
      report = {status: "setup_failed", output: String(err.message || err).slice(0, 1600)};
    }
    fs.writeFileSync(path.join(root, "Result/report.json"), JSON.stringify(report));
  })();
}
module.exports = {smoke};
