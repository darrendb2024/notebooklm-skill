#!/usr/bin/env python3
"""
Simple NotebookLM Question Interface
Based on MCP server implementation - simplified without sessions

Implements hybrid auth approach:
- Persistent browser profile (user_data_dir) for fingerprint consistency
- Manual cookie injection from state.json for session cookies (Playwright bug workaround)
See: https://github.com/microsoft/playwright/issues/36139
"""

import argparse
import sys
import time
import re
from pathlib import Path

from patchright.sync_api import sync_playwright

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from auth_manager import AuthManager
from notebook_manager import NotebookLibrary
from config import QUERY_INPUT_SELECTORS, RESPONSE_SELECTORS
from browser_utils import BrowserFactory, StealthUtils


# Follow-up reminder (adapted from MCP server for stateless operation)
# Since we don't have persistent sessions, we encourage comprehensive questions
FOLLOW_UP_REMINDER = (
    "\n\nEXTREMELY IMPORTANT: Is that ALL you need to know? "
    "You can always ask another question! Think about it carefully: "
    "before you reply to the user, review their original request and this answer. "
    "If anything is still unclear or missing, ask me another comprehensive question "
    "that includes all necessary context (since each question opens a new browser session)."
)


def clear_chat_history(page) -> bool:
    """Clear NotebookLM chat history via Chat options menu."""
    try:
        options_btn = page.query_selector('[aria-label="Chat options"]')
        if not options_btn:
            return False
        options_btn.click()
        time.sleep(1)
        # Find "Delete chat history" menu item
        menu_items = page.query_selector_all('[role="menuitem"], .mat-menu-item, [class*="menu-item"]')
        for item in menu_items:
            try:
                txt = item.inner_text().strip()
                if "Delete chat history" in txt or "delete" in txt.lower():
                    item.click()
                    time.sleep(1)
                    # Confirm if dialog appears
                    confirm = page.query_selector('button:has-text("Delete"), button:has-text("Confirm"), button:has-text("OK")')
                    if confirm:
                        confirm.click()
                        time.sleep(1)
                    print("  🗑️ Chat history cleared")
                    return True
            except:
                continue
        # Close menu if delete not found
        page.keyboard.press("Escape")
        return False
    except Exception as e:
        print(f"  ⚠️ Could not clear chat history: {e}")
        return False


def ask_notebooklm(question: str, notebook_url: str, headless: bool = False, new_chat: bool = True) -> str:
    """
    Ask a question to NotebookLM

    Args:
        question: Question to ask
        notebook_url: NotebookLM notebook URL
        headless: Run browser in headless mode
        new_chat: Clear chat history before asking (avoids context contamination)

    Returns:
        Answer text from NotebookLM
    """
    auth = AuthManager()

    if not auth.is_authenticated():
        print("⚠️ Not authenticated. Run: python auth_manager.py setup")
        return None

    print(f"💬 Asking: {question}")
    print(f"📚 Notebook: {notebook_url}")

    playwright = None
    context = None

    try:
        # Start playwright
        playwright = sync_playwright().start()

        # Launch persistent browser context using factory
        context = BrowserFactory.launch_persistent_context(
            playwright,
            headless=headless
        )

        # Navigate to notebook
        page = context.new_page()
        print("  🌐 Opening notebook...")
        page.goto(notebook_url, wait_until="domcontentloaded")

        # Verify we're on NotebookLM (check URL directly, no navigation event needed)
        current_url = page.url
        if "notebooklm.google.com" not in current_url:
            # Redirected to login or elsewhere
            raise Exception(f"Unexpected URL after navigation: {current_url}")

        # Wait for SPA to finish mounting components
        time.sleep(3)

        # Wait for query input — give page time to fully render chat interface
        print("  ⏳ Waiting for query input...")
        query_element = None

        for selector in QUERY_INPUT_SELECTORS:
            try:
                query_element = page.wait_for_selector(
                    selector,
                    timeout=30000,
                    state="visible"
                )
                if query_element:
                    print(f"  ✓ Found input: {selector}")
                    break
            except:
                continue

        if not query_element:
            # Dump available inputs for debugging — also check shadow DOM
            try:
                inputs = page.evaluate("""() => {
                    function queryAll(root, sel) {
                        let found = [...root.querySelectorAll(sel)];
                        root.querySelectorAll('*').forEach(el => {
                            if (el.shadowRoot) found = found.concat(queryAll(el.shadowRoot, sel));
                        });
                        return found;
                    }
                    const sel = 'textarea, [contenteditable="true"], [role="textbox"], input[type="text"]';
                    const els = queryAll(document, sel);
                    return els.map(e => ({
                        tag: e.tagName,
                        class: e.className ? String(e.className).substring(0, 60) : '',
                        aria: e.getAttribute('aria-label') || '',
                        placeholder: e.getAttribute('placeholder') || '',
                        role: e.getAttribute('role') || '',
                        visible: e.offsetParent !== null,
                        inShadow: e.getRootNode() !== document
                    }));
                }""")
                print(f"  🔍 Found {len(inputs)} input elements (including shadow DOM):")
                for el in inputs:
                    shadow = " [SHADOW]" if el.get('inShadow') else ""
                    print(f"     {el['tag']}{shadow} class='{el['class']}' aria='{el['aria']}' placeholder='{el['placeholder']}' visible={el['visible']}")
                # Also dump page title and URL for context
                print(f"  🔍 Page URL: {page.url}")
                print(f"  🔍 Page title: {page.title()}")
            except Exception as de:
                print(f"  🔍 Debug failed: {de}")
            print("  ❌ Could not find query input")
            return None

        # Clear chat history for fresh context (avoids contamination from previous sessions)
        if new_chat:
            clear_chat_history(page)
            time.sleep(1)

        # Snapshot existing responses BEFORE typing (fix: prevent returning stale cached response)
        previous_response = None
        for selector in RESPONSE_SELECTORS:
            try:
                elements = page.query_selector_all(selector)
                if elements:
                    previous_response = elements[-1].inner_text().strip()
                    print(f"  📸 Snapshot: found existing response ({len(previous_response)} chars)")
                    break
            except:
                continue

        # Type question (human-like, fast)
        print("  ⏳ Typing question...")

        # Use primary selector for typing
        input_selector = QUERY_INPUT_SELECTORS[0]
        StealthUtils.human_type(page, input_selector, question)

        # Submit
        print("  📤 Submitting...")
        page.keyboard.press("Enter")

        # Small pause
        StealthUtils.random_delay(500, 1500)

        # Wait for response (MCP approach: poll for stable text)
        print("  ⏳ Waiting for answer...")

        answer = None
        stable_count = 0
        last_text = None
        saw_activity = False              # thinking indicator OR new streaming text seen
        start = time.time()
        no_activity_grace = 60            # fail fast if nothing happens at all
        deadline = start + 240            # overall cap for genuinely long responses

        while time.time() < deadline:
            # Check if NotebookLM is still thinking (most reliable indicator)
            try:
                thinking_element = page.query_selector('div.thinking-message')
                if thinking_element and thinking_element.is_visible():
                    saw_activity = True
                    time.sleep(1)
                    continue
            except:
                pass

            # Try to find response with MCP selectors
            for selector in RESPONSE_SELECTORS:
                try:
                    elements = page.query_selector_all(selector)
                    if elements:
                        # Get last (newest) response
                        latest = elements[-1]
                        text = latest.inner_text().strip()

                        # Must be different from pre-question snapshot
                        if text and text != previous_response:
                            saw_activity = True
                            if text == last_text:
                                stable_count += 1
                                if stable_count >= 3:  # Stable for 3 polls
                                    answer = text
                                    break
                            else:
                                stable_count = 0
                                last_text = text
                except:
                    continue

            if answer:
                break

            # Fail fast: no thinking indicator and no new text within grace window
            # (submit didn't register, or stale chat UI) — avoids a full 4-minute hang.
            if not saw_activity and (time.time() - start) > no_activity_grace:
                print(f"  ❌ No response activity after {no_activity_grace}s — submit likely didn't register (try --new-chat / re-auth)")
                return None

            time.sleep(1)

        if not answer:
            print("  ❌ Timeout waiting for answer")
            return None

        print("  ✅ Got answer!")
        # Add follow-up reminder to encourage Claude to ask more questions
        return answer + FOLLOW_UP_REMINDER

    except Exception as e:
        print(f"  ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return None

    finally:
        # Always clean up
        if context:
            try:
                context.close()
            except:
                pass

        if playwright:
            try:
                playwright.stop()
            except:
                pass


def main():
    parser = argparse.ArgumentParser(description='Ask NotebookLM a question')

    parser.add_argument('--question', required=True, help='Question to ask')
    parser.add_argument('--notebook-url', help='NotebookLM notebook URL')
    parser.add_argument('--notebook-id', help='Notebook ID from library')
    parser.add_argument('--show-browser', action='store_true', help='Show browser')
    parser.add_argument('--new-chat', action='store_true', help='(deprecated) clearing chat history is now the default')
    parser.add_argument('--keep-history', action='store_true', help='Do NOT clear chat history (keep prior chat context)')

    args = parser.parse_args()

    # Resolve notebook URL
    notebook_url = args.notebook_url

    if not notebook_url and args.notebook_id:
        library = NotebookLibrary()
        notebook = library.get_notebook(args.notebook_id)
        if notebook:
            notebook_url = notebook['url']
        else:
            print(f"❌ Notebook '{args.notebook_id}' not found")
            return 1

    if not notebook_url:
        # Check for active notebook first
        library = NotebookLibrary()
        active = library.get_active_notebook()
        if active:
            notebook_url = active['url']
            print(f"📚 Using active notebook: {active['name']}")
        else:
            # Show available notebooks
            notebooks = library.list_notebooks()
            if notebooks:
                print("\n📚 Available notebooks:")
                for nb in notebooks:
                    mark = " [ACTIVE]" if nb.get('id') == library.active_notebook_id else ""
                    print(f"  {nb['id']}: {nb['name']}{mark}")
                print("\nSpecify with --notebook-id or set active:")
                print("python scripts/run.py notebook_manager.py activate --id ID")
            else:
                print("❌ No notebooks in library. Add one first:")
                print("python scripts/run.py notebook_manager.py add --url URL --name NAME --description DESC --topics TOPICS")
            return 1

    # Ask the question
    answer = ask_notebooklm(
        question=args.question,
        notebook_url=notebook_url,
        headless=not args.show_browser,
        new_chat=not args.keep_history
    )

    if answer:
        print("\n" + "=" * 60)
        print(f"Question: {args.question}")
        print("=" * 60)
        print()
        print(answer)
        print()
        print("=" * 60)
        return 0
    else:
        print("\n❌ Failed to get answer")
        return 1


if __name__ == "__main__":
    sys.exit(main())
