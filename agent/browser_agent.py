"""
Browser Agent - Playwright layer for web chatbot interaction.

Handles:
- Navigating to property websites
- Detecting and opening chat widgets
- Conducting conversations as the persona
- Capturing full transcripts with timestamps
- Detecting the chatbot platform in use
- Graceful failure handling
"""

import asyncio
import time
from datetime import datetime
from typing import Optional
from loguru import logger
from playwright.async_api import (
    async_playwright,
    Page,
    Browser,
    TimeoutError as PlaywrightTimeout
)

from database.models import (
    Engagement, Message,
    MessageSender, ConversationStage, ChannelType,
    EngagementStatus
)
from database.connection import get_db


# --- Known chatbot platform fingerprints ---
# We detect these from page source / iframe URLs / widget attributes

CHATBOT_PLATFORMS = {
    "EliseAI":   ["elise", "helloelise"],
    "Knock":     ["knock.app", "knockcrm"],
    "Funnel":    ["funnelleasing", "funnel.io"],
    "Quext":     ["quext.io", "quextai"],
    "Zuma":      ["zumaai", "zumapets"],
    "Rentgrata": ["rentgrata"],
    "Anyone Home": ["anyonehome"],
    "Lease Hawk": ["leasehawk"],
    "Nurture Boss": ["nurtureboss"],
}

# Chat widget selectors to try, in order of likelihood
CHAT_WIDGET_SELECTORS = [
    # Generic open buttons
    "button[class*='chat']",
    "button[class*='Chat']",
    "div[class*='chat-button']",
    "div[class*='chatbot']",
    "div[class*='chat-widget']",
    "[aria-label*='chat' i]",
    "[aria-label*='Chat' i]",
    # Common platform-specific
    "iframe[src*='elise']",
    "iframe[src*='knock']",
    "iframe[src*='funnel']",
    "[id*='chat-widget']",
    "[id*='chatWidget']",
    "[class*='LiveChat']",
    # Fallback - any suspiciously round button in corner
    "button[style*='fixed']",
    "div[style*='fixed'][style*='bottom']",
]

# Selectors for the chat input field once widget is open
CHAT_INPUT_SELECTORS = [
    "textarea[placeholder*='message' i]",
    "textarea[placeholder*='type' i]",
    "input[placeholder*='message' i]",
    "input[placeholder*='type' i]",
    "input[placeholder*='Ask' i]",
    "div[contenteditable='true']",
    "textarea",
    "input[type='text']",
]

# Selectors for send button
SEND_BUTTON_SELECTORS = [
    "button[type='submit']",
    "button[aria-label*='send' i]",
    "button[class*='send']",
    "[data-testid*='send']",
]


class BrowserAgent:
    def __init__(self, headless: bool = True, slow_mo: int = 150):
        """
        headless: Run without visible browser (True for Cloud Run)
        slow_mo: Milliseconds between actions — makes behavior more human-like
        """
        self.headless = headless
        self.slow_mo = slow_mo
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        logger.info(f"BrowserAgent initialized (headless={headless}, slow_mo={slow_mo}ms)")

    # -------------------------------------------------------------------------
    # LIFECYCLE
    # -------------------------------------------------------------------------

    async def start(self):
        """Launch the browser."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",   # Required for Cloud Run
                "--disable-blink-features=AutomationControlled",
            ]
        )
        logger.info("Browser launched")

    async def stop(self):
        """Close browser and cleanup."""
        if self.page:
            await self.page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser stopped")

    async def new_page(self) -> Page:
        """
        Create a new page with human-like headers.
        Overrides automation signals that some chatbots detect.
        """
        context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        # Remove webdriver flag that bot detectors look for
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        self.page = await context.new_page()
        return self.page

    # -------------------------------------------------------------------------
    # NAVIGATION
    # -------------------------------------------------------------------------

    async def navigate_to_property(self, url: str) -> bool:
        """
        Load the property website and wait for it to be interactive.
        Returns True if page loaded successfully.
        """
        logger.info(f"Navigating to {url}")
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Human-like pause after page load
            await self._human_pause(2, 4)
            # Scroll down slightly — some chat widgets only appear after scroll
            await self.page.evaluate("window.scrollBy(0, 300)")
            await self._human_pause(1, 2)
            logger.success(f"Page loaded: {url}")
            return True
        except PlaywrightTimeout:
            logger.error(f"Timeout loading {url}")
            return False
        except Exception as e:
            logger.error(f"Navigation error for {url}: {e}")
            return False

    # -------------------------------------------------------------------------
    # PLATFORM DETECTION
    # -------------------------------------------------------------------------

    async def detect_chatbot_platform(self) -> str:
        """
        Attempts to identify which chatbot platform the property is using
        by examining page source, iframes, and script tags.
        """
        try:
            # Check page HTML for platform fingerprints
            content = await self.page.content()
            content_lower = content.lower()

            for platform, signals in CHATBOT_PLATFORMS.items():
                if any(signal in content_lower for signal in signals):
                    logger.info(f"Detected platform: {platform}")
                    return platform

            # Check all iframes
            frames = self.page.frames
            for frame in frames:
                frame_url = frame.url.lower()
                for platform, signals in CHATBOT_PLATFORMS.items():
                    if any(signal in frame_url for signal in signals):
                        logger.info(f"Detected platform via iframe: {platform}")
                        return platform

            logger.info("Platform not identified — unknown chatbot")
            return "Unknown"

        except Exception as e:
            logger.warning(f"Platform detection failed: {e}")
            return "Unknown"

    # -------------------------------------------------------------------------
    # CHAT WIDGET INTERACTION
    # -------------------------------------------------------------------------

    async def find_and_open_chat(self) -> bool:
        """
        Locates the chat widget on the page and clicks to open it.
        Tries multiple selector strategies.
        Returns True if chat was successfully opened.
        """
        logger.info("Searching for chat widget...")

        for selector in CHAT_WIDGET_SELECTORS:
            try:
                element = await self.page.wait_for_selector(
                    selector,
                    timeout=3000,
                    state="visible"
                )
                if element:
                    logger.info(f"Found chat widget with selector: {selector}")
                    await self._human_pause(0.5, 1)
                    await element.click()
                    await self._human_pause(1.5, 3)

                    # Verify chat opened by looking for input field
                    if await self._find_chat_input():
                        logger.success("Chat widget opened successfully")
                        return True
            except PlaywrightTimeout:
                continue
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue

        # Last resort: look for chat inside iframes
        if await self._try_iframe_chat():
            return True

        logger.warning("Could not locate chat widget on this page")
        return False

    async def _find_chat_input(self, timeout: int = 5000) -> bool:
        """Check if a chat input field is visible on screen."""
        for selector in CHAT_INPUT_SELECTORS:
            try:
                el = await self.page.wait_for_selector(
                    selector, timeout=timeout, state="visible"
                )
                if el:
                    return True
            except PlaywrightTimeout:
                continue
        return False

    async def _try_iframe_chat(self) -> bool:
        """
        Some chatbots render entirely inside an iframe.
        Try to switch context to each iframe and look for inputs.
        """
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                for selector in CHAT_INPUT_SELECTORS:
                    el = await frame.query_selector(selector)
                    if el:
                        logger.info(f"Found chat input inside iframe: {frame.url}")
                        self._active_frame = frame
                        return True
            except Exception:
                continue
        return False

    # -------------------------------------------------------------------------
    # SENDING AND RECEIVING MESSAGES
    # -------------------------------------------------------------------------

    async def send_message(self, text: str) -> bool:
        """
        Types and sends a message in the chat widget.
        Uses human-like typing with realistic delays.
        Returns True if message was sent successfully.
        """
        frame = getattr(self, '_active_frame', self.page)

        input_el = None
        for selector in CHAT_INPUT_SELECTORS:
            try:
                input_el = await frame.wait_for_selector(
                    selector, timeout=5000, state="visible"
                )
                if input_el:
                    break
            except PlaywrightTimeout:
                continue

        if not input_el:
            logger.error("Could not find chat input to type into")
            return False

        try:
            await input_el.click()
            await self._human_pause(0.3, 0.7)

            # Type like a human — character by character with variance
            await input_el.type(text, delay=self._human_typing_delay())
            await self._human_pause(0.5, 1.5)  # Pause before sending

            # Try pressing Enter first (most common)
            await self.page.keyboard.press("Enter")
            await self._human_pause(0.5, 1)

            # If Enter didn't send it, look for a send button
            sent = await self._verify_message_sent(text)
            if not sent:
                await self._click_send_button(frame)

            logger.debug(f"Message sent: {text[:60]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    async def _click_send_button(self, frame):
        for selector in SEND_BUTTON_SELECTORS:
            try:
                btn = await frame.query_selector(selector)
                if btn:
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    async def _verify_message_sent(self, text: str, timeout: int = 3000) -> bool:
        """Check that the message appears in the chat window."""
        try:
            # Look for the message text appearing in the chat
            await self.page.wait_for_function(
                f"document.body.innerText.includes({json.dumps(text[:30])})",
                timeout=timeout
            )
            return True
        except Exception:
            return False

    async def wait_for_response(
        self,
        previous_message_count: int,
        timeout: int = 30000
    ) -> Optional[str]:
        """
        Waits for a new message to appear in the chat window.
        Returns the response text, or None if timeout.

        Uses message count change as the signal rather than trying
        to parse specific elements — works across platforms.
        """
        logger.debug("Waiting for chatbot response...")
        start = time.time()
        last_text = ""

        while (time.time() - start) * 1000 < timeout:
            try:
                # Grab all visible text in the chat area
                current_text = await self.page.evaluate("""
                    () => {
                        // Try common chat container selectors
                        const selectors = [
                            '[class*="message"]',
                            '[class*="chat"]',
                            '[class*="conversation"]',
                            '[role="log"]',
                            '[aria-live]'
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el) return el.innerText;
                        }
                        return document.body.innerText;
                    }
                """)

                if current_text != last_text and len(current_text) > len(last_text) + 10:
                    # Something new appeared — extract just the new part
                    await self._human_pause(1, 2)  # Wait to make sure it's complete

                    # Confirm it stopped growing (message complete)
                    check_text = await self.page.evaluate(
                        "() => document.body.innerText"
                    )
                    if check_text == current_text:
                        # Stable — extract the new content
                        new_content = self._extract_latest_response(current_text)
                        logger.debug(f"Response received: {new_content[:80]}...")
                        return new_content

                    last_text = current_text

            except Exception as e:
                logger.debug(f"Response polling error: {e}")

            await asyncio.sleep(1)

        logger.warning("Timeout waiting for chatbot response")
        return None

    def _extract_latest_response(self, full_text: str) -> str:
        """
        Pull the most recent message from the full chat text.
        Rough heuristic: take the last non-empty paragraph.
        """
        lines = [l.strip() for l in full_text.split('\n') if l.strip()]
        if not lines:
            return full_text

        # Take last 3 lines as the response (covers multi-line replies)
        return " ".join(lines[-3:])

    # -------------------------------------------------------------------------
    # FULL ENGAGEMENT RUN
    # -------------------------------------------------------------------------

    async def run_engagement(
        self,
        engagement_id: str,
        property_url: str,
        persona: dict,
        orchestrator,  # Orchestrator instance
    ) -> dict:
        """
        Runs the full webchat portion of an engagement:
        Discovery → Nuance → Handoff Trigger

        Returns a summary dict with transcript and metadata.
        """
        result = {
            "engagement_id": engagement_id,
            "property_url": property_url,
            "persona_id": persona["id"],
            "platform": "Unknown",
            "success": False,
            "handoff_triggered": False,
            "transcript": [],
            "error": None,
        }

        try:
            await self.start()
            await self.new_page()

            # Load the property site
            loaded = await self.navigate_to_property(property_url)
            if not loaded:
                result["error"] = "Page failed to load"
                return result

            # Detect the platform
            result["platform"] = await self.detect_chatbot_platform()

            # Find and open the chat
            chat_opened = await self.find_and_open_chat()
            if not chat_opened:
                result["error"] = "Could not locate chat widget"
                return result

            # Update engagement status in DB
            with get_db() as db:
                engagement = db.query(Engagement).filter_by(id=engagement_id).first()
                if engagement:
                    engagement.status = EngagementStatus.IN_PROGRESS
                    engagement.started_at = datetime.utcnow()
                    engagement.chatbot_platform = result["platform"]

            # Run through the conversation stages
            stages = [
                ConversationStage.DISCOVERY,
                ConversationStage.NUANCE,
                ConversationStage.HANDOFF_TRIGGER,
            ]

            conversation_history = []
            message_count = 0

            for stage in stages:
                logger.info(f"--- Stage: {stage.value} ---")

                # Generate the persona message for this stage
                if not conversation_history:
                    # Opening message
                    last_property_msg = "Hello! Welcome. How can I help you today?"
                else:
                    last_property_msg = conversation_history[-1]["content"] \
                        if conversation_history[-1]["sender"] != "persona" else ""

                persona_msg = orchestrator.generate_persona_message(
                    persona=persona,
                    stage=stage,
                    conversation_history=conversation_history,
                    last_property_message=last_property_msg,
                    channel=ChannelType.WEBCHAT
                )

                # Send the message
                sent = await self.send_message(persona_msg)
                if not sent:
                    logger.warning(f"Failed to send message at stage {stage.value}")
                    break

                sent_at = datetime.utcnow()

                # Log persona message
                persona_entry = {
                    "sender": "persona",
                    "channel": "webchat",
                    "stage": stage.value,
                    "content": persona_msg,
                    "sent_at": sent_at.isoformat(),
                }
                conversation_history.append(persona_entry)
                result["transcript"].append(persona_entry)

                self._save_message(
                    engagement_id=engagement_id,
                    sender=MessageSender.PERSONA,
                    channel=ChannelType.WEBCHAT,
                    stage=stage,
                    content=persona_msg,
                    sent_at=sent_at,
                )

                # Wait for property response
                message_count += 1
                response = await self.wait_for_response(message_count, timeout=30000)

                if response:
                    received_at = datetime.utcnow()
                    property_entry = {
                        "sender": "ai_bot",
                        "channel": "webchat",
                        "stage": stage.value,
                        "content": response,
                        "sent_at": received_at.isoformat(),
                    }
                    conversation_history.append(property_entry)
                    result["transcript"].append(property_entry)

                    self._save_message(
                        engagement_id=engagement_id,
                        sender=MessageSender.AI_BOT,
                        channel=ChannelType.WEBCHAT,
                        stage=stage,
                        content=response,
                        sent_at=received_at,
                    )
                    message_count += 1
                else:
                    logger.warning(f"No response received at stage {stage.value}")

                # Pause between stages like a real human would
                await self._human_pause(3, 7)

            # Mark handoff triggered if we made it to that stage
            result["handoff_triggered"] = True

            with get_db() as db:
                engagement = db.query(Engagement).filter_by(id=engagement_id).first()
                if engagement:
                    engagement.status = EngagementStatus.AWAITING_HUMAN
                    engagement.handoff_triggered_at = datetime.utcnow()

            result["success"] = True
            logger.success(f"Engagement {engagement_id} webchat phase complete")

        except Exception as e:
            logger.error(f"Engagement {engagement_id} failed: {e}")
            result["error"] = str(e)

            with get_db() as db:
                engagement = db.query(Engagement).filter_by(id=engagement_id).first()
                if engagement:
                    engagement.status = EngagementStatus.FAILED

        finally:
            await self.stop()

        return result

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _save_message(
        self,
        engagement_id: str,
        sender: MessageSender,
        channel: ChannelType,
        stage: ConversationStage,
        content: str,
        sent_at: datetime,
    ):
        """Persist a single message to the database."""
        with get_db() as db:
            msg = Message(
                engagement_id=engagement_id,
                sender=sender,
                channel=channel,
                stage=stage,
                content=content,
                sent_at=sent_at,
            )
            db.add(msg)

    async def _human_pause(self, min_sec: float, max_sec: float):
        """Random pause to simulate human reading/thinking time."""
        import random
        duration = random.uniform(min_sec, max_sec)
        await asyncio.sleep(duration)

    def _human_typing_delay(self) -> int:
        """Returns a typing delay in ms that feels human (60-120 WPM range)."""
        import random
        return random.randint(50, 120)


import json  # needed for _verify_message_sent
