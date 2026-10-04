const p = require('puppeteer');
(async () => {
  const b = await p.connect({ browserURL: 'http://127.0.0.1:9222' });
  const pg = (await b.pages()).find((x) => x.url().includes('photos.google.com/photo/')) || (await b.pages())[0];
  console.log('URL:', pg.url());
  const info = await pg.evaluate(() => {
    const all = [...document.querySelectorAll('img, video, canvas, div[style*="background-image"]')];
    return all.map((e) => ({
      tag: e.tagName,
      src: e.src || e.getAttribute('src') || '',
      bg: e.style.backgroundImage || '',
      w: e.offsetWidth,
      h: e.offsetHeight,
      role: e.getAttribute('role') || '',
    }));
  });
  console.log('Media elements count:', info.length);
  console.log('Elements:', JSON.stringify(info.slice(0, 10), null, 2));
  await b.disconnect();
})().catch(console.error);
