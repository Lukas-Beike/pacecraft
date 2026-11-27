const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

// One button concept across the app (#4279): a static scan of public/*.js and index.html. Words in dialogs and
// forms (Löschen red and leftmost, Speichern and Übernehmen always `push-button`, adding as "+ Objekt", undo as
// `message-undo`), icons on rows and cards, and one close glyph as `icon-button`.
const PUBLIC = path.join(__dirname, "..", "public");
const read = (name) => fs.readFileSync(path.join(PUBLIC, name), "utf8").replaceAll("\r\n", "\n");
const SOURCES = fs.readdirSync(PUBLIC).filter((name) => name.endsWith(".js")).map((name) => [name, read(name)]);
const HTML = read("index.html");

// Every <button> of the markup and every h("button", ...) of the scripts: [file, line, class names, text].
function buttons() {
  const found = [];
  for (const match of HTML.matchAll(/<button\b([^>]*)>([^<]*)<\/button>/g)) {
    const className = /class="([^"]*)"/.exec(match[1])?.[1] || "";
    found.push(["index.html", HTML.slice(0, match.index).split("\n").length, className.split(/\s+/), match[2].trim()]);
  }
  for (const [name, source] of SOURCES) {
    source.split("\n").forEach((line, index) => {
      for (const match of line.matchAll(/h\("button",\s*(?:\{([^}]*)\}|"([^"]*)")\s*,\s*"([^"]+)"/g)) {
        const className = /className:\s*"([^"]*)"/.exec(match[1] || "")?.[1] ?? match[2] ?? "";
        found.push([name, index + 1, className.split(/\s+/), match[3]]);
      }
    });
  }
  return found;
}

const where = (rows) => rows.map(([file, line, , text]) => `${file}:${line} "${text}"`);

test("save and accept buttons are always push-button (#4279)", () => {
  const wrong = buttons().filter(([, , classes, text]) => /^(Speichern|Lokal speichern|Profil speichern|Übernehmen)$/.test(text) && !classes.includes("push-button"));
  assert.deepEqual(where(wrong), []);
});

test("adding is \"+ Objekt\", never a bare Hinzufügen (#4279)", () => {
  const wrong = buttons().filter(([, , , text]) => /^(\+ )?Hinzufügen$/.test(text));
  assert.deepEqual(where(wrong), []);
});

test("an undo is always message-undo (#4279)", () => {
  const wrong = buttons().filter(([, , classes, text]) => text === "Rückgängig" && !classes.includes("message-undo"));
  assert.deepEqual(where(wrong), []);
});

test("a dialog's Löschen or Entfernen is red outlined and its leftmost button (#4279)", () => {
  const wrong = [];
  for (const block of HTML.matchAll(/<div class="dialog-actions">([\s\S]*?)<\/div>/g)) {
    const row = [...block[1].matchAll(/<button\b([^>]*)>([^<]*)<\/button>/g)];
    row.forEach((button, index) => {
      if (!/^(Löschen|Entfernen)$/.test(button[2].trim())) return;
      const classes = /class="([^"]*)"/.exec(button[1])?.[1].split(/\s+/) || [];
      if (index !== 0 || !classes.includes("danger-button")) wrong.push(`${button[2].trim()} at position ${index + 1}, classes: ${classes.join(" ")}`);
    });
  }
  assert.deepEqual(wrong, []);
});

test("the scan reads the buttons it guards", () => {
  const all = buttons();
  assert.ok(all.some(([file, , classes, text]) => file === "index.html" && text === "Speichern" && classes.includes("push-button")));
  assert.ok(all.some(([file, , classes, text]) => file !== "index.html" && text === "Rückgängig" && classes.includes("message-undo")));
});
