const p = require('puppeteer');
(async () => {
  const b = await p.connect({ browserURL: 'http://127.0.0.1:9222', defaultViewport: null });
  const pages = await b.pages();
  console.log('Pages:', pages.map((pg) => pg.url()));
  const pg = pages.find((x) => x.url().includes('photos.google.com')) || pages[0];
  console.log('Active URL:', pg.url());
  const btns = await pg.evaluate(() => {
    return [...document.querySelectorAll('button, [role="button"]')].map((b) => b.getAttribute('aria-label') || b.innerText).filter(Boolean);
  });
  console.log('Buttons:', btns.slice(0, 15));
  await b.disconnect();
})().catch(console.error);
