const { chromium } = require('playwright');

(async () => {
  console.log('Connecting Playwright to Chrome on 9222...');
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
  const context = browser.contexts()[0];
  const pages = context.pages();
  console.log(`Found ${pages.length} open pages:`);
  for (let i = 0; i < pages.length; i++) {
    console.log(`  [${i}] ${pages[i].url()} | Title: ${await pages[i].title()}`);
  }

  const page = pages.find((p) => p.url().includes('photos.google.com')) || pages[0];
  console.log(`Active page: ${page.url()}`);

  // Test navigation
  console.log('Testing ArrowRight navigation with Playwright...');
  const beforeUrl = page.url();
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(2000);
  const afterUrl = page.url();
  console.log(`Before URL: ${beforeUrl}`);
  console.log(`After URL:  ${afterUrl}`);

  if (beforeUrl === afterUrl) {
    console.log('ArrowRight did not change URL. Searching for on-screen next button...');
    const nextBtn = page.locator('[aria-label*="Next photo" i], [aria-label*="View next photo" i], [aria-label*="Következő" i], [data-nav="next"]').first();
    const count = await nextBtn.count();
    console.log(`Next button count: ${count}`);
    if (count > 0) {
      await nextBtn.click();
      await page.waitForTimeout(2000);
      console.log(`After next button click URL: ${page.url()}`);
    } else {
      console.log('Clicking right side of screen with Playwright...');
      const vp = page.viewportSize() || { width: 1280, height: 800 };
      await page.mouse.click(vp.width - 30, vp.height / 2);
      await page.waitForTimeout(2000);
      console.log(`After right click URL: ${page.url()}`);
    }
  }

  await browser.close();
  console.log('Done Playwright check.');
})().catch(console.error);
