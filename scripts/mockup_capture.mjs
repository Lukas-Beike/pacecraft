// One app-style mockup from the disposable demo fixture (#3820).
//
//   node scripts/mockup_capture.mjs --url http://127.0.0.1:<port> --route analysis/running
//     --out <png in the temp directory> [--inject - < script.js]
//     [--width 412] [--theme light|dark] [--selector <css>] [--wait-for <css>]
//
// Starts from `scripts/demo-container.ps1`: logs in with the demo's published fake
// password, opens the route, runs the optional inject script in the page (the app's
// own globals such as `h`, `analysisChart` and `analysisChartGroup` draw the new
// part), hides the bottom navigation and the notices, and writes the page or the
// selected element as a PNG. Only a loopback address is accepted, so it never
// reaches the connected app. Exit 0 written, 1 capture failed, 2 bad input.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

import { baseUrl, scratchPath } from "./ui_walk.mjs";

// The demo container's fake password, printed by scripts/demo-container.ps1.
export const DEMO_PASSWORD = "e2e-fixture-password-1234";
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);
const HIDDEN = [".bottom-nav", "#statusStrip"];

export function options(argv) {
  const { values } = parseArgs({
    args: argv,
    options: {
      url: { type: "string" }, route: { type: "string" }, out: { type: "string" }, inject: { type: "string" },
      width: { type: "string", default: "412" }, theme: { type: "string", default: "light" }, selector: { type: "string" },
      "wait-for": { type: "string" },
    },
  });
  if (!values.url || !values.route || !values.out) throw new Error("--url, --route and --out are required");
  const url = baseUrl(values.url);
  if (!LOOPBACK.has(new URL(url).hostname)) throw new Error("only the local demo fixture (a loopback address) is captured");
  const width = Number(values.width);
  if (!Number.isInteger(width) || width < 280 || width > 1920) throw new Error("--width is a whole number of pixels from 280 to 1920");
  if (!["light", "dark"].includes(values.theme)) throw new Error("--theme is light or dark");
  if (path.extname(values.out).toLowerCase() !== ".png") throw new Error("--out names a .png file");
  return {
    url, width, theme: values.theme, selector: values.selector, waitFor: values["wait-for"],
    route: String(values.route).replace(/^#?\/?/, ""),
    out: scratchPath(values.out),
    inject: values.inject === undefined ? null : injectScript(values.inject, readStdin),
  };
}

// The inject script comes on standard input (`--inject -`), so no file path is read.
export function injectScript(flag, read) {
  if (flag !== "-") throw new Error("--inject - reads the script from standard input");
  const script = read();
  if (!script.trim()) throw new Error("--inject - got an empty script on standard input");
  return script;
}

const readStdin = () => fs.readFileSync(0, "utf8");

async function capture({ url, route, out, inject, width, theme, selector, waitFor }) {
  const { chromium } = await import("@playwright/test");
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ baseURL: url, viewport: { width, height: 915 }, colorScheme: theme, serviceWorkers: "block" });
    await page.goto("/");
    await page.getByLabel("Passwort", { exact: true }).fill(DEMO_PASSWORD);
    await page.getByRole("button", { name: "Anmelden" }).click();
    await page.locator("#appShell").waitFor({ state: "visible", timeout: 30000 });
    await page.goto(`/#${route}`);
    // `state` is a top-level const of a classic script: a global binding, not a property of globalThis (Gitar).
    await page.waitForFunction("typeof state !== 'undefined' && Boolean(state.data) && !state.loadPromise", null, { timeout: 30000 });
    // A view's later reads (the event stream keeps the network busy) finish when this element shows.
    if (waitFor) await page.locator(waitFor).first().waitFor({ state: "visible", timeout: 30000 });
    if (inject) await page.evaluate(`(async () => {\n${inject}\n})()`);
    await page.evaluate((hidden) => hidden.forEach((css) => document.querySelectorAll(css).forEach((node) => { node.style.display = "none"; })), HIDDEN);
    await page.waitForTimeout(300);
    const target = selector ? page.locator(selector).first() : page;
    await target.screenshot({ path: out, ...(selector ? {} : { fullPage: true }) });
  } finally {
    await browser.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  let parsed;
  try {
    parsed = options(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exit(2);
  }
  try {
    await capture(parsed);
    console.log(`written ${parsed.out}`);
  } catch (error) {
    console.error(`capture failed: ${error.message}`);
    process.exit(1);
  }
}
