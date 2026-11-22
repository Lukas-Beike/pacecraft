const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { babelParse, traverse, extractFunctions } = require("./frontend-harness.cjs");

const PUBLIC = path.join(__dirname, "../public");

// Every confirmation names its question in a title and its action as a verb (#3934 G-02): no
// "Aktion bestätigen" and no "Bestätigen" button, and the text is not the title again.
test("every requestConfirmation call passes a title and a verb label", () => {
  const offenders = [];
  for (const file of fs.readdirSync(PUBLIC).filter((name) => name.endsWith(".js"))) {
    const source = fs.readFileSync(path.join(PUBLIC, file), "utf8");
    traverse(babelParse(source, file, false), {
      CallExpression(callPath) {
        const { callee, arguments: args } = callPath.node;
        if (callee.type !== "Identifier" || callee.name !== "requestConfirmation") return;
        const options = args[1]?.type === "ObjectExpression" ? args[1].properties : [];
        const named = new Set(options.map((property) => property.key?.name));
        if (!named.has("title") || !named.has("confirmLabel")) offenders.push(`${file}:${callPath.node.loc.start.line}`);
        // The question is the title's; the text names the consequence and asks nothing.
        const first = args[0];
        let text = "";
        if (first?.type === "StringLiteral") text = first.value;
        if (first?.type === "TemplateLiteral") text = first.quasis.map((part) => part.value.cooked).join("");
        if (text.includes("?")) offenders.push(`${file}:${callPath.node.loc.start.line} text asks a question`);
      },
    });
  }
  assert.deepEqual(offenders, []);
});

test("a confirmation without a title or a verb label is refused", () => {
  const { requestConfirmation } = extractFunctions("auth.js", ["requestConfirmation"], { $: () => null });
  assert.throws(() => requestConfirmation("Text", { title: "Frage?" }), /confirmLabel/);
  assert.throws(() => requestConfirmation("Text", { confirmLabel: "Löschen" }), /title/);
});
