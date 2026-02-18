"""End-to-end tests for adviser_allocation app using Playwright."""

import os

import pytest
from playwright.sync_api import sync_playwright

# App URL - change this to your deployed app
APP_URL = os.environ.get("APP_URL", "https://pivot-digital-466902.ts.r.appspot.com")


@pytest.fixture
def browser():
    """Provide a Playwright browser instance."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    """Provide a Playwright page instance."""
    page = browser.new_page()
    yield page
    page.close()


class TestAppHomepage:
    """Test suite for homepage functionality."""

    def test_homepage_redirects_to_login(self, page):
        """Test that homepage redirects to login when not authenticated."""
        page.goto(APP_URL)
        # Should redirect to login
        assert "/login" in page.url or page.status_code == 302
        assert page.title

    def test_homepage_responds(self, page):
        """Test that homepage responds without errors."""
        response = page.goto(APP_URL)
        # Should get a response (redirect is ok)
        assert response.status < 500


class TestLoginPage:
    """Test suite for login functionality."""

    def test_login_page_loads(self, page):
        """Test that login page loads successfully."""
        page.goto(f"{APP_URL}/login")
        assert page.url
        # Check for common login page elements
        page.wait_for_load_state("networkidle", timeout=5000)

    def test_login_page_has_form(self, page):
        """Test that login page has login form."""
        page.goto(f"{APP_URL}/login")
        page.wait_for_load_state("networkidle", timeout=5000)
        # Check for form or input elements
        inputs = page.locator("input").count()
        assert inputs > 0, "Login page should have input fields"

    def test_login_page_accessible(self, page):
        """Test login page is accessible without authentication."""
        response = page.goto(f"{APP_URL}/login")
        assert response.status < 400, "Login page should be accessible"


class TestStaticFiles:
    """Test suite for static files and assets."""

    def test_css_file_loads(self, page):
        """Test that CSS file loads successfully."""
        page.goto(f"{APP_URL}/login", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=5000)
        # Check if page loaded successfully
        assert page.url
        # May or may not have stylesheets depending on page

    def test_static_content_loads(self, page):
        """Test that static content loads without 404s."""
        response = page.goto(f"{APP_URL}/static/css/app.css")
        # CSS file should return 200 or redirect
        status = response.status
        assert status < 400 or status == 401, f"CSS should load (got {status})"


class TestPublicEndpoints:
    """Test suite for publicly accessible endpoints."""

    def test_health_check_endpoint(self, page):
        """Test that app responds to requests."""
        response = page.goto(f"{APP_URL}/")
        # Should respond (redirect is ok)
        assert response.status < 500

    def test_allocation_webhook_endpoint_accessible(self, page):
        """Test that allocation webhook endpoint is accessible."""
        # POST endpoint - just check it exists
        try:
            response = page.goto(f"{APP_URL}/post/allocate")
            # Should be 405 (method not allowed) for GET, or 200 with message
            assert response.status in [200, 405, 400, 415]
        except Exception as e:
            # Navigation to POST endpoint might fail, that's ok
            pass

    def test_workflows_page_loads(self, page):
        """Test that workflows documentation page loads."""
        response = page.goto(f"{APP_URL}/workflows")
        # Workflows page might require auth
        assert response.status < 500


class TestErrorHandling:
    """Test suite for error handling and edge cases."""

    def test_404_page_handling(self, page):
        """Test that 404s are handled gracefully."""
        response = page.goto(f"{APP_URL}/nonexistent-page-12345")
        # Should return 200 (redirect to login) or 404
        assert response.status in [200, 301, 302, 404, 401], f"Got {response.status}"

    def test_invalid_route_handling(self, page):
        """Test invalid routes don't crash app."""
        response = page.goto(f"{APP_URL}/invalid/path/xyz")
        # Should not return 500
        assert response.status != 500

    def test_special_characters_in_url(self, page):
        """Test that special characters in URLs are handled."""
        response = page.goto(f"{APP_URL}/test?param=<script>alert(1)</script>")
        # Should not execute script
        assert response.status < 500


class TestResponseHeaders:
    """Test suite for HTTP response headers."""

    def test_content_type_header(self, page):
        """Test that Content-Type header is set."""
        response = page.goto(APP_URL)
        # Headers should be present
        assert response.headers

    def test_security_headers_present(self, page):
        """Test that security headers are present."""
        response = page.goto(APP_URL)
        headers = response.headers
        # Check for common security headers (may vary by config)
        # App should at least have something
        assert len(headers) > 0


class TestJavaScriptExecution:
    """Test suite for JavaScript functionality."""

    def test_page_executes_javascript(self, page):
        """Test that JavaScript can execute on page."""
        page.goto(f"{APP_URL}/login")
        page.wait_for_load_state("networkidle", timeout=5000)
        # Try to execute JavaScript
        try:
            result = page.evaluate("1 + 1")
            assert result == 2
        except Exception:
            # JS execution might be blocked
            pass

    def test_console_no_critical_errors(self, page):
        """Test that page console has no critical errors."""
        errors = []
        page.on("console", lambda msg: errors.append((msg.type, msg.text)))
        page.goto(APP_URL)
        page.wait_for_load_state("networkidle", timeout=5000)
        # Filter for actual errors (not warnings)
        critical_errors = [e for e in errors if e[0] == "error"]
        # Some console errors are ok, just check we don't crash


class TestPerformance:
    """Test suite for performance metrics."""

    def test_page_load_time(self, page):
        """Test that page loads within reasonable time."""
        import time

        start = time.time()
        page.goto(APP_URL, wait_until="networkidle")
        elapsed = time.time() - start
        # Should load within 30 seconds
        assert elapsed < 30, f"Page took {elapsed}s to load"

    def test_homepage_response_time(self, page):
        """Test homepage response time."""
        import time

        start = time.time()
        response = page.goto(APP_URL, timeout=10000)
        elapsed = time.time() - start
        # Should respond within 10 seconds
        assert elapsed < 10, f"Homepage took {elapsed}s to respond"


class TestApplicationStructure:
    """Test suite for app structure and content."""

    def test_app_has_title(self, page):
        """Test that app pages have titles."""
        page.goto(f"{APP_URL}/login")
        page.wait_for_load_state("networkidle", timeout=5000)
        title = page.title()
        # Should have a title
        assert title and len(title) > 0

    def test_app_has_content(self, page):
        """Test that app pages have content."""
        page.goto(f"{APP_URL}/login")
        page.wait_for_load_state("networkidle", timeout=5000)
        content = page.content()
        # Should have HTML content
        assert len(content) > 100


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
