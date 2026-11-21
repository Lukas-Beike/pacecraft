const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SCRIPT = path.join(__dirname, "..", "scripts", "mockup_capture.mjs");
const load = () => import(require("node:url").pathToFileURL(SCRIPT).href);

test("mockup capture reaches only the local demo and keeps its files in the temp directory", async () => {
  const { options } = await load();
  const out = path.join(os.tmpdir(), "mockup.png");
  const parsed = options(["--url", "http://127.0.0.1:8123/x", "--route", "#analysis/running", "--out", out]);
  assert.deepEqual(
    { url: parsed.url, route: parsed.route, width: parsed.width, theme: parsed.theme, inject: parsed.inject },
    { url: "http://127.0.0.1:8123", route: "analysis/running", width: 412, theme: "light", inject: null },
  );
  // Never the connected app: a non-loopback address is refused (#3820).
  assert.throws(() => options(["--url", "https://coach.example.invalid", "--route", "coach", "--out", out]), /loopback/);
  assert.throws(() => options(["--url", "http://127.0.0.1:8123", "--route", "coach", "--out", path.join(__dirname, "..", "shot.png")]), /system temp directory/);
  assert.throws(() => options(["--url", "http://127.0.0.1:8123", "--route", "coach", "--out", path.join(os.tmpdir(), "shot.jpg")]), /\.png/);
  assert.throws(() => options(["--url", "http://127.0.0.1:8123", "--route", "coach", "--out", out, "--width", "99"]), /280 to 1920/);
  assert.throws(() => options(["--url", "http://127.0.0.1:8123", "--route", "coach", "--out", out, "--theme", "blue"]), /light or dark/);
  assert.throws(() => options(["--url", "http://127.0.0.1:8123", "--out", out]), /required/);
  // The inject script is read from the temp directory only.
  const inject = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "mockup-")), "inject.js");
  fs.writeFileSync(inject, "document.body.dataset.mockup = '1';");
  const injected = options(["--url", "http://localhost:8123", "--route", "coach", "--out", out, "--inject", inject, "--wait-for", "#card"]);
  assert.match(injected.inject, /mockup/);
  assert.equal(injected.waitFor, "#card");
  assert.throws(() => options(["--url", "http://localhost:8123", "--route", "coach", "--out", out, "--inject", SCRIPT]), /system temp directory/);
});
