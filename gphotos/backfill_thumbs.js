const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const LOG = path.join(ROOT, 'run-log.tsv');
const THUMB_DIR = path.join(ROOT, 'thumbnails');
fs.mkdirSync(THUMB_DIR, { recursive: true });

(async () => {
  const lines = fs.readFileSync(LOG, 'utf8').split(/\r?\n/).filter(Boolean);
  const items = lines.map((l) => l.split('\t')[0]).filter(Boolean);

  const missing = items.filter((id) => !fs.existsSync(path.join(THUMB_DIR, `${id}.jpg`)));
  console.log(`Total items: ${items.length}, Missing thumbnails: ${missing.length}`);

  if (!missing.length) {
    console.log('All thumbnails already present!');
    return;
  }

  const b = await puppeteer.connect({ browserURL: 'http://127.0.0.1:9222', defaultViewport: null });
  const p = (await b.pages()).find((x) => x.url().includes('photos.google.com')) || (await b.newPage());

  for (let i = 0; i < missing.length; i++) {
    const id = missing[i];
    const out = path.join(THUMB_DIR, `${id}.jpg`);
    try {
      await p.goto(`https://photos.google.com/photo/${id}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await new Promise((r) => setTimeout(r, 1000));

      const box = await p.evaluate(() => {
        const imgs = [...document.querySelectorAll('img, video')];
        let best = null, maxArea = 0;
        for (const el of imgs) {
          const r = el.getBoundingClientRect();
          const area = r.width * r.height;
          if (area > maxArea && r.width > 80 && r.height > 80) {
            maxArea = area;
            best = { x: Math.max(0, r.x), y: Math.max(0, r.y), w: Math.max(10, r.width), h: Math.max(10, r.height) };
          }
        }
        return best;
      });

      if (box) {
        await p.screenshot({
          path: out,
          type: 'jpeg',
          quality: 80,
          clip: { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.w), height: Math.round(box.h) },
        });
        console.log(`[${i + 1}/${missing.length}] Saved thumbnail for ${id}`);
      } else {
        await p.screenshot({ path: out, type: 'jpeg', quality: 70 });
        console.log(`[${i + 1}/${missing.length}] Fallback screenshot for ${id}`);
      }
    } catch (e) {
      console.log(`FAIL ${id}: ${e.message}`);
    }
  }

  await b.disconnect();
  console.log('Finished backfill!');
})().catch(console.error);
