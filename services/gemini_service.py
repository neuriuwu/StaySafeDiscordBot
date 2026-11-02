"""Gemini chat integration with bounded, per-session in-memory history."""

import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Final

from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)
MODEL_NAME: Final[str] = "gemini-3.5-flash-lite"
MAX_HISTORY_MESSAGES: Final[int] = 24
MAX_SESSIONS: Final[int] = 500
MAX_OUTPUT_TOKENS: Final[int] = 500


def clean_model_text(text: str) -> str:
    """Remove HTML/XML tags from model output while keeping surrounding text."""
    return re.sub(r"<[^>]+>", "", text).strip()


MODE_INSTRUCTIONS: Final[dict[str, str]] = {
    "casual": (
        "keep it chill and natural. just match their energy, be curious, "
        "and don't start dropping unsolicited advice unless they actually ask for it."
    ),
    "vent": (
        "listening-first mode. validate their feelings straight up without any "
        "toxic positivity or rushing to fix things. just let them vent, reflect back "
        "what they're saying, and keep it grounded. validate them like a real friend. "
        "nah that's actually so annoying" or "wtf that sucks."
    ),
    "advice": (
        "they want actual advice. give them a clean, low-key structured breakdown "
        "with realistic next steps. keep it practical, include pros/cons if it fits, "
        "and keep the older sibling energy."
        "older sibling energy, keep it practical and low-key."
    ),
    "identity": (
        "zero judgment zone. completely warm and identity-affirming. respect "
        "their pronouns and pacing completely—never pressure them toward a label "
        "or conclusion, just let them figure it out at their own speed."
        "zero weirdness, totally chill, completely safe and affirming without making it a big theatrical production."
    ),
}

BASE_SYSTEM_INSTRUCTION: Final[str] = """\
You are an 18-year-old Gen Z Discord companion. You talk like a real human teenager typing in a chatroom, NOT an AI trying to sound hip.

CORE RULES & VIBE:
- NEVER say things like "hey bestie 👋" or use corny corporate emoji waves. That is strictly banned.
- Type in lowercase. Use minimal punctuation. 
- Talk like you're actually hanging out in a vc or text channel—casual, dry, deadpan, or genuinely supportive when someone's venting.
- Use slang naturally (fr, rn, lowkey, real, valid, literals) or just talk normally. Don't force 5 slang words into every single sentence.
- Ban list: "delve", "navigate", "multifaceted", "bestie", "foster", "tapestry", "journey".
- If they say hi, just say something normal like "yo" or "what's good" or "hey".
Never act like an AI, never drop robotic disclaimers, and definitely never sound like a brand twitter account.
- Output plain text and standard Discord markdown only. Never output HTML tags, XML tags, or paragraph wrappers such as <p>...</p>.

Current conversation mode: {mode_instruction}
"""


class GeminiServiceError(Exception):
    """A safe-to-display failure from the Gemini service."""

    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass
class _Conversation:
    """One user's bounded chat history and request lock."""

    history: deque[types.Content] = field(
        default_factory=lambda: deque(maxlen=MAX_HISTORY_MESSAGES)
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class GeminiChatService:
    """Generate Gemini replies using isolated, bounded in-memory histories."""

    def __init__(self, api_key: str, model: str = MODEL_NAME) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._sessions: OrderedDict[str, _Conversation] = OrderedDict()

    def _get_conversation(self, session_key: str) -> _Conversation:
        conversation = self._sessions.get(session_key)
        if conversation is not None:
            self._sessions.move_to_end(session_key)
            return conversation

        conversation = _Conversation()
        self._sessions[session_key] = conversation
        while len(self._sessions) > MAX_SESSIONS:
            oldest_unlocked = next(
                (
                    key
                    for key, candidate in self._sessions.items()
                    if key != session_key and not candidate.lock.locked()
                ),
                None,
            )
            if oldest_unlocked is None:
                break
            del self._sessions[oldest_unlocked]
        return conversation

    async def reply(self, session_key: str, message: str, mode: str = "casual") -> str:
        """Generate a text response and retain successful turns for the same user."""
        return await self.reply_multimodal(session_key, [message], mode)

    async def reply_multimodal(
        self,
        session_key: str,
        parts: list[types.Part | str],
        mode: str = "casual",
    ) -> str:
        """Generate a response from text and image parts in one user turn."""
        if not parts:
            raise ValueError("At least one text or image part is required.")

        normalized_parts = [
            types.Part.from_text(text=part) if isinstance(part, str) else part
            for part in parts
        ]
        user_content = types.Content(role="user", parts=normalized_parts)

        normalized_mode = mode if mode in MODE_INSTRUCTIONS else "casual"
        conversation = self._get_conversation(session_key)
        async with conversation.lock:
            input_contents = [*conversation.history, user_content]
            config = types.GenerateContentConfig(
                system_instruction=BASE_SYSTEM_INSTRUCTION.format(
                    mode_instruction=MODE_INSTRUCTIONS[normalized_mode]
                ),
                thinking_config=types.ThinkingConfig(thinking_level="low"),
                max_output_tokens=MAX_OUTPUT_TOKENS,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            )
            started_at = time.perf_counter()
            logger.info(
                "Gemini request started model=%s history_messages=%d input_parts=%d",
                self._model,
                len(conversation.history),
                len(normalized_parts),
            )
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=input_contents,
                    config=config,
                )
            except errors.APIError as exc:
                logger.exception(
                    "Gemini API request failed after %.2f seconds.",
                    time.perf_counter() - started_at,
                )
                raise GeminiServiceError(
                    "Gemini API request failed.",
                    rate_limited=exc.code == 429,
                ) from exc
            finally:
                logger.info(
                    "Gemini request finished model=%s elapsed_seconds=%.2f",
                    self._model,
                    time.perf_counter() - started_at,
                )

            raw_answer = response.text
            if not raw_answer or not raw_answer.strip():
                logger.warning("Gemini returned an empty response.")
                raise GeminiServiceError("Gemini returned an empty response.")
            answer = clean_model_text(raw_answer)
            if not answer:
                logger.warning("Gemini response contained no text after HTML/XML cleanup.")
                raise GeminiServiceError("Gemini returned no usable text.")

            model_content = types.Content(
                role="model",
                parts=[types.Part.from_text(text=answer)],
            )
            conversation.history.extend((user_content, model_content))
            return answer

    async def reset(self, session_key: str) -> None:
        """Clear history for one user and one Discord channel or thread."""
        conversation = self._sessions.get(session_key)
        if conversation is not None:
            async with conversation.lock:
                conversation.history.clear()
