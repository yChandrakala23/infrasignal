// Browser end-to-end test + screenshot generator (Playwright). See docs/TESTING.md.
//   cd backend && AUTO_SEED_SAMPLE=1 ... uvicorn app.main:app --port 8765     (accounts set via ADMIN_/OFFICER_/AUTHORITY_ env vars)
//   BASE=http://localhost:8765 OFFICER_EMAIL=.. OFFICER_PASSWORD=.. AUTHORITY_EMAIL=.. AUTHORITY_PASSWORD=.. node scripts/e2e_screenshots.js
// Needs `npm i playwright` and a Chromium. Map tiles and web fonts are blocked on purpose so runs are repeatable offline.
const path = require('path'), fs = require('fs');
const E = process.env;
const OFF = [E.OFFICER_EMAIL || 'officer@example.test', E.OFFICER_PASSWORD || 'Officer-test-pass-1'];
const AUT = [E.AUTHORITY_EMAIL || 'authority@example.test', E.AUTHORITY_PASSWORD || 'Authority-test-pass-1'];
const ROOT = path.resolve(__dirname, '..', '..');
const { chromium } = require('playwright');
const SHOT = (E.SHOT_DIR || path.join(ROOT, 'screenshots')) + '/'; fs.mkdirSync(SHOT, { recursive: true });
const base = E.BASE || 'http://localhost:8765';
const problems = [];
const PHOTO = path.join(require('os').tmpdir(), 'infrasignal_e2e_photo.jpg');
// a tiny valid JPEG (brown square) so the upload path is exercised for real
fs.writeFileSync(PHOTO, Buffer.from('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAAwAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDBooorhOwKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD//2Q==', 'base64'));
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  await ctx.route(/fonts\.(googleapis|gstatic)\.com|tile\.openstreetmap\.org/, r => r.abort());
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error' && !/net::ERR_FAILED|Failed to load resource/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', e => problems.push('pageerror: ' + e.message));
  page.on('response', r => { if (r.status() >= 500) problems.push('HTTP ' + r.status() + ' ' + r.url()); });
  const step = async (name, fn) => { try { await fn(); console.log('ok   ', name); } catch (e) { problems.push(name + ': ' + e.message.split('\n')[0]); console.log('FAIL ', name, e.message.split('\n')[0]); } };

  // ---------- citizen
  await step('citizen map loads', async () => {
    await page.goto(base + '/citizen'); await page.waitForSelector('#pub .leaflet-interactive', { timeout: 15000 });
    await page.waitForTimeout(600); await page.screenshot({ path: SHOT + '01_citizen_map.png' });
  });
  await step('citizen road search + profile', async () => {
    await page.fill('#rq', 'Hosur'); await page.waitForSelector('.roadpick'); await page.click('.roadpick');
    await page.waitForSelector('#rprofile h2'); await page.waitForTimeout(500);
    const t = await page.textContent('#rprofile'); if (!/Government records/.test(t)) throw new Error('no govt records block');
    await page.screenshot({ path: SHOT + '02_citizen_road_profile.png' });
  });
  await step('citizen submit report', async () => {
    await page.click('button[data-t=report]'); await page.waitForSelector('#pick .leaflet-tile-pane', { state: 'attached' });
    await page.waitForTimeout(400);
    const box = await page.locator('#pick').boundingBox();
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForSelector('#locbox b', { timeout: 8000 });
    await page.selectOption('#cat', 'pothole'); await page.fill('#desc', 'Deep pothole near the bus stop, two-wheelers swerving');
    await page.setInputFiles('#ph', PHOTO);
    await page.click('#submitBtn'); await page.waitForSelector('.receipt, #ferr:not([hidden])', { timeout: 10000 });
    const rec = await page.locator('.receipt').count(); const err = await page.textContent('#ferr');
    await page.screenshot({ path: SHOT + '03_citizen_report_form.png' });
    if (!rec) throw new Error('no receipt; form error: ' + err);
  });
  await step('citizen submit without location is rejected', async () => {
    await page.reload(); await page.click('button[data-t=report]'); await page.click('#submitBtn');
    await page.waitForSelector('#ferr:not([hidden])'); 
  });
  await step('citizen track', async () => {
    await page.click('button[data-t=track]'); await page.click('#tf button');
    await page.waitForSelector('#tr .card, #tr .notice'); const t = await page.textContent('#tr'); if (/Status/.test(t) === false && /No report/.test(t)) throw new Error('track failed');
    await page.screenshot({ path: SHOT + '04_citizen_track.png' });
  });
  // ---------- gov
  const gov = await ctx.newPage(); gov.on('pageerror', e => problems.push('gov pageerror: ' + e.message));
  gov.on('response', r => { if (r.status() >= 500) problems.push('HTTP ' + r.status() + ' ' + r.url()); });
  await step('gov login rejects wrong role', async () => {
    await gov.goto(base + '/gov'); await gov.fill('#em', AUT[0]); await gov.fill('#pw', AUT[1]); await gov.click('#lf button');
    await gov.waitForSelector('#lerr:not([hidden])');
  });
  await step('gov login + overview', async () => {
    await gov.fill('#em', OFF[0]); await gov.fill('#pw', OFF[1]); await gov.click('#lf button');
    await gov.waitForSelector('.stat b', { timeout: 15000 }); await gov.waitForSelector('#ch-ward'); await gov.waitForTimeout(1200);
    await gov.screenshot({ path: SHOT + '05_gov_overview.png', fullPage: true });
  });
  await step('gov filter by ward changes results', async () => {
    const before = await gov.textContent('.stat b');
    const val = await gov.$eval('#f-ward', s => [...s.options].find(o => o.value)?.value);
    await gov.selectOption('#f-ward', val); await gov.waitForTimeout(900);
    const after = await gov.textContent('.stat b'); if (before === after) throw new Error('filter did not change totals ' + before);
  });
  await step('gov works list + profile', async () => {
    await gov.click('button[data-v=works]'); await gov.waitForSelector('#list tr.click', { timeout: 8000 });
    await gov.screenshot({ path: SHOT + '06_gov_works.png' });
    await gov.click('#list tr.click >> nth=0'); await gov.waitForSelector('#drawer .stages'); await gov.waitForTimeout(400);
    await gov.screenshot({ path: SHOT + '07_gov_work_profile.png' });
  });
  await step('gov documents list + extractions', async () => {
    await gov.keyboard.press('Escape'); await gov.click('button[data-v=documents]'); await gov.waitForSelector('#dl tr.click');
    await gov.screenshot({ path: SHOT + '08_gov_documents.png' });
    await gov.click('#dl tr.click >> nth=0'); await gov.waitForSelector('#drawer table'); await gov.waitForTimeout(1200);
    await gov.screenshot({ path: SHOT + '09_gov_document_extractions.png' });
  });
  await step('gov matching review: verify a suggestion', async () => {
    await gov.keyboard.press('Escape'); await gov.click('button[data-v=matching]'); await gov.waitForSelector('[data-lk]', { timeout: 8000 });
    await gov.screenshot({ path: SHOT + '10_gov_matching_review.png', fullPage: true });
    await gov.click('[data-lk][data-s=verified] >> nth=0'); await gov.waitForSelector('.toast'); 
  });
  await step('gov import preview', async () => {
    await gov.click('button[data-v=imports]'); await gov.setInputFiles('#if', path.join(ROOT, 'backend', 'data', 'sample', 'sample_tenders_kppp.csv'));
    await gov.selectOption('#ik', 'tenders'); await gov.click('#ipv'); await gov.waitForSelector('[data-map]', { timeout: 8000 });
    await gov.screenshot({ path: SHOT + '11_gov_import_preview.png' });
  });
  await step('gov roads + contractors', async () => {
    await gov.click('button[data-v=roads]'); await gov.waitForSelector('#rl table'); await gov.click('button[data-v=contractors]'); await gov.waitForSelector('table');
  });
  await step('gov evaluation', async () => {
    await gov.click('button[data-v=evaluation]'); await gov.waitForSelector('.stat b', { timeout: 60000 }); await gov.waitForTimeout(300);
    await gov.screenshot({ path: SHOT + '12_gov_evaluation.png', fullPage: true });
  });
  // ---------- authority
  const au = await ctx.newPage(); au.on('pageerror', e => problems.push('authority pageerror: ' + e.message));
  au.on('response', r => { if (r.status() >= 500) problems.push('HTTP ' + r.status() + ' ' + r.url()); });
  await step('authority login + overview', async () => {
    await au.goto(base + '/authority'); await au.fill('#em', AUT[0]); await au.fill('#pw', AUT[1]); await au.click('#lf button');
    await au.waitForSelector('#c-day', { timeout: 15000 }); await au.waitForTimeout(1200);
    await au.screenshot({ path: SHOT + '13_authority_overview.png', fullPage: true });
  });
  await step('authority clusters + detail + status update', async () => {
    await au.click('button[data-v=clusters]'); await au.waitForSelector('#cl tr.click'); await au.screenshot({ path: SHOT + '14_authority_clusters.png' });
    await au.click('#cl tr.click >> nth=0'); await au.waitForSelector('#drawer .card'); await au.waitForTimeout(500);
    await au.screenshot({ path: SHOT + '15_authority_cluster_detail.png' });
    await au.selectOption('#ns', 'under_review'); await au.fill('#nn', 'Inspection scheduled'); await au.click('#setst'); await au.waitForSelector('.toast');
  });
  await step('authority alerts + settings', async () => {
    await au.keyboard.press('Escape'); await au.click('button[data-v=alerts]'); await au.waitForSelector('#al .card'); await au.screenshot({ path: SHOT + '16_authority_alerts.png' });
    await au.click('button[data-v=settings]'); await au.waitForSelector('form[data-rule]');
    await au.fill('#multiple_reports-min_reports', '4'); await au.click('form[data-rule=multiple_reports] button'); await au.waitForSelector('.toast');
    await au.screenshot({ path: SHOT + '17_authority_settings.png', fullPage: true });
  });
  await step('authority unmatched + reports', async () => {
    await au.click('button[data-v=unmatched]'); await au.waitForSelector('table'); await au.click('button[data-v=reports]'); await au.waitForSelector('#rl tr.click');
    await au.click('#rl tr.click >> nth=0'); await au.waitForSelector('#drawer');
  });
  // ---------- mobile
  const mob = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true });
  await mob.route(/fonts\.(googleapis|gstatic)\.com|tile\.openstreetmap\.org/, r => r.abort());
  const mp = await mob.newPage();
  await step('mobile citizen', async () => {
    await mp.goto(base + '/citizen'); await mp.waitForSelector('#pub .leaflet-interactive'); await mp.screenshot({ path: SHOT + '18_mobile_citizen_map.png' });
    const sw = await mp.evaluate(() => document.documentElement.scrollWidth); if (sw > 400) throw new Error('horizontal scroll ' + sw);
    await mp.click('button[data-t=report]'); await mp.waitForTimeout(500); await mp.screenshot({ path: SHOT + '19_mobile_citizen_report.png', fullPage: true });
  });
  console.log('\nPROBLEMS:', problems.length ? '\n' + problems.join('\n') : 'none');
  await browser.close();
})();
