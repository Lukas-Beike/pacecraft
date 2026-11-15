const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { pathToFileURL } = require("node:url");

const ROOT = path.join(__dirname, "..");
const SCRIPT = path.join(ROOT, "scripts", "post_release_actions.mjs");
const load = () => import(pathToFileURL(SCRIPT).href);
const PASSWORD = "synthetic-password-for-a-test";

test("the address is checked and the secret comes from stdin, trimmed, never from a terminal", async () => {
  const { parseInput } = await load();
  const piped = (text) => ({ isTTY: false, read: () => text });
  assert.throws(() => parseInput([], piped(PASSWORD)), /required/);
  assert.throws(() => parseInput(["--url", "file:///etc/passwd"], piped(PASSWORD)), /http\(s\)/);
  assert.throws(() => parseInput(["--url", "http://127.0.0.1:8093", "--password-file", "x"], piped(PASSWORD)), /Unknown option/);
  assert.throws(() => parseInput(["--url", "http://127.0.0.1:8093"], { isTTY: true, read: () => PASSWORD }), /redirect the secret file/);
  assert.throws(() => parseInput(["--url", "http://127.0.0.1:8093"], piped("\n")), /empty/);
  assert.deepEqual(parseInput(["--url", "http://127.0.0.1:8093/some/path"], piped(`${PASSWORD}\n`)), { url: "http://127.0.0.1:8093", password: PASSWORD });
});

test("a refused input exits 2 and never prints the secret", () => {
  for (const [args, input] of [[["--url", "http://127.0.0.1:1"], "\n"], [["--url", "not a url"], PASSWORD], [[], PASSWORD]]) {
    const result = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8", input });
    assert.equal(result.status, 2);
    const output = result.stdout + result.stderr;
    assert.match(output, /^FAIL /);
    assert.ok(!output.includes(PASSWORD), "the secret is not printed");
  }
});

test("plan snapshots compare by id and content without exposing either", async () => {
  const { planSnapshot, samePlan } = await load();
  const unit = (id, date, hash) => ({ id, date, payload_hash: hash, archived: false, local_deleted: false });
  const before = planSnapshot([unit("a", "2026-11-20", "h1"), unit("b", "2026-11-21", "h2")]);
  assert.ok(samePlan(before, planSnapshot([unit("b", "2026-11-21", "h2"), unit("a", "2026-11-20", "h1")])));
  assert.ok(!samePlan(before, planSnapshot([unit("a", "2026-11-21", "h9"), unit("b", "2026-11-21", "h2")])), "a moved unit");
  assert.ok(!samePlan(before, planSnapshot([unit("a", "2026-11-20", "h1")])), "a removed unit");
  assert.ok(!samePlan(before, planSnapshot([unit("a", "2026-11-20", "h1"), unit("b", "2026-11-21", "h2"), unit("c", "2026-12-05", "h3")])), "an added unit");
});

// Planning and moving a unit wait until a Coach plan change is undoable as a whole turn (#2851).
test("the script asks only its question and presses only login and send", async () => {
  const { MESSAGES } = await load();
  assert.deepEqual(Object.keys(MESSAGES), ["ask"]);
  const source = fs.readFileSync(SCRIPT, "utf8");
  const clicks = [...source.matchAll(/(\S[^\n]*)\.click\(/g)].map((match) => match[1].trim());
  assert.deepEqual(clicks.sort((a, b) => a.localeCompare(b)), [
    'await page.getByRole("button", { name: "Anmelden" })',
    'await page.locator("#sendButton")',
  ]);
  assert.ok(!/\b(sync|garmin|intervals|delete|settings|nutrition|checkin|undo)\b/i.test(source.replace(/\/\/.*$/gm, "").replace(/"[^"\n]*"/g, "")), "no forbidden control is named in code");
});

test("the fixture answers exactly the script's question", async () => {
  const { MESSAGES } = await load();
  const fixture = fs.readFileSync(path.join(ROOT, "e2e", "fixture_runtime.py"), "utf8");
  assert.ok(fixture.includes(JSON.stringify(MESSAGES.ask).slice(1, -1)), "fixture has the same message");
});
