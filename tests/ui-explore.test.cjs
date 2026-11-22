const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SCRIPT = path.join(__dirname, "..", "scripts", "ui_explore.mjs");
const PASSWORD = "synthetic-password-for-a-test";

// #3913: a refused input exits 2 before any browser starts and never prints the secret or its path.
test("a refused input exits 2 and never prints the secret or its file", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ui-explore-"));
  try {
    const secret = path.join(dir, "secret-file");
    const plan = JSON.stringify({ steps: [{ go: "plan" }] });
    fs.writeFileSync(secret, PASSWORD);
    const base = ["--url", "http://127.0.0.1:1", "--out-dir", path.join(dir, "out")];
    // The plan comes only on standard input (`--plan -`), never from a path.
    for (const [args, input] of [
      [[...base, "--plan", "--password-file", secret], plan],
      [[...base, "--password-file", secret, "--plan", secret], plan],
      [[...base, "--password-file", secret, "--plan", "-"], PASSWORD],
      [[...base, "--password-file", secret, "--plan", "-"], JSON.stringify({ steps: [{ shot: "../x" }] })],
      [[...base, "--password-file", `${secret}-missing`, "--plan", "-"], plan],
      [["--url", "file:///x", "--password-file", secret, "--out-dir", dir, "--plan", "-"], plan],
    ]) {
      const result = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8", input });
      const output = result.stdout + result.stderr;
      assert.equal(result.status, 2, output);
      assert.match(output, /^FAIL /);
      assert.ok(!output.includes(PASSWORD) && !output.includes("secret-file"), output);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
