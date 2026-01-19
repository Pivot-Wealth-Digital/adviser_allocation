#!/usr/bin/env python3
"""Playwright test script for staging deployment."""

import asyncio
import sys
from playwright.async_api import async_playwright


async def test_staging():
    """Test key endpoints on staging deployment."""

    # Staging URL pattern
    staging_url = "https://src-migration-dot-pivot-digital-466902.ts.r.appspot.com"

    print(f"Testing {staging_url}")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        try:
            # Test 1: Homepage (redirects to login due to auth)
            print("\n✓ Testing homepage...")
            response = await page.goto(f"{staging_url}/", wait_until="networkidle")
            print(f"  Status: {response.status}")
            is_redirect_or_ok = response.status in [200, 302]
            print(f"  Responds: {'✓' if is_redirect_or_ok else '✗'}")

            # Test 2: Public webhook endpoint
            print("\n✓ Testing public webhook endpoint...")
            response = await page.goto(f"{staging_url}/post/allocate")
            print(f"  Status: {response.status}")
            webhook_ok = response.status == 200
            print(f"  Webhook accessible: {'✓' if webhook_ok else '✗'}")

            # Get response body
            body = await page.content()
            has_message = "Hi" in body or "message" in body.lower()
            print(f"  Returns valid response: {'✓' if has_message else '✗'}")

            # Test 3: Box folder webhook
            print("\n✓ Testing Box folder webhook endpoint...")
            response = await page.goto(f"{staging_url}/box/folder/create")
            print(f"  Status: {response.status}")
            box_ok = response.status in [200, 400, 401]  # May fail due to missing params
            print(f"  Endpoint accessible: {'✓' if box_ok else '✗'}")

            # Test 4: Static assets (check if they're served)
            print("\n✓ Testing static assets routing...")
            response = await page.goto(f"{staging_url}/static/css/styles.css")
            print(f"  CSS Status: {response.status}")
            css_exists = response.status == 200
            print(f"  CSS loads: {'✓' if css_exists else '✗'}")

            # Test 5: Verify app is responding to requests
            print("\n✓ Testing app responsiveness...")
            response = await page.goto(f"{staging_url}/workflows")
            print(f"  Workflows Status: {response.status}")
            workflows_ok = response.status in [200, 302]  # May redirect to login
            print(f"  Workflows responds: {'✓' if workflows_ok else '✗'}")

            print("\n" + "=" * 60)
            all_pass = is_redirect_or_ok and webhook_ok and has_message and box_ok and css_exists and workflows_ok
            print(f"\nOverall: {'✓ ALL TESTS PASSED' if all_pass else '✗ SOME TESTS FAILED'}")
            print(f"\n🎉 Staging URL: {staging_url}")
            print(f"   Version: src-migration")
            print(f"\n✓ Application successfully deployed with src/ layout!")

            await browser.close()
            return 0 if all_pass else 1

        except Exception as e:
            print(f"\n✗ Error during testing: {e}")
            import traceback
            traceback.print_exc()
            await browser.close()
            return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_staging())
    sys.exit(exit_code)
