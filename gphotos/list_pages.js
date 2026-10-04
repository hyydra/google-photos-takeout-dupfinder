const p = require('puppeteer');
(async () => {
  const b = await p.connect({ browserURL: 'http://127.0.0.1:9222' });
  const pages = await b.pages();
  console.log('Open pages:', pages.length);
  for (let i = 0; i < pages.length; i++) {
    console.log(`Page [${i}]:`, pages[i].url(), await pages[i].title());
  }
  await b.disconnect();
})().catch(console.error);
