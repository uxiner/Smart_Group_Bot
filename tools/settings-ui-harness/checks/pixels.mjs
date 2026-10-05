import { chromium } from 'playwright';
import fs from 'node:fs';
const files = process.argv.slice(2);
const b = await chromium.launch({ channel: 'chrome', headless: true });
const p = await b.newPage();
for (const f of files) {
  const data = fs.readFileSync(f).toString('base64');
  const stats = await p.evaluate(async (b64) => {
    const img = new Image();
    img.src = 'data:image/png;base64,' + b64;
    await img.decode();
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const { data: px } = ctx.getImageData(0, 0, c.width, c.height);
    let dark = 0, light = 0, cyan = 0, violet = 0, total = 0;
    const sample = [];
    for (let i = 0; i < px.length; i += 16) {
      const r = px[i], g = px[i + 1], bl = px[i + 2];
      const lum = 0.2126 * r + 0.7152 * g + 0.0722 * bl;
      if (lum < 70) dark += 1; else if (lum > 150) light += 1;
      // ice cyan #71D9EF
      if (Math.abs(r - 113) < 34 && Math.abs(g - 217) < 34 && Math.abs(bl - 239) < 34) cyan += 1;
      // lavender #B8A4F4
      if (Math.abs(r - 184) < 34 && Math.abs(g - 164) < 34 && Math.abs(bl - 244) < 34) violet += 1;
      if (total < 6 && i % 1600 === 0) sample.push([r, g, bl]);
      total += 1;
    }
    return { w: img.width, h: img.height, darkPct: +(100 * dark / total).toFixed(1), lightPct: +(100 * light / total).toFixed(1), cyanPct: +(100 * cyan / total).toFixed(2), violetPct: +(100 * violet / total).toFixed(2), sample };
  }, data);
  console.log(f.split('/').pop(), JSON.stringify(stats));
}
await b.close();
