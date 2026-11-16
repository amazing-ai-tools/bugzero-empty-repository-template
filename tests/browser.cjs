// Development-only browser checks against the compiled Rust/WebAssembly bundle.
// Install Playwright externally; see README. No JavaScript application runtime is added.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');

const dist = path.resolve(__dirname, '../rust/frontend/dist');
const copy = {
  en: ['Your app', 'Language', 'Built with Rust', 'A fresh start for your next idea.', 'Connecting to your app…', 'Your app is ready.', 'Your app is starting. Please refresh in a moment.', 'Revision'],
  fr: ['Votre application', 'Langue', 'Créé avec Rust', 'Un nouveau départ pour votre prochaine idée.', 'Connexion à votre application…', 'Votre application est prête.', 'Votre application démarre. Veuillez actualiser la page dans un instant.', 'Révision'],
  pt: ['Seu aplicativo', 'Idioma', 'Feito com Rust', 'Um novo começo para sua próxima ideia.', 'Conectando ao seu aplicativo…', 'Seu aplicativo está pronto.', 'Seu aplicativo está iniciando. Atualize a página em instantes.', 'Revisão'],
};
const revision = '1234567890abcdef1234567890abcdef12345678';
const server = http.createServer(async (req, res) => {
  try {
    const relative = new URL(req.url, 'http://test.invalid').pathname;
    const file = path.resolve(dist, relative === '/' ? 'index.html' : '.' + relative);
    if (!file.startsWith(dist + path.sep)) throw Error('Invalid path');
    const body = await fs.readFile(file);
    const types = { '.wasm': 'application/wasm', '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json' };
    res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch {
    res.writeHead(404).end();
  }
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  let scenarios = 0;
  try {
    // Exercise the actual shipped config, not only runtime config fixtures.
    const bundled = await browser.newContext({ locale: 'fr-FR' });
    const bundledPage = await bundled.newPage();
    bundledPage.on('pageerror', error => errors.push(error.message));
    await bundledPage.route('**/api/health/db', route => route.fulfill({ json: { status: 'healthy', revision } }));
    await bundledPage.goto(base);
    await bundledPage.getByText(copy.fr[5], { exact: true }).waitFor();
    assert.equal(await bundledPage.locator('h1').innerText(), copy.fr[0]);
    assert.equal(await bundledPage.title(), copy.fr[0]);
    await bundled.close();
    scenarios++;
    for (const initial of ['en', 'fr', 'pt']) {
      const context = await browser.newContext({ locale: initial === 'pt' ? 'pt-BR' : initial + '-CA' });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      await page.route('**/app-config.json', route => route.fulfill({ json: { display_name: '' } }));
      await page.route('**/api/health/db', async route => {
        await gate;
        await route.fulfill({ json: { status: 'healthy', revision } });
      });
      await page.goto(base);
      await page.getByText(copy[initial][4], { exact: true }).waitFor();
      assert.equal(await page.locator('html').getAttribute('lang'), initial);
      assert.equal(await page.title(), copy[initial][0]);
      assert.equal(await page.locator('header select').inputValue(), initial);
      assert.equal(await page.getByLabel(copy[initial][1]).count(), 1);
      // Switch while the API request is still pending; late responses must stay translated.
      const selected = initial === 'en' ? 'fr' : initial === 'fr' ? 'pt' : 'en';
      await page.locator('header select').selectOption(selected);
      await page.getByText(copy[selected][4], { exact: true }).waitFor();
      release();
      await page.getByText(copy[selected][5], { exact: true }).waitFor();
      assert.equal(await page.locator('h1').innerText(), copy[selected][0]);
      assert.equal(await page.title(), copy[selected][0]);
      assert.equal(await page.locator('.eyebrow').textContent(), copy[selected][2]);
      assert.equal(await page.locator('.intro').innerText(), copy[selected][3]);
      assert.equal(await page.locator('.revision').innerText(), `${copy[selected][7]} ${revision}`);
      assert.equal(await page.evaluate(() => localStorage.getItem('app-language')), selected);
      assert.equal(await page.locator('.indicator.ready').count(), 1);
      await page.reload();
      await page.getByText(copy[selected][5], { exact: true }).waitFor();
      assert.equal(await page.locator('header select').inputValue(), selected);
      assert.equal(await page.locator('html').getAttribute('lang'), selected);
      // Native select remains keyboard operable in every language.
      await page.locator('header select').focus();
      await page.keyboard.press('Home');
      await page.keyboard.press('Enter');
      await page.getByText(copy.en[5], { exact: true }).waitFor();
      for (const locale of ['en', 'fr', 'pt']) {
        await page.setViewportSize({ width: 320, height: 640 });
        await page.locator('header select').selectOption(locale);
        await page.getByText(copy[locale][5], { exact: true }).waitFor();
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'UI overflows at 320px');
        const header = await page.locator('header').boundingBox();
        const main = await page.locator('main').boundingBox();
        assert(header.y + header.height <= main.y, 'Selector is not above the main interface');
      }
      await context.close();
      scenarios++;
    }
    for (const health of [{ status: 503, json: {} }, { status: 200, json: { status: 'starting', revision } }, { status: 200, body: 'invalid JSON' }]) {
      const context = await browser.newContext();
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/app-config.json', route => route.fulfill({ json: { display_name: 'My App' } }));
      await page.route('**/api/health/db', route => route.fulfill(health));
      await page.goto(base);
      for (const locale of ['en', 'fr', 'pt']) {
        await page.locator('header select').selectOption(locale);
        await page.getByText(copy[locale][6], { exact: true }).waitFor();
        assert.equal(await page.locator('h1').innerText(), 'My App');
        assert.equal(await page.title(), 'My App');
        assert.equal(await page.locator('.indicator.ready').count(), 0);
      }
      await context.close();
      scenarios++;
    }
    for (const [saved, languages, blocked, expected] of [
      ['fr', ['pt-BR'], false, 'fr'], ['invalid', ['de-DE', 'pt-BR'], false, 'pt'],
      [null, ['de-DE'], false, 'en'], [null, ['fr-FR'], true, 'fr'],
    ]) {
      const context = await browser.newContext();
      await context.addInitScript(({ saved, languages, blocked }) => {
        Object.defineProperty(navigator, 'languages', { get: () => languages });
        if (saved) localStorage.setItem('app-language', saved);
        if (blocked) Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('Denied', 'SecurityError'); } });
      }, { saved, languages, blocked });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/app-config.json', route => route.fulfill({ status: 503, json: {} }));
      await page.route('**/api/health/db', route => route.fulfill({ json: { status: 'healthy', revision } }));
      await page.goto(base);
      await page.getByText(copy[expected][5], { exact: true }).waitFor();
      assert.equal(await page.locator('header select').inputValue(), expected);
      await page.locator('header select').selectOption('pt');
      await page.getByText(copy.pt[5], { exact: true }).waitFor();
      await context.close();
      scenarios++;
    }
    const noJs = await browser.newContext({ javaScriptEnabled: false });
    const page = await noJs.newPage();
    await page.goto(base);
    for (const lang of ['en', 'fr', 'pt']) assert(await page.locator(`noscript p[lang=${lang}]`).isVisible());
    await noJs.close();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ scenarios: scenarios + 1, locales: ['en', 'fr', 'pt'], viewport: 320, pageErrors: errors.length, realWebAssembly: true }));
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
