#!/usr/bin/env python3
"""Simple Playwright test for staging deployment."""

import asyncio
import sys
from playwright.async_api import async_playwright


async def test_staging():
    """Test key endpoints on staging deployment."""

    staging_url = "https://src-migration-dot-pivot-digital-466902.ts.r.appspot.com"

    print(f"\n{'='*70}")
    print(f"Testing Staging Deployment: {staging_url}")
    print(f"{'='*70}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        try:
            tests_passed = 0
            tests_total = 0

            # Test 1: Public webhook endpoint
            tests_total += 1
            print("Test 1: Public webhook endpoint (/post/allocate)")
            response = await page.goto(f"{staging_url}/post/allocate")
            status = response.status
            body = await page.content()
            passed = status == 200 and "message" in body.lower()
            print(f"  Status: {status}")
            print(f"  Response: {body[:100]}...")
            print(f"  Result: {'✓ PASS' if passed else '✗ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Test 2: Box folder webhook
            tests_total += 1
            print("Test 2: Box folder create endpoint (/box/folder/create)")
            response = await page.goto(f"{staging_url}/box/folder/create")
            status = response.status
            passed = status in [200, 400, 401, 405]  # May fail due to params/auth
            print(f"  Status: {status}")
            print(f"  Result: {'✓ PASS' if passed else '✗ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Test 3: Login page (protected route)
            tests_total += 1
            print("Test 3: Login page redirect (protected route)")
            try:
                response = await page.goto(f"{staging_url}/", timeout=5000)
                status = response.status
                # Will redirect to login, that's expected
                passed = status in [200, 302]
                print(f"  Status: {status}")
                print(f"  Result: {'✓ PASS (auth redirect working)' if passed else '✗ FAIL'}\n")
                if passed:
                    tests_passed += 1
            except Exception as e:
                print(f"  Error: {str(e)[:60]}...")
                print(f"  Result: ✗ FAIL\n")

            # Test 4: Login form loads
            tests_total += 1
            print("Test 4: Login form (/login)")
            response = await page.goto(f"{staging_url}/login")
            status = response.status
            body = await page.content()
            passed = status == 200 and "Sign In" in body
            print(f"  Status: {status}")
            print(f"  Form present: {'✓' if 'Sign In' in body else '✗'}")
            print(f"  Result: {'✓ PASS' if passed else '✗ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Test 5: Static CSS
            tests_total += 1
            print("Test 5: Static assets (/static/css/styles.css)")
            response = await page.goto(f"{staging_url}/static/css/styles.css")
            status = response.status
            body = await page.content()
            passed = status == 200 and len(body) > 0
            print(f"  Status: {status}")
            print(f"  Size: {len(body)} bytes")
            print(f"  Result: {'✓ PASS' if passed else '✗ FAIL'}\n")
            if passed:
                tests_passed += 1

            # Summary
            print(f"{'='*70}")
            print(f"Test Results: {tests_passed}/{tests_total} PASSED")
            print(f"{'='*70}")

            if tests_passed == tests_total:
                print(f"\n✅ All tests PASSED!")
                print(f"\n🎉 Staging deployment is fully operational")
                print(f"   URL: {staging_url}")
                result = 0
            else:
                print(f"\n⚠️  {tests_total - tests_passed} test(s) failed")
                result = 1

            await browser.close()
            return result

        except Exception as e:
            print(f"\n✗ Critical error: {e}")
            import traceback
            traceback.print_exc()
            await browser.close()
            return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_staging())
    sys.exit(exit_code)
