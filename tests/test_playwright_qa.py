"""Playwright QA & UI/UX review of all adviser_allocation pages.

Authenticates via /login_bypass (debug mode), visits every UI page,
takes screenshots, checks for errors, and tests key interactions.

Usage:
    CLOUD_SQL_USE_PROXY=true uv run python tests/test_playwright_qa.py
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

BASE_URL = "http://localhost:8080"
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# Collect results
results: list[dict] = []
console_errors: list[dict] = []


def record(page_name: str, url: str, status: str, issues: list[str]):
    results.append({"page": page_name, "url": url, "status": status, "issues": issues})


def setup_console_listener(page: Page, page_name: str):
    """Capture JS console errors."""

    def on_console(msg):
        if msg.type == "error":
            console_errors.append({"page": page_name, "text": msg.text})

    page.on("console", on_console)


def authenticate(context: BrowserContext):
    """Log in via /login_bypass (POST)."""
    page = context.new_page()
    # POST via page JS to set session cookie, don't follow redirect
    page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=15000)
    page.evaluate("""
        fetch('/login_bypass', {method: 'POST', redirect: 'manual'})
            .then(r => window._bypassStatus = r.status)
            .catch(e => window._bypassError = e.message)
    """)
    page.wait_for_timeout(2000)
    # Navigate to home to confirm session is set
    page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=30000)
    # Verify we're logged in (not redirected to /login)
    if "/login" in page.url:
        print("FATAL: Authentication failed — redirected to /login")
        sys.exit(1)
    page.close()
    print("Authenticated successfully as admin user\n")


def take_screenshot(page: Page, name: str):
    try:
        page.screenshot(path=str(SCREENSHOTS_DIR / f"{name}.png"), full_page=True, timeout=15000)
    except Exception:
        # Fallback: viewport-only screenshot
        try:
            page.screenshot(
                path=str(SCREENSHOTS_DIR / f"{name}.png"), full_page=False, timeout=10000
            )
        except Exception:
            print(f"    [warn] Could not capture screenshot for {name}")


def visit_page(page: Page, name: str, path: str, expected_status: int = 200) -> int:
    """Navigate to a page and return the HTTP status."""
    setup_console_listener(page, name)
    try:
        resp = page.goto(f"{BASE_URL}{path}", wait_until="networkidle", timeout=30000)
        status_code = resp.status if resp else 0
    except Exception:
        # Fallback: page loaded but networkidle timed out (slow API calls)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        status_code = 200  # page loaded but slow
    take_screenshot(page, name)
    return status_code


# ---------------------------------------------------------------------------
# Page-specific test functions
# ---------------------------------------------------------------------------


def test_login(page: Page):
    """Test login page (unauthenticated)."""
    name = "01_login"
    issues = []

    # Use a fresh context without auth
    resp = page.goto(f"{BASE_URL}/login", wait_until="networkidle")
    take_screenshot(page, name)

    # Check for Google sign-in button
    google_btn = page.locator("a[href='/login/google'], button:has-text('Google')")
    if google_btn.count() == 0:
        issues.append("Missing Google sign-in button")

    # Check for dev bypass button
    bypass_btn = page.locator(
        "form[action='/login_bypass'], button:has-text('Bypass'), button:has-text('Dev')"
    )
    if bypass_btn.count() == 0:
        issues.append("Missing dev bypass login button")

    status = "PASS" if not issues else "WARN"
    record(name, "/login", status, issues)


def test_homepage(page: Page):
    name = "02_homepage"
    issues = []
    visit_page(page, name, "/")

    # Check cards render
    cards = page.locator(".card")
    card_count = cards.count()
    if card_count < 2:
        issues.append(f"Expected at least 2 cards, found {card_count}")

    # Admin cards should be visible (we're admin)
    admin_text = page.locator("text=System Configuration")
    if admin_text.count() == 0:
        issues.append("Admin 'System Configuration' card not visible (should be visible for admin)")

    employee_text = page.locator("text=Employee Management")
    if employee_text.count() == 0:
        issues.append("Admin 'Employee Management' card not visible (should be visible for admin)")

    # Quick Info card
    info_card = page.locator(".info-card")
    if info_card.count() == 0:
        issues.append("Quick Info card missing")

    status = "PASS" if not issues else "WARN"
    record(name, "/", status, issues)


def test_earliest_availability(page: Page):
    name = "03_earliest_availability"
    issues = []
    visit_page(page, name, "/availability/earliest")

    # Date picker
    date_input = page.locator("#agreementStartDate")
    if date_input.count() == 0:
        issues.append("Missing date picker #agreementStartDate")

    # Compute button
    compute_btn = page.locator("button:has-text('Compute')")
    if compute_btn.count() == 0:
        issues.append("Missing Compute button")

    # Include non-taking dropdown
    include_select = page.locator("#includeNo")
    if include_select.count() == 0:
        issues.append("Missing #includeNo dropdown")

    # FAQ section
    faq_items = page.locator(".faq-question")
    if faq_items.count() < 3:
        issues.append(f"Expected 3+ FAQ items, found {faq_items.count()}")

    status = "PASS" if not issues else "WARN"
    record(name, "/availability/earliest", status, issues)


def test_adviser_schedule(page: Page):
    name = "04_adviser_schedule"
    issues = []
    visit_page(page, name, "/availability/schedule")

    # Adviser dropdown
    adviser_select = page.locator("#adviserSelect")
    if adviser_select.count() == 0:
        issues.append("Missing #adviserSelect dropdown")

    # Date picker
    date_input = page.locator("#agreementStartDate")
    if date_input.count() == 0:
        issues.append("Missing date picker")

    # Compute button
    compute_btn = page.locator("button:has-text('Compute')")
    if compute_btn.count() == 0:
        issues.append("Missing Compute button")

    status = "PASS" if not issues else "WARN"
    record(name, "/availability/schedule", status, issues)


def test_allocation_history(page: Page):
    name = "05_allocation_history"
    issues = []
    visit_page(page, name, "/allocations/history")

    # Filter controls
    for fid in ["daysFilter", "statusFilter", "dealFilter", "adviserFilter"]:
        el = page.locator(f"#{fid}")
        if el.count() == 0:
            issues.append(f"Missing filter #{fid}")

    # Clear filters button
    clear_btn = page.locator("button:has-text('Clear')")
    if clear_btn.count() == 0:
        issues.append("Missing Clear Filters button")

    # Export button
    export_btn = page.locator("button:has-text('Export')")
    if export_btn.count() == 0:
        issues.append("Missing Export button")

    # Pagination
    pagination = page.locator("button:has-text('Prev'), button:has-text('Next')")
    if pagination.count() < 2:
        issues.append("Missing pagination controls")

    status = "PASS" if not issues else "WARN"
    record(name, "/allocations/history", status, issues)


def test_workflows(page: Page):
    name = "06_workflows"
    issues = []
    visit_page(page, name, "/workflows")

    # Mermaid diagrams
    mermaid_divs = page.locator(".mermaid")
    if mermaid_divs.count() == 0:
        issues.append("No .mermaid diagram containers found")
    else:
        # Wait for mermaid to render SVGs
        page.wait_for_timeout(2000)
        svgs = page.locator(".mermaid svg")
        if svgs.count() == 0:
            issues.append("Mermaid diagrams did not render (no SVG elements)")
        take_screenshot(page, f"{name}_after_mermaid")

    # Navigation buttons
    for text in ["Allocation", "Guide", "Automation"]:
        btn = page.locator(f"button:has-text('{text}'), a:has-text('{text}')")
        if btn.count() == 0:
            issues.append(f"Missing navigation button containing '{text}'")

    status = "PASS" if not issues else "WARN"
    record(name, "/workflows", status, issues)


def test_workflows_detail(page: Page):
    name = "07_workflows_adviser_allocation"
    issues = []
    visit_page(page, name, "/workflows/adviser-allocation")

    # Breadcrumb back link
    back_link = page.locator("a[href='/workflows']")
    if back_link.count() == 0:
        issues.append("Missing breadcrumb back link to /workflows")

    status = "PASS" if not issues else "WARN"
    record(name, "/workflows/adviser-allocation", status, issues)


def test_availability_matrix(page: Page):
    name = "08_availability_matrix"
    issues = []
    visit_page(page, name, "/availability/matrix")

    # Table should render
    table = page.locator("table")
    if table.count() == 0:
        issues.append("No table found on matrix page")

    status = "PASS" if not issues else "WARN"
    record(name, "/availability/matrix", status, issues)


def test_clarify_chart(page: Page):
    name = "09_clarify_chart"
    issues = []
    visit_page(page, name, "/availability/clarify-chart")

    # Adviser filter
    adviser_filter = page.locator("#adviserFilter")
    if adviser_filter.count() == 0:
        issues.append("Missing #adviserFilter dropdown")

    # Chart canvas
    canvas = page.locator("#clarifyChart")
    if canvas.count() == 0:
        issues.append("Missing #clarifyChart canvas")

    # Wait for chart to load
    page.wait_for_timeout(2000)

    # Refresh button
    refresh_btn = page.locator("#refreshBtn")
    if refresh_btn.count() == 0:
        issues.append("Missing Refresh Chart button")

    take_screenshot(page, f"{name}_with_chart")

    status = "PASS" if not issues else "WARN"
    record(name, "/availability/clarify-chart", status, issues)


def test_meetings(page: Page):
    name = "10_meetings"
    issues = []
    visit_page(page, name, "/availability/meetings")

    # Adviser dropdown
    adviser_select = page.locator("#adviserSelect")
    if adviser_select.count() == 0:
        issues.append("Missing #adviserSelect dropdown")

    # Weeks input
    weeks_input = page.locator("#weeksBack")
    if weeks_input.count() == 0:
        issues.append("Missing #weeksBack input")

    status = "PASS" if not issues else "WARN"
    record(name, "/availability/meetings", status, issues)


def test_closures_ui(page: Page):
    name = "11_closures"
    issues = []
    status_code = visit_page(page, name, "/closures/ui")

    if status_code == 403:
        issues.append("Got 403 — admin_required blocked access")
        record(name, "/closures/ui", "FAIL", issues)
        return

    # Add form elements
    for eid in ["start", "end", "tagSelect", "description", "submit"]:
        el = page.locator(f"#{eid}")
        if el.count() == 0:
            issues.append(f"Missing form element #{eid}")

    # Closures table
    table = page.locator("table")
    if table.count() == 0:
        issues.append("No closures table found")

    status = "PASS" if not issues else "WARN"
    record(name, "/closures/ui", status, issues)


def test_capacity_overrides_ui(page: Page):
    name = "12_capacity_overrides"
    issues = []
    status_code = visit_page(page, name, "/capacity_overrides/ui")

    if status_code == 403:
        issues.append("Got 403 — admin_required blocked access")
        record(name, "/capacity_overrides/ui", "FAIL", issues)
        return

    # Form elements
    for eid in ["emailSelect", "effective", "limit", "podType", "notes", "submit"]:
        el = page.locator(f"#{eid}")
        if el.count() == 0:
            issues.append(f"Missing form element #{eid}")

    # Overrides table
    table = page.locator("table")
    if table.count() == 0:
        issues.append("No overrides table found")

    status = "PASS" if not issues else "WARN"
    record(name, "/capacity_overrides/ui", status, issues)


def test_employees_ui(page: Page):
    name = "13_employees"
    issues = []
    status_code = visit_page(page, name, "/employees/ui")

    if status_code == 403:
        issues.append("Got 403 — admin_required blocked access")
        record(name, "/employees/ui", "FAIL", issues)
        return

    # Employee table
    table = page.locator("table")
    if table.count() == 0:
        issues.append("No employees table found")

    status = "PASS" if not issues else "WARN"
    record(name, "/employees/ui", status, issues)


def test_leave_requests_ui(page: Page):
    name = "14_leave_requests"
    issues = []
    status_code = visit_page(page, name, "/leave_requests/ui")

    if status_code == 403:
        issues.append("Got 403 — admin_required blocked access")
        record(name, "/leave_requests/ui", "FAIL", issues)
        return

    # Filters
    for eid in ["employeeFilter", "statusFilter"]:
        el = page.locator(f"#{eid}")
        if el.count() == 0:
            issues.append(f"Missing filter #{eid}")

    # Calendar toggle
    cal_toggle = page.locator("#calendarToggle")
    if cal_toggle.count() == 0:
        issues.append("Missing #calendarToggle button")

    # Leave table
    table = page.locator("table")
    if table.count() == 0:
        issues.append("No leave requests table found")

    status = "PASS" if not issues else "WARN"
    record(name, "/leave_requests/ui", status, issues)


# ---------------------------------------------------------------------------
# Sidebar & navigation tests
# ---------------------------------------------------------------------------


def test_sidebar(page: Page):
    """Test sidebar toggle, collapsible section, and admin links."""
    name = "15_sidebar"
    issues = []

    page.goto(f"{BASE_URL}/", wait_until="networkidle")

    # Clear localStorage to test default state
    page.evaluate("localStorage.removeItem('sidebar_open')")
    page.reload(wait_until="networkidle")

    sidebar = page.locator("#sidebar")
    toggle = page.locator("#sidebarToggle")

    # Sidebar should be hidden by default (not have 'open' class)
    has_open = page.evaluate("document.getElementById('sidebar')?.classList.contains('open')")
    if has_open:
        issues.append("Sidebar has 'open' class by default — should be hidden")

    # Hamburger toggle should be visible
    if not toggle.is_visible():
        issues.append("Hamburger toggle not visible when sidebar is closed")

    take_screenshot(page, f"{name}_closed")

    # Click hamburger to open sidebar
    toggle.click()
    page.wait_for_timeout(500)

    has_open_after = page.evaluate("document.getElementById('sidebar')?.classList.contains('open')")
    if not has_open_after:
        issues.append("Sidebar did not open after clicking hamburger")

    take_screenshot(page, f"{name}_open")

    # Check sidebar links
    expected_links = [
        "Home",
        "Earliest Availability",
        "Adviser Schedule",
        "Allocation History",
        "Workflows",
    ]
    for link_text in expected_links:
        link = sidebar.locator(f"a:has-text('{link_text}')")
        if link.count() == 0:
            issues.append(f"Missing sidebar link: {link_text}")

    # Check Settings & Tools collapsible
    settings_toggle = page.locator(".sidebar-collapsible-toggle")
    if settings_toggle.count() == 0:
        issues.append("Missing Settings & Tools collapsible toggle")
    else:
        settings_toggle.click()
        page.wait_for_timeout(500)
        take_screenshot(page, f"{name}_settings_open")

        # Check sub-sections visible
        for section_text in ["Visualizations", "Admin", "Data"]:
            section = sidebar.locator(f"text={section_text}")
            if section.count() == 0:
                issues.append(f"Missing sub-section: {section_text}")

        # Check admin-only links
        admin_links = ["Closures", "Capacity Overrides", "Employees", "Leave Requests"]
        for link_text in admin_links:
            link = sidebar.locator(f"a:has-text('{link_text}')")
            if link.count() == 0:
                issues.append(f"Missing admin sidebar link: {link_text}")

    # Click outside to close
    page.locator(".container").click(position={"x": 500, "y": 300})
    page.wait_for_timeout(500)

    has_open_final = page.evaluate("document.getElementById('sidebar')?.classList.contains('open')")
    if has_open_final:
        issues.append("Sidebar did not close when clicking outside")

    take_screenshot(page, f"{name}_closed_again")

    status = "PASS" if not issues else "WARN"
    record(name, "sidebar interactions", status, issues)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_report():
    print("\n" + "=" * 72)
    print("  PLAYWRIGHT QA REPORT — Adviser Allocation")
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
    print(f"  Screenshots saved to: {SCREENSHOTS_DIR}")
    print("=" * 72)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )

        # Authenticate
        print("Authenticating via /login_bypass...")
        authenticate(context)

        # Run tests with authenticated context
        page = context.new_page()

        print("Testing pages...\n")

        tests = [
            ("Homepage", test_homepage),
            ("Earliest Availability", test_earliest_availability),
            ("Adviser Schedule", test_adviser_schedule),
            ("Allocation History", test_allocation_history),
            ("Workflows", test_workflows),
            ("Workflows Detail", test_workflows_detail),
            ("Availability Matrix", test_availability_matrix),
            ("Clarify Chart", test_clarify_chart),
            ("Meetings", test_meetings),
            ("Closures UI", test_closures_ui),
            ("Capacity Overrides UI", test_capacity_overrides_ui),
            ("Employees UI", test_employees_ui),
            ("Leave Requests UI", test_leave_requests_ui),
            ("Sidebar Interactions", test_sidebar),
        ]

        for label, test_fn in tests:
            try:
                test_fn(page)
                print(f"  [done] {label}")
            except Exception as exc:
                print(f"  [CRASH] {label}: {exc}")
                record(label, "", "FAIL", [f"Crashed: {exc}"])

        page.close()

        # Test login page with a fresh (unauthenticated) context
        print("\n  Testing login page (unauthenticated)...")
        login_context = browser.new_context(viewport={"width": 1440, "height": 900})
        login_page = login_context.new_page()
        test_login(login_page)
        login_page.close()
        login_context.close()
        print("  [done] Login Page")

        context.close()
        browser.close()

    print_report()


if __name__ == "__main__":
    main()
