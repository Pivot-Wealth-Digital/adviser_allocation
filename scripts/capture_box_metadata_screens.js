const { chromium } = require('playwright');

(async () => {
  const baseUrl = process.env.BOX_GUIDE_BASE_URL || 'http://127.0.0.1:8080';
  const username = process.env.BOX_GUIDE_USERNAME || 'admin';
  const password = process.env.BOX_GUIDE_PASSWORD || 'passwordpw';

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  async function captureElement(selector, path, padding = 24, fallbackFullPage = true) {
    const element = await page.$(selector);
    if (element) {
      await element.scrollIntoViewIfNeeded().catch(() => {});
      const box = await element.boundingBox();
      if (box) {
        const viewport = page.viewportSize();
        const x = Math.max(box.x - padding, 0);
        const y = Math.max(box.y - padding, 0);
        const width = Math.min(box.width + padding * 2, viewport.width - x);
        const height = Math.min(box.height + padding * 2, viewport.height - y);
        if (width > 0 && height > 0) {
          await page.screenshot({ path, clip: { x, y, width, height } });
          return;
        }
      }
    }
    if (fallbackFullPage) {
      await page.screenshot({ path, fullPage: true });
    } else {
      throw new Error(`Unable to capture screenshot for selector: ${selector}`);
    }
  }

  async function ensureLoggedIn() {
    await page.goto(`${baseUrl}/box/folder/metadata/status`, { waitUntil: 'domcontentloaded' });
    const usernameInput = await page.$('input[name="username"]');
    if (usernameInput) {
      await page.fill('input[name="username"]', username);
      await page.fill('input[name="password"]', password);
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'networkidle' }),
        page.click('button[type="submit"], input[type="submit"]'),
      ]);
    } else {
      await page.waitForLoadState('networkidle');
    }
  }

  try {
    await ensureLoggedIn();
    await page.waitForSelector('.summary-card', { timeout: 10000 });
    await page.waitForTimeout(1000);

    const stepDir = 'static/images/box_metadata';
    const fs = require('fs');
    if (!fs.existsSync(stepDir)) {
      fs.mkdirSync(stepDir, { recursive: true });
    }

    await captureElement('.assignment-card, details.assignment-slot', `${stepDir}/step1.png`, 24, true);

    const folderId = await page.evaluate(() => {
      const button = document.querySelector('.collab-link[data-folder-id]');
      return button ? button.dataset.folderId : null;
    });
    if (!folderId) {
      throw new Error('Unable to locate a folder id for collaborators view.');
    }

    await page.goto(`${baseUrl}/box/collaborators?folder_id=${folderId}`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#folder-id', { timeout: 10000 });
    await page.waitForTimeout(1000);
    await captureElement('.lookup-card', `${stepDir}/step2.png`, 30, true);

    const loadSubfoldersButton = await page.$('#load-subfolders');
    if (loadSubfoldersButton) {
      await loadSubfoldersButton.click();
      await page.waitForTimeout(1200);
      const subfolderInputs = page.locator('#subfolder-list input[type="checkbox"]');
      if ((await subfolderInputs.count()) > 0) {
        await subfolderInputs.first().check();
      }
      await page.waitForTimeout(800);
      await captureElement('#subfolder-wrapper', `${stepDir}/step3.png`, 30, true);
    } else {
      await page.screenshot({ path: `${stepDir}/step3.png`, fullPage: true });
    }

    const showButton = (await page.$('#subfolder-show')) || (await page.$('#root-show'));
    if (showButton) {
      await Promise.all([
        page.waitForResponse((resp) => resp.url().includes('/box/folder/collaborators') && resp.status() === 200),
        showButton.click(),
      ]).catch(() => {});
    }
    await page.waitForSelector('#collab-table-body tr', { timeout: 10000 });

    const hubspotButton = await page.$('.hubspot-btn');
    if (hubspotButton) {
      await Promise.all([
        page.waitForSelector('#contact-card:not(.hidden)', { timeout: 10000 }),
        hubspotButton.click(),
      ]);
      await page.waitForTimeout(800);
    }

    await captureElement('#contact-card', `${stepDir}/step4.png`, 30, true);

    const firstDealCheckbox = page.locator('#deal-list input[data-deal-index]').first();
    if (await firstDealCheckbox.count()) {
      await firstDealCheckbox.click();
      await page.waitForTimeout(500);
      const previewButton = await page.$('#metadata-preview-btn');
      if (previewButton) {
        await previewButton.click();
        await page.waitForSelector('#metadata-preview:not(.hidden)', { timeout: 10000 }).catch(() => {});
      }
    }

    await page.waitForTimeout(800);
    await captureElement('#metadata-preview', `${stepDir}/step5.png`, 30, true);
  } catch (err) {
    console.error('Failed to capture screenshots:', err);
    process.exitCode = 1;
  } finally {
    await page.close();
    await context.close();
    await browser.close();
  }
})();
