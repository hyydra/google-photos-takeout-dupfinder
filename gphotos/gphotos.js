// Google Photos duplicate audit and trash pipeline using Playwright.
// Usage:
//   node gphotos.js run [--limit 500] [--takeout ../takeout.sqlite] [--delete-gp-dups] [--delete-local-dups] [--keep]
//   node gphotos.js report
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = __dirname;
const PROFILE = path.join(ROOT, 'chrome-profile');
const DONE = path.join(ROOT, 'done.txt');
const FAILED = path.join(ROOT, 'failed.txt');
const TMP = path.join(ROOT, 'downloads', 'tmp');
const KEEP = path.join(ROOT, 'downloads', 'keep');
const THUMB_DIR = path.join(ROOT, 'thumbnails');
const DB = path.join(ROOT, 'gphotos.sqlite');
const LOG = path.join(ROOT, 'run-log.tsv');

fs.mkdirSync(TMP, { recursive: true });
fs.mkdirSync(THUMB_DIR, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const lines = (f) => (fs.existsSync(f) ? fs.readFileSync(f, 'utf8').split(/\r?\n/).filter(Boolean) : []);
const flagVal = (n) => { const i = process.argv.indexOf(n); return i > 0 ? process.argv[i + 1] : null; };
const idFromUrl = (u) => (u.match(/\/photo\/([A-Za-z0-9_-]+)/) || [])[1] || null;
const keep = process.argv.includes('--keep');

function getActivePage(context) {
  const pages = context.pages().filter((p) => !p.isClosed());
  return (
    pages.find((p) => p.url().includes('photos.google.com/photo/')) ||
    pages.find((p) => p.url().includes('photos.google.com')) ||
    pages[0] ||
    null
  );
}

async function launch() {
  // Purge any stale Chrome lockfiles before launching
  ['lockfile', 'SingletonLock', 'SingletonCookie', 'SingletonSocket'].forEach((f) => {
    try { fs.rmSync(path.join(PROFILE, f), { force: true }); } catch (e) {}
  });

  return chromium.launchPersistentContext(PROFILE, {
    channel: 'chrome',
    headless: false,
    viewport: null,
    acceptDownloads: true,
    downloadsPath: TMP,
    args: ['--start-maximized', '--no-first-run'],
  });
}

async function captureThumbnail(page, outPath) {
  if (fs.existsSync(outPath)) return true;
  try {
    const el = page.locator('img.BiCYpc, img[src*="photos.fife"], video').first();
    if ((await el.count()) > 0) {
      await el.screenshot({ path: outPath, type: 'jpeg', quality: 80, timeout: 2000 });
      return true;
    }
  } catch (e) {}
  try {
    const dims = await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight }));
    await page.screenshot({
      path: outPath,
      type: 'jpeg',
      quality: 75,
      clip: { x: 60, y: 60, width: Math.max(200, dims.width - 120), height: Math.max(200, dims.height - 120) },
    });
    return true;
  } catch (e) {}
  return false;
}

async function trashCurrent(page) {
  try {
    let dialog = page.locator('[role="dialog"], [role="alertdialog"]');
    if ((await dialog.count()) === 0) {
      await page.keyboard.press('#');
      await sleep(800);
    }

    if ((await dialog.count()) === 0) {
      const trashBtn = page.locator('button[aria-label*="Delete" i], button[aria-label*="Trash" i], button[aria-label*="Törlés" i]').first();
      if ((await trashBtn.count()) > 0) {
        await trashBtn.click().catch(() => {});
        await sleep(800);
      }
    }

    const confirmBtn = page
      .locator('[role="dialog"] button, [role="alertdialog"] button')
      .filter({ hasText: /move to trash|move to bin|trash|bin|áthelyez.*kuk|kuká/i })
      .last();

    if ((await confirmBtn.count()) > 0) {
      await confirmBtn.click();
      await sleep(1500);
      return true;
    }
  } catch (e) {}
  return false;
}

async function advanceNext(context) {
  const page = getActivePage(context);
  if (!page) return false;

  const beforeUrl = page.url();
  try {
    await page.keyboard.press('ArrowRight');
    await sleep(250);
  } catch (e) {}

  if (page.url() !== beforeUrl) return true;

  try {
    const nextBtn = page.locator('.SxgK2b.Cwtbxf, [aria-label*="next photo" i], [aria-label*="View next photo" i], [aria-label*="Következő" i]').first();
    if ((await nextBtn.count()) > 0) {
      await nextBtn.click({ force: true }).catch(() => {});
      await sleep(250);
    }
  } catch (e) {}

  if (page.url() !== beforeUrl) return true;

  try {
    const dims = await page.evaluate(() => ({ w: window.innerWidth, h: window.innerHeight }));
    await page.mouse.click(dims.w - 40, dims.h / 2);
    await sleep(250);
  } catch (e) {}

  return page.url() !== beforeUrl;
}

async function triggerDownload(context) {
  const page = getActivePage(context);
  if (!page) return null;

  for (const f of fs.readdirSync(TMP)) {
    try { fs.rmSync(path.join(TMP, f), { force: true }); } catch (e) {}
  }

  // 1. Focus the photo viewer canvas by clicking the image element
  try {
    const img = page.locator('img.BiCYpc, img[src*="photos.fife"], video').first();
    if ((await img.count()) > 0) {
      await img.click({ force: true }).catch(() => {});
      await sleep(150);
    }
  } catch (e) {}

  // 2. Send Shift+KeyD
  await page.keyboard.press('Shift+KeyD').catch(() => {});

  // 3. Poll TMP for downloaded file (including .crdownload once stream completes)
  const start = Date.now();
  let lastSize = 0;
  let stableCount = 0;

  while (Date.now() - start < 15000) {
    const files = fs.readdirSync(TMP);
    if (files.length > 0) {
      const fpath = path.join(TMP, files[0]);
      try {
        const sz = fs.statSync(fpath).size;
        if (sz > 0) {
          if (sz === lastSize) {
            stableCount++;
            if (stableCount >= 2) {
              const cleanPath = fpath.replace(/\.crdownload$/, '');
              if (fpath !== cleanPath) {
                try { fs.renameSync(fpath, cleanPath); } catch (e) {}
              }
              return fs.existsSync(cleanPath) ? cleanPath : fpath;
            }
          } else {
            lastSize = sz;
            stableCount = 0;
          }
        }
      } catch (e) {}
    }

    // If after 2.5s no file started downloading, retry Shift+KeyD or menu
    if (Date.now() - start > 2500 && files.length === 0) {
      try {
        await page.keyboard.press('Shift+KeyD').catch(() => {});
      } catch (e) {}
    }

    // If after 5s still no file, try clicking the 3-dots menu -> Download
    if (Date.now() - start > 5000 && files.length === 0) {
      try {
        const moreBtn = page.locator('button[aria-label*="More options" i], button[aria-label*="További" i], button[data-tooltip*="options" i], button[aria-label*="Options" i]').first();
        if ((await moreBtn.count()) > 0) {
          await moreBtn.click().catch(() => {});
          await sleep(400);
          const dlItem = page.locator('[role="menuitem"], [role="menu"] div, div[jsaction*="click"]').filter({ hasText: /download|letöltés/i }).first();
          if ((await dlItem.count()) > 0) {
            await dlItem.click().catch(() => {});
          }
        }
      } catch (e) {}
    }

    await sleep(300);
  }

  return null;
}

async function run() {
  const takeout = flagVal('--takeout');
  const delGp = process.argv.includes('--delete-gp-dups');
  const delLocal = process.argv.includes('--delete-local-dups');
  const limit = Number(flagVal('--limit')) || Infinity;
  const done = new Set(lines(DONE));

  console.log(`[START] Launching Playwright with saved profile (Limit: ${limit === Infinity ? 'Unlimited' : limit})`);
  console.log(`[CONFIG] Takeout DB: ${takeout || 'None'} | Auto-Trash GP dups: ${delGp} | Auto-Trash Local dups: ${delLocal}`);

  const context = await launch();
  let page = getActivePage(context) || (await context.newPage());

  await page.goto('https://photos.google.com/', { waitUntil: 'domcontentloaded' });
  await sleep(2500);

  const photoLinks = page.locator('c-wiz a[href*="/photo/"], a.p137Zd, a[href*="/photo/"]');
  if ((await photoLinks.count()) === 0) {
    throw new Error('No photos found in Google Photos library. Please verify login.');
  }

  // Click first photo to enter viewer
  await photoLinks.first().click();
  await sleep(2000);

  let count = 0;
  let stuck = 0;
  let lastId = null;

  console.log(`\nStarting inspection loop...`);

  while (count < limit && stuck < 40) {
    page = getActivePage(context);
    if (!page) {
      await sleep(1000);
      continue;
    }

    if (!page.url().includes('/photo/')) {
      const pl = page.locator('c-wiz a[href*="/photo/"], a.p137Zd, a[href*="/photo/"]');
      if ((await pl.count()) > 0) {
        await pl.first().click();
        await sleep(2000);
      }
      continue;
    }

    const id = idFromUrl(page.url());
    if (!id) {
      await sleep(1000);
      stuck++;
      continue;
    }

    if (id === lastId) {
      stuck++;
      if (stuck > 3) {
        await page.evaluate(() => {
          if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
        }).catch(() => {});
      }
      await advanceNext(context);
      await sleep(400);
      continue;
    }

    stuck = 0;
    lastId = id;
    let trashed = false;

    if (!done.has(id)) {
      try {
        // Allow photo viewer to hydrate and render
        await sleep(1400);
        page = getActivePage(context) || page;

        // 1. Capture preview thumbnail
        await captureThumbnail(page, path.join(THUMB_DIR, `${id}.jpg`));

        // 2. Download original file
        const downloadedPath = await triggerDownload(context);
        if (!downloadedPath || !fs.existsSync(downloadedPath)) {
          throw new Error('Download timeout (could not fetch original file)');
        }

        let target = downloadedPath;
        if (keep) {
          fs.mkdirSync(KEEP, { recursive: true });
          target = path.join(KEEP, `${id}_${path.basename(downloadedPath)}`);
          fs.copyFileSync(downloadedPath, target);
        }

        // 3. Ingest & measure metadata
        const args = ['gp_ingest.py', DB, id, target];
        if (keep) args.push('--keep');
        if (takeout) args.push('--takeout', path.resolve(takeout));

        const r = spawnSync('python', args, { cwd: ROOT, encoding: 'utf8', maxBuffer: 1 << 26 });
        if (r.status !== 0) throw new Error('Ingest failed: ' + r.stderr);

        const v = JSON.parse(r.stdout.trim().split('\n').pop());
        let action = 'KEEP';
        if (v.gp_dup_of) action = delGp ? 'TRASH' : 'WOULD_TRASH';
        else if (v.local_dup) action = delLocal ? 'TRASH' : 'WOULD_TRASH';
        else if (v.probable) action = 'PROBABLE_REVIEW';

        if (action === 'TRASH') {
          page = getActivePage(context) || page;
          await trashCurrent(page);
          trashed = true;
        }

        // 4. Record to log and database
        fs.appendFileSync(
          LOG,
          [id, action, v.sha, v.size, `${v.w}x${v.h}`, v.dt, v.make, v.model, v.gp_dup_of || '', v.local_dup || '', v.probable || ''].join('\t') + '\n'
        );
        fs.appendFileSync(DONE, id + '\n');
        done.add(id);
        count++;

        // 5. Update HTML report live on disk
        spawnSync('python', ['build_html_report.py', '--log', LOG, '--out', path.join(ROOT, 'report.html')], { cwd: ROOT });

        process.stdout.write(`\r[${count}] Processed: ${id} | Verdict: ${action} | Dims: ${v.w}x${v.h}   \n`);
      } catch (e) {
        fs.appendFileSync(FAILED, `${id}\t${e.message.replace(/\s+/g, ' ')}\n`);
        fs.appendFileSync(DONE, id + '\n'); // Mark done so it doesn't block future scans
        done.add(id);
        console.log(`\n[SKIP] ${id}: ${e.message}`);
      }
    } else {
      process.stdout.write(`\rFast-forwarding: ${id}   `);
    }

    if (trashed) {
      await sleep(1000);
      page = getActivePage(context);
      if (page && idFromUrl(page.url()) === id) await advanceNext(context);
    } else {
      await advanceNext(context);
    }

    await sleep(done.has(id) ? 100 : 400);
  }

  console.log(`\n\n[DONE] Finished run. Total processed this session: ${count}`);
  console.log('Generating final HTML report...');
  spawnSync('python', ['build_html_report.py', '--log', LOG, '--out', path.join(ROOT, 'report.html')], { cwd: ROOT, stdio: 'inherit' });
  console.log(`Report ready: ${path.join(ROOT, 'report.html')}`);
  await context.close();
}

async function report() {
  console.log('Generating HTML visual grid report...');
  spawnSync('python', ['build_html_report.py', '--log', LOG, '--out', path.join(ROOT, 'report.html')], { cwd: ROOT, stdio: 'inherit' });
  console.log(`Report ready: ${path.join(ROOT, 'report.html')}`);
}

const cmd = process.argv[2] || 'run';
({ run, report }[cmd] || (() => { console.log('usage: node gphotos.js [run [flags] | report]'); }))()
  .catch((e) => { console.error(e); process.exit(1); });
