#!/usr/bin/env node
/**
 * Build the vendored globe, then put back what `vite build` deletes.
 *
 * WHY THIS SCRIPT EXISTS
 *     `vite build` empties dist/ and repopulates it from the JS/CSS graph only.
 *     This project has no public/ directory, so the 3D aircraft models, the
 *     event imagery, and the SVG icons that live in dist/ are NOT build
 *     outputs — they are static content that was placed there upstream. A plain
 *     rebuild silently deletes 21 of them, including every aircraft .glb, and
 *     the globe comes up with invisible aircraft and no icons. Nothing warns
 *     you; the build succeeds.
 *
 *     So the build is two steps and the second one is not optional: snapshot
 *     the static content, build, restore. If the restore cannot account for
 *     every file it snapshotted, this exits non-zero rather than shipping a
 *     globe with holes in it.
 *
 * Usage:  node tools/build-dist.mjs [--check]
 *           --check   verify dist/ is complete without rebuilding
 */
import { execFileSync } from "node:child_process";
import {
  cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER = resolve(HERE, "..");
const DIST = join(SERVER, "dist");
const STAMP = join(SERVER, ".dist-static.json");

/**
 * The static content in dist/ that `vite build` will not reproduce: anything
 * that is not a hashed build output. Hashed assets live in assets/ and follow
 * the `name-HASH.ext` pattern vite emits.
 */
const STATIC_ROOTS = ["models", "events"];
const STATIC_FILES = [
  "location.svg", "logo.svg", "mic.svg", "pin.svg", "visual-presets.svg",
];

/**
 * vite emits content-hashed filenames (`index-BDNJLWx-.js`). Those are build
 * outputs and must NOT be preserved — keeping them means every rebuild leaves
 * last week's chunks behind, and dist/ grows by a few hundred KB each time
 * until nobody can tell which files the running app actually uses.
 *
 * The first version of this only checked for the `assets/` directory, which
 * kept Cesium's 300-odd static files (correct) but also 57 stale hashed
 * chunks (not correct). Hence the hash pattern.
 */
const HASHED = /-[A-Za-z0-9_-]{8}\.(js|css|wasm|webp|json)$/;

const isBuildOutput = (rel) =>
  rel === "index.html" ||
  rel.endsWith(".webmanifest") ||
  HASHED.test(rel.split("/").pop());

function walk(root, base = root) {
  const out = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const full = join(root, entry.name);
    if (entry.isDirectory()) out.push(...walk(full, base));
    else out.push(relative(base, full));
  }
  return out;
}

function snapshot() {
  if (!existsSync(DIST)) return [];
  return walk(DIST).filter((rel) => !isBuildOutput(rel)).sort();
}

function verify(manifest) {
  const missing = manifest.filter((rel) => !existsSync(join(DIST, rel)));
  if (missing.length) {
    console.error(`[build] MISSING ${missing.length} static file(s):`);
    for (const m of missing.slice(0, 25)) console.error(`   ${m}`);
    if (missing.length > 25) console.error(`   ... and ${missing.length - 25} more`);
    return false;
  }
  return true;
}

const checkOnly = process.argv.includes("--check");

if (checkOnly) {
  const manifest = existsSync(STAMP)
    ? JSON.parse(readFileSync(STAMP, "utf8"))
    : snapshot();
  if (!verify(manifest)) process.exit(1);
  console.log(`[build] dist/ is complete — ${manifest.length} static file(s) present`);
  process.exit(0);
}

// 1. remember what must survive
const keep = snapshot();
if (!keep.length) {
  console.error(
    "[build] dist/ has no static content to preserve. That is not expected —\n" +
    "        refusing to build rather than produce a globe with no aircraft.",
  );
  process.exit(1);
}
const stash = join(SERVER, ".dist-stash");
rmSync(stash, { recursive: true, force: true });
mkdirSync(stash, { recursive: true });
for (const rel of keep) {
  const dest = join(stash, rel);
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(join(DIST, rel), dest);
}
console.log(`[build] preserved ${keep.length} static file(s)`);

// 2. build
console.log("[build] running vite build…");
execFileSync("npx", ["vite", "build"], { cwd: SERVER, stdio: "inherit" });

// 3. put it back
let restored = 0;
for (const rel of keep) {
  const dest = join(DIST, rel);
  if (existsSync(dest)) continue;      // vite produced it after all
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(join(stash, rel), dest);
  restored++;
}
rmSync(stash, { recursive: true, force: true });
writeFileSync(STAMP, JSON.stringify(keep, null, 2));

console.log(`[build] restored ${restored} static file(s) that vite dropped`);
if (!verify(keep)) {
  console.error("[build] FAILED — dist/ is incomplete, not shipping this");
  process.exit(1);
}
const total = walk(DIST).length;
console.log(`[build] dist/ complete — ${total} files`);
