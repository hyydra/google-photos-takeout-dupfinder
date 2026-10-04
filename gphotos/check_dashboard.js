const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  
  try {
    const resp = await page.goto('http://localhost:8765/', { waitUntil: 'networkidle', timeout: 10000 });
    console.log('Dashboard HTTP Status:', resp.status());
    
    // Evaluate metrics
    const metrics = await page.evaluate(() => {
      const cards = Array.from(document.querySelectorAll('.metric-card'));
      return cards.map(c => ({
        label: c.querySelector('.metric-lbl')?.innerText?.trim(),
        value: c.querySelector('.metric-val')?.innerText?.trim()
      }));
    });
    console.log('Metrics on page:', JSON.stringify(metrics, null, 2));

    // Evaluate groups and cards
    const info = await page.evaluate(() => {
      const groups = document.querySelectorAll('.dup-group');
      const allCards = document.querySelectorAll('.card');
      const keepers = document.querySelectorAll('.card.card-keeper');
      const dups = document.querySelectorAll('.card.card-duplicate');
      const blueBorders = Array.from(groups).map(g => window.getComputedStyle(g).borderColor);
      return {
        groupCount: groups.length,
        totalCards: allCards.length,
        keeperCards: keepers.length,
        duplicateCards: dups.length,
        blueBorders
      };
    });
    console.log('Layout & CSS inspection:', JSON.stringify(info, null, 2));

    const screenshotPath = path.join(__dirname, 'dashboard_verified.png');
    await page.screenshot({ path: screenshotPath, fullPage: false });
    console.log('Screenshot saved to:', screenshotPath);
  } catch (e) {
    console.error('Error fetching dashboard:', e.message);
  } finally {
    await browser.close();
  }
})();
