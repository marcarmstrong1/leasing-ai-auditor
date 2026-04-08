"""
Funnel chat handler.

Funnel's actual sequence on Camden:
1. Iframe exists with about:blank
2. Click main launcher -> iframe gets injected with a pop-up bubble
3. Click the pop-up bubble -> full chat UI with input appears
"""

import asyncio
import time
from typing import Optional
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout


FUNNEL_IFRAME_SELECTORS = [
    "iframe[class*='funnel']",
    "iframe[data-testid='iframe']",
    "iframe[title='Chatbot']",
    "iframe[title='Chat']",
    "[id*='funnel-chat'] iframe",
    "[id*='funnel'] iframe",
]

FUNNEL_INPUT_SELECTORS = [
    "textarea",
    "input[type='text']",
    "[contenteditable='true']",
    "[placeholder*='message' i]",
    "[placeholder*='type' i]",
    "[placeholder*='Ask' i]",
    "[data-testid*='input']",
]

# Selectors for the Funnel pop-up greeting bubble
FUNNEL_POPUP_SELECTORS = [
    "[data-testid='pop-up-message']",
    "[aria-label*='availability' i]",
    "[aria-label*='tour' i]",
    "[aria-label*='chat' i]",
    "[class*='pop-up']",
    "[class*='popup']",
    "[class*='greeting']",
    "[class*='bubble']",
    "[tabindex='0']",  # Funnel makes the bubble focusable
]


async def open_funnel_chat(page: Page) -> Optional[object]:
    logger.info("Using Funnel iframe handler...")

    # Step 1: Get iframe frame reference
    iframe_frame = None
    for selector in FUNNEL_IFRAME_SELECTORS:
        try:
            iframe_el = await page.wait_for_selector(
                selector, timeout=8000, state="attached"
            )
            if iframe_el:
                frame = await iframe_el.content_frame()
                if frame:
                    logger.info(f"Got iframe reference (url: {frame.url})")
                    iframe_frame = frame
                    break
        except PlaywrightTimeout:
            continue

    if not iframe_frame:
        logger.warning("Could not get Funnel iframe reference")
        return None

    # Step 2: Click the main page launcher
    logger.info("Clicking main page launcher...")
    await page.evaluate("""
        () => {
            const selectors = [
                '[id*="funnel"] button',
                '[class*="funnel"] button',
                '[id*="funnel-chat-container"] button',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    return sel;
                }
            }
        }
    """)
    await asyncio.sleep(2)

    # Step 3: Wait for iframe content to appear
    logger.info("Waiting for iframe content...")
    for attempt in range(15):
        try:
            html = await iframe_frame.evaluate(
                "() => document.body ? document.body.innerHTML : ''"
            )
            if html and len(html) > 50:
                logger.info(f"Iframe has content (attempt {attempt+1})")
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    # Step 4: Click the pop-up greeting bubble to open full chat
    logger.info("Clicking Funnel pop-up bubble to open chat...")
    bubble_clicked = False
    for selector in FUNNEL_POPUP_SELECTORS:
        try:
            el = await iframe_frame.wait_for_selector(
                selector, timeout=3000, state="visible"
            )
            if el:
                logger.info(f"Clicking pop-up: {selector}")
                await el.click()
                await asyncio.sleep(2)
                bubble_clicked = True
                break
        except PlaywrightTimeout:
            continue
        except Exception as e:
            logger.debug(f"Pop-up selector {selector} failed: {e}")
            continue

    if not bubble_clicked:
        # Try JS click on anything interactive in the iframe
        logger.info("Trying JS click on iframe interactive elements...")
        await iframe_frame.evaluate("""
            () => {
                const candidates = document.querySelectorAll(
                    '[tabindex], button, [role="button"], [onclick]'
                );
                for (const el of candidates) {
                    if (el.offsetParent !== null) {
                        el.click();
                        return;
                    }
                }
            }
        """)
        await asyncio.sleep(2)

    # Step 5: Look for the actual chat input
    logger.info("Looking for chat input after bubble click...")
    for selector in FUNNEL_INPUT_SELECTORS:
        try:
            el = await iframe_frame.wait_for_selector(
                selector, timeout=8000, state="visible"
            )
            if el:
                logger.success(f"Funnel chat open — input found: {selector}")
                return iframe_frame
        except PlaywrightTimeout:
            continue

    # Debug: log what's in the iframe now
    try:
        content = await iframe_frame.evaluate("() => document.body.innerHTML")
        logger.info(f"Iframe after bubble click: {content[:600]}")
    except Exception:
        pass

    logger.warning("Could not open Funnel chat input after bubble click")
    return None


async def send_funnel_message(frame, text: str) -> bool:
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
        logger.error("No input field in Funnel iframe")
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


async def wait_funnel_response(frame, timeout: int = 45000) -> Optional[str]:
    start = time.time()
    last_text = await frame.evaluate("() => document.body.innerText")

    while (time.time() - start) * 1000 < timeout:
        try:
            current = await frame.evaluate("() => document.body.innerText")
            if len(current) > len(last_text) + 15:
                await asyncio.sleep(2)
                stable = await frame.evaluate("() => document.body.innerText")
                if len(stable) >= len(current):
                    lines = [l.strip() for l in stable.split('\n') if l.strip()]
                    return " ".join(lines[-4:]) if lines else stable
                last_text = current
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
    """
    Runs the full conversation inside the Funnel iframe.
    Returns transcript list.
    """
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
            else "Hello! Welcome, how can I help you today?"
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

        # Save immediately with fresh session
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
            logger.info(f"Stage {stage.value} complete — got response")
        else:
            logger.warning(f"No response at stage {stage.value}")

        import asyncio, random
        await asyncio.sleep(random.uniform(3, 6))

    return transcript
