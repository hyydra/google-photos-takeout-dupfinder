const p = require('puppeteer');
(async () => {
  const b = await p.connect({ browserURL: 'http://127.0.0.1:9222' });
  const pg = (await b.pages()).find((x) => x.url().includes('photos.google.com/photo/')) || (await b.pages())[0];
  console.log('Before URL:', pg.url());

  // Test 1: Press ArrowRight
  await pg.keyboard.press('ArrowRight');
  await new Promise((r) => setTimeout(r, 1500));
  console.log('After ArrowRight URL:', pg.url());

  // Test 2: If same, click Next button or right side
  if (pg.url().includes('AF1QipMajqfGgHzQ4gNtAXPGW5HBsBWKkvDeI3sa8x8M')) {
    console.log('ArrowRight did not advance URL. Trying Next button or right-side click...');
    const nextBtn = await pg.$('button[aria-label*="Next"], button[aria-label*="next"], button[aria-label*="Következő"], [data-nav="next"]');
    if (nextBtn) {
      console.log('Found nextBtn, clicking...');
      await nextBtn.click();
    } else {
      console.log('Clicking right edge of screen...');
      const { width, height } = await pg.evaluate(() => ({ width: innerWidth, height: innerHeight }));
      await pg.mouse.click(width - 50, height / 2);
    }
    await new Promise((r) => setTimeout(r, 1500));
    console.log('After click URL:', pg.url());
  }

  await b.disconnect();
})().catch(console.error);
