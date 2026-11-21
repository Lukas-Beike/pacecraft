// Sonar's own JavaScript rules for `tools/quality_baseline.py`: the page scripts, and
// Sonar's complexity and nested-ternary rules for the rest it analyzes (#3058).
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// The pre-push hook checks a clean export of HEAD, which has no node_modules.
const root = fileURLToPath(new URL("..", import.meta.url));
const modules = process.env.PACECRAFT_NODE_MODULES || join(root, "node_modules");
const load = createRequire(join(modules, "_"));
const sonarjs = load("eslint-plugin-sonarjs");
// Resolved from the packages that depend on them, not from npm's hoisting.
const fromEslint = createRequire(load.resolve("eslint"));
const espree = fromEslint("espree");
const eslintScope = fromEslint("eslint-scope");
const globals = createRequire(load.resolve("eslint-plugin-sonarjs"))("globals");

const SERVICE_WORKER = "public/service-worker.js";
const PARSE = { ecmaVersion: "latest", sourceType: "script", range: true, loc: true };

// `globalThis.name = ...` or `window.name = ...` anywhere in a file also shares a name.
function assignedGlobals(node, found) {
  if (!node || typeof node.type !== "string") return found;
  const target = node.type === "AssignmentExpression" ? node.left : null;
  if (target?.type === "MemberExpression" && !target.computed && ["globalThis", "window"].includes(target.object.name)) found.push(target.property.name);
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) if (child && typeof child === "object") assignedGlobals(child, found);
  }
  return found;
}

// The client is plain scripts sharing one global scope: what one file declares in it,
// every other page script may use (#3016). Derived from the checked tree's public/ (the
// ratchet runs ESLint there: a HEAD export or a test fixture), never listed by hand.
function pageDeclarations() {
  const tree = existsSync(join(process.cwd(), "public")) ? process.cwd() : root;
  const names = new Set();
  for (const file of readdirSync(join(tree, "public")).filter((name) => name.endsWith(".js"))) {
    if (`public/${file}` === SERVICE_WORKER) continue;
    let program;
    try {
      program = espree.parse(readFileSync(join(tree, "public", file), "utf8"), PARSE);
    } catch {
      continue; // ESLint reports the syntax error with the file's path
    }
    const scope = eslintScope.analyze(program, { ecmaVersion: 2022, sourceType: "script" }).globalScope;
    scope.variables.forEach((variable) => names.add(variable.name));
    assignedGlobals(program, []).forEach((name) => names.add(name));
  }
  return Object.fromEntries([...names].map((name) => [name, "readonly"]));
}

// Page scripts the Node unit tests require export behind this guard; only those files
// see `module`, so an unguarded `module.exports` elsewhere stays a finding.
const NODE_EXPORT_GUARD = 'typeof module !== "undefined"';
function nodeExportFiles() {
  const tree = existsSync(join(process.cwd(), "public")) ? process.cwd() : root;
  return readdirSync(join(tree, "public"))
    .filter((name) => name.endsWith(".js") && readFileSync(join(tree, "public", name), "utf8").includes(NODE_EXPORT_GUARD))
    .map((name) => `public/${name}`);
}

const sonarOnly = {
  plugins: { sonarjs },
  linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: "off" },
  rules: {
    "sonarjs/cognitive-complexity": ["error", 15],
    "sonarjs/no-nested-conditional": "error",
    // S4624 failed #3254 only in CI (#3271). S7776 (Set membership) maps to
    // unicorn/prefer-set-has, a plugin this repository does not install.
    "sonarjs/no-nested-template-literals": "error",
    // S8786 (super-linear regex) and S4036 (command from PATH) failed L28's
    // pull requests only in CI (#3381).
    "sonarjs/super-linear-regex": "error",
    "sonarjs/no-os-command-from-path": "error",
    // S5843 (regex too complex) failed #3781 only in CI (#3792).
    "sonarjs/regex-complexity": "error",
  },
};
// A call to a helper declared nowhere is a runtime ReferenceError (#3016).
const shared = { ...sonarOnly, rules: { ...sonarOnly.rules, "no-undef": "error" } };

export default [
  {
    files: ["public/**/*.js"],
    ignores: [SERVICE_WORKER],
    ...shared,
    languageOptions: { ecmaVersion: "latest", sourceType: "script", globals: { ...globals.browser, ...pageDeclarations() } },
  },
  {
    files: [SERVICE_WORKER],
    ...shared,
    languageOptions: { ecmaVersion: "latest", sourceType: "script", globals: globals.serviceworker },
  },
  ...nodeExportFiles().map((file) => ({ files: [file], languageOptions: { globals: { module: "readonly" } } })),
  // The Node scripts, browser specs and unit tests Sonar also analyzes (#3058).
  { files: ["scripts/**/*.mjs", "tools/**/*.mjs"], ...sonarOnly, languageOptions: { ecmaVersion: "latest", sourceType: "module" } },
  { files: ["e2e/**/*.js", "tests/**/*.cjs", "*.cjs"], ...sonarOnly, languageOptions: { ecmaVersion: "latest", sourceType: "commonjs" } },
];
