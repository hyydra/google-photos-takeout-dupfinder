const p = require('puppeteer');
(async () => {
  const b = await p.connect({ browserURL: 'http://127.0.0.1:9222' });
  const pages = await b.pages();
  while (pages.length > 1) {
    await pages.pop().close();
  }
  const pg = pages[0];
  console.log('Page URL:', pg.url());

  // Wait for photo links to appear
  await pg.waitForSelector('a[href*="/photo/"]', { timeout: 15000 }).catch(() => console.log('Selector timeout'));

  const links = await pg.evaluate(() => {
    return [...document.querySelectorAll('a[href*="/photo/"]')].map((a) => a.href);
  });
  console.log(`Found ${links.length} photo links!`);
  if (links.length) {
    console.log('Sample links:', links.slice(0, 5));
    // Click the first photo to enter photo viewer
    await pg.click('a[href*="/photo/"]');
    await new Promise((r) => setTimeout(r, 2000));
    console.log('After clicking first photo URL:', pg.url());
  }
  await b.disconnect();
})().catch(console.error);
