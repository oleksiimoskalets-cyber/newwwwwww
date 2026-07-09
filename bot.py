# ─────────────────────────────────────────────────────────────
#  sepia bot - ticket system, moderation & detailed logging
# ─────────────────────────────────────────────────────────────

import os
import re
import asyncio
import datetime
from collections import defaultdict

import discord
from discord.ext import commands
from discord import ui, app_commands
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

load_dotenv()

# ─── background web server for Render health checks ──────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    
    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        print(f"Health check web server started on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"Failed to start health check server: {e}")

# Start the health check server in a background thread
threading.Thread(target=run_web_server, daemon=True).start()

# ─── configuration ───────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")

TICKET_CHANNEL_ID   = 1524735248233922621
TICKET_CATEGORY_ID  = 1524720688827727994
LOG_CHANNEL_ID      = 1524751486834184192
PING_ROLES          = [1521769888249810985, 1524736000356651108, 1524785164025069739]
EMBED_COLOR         = 0x000000
TICKET_IMAGE        = "https://i.pinimg.com/736x/04/c2/68/04c2681b9e885b1502e3c301c98061b3.jpg"

# anti-spam
SPAM_MSG_LIMIT      = 10
SPAM_TIME_WINDOW    = 5       # seconds
SPAM_TIMEOUT        = 300     # seconds (5 min)

# anti-raid
RAID_CHANNEL_LIMIT  = 3
RAID_CHANNEL_WINDOW = 10      # seconds
RAID_JOIN_LIMIT     = 10
RAID_JOIN_WINDOW    = 10      # seconds

# ─── bot setup ───────────────────────────────────────────────
intents = discord.Intents.all()

bot = commands.Bot(command_prefix=["!", "s!"], intents=intents, help_command=None)

# ─── in-memory stores ───────────────────────────────────────
active_tickets   = {}                          # channel_id -> {owner, claimed_by, type}
user_messages    = defaultdict(list)           # user_id -> [(timestamp, channel_id)]
channel_creates  = defaultdict(list)           # user_id -> [(timestamp, channel_id)]
recent_joins     = []                          # [(user_id, timestamp)]
raid_mode        = False
last_nsfw_msg_id = None
invites_cache    = {}                          # guild_id -> {code: invite}

# ─── stats database ──────────────────────────────────────────
import sqlite3

def init_db():
    conn = sqlite3.connect("stats.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            guild_id INTEGER,
            user_id INTEGER,
            inviter_id INTEGER,
            invite_code TEXT,
            timestamp TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leaves (
            guild_id INTEGER,
            user_id INTEGER,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def record_join(guild_id, user_id, inviter_id, invite_code):
    try:
        conn = sqlite3.connect("stats.db")
        cursor = conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cursor.execute(
            "INSERT OR REPLACE INTO invites (guild_id, user_id, inviter_id, invite_code, timestamp) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, inviter_id, invite_code, now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"record_join db error: {e}")

def record_leave(guild_id, user_id):
    try:
        conn = sqlite3.connect("stats.db")
        cursor = conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO leaves (guild_id, user_id, timestamp) VALUES (?, ?, ?)",
            (guild_id, user_id, now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"record_leave db error: {e}")

def get_stats(guild_id):
    try:
        conn = sqlite3.connect("stats.db")
        cursor = conn.cursor()
        
        # Start of today (UTC)
        today_start = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        # Joins today
        cursor.execute("SELECT COUNT(*) FROM invites WHERE guild_id = ? AND timestamp >= ?", (guild_id, today_start))
        joins_today = cursor.fetchone()[0]
        
        # Leaves today
        cursor.execute("SELECT COUNT(*) FROM leaves WHERE guild_id = ? AND timestamp >= ?", (guild_id, today_start))
        leaves_today = cursor.fetchone()[0]
        
        # Total invites overall
        cursor.execute("""
            SELECT inviter_id, COUNT(*) as count 
            FROM invites 
            WHERE guild_id = ? AND inviter_id IS NOT NULL 
            GROUP BY inviter_id 
            ORDER BY count DESC 
            LIMIT 10
        """, (guild_id,))
        leaderboard_overall = cursor.fetchall()
        
        # Total invites today
        cursor.execute("""
            SELECT inviter_id, COUNT(*) as count 
            FROM invites 
            WHERE guild_id = ? AND inviter_id IS NOT NULL AND timestamp >= ? 
            GROUP BY inviter_id 
            ORDER BY count DESC 
            LIMIT 10
        """, (guild_id, today_start))
        leaderboard_today = cursor.fetchall()
        
        conn.close()
        return joins_today, leaves_today, leaderboard_overall, leaderboard_today
    except Exception as e:
        print(f"get_stats db error: {e}")
        return 0, 0, [], []

def get_user_invite_count(guild_id, user_id):
    try:
        conn = sqlite3.connect("stats.db")
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM invites WHERE guild_id = ? AND inviter_id = ?", (guild_id, user_id))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0

async def update_invites_cache(guild: discord.Guild):
    try:
        invites = await guild.invites()
        invites_cache[guild.id] = {invite.code: invite for invite in invites}
    except Exception:
        pass

async def find_inviter(member: discord.Member):
    guild = member.guild
    cached = invites_cache.get(guild.id, {})
    try:
        current_invites = await guild.invites()
    except Exception:
        return None, None
        
    used_invite = None
    for invite in current_invites:
        old_invite = cached.get(invite.code)
        if old_invite and invite.uses > old_invite.uses:
            used_invite = invite
            break
            
    # Update cache
    invites_cache[guild.id] = {invite.code: invite for invite in current_invites}
    
    if used_invite:
        return used_invite.inviter, used_invite.code
    return None, None



# ═════════════════════════════════════════════════════════════
#  helpers
# ═════════════════════════════════════════════════════════════
def ts(dt=None):
    """discord relative timestamp"""
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    return f"<t:{int(dt.timestamp())}:F>"

def ts_relative(dt=None):
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    return f"<t:{int(dt.timestamp())}:R>"

async def send_log(guild: discord.Guild, embed: discord.Embed):
    """send an embed to the log channel"""
    try:
        ch = guild.get_channel(LOG_CHANNEL_ID)
        if ch:
            await ch.send(embed=embed)
    except Exception as e:
        print(f"log error: {e}")

def perms_str(perms):
    """convert permission overwrites to readable string"""
    allowed = [p for p, v in perms if v is True]
    denied  = [p for p, v in perms if v is False]
    parts = []
    if allowed:
        parts.append(f"allowed: {', '.join(allowed)}")
    if denied:
        parts.append(f"denied: {', '.join(denied)}")
    return " | ".join(parts) if parts else "none"


# ═════════════════════════════════════════════════════════════
#  ticket dropdown view (persistent)
# ═════════════════════════════════════════════════════════════
class TicketDropdown(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="general support",
                description="get help with general questions",
                value="general_support",
                emoji=discord.PartialEmoji.from_str("<:hehe:1521856286919233556>")
            ),
            discord.SelectOption(
                label="report",
                description="report a user or issue",
                value="report",
                emoji=discord.PartialEmoji.from_str("<a:idk:1521840775225151559>")
            ),
            discord.SelectOption(
                label="host",
                description="host a giveaway/ event",
                value="host",
                emoji=discord.PartialEmoji.from_str("<:thats_it:1521844068664086599>")
            ),
            discord.SelectOption(
                label="partner",
                description="partnership applications",
                value="partner",
                emoji=discord.PartialEmoji.from_str("<:rizz:1521843982617808946>")
            ),
            discord.SelectOption(
                label="boosters perk",
                description="booster perks and rewards",
                value="boosters_perk",
                emoji=discord.PartialEmoji.from_str("<:danthechud:1521783555276017705>")
            ),
            discord.SelectOption(
                label="mod application",
                description="apply for staff / moderator",
                value="mod_application",
                emoji=discord.PartialEmoji.from_str("<:yummmiii:1521856695968600065>")
            ),
        ]
        super().__init__(
            custom_id="ticket_create",
            placeholder="select a ticket category",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        guild   = interaction.guild
        user    = interaction.user
        category = self.values[0]

        # check if user already has a ticket
        existing = next(
            ((cid, d) for cid, d in active_tickets.items() if d["owner"] == user.id),
            None
        )
        if existing:
            return await interaction.response.send_message(
                f"you already have an open ticket: <#{existing[0]}>",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        labels = {
            "general_support": "general support",
            "report": "report",
            "host": "host",
            "partner": "partner",
            "boosters_perk": "boosters perk",
            "mod_application": "mod application"
        }

        try:
            # permission overwrites
            overwrites = {
                # hide from @everyone
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                # allow ticket creator
                user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True
                ),
                # allow bot
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True,
                    read_message_history=True
                )
            }
            # allow staff roles
            for role_id in PING_ROLES:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_messages=True,
                        attach_files=True
                    )

            ticket_cat = guild.get_channel(TICKET_CATEGORY_ID)
            ticket_ch = await guild.create_text_channel(
                name=f"ticket-{user.name}",
                category=ticket_cat,
                overwrites=overwrites,
                reason=f"ticket created by {user} ({category})"
            )

            # store ticket
            active_tickets[ticket_ch.id] = {
                "owner": user.id,
                "claimed_by": None,
                "type": category
            }

            # ticket embed
            embed = discord.Embed(
                title=labels[category],
                description=(
                    f"hello {user.mention},\n"
                    f"thank you for reaching out. a staff member will be with you shortly."
                ),
                color=EMBED_COLOR
            )
            embed.add_field(name="created", value=ts(), inline=True)
            embed.add_field(name="by", value=user.mention, inline=True)
            embed.add_field(name="category", value=labels[category], inline=True)
            embed.set_thumbnail(url=user.display_avatar.url)
            embed.timestamp = datetime.datetime.now(datetime.timezone.utc)

            # buttons
            view = TicketButtons()
            pings = " ".join(f"<@&{r}>" for r in PING_ROLES)

            await ticket_ch.send(
                content=f"{pings} - new ticket from {user.mention}",
                embed=embed,
                view=view
            )

            await interaction.followup.send(
                f"your ticket has been created: {ticket_ch.mention}",
                ephemeral=True
            )

            # detailed log
            log_embed = discord.Embed(
                title="ticket created",
                description=(
                    f"**user:** {user} ({user.mention})\n"
                    f"**user id:** `{user.id}`\n"
                    f"**account created:** {ts_relative(user.created_at)}\n"
                    f"**category:** {labels[category]}\n"
                    f"**channel:** {ticket_ch.mention}\n"
                    f"**channel id:** `{ticket_ch.id}`\n"
                    f"**timestamp:** {ts()}"
                ),
                color=EMBED_COLOR
            )
            log_embed.set_thumbnail(url=user.display_avatar.url)
            log_embed.set_footer(text=f"ticket id: {ticket_ch.id}")
            log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
            await send_log(guild, log_embed)

        except Exception as e:
            print(f"ticket creation error: {e}")
            await interaction.followup.send("failed to create ticket. please try again.", ephemeral=True)


class TicketDropdownView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())


# ═════════════════════════════════════════════════════════════
#  ticket buttons (claim & close)
# ═════════════════════════════════════════════════════════════
class TicketButtons(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="claim", style=discord.ButtonStyle.secondary, custom_id="ticket_claim")
    async def claim_button(self, interaction: discord.Interaction, button: ui.Button):
        ticket = active_tickets.get(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("this is not a valid ticket.", ephemeral=True)

        if ticket["claimed_by"]:
            claimer = interaction.guild.get_member(ticket["claimed_by"])
            return await interaction.response.send_message(
                f"this ticket is already claimed by {claimer.mention if claimer else 'someone'}.",
                ephemeral=True
            )

        # check if user is staff
        member = interaction.user
        is_staff = any(member.get_role(r) for r in PING_ROLES)
        if not is_staff:
            return await interaction.response.send_message(
                "you do not have permission to claim tickets.",
                ephemeral=True
            )

        ticket["claimed_by"] = member.id
        active_tickets[interaction.channel.id] = ticket

        # update button
        button.label = f"claimed by {member.name}"
        button.disabled = True
        await interaction.response.edit_message(view=self)

        claim_embed = discord.Embed(
            description=f"this ticket has been claimed by {member.mention}.",
            color=EMBED_COLOR
        )
        claim_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await interaction.channel.send(embed=claim_embed)

        # detailed log
        ticket_owner_id = ticket["owner"]
        ticket_type = ticket["type"]
        owner = interaction.guild.get_member(ticket_owner_id)
        owner_display = owner.mention if owner else f"`{ticket_owner_id}`"
        log_embed = discord.Embed(
            title="ticket claimed",
            description=(
                f"**staff member:** {member} ({member.mention})\n"
                f"**staff id:** `{member.id}`\n"
                f"**ticket owner:** {owner_display}\n"
                f"**channel:** {interaction.channel.mention}\n"
                f"**ticket type:** {ticket_type}\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(interaction.guild, log_embed)

    @ui.button(label="close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: ui.Button):
        ticket = active_tickets.get(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("this is not a valid ticket.", ephemeral=True)

        await interaction.response.send_message("closing this ticket in 5 seconds...")

        # detailed log
        ticket_owner_id = ticket["owner"]
        ticket_type = ticket["type"]
        claimed_by_id = ticket["claimed_by"]
        owner = interaction.guild.get_member(ticket_owner_id)
        owner_display = owner.mention if owner else f"`{ticket_owner_id}`"
        claimed_display = f"<@{claimed_by_id}>" if claimed_by_id else "unclaimed"
        log_embed = discord.Embed(
            title="ticket closed",
            description=(
                f"**closed by:** {interaction.user} ({interaction.user.mention})\n"
                f"**closer id:** `{interaction.user.id}`\n"
                f"**ticket owner:** {owner_display}\n"
                f"**channel:** #{interaction.channel.name}\n"
                f"**channel id:** `{interaction.channel.id}`\n"
                f"**ticket type:** {ticket_type}\n"
                f"**claimed by:** {claimed_display}\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(interaction.guild, log_embed)

        active_tickets.pop(interaction.channel.id, None)

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason="ticket closed")
        except Exception as e:
            print(f"failed to delete ticket channel: {e}")


# ═════════════════════════════════════════════════════════════
#  commands - manual setup
# ═════════════════════════════════════════════════════════════
@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup_panel(ctx: commands.Context):
    """manually setup or refresh the ticket panel"""
    try:
        ch = bot.get_channel(TICKET_CHANNEL_ID)
        if not ch:
            ch = await bot.fetch_channel(TICKET_CHANNEL_ID)

        # delete old panel messages in this channel
        async for msg in ch.history(limit=50):
            if msg.author == bot.user and msg.components:
                try:
                    await msg.delete()
                except Exception:
                    pass

        embed = discord.Embed(
            title="support tickets",
            description="select a category below to open a ticket.\nour team will assist you as soon as possible.",
            color=EMBED_COLOR
        )
        embed.set_image(url=TICKET_IMAGE)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)

        view = TicketDropdownView()
        await ch.send(embed=embed, view=view)
        try:
            await ctx.message.delete()
        except Exception:
            pass
    except discord.Forbidden:
        await ctx.send("❌ error: bot lacks permission to send messages/embeds in the ticket channel.", delete_after=10)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=10)


# ═════════════════════════════════════════════════════════════
#  commands - moderation (prefix: s! or !)
# ═════════════════════════════════════════════════════════════

def is_mod_or_admin():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        is_staff = any(ctx.author.get_role(r) for r in PING_ROLES)
        if not is_staff:
            raise commands.MissingPermissions(["Staff Role"])
        return True
    return commands.check(predicate)

@bot.command(name="timeout")
@is_mod_or_admin()
async def timeout_member(ctx, member: discord.Member, duration: str, *, reason: str = "no reason provided"):
    """timeout a member. format: s!timeout @member 10m [reason]"""
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ you cannot timeout someone with an equal or higher role than you.", delete_after=5)
    if member.top_role >= ctx.guild.me.top_role:
        return await ctx.send("❌ i cannot timeout this member as their role is higher than mine.", delete_after=5)

    time_units = {"m": 60, "h": 3600, "d": 86400}
    unit = duration[-1].lower()
    val_str = duration[:-1]

    if unit in time_units and val_str.isdigit():
        seconds = int(val_str) * time_units[unit]
    elif duration.isdigit():
        seconds = int(duration) * 60
        duration = f"{duration}m"
    else:
        return await ctx.send("❌ invalid duration format. use e.g. 10m, 2h, 1d.", delete_after=5)

    try:
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
        await member.timeout(until, reason=f"by {ctx.author}: {reason}")
        
        embed = discord.Embed(
            description=f"✅ {member.mention} has been timed out for **{duration}**.\n**reason:** {reason}",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)

        log_embed = discord.Embed(
            title="member timed out",
            description=(
                f"**user:** {member} ({member.mention})\n"
                f"**user id:** `{member.id}`\n"
                f"**moderator:** {ctx.author} ({ctx.author.mention})\n"
                f"**duration:** {duration}\n"
                f"**reason:** {reason}\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(ctx.guild, log_embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="ban")
@is_mod_or_admin()
@commands.has_permissions(ban_members=True)
async def ban_member(ctx, member: discord.Member, *, reason: str = "no reason provided"):
    """ban a member. format: s!ban @member [reason]"""
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ you cannot ban someone with an equal or higher role than you.", delete_after=5)
    if member.top_role >= ctx.guild.me.top_role:
        return await ctx.send("❌ i cannot ban this member as their role is higher than mine.", delete_after=5)

    try:
        await member.ban(reason=f"by {ctx.author}: {reason}", delete_message_seconds=604800)
        embed = discord.Embed(
            description=f"✅ {member} has been banned.\n**reason:** {reason}",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="kick")
@is_mod_or_admin()
@commands.has_permissions(kick_members=True)
async def kick_member(ctx, member: discord.Member, *, reason: str = "no reason provided"):
    """kick a member. format: s!kick @member [reason]"""
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ you cannot kick someone with an equal or higher role than you.", delete_after=5)
    if member.top_role >= ctx.guild.me.top_role:
        return await ctx.send("❌ i cannot kick this member as their role is higher than mine.", delete_after=5)

    try:
        await member.kick(reason=f"by {ctx.author}: {reason}")
        embed = discord.Embed(
            description=f"✅ {member} has been kicked.\n**reason:** {reason}",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="warn")
@is_mod_or_admin()
async def warn_member(ctx, member: discord.Member, *, reason: str):
    """warn a member. format: s!warn @member <reason>"""
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ you cannot warn someone with an equal or higher role than you.", delete_after=5)

    try:
        try:
            dm_embed = discord.Embed(
                title=f"you have been warned in {ctx.guild.name}",
                description=f"**reason:** {reason}",
                color=EMBED_COLOR
            )
            await member.send(embed=dm_embed)
            dm_status = "notified via DM"
        except Exception:
            dm_status = "could not DM user"

        embed = discord.Embed(
            description=f"✅ {member.mention} has been warned.\n**reason:** {reason}\n*({dm_status})*",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)

        log_embed = discord.Embed(
            title="member warned",
            description=(
                f"**user:** {member} ({member.mention})\n"
                f"**user id:** `{member.id}`\n"
                f"**moderator:** {ctx.author} ({ctx.author.mention})\n"
                f"**reason:** {reason}\n"
                f"**DM status:** {dm_status}\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(ctx.guild, log_embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="unban")
@is_mod_or_admin()
@commands.has_permissions(ban_members=True)
async def unban_user(ctx, user_id: int):
    """unban a user by ID. format: s!unban <user_id>"""
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"by {ctx.author}")
        embed = discord.Embed(
            description=f"✅ {user} has been unbanned.",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)
    except discord.NotFound:
        await ctx.send("❌ user ban not found or invalid user ID.", delete_after=5)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="purge", aliases=["clear"])
@is_mod_or_admin()
@commands.has_permissions(manage_messages=True)
async def purge_messages(ctx, amount: int):
    """purge messages. format: s!purge <amount>"""
    if amount < 1 or amount > 100:
        return await ctx.send("❌ please specify an amount between 1 and 100.", delete_after=5)

    try:
        deleted = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"✅ cleared {len(deleted) - 1} messages.", delete_after=3)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="lock")
@is_mod_or_admin()
@commands.has_permissions(manage_channels=True)
async def lock_channel(ctx):
    """lock the current channel. format: s!lock"""
    try:
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        if overwrite.send_messages is False:
            return await ctx.send("🔒 this channel is already locked.", delete_after=5)

        overwrite.send_messages = False
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"locked by {ctx.author}")
        
        embed = discord.Embed(
            description="🔒 **this channel has been locked.**",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)

        log_embed = discord.Embed(
            title="channel locked",
            description=(
                f"**channel:** {ctx.channel.mention} ({ctx.channel.name})\n"
                f"**moderator:** {ctx.author} ({ctx.author.mention})\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(ctx.guild, log_embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="unlock")
@is_mod_or_admin()
@commands.has_permissions(manage_channels=True)
async def unlock_channel(ctx):
    """unlock the current channel. format: s!unlock"""
    try:
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        if overwrite.send_messages is not False:
            return await ctx.send("🔓 this channel is not locked.", delete_after=5)

        overwrite.send_messages = None
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"unlocked by {ctx.author}")
        
        embed = discord.Embed(
            description="🔓 **this channel has been unlocked.**",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)

        log_embed = discord.Embed(
            title="channel unlocked",
            description=(
                f"**channel:** {ctx.channel.mention} ({ctx.channel.name})\n"
                f"**moderator:** {ctx.author} ({ctx.author.mention})\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(ctx.guild, log_embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)



@bot.command(name="slowmode", aliases=["slow"])
@is_mod_or_admin()
@commands.has_permissions(manage_channels=True)
async def set_slowmode(ctx, seconds: int):
    """set slowmode for the channel. format: s!slowmode <seconds>"""
    try:
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await ctx.send("🔓 slowmode has been disabled.")
        else:
            await ctx.send(f"⏳ slowmode set to {seconds} seconds.")
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="nuke")
@is_mod_or_admin()
@commands.has_permissions(manage_channels=True)
async def nuke_channel(ctx):
    """clone and delete the current channel. format: s!nuke"""
    try:
        channel = ctx.channel
        pos = channel.position
        new_ch = await channel.clone(reason="channel nuke")
        await new_ch.edit(position=pos)
        await channel.delete(reason="nuke")
        
        embed = discord.Embed(
            title="channel nuked",
            description="this channel was purged and recreated.",
            color=EMBED_COLOR
        )
        embed.set_image(url="https://media.giphy.com/media/oe33xf3B50fsc/giphy.gif")
        await new_ch.send(embed=embed, delete_after=10)

        log_embed = discord.Embed(
            title="channel nuked",
            description=(
                f"**channel:** #{new_ch.name} (cloned from original)\n"
                f"**moderator:** {ctx.author} ({ctx.author.mention})\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(ctx.guild, log_embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="nick", aliases=["nickname"])
@is_mod_or_admin()
@commands.has_permissions(manage_nicknames=True)
async def change_nickname(ctx, member: discord.Member, *, nickname: str = None):
    """change a member's nickname. format: s!nick @member [new_nickname]"""
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ you cannot change nickname of someone with equal/higher role.", delete_after=5)
    if member.top_role >= ctx.guild.me.top_role:
        return await ctx.send("❌ i cannot edit this member's nickname.", delete_after=5)

    try:
        await member.edit(nick=nickname)
        embed = discord.Embed(
            description=f"✅ changed nickname for {member.mention} to **{nickname or 'reset'}**.",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="vmute")
@is_mod_or_admin()
@commands.has_permissions(mute_members=True)
async def voice_mute(ctx, member: discord.Member, *, reason: str = "no reason provided"):
    """server voice mute a member. format: s!vmute @member"""
    try:
        if not member.voice or not member.voice.channel:
            return await ctx.send("❌ member is not in a voice channel.", delete_after=5)
        await member.edit(mute=True, reason=f"by {ctx.author}: {reason}")
        await ctx.send(f"🔇 server voice muted {member.mention}.")
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="vunmute")
@is_mod_or_admin()
@commands.has_permissions(mute_members=True)
async def voice_unmute(ctx, member: discord.Member):
    """server voice unmute a member. format: s!vunmute @member"""
    try:
        if not member.voice or not member.voice.channel:
            return await ctx.send("❌ member is not in a voice channel.", delete_after=5)
        await member.edit(mute=False, reason=f"unmuted by {ctx.author}")
        await ctx.send(f"🔊 server voice unmuted {member.mention}.")
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="vdeafen")
@is_mod_or_admin()
@commands.has_permissions(deafen_members=True)
async def voice_deafen(ctx, member: discord.Member, *, reason: str = "no reason provided"):
    """server voice deafen a member. format: s!vdeafen @member"""
    try:
        if not member.voice or not member.voice.channel:
            return await ctx.send("❌ member is not in a voice channel.", delete_after=5)
        await member.edit(deafen=True, reason=f"by {ctx.author}: {reason}")
        await ctx.send(f"🔇 server voice deafened {member.mention}.")
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)

@bot.command(name="vundeafen")
@is_mod_or_admin()
@commands.has_permissions(deafen_members=True)
async def voice_undeafen(ctx, member: discord.Member):
    """server voice undeafen a member. format: s!vundeafen @member"""
    try:
        if not member.voice or not member.voice.channel:
            return await ctx.send("❌ member is not in a voice channel.", delete_after=5)
        await member.edit(deafen=False, reason=f"undeafened by {ctx.author}")
        await ctx.send(f"🔊 server voice undeafened {member.mention}.")
    except Exception as e:
        await ctx.send(f"❌ error: {e}", delete_after=5)


# ═════════════════════════════════════════════════════════════
#  commands - utility / member (prefix: s! or !)
# ═════════════════════════════════════════════════════════════

@bot.command(name="ping")
async def ping_latency(ctx):
    """check bot response time. format: s!ping"""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 pong! **{latency}ms**")

@bot.command(name="avatar", aliases=["av"])
async def show_avatar(ctx, member: discord.Member = None):
    """displays avatar of a member. format: s!avatar [@member]"""
    member = member or ctx.author
    embed = discord.Embed(
        title=f"{member.name}'s avatar",
        color=EMBED_COLOR
    )
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="userinfo", aliases=["whois", "ui"])
async def user_info(ctx, member: discord.Member = None):
    """displays information about a member. format: s!userinfo [@member]"""
    member = member or ctx.author
    
    roles = [role.mention for role in member.roles[1:]]
    roles.reverse()
    roles_str = ", ".join(roles) if roles else "none"

    embed = discord.Embed(
        title=f"user profile - {member}",
        color=EMBED_COLOR
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="tag", value=member.mention, inline=True)
    embed.add_field(name="user id", value=f"`{member.id}`", inline=True)
    embed.add_field(name="nickname", value=member.nick or "none", inline=True)
    embed.add_field(name="bot?", value="yes" if member.bot else "no", inline=True)
    embed.add_field(name="account created", value=ts(member.created_at), inline=False)
    embed.add_field(name="joined server", value=ts(member.joined_at) if member.joined_at else "unknown", inline=False)
    embed.add_field(name="highest role", value=member.top_role.mention, inline=True)
    embed.add_field(name="roles list", value=roles_str, inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="serverinfo", aliases=["si"])
async def server_info(ctx):
    """displays information about the server. format: s!serverinfo"""
    guild = ctx.guild
    
    categories = len(guild.categories)
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    threads = len(guild.threads)

    total_members = guild.member_count
    bots = sum(1 for m in guild.members if m.bot)
    humans = total_members - bots

    embed = discord.Embed(
        title=guild.name,
        color=EMBED_COLOR
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
        
    embed.add_field(name="owner", value=guild.owner.mention, inline=True)
    embed.add_field(name="server id", value=f"`{guild.id}`", inline=True)
    embed.add_field(name="created at", value=ts(guild.created_at), inline=False)
    embed.add_field(name="boosts", value=f"{guild.premium_subscription_count} boosts (level {guild.premium_tier})", inline=True)
    embed.add_field(name="members", value=f"👥 {total_members} total\n👤 {humans} humans\n🤖 {bots} bots", inline=True)
    embed.add_field(name="channels", value=f"📂 {categories} categories\n💬 {text_channels} text\n🔊 {voice_channels} voice\n🧵 {threads} threads", inline=True)
    embed.add_field(name="roles", value=f"{len(guild.roles)} roles", inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name="botinfo", aliases=["bi"])
async def bot_info(ctx):
    """displays information about the bot. format: s!botinfo"""
    embed = discord.Embed(
        title="bot statistics",
        color=EMBED_COLOR
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="bot name", value=bot.user.name, inline=True)
    embed.add_field(name="bot id", value=f"`{bot.user.id}`", inline=True)
    embed.add_field(name="library", value="discord.py v2.3.2+", inline=True)
    embed.add_field(name="guilds count", value=f"{len(bot.guilds)} guilds", inline=True)
    embed.add_field(name="total members", value=f"{sum(g.member_count for g in bot.guilds)} members", inline=True)
    embed.add_field(name="prefix", value="`s!` or `!`", inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name="leaderboard", aliases=["stats", "invites"])
async def leaderboard_command(ctx):
    """displays invite leaderboard and join/leave stats. format: s!leaderboard"""
    guild = ctx.guild
    if not guild:
        return
        
    joins_today, leaves_today, lb_overall, lb_today = get_stats(guild.id)
    
    # Format today's leaderboard
    today_lb_str = ""
    if lb_today:
        for idx, (inviter_id, count) in enumerate(lb_today, 1):
            today_lb_str += f"{idx}. <@{inviter_id}> - **{count}** joins\n"
    else:
        today_lb_str = "*No invites today.*"
        
    # Format overall leaderboard
    overall_lb_str = ""
    if lb_overall:
        for idx, (inviter_id, count) in enumerate(lb_overall, 1):
            overall_lb_str += f"{idx}. <@{inviter_id}> - **{count}** joins\n"
    else:
        overall_lb_str = "*No invites recorded.*"
        
    embed = discord.Embed(
        title="Server Statistics & Invite Leaderboard",
        color=EMBED_COLOR
    )
    embed.add_field(name="📈 Daily Stats", value=f"**Joins Today:** {joins_today}\n**Leaves Today:** {leaves_today}", inline=False)
    embed.add_field(name="🏆 Today's Top Inviters", value=today_lb_str, inline=True)
    embed.add_field(name="👑 Overall Top Inviters", value=overall_lb_str, inline=True)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    
    await ctx.send(embed=embed)


@bot.command(name="help")
async def custom_help(ctx):
    """displays the bot command list. format: s!help"""
    embed = discord.Embed(
        title="sepia bot - command list",
        description="here is a list of all commands you can use with this bot. prefix: **s!** or **!**",
        color=EMBED_COLOR
    )
    
    moderation_text = (
        "`s!setup` - manual ticket setup panel\n"
        "`s!timeout @user <duration> [reason]` - timeout a member (e.g. 10m, 2h, 1d)\n"
        "`s!ban @user [reason]` - ban a member from the server\n"
        "`s!kick @user [reason]` - kick a member from the server\n"
        "`s!unban <user_id>` - unban a user using their ID\n"
        "`s!warn @user <reason>` - warn a member (sends direct message)\n"
        "`s!purge <amount>` - clear up to 100 messages (alias: `s!clear`)\n"
        "`s!lock` - lock current channel text inputs\n"
        "`s!unlock` - unlock current channel text inputs\n"
        "`s!slowmode <seconds>` - set slowmode delay (alias: `s!slow`)\n"
        "`s!nuke` - wipe and recreate the current channel\n"
        "`s!nick @user [nickname]` - edit or reset user nickname (alias: `s!nickname`)\n"
        "`s!vmute @user` - server voice mute a member in VC\n"
        "`s!vunmute @user` - server voice unmute a member in VC\n"
        "`s!vdeafen @user` - server voice deafen a member in VC\n"
        "`s!vundeafen @user` - server voice undeafen a member in VC"
    )
    
    utility_text = (
        "`s!leaderboard` - invite leaderboard & daily stats (alias: `s!stats`, `s!invites`)\n"
        "`s!ping` - shows bot response delay\n"
        "`s!avatar [@user]` - displays avatar of a member (alias: `s!av`)\n"
        "`s!userinfo [@user]` - displays detailed user profile (alias: `s!whois`, `s!ui`)\n"
        "`s!serverinfo` - displays server stats (alias: `s!si`)\n"
        "`s!botinfo` - displays bot stats (alias: `s!bi`)\n"
        "`.ad` - trigger message for server advertisement"
    )

    embed.add_field(name="🛡️ moderation commands", value=moderation_text, inline=False)
    embed.add_field(name="⚙️ utility / member commands", value=utility_text, inline=False)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    
    await ctx.send(embed=embed)


# ═════════════════════════════════════════════════════════════
#  on_ready - send ticket panel
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"logged in as {bot.user}")

    # Set Do Not Disturb status with custom text "hmm.."
    try:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.Activity(type=discord.ActivityType.custom, name="custom", state="hmm..")
        )
    except Exception as e:
        print(f"failed to set status: {e}")

    # cache invites
    print("Caching invites...")
    for guild in bot.guilds:
        await update_invites_cache(guild)
    print("Invites cached.")

    # register persistent views
    bot.add_view(TicketDropdownView())
    bot.add_view(TicketButtons())

    # invite link
    print(f"\ninvite link (administrator):")
    print(f"https://discord.com/oauth2/authorize?client_id={bot.user.id}&permissions=8&scope=bot\n")

    # send ticket panel (if not already sent)
    try:
        ch = bot.get_channel(TICKET_CHANNEL_ID)
        if not ch:
            ch = await bot.fetch_channel(TICKET_CHANNEL_ID)

        async for msg in ch.history(limit=10):
            if msg.author == bot.user and msg.components:
                print("ticket panel already exists, skipping")
                return

        embed = discord.Embed(
            title="support tickets",
            description="select a category below to open a ticket.\nour team will assist you as soon as possible.",
            color=EMBED_COLOR
        )
        embed.set_image(url=TICKET_IMAGE)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)

        view = TicketDropdownView()
        await ch.send(embed=embed, view=view)
        print("ticket panel sent successfully")

    except discord.Forbidden:
        print("\n" + "="*60)
        print(" ERROR: BOT LACKS ACCESS TO THE TICKET CHANNEL!")
        print(f" Channel ID: {TICKET_CHANNEL_ID}")
        print(" Please ensure that:")
        print(" 1. The bot has been invited to the server.")
        print(" 2. The bot has 'View Channel', 'Send Messages', and 'Read History' permissions in that channel.")
        print("="*60 + "\n")
    except Exception as e:
        print(f"failed to setup ticket panel: {e}")


# ═════════════════════════════════════════════════════════════
#  on_message - claimed ticket guard + anti-invite + anti-spam
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    # ── .ad trigger ──
    if message.content.strip().lower() == ".ad":
        try:
            ad_text = (
                "_ _\n"
                "_ _                         [**@sepia**](https://discord.gg/YjPXtHzc9d) \n"
                "-# _ _                        social  •  **bmf**  •  gws .ᐟ"
            )
            await message.channel.send(ad_text)
            try:
                await message.delete()
            except Exception:
                pass
            return
        except Exception as e:
            print(f"failed to send .ad: {e}")

    # ── nsfw warning channel response ──
    if message.channel.id == 1521602890907521034:
        global last_nsfw_msg_id
        try:
            if last_nsfw_msg_id:
                try:
                    old_msg = await message.channel.fetch_message(last_nsfw_msg_id)
                    await old_msg.delete()
                except Exception:
                    pass

            # backup search to clean any older orphaned warnings
            try:
                async for msg in message.channel.history(limit=15):
                    if msg.author == bot.user and "# No NSFW" in msg.content and msg.id != last_nsfw_msg_id:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
            except Exception:
                pass

            new_msg = await message.channel.send("# No NSFW <:shut_it:1521856221861122089>")
            last_nsfw_msg_id = new_msg.id
        except Exception as e:
            print(f"failed to send nsfw warning: {e}")

    member = message.author
    is_admin = member.guild_permissions.administrator



    # ── anti discord invite link ──
    if not is_admin:
        invite_pattern = re.compile(
            r"(discord\.(gg|io|me|li|com/invite)/[^\s]+|discordapp\.com/invite/[^\s]+)",
            re.IGNORECASE
        )
        if invite_pattern.search(message.content):
            try:
                content_backup = message.content
                await message.delete()

                warn = await message.channel.send(
                    f"{message.author.mention}, discord invite links are not allowed."
                )
                await asyncio.sleep(5)
                await warn.delete()

                # detailed log
                log_embed = discord.Embed(
                    title="invite link deleted",
                    description=(
                        f"**user:** {message.author} ({message.author.mention})\n"
                        f"**user id:** `{message.author.id}`\n"
                        f"**channel:** {message.channel.mention}\n"
                        f"**channel id:** `{message.channel.id}`\n"
                        f"**message content:** ||{content_backup[:900]}||\n"
                        f"**links found:** ||{', '.join(invite_pattern.findall(content_backup)[0] if invite_pattern.findall(content_backup) else ['unknown'])}||\n"
                        f"**user roles:** {', '.join(r.mention for r in message.author.roles[1:]) or 'none'}\n"
                        f"**account age:** {ts_relative(message.author.created_at)}\n"
                        f"**timestamp:** {ts()}"
                    ),
                    color=EMBED_COLOR
                )
                log_embed.set_thumbnail(url=message.author.display_avatar.url)
                log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
                await send_log(message.guild, log_embed)
            except Exception as e:
                print(f"anti-invite error: {e}")
            return

    # ── anti spam ──
    if not is_admin:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        user_messages[message.author.id].append((now, message.channel.id))

        # clean old entries
        user_messages[message.author.id] = [
            (t, c) for t, c in user_messages[message.author.id]
            if now - t < SPAM_TIME_WINDOW
        ]

        if len(user_messages[message.author.id]) >= SPAM_MSG_LIMIT:
            try:
                # bulk delete spam messages
                def is_spam(m):
                    return (
                        m.author.id == message.author.id
                        and (datetime.datetime.now(datetime.timezone.utc) - m.created_at).total_seconds() < SPAM_TIME_WINDOW
                    )

                deleted = await message.channel.purge(limit=20, check=is_spam)

                # timeout user
                if isinstance(message.author, discord.Member) and not message.author.top_role >= message.guild.me.top_role:
                    await message.author.timeout(
                        datetime.timedelta(seconds=SPAM_TIMEOUT),
                        reason="anti-spam: sending messages too fast"
                    )

                user_messages.pop(message.author.id, None)

                warn = await message.channel.send(
                    f"{message.author.mention} has been timed out for spamming."
                )
                await asyncio.sleep(5)
                await warn.delete()

                # detailed log
                log_embed = discord.Embed(
                    title="anti-spam triggered",
                    description=(
                        f"**user:** {message.author} ({message.author.mention})\n"
                        f"**user id:** `{message.author.id}`\n"
                        f"**channel:** {message.channel.mention}\n"
                        f"**messages deleted:** {len(deleted)}\n"
                        f"**timeout duration:** {SPAM_TIMEOUT}s\n"
                        f"**messages in window:** {SPAM_MSG_LIMIT}+ in {SPAM_TIME_WINDOW}s\n"
                        f"**user roles:** {', '.join(r.mention for r in message.author.roles[1:]) or 'none'}\n"
                        f"**account age:** {ts_relative(message.author.created_at)}\n"
                        f"**joined server:** {ts_relative(message.author.joined_at)}\n"
                        f"**timestamp:** {ts()}"
                    ),
                    color=EMBED_COLOR
                )
                log_embed.set_thumbnail(url=message.author.display_avatar.url)
                log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
                await send_log(message.guild, log_embed)
            except Exception as e:
                print(f"anti-spam error: {e}")

    await bot.process_commands(message)


# ═════════════════════════════════════════════════════════════
#  anti-raid: mass channel creation
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_guild_channel_create(channel):
    global raid_mode
    if not channel.guild:
        return

    guild = channel.guild
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    # detailed log for all channel creations
    ch_type = str(channel.type).replace("ChannelType.", "")
    log_embed = discord.Embed(
        title="channel created",
        description=(
            f"**name:** #{channel.name}\n"
            f"**channel id:** `{channel.id}`\n"
            f"**type:** {ch_type}\n"
            f"**category:** {channel.category.name if channel.category else 'none'}\n"
            f"**position:** {channel.position}\n"
            f"**timestamp:** {ts()}"
        ),
        color=EMBED_COLOR
    )

    # try to find who created it from audit log
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_create, limit=1):
            if entry.target.id == channel.id:
                log_embed.description += (
                    f"\n**created by:** {entry.user} ({entry.user.mention})\n"
                    f"**creator id:** `{entry.user.id}`\n"
                    f"**reason:** {entry.reason or 'none'}"
                )
                log_embed.set_thumbnail(url=entry.user.display_avatar.url)

                executor = entry.user
                if executor.id == bot.user.id or executor.id == guild.owner_id:
                    break

                channel_creates[executor.id].append((now, channel.id))
                channel_creates[executor.id] = [
                    (t, c) for t, c in channel_creates[executor.id]
                    if now - t < RAID_CHANNEL_WINDOW
                ]

                if len(channel_creates[executor.id]) >= RAID_CHANNEL_LIMIT:
                    raid_mode = True

                    # ban the raider
                    raider = guild.get_member(executor.id)
                    if raider and raider != guild.owner:
                        try:
                            await raider.ban(
                                reason="anti-raid: mass channel creation",
                                delete_message_seconds=604800
                            )
                        except Exception:
                            pass

                    # delete all raid channels
                    for _, cid in channel_creates[executor.id]:
                        raid_ch = guild.get_channel(cid)
                        if raid_ch:
                            try:
                                await raid_ch.delete(reason="anti-raid: cleanup")
                            except Exception:
                                pass

                    channel_creates.pop(executor.id, None)

                    # raid alert log
                    raid_embed = discord.Embed(
                        title="raid detected - mass channel creation",
                        description=(
                            f"**raider:** {executor} ({executor.mention})\n"
                            f"**raider id:** `{executor.id}`\n"
                            f"**channels created:** {RAID_CHANNEL_LIMIT}+ in {RAID_CHANNEL_WINDOW}s\n"
                            f"**action taken:** banned, all created channels deleted, 7 days of messages purged\n"
                            f"**raid mode:** active for 30 seconds\n"
                            f"**account age:** {ts_relative(executor.created_at)}\n"
                            f"**timestamp:** {ts()}"
                        ),
                        color=EMBED_COLOR
                    )
                    raid_embed.set_thumbnail(url=executor.display_avatar.url)
                    raid_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
                    await send_log(guild, raid_embed)

                    await asyncio.sleep(30)
                    raid_mode = False
                    return
            break
    except Exception as e:
        log_embed.description += f"\n**audit log:** could not fetch ({e})"

    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  anti-raid: mass join detection + member join log
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_member_join(member: discord.Member):
    global raid_mode
    guild = member.guild
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    # Track inviter
    inviter, invite_code = await find_inviter(member)
    inviter_id = inviter.id if inviter else None
    record_join(guild.id, member.id, inviter_id, invite_code)

    # Get stats for today
    joins_today, leaves_today, _, _ = get_stats(guild.id)
    inviter_count = get_user_invite_count(guild.id, inviter_id) if inviter_id else 0

    # ── detailed join log ──
    account_age = datetime.datetime.now(datetime.timezone.utc) - member.created_at
    is_new_account = account_age.days < 7

    invite_info = f"**invited by:** {inviter.mention} (code: `{invite_code}` | **{inviter_count}** invites)\n" if inviter else "**invited by:** unknown/vanity url\n"

    log_embed = discord.Embed(
        title="member joined",
        description=(
            f"**user:** {member} ({member.mention})\n"
            f"**user id:** `{member.id}`\n"
            f"{invite_info}"
            f"**account created:** {ts(member.created_at)} ({ts_relative(member.created_at)})\n"
            f"**account age:** {account_age.days} days\n"
            f"**is bot:** {'yes' if member.bot else 'no'}\n"
            f"**member count:** {guild.member_count}\n"
            f"**joins today:** {joins_today} | **leaves today:** {leaves_today}\n"
            f"**timestamp:** {ts()}"
        ),
        color=EMBED_COLOR
    )
    if is_new_account:
        log_embed.description += "\n**warning:** new account (less than 7 days old)"
    log_embed.set_thumbnail(url=member.display_avatar.url)
    log_embed.set_footer(text=f"user id: {member.id}")
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)

    # ── suspicious bot alert ──
    if member.bot:
        bot_embed = discord.Embed(
            title="bot added to server",
            description=(
                f"**bot:** {member} ({member.mention})\n"
                f"**bot id:** `{member.id}`\n"
                f"**bot created:** {ts(member.created_at)} ({ts_relative(member.created_at)})\n"
                f"**timestamp:** {ts()}\n\n"
                f"**check who added this bot in server settings > integrations**"
            ),
            color=EMBED_COLOR
        )
        bot_embed.set_thumbnail(url=member.display_avatar.url)
        bot_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, bot_embed)

    # ── mass join detection ──
    recent_joins.append((member.id, now_ts))
    # clean old
    while recent_joins and now_ts - recent_joins[0][1] > RAID_JOIN_WINDOW:
        recent_joins.pop(0)

    if len(recent_joins) >= RAID_JOIN_LIMIT:
        raid_mode = True

        banned_count = 0
        for uid, _ in recent_joins:
            try:
                raider = guild.get_member(uid)
                if raider and raider != guild.owner and raider.id != bot.user.id:
                    await raider.ban(reason="anti-raid: mass join detected", delete_message_seconds=604800)
                    banned_count += 1
            except Exception:
                pass

        recent_joins.clear()

        raid_embed = discord.Embed(
            title="raid detected - mass join",
            description=(
                f"**joins detected:** {RAID_JOIN_LIMIT}+ in {RAID_JOIN_WINDOW}s\n"
                f"**users banned:** {banned_count}\n"
                f"**action taken:** all recent joiners banned, 7 days of messages purged\n"
                f"**raid mode:** active for 30 seconds\n"
                f"**timestamp:** {ts()}"
            ),
            color=EMBED_COLOR
        )
        raid_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, raid_embed)

        await asyncio.sleep(30)
        raid_mode = False


# ═════════════════════════════════════════════════════════════
#  logging: member leave
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    roles = ", ".join(r.mention for r in member.roles[1:]) or "none"
    join_date = member.joined_at

    # Record leave in DB
    record_leave(guild.id, member.id)

    # Get stats for today
    joins_today, leaves_today, _, _ = get_stats(guild.id)

    # check if it was a kick or ban via audit log
    action_info = ""
    try:
        async for entry in guild.audit_logs(limit=3):
            if entry.action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                if entry.target.id == member.id:
                    action_type = "kicked" if entry.action == discord.AuditLogAction.kick else "banned"
                    action_info = (
                        f"\n**action:** {action_type}\n"
                        f"**by:** {entry.user} ({entry.user.mention})\n"
                        f"**reason:** {entry.reason or 'none'}"
                    )
                    break
    except Exception:
        pass

    duration = ""
    if join_date:
        time_in_server = datetime.datetime.now(datetime.timezone.utc) - join_date
        duration = f"\n**time in server:** {time_in_server.days} days"

    log_embed = discord.Embed(
        title="member left",
        description=(
            f"**user:** {member} ({member.mention})\n"
            f"**user id:** `{member.id}`\n"
            f"**roles:** {roles}\n"
            f"**joined:** {ts(join_date) if join_date else 'unknown'} ({ts_relative(join_date) if join_date else 'unknown'})"
            f"{duration}"
            f"{action_info}\n"
            f"**member count:** {guild.member_count}\n"
            f"**joins today:** {joins_today} | **leaves today:** {leaves_today}\n"
            f"**timestamp:** {ts()}"
        ),
        color=EMBED_COLOR
    )
    log_embed.set_thumbnail(url=member.display_avatar.url)
    log_embed.set_footer(text=f"user id: {member.id}")
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: message edit
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild:
        return
    if before.author.bot:
        return
    if before.content == after.content:
        return

    log_embed = discord.Embed(
        title="message edited",
        description=(
            f"**user:** {after.author} ({after.author.mention})\n"
            f"**user id:** `{after.author.id}`\n"
            f"**channel:** {after.channel.mention}\n"
            f"**channel id:** `{after.channel.id}`\n"
            f"**message id:** `{after.id}`\n"
            f"**message link:** [jump to message]({after.jump_url})\n"
            f"**timestamp:** {ts()}"
        ),
        color=EMBED_COLOR
    )
    log_embed.add_field(
        name="before",
        value=(before.content or "*empty*")[:1024],
        inline=False
    )
    log_embed.add_field(
        name="after",
        value=(after.content or "*empty*")[:1024],
        inline=False
    )
    log_embed.set_thumbnail(url=after.author.display_avatar.url)
    log_embed.set_footer(text=f"message id: {after.id}")
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(before.guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: message delete
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild:
        return
    if message.author and message.author.bot:
        return

    author = message.author
    content = message.content or "*empty/uncached*"

    desc = (
        f"**user:** {author} ({author.mention})\n"
        f"**user id:** `{author.id}`\n"
        f"**channel:** {message.channel.mention}\n"
        f"**channel id:** `{message.channel.id}`\n"
        f"**message id:** `{message.id}`\n"
        f"**message created:** {ts(message.created_at)}\n"
        f"**content:**\n{content[:900]}\n"
        f"**timestamp:** {ts()}"
    )

    if message.attachments:
        att_list = "\n".join(f"[{a.filename}]({a.url}) ({a.size} bytes)" for a in message.attachments)
        desc += f"\n**attachments:**\n{att_list[:500]}"

    if message.embeds:
        desc += f"\n**embeds:** {len(message.embeds)} embed(s)"

    if message.stickers:
        desc += f"\n**stickers:** {', '.join(s.name for s in message.stickers)}"

    # try to find who deleted it
    try:
        async for entry in message.guild.audit_logs(action=discord.AuditLogAction.message_delete, limit=1):
            if entry.target.id == author.id:
                time_diff = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                if time_diff < 5:
                    desc += (
                        f"\n**deleted by:** {entry.user} ({entry.user.mention})\n"
                        f"**deleter id:** `{entry.user.id}`"
                    )
            break
    except Exception:
        pass

    log_embed = discord.Embed(
        title="message deleted",
        description=desc,
        color=EMBED_COLOR
    )
    log_embed.set_thumbnail(url=author.display_avatar.url)
    log_embed.set_footer(text=f"message id: {message.id}")
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(message.guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: bulk message delete
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_bulk_message_delete(messages):
    if not messages:
        return

    first = messages[0]
    if not first.guild:
        return

    authors = set()
    for m in messages:
        if m.author:
            authors.add(f"{m.author} (`{m.author.id}`)")

    log_embed = discord.Embed(
        title="bulk messages deleted",
        description=(
            f"**channel:** {first.channel.mention}\n"
            f"**channel id:** `{first.channel.id}`\n"
            f"**messages deleted:** {len(messages)}\n"
            f"**authors involved:** {', '.join(list(authors)[:10])}\n"
            f"**timestamp:** {ts()}"
        ),
        color=EMBED_COLOR
    )
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(first.guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: member update (roles, nickname, avatar)
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    guild = after.guild

    # role changes
    added_roles = [r for r in after.roles if r not in before.roles]
    removed_roles = [r for r in before.roles if r not in after.roles]

    if added_roles or removed_roles:
        desc = (
            f"**user:** {after} ({after.mention})\n"
            f"**user id:** `{after.id}`\n"
        )
        if added_roles:
            desc += f"**roles added:** {', '.join(r.mention for r in added_roles)}\n"
        if removed_roles:
            desc += f"**roles removed:** {', '.join(r.mention for r in removed_roles)}\n"

        # try to find who changed it
        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.member_role_update, limit=1):
                if entry.target.id == after.id:
                    time_diff = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                    if time_diff < 5:
                        desc += (
                            f"**changed by:** {entry.user} ({entry.user.mention})\n"
                            f"**reason:** {entry.reason or 'none'}"
                        )
                break
        except Exception:
            pass

        desc += f"\n**timestamp:** {ts()}"

        log_embed = discord.Embed(title="member roles updated", description=desc, color=EMBED_COLOR)
        log_embed.set_thumbnail(url=after.display_avatar.url)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, log_embed)

    # nickname changes
    if before.nick != after.nick:
        desc = (
            f"**user:** {after} ({after.mention})\n"
            f"**user id:** `{after.id}`\n"
            f"**before:** {before.nick or '*none*'}\n"
            f"**after:** {after.nick or '*none*'}\n"
        )

        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.member_update, limit=1):
                if entry.target.id == after.id:
                    time_diff = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                    if time_diff < 5:
                        desc += f"**changed by:** {entry.user} ({entry.user.mention})\n"
                break
        except Exception:
            pass

        desc += f"**timestamp:** {ts()}"

        log_embed = discord.Embed(title="nickname changed", description=desc, color=EMBED_COLOR)
        log_embed.set_thumbnail(url=after.display_avatar.url)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, log_embed)

    # timeout changes
    if before.timed_out_until != after.timed_out_until:
        if after.timed_out_until and after.timed_out_until > datetime.datetime.now(datetime.timezone.utc):
            desc = (
                f"**user:** {after} ({after.mention})\n"
                f"**user id:** `{after.id}`\n"
                f"**timeout until:** {ts(after.timed_out_until)} ({ts_relative(after.timed_out_until)})\n"
            )
        else:
            desc = (
                f"**user:** {after} ({after.mention})\n"
                f"**user id:** `{after.id}`\n"
                f"**timeout removed**\n"
            )

        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.member_update, limit=1):
                if entry.target.id == after.id:
                    time_diff = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                    if time_diff < 5:
                        desc += (
                            f"**by:** {entry.user} ({entry.user.mention})\n"
                            f"**reason:** {entry.reason or 'none'}\n"
                        )
                break
        except Exception:
            pass

        desc += f"**timestamp:** {ts()}"

        log_embed = discord.Embed(title="member timeout updated", description=desc, color=EMBED_COLOR)
        log_embed.set_thumbnail(url=after.display_avatar.url)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: bans
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    desc = (
        f"**user:** {user} ({user.mention})\n"
        f"**user id:** `{user.id}`\n"
        f"**account created:** {ts(user.created_at)} ({ts_relative(user.created_at)})\n"
    )

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=1):
            if entry.target.id == user.id:
                desc += (
                    f"**banned by:** {entry.user} ({entry.user.mention})\n"
                    f"**banner id:** `{entry.user.id}`\n"
                    f"**reason:** {entry.reason or 'none'}\n"
                )
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="member banned", description=desc, color=EMBED_COLOR)
    log_embed.set_thumbnail(url=user.display_avatar.url)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    desc = (
        f"**user:** {user} ({user.mention})\n"
        f"**user id:** `{user.id}`\n"
    )

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.unban, limit=1):
            if entry.target.id == user.id:
                desc += (
                    f"**unbanned by:** {entry.user} ({entry.user.mention})\n"
                    f"**reason:** {entry.reason or 'none'}\n"
                )
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="member unbanned", description=desc, color=EMBED_COLOR)
    log_embed.set_thumbnail(url=user.display_avatar.url)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: channel delete
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_guild_channel_delete(channel):
    if not channel.guild:
        return

    guild = channel.guild
    ch_type = str(channel.type).replace("ChannelType.", "")

    desc = (
        f"**name:** #{channel.name}\n"
        f"**channel id:** `{channel.id}`\n"
        f"**type:** {ch_type}\n"
        f"**category:** {channel.category.name if channel.category else 'none'}\n"
    )

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_delete, limit=1):
            if entry.target.id == channel.id:
                desc += (
                    f"**deleted by:** {entry.user} ({entry.user.mention})\n"
                    f"**deleter id:** `{entry.user.id}`\n"
                    f"**reason:** {entry.reason or 'none'}\n"
                )
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="channel deleted", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: channel update
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_guild_channel_update(before, after):
    if not after.guild:
        return

    guild = after.guild
    changes = []

    if before.name != after.name:
        changes.append(f"**name:** #{before.name} -> #{after.name}")

    if hasattr(before, "topic") and hasattr(after, "topic"):
        if before.topic != after.topic:
            changes.append(f"**topic:** {before.topic or '*none*'} -> {after.topic or '*none*'}")

    if hasattr(before, "slowmode_delay") and hasattr(after, "slowmode_delay"):
        if before.slowmode_delay != after.slowmode_delay:
            changes.append(f"**slowmode:** {before.slowmode_delay}s -> {after.slowmode_delay}s")

    if hasattr(before, "nsfw") and hasattr(after, "nsfw"):
        if before.nsfw != after.nsfw:
            changes.append(f"**nsfw:** {before.nsfw} -> {after.nsfw}")

    if before.category != after.category:
        changes.append(
            f"**category:** {before.category.name if before.category else 'none'} -> "
            f"{after.category.name if after.category else 'none'}"
        )

    if before.overwrites != after.overwrites:
        changes.append("**permissions:** permission overwrites changed")

    if not changes:
        return

    desc = (
        f"**channel:** {after.mention}\n"
        f"**channel id:** `{after.id}`\n"
        + "\n".join(changes) + "\n"
    )

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_update, limit=1):
            if entry.target.id == after.id:
                time_diff = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                if time_diff < 5:
                    desc += (
                        f"**changed by:** {entry.user} ({entry.user.mention})\n"
                        f"**reason:** {entry.reason or 'none'}\n"
                    )
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="channel updated", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: role create/delete/update
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_guild_role_create(role: discord.Role):
    guild = role.guild
    desc = (
        f"**role:** {role.mention}\n"
        f"**role name:** {role.name}\n"
        f"**role id:** `{role.id}`\n"
        f"**color:** {str(role.color)}\n"
        f"**hoisted:** {role.hoist}\n"
        f"**mentionable:** {role.mentionable}\n"
        f"**position:** {role.position}\n"
    )

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.role_create, limit=1):
            if entry.target.id == role.id:
                desc += f"**created by:** {entry.user} ({entry.user.mention})\n"
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="role created", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    desc = (
        f"**role name:** {role.name}\n"
        f"**role id:** `{role.id}`\n"
        f"**color:** {str(role.color)}\n"
        f"**had {len(role.members)} members**\n"
    )

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.role_delete, limit=1):
            if entry.target.id == role.id:
                desc += (
                    f"**deleted by:** {entry.user} ({entry.user.mention})\n"
                    f"**reason:** {entry.reason or 'none'}\n"
                )
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="role deleted", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    guild = after.guild
    changes = []

    if before.name != after.name:
        changes.append(f"**name:** {before.name} -> {after.name}")
    if before.color != after.color:
        changes.append(f"**color:** {before.color} -> {after.color}")
    if before.hoist != after.hoist:
        changes.append(f"**hoisted:** {before.hoist} -> {after.hoist}")
    if before.mentionable != after.mentionable:
        changes.append(f"**mentionable:** {before.mentionable} -> {after.mentionable}")
    if before.permissions != after.permissions:
        added_perms = [p for p, v in after.permissions if v and not dict(before.permissions).get(p, False)]
        removed_perms = [p for p, v in before.permissions if v and not dict(after.permissions).get(p, False)]
        if added_perms:
            changes.append(f"**permissions added:** {', '.join(added_perms)}")
        if removed_perms:
            changes.append(f"**permissions removed:** {', '.join(removed_perms)}")

    if not changes:
        return

    desc = (
        f"**role:** {after.mention}\n"
        f"**role id:** `{after.id}`\n"
        + "\n".join(changes) + "\n"
    )

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.role_update, limit=1):
            if entry.target.id == after.id:
                time_diff = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                if time_diff < 5:
                    desc += f"**changed by:** {entry.user} ({entry.user.mention})\n"
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="role updated", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: voice state
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):
    guild = member.guild

    if not before.channel and after.channel:
        title = "voice channel joined"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**user id:** `{member.id}`\n"
            f"**channel:** {after.channel.mention} ({after.channel.name})\n"
            f"**channel id:** `{after.channel.id}`\n"
            f"**self muted:** {after.self_mute}\n"
            f"**self deafened:** {after.self_deaf}\n"
            f"**timestamp:** {ts()}"
        )
    elif before.channel and not after.channel:
        title = "voice channel left"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**user id:** `{member.id}`\n"
            f"**channel:** {before.channel.mention} ({before.channel.name})\n"
            f"**channel id:** `{before.channel.id}`\n"
            f"**timestamp:** {ts()}"
        )
    elif before.channel and after.channel and before.channel.id != after.channel.id:
        title = "voice channel switched"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**user id:** `{member.id}`\n"
            f"**from:** {before.channel.mention} ({before.channel.name})\n"
            f"**to:** {after.channel.mention} ({after.channel.name})\n"
            f"**timestamp:** {ts()}"
        )
    elif before.self_mute != after.self_mute:
        title = "voice self mute toggled"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**self muted:** {after.self_mute}\n"
            f"**channel:** {after.channel.mention}\n"
            f"**timestamp:** {ts()}"
        )
    elif before.self_deaf != after.self_deaf:
        title = "voice self deafen toggled"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**self deafened:** {after.self_deaf}\n"
            f"**channel:** {after.channel.mention}\n"
            f"**timestamp:** {ts()}"
        )
    elif before.mute != after.mute:
        title = "voice server mute toggled"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**server muted:** {after.mute}\n"
            f"**channel:** {after.channel.mention}\n"
            f"**timestamp:** {ts()}"
        )
    elif before.deaf != after.deaf:
        title = "voice server deafen toggled"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**server deafened:** {after.deaf}\n"
            f"**channel:** {after.channel.mention}\n"
            f"**timestamp:** {ts()}"
        )
    elif before.self_stream != after.self_stream:
        title = "voice stream toggled"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**streaming:** {after.self_stream}\n"
            f"**channel:** {after.channel.mention}\n"
            f"**timestamp:** {ts()}"
        )
    elif before.self_video != after.self_video:
        title = "voice camera toggled"
        desc = (
            f"**user:** {member} ({member.mention})\n"
            f"**camera on:** {after.self_video}\n"
            f"**channel:** {after.channel.mention}\n"
            f"**timestamp:** {ts()}"
        )
    else:
        return

    log_embed = discord.Embed(title=title, description=desc, color=EMBED_COLOR)
    log_embed.set_thumbnail(url=member.display_avatar.url)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: emoji create/delete
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_guild_emojis_update(guild, before, after):
    before_ids = {e.id for e in before}
    after_ids = {e.id for e in after}

    added = [e for e in after if e.id not in before_ids]
    removed = [e for e in before if e.id not in after_ids]

    for emoji in added:
        desc = (
            f"**name:** {emoji.name}\n"
            f"**emoji id:** `{emoji.id}`\n"
            f"**animated:** {emoji.animated}\n"
            f"**url:** [link]({emoji.url})\n"
            f"**timestamp:** {ts()}"
        )
        log_embed = discord.Embed(title="emoji added", description=desc, color=EMBED_COLOR)
        log_embed.set_thumbnail(url=str(emoji.url))
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, log_embed)

    for emoji in removed:
        desc = (
            f"**name:** {emoji.name}\n"
            f"**emoji id:** `{emoji.id}`\n"
            f"**animated:** {emoji.animated}\n"
            f"**timestamp:** {ts()}"
        )
        log_embed = discord.Embed(title="emoji removed", description=desc, color=EMBED_COLOR)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: sticker create/delete
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_guild_stickers_update(guild, before, after):
    before_ids = {s.id for s in before}
    after_ids = {s.id for s in after}

    added = [s for s in after if s.id not in before_ids]
    removed = [s for s in before if s.id not in after_ids]

    for sticker in added:
        desc = (
            f"**name:** {sticker.name}\n"
            f"**sticker id:** `{sticker.id}`\n"
            f"**description:** {sticker.description or 'none'}\n"
            f"**timestamp:** {ts()}"
        )
        log_embed = discord.Embed(title="sticker added", description=desc, color=EMBED_COLOR)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, log_embed)

    for sticker in removed:
        desc = (
            f"**name:** {sticker.name}\n"
            f"**sticker id:** `{sticker.id}`\n"
            f"**timestamp:** {ts()}"
        )
        log_embed = discord.Embed(title="sticker removed", description=desc, color=EMBED_COLOR)
        log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        await send_log(guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: server update
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    changes = []

    if before.name != after.name:
        changes.append(f"**name:** {before.name} -> {after.name}")
    if before.icon != after.icon:
        changes.append("**icon:** changed")
    if before.banner != after.banner:
        changes.append("**banner:** changed")
    if before.description != after.description:
        changes.append(f"**description:** {before.description or '*none*'} -> {after.description or '*none*'}")
    if before.verification_level != after.verification_level:
        changes.append(f"**verification level:** {before.verification_level} -> {after.verification_level}")
    if before.default_notifications != after.default_notifications:
        changes.append(f"**default notifications:** {before.default_notifications} -> {after.default_notifications}")
    if before.explicit_content_filter != after.explicit_content_filter:
        changes.append(f"**content filter:** {before.explicit_content_filter} -> {after.explicit_content_filter}")
    if before.afk_channel != after.afk_channel:
        changes.append(
            f"**afk channel:** {before.afk_channel or 'none'} -> {after.afk_channel or 'none'}"
        )
    if before.system_channel != after.system_channel:
        changes.append(
            f"**system channel:** {before.system_channel or 'none'} -> {after.system_channel or 'none'}"
        )
    if before.vanity_url_code != after.vanity_url_code:
        changes.append(f"**vanity url:** {before.vanity_url_code or 'none'} -> {after.vanity_url_code or 'none'}")

    if not changes:
        return

    desc = "\n".join(changes) + "\n"

    try:
        async for entry in after.audit_logs(action=discord.AuditLogAction.guild_update, limit=1):
            desc += (
                f"\n**changed by:** {entry.user} ({entry.user.mention})\n"
                f"**reason:** {entry.reason or 'none'}\n"
            )
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="server updated", description=desc, color=EMBED_COLOR)
    if after.icon:
        log_embed.set_thumbnail(url=after.icon.url)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(after, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: invite create/delete
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_invite_create(invite: discord.Invite):
    desc = (
        f"**code:** {invite.code}\n"
        f"**url:** {invite.url}\n"
        f"**channel:** {invite.channel.mention}\n"
        f"**created by:** {invite.inviter} ({invite.inviter.mention})\n"
        f"**max uses:** {invite.max_uses or 'unlimited'}\n"
        f"**max age:** {invite.max_age or 'never expires'}s\n"
        f"**temporary:** {invite.temporary}\n"
        f"**timestamp:** {ts()}"
    )

    log_embed = discord.Embed(title="invite created", description=desc, color=EMBED_COLOR)
    if invite.inviter:
        log_embed.set_thumbnail(url=invite.inviter.display_avatar.url)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(invite.guild, log_embed)


@bot.event
async def on_invite_delete(invite: discord.Invite):
    desc = (
        f"**code:** {invite.code}\n"
        f"**channel:** {invite.channel.mention}\n"
        f"**uses:** {invite.uses}\n"
        f"**timestamp:** {ts()}"
    )

    log_embed = discord.Embed(title="invite deleted", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(invite.guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: thread create/delete
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_thread_create(thread):
    desc = (
        f"**name:** {thread.name}\n"
        f"**thread id:** `{thread.id}`\n"
        f"**parent channel:** {thread.parent.mention}\n"
        f"**owner:** <@{thread.owner_id}>\n"
        f"**auto archive:** {thread.auto_archive_duration} minutes\n"
        f"**timestamp:** {ts()}"
    )

    log_embed = discord.Embed(title="thread created", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(thread.guild, log_embed)


@bot.event
async def on_thread_delete(thread):
    desc = (
        f"**name:** {thread.name}\n"
        f"**thread id:** `{thread.id}`\n"
        f"**parent channel:** #{thread.parent.name if thread.parent else 'unknown'}\n"
        f"**timestamp:** {ts()}"
    )

    log_embed = discord.Embed(title="thread deleted", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(thread.guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: webhooks
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_webhooks_update(channel):
    desc = (
        f"**channel:** {channel.mention}\n"
        f"**channel id:** `{channel.id}`\n"
    )

    try:
        async for entry in channel.guild.audit_logs(limit=1):
            if entry.action in (
                discord.AuditLogAction.webhook_create,
                discord.AuditLogAction.webhook_delete,
                discord.AuditLogAction.webhook_update
            ):
                action_name = str(entry.action).split(".")[-1].replace("_", " ")
                desc += (
                    f"**action:** {action_name}\n"
                    f"**by:** {entry.user} ({entry.user.mention})\n"
                )
            break
    except Exception:
        pass

    desc += f"**timestamp:** {ts()}"

    log_embed = discord.Embed(title="webhook updated", description=desc, color=EMBED_COLOR)
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(channel.guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  logging: reactions
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    if not reaction.message.guild:
        return

    log_embed = discord.Embed(
        title="reaction added",
        description=(
            f"**user:** {user} ({user.mention})\n"
            f"**emoji:** {reaction.emoji}\n"
            f"**channel:** {reaction.message.channel.mention}\n"
            f"**message:** [jump]({reaction.message.jump_url})\n"
            f"**timestamp:** {ts()}"
        ),
        color=EMBED_COLOR
    )
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(reaction.message.guild, log_embed)


@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return
    if not reaction.message.guild:
        return

    log_embed = discord.Embed(
        title="reaction removed",
        description=(
            f"**user:** {user} ({user.mention})\n"
            f"**emoji:** {reaction.emoji}\n"
            f"**channel:** {reaction.message.channel.mention}\n"
            f"**message:** [jump]({reaction.message.jump_url})\n"
            f"**timestamp:** {ts()}"
        ),
        color=EMBED_COLOR
    )
    log_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    await send_log(reaction.message.guild, log_embed)


# ═════════════════════════════════════════════════════════════
#  error handling
# ═════════════════════════════════════════════════════════════
@bot.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"error in {event}: {traceback.format_exc()}")


# ═════════════════════════════════════════════════════════════
#  run
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    bot.run(TOKEN)
