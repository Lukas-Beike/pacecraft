const test = require("node:test");
const assert = require("node:assert/strict");
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
  assert.equal(options(["--url", "http://localhost:8123", "--route", "coach", "--out", out, "--wait-for", "#card"]).waitFor, "#card");
  // The inject script comes on standard input only, never from a file path (Sonar S8707).
  const { injectScript } = await load();
  assert.equal(injectScript("-", () => "document.body.dataset.mockup = '1';"), "document.body.dataset.mockup = '1';");
  assert.throws(() => injectScript(SCRIPT, () => "x"), /standard input/);
  assert.throws(() => injectScript("-", () => "  \n"), /empty script/);
  assert.throws(() => options(["--url", "http://localhost:8123", "--route", "coach", "--out", out, "--inject", SCRIPT]), /standard input/);
});
