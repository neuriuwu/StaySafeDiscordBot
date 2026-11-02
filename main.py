"""Entrypoint for the StaySafeBot Discord application."""

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class StaySafeBot(commands.Bot):
    """Discord bot that loads the chat cog and registers application commands."""

    async def setup_hook(self) -> None:
        await self.load_extension("cogs.chat")
        synced_commands = await self.tree.sync()
        logger.info("Synced %d application command(s).", len(synced_commands))

    async def on_ready(self) -> None:
        """Advertise the bot's role without implying professional credentials."""
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="your thoughts, lol",
            )
        )
        logger.info("Connected to Discord as %s.", self.user)


def create_bot() -> StaySafeBot:
    """Create the bot with only the intents required by this application."""
    intents = discord.Intents.default()
    intents.message_content = True
    return StaySafeBot(
        command_prefix=commands.when_mentioned,
        intents=intents,
        help_command=None,
    )


def main() -> None:
    """Load configuration and start the Discord client."""
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and configure it."
        )
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and configure it."
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = create_bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
