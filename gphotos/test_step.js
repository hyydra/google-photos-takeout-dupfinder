const { chromium } = require('playwright');
const path = require('path');

const PROFILE = path.join(__dirname, 'chrome-profile');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const idFromUrl = (u) => (u.match(/\/photo\/([A-Za-z0-9_-]+)/) || [])[1] || null;

(async () => {
  ['lockfile', 'SingletonLock', 'SingletonCookie', 'SingletonSocket'].forEach((f) => {
    try { require('fs').rmSync(path.join(PROFILE, f), { force: true }); } catch (e) {}
  });

  const context = await chromium.launchPersistentContext(PROFILE, {
    channel: 'chrome',
    headless: false,
    viewport: null,
    args: ['--start-maximized', '--no-first-run'],
  });

  const page = context.pages().find(p => p.url().includes('photos.google.com')) || context.pages()[0] || await context.newPage();
  if (!page.url().includes('/photo/')) {
    await page.goto('https://photos.google.com/', { waitUntil: 'domcontentloaded' });
    await sleep(2000);
    const photo = page.locator('c-wiz a[href*="/photo/"], a.p137Zd').first();
    await photo.click();
    await sleep(2000);
  }

  console.log('Current URL:', page.url());
  console.log('Current photo ID:', idFromUrl(page.url()));

  for (let i = 1; i <= 5; i++) {
    const beforeId = idFromUrl(page.url());
    console.log(`\n--- Step ${i}: Current ID: ${beforeId} ---`);
    
    // Press ArrowRight
    await page.keyboard.press('ArrowRight');
    
    // Wait for URL to change (up to 3s)
    let newId = null;
    const t0 = Date.now();
    while (Date.now() - t0 < 3000) {
      await sleep(150);
      const cur = idFromUrl(page.url());
      if (cur && cur !== beforeId) {
        newId = cur;
        break;
      }
    }
    
    if (newId) {
      console.log(`Step ${i} SUCCEEDED via ArrowRight in ${Date.now() - t0}ms -> New ID: ${newId}`);
    } else {
      console.log(`Step ${i} did not change URL on ArrowRight alone, attempting next button click...`);
      const nextBtn = page.locator('.SxgK2b.Cwtbxf, [aria-label*="next" i], [aria-label*="Következő" i]').first();
      if (await nextBtn.count() > 0) {
        await nextBtn.click({ force: true, timeout: 1500 });
        await sleep(500);
        console.log(`After click, URL is: ${page.url()} (ID: ${idFromUrl(page.url())})`);
      }
    }
  }

  await context.close();
  console.log('\nTest completed.');
})();
