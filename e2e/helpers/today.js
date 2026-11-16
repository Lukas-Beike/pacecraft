// `run_browser_tests.py --today YYYY-MM-DD` (#3200): the fixture's athlete clock and, through this
// test, the browser clock show that date, so a spec whose expectation depends on the weekday fails
// in its own run on a pinned Monday or Sunday instead of on every PR from that day on.
const base = require("@playwright/test");

const pinned = process.env.E2E_TODAY || "";

const test = pinned
  ? base.test.extend({
      page: async ({ page }, use) => {
        // Midday keeps the date in every time zone the fixture profile may use; time keeps flowing.
        await page.clock.install({ time: new Date(`${pinned}T12:00:00Z`) });
        await use(page);
      },
    })
  : base.test;

// The date a spec computes outside the page: the pinned one, else today in the given time zone.
function todayIn(timeZone) {
  return pinned || new Intl.DateTimeFormat("en-CA", { timeZone }).format(new Date());
}

module.exports = { ...base, test, todayIn };
