from playwright.sync_api import sync_playwright


def open_job_in_browser(url: str) -> None:
    """Open a job's URL in a real, visible browser window and block until the user closes it."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(url)
        page.wait_for_event("close", timeout=0)
        browser.close()
