# Streamcord / TwitchBot, the best Twitch.tv bot for Discord
# Copyright (C) Akira, 2017-2020
# Public build - 04/19/2020

import asyncio
import datadog
import discord
import logging
import platform
import time

from discord.ext import commands
from motor import motor_asyncio as motor
from os import getenv
from rethinkdb import RethinkDB
from tabulate import tabulate
from .utils import lang, functions, chttp, ws, mongo
from .utils.functions import LogFilter, dogstatsd
from .utils.lang import async_lang

if getenv('VERSION') is None:
    raise RuntimeError('Could not load env file')

if not (functions.is_canary_bot() or getenv('ENABLE_PRO_FEATURES') == '1'):
    datadog.initialize(
        api_key=getenv('DD_API_KEY'),
        app_key=getenv('DD_APP_KEY'),
        statsd_host=getenv('DD_AGENT_ADDR'))

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s @ %(name)s [%(asctime)s] - %(message)s',
    datefmt='%d-%b %H:%M:%S')
logging.captureWarnings(True)
log = logging.getLogger('bot.core')
gw_log = logging.getLogger('discord.gateway')
# filter out "unknown event" logs from discord.py
gw_log.addFilter(LogFilter())

r = RethinkDB()
r.set_loop_type('asyncio')


class TwitchBot(commands.AutoShardedBot):
    def __init__(self, *args, i18n_dir=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.active_vc = {}
        self.cluster_index = round(min(self.shard_ids) / 5)
        self.i18n_dir = i18n_dir
        self.shard_ids = kwargs.get('shard_ids', [0])
        self.uptime = 0
        self.languages = {}

        asyncio.get_event_loop().run_until_complete(async_lang.load_languages(self))
        asyncio.get_event_loop().run_until_complete(self._db_connect())

        self.chttp = chttp.BaseCHTTP(self)
        self.chttp_stream = chttp.TwitchCHTTP(self, is_backend=True)
        self.chttp_twitch = chttp.TwitchCHTTP(self, is_backend=False)

        self.mongo = motor.AsyncIOMotorClient(getenv('MONGO_ADDR'))
        self.db: motor.AsyncIOMotorDatabase = self.mongo[getenv('MONGO_DB')]

        self.add_command(self.__reload__)
        modules = [
            "twitchbot.cogs.events",
            "twitchbot.cogs.general",
            "twitchbot.cogs.games",
            "twitchbot.cogs.audio",
            "twitchbot.cogs.live_role",
            "twitchbot.cogs.notifs",
            "twitchbot.cogs.dev",
            "twitchbot.cogs.twitch",
            # "twitchbot.cogs.status_channels",
            "twitchbot.cogs.easter_eggs"
        ]
        if getenv('ENABLE_PRO_FEATURES') == '1':
            modules.append('twitchbot.cogs.moderation')
        for m in modules:
            # don't catch exceptions; it's probably never good to ignore a
            # failed cog in both dev and production environments
            self.load_extension(m)
            log.debug('Loaded module %s', m)
        log.info('Loaded %i modules', len(modules))

        self.ws = ws.ThreadedWebServer(self)
        if 'web-server' not in (getenv('SC_DISABLED_FEATURES') or []):
            self.ws_thread = self.ws.keep_alive()

    async def _db_connect(self):
        ctime = time.time()
        self.rethink = await r.connect(
            host=getenv('RETHINK_HOST'),
            port=int(getenv('RETHINK_PORT')),
            db=getenv('RETHINK_DB'),
            user=getenv('RETHINK_USER'),
            password=getenv('RETHINK_PASS'))
        log.info(
            'Connected to RethinkDB on %s:%s in %ims',
            getenv('RETHINK_HOST'),
            getenv('RETHINK_PORT'),
            round((time.time() - ctime) * 1000))

    async def on_ready(self):
        print("""\
          ___ _                                    _ 
         / __| |_ _ _ ___ __ _ _ __  __ ___ _ _ __| |
         \\__ \\  _| '_/ -_) _` | '  \\/ _/ _ \\ '_/ _` |
         |___/\\__|_| \\___\\__,_|_|_|_\\__\\___/_| \\__,_|\
        """)
        table_rows = [
            ['discord.py', f'v{discord.__version__}'],
            ['python', f'v{platform.python_version()}'],
            ['system', f'{platform.system()} v{platform.version()}'],
            ['discord user', f'{self.user} (id: {self.user.id})'],
            ['guilds', len(self.guilds)],
            ['users', len(self.users)],
            ['shard ids', getattr(self, 'shard_ids', 'None')],
            ['cluster index', self.cluster_index]
        ]
        logging.info('\n' + tabulate(table_rows))
        self.uptime = time.time()
        await dogstatsd.increment('bot.ready_events')

    async def on_command(self, ctx):
        commands.Cooldown(1, 5, commands.BucketType.user).update_rate_limit()
        await dogstatsd.increment('bot.commands_run', tags=[f'command:{ctx.command}'])

    async def on_message(self, message):
        # use this so we can have a separate on_message in cogs/events.py
        pass

    @staticmethod
    async def _handle_check_failure(ctx: commands.Context, msgs: dict, err: commands.CheckFailure):
        if isinstance(err, commands.NoPrivateMessage):
            error_message = msgs['permissions']['no_pm']
        elif isinstance(err, commands.CommandOnCooldown):
            error_message = msgs['errors']['cooldown'].format(
                time=round(getattr(err, 'retry_after', 1), 1))
        elif isinstance(err, commands.MissingPermissions):
            error_message = msgs['permissions']['user_need_perm'].format(permission=', '.join(err.missing_perms))
        elif isinstance(err, commands.BotMissingPermissions):
            error_message = msgs['permissions']['bot_need_perm'].format(permission=', '.join(err.missing_perms))
        elif isinstance(err, commands.BadArgument):
            error_message = msgs['errors']['not_found'] \
                if ctx.command == "notif_add" \
                else "Invalid argument."
        else:
            error_message = msgs['errors']['check_fail']
        return await ctx.send(error_message)

    async def on_command_error(self, ctx, error):
        msgs = await lang.get_lang(ctx)
        if isinstance(error, commands.CommandInvokeError):
            error = error.original
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, discord.Forbidden):
            try:
                return await ctx.send(msgs['errors']['forbidden'])
            except discord.Forbidden:
                pass
        elif isinstance(error, chttp.exceptions.RatelimitExceeded):
            return await ctx.send(msgs['errors']['too_many_requests'])
        elif isinstance(error, asyncio.CancelledError):
            return await ctx.send(
                msgs['errors']['conn_closed'].format(
                    reason=getattr(error, 'reason', 'disconnected')))
        elif isinstance(error, discord.ConnectionClosed):
            return await ctx.send(
                msgs['errors']['conn_closed'].format(
                    reason=getattr(error, 'reason', 'disconnected')))
        elif isinstance(error, discord.NotFound):
            return await ctx.send(msgs['errors']['not_found'])
        elif isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(
                msgs['errors']['missing_arg'].format(
                    param=getattr(error, 'param', None)))
        # check failures
        elif isinstance(error, commands.CheckFailure):
            return await TwitchBot._handle_check_failure(ctx, msgs, error)
        else:
            # Process unhandled exceptions with an error report
            err = f"{type(error).__name__}: {error}"
            logging.fatal(err)
            e = discord.Embed(
                color=discord.Color.red(),
                title=msgs['games']['generic_error'],
                description=f"{msgs['errors']['err_report']}\n```\n{err}\n```")
            return await ctx.send(embed=e)

    @commands.command(hidden=True, name="reload")
    async def __reload__(ctx: commands.Context, cog: str):
        if not functions.is_owner(ctx.author.id):
            return
        try:
            ctx.bot.unload_extension(cog)
            ctx.bot.load_extension(cog)
        except Exception as e:
            await ctx.send(f"Failed to reload cog: `{type(e).__name__}: {e}`")
        else:
            await ctx.send('✅')

    @staticmethod
    def initialize(i18n_dir=None, shard_count=1, shard_ids=None):
        if functions.is_canary_bot():
            if getenv('ENABLE_PRO_FEATURES') == '1':
                activity = discord.Game(name="with new Pro features")
            else:
                activity = discord.Game(name="with new features")
            prefixes = ["twbeta ", "tb "]
            status = discord.Status.idle
        else:
            if getenv('ENABLE_PRO_FEATURES') == '1':
                activity = discord.Streaming(
                    name="?twitch help · streamcord.io/twitch/pro",
                    url="https://twitch.tv/streamcordbot")
                prefixes = ["?twitch ", "?Twitch "]
            else:
                activity = discord.Streaming(
                    name="!twitch help · streamcord.io/twitch",
                    url="https://twitch.tv/streamcordbot")
                prefixes = ["twitch ", "Twitch ", "!twitch ", "t "]
            status = discord.Status.online

        opts = {}
        if getenv('ENABLE_PRO_FEATURES') != '1':
            opts['max_messages'] = None
            opts['fetch_offline_members'] = False
            logging.info('fetch guild subscriptions? %s', getenv('GUILD_SUBSCRIPTIONS'))
            opts['guild_subscriptions'] = getenv('GUILD_SUBSCRIPTIONS') == '1'

        bot = TwitchBot(
            activity=activity,
            command_prefix=prefixes,
            i18n_dir=i18n_dir,
            owner_id=236251438685093889,
            shard_count=shard_count,
            shard_ids=list(shard_ids),
            status=status,
            **opts)
        return bot
