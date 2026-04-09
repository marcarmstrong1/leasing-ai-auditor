"""
Browser Agent - Playwright layer for web chatbot interaction.
Updated with Funnel-specific selectors and shadow DOM support.
"""

import asyncio
import json
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


CHATBOT_PLATFORMS = {
    "EliseAI":      ["elise", "helloelise"],
    "Funnel":       ["knock.app", "knockcrm", "funnelleasing", "funnel.io",
                     "funnellease", "funnel-lease"],
    "Quext":        ["quext.io", "quextai"],
    "Zuma":         ["zumaai", "zumapets"],
    "Rentgrata":    ["rentgrata"],
    "Anyone Home":  ["anyonehome"],
    "Lease Hawk":   ["leasehawk"],
    "Nurture Boss": ["nurtureboss"],
}

# Ordered from most to least specific
CHAT_WIDGET_SELECTORS = [
    # Funnel / Knock specific
    "[data-testid='chat-button']",
    "[data-testid='chatbot-button']",
    "[class*='ChatButton']",
    "[class*='chat-launcher']",
    "[class*='chatLauncher']",
    "[id*='chat-launcher']",
    "[class*='FunnelChat']",
    "[class*='funnel-chat']",
    "button[class*='launcher']",
    # Generic chat triggers
    "button[class*='chat']",
    "button[class*='Chat']",
    "div[class*='chat-button']",
    "div[class*='chatbot']",
    "div[class*='chat-widget']",
    "[aria-label*='chat' i]",
    "[aria-label*='message' i]",
    "[title*='chat' i]",
    # Iframe-based widgets
    "iframe[src*='funnel']",
    "iframe[src*='knock']",
    "iframe[src*='elise']",
    "iframe[src*='chat']",
    # Common IDs
    "[id*='chat-widget']",
    "[id*='chatWidget']",
    "[id*='chat-button']",
    # Fixed position elements (chat bubbles are almost always fixed)
    "div[style*='position: fixed']",
    "div[style*='position:fixed']",
]

CHAT_INPUT_SELECTORS = [
    "textarea[placeholder*='message' i]",
    "textarea[placeholder*='type' i]",
    "textarea[placeholder*='Ask' i]",
    "input[placeholder*='message' i]",
    "input[placeholder*='type' i]",
    "input[placeholder*='Ask' i]",
    "div[contenteditable='true']",
    "[data-testid*='input']",
    "[data-testid*='message']",
    "textarea",
    "input[type='text']",
]

SEND_BUTTON_SELECTORS = [
    "button[type='submit']",
    "button[aria-label*='send' i]",
    "button[class*='send']",
    "button[class*='Send']",
    "[data-testid*='send']",
    "button[class*='submit']",
]


class BrowserAgent:
    def __init__(self, headless: bool = True, slow_mo: int = 150):
        self.headless = headless
        self.slow_mo = slow_mo
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self._active_frame = None
        logger.info(f"BrowserAgent initialized (headless={headless}, slow_mo={slow_mo}ms)")

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1440,900",
                "--disable-infobars",
                "--disable-extensions",
                "--disable-gpu",
                "--start-maximized",
                "--ignore-certificate-errors",
                "--allow-running-insecure-content",
                # Make it look like a real user profile
            ]
        )
        logger.info("Browser launched")

    async def stop(self):
        if self.page:
            await self.page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser stopped")

    async def new_page(self) -> Page:
        context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
            color_scheme="light",
            device_scale_factor=1,
            has_touch=False,
            java_script_enabled=True,
            permissions=["geolocation"],
            geolocation={"latitude": 29.7604, "longitude": -95.3698},  # Houston
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        await context.add_init_script("""
            // Remove webdriver flag
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Realistic plugin list
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    return [
                        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
                        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                        {name: 'Native Client', filename: 'internal-nacl-plugin'},
                    ];
                }
            });
            // Realistic language settings
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            // Realistic hardware concurrency
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            // Realistic device memory
            Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
            // Realistic screen properties
            Object.defineProperty(screen, 'colorDepth', {get: () => 24});
            Object.defineProperty(screen, 'pixelDepth', {get: () => 24});
        """)
        self.page = await context.new_page()
        return self.page

    async def navigate_to_property(self, url: str) -> bool:
        logger.info(f"Navigating to {url}")
        try:
            await self.page.goto(url, wait_until="networkidle", timeout=45000)
            await self._human_pause(3, 5)
            # Scroll to trigger lazy-loaded widgets
            await self.page.evaluate("window.scrollBy(0, 400)")
            await self._human_pause(1, 2)
            await self.page.evaluate("window.scrollTo(0, 0)")
            await self._human_pause(1, 2)
            logger.success(f"Page loaded: {url}")
            return True
        except PlaywrightTimeout:
            logger.error(f"Timeout loading {url}")
            return False
        except Exception as e:
            logger.error(f"Navigation error: {e}")
            return False

    async def detect_chatbot_platform(self) -> str:
        try:
            content = await self.page.content()
            content_lower = content.lower()
            for platform, signals in CHATBOT_PLATFORMS.items():
                if any(signal in content_lower for signal in signals):
                    logger.info(f"Detected platform: {platform}")
                    return platform
            for frame in self.page.frames:
                frame_url = frame.url.lower()
                for platform, signals in CHATBOT_PLATFORMS.items():
                    if any(signal in frame_url for signal in signals):
                        logger.info(f"Detected platform via iframe: {platform}")
                        return platform
            logger.info("Platform not identified — unknown")
            return "Unknown"
        except Exception as e:
            logger.warning(f"Platform detection failed: {e}")
            return "Unknown"

    async def find_and_open_chat(self) -> bool:
        """
        Multi-strategy chat widget finder.
        Tries standard selectors, then shadow DOM, then iframe search.
        """
        logger.info("Searching for chat widget...")

        # Wait longer for JS-heavy pages to fully render widgets
        await self._human_pause(3, 5)

        # Strategy 1: Standard selectors
        for selector in CHAT_WIDGET_SELECTORS:
            try:
                element = await self.page.wait_for_selector(
                    selector, timeout=2000, state="visible"
                )
                if element:
                    logger.info(f"Found widget: {selector}")
                    await self._human_pause(0.5, 1)
                    await element.click()
                    await self._human_pause(2, 4)
                    if await self._find_chat_input():
                        logger.success("Chat widget opened via standard selector")
                        return True
            except PlaywrightTimeout:
                continue
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue

        # Strategy 2: Shadow DOM pierce
        if await self._try_shadow_dom():
            return True

        # Strategy 3: Search all iframes
        if await self._try_iframe_chat():
            return True

        # Strategy 4: JavaScript evaluation — find fixed-position buttons
        if await self._try_js_find_chat():
            return True

        logger.warning("Could not locate chat widget")
        return False

    async def _try_shadow_dom(self) -> bool:
        """Pierce shadow DOM to find chat widgets — common with Funnel."""
        try:
            result = await self.page.evaluate("""
                () => {
                    function findInShadow(root, selector) {
                        const el = root.querySelector(selector);
                        if (el) return el;
                        const shadows = Array.from(root.querySelectorAll('*'))
                            .filter(e => e.shadowRoot)
                            .map(e => e.shadowRoot);
                        for (const shadow of shadows) {
                            const found = findInShadow(shadow, selector);
                            if (found) return found;
                        }
                        return null;
                    }
                    const triggers = [
                        '[class*="chat"]', '[class*="Chat"]',
                        '[aria-label*="chat" i]', 'button[class*="launch"]',
                        '[class*="messenger"]', '[class*="widget"]'
                    ];
                    for (const sel of triggers) {
                        const el = findInShadow(document, sel);
                        if (el && el.offsetParent !== null) {
                            el.click();
                            return sel;
                        }
                    }
                    return null;
                }
            """)
            if result:
                logger.info(f"Opened chat via shadow DOM: {result}")
                await self._human_pause(2, 3)
                if await self._find_chat_input(timeout=5000):
                    logger.success("Chat opened via shadow DOM")
                    return True
        except Exception as e:
            logger.debug(f"Shadow DOM search failed: {e}")
        return False

    async def _try_iframe_chat(self) -> bool:
        """Search all iframes for chat input fields."""
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                for selector in CHAT_INPUT_SELECTORS:
                    el = await frame.query_selector(selector)
                    if el:
                        logger.info(f"Found chat in iframe: {frame.url[:60]}")
                        self._active_frame = frame
                        return True
            except Exception:
                continue
        return False

    async def _try_js_find_chat(self) -> bool:
        """
        Last resort: find all fixed-position elements and try clicking
        ones that look like chat launchers.
        """
        try:
            clicked = await self.page.evaluate("""
                () => {
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        const style = window.getComputedStyle(el);
                        const text = (el.textContent || '').toLowerCase();
                        const cls = (el.className || '').toLowerCase();
                        const isFixed = style.position === 'fixed';
                        const isBottom = parseInt(style.bottom) < 150;
                        const looksLikeChat = (
                            cls.includes('chat') || cls.includes('messenger') ||
                            cls.includes('support') || cls.includes('help') ||
                            el.tagName === 'BUTTON'
                        );
                        if (isFixed && isBottom && looksLikeChat) {
                            el.click();
                            return el.className;
                        }
                    }
                    return null;
                }
            """)
            if clicked:
                logger.info(f"Clicked fixed element via JS: {clicked[:60]}")
                await self._human_pause(2, 3)
                if await self._find_chat_input(timeout=5000):
                    logger.success("Chat opened via JS fixed element search")
                    return True
        except Exception as e:
            logger.debug(f"JS fixed element search failed: {e}")
        return False

    async def _find_chat_input(self, timeout: int = 5000) -> bool:
        frame = self._active_frame or self.page
        for selector in CHAT_INPUT_SELECTORS:
            try:
                el = await frame.wait_for_selector(
                    selector, timeout=timeout, state="visible"
                )
                if el:
                    return True
            except PlaywrightTimeout:
                continue
        return False

    async def send_message(self, text: str) -> bool:
        frame = self._active_frame or self.page
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
            logger.error("Could not find chat input")
            return False

        try:
            await input_el.click()
            await self._human_pause(0.3, 0.7)
            await input_el.type(text, delay=self._human_typing_delay())
            await self._human_pause(0.5, 1.5)
            await self.page.keyboard.press("Enter")
            await self._human_pause(0.5, 1)
            logger.debug(f"Sent: {text[:60]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    async def wait_for_response(
        self,
        previous_message_count: int,
        timeout: int = 45000
    ) -> Optional[str]:
        logger.debug("Waiting for response...")
        start = time.time()
        last_text = ""

        while (time.time() - start) * 1000 < timeout:
            try:
                frame = self._active_frame or self.page
                current_text = await frame.evaluate("""
                    () => {
                        const selectors = [
                            '[class*="message"]', '[class*="chat"]',
                            '[class*="conversation"]', '[role="log"]',
                            '[aria-live]', '[class*="transcript"]'
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText.length > 10) return el.innerText;
                        }
                        return document.body.innerText;
                    }
                """)

                if len(current_text) > len(last_text) + 10:
                    await self._human_pause(1.5, 2.5)
                    check_text = await frame.evaluate(
                        "() => document.body.innerText"
                    )
                    if len(check_text) >= len(current_text):
                        new_content = self._extract_latest_response(current_text)
                        logger.debug(f"Response: {new_content[:80]}...")
                        return new_content
                    last_text = current_text

            except Exception as e:
                logger.debug(f"Polling error: {e}")

            await asyncio.sleep(1)

        logger.warning("Timeout waiting for response")
        return None

    def _extract_latest_response(self, full_text: str) -> str:
        lines = [l.strip() for l in full_text.split('\n') if l.strip()]
        return " ".join(lines[-3:]) if lines else full_text

    async def run_engagement(
        self,
        engagement_id: str,
        property_url: str,
        persona: dict,
        orchestrator,
    ) -> dict:
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

            loaded = await self.navigate_to_property(property_url)
            if not loaded:
                result["error"] = "Page failed to load"
                return result

            result["platform"] = await self.detect_chatbot_platform()

            chat_opened = await self.find_and_open_chat()
            if not chat_opened:
                result["error"] = "Could not locate chat widget"
                return result

            with get_db() as db:
                engagement = db.query(Engagement).filter_by(id=engagement_id).first()
                if engagement:
                    engagement.status = EngagementStatus.IN_PROGRESS
                    engagement.started_at = datetime.utcnow()
                    engagement.chatbot_platform = result["platform"]

            stages = [
                ConversationStage.DISCOVERY,
                ConversationStage.NUANCE,
                ConversationStage.HANDOFF_TRIGGER,
            ]

            conversation_history = []
            message_count = 0

            for stage in stages:
                logger.info(f"--- Stage: {stage.value} ---")

                last_property_msg = (
                    conversation_history[-1]["content"]
                    if conversation_history and conversation_history[-1]["sender"] != "persona"
                    else "Hello! Welcome. How can I help you today?"
                )

                persona_msg = orchestrator.generate_persona_message(
                    persona=persona,
                    stage=stage,
                    conversation_history=conversation_history,
                    last_property_message=last_property_msg,
                    channel=ChannelType.WEBCHAT
                )

                sent = await self.send_message(persona_msg)
                if not sent:
                    logger.warning(f"Failed to send at stage {stage.value}")
                    break

                sent_at = datetime.utcnow()
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
                    engagement_id, MessageSender.PERSONA,
                    ChannelType.WEBCHAT, stage, persona_msg, sent_at
                )

                message_count += 1
                response = await self.wait_for_response(message_count, timeout=45000)

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
                        engagement_id, MessageSender.AI_BOT,
                        ChannelType.WEBCHAT, stage, response, received_at
                    )
                    message_count += 1
                else:
                    logger.warning(f"No response at stage {stage.value}")

                await self._human_pause(3, 7)

            result["handoff_triggered"] = True

            with get_db() as db:
                engagement = db.query(Engagement).filter_by(id=engagement_id).first()
                if engagement:
                    engagement.status = EngagementStatus.AWAITING_HUMAN
                    engagement.handoff_triggered_at = datetime.utcnow()

            result["success"] = True
            logger.success(f"Webchat phase complete for {engagement_id}")

        except Exception as e:
            logger.error(f"Engagement failed: {e}")
            result["error"] = str(e)
            with get_db() as db:
                engagement = db.query(Engagement).filter_by(id=engagement_id).first()
                if engagement:
                    engagement.status = EngagementStatus.FAILED
        finally:
            await self.stop()

        return result

    def _save_message(self, engagement_id, sender, channel, stage, content, sent_at):
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
        import random
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    def _human_typing_delay(self) -> int:
        import random
        return random.randint(50, 120)


# Patch: override find_and_open_chat to use Funnel handler when detected
_original_run_engagement = BrowserAgent.run_engagement



# Platform-aware run_engagement
async def _patched_run_engagement(
    self,
    engagement_id: str,
    property_url: str,
    persona: dict,
    orchestrator,
) -> dict:
    from database.models import (
        EngagementStatus, MessageSender, ConversationStage,
        ChannelType, Message, Engagement
    )
    from database.connection import get_db
    from datetime import datetime

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

    def save_message(engagement_id, sender, channel, stage, content, sent_at):
        """Fresh DB session per message to avoid scope issues."""
        try:
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
                db.flush()
            logger.debug(f"Saved message: {sender} | {stage}")
        except Exception as e:
            logger.error(f"Failed to save message: {e}")

    try:
        await self.start()
        await self.new_page()

        loaded = await self.navigate_to_property(property_url)
        if not loaded:
            result["error"] = "Page failed to load"
            return result

        result["platform"] = await self.detect_chatbot_platform()

        # Update engagement started
        with get_db() as db:
            eng = db.query(Engagement).filter_by(id=engagement_id).first()
            if eng:
                eng.status = EngagementStatus.IN_PROGRESS
                eng.started_at = datetime.utcnow()
                eng.chatbot_platform = result["platform"]

        if result["platform"] == "Funnel":
            from agent.funnel_handler import run_funnel_engagement
            transcript = await run_funnel_engagement(
                page=self.page,
                engagement_id=engagement_id,
                persona=persona,
                orchestrator=orchestrator,
                db_save_fn=save_message,
            )
        else:
            chat_opened = await self.find_and_open_chat()
            if not chat_opened:
                result["error"] = "Could not locate chat widget"
                return result

            transcript = []
            conversation_history = []
            stages = [
                ConversationStage.DISCOVERY,
                ConversationStage.NUANCE,
                ConversationStage.HANDOFF_TRIGGER,
            ]
            for stage in stages:
                last_msg = (
                    conversation_history[-1]["content"]
                    if conversation_history and conversation_history[-1]["sender"] != "persona"
                    else "Hello! How can I help you?"
                )
                persona_msg = orchestrator.generate_persona_message(
                    persona=persona, stage=stage,
                    conversation_history=conversation_history,
                    last_property_message=last_msg,
                    channel=ChannelType.WEBCHAT,
                )
                sent = await self.send_message(persona_msg)
                if not sent:
                    break
                sent_at = datetime.utcnow()
                e = {"sender": "persona", "channel": "webchat",
                     "stage": stage.value, "content": persona_msg,
                     "sent_at": sent_at.isoformat()}
                conversation_history.append(e)
                transcript.append(e)
                save_message(engagement_id, MessageSender.PERSONA,
                             ChannelType.WEBCHAT, stage, persona_msg, sent_at)

                response = await self.wait_for_response(len(transcript), 45000)
                if response:
                    recv = datetime.utcnow()
                    r = {"sender": "ai_bot", "channel": "webchat",
                         "stage": stage.value, "content": response,
                         "sent_at": recv.isoformat()}
                    conversation_history.append(r)
                    transcript.append(r)
                    save_message(engagement_id, MessageSender.AI_BOT,
                                 ChannelType.WEBCHAT, stage, response, recv)

                await self._human_pause(3, 6)

        result["transcript"] = transcript

        if transcript:
            result["success"] = True
            result["handoff_triggered"] = True
            with get_db() as db:
                eng = db.query(Engagement).filter_by(id=engagement_id).first()
                if eng:
                    eng.status = EngagementStatus.AWAITING_HUMAN
                    eng.handoff_triggered_at = datetime.utcnow()
            logger.success(
                f"Webchat complete | Platform: {result['platform']} "
                f"| Messages: {len(transcript)}"
            )
        else:
            result["error"] = "Could not open Funnel chat" \
                if result["platform"] == "Funnel" else "No messages captured"
            with get_db() as db:
                eng = db.query(Engagement).filter_by(id=engagement_id).first()
                if eng:
                    eng.status = EngagementStatus.FAILED

    except Exception as e:
        logger.error(f"Engagement failed: {e}")
        result["error"] = str(e)
        with get_db() as db:
            eng = db.query(Engagement).filter_by(id=engagement_id).first()
            if eng:
                eng.status = EngagementStatus.FAILED
    finally:
        await self.stop()

    return result

BrowserAgent.run_engagement = _patched_run_engagement
