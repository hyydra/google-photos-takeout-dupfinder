// Google Photos duplicate audit and trash pipeline (Puppeteer).
//
// Usage:
//   node gphotos.js run [--limit 500] [--takeout ../takeout.sqlite] [--keep] [--headless]
//   node gphotos.js trash [--headless]               # shows what it would do, trashes nothing
//   node gphotos.js trash --confirm <count> [--ids id1,id2,...] [--headless]
//   node gphotos.js report
//
// `run` walks the library newest-first with the photo viewer, downloads each original, and hands it to
// gp_ingest.py (SHA-256, pixel hash, perceptual hash, EXIF cross-match). It NEVER trashes anything: it
// only logs. Duplicates are reviewed and marked in the dashboard (saved to marked.txt), and only
// `trash --confirm <count>` moves exactly those marked photos to the Google Photos trash (recoverable
// for 60 days). Videos are skipped.
//
// Downloads are handled by Chrome itself (Browser.setDownloadBehavior) and picked up from a folder:
// Playwright's download interception closes the whole browser on Google Photos' download tab.
// Every browser step has a timeout and each photo is raced against a deadline, so a stuck page
// produces a logged error and a screenshot instead of a silent hang.
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { promisify } = require('util');
const execFileP = promisify(execFile);

const ROOT = __dirname;
const PROFILE = path.join(ROOT, 'chrome-profile');
const DONE = path.join(ROOT, 'done.txt');
const FAILED = path.join(ROOT, 'failed.txt');
const TMP = path.join(ROOT, 'downloads', 'tmp');
const KEEP = path.join(ROOT, 'downloads', 'keep');
const DB = path.join(ROOT, 'gphotos.sqlite');
const LOG = path.join(ROOT, 'run-log.tsv');
const STALL_SHOT = path.join(ROOT, 'debug_stall.png');

const PHOTO_DEADLINE_MS = 120000;
const END_AFTER_FAILED_NEXT = 5;
const REPORT_EVERY = 200;

fs.mkdirSync(TMP, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const lines = (f) => (fs.existsSync(f) ? fs.readFileSync(f, 'utf8').split(/\r?\n/).filter(Boolean) : []);
const flagVal = (n) => { const i = process.argv.indexOf(n); return i > 0 ? process.argv[i + 1] : null; };
const idFromUrl = (u) => (u.match(/\/photo\/([A-Za-z0-9_-]+)/) || [])[1] || null;
const log = (m) => console.log(`[${new Date().toTimeString().slice(0, 8)}] ${m}`);
const headless = process.argv.includes('--headless');

function withTimeout(promise, ms, label) {
  let t;
  const deadline = new Promise((_, rej) => { t = setTimeout(() => rej(new Error(`timeout after ${ms / 1000}s: ${label}`)), ms); });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(t));
}

class VideoSkip extends Error {}

async function launch() {
  for (const f of ['lockfile', 'SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
    try { fs.rmSync(path.join(PROFILE, f), { force: true }); } catch (e) {}
  }
  const browser = await puppeteer.launch({
    channel: 'chrome',
    headless,
    userDataDir: PROFILE,
    defaultViewport: headless ? { width: 1400, height: 900 } : null,
    args: headless ? ['--no-first-run'] : ['--start-maximized', '--no-first-run'],
  });
  // If Chrome goes away, stop instead of waiting forever.
  browser.on('disconnected', () => { log('browser closed, exiting'); process.exit(1); });

  // Chrome saves downloads straight into TMP.
  const cdp = await browser.target().createCDPSession();
  await cdp.send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: TMP });

  // An unhandled JS dialog freezes every page call: dismiss them.
  const watch = (p) => p.on('dialog', (d) => { log(`dialog ${d.type()}: ${d.message()}`); d.dismiss().catch(() => {}); });
  (await browser.pages()).forEach(watch);
  browser.on('targetcreated', async (t) => { if (t.type() === 'page') { const p = await t.page().catch(() => null); if (p) watch(p); } });
  return browser;
}

async function activePage(browser) {
  const pages = (await browser.pages()).filter((p) => !p.isClosed());
  return pages.find((p) => p.url().includes('photos.google.com/photo/'))
    || pages.find((p) => p.url().includes('photos.google.com'))
    || pages[0] || null;
}

// Count elements matching `selector` that are actually on screen (Google Photos preloads the
// neighbouring photos off-screen with identical classes).
const onScreenCount = (page, selector) => page.evaluate((s) => [...document.querySelectorAll(s)].filter((e) => {
  const r = e.getBoundingClientRect();
  return r.width > 50 && r.height > 50 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth
    && getComputedStyle(e).visibility !== 'hidden';
}).length, selector);

const MEDIA = 'img.BiCYpc, img[src*="photos.fife"], video';

// Wait until the viewer shows the photo, and say whether it is a video.
async function waitForViewer(page) {
  const start = Date.now();
  while ((await onScreenCount(page, MEDIA)) === 0) {
    if (Date.now() - start > 10000) throw new Error('viewer did not show a photo');
    await sleep(250);
  }
  const video = (await onScreenCount(page, 'video, [aria-label*="Play video" i]')) > 0;
  return video ? 'video' : 'image';
}

// Wait for a finished, size-stable file in TMP.
async function waitForFile(ms) {
  const start = Date.now();
  let last = -1, stable = 0;
  while (Date.now() - start < ms) {
    const names = fs.readdirSync(TMP);
    const f = names.find((n) => !n.endsWith('.crdownload') && !n.endsWith('.tmp'));
    if (f && !names.some((n) => n.endsWith('.crdownload'))) {
      const size = fs.statSync(path.join(TMP, f)).size;
      if (size > 0 && size === last && ++stable >= 2) return path.join(TMP, f);
      if (size !== last) { last = size; stable = 0; }
    }
    await sleep(400);
  }
  return null;
}

// Shift+D downloads the original; fall back to the "More options" menu.
async function downloadOriginal(page, id) {
  for (const f of fs.readdirSync(TMP)) fs.rmSync(path.join(TMP, f), { force: true });
  await sleep(1200); // let the viewer finish hydrating before sending keys

  await page.keyboard.down('Shift');
  await page.keyboard.press('KeyD');
  await page.keyboard.up('Shift');
  let file = await waitForFile(12000);

  if (!file) {
    await page.evaluate(() => {
      const b = document.querySelector('button[aria-label*="More options" i], button[aria-label*="Options" i]');
      if (b) b.click();
    });
    await sleep(500);
    await page.evaluate(() => {
      const m = [...document.querySelectorAll('[role="menuitem"]')].find((e) => /download/i.test(e.textContent));
      if (m) m.click();
    });
    file = await waitForFile(15000);
  }
  if (!file) throw new Error('download did not start');
  const target = path.join(TMP, `${id}_${path.basename(file)}`);
  fs.renameSync(file, target);
  return target;
}

async function ingest(id, file, takeout, keep) {
  const args = ['gp_ingest.py', DB, id, file];
  if (keep) args.push('--keep');
  if (takeout) args.push('--takeout', path.resolve(takeout));
  try {
    const { stdout } = await execFileP('python', args, { cwd: ROOT, maxBuffer: 1 << 26, timeout: 180000 });
    return JSON.parse(stdout.trim().split('\n').pop());
  } catch (e) {
    throw new Error('ingest failed: ' + String(e.stderr || e.message).trim().split('\n').pop());
  }
}

async function trashCurrent(page) {
  try {
    const hasDialog = () => page.evaluate(() => document.querySelectorAll('[role="dialog"], [role="alertdialog"]').length);
    if ((await hasDialog()) === 0) {
      await page.keyboard.press('#');
      await sleep(800);
    }
    if ((await hasDialog()) === 0) {
      await page.evaluate(() => {
        const b = document.querySelector('button[aria-label*="Delete" i], button[aria-label*="Trash" i]');
        if (b) b.click();
      });
      await sleep(800);
    }
    const clicked = await page.evaluate(() => {
      const btns = [...document.querySelectorAll('[role="dialog"] button, [role="alertdialog"] button')]
        .filter((b) => /move to trash|move to bin|trash|bin/i.test(b.textContent));
      const b = btns[btns.length - 1];
      if (b) { b.click(); return true; }
      return false;
    });
    if (clicked) { await sleep(1500); return true; }
  } catch (e) { log('trash failed: ' + e.message); }
  return false;
}

// Go to the next photo; resolves true once the URL shows a different photo id.
async function goNext(page, id) {
  const waitChange = () => page.waitForFunction(
    (cur) => { const m = location.href.match(/\/photo\/([A-Za-z0-9_-]+)/); return !!m && m[1] !== cur; },
    { timeout: 5000 }, id,
  ).then(() => true, () => false);

  await page.keyboard.press('ArrowRight').catch(() => {});
  if (await waitChange()) return true;
  await page.evaluate(() => {
    const b = document.querySelector('[aria-label*="next photo" i], [aria-label*="View next photo" i]');
    if (b) b.click();
  }).catch(() => {});
  return waitChange();
}

async function openFirstPhoto(page) {
  await page.goto('https://photos.google.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  try {
    await page.waitForSelector('a[href*="/photo/"]', { timeout: 20000 });
  } catch (e) {
    throw new Error('No photos found in Google Photos library. Please verify login.');
  }
  await page.evaluate(() => document.querySelector('a[href*="/photo/"]').click());
  await page.waitForFunction(() => /\/photo\//.test(location.href), { timeout: 15000 });
}

function rebuildReport() {
  return execFileP('python', ['build_html_report.py', '--log', LOG, '--out', path.join(ROOT, 'report.html')], { cwd: ROOT, timeout: 120000 })
    .catch((e) => log('report rebuild failed: ' + e.message));
}

async function run() {
  const takeout = flagVal('--takeout');
  if (process.argv.includes('--delete-gp-dups') || process.argv.includes('--delete-local-dups')) {
    console.error('--delete-gp-dups / --delete-local-dups no longer exist. Use --auto-trash (byte-identical online duplicates only), or mark duplicates in the dashboard and run: node gphotos.js trash --confirm <count>');
    process.exit(2);
  }
  const keep = process.argv.includes('--keep');
  const autoTrash = process.argv.includes('--auto-trash');
  const limit = Number(flagVal('--limit')) || Infinity;
  const done = new Set(lines(DONE));
  const statusById = new Map(lines(LOG).map((l) => l.split('\t')).map((c) => [c[0], c[1]]));

  log(`start (${autoTrash ? 'AUTO-TRASH on: byte-identical online duplicates go to the Google Photos trash' : 'log only'}) | limit ${limit === Infinity ? 'none' : limit} | takeout ${takeout || 'none'}`);

  const browser = await launch();
  const page = (await activePage(browser)) || (await browser.newPage());
  await openFirstPhoto(page);

  let count = 0, videos = 0, failedNext = 0, fastForwarded = 0;

  while (count < limit && failedNext < END_AFTER_FAILED_NEXT) {
    const id = idFromUrl(page.url());
    if (!id) { await sleep(500); failedNext++; continue; }

    let trashed = false;
    if (done.has(id)) {
      if (++fastForwarded % 50 === 0) log(`fast-forwarded ${fastForwarded} already-done photos`);
    } else {
      try {
        trashed = await withTimeout((async () => {
          if ((await waitForViewer(page)) === 'video') throw new VideoSkip();
          const file = await downloadOriginal(page, id);
          let target = file;
          if (keep) {
            fs.mkdirSync(KEEP, { recursive: true });
            target = path.join(KEEP, path.basename(file));
            fs.copyFileSync(file, target);
          }
          const v = await ingest(id, target, takeout, keep);
          if (keep && fs.existsSync(file)) fs.rmSync(file, { force: true });
          if (!v.w || !v.h) throw new VideoSkip(); // not a readable image

          // WOULD_TRASH = byte-identical to another photo; PROBABLE_REVIEW = same pixels / near-identical /
          // same EXIF but different bytes (never auto-trashed, only logged for review in the dashboard).
          let action = 'KEEP';
          if (v.gp_dup_of || v.local_dup) action = 'WOULD_TRASH';
          else if (v.probable) action = 'PROBABLE_REVIEW';

          // Auto-trash: only a byte-identical (SHA-256 + size) copy of an online photo that was kept.
          let didTrash = false;
          if (autoTrash && action === 'WOULD_TRASH' && v.gp_dup_of && v.sha && v.size > 0 && v.crc_confirmed
              && statusById.get(v.gp_dup_of) === 'KEEP') {
            didTrash = await trashCurrent(page);
            if (didTrash) action = 'TRASH';
          }
          statusById.set(id, action);

          fs.appendFileSync(LOG, [id, action, v.sha, v.size, `${v.w}x${v.h}`, v.dt, v.make, v.model,
            v.gp_dup_of || '', v.local_dup || '', v.probable || '', v.reason || '', v.crc || ''].join('\t') + '\n');
          fs.appendFileSync(DONE, id + '\n');
          done.add(id);
          count++;
          log(`#${count} ${id.slice(0, 12)}… ${action}${v.reason ? ' (' + v.reason + ')' : ''} ${v.w}x${v.h}`);
          return didTrash;
        })(), PHOTO_DEADLINE_MS, `photo ${id}`);
      } catch (e) {
        fs.appendFileSync(DONE, id + '\n');
        done.add(id);
        if (e instanceof VideoSkip) {
          videos++;
          log(`skip ${id.slice(0, 12)}… video/non-image (${videos} skipped)`);
        } else {
          fs.appendFileSync(FAILED, `${id}\t${e.message.replace(/\s+/g, ' ')}\n`);
          log(`FAILED ${id.slice(0, 12)}…: ${e.message}`);
          await page.screenshot({ path: STALL_SHOT }).catch(() => {});
        }
      }
      if (count > 0 && count % REPORT_EVERY === 0) await rebuildReport();
    }

    if (trashed) await sleep(1000); // after a trash Google Photos moves to the next photo by itself
    const moved = trashed && idFromUrl(page.url()) !== id ? true : await goNext(page, id);
    failedNext = moved ? 0 : failedNext + 1;
    if (!moved) log(`could not advance from ${id.slice(0, 12)}… (${failedNext}/${END_AFTER_FAILED_NEXT})`);
  }

  log(`finished: ${count} processed, ${videos} videos skipped, ${fastForwarded} fast-forwarded`);
  await rebuildReport();
  browser.removeAllListeners('disconnected');
  await browser.close();
}

// Move exactly the photos marked in the dashboard (marked.txt) to the Google Photos trash.
// Refuses unless --confirm <count> matches the number of eligible photos.
async function trashMarked() {
  const allMarks = lines(path.join(ROOT, 'marked.txt'));
  const onlyIds = flagVal('--ids'); // dashboard group button: trash exactly these ids
  const marked = onlyIds ? onlyIds.split(',').filter(Boolean) : allMarks;
  const status = new Map(lines(LOG).map((l) => l.split('\t')).map((c) => [c[0], c[1]]));
  const eligible = marked.filter((id) => ['WOULD_TRASH', 'PROBABLE_REVIEW'].includes(status.get(id)));
  console.log(`${marked.length} marked in the dashboard, ${eligible.length} eligible `
    + `(${marked.length - eligible.length} not in the scan log as a duplicate/suspect, or already trashed)`);
  if (eligible.length === 0) return;
  if (flagVal('--confirm') !== String(eligible.length)) {
    console.log(`\nNothing was trashed. To move these ${eligible.length} photos to the Google Photos trash `
      + `(recoverable for 60 days) run:\n  node gphotos.js trash --confirm ${eligible.length}`);
    return;
  }
  // A running scan owns the Chrome profile; a second Chrome on it would clash with the scan.
  const scanCount = await execFileP('powershell', ['-NoProfile', '-Command',
    "(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { $_.CommandLine -like '*gphotos.js*' -and $_.CommandLine -like '* run*' } | Measure-Object).Count"])
    .then((r) => parseInt(r.stdout, 10) || 0).catch(() => 0);
  if (scanCount > 0) {
    console.log('A scan is running and holds the Chrome profile. Stop it first, then run the trash step again.');
    return;
  }
  const browser = await launch();
  const page = (await activePage(browser)) || (await browser.newPage());
  let ok = 0;
  for (const id of eligible) {
    try {
      await page.goto(`https://photos.google.com/photo/${id}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await waitForViewer(page);
      if (idFromUrl(page.url()) !== id) throw new Error('viewer opened a different photo');
      await sleep(1000);
      if (!(await trashCurrent(page))) throw new Error('trash confirmation not found');
      fs.appendFileSync(path.join(ROOT, 'trashed.txt'), id + '\n');
      ok++;
      log(`trashed ${ok}/${eligible.length} ${id.slice(0, 12)}…`);
    } catch (e) {
      fs.appendFileSync(FAILED, `${id}\ttrash: ${e.message}\n`);
      log(`NOT trashed ${id.slice(0, 12)}…: ${e.message}`);
      await page.screenshot({ path: STALL_SHOT }).catch(() => {});
    }
  }
  // Reflect the result in the log and drop trashed ids from the marks.
  const trashedIds = new Set(lines(path.join(ROOT, 'trashed.txt')));
  fs.writeFileSync(LOG, lines(LOG).map((l) => {
    const c = l.split('\t');
    if (trashedIds.has(c[0])) c[1] = 'TRASH';
    return c.join('\t');
  }).join('\n') + '\n');
  fs.writeFileSync(path.join(ROOT, 'marked.txt'), allMarks.filter((id) => !trashedIds.has(id)).join('\n') + '\n');
  log(`done: ${ok} of ${eligible.length} moved to the Google Photos trash`);
  browser.removeAllListeners('disconnected');
  await browser.close();
}

// Download every online photo that was scanned before CRC32 existed, check its SHA-256 still matches the
// recorded one, and store the CRC32. Needed so old photos can act as a CRC-confirmed reference.
async function backfillCrc() {
  const scanCount = await execFileP('powershell', ['-NoProfile', '-Command',
    "(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { $_.CommandLine -like '*gphotos.js*' -and $_.CommandLine -like '* run*' } | Measure-Object).Count"])
    .then((r) => parseInt(r.stdout, 10) || 0).catch(() => 0);
  if (scanCount > 0) {
    console.log('A scan is running and holds the Chrome profile. Stop it first, then run backfill-crc again.');
    return;
  }
  const { stdout } = await execFileP('python', ['gp_crc.py', 'list', DB], { cwd: ROOT, timeout: 120000 });
  const ids = JSON.parse(stdout.trim().split('\n').pop());
  log(`backfill-crc: ${ids.length} photo(s) without a CRC`);
  if (!ids.length) return;
  const limit = Number(flagVal('--limit')) || Infinity;

  const browser = await launch();
  const page = (await activePage(browser)) || (await browser.newPage());
  let ok = 0, mismatch = 0, failed = 0;
  for (const id of ids.slice(0, limit)) {
    try {
      await withTimeout((async () => {
        await page.goto(`https://photos.google.com/photo/${id}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
        if (idFromUrl(page.url()) !== id) throw new Error('viewer opened a different photo');
        if ((await waitForViewer(page)) === 'video') throw new Error('is a video');
        const file = await downloadOriginal(page, id);
        const { stdout: out } = await execFileP('python', ['gp_crc.py', 'fix', DB, id, file], { cwd: ROOT, timeout: 180000 });
        const r = JSON.parse(out.trim().split('\n').pop());
        if (r.sha_match) { ok++; } else { mismatch++; log(`SHA-256 differs from the recorded one for ${id.slice(0, 12)}… (CRC not stored)`); }
      })(), PHOTO_DEADLINE_MS, `crc ${id}`);
      if ((ok + mismatch + failed) % 25 === 0) log(`crc backfill: ${ok} stored, ${mismatch} sha mismatches, ${failed} failed of ${ids.length}`);
    } catch (e) {
      failed++;
      log(`crc FAILED ${id.slice(0, 12)}…: ${e.message}`);
    }
  }
  log(`crc backfill done: ${ok} stored, ${mismatch} sha mismatches, ${failed} failed`);
  browser.removeAllListeners('disconnected');
  await browser.close();
}

async function report() {
  await rebuildReport();
  log(`report ready: ${path.join(ROOT, 'report.html')}`);
}

const cmd = process.argv[2] || 'run';
(cmd === 'report' ? report() : cmd === 'trash' ? trashMarked() : cmd === 'backfill-crc' ? backfillCrc() : run()).catch((e) => {
  console.error('\n[FATAL]', e.message);
  process.exit(1);
});
