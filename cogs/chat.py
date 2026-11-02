"""Slash commands and message handling for supportive conversations."""

import logging
import os
import re
from typing import Final

import discord
from discord import app_commands
from discord.ext import commands
from google.genai import types

from services.gemini_service import GeminiChatService, GeminiServiceError

logger = logging.getLogger(__name__)
DISCORD_MESSAGE_LIMIT: Final[int] = 2_000
PRIVATE_THREAD_PREFIX: Final[str] = "safe-chat-"


def _session_key(
    guild_id: int | None, channel_id: int, user_id: int
) -> str:
    """Keep every user's memory separate, including inside shared threads."""
    scope = str(guild_id) if guild_id is not None else "dm"
    return f"{scope}:{channel_id}:{user_id}"


def _split_message(text: str) -> list[str]:
    """Split long model output into messages Discord accepts."""
    remaining = text.strip()
    chunks: list[str] = []
    while len(remaining) > DISCORD_MESSAGE_LIMIT:
        split_at = remaining.rfind("\n", 0, DISCORD_MESSAGE_LIMIT + 1)
        if split_at < DISCORD_MESSAGE_LIMIT // 2:
            split_at = remaining.rfind(" ", 0, DISCORD_MESSAGE_LIMIT + 1)
        if split_at < DISCORD_MESSAGE_LIMIT // 2:
            split_at = DISCORD_MESSAGE_LIMIT
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or ["I couldn't create a response just now. Please try again."]


class ChatCog(commands.Cog):
    """Slash commands and natural-message handling for conversations."""

    def __init__(self, bot: commands.Bot, service: GeminiChatService) -> None:
        self.bot = bot
        self.service = service
        self.user_modes: dict[tuple[int, int], str] = {}

    async def _reply_to_interaction(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        """Send a deferred interaction response, splitting it to fit Discord."""
        for index, chunk in enumerate(_split_message(message)):
            if index == 0:
                await interaction.followup.send(chunk, ephemeral=ephemeral)
            elif interaction.channel is not None:
                await interaction.channel.send(chunk)

    async def _generate_reply(
        self,
        *,
        channel_id: int,
        guild_id: int | None,
        user_id: int,
        message: str,
        mode: str,
    ) -> str:
        return await self.service.reply(
            session_key=_session_key(guild_id, channel_id, user_id),
            message=message,
            mode=mode,
        )

    @app_commands.command(name="mode", description="Set your conversation mode in this channel.")
    @app_commands.describe(mode="Choose how you'd like the bot to respond.")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Casual", value="casual"),
            app_commands.Choice(name="Vent", value="vent"),
            app_commands.Choice(name="Advice", value="advice"),
            app_commands.Choice(name="Identity", value="identity"),
        ]
    )
    async def set_mode(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ) -> None:
        """Set a per-user, per-channel mode for natural message conversations."""
        self.user_modes[(interaction.channel_id, interaction.user.id)] = mode.value
        await interaction.response.send_message(
            f"got it — i'll use **{mode.name.lower()}** mode for you in this channel",
            ephemeral=True,
        )

    @app_commands.command(name="rant", description="Vent; I'll listen without rushing to fix things.")
    @app_commands.describe(message="What's weighing on you?")
    async def rant(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 2_000],
    ) -> None:
        """Respond in a listening-first, nonjudgmental mode."""
        await interaction.response.defer(thinking=True)
        try:
            answer = await self._generate_reply(
                channel_id=interaction.channel_id,
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                message=message,
                mode="vent",
            )
        except GeminiServiceError as exc:
            logger.exception("Gemini failed while handling /rant.")
            if exc.rate_limited:
                answer = "Gemini is rate-limiting requests right now. Please try again shortly."
            else:
                answer = "I couldn't reach the conversation service just now. Please try again shortly."
        await self._reply_to_interaction(interaction, answer)

    @app_commands.command(
        name="private",
        description="Create an invite-only private thread for your conversation.",
    )
    async def private(self, interaction: discord.Interaction) -> None:
        """Create a bot-owned private thread and invite only the command user."""
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this command in a server text channel to create a private thread.",
                ephemeral=True,
            )
            return

        bot_member = guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "I couldn't verify my permissions in this server. Please try again later.",
                ephemeral=True,
            )
            return
        permissions = channel.permissions_for(bot_member)
        required_permissions = (
            permissions.create_private_threads
            and permissions.send_messages_in_threads
            and permissions.manage_threads
        )
        if not required_permissions:
            await interaction.response.send_message(
                "I need Create Private Threads, Send Messages in Threads, and Manage Threads "
                "permissions in this channel to set that up.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        display_name = re.sub(r"[^a-zA-Z0-9-]", "-", interaction.user.display_name)
        thread_name = f"{PRIVATE_THREAD_PREFIX}{display_name}"[:100]
        thread: discord.Thread | None = None
        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1_440,
                reason="Private conversation requested through /private",
            )
            await thread.add_user(interaction.user)
            await thread.send(
                f"Hi {interaction.user.mention}. This is your private conversation thread. "
                "Just send me a message here whenever you'd like to talk. "
                "I'm an AI sounding board, not a therapist or mental health professional."
            )
        except discord.HTTPException as exc:
            logger.exception("Discord returned an error while setting up a private thread.")
            if thread is not None:
                try:
                    await thread.delete(reason="Could not complete private thread setup")
                except discord.HTTPException:
                    logger.exception("Could not remove an incomplete private thread.")
            if isinstance(exc, discord.Forbidden):
                error_message = (
                    "Discord denied permission to create or set up that private thread. "
                    "Please ask a server moderator to check my channel permissions."
                )
            else:
                error_message = (
                    "Discord couldn't finish setting up the private thread. Please try again."
                )
            await interaction.followup.send(
                error_message,
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Your private thread is ready: {thread.mention}\n"
            "Private threads limit visibility to members, but they are not end-to-end "
            "encrypted and server staff may have access.",
            ephemeral=True,
        )

    @app_commands.command(
        name="reset",
        description="Clear your conversation memory for this channel or thread.",
    )
    async def reset(self, interaction: discord.Interaction) -> None:
        """Clear only the invoking user's history for the current channel/thread."""
        key = _session_key(
            interaction.guild_id, interaction.channel_id, interaction.user.id
        )
        await self.service.reset(key)
        await interaction.response.send_message(
            "Your conversation memory for this channel or thread has been cleared.",
            ephemeral=True,
        )

    @app_commands.command(
        name="help",
        description="Show what this bot can do and its important limitations.",
    )
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Explain available commands and provide the AI disclaimer."""
        await interaction.response.send_message(
            "**StaySafeBot** is an empathetic AI sounding board for casual chat, "
            "venting, brainstorming, and identity exploration.\n"
            "• Send me a message, mention me, or reply to one of my messages to chat.\n"
            "• `/mode` — choose casual, vent, advice, or identity mode for this channel.\n"
            "• `/rant` — get a listening-first response.\n"
            "• `/private` — create an invite-only thread in this server.\n"
            "• `/reset` — clear your memory for this channel or thread.\n\n"
            "I'm an AI, not a licensed mental health professional or therapist. "
            "My replies can be mistaken; for urgent danger, contact local emergency "
            "services or someone you trust. Private Discord threads are not "
            "end-to-end encrypted and may be accessible to server staff.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Respond to mentions, replies to the bot, DMs, and private threads."""
        if message.author.bot or self.bot.user is None:
            return

        bot_user = self.bot.user
        is_mention = bot_user in message.mentions
        is_direct_message = isinstance(
            message.channel, (discord.DMChannel, discord.GroupChannel)
        )
        is_private_thread = (
            isinstance(message.channel, discord.Thread)
            and message.channel.type is discord.ChannelType.private_thread
        )

        referenced_message: discord.Message | None = None
        if message.reference is not None:
            resolved = message.reference.resolved
            if isinstance(resolved, discord.Message):
                referenced_message = resolved
            elif message.reference.message_id is not None:
                try:
                    referenced_message = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                except discord.HTTPException:
                    logger.warning(
                        "Could not fetch a replied-to message in channel %s.",
                        message.channel.id,
                        exc_info=True,
                    )

        is_reply_to_bot = (
            referenced_message is not None
            and referenced_message.author.id == bot_user.id
        )
        if not (
            is_mention
            or is_reply_to_bot
            or is_direct_message
            or is_private_thread
        ):
            return

        user_message = re.sub(rf"<@!?{bot_user.id}>", "", message.content).strip()
        attachments = list(message.attachments)
        if is_reply_to_bot and referenced_message is not None:
            attachments.extend(referenced_message.attachments)

        input_parts: list[types.Part | str] = []
        try:
            async with message.channel.typing():
                image_count = 0
                for attachment in attachments:
                    mime_type = attachment.content_type
                    if mime_type is None or not mime_type.startswith("image/"):
                        continue
                    image_bytes = await attachment.read()
                    input_parts.append(
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                    )
                    image_count += 1

                if user_message:
                    input_parts.insert(0, user_message)
                elif image_count:
                    input_parts.insert(0, "what's this?")
                else:
                    return

                mode = self.user_modes.get(
                    (message.channel.id, message.author.id),
                    "casual",
                )
                answer = await self.service.reply_multimodal(
                    session_key=_session_key(
                        message.guild.id if message.guild else None,
                        message.channel.id,
                        message.author.id,
                    ),
                    parts=input_parts,
                    mode=mode,
                )
        except discord.HTTPException:
            logger.exception("Discord failed to download a message image attachment.")
            answer = "i couldn't load that image from discord — could you try sending it again?"
        except GeminiServiceError as exc:
            logger.exception("Gemini failed while handling a Discord message.")
            if exc.rate_limited:
                answer = "Gemini is rate-limiting requests right now. Please try again shortly."
            else:
                answer = "I couldn't reach the conversation service just now. Please try again shortly."

        for index, chunk in enumerate(_split_message(answer)):
            try:
                if index == 0:
                    await message.reply(chunk, mention_author=False)
                else:
                    await message.channel.send(chunk)
            except discord.HTTPException:
                logger.exception("Discord failed to deliver a chat response.")
                return


async def setup(bot: commands.Bot) -> None:
    """Register this cog as a Discord extension."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    service = GeminiChatService(api_key=api_key)
    await bot.add_cog(ChatCog(bot, service))
