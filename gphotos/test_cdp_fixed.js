const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const PROFILE = path.join(__dirname, 'chrome-profile');
const TMP = path.join(__dirname, 'downloads', 'tmp');
fs.mkdirSync(TMP, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  ['lockfile', 'SingletonLock', 'SingletonCookie', 'SingletonSocket'].forEach((f) => {
    try { fs.rmSync(path.join(PROFILE, f), { force: true }); } catch (e) {}
  });

  const context = await chromium.launchPersistentContext(PROFILE, {
    channel: 'chrome',
    headless: false,
    viewport: null,
    acceptDownloads: true,
    args: [
      '--start-maximized',
      '--no-first-run',
      '--safebrowsing-disable-download-protection',
      '--disable-features=DownloadBubble,DownloadBubbleV2'
    ],
  });

  const page = context.pages()[0] || (await context.newPage());
  const cdp = await context.newCDPSession(page);

  await cdp.send('Page.setDownloadBehavior', {
    behavior: 'allow',
    downloadPath: TMP,
  });

  await page.goto('https://photos.google.com/photo/AF1QipPb4UKuC4elOBvSHbjbG5Q912CdX8jjpzkPjWUG', { waitUntil: 'domcontentloaded' });
  await sleep(2500);

  for (const f of fs.readdirSync(TMP)) {
    try { fs.rmSync(path.join(TMP, f), { force: true }); } catch (e) {}
  }

  console.log('Sending Shift+KeyD...');
  await page.keyboard.press('Shift+KeyD');

  console.log('Waiting for file in TMP...');
  const start = Date.now();
  let found = null;
  while (Date.now() - start < 15000) {
    const all = fs.readdirSync(TMP);
    const complete = all.filter(f => !f.endsWith('.crdownload') && !f.endsWith('.tmp'));
    if (complete.length > 0 && fs.statSync(path.join(TMP, complete[0])).size > 0) {
      found = path.join(TMP, complete[0]);
      break;
    }
    await sleep(300);
  }

  if (found) {
    console.log('SUCCESS! File downloaded to TMP:', found, 'size:', fs.statSync(found).size);
  } else {
    console.log('Failed. Files in TMP:', fs.readdirSync(TMP));
  }

  await sleep(1000);
  await context.close();
  process.exit(0);
})();
