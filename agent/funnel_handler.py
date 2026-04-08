"""
Funnel chat handler — built from live DOM inspection of Camden Yorktown.

Structure:
- Container: div#funnel-chat-container-{uuid} (dynamically created on click)
- Iframe: iframe.funnel-chat-iframe (inside the container)
- Messages: div[data-testid="messages"] inside the iframe
- Each message: div[data-testid="message"] with aria-label containing full text
- Message text: div.dPKQZ inside each message
- Input: textarea or input inside the iframe
"""

import asyncio
import time
from typing import Optional
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout


FUNNEL_INPUT_SELECTORS = [
    "textarea",
    "input[type='text']",
    "[placeholder*='message' i]",
    "[placeholder*='type' i]",
    "[placeholder*='Ask' i]",
    "[data-testid*='input']",
]


async def open_funnel_chat(page: Page) -> Optional[object]:
    """
    Waits for the Funnel launcher button, clicks it, waits for the
    iframe to be injected, then returns the frame ready for input.
    """
    logger.info("Opening Funnel chat...")
    # Re-apply stealth to this page context just in case
    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
        logger.debug("Stealth applied to page")
    except Exception as e:
        logger.debug(f"Stealth patch skipped: {e}")

    # Step 1: Click the launcher — Funnel injects the container+iframe on click
    # The container div doesn't exist yet, so we click whatever triggers it
    launcher_clicked = await page.evaluate("""
        () => {
            // Try the quES0 div which is the launcher on Camden
            const launchers = [
                '.quES0',
                '[class*="launcher"]',
                '[aria-label*="chat" i]',
                '[aria-label*="Chat"]',
                'button[class*="chat"]',
            ];
            for (const sel of launchers) {
                const el = document.querySelector(sel);
                if (el) {
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    return sel;
                }
            }
            return null;
        }
    """)
    logger.info(f"Launcher clicked: {launcher_clicked}")
    await asyncio.sleep(2)

    # Step 2: Wait for Funnel to inject the iframe into the DOM
    logger.info("Waiting for funnel-chat-iframe to appear...")
    iframe_el = None
    for attempt in range(20):
        try:
            iframe_el = await page.wait_for_selector(
                "iframe.funnel-chat-iframe",
                timeout=2000,
                state="attached"
            )
            if iframe_el:
                logger.info(f"iframe.funnel-chat-iframe found (attempt {attempt+1})")
                break
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(1)

    if not iframe_el:
        logger.warning("funnel-chat-iframe never appeared in DOM")
        return None

    # Step 3: Get the frame context
    frame = await iframe_el.content_frame()
    if not frame:
        logger.warning("Could not get content frame")
        return None

    logger.info(f"Frame URL: {frame.url}")

    # Step 4: Wait for chat content to load inside iframe
    logger.info("Waiting for chat content inside iframe...")
    for attempt in range(20):
        try:
            el = await frame.wait_for_selector(
                "[data-testid='messages'], [data-testid='chat']",
                timeout=2000,
                state="attached"
            )
            if el:
                logger.info(f"Chat container found (attempt {attempt+1})")
                break
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(1)

    # Step 5: Check if there's a pop-up greeting bubble to click through
    try:
        popup = await frame.wait_for_selector(
            "[data-testid='pop-up-message'], [aria-label*='availability' i], [aria-label*='tour' i]",
            timeout=3000,
            state="visible"
        )
        if popup:
            logger.info("Clicking pop-up greeting bubble...")
            await popup.click()
            await asyncio.sleep(2)
    except PlaywrightTimeout:
        logger.info("No pop-up bubble — chat already open")

    # Step 6: Find the input field
    for selector in FUNNEL_INPUT_SELECTORS:
        try:
            el = await frame.wait_for_selector(
                selector, timeout=5000, state="visible"
            )
            if el:
                logger.success(f"Funnel chat ready — input: {selector}")
                return frame
        except PlaywrightTimeout:
            continue

    # Log what's in the iframe for debugging
    try:
        content = await frame.evaluate("() => document.body.innerHTML")
        logger.info(f"Iframe content at failure: {content[:500]}")
    except Exception:
        pass

    logger.warning("Could not find input field in Funnel iframe")
    return None


async def send_funnel_message(frame, text: str) -> bool:
    """Send a message in the Funnel chat."""
    input_el = None
    for selector in FUNNEL_INPUT_SELECTORS:
        try:
            input_el = await frame.wait_for_selector(
                selector, timeout=5000, state="visible"
            )
            if input_el:
                break
        except PlaywrightTimeout:
            continue

    if not input_el:
        logger.error("No input field found")
        return False

    try:
        await input_el.click()
        await asyncio.sleep(0.5)
        await input_el.fill("")
        await input_el.type(text, delay=80)
        await asyncio.sleep(0.8)
        await input_el.press("Enter")
        await asyncio.sleep(0.5)
        logger.debug(f"Sent: {text[:60]}")
        return True
    except Exception as e:
        logger.error(f"Send failed: {e}")
        return False


async def get_latest_bot_message(frame) -> Optional[str]:
    """
    Extracts the latest bot message using the exact DOM structure
    found via live inspection: div[data-testid='message'] with aria-label.
    """
    try:
        messages = await frame.evaluate("""
            () => {
                // Only get bot messages using confirmed aria-label prefix
                const msgs = document.querySelectorAll('[aria-label^="Chatbot says"]');
                const results = [];
                msgs.forEach(msg => {
                    const label = msg.getAttribute('aria-label') || '';
                    const text = label.replace(/^Chatbot says\s*/u, '').trim();
                    if (text) results.push({label: text, text: text});
                });
                return results;
            }
        """)

        if not messages:
            return None

        # Get the last bot message (skip persona messages)
        for msg in reversed(messages):
            label = msg.get('label', '')
            text = msg.get('text', '')
            content = label or text
            # Filter out the generic greeting duplicates
            if content and len(content) > 10:
                logger.debug(f"Latest bot message: {content[:80]}")
                return content

        return None
    except Exception as e:
        logger.debug(f"get_latest_bot_message error: {e}")
        return None


async def wait_funnel_response(frame, timeout: int = 45000) -> Optional[str]:
    """
    Waits for a new BOT message using aria-label starting with 'Chatbot says'.
    Counts only bot messages to avoid false positives from user messages.
    """
    start = time.time()

    # Count current BOT messages as baseline
    try:
        initial_count = await frame.evaluate("""
            () => document.querySelectorAll('[aria-label^="Chatbot says"]').length
        """)
    except Exception:
        initial_count = 0

    logger.debug(f"Waiting for bot response (baseline: {initial_count} bot messages)")

    while (time.time() - start) * 1000 < timeout:
        try:
            current_count = await frame.evaluate("""
                () => document.querySelectorAll('[aria-label^="Chatbot says"]').length
            """)

            if current_count > initial_count:
                # New bot message appeared — wait for typing to finish
                await asyncio.sleep(2)

                stable_count = await frame.evaluate("""
                    () => document.querySelectorAll('[aria-label^="Chatbot says"]').length
                """)

                if stable_count >= current_count:
                    # Get the latest bot message text from aria-label
                    response = await frame.evaluate("""
                        () => {
                            const msgs = document.querySelectorAll('[aria-label^="Chatbot says"]');
                            if (!msgs.length) return null;
                            const last = msgs[msgs.length - 1];
                            // Strip the "Chatbot says " prefix
                            const label = last.getAttribute('aria-label') || '';
                            return label.replace(/^Chatbot says\s*/u, '').trim();
                        }
                    """)
                    if response:
                        logger.debug(f"Response captured: {response[:80]}")
                        return response
                    initial_count = current_count

        except Exception as e:
            logger.debug(f"Poll error: {e}")

        await asyncio.sleep(1)

    logger.warning("Timeout waiting for Funnel response")
    return None


async def run_funnel_engagement(
    page,
    engagement_id: str,
    persona: dict,
    orchestrator,
    db_save_fn,
) -> list:
    """Full conversation loop inside Funnel chat."""
    from database.models import (
        MessageSender, ConversationStage, ChannelType
    )
    from datetime import datetime

    frame = await open_funnel_chat(page)
    if not frame:
        return []

    stages = [
        ConversationStage.DISCOVERY,
        ConversationStage.NUANCE,
        ConversationStage.HANDOFF_TRIGGER,
    ]

    conversation_history = []
    transcript = []

    for stage in stages:
        logger.info(f"--- Stage: {stage.value} ---")

        last_property_msg = (
            conversation_history[-1]["content"]
            if conversation_history and conversation_history[-1]["sender"] != "persona"
            else "Hi, I'm Birdie, Camden's Virtual Leasing Assistant. How can I help?"
        )

        persona_msg = orchestrator.generate_persona_message(
            persona=persona,
            stage=stage,
            conversation_history=conversation_history,
            last_property_message=last_property_msg,
            channel=ChannelType.WEBCHAT,
        )

        sent = await send_funnel_message(frame, persona_msg)
        if not sent:
            logger.warning(f"Send failed at stage {stage.value}")
            break

        sent_at = datetime.utcnow()
        entry = {
            "sender": "persona", "channel": "webchat",
            "stage": stage.value, "content": persona_msg,
            "sent_at": sent_at.isoformat(),
        }
        conversation_history.append(entry)
        transcript.append(entry)
        db_save_fn(
            engagement_id=engagement_id,
            sender=MessageSender.PERSONA,
            channel=ChannelType.WEBCHAT,
            stage=stage,
            content=persona_msg,
            sent_at=sent_at,
        )

        response = await wait_funnel_response(frame, timeout=45000)
        if response:
            received_at = datetime.utcnow()
            r_entry = {
                "sender": "ai_bot", "channel": "webchat",
                "stage": stage.value, "content": response,
                "sent_at": received_at.isoformat(),
            }
            conversation_history.append(r_entry)
            transcript.append(r_entry)
            db_save_fn(
                engagement_id=engagement_id,
                sender=MessageSender.AI_BOT,
                channel=ChannelType.WEBCHAT,
                stage=stage,
                content=response,
                sent_at=received_at,
            )
            logger.info(f"Stage {stage.value} complete — response: {response[:80]}")
        else:
            logger.warning(f"No response at stage {stage.value}")

        import random
        await asyncio.sleep(random.uniform(3, 6))

    return transcript
