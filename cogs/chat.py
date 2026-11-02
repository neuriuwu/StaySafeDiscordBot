"""Slash commands and message handling for supportive conversations."""

import logging
import os
import re
from typing import Final, Literal

import discord
from discord import app_commands
from discord.ext import commands

from services.gemini_service import GeminiChatService, GeminiServiceError

logger = logging.getLogger(__name__)
DISCORD_MESSAGE_LIMIT: Final[int] = 2_000
PRIVATE_THREAD_PREFIX: Final[str] = "safe-chat-"
ConversationMode = Literal["casual", "vent", "advice", "identity"]


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
    """Chat commands, private thread setup, and mention/thread responses."""

    def __init__(self, bot: commands.Bot, service: GeminiChatService) -> None:
        self.bot = bot
        self.service = service

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

    @app_commands.command(name="talk", description="Chat with a supportive AI sounding board.")
    @app_commands.describe(
        message="What would you like to talk about?",
        mode="Choose the tone that would feel most helpful.",
    )
    async def talk(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 2_000],
        mode: ConversationMode = "casual",
    ) -> None:
        """Start or continue a conversation in the current channel or thread."""
        await interaction.response.defer(thinking=True)
        try:
            answer = await self._generate_reply(
                channel_id=interaction.channel_id,
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                message=message,
                mode=mode,
            )
        except GeminiServiceError as exc:
            logger.exception("Gemini failed while handling /talk.")
            if exc.rate_limited:
                answer = "Gemini is rate-limiting requests right now. Please try again shortly."
            else:
                answer = "I couldn't reach the conversation service just now. Please try again shortly."
        await self._reply_to_interaction(interaction, answer)

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
                "Use `/talk` or `/rant` here whenever you'd like. "
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
            "• `/talk` — chat and choose a mode.\n"
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
        """Reply to bot mentions and messages in private threads the bot created."""
        if message.author.bot or self.bot.user is None:
            return

        is_own_private_thread = (
            isinstance(message.channel, discord.Thread)
            and message.channel.type is discord.ChannelType.private_thread
            and message.channel.owner_id == self.bot.user.id
        )
        is_mention = self.bot.user in message.mentions
        if not is_own_private_thread and not is_mention:
            return

        user_message = message.content
        if is_mention:
            user_message = user_message.replace(f"<@{self.bot.user.id}>", "")
            user_message = user_message.replace(f"<@!{self.bot.user.id}>", "")
        user_message = user_message.strip()
        if not user_message:
            return

        try:
            async with message.channel.typing():
                answer = await self._generate_reply(
                    channel_id=message.channel.id,
                    guild_id=message.guild.id if message.guild else None,
                    user_id=message.author.id,
                    message=user_message,
                    mode="casual",
                )
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
