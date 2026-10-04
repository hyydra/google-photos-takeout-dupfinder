const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const PROFILE = path.join(__dirname, 'chrome-profile');
const THUMB_DIR = path.join(__dirname, 'thumbnails');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const lines = fs.readFileSync(path.join(__dirname, 'run-log.tsv'), 'utf8').split(/\r?\n/).filter(Boolean);
const pids = lines.map(l => l.split('\t')[0]).filter(Boolean);

(async () => {
  ['lockfile', 'SingletonLock', 'SingletonCookie', 'SingletonSocket'].forEach((f) => {
    try { fs.rmSync(path.join(PROFILE, f), { force: true }); } catch (e) {}
  });

  const context = await chromium.launchPersistentContext(PROFILE, {
    channel: 'chrome',
    headless: false,
    viewport: { width: 1280, height: 800 },
    args: ['--start-maximized', '--no-first-run'],
  });

  const page = context.pages()[0] || await context.newPage();

  console.log(`Backfilling real previews for ${pids.length} items...`);

  for (let i = 0; i < pids.length; i++) {
    const pid = pids[i];
    const thumbPath = path.join(THUMB_DIR, `${pid}.jpg`);
    
    console.log(`[${i + 1}/${pids.length}] Fetching preview for ${pid}...`);
    try {
      await page.goto(`https://photos.google.com/photo/${pid}`, { waitUntil: 'domcontentloaded' });
      await sleep(1500);

      const img = page.locator('img.BiCYpc:visible, img[src*="photos.fife"]:visible, img[src*="googleusercontent.com"]:visible').first();
      await img.waitFor({ state: 'visible', timeout: 6000 }).catch(() => {});
      
      if (await img.count() > 0) {
        await img.screenshot({ path: thumbPath, type: 'jpeg', quality: 85, timeout: 3000 });
        console.log(`  -> Saved crisp preview: ${fs.statSync(thumbPath).size} bytes`);
      } else {
        // Screenshot main viewport
        await page.screenshot({ path: thumbPath, type: 'jpeg', quality: 80 });
        console.log(`  -> Fallback page screenshot: ${fs.statSync(thumbPath).size} bytes`);
      }
    } catch (e) {
      console.log(`  -> Error: ${e.message}`);
    }
  }

  await context.close();
  console.log('\nBackfill completed. Rebuilding HTML report...');
  require('child_process').spawnSync('python', ['build_html_report.py', '--log', 'run-log.tsv', '--out', 'report.html'], { cwd: __dirname, stdio: 'inherit' });
})();
