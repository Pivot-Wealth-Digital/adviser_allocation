"""Playwright smoke test against live Cloud Run canary.

Authenticates via /login_bypass (DEV_LOGIN_ENABLED must be set),
visits all UI pages, checks for errors, verifies headers, and
takes screenshots.

Usage:
    uv run python tests/test_prod_smoke.py
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

BASE_URL = "https://adviser-allocation-307314618542.australia-southeast1.run.app"
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots" / "prod"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

results: list[dict] = []
console_errors: list[dict] = []


def record(page_name: str, url: str, status: str, issues: list[str]):
    results.append({"page": page_name, "url": url, "status": status, "issues": issues})


def setup_console_listener(page: Page, page_name: str):
    def on_console(msg):
        if msg.type == "error":
            console_errors.append({"page": page_name, "text": msg.text})

    page.on("console", on_console)


def authenticate(context: BrowserContext):
    """Log in via /login_bypass (POST)."""
    page = context.new_page()
    page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=30000)
    page.evaluate(f"""
        fetch('{BASE_URL}/login_bypass', {{method: 'POST', redirect: 'manual', credentials: 'include'}})
            .then(r => window._bypassStatus = r.status)
            .catch(e => window._bypassError = e.message)
    """)
    page.wait_for_timeout(3000)
    page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=30000)
    if "/login" in page.url:
        print("FATAL: Authentication failed - redirected to /login")
        print(f"  URL: {page.url}")
        page.screenshot(path=str(SCREENSHOTS_DIR / "auth_failed.png"))
        sys.exit(1)
    page.close()
    print("Authenticated successfully as admin user\n")


def take_screenshot(page: Page, name: str):
    try:
        page.screenshot(path=str(SCREENSHOTS_DIR / f"{name}.png"), full_page=True, timeout=15000)
    except Exception:
        try:
            page.screenshot(
                path=str(SCREENSHOTS_DIR / f"{name}.png"), full_page=False, timeout=10000
            )
        except Exception:
            print(f"    [warn] Could not capture screenshot for {name}")


def visit_page(page: Page, name: str, path: str) -> int:
    setup_console_listener(page, name)
    try:
        resp = page.goto(f"{BASE_URL}{path}", wait_until="networkidle", timeout=30000)
        status_code = resp.status if resp else 0
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        status_code = 200
    take_screenshot(page, name)
    return status_code


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health(page: Page):
    """Check /health returns 200."""
    name = "00_health"
    issues = []
    resp = page.goto(f"{BASE_URL}/health", wait_until="networkidle", timeout=15000)
    status_code = resp.status if resp else 0
    if status_code != 200:
        issues.append(f"Expected 200, got {status_code}")
    record(name, "/health", "PASS" if not issues else "FAIL", issues)


def test_login(page: Page):
    name = "01_login"
    issues = []
    resp = page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=15000)
    take_screenshot(page, name)

    google_btn = page.locator("a[href='/login/google'], button:has-text('Google')")
    if google_btn.count() == 0:
        issues.append("Missing Google sign-in button")

    bypass_btn = page.locator(
        "form[action*='login_bypass'], button:has-text('Bypass'), button:has-text('Dev')"
    )
    if bypass_btn.count() == 0:
        issues.append("Missing dev bypass login button")

    record(name, "/login", "PASS" if not issues else "WARN", issues)


def test_favicon(page: Page):
    """Check favicon loads."""
    name = "01b_favicon"
    issues = []
    resp = page.goto(f"{BASE_URL}/static/images/favicon.svg", timeout=10000)
    status_code = resp.status if resp else 0
    if status_code != 200:
        issues.append(f"Favicon returned {status_code}")
    record(name, "/static/images/favicon.svg", "PASS" if not issues else "FAIL", issues)


def test_csp_header(page: Page):
    """Check Content-Security-Policy header is present."""
    name = "01c_csp"
    issues = []
    resp = page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)
    headers = resp.headers if resp else {}
    if "content-security-policy" not in headers:
        issues.append("Missing Content-Security-Policy header")
    record(name, "/ (headers)", "PASS" if not issues else "WARN", issues)


def test_homepage(page: Page):
    name = "02_homepage"
    issues = []
    visit_page(page, name, "/")
    cards = page.locator(".card")
    if cards.count() < 2:
        issues.append(f"Expected 2+ cards, found {cards.count()}")
    admin_text = page.locator("text=System Configuration")
    if admin_text.count() == 0:
        issues.append("Admin 'System Configuration' card not visible")
    record(name, "/", "PASS" if not issues else "WARN", issues)


def test_earliest_availability(page: Page):
    name = "03_earliest_availability"
    issues = []
    visit_page(page, name, "/availability/earliest")
    for eid in ["agreementStartDate"]:
        if page.locator(f"#{eid}").count() == 0:
            issues.append(f"Missing #{eid}")
    if page.locator("button:has-text('Compute')").count() == 0:
        issues.append("Missing Compute button")
    record(name, "/availability/earliest", "PASS" if not issues else "WARN", issues)


def test_adviser_schedule(page: Page):
    name = "04_adviser_schedule"
    issues = []
    visit_page(page, name, "/availability/schedule")
    if page.locator("#adviserSelect").count() == 0:
        issues.append("Missing #adviserSelect")
    record(name, "/availability/schedule", "PASS" if not issues else "WARN", issues)


def test_allocation_history(page: Page):
    name = "05_allocation_history"
    issues = []
    visit_page(page, name, "/allocations/history")
    for fid in ["daysFilter", "statusFilter", "dealFilter", "adviserFilter"]:
        if page.locator(f"#{fid}").count() == 0:
            issues.append(f"Missing #{fid}")
    record(name, "/allocations/history", "PASS" if not issues else "WARN", issues)


def test_allocation_history_modal(page: Page):
    """Test modal opens and Escape closes it."""
    name = "05b_modal_escape"
    issues = []
    # Click first "View" button if any rows exist
    view_btns = page.locator("button:has-text('View')")
    if view_btns.count() > 0:
        view_btns.first.click()
        page.wait_for_timeout(500)
        modal = page.locator("[role='dialog']")
        if modal.count() == 0:
            issues.append("Modal did not open with role='dialog'")
        else:
            take_screenshot(page, f"{name}_open")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            modal_visible = page.locator(".modal.open, .modal[style*='block']")
            if modal_visible.count() > 0:
                issues.append("Modal did not close on Escape key")
    else:
        issues.append("No allocation rows to test modal (not a code issue)")
    record(name, "/allocations/history (modal)", "PASS" if not issues else "WARN", issues)


def test_workflows(page: Page):
    name = "06_workflows"
    issues = []
    visit_page(page, name, "/workflows")
    mermaid_divs = page.locator(".mermaid")
    if mermaid_divs.count() == 0:
        issues.append("No .mermaid containers")
    else:
        page.wait_for_timeout(2000)
        if page.locator(".mermaid svg").count() == 0:
            issues.append("Mermaid diagrams did not render")
    record(name, "/workflows", "PASS" if not issues else "WARN", issues)


def test_availability_matrix(page: Page):
    name = "08_availability_matrix"
    issues = []
    visit_page(page, name, "/availability/matrix")
    if page.locator("table").count() == 0:
        issues.append("No table found")
    record(name, "/availability/matrix", "PASS" if not issues else "WARN", issues)


def test_clarify_chart(page: Page):
    name = "09_clarify_chart"
    issues = []
    visit_page(page, name, "/availability/clarify-chart")
    if page.locator("#clarifyChart").count() == 0:
        issues.append("Missing #clarifyChart canvas")
    page.wait_for_timeout(2000)
    record(name, "/availability/clarify-chart", "PASS" if not issues else "WARN", issues)


def test_meetings(page: Page):
    name = "10_meetings"
    issues = []
    visit_page(page, name, "/availability/meetings")
    if page.locator("#adviserSelect").count() == 0:
        issues.append("Missing #adviserSelect")
    record(name, "/availability/meetings", "PASS" if not issues else "WARN", issues)


def test_closures_ui(page: Page):
    name = "11_closures"
    issues = []
    status_code = visit_page(page, name, "/closures/ui")
    if status_code == 403:
        issues.append("Got 403 - admin_required blocked access")
        record(name, "/closures/ui", "FAIL", issues)
        return
    for eid in ["start", "end", "tagSelect", "description", "submit"]:
        if page.locator(f"#{eid}").count() == 0:
            issues.append(f"Missing #{eid}")
    record(name, "/closures/ui", "PASS" if not issues else "WARN", issues)


def test_capacity_overrides_ui(page: Page):
    name = "12_capacity_overrides"
    issues = []
    status_code = visit_page(page, name, "/capacity_overrides/ui")
    if status_code == 403:
        issues.append("Got 403 - admin_required blocked access")
        record(name, "/capacity_overrides/ui", "FAIL", issues)
        return
    for eid in ["emailSelect", "effective", "limit", "podType", "notes", "submit"]:
        if page.locator(f"#{eid}").count() == 0:
            issues.append(f"Missing #{eid}")
    record(name, "/capacity_overrides/ui", "PASS" if not issues else "WARN", issues)


def test_employees_ui(page: Page):
    name = "13_employees"
    issues = []
    status_code = visit_page(page, name, "/employees/ui")
    if status_code == 403:
        issues.append("Got 403 - admin_required blocked access")
        record(name, "/employees/ui", "FAIL", issues)
        return
    if page.locator("table").count() == 0:
        issues.append("No employees table")
    record(name, "/employees/ui", "PASS" if not issues else "WARN", issues)


def test_leave_requests_ui(page: Page):
    name = "14_leave_requests"
    issues = []
    status_code = visit_page(page, name, "/leave_requests/ui")
    if status_code == 403:
        issues.append("Got 403 - admin_required blocked access")
        record(name, "/leave_requests/ui", "FAIL", issues)
        return
    for eid in ["employeeFilter", "statusFilter"]:
        if page.locator(f"#{eid}").count() == 0:
            issues.append(f"Missing #{eid}")
    record(name, "/leave_requests/ui", "PASS" if not issues else "WARN", issues)


def test_404_page(page: Page):
    """Check /nonexistent returns 404 with styled error page."""
    name = "15_404"
    issues = []
    resp = page.goto(f"{BASE_URL}/nonexistent-page-test", wait_until="networkidle", timeout=15000)
    status_code = resp.status if resp else 0
    take_screenshot(page, name)
    if status_code != 404:
        issues.append(f"Expected 404, got {status_code}")
    # Check for styled error page (not raw text)
    error_heading = page.locator("h1, h2, .error")
    if error_heading.count() == 0:
        issues.append("404 page has no styled error heading")
    record(name, "/nonexistent-page-test", "PASS" if not issues else "WARN", issues)


def test_sidebar(page: Page):
    name = "16_sidebar"
    issues = []
    page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)

    toggle = page.locator("#sidebarToggle")
    if not toggle.is_visible():
        issues.append("Hamburger toggle not visible")
    else:
        toggle.click()
        page.wait_for_timeout(500)
        has_open = page.evaluate("document.getElementById('sidebar')?.classList.contains('open')")
        if not has_open:
            issues.append("Sidebar did not open after click")
        take_screenshot(page, f"{name}_open")

        # Check key links
        sidebar = page.locator("#sidebar")
        for link_text in ["Home", "Earliest Availability", "Allocation History"]:
            if sidebar.locator(f"a:has-text('{link_text}')").count() == 0:
                issues.append(f"Missing sidebar link: {link_text}")

    record(name, "sidebar", "PASS" if not issues else "WARN", issues)


# ---------------------------------------------------------------------------
# Report & Main
# ---------------------------------------------------------------------------


def print_report():
    print("\n" + "=" * 72)
    print("  PRODUCTION SMOKE TEST REPORT")
    print("=" * 72)

    pass_count = sum(1 for r in results if r["status"] == "PASS")
    warn_count = sum(1 for r in results if r["status"] == "WARN")
    fail_count = sum(1 for r in results if r["status"] == "FAIL")

    for r in results:
        icon = {"PASS": "OK", "WARN": "!!", "FAIL": "XX"}[r["status"]]
        print(f"\n  [{icon}] {r['page']}  ({r['url']})")
        if r["issues"]:
            for issue in r["issues"]:
                print(f"       - {issue}")

    if console_errors:
        print(f"\n  JS Console Errors ({len(console_errors)}):")
        for err in console_errors:
            print(f"    [{err['page']}] {err['text'][:120]}")

    print(f"\n  Summary: {pass_count} PASS / {warn_count} WARN / {fail_count} FAIL")
    print(f"  Screenshots: {SCREENSHOTS_DIR}")
    print("=" * 72)

    if fail_count > 0:
        sys.exit(1)


def main():
    print(f"Target: {BASE_URL}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # --- Unauthenticated tests ---
        print("Running unauthenticated tests...")
        unauth_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        unauth_page = unauth_ctx.new_page()

        test_health(unauth_page)
        print("  [done] Health check")
        test_login(unauth_page)
        print("  [done] Login page")
        test_favicon(unauth_page)
        print("  [done] Favicon")

        unauth_page.close()
        unauth_ctx.close()

        # --- Authenticated tests ---
        print("\nAuthenticating via /login_bypass...")
        auth_ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        authenticate(auth_ctx)

        page = auth_ctx.new_page()

        test_csp_header(page)
        print("  [done] CSP header")

        tests = [
            ("Homepage", test_homepage),
            ("Earliest Availability", test_earliest_availability),
            ("Adviser Schedule", test_adviser_schedule),
            ("Allocation History", test_allocation_history),
            ("Allocation History Modal", test_allocation_history_modal),
            ("Workflows", test_workflows),
            ("Availability Matrix", test_availability_matrix),
            ("Clarify Chart", test_clarify_chart),
            ("Meetings", test_meetings),
            ("Closures UI", test_closures_ui),
            ("Capacity Overrides UI", test_capacity_overrides_ui),
            ("Employees UI", test_employees_ui),
            ("Leave Requests UI", test_leave_requests_ui),
            ("404 Page", test_404_page),
            ("Sidebar", test_sidebar),
        ]

        for label, test_fn in tests:
            try:
                test_fn(page)
                print(f"  [done] {label}")
            except Exception as exc:
                print(f"  [CRASH] {label}: {exc}")
                record(label, "", "FAIL", [f"Crashed: {exc}"])

        page.close()
        auth_ctx.close()
        browser.close()

    print_report()


if __name__ == "__main__":
    main()
