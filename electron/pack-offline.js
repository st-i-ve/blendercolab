/* Build dist/fleet-electron/ without electron-builder.
 *
 *     node pack-offline.js
 *
 * WHY THIS EXISTS. `npm run pack` (electron-builder --dir) gets as far as
 * "packaging platform=win32 electron=43.4.0", reports the cached Electron
 * zip at 100%, then makes one more HTTPS request and sits on it until its
 * 600-second timeout -- on 2026-08-17, with the Electron zip, winCodeSign
 * and NSIS caches all primed by hand and signing discovery disabled. The
 * artifact was a network round trip away, not a config change away.
 *
 * So this does the copying electron-builder would have done, from files
 * already on disk. It is NOT a replacement for electron-builder: no
 * installer, no asar, no cross-platform targets. `npm run dist` and the
 * GitHub Actions matrix remain the way real installers get made. This is
 * how you get a double-clickable app on a connection that cannot finish
 * the download.
 *
 * WHAT IT PRODUCES -- deliberately shaped like dist/blendfleetweb/, the
 * program first and the parts it runs beside it:
 *
 *   dist/fleet-electron/
 *     BlendFleet.exe          the renamed Electron binary
 *     backend/                the frozen Python sidecar (30 MB, no Qt)
 *     resources/app/          main.js, preload.js, shell.css
 *     resources/web/          the dashboard, byte for byte
 *     resources/assets/       fonts and logo
 *     *.dll, locales/, ...    Chromium's own files
 *
 * app.isPackaged is true in the result: Electron sets process.defaultApp
 * only when it is handed a path to run (`electron .`), and this build is
 * started by its own exe with resources/app in place. That is what makes
 * main.js resolve the sidecar beside the exe rather than in the repo.
 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const ROOT = path.join(HERE, '..');
const OUT = path.join(ROOT, 'dist', 'fleet-electron');
const RUNTIME = path.join(HERE, 'node_modules', 'electron', 'dist');
const SIDECAR = path.join(ROOT, 'dist', 'backend', 'blendfleet-backend');
const RCEDIT = path.join(HERE, 'node_modules', 'electron-winstaller',
                         'vendor', 'rcedit.exe');

const manifest = require('./package.json');

function copy(from, to) {
  fs.cpSync(from, to, { recursive: true });
}

function step(message) {
  process.stdout.write(`  • ${message}\n`);
}

/* Refuse rather than produce something half-assembled: a folder with an
   exe and no sidecar starts, shows the dashboard, and leaves every card
   empty -- the worst failure to hand someone. */
for (const [what, where] of [['the Electron runtime', RUNTIME],
                             ['the frozen sidecar', SIDECAR]]) {
  if (!fs.existsSync(where)) {
    console.error(`missing ${what}: ${where}`);
    console.error(where === SIDECAR
      ? 'build it first: npm run backend'
      : 'install it first: npm install');
    process.exit(1);
  }
}

if (fs.existsSync(OUT)) {
  step(`clearing ${path.relative(ROOT, OUT)}`);
  fs.rmSync(OUT, { recursive: true, force: true });
}

step('copying the Electron runtime');
copy(RUNTIME, OUT);

step(`renaming electron.exe to ${manifest.productName}.exe`);
fs.renameSync(path.join(OUT, 'electron.exe'),
              path.join(OUT, `${manifest.productName}.exe`));

/* default_app.asar is the "no app was given to me" screen. Leaving it
   next to a real app is harmless but misleading in a shipped folder. */
const placeholder = path.join(OUT, 'resources', 'default_app.asar');
if (fs.existsSync(placeholder)) fs.rmSync(placeholder);

step('installing the shell into resources/app');
const appDir = path.join(OUT, 'resources', 'app');
fs.mkdirSync(appDir, { recursive: true });
for (const file of manifest.build.files) copy(path.join(HERE, file),
                                              path.join(appDir, file));
/* A runtime manifest, not this one: the build config, devDependencies and
   scripts describe how the app is MADE and mean nothing to the app once
   it is made. */
fs.writeFileSync(path.join(appDir, 'package.json'), JSON.stringify({
  name: manifest.name,
  productName: manifest.productName,
  version: manifest.version,
  description: manifest.description,
  main: manifest.main,
  license: manifest.license,
}, null, 2) + '\n');

/* The same list electron-builder reads, so the two builds cannot drift:
   extraResources land in resources/, extraFiles beside the exe. */
step('copying extraResources');
for (const entry of manifest.build.extraResources) {
  copy(path.join(HERE, entry.from), path.join(OUT, 'resources', entry.to));
}

step('copying extraFiles');
for (const entry of manifest.build.extraFiles) {
  copy(path.join(HERE, entry.from), path.join(OUT, entry.to));
}

/* Cosmetic, and worth it: an unbranded exe in a folder called
   fleet-electron is a thing you have to remember is yours. */
if (fs.existsSync(RCEDIT)) {
  step('stamping the icon and version strings');
  try {
    execFileSync(RCEDIT, [
      path.join(OUT, `${manifest.productName}.exe`),
      '--set-icon', path.join(ROOT, 'assets', 'logo', 'blendfleet.ico'),
      '--set-version-string', 'ProductName', manifest.productName,
      '--set-version-string', 'FileDescription', manifest.description,
      '--set-version-string', 'CompanyName', manifest.productName,
      '--set-file-version', manifest.version,
      '--set-product-version', manifest.version,
    ], { stdio: 'pipe' });
  } catch (e) {
    /* Not fatal: the app runs, and its WINDOW icon comes from
       resources/assets/logo either way. Said out loud rather than
       swallowed, so a missing icon is explained. */
    step(`could not stamp the exe (${e.message.trim()}) -- continuing`);
  }
}

const exe = path.join(OUT, `${manifest.productName}.exe`);
step(`done: ${exe}`);
