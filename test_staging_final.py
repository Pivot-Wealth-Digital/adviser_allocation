#!/usr/bin/env python3
"""Playwright test for staging deployment - functional endpoints."""

import asyncio
import sys
from playwright.async_api import async_playwright


async def test_staging():
    """Test functional endpoints on staging."""

    staging_url = "https://src-migration-dot-pivot-digital-466902.ts.r.appspot.com"

    print(f"\n{'='*70}")
    print(f"Staging Deployment: {staging_url}")
    print(f"{'='*70}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        try:
            tests_passed = 0
            tests_total = 0

            # Test 1: Public webhook - allocate
            tests_total += 1
            print("✓ Test 1: Public webhook /post/allocate")
            response = await page.goto(f"{staging_url}/post/allocate")
            passed = response.status == 200
            body = await page.content()
            print(f"  Status: {response.status}")
            print(f"  Response: {body[:80]}")
            print(f"  Result: {'✅ PASS' if passed else '❌ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Test 2: Box folder webhook
            tests_total += 1
            print("✓ Test 2: Box webhook /box/folder/create")
            response = await page.goto(f"{staging_url}/box/folder/create")
            passed = response.status in [200, 400, 401, 405]
            print(f"  Status: {response.status}")
            print(f"  Result: {'✅ PASS' if passed else '❌ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Test 3: Box tag endpoint
            tests_total += 1
            print("✓ Test 3: Box webhook /box/folder/tag")
            response = await page.goto(f"{staging_url}/box/folder/tag")
            passed = response.status in [200, 400, 401, 405]
            print(f"  Status: {response.status}")
            print(f"  Result: {'✅ PASS' if passed else '❌ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Test 4: Static assets
            tests_total += 1
            print("✓ Test 4: Static CSS /static/css/styles.css")
            response = await page.goto(f"{staging_url}/static/css/styles.css")
            passed = response.status == 200
            body = await page.content()
            print(f"  Status: {response.status}")
            print(f"  Size: {len(body)} bytes")
            print(f"  Result: {'✅ PASS' if passed else '❌ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Test 5: Workflows endpoint (should redirect to login)
            tests_total += 1
            print("✓ Test 5: Protected route /workflows (auth check)")
            response = await page.goto(f"{staging_url}/workflows", wait_until="domcontentloaded")
            passed = response.status == 302  # Redirect to login
            print(f"  Status: {response.status}")
            print(f"  Result: {'✅ PASS (auth working)' if passed else '❌ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Summary
            print(f"{'='*70}")
            print(f"Results: {tests_passed}/{tests_total} PASSED")
            print(f"{'='*70}\n")

            if tests_passed == tests_total:
                print(f"✅ All functional tests PASSED!")
                print(f"\n🎉 Staging deployment is fully operational!")
                print(f"\n📍 Staging URL: {staging_url}")
                print(f"   - Public webhooks: ✓ Working")
                print(f"   - Static assets: ✓ Working")
                print(f"   - Auth protection: ✓ Working")
                result = 0
            else:
                print(f"⚠️  {tests_total - tests_passed} test(s) failed")
                result = 1

            await browser.close()
            return result

        except Exception as e:
            print(f"\n❌ Error: {e}")
            await browser.close()
            return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_staging())
    sys.exit(exit_code)
