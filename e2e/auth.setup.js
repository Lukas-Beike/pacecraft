const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("@playwright/test");

const AUTH_STATE_PATH = process.env.E2E_AUTH_STATE_PATH || path.join(__dirname, "..", "playwright", ".auth", "user.json");

// Outside CI a run without a password is a server-free run (#3026): specs such as
// ui-walk-steps.spec.js test a static page. They get an empty session; a spec that needs
// the app ends at its login dialog. CI always needs the password and the login.
async function withoutLogin() {
  await fs.mkdir(path.dirname(AUTH_STATE_PATH), { recursive: true });
  await fs.writeFile(AUTH_STATE_PATH, JSON.stringify({ cookies: [], origins: [] }));
  console.warn("E2E_APP_PASSWORD is unset: no login. Only server-free specs can pass; the others end at the login dialog.");
}

async function authenticate(config) {
  const started = performance.now();
  const password = process.env.E2E_APP_PASSWORD;
  if (!password && !process.env.CI) return withoutLogin();
  if (!password || password.length < 12) {
    throw new Error("E2E_APP_PASSWORD must be set to a fake password of at least 12 characters.");
  }
  const baseURL = config.projects[0]?.use?.baseURL;
  if (!baseURL) throw new Error("The Playwright baseURL is required for E2E authentication.");

  await fs.mkdir(path.dirname(AUTH_STATE_PATH), { recursive: true });
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ baseURL });
    const page = await context.newPage();
    await page.goto("/");
    await page.getByLabel("Passwort", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Anmelden" }).click();
    await page.locator("#loginDialog").waitFor({ state: "hidden" });
    await page.locator("#appShell").waitFor({ state: "visible" });
    await context.storageState({ path: AUTH_STATE_PATH });
    await context.close();
  } finally {
    await browser.close();
    if (process.env.E2E_PHASE_TIMINGS_PATH) {
      await fs.writeFile(process.env.E2E_PHASE_TIMINGS_PATH, JSON.stringify({ authentication_seconds: (performance.now() - started) / 1000 }));
    }
  }
}

module.exports = authenticate;
