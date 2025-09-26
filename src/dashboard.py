import os
import json
import asyncio
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "dashboard_state.json"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------------ Storage helpers ------------------------

def load_state():
    if not os.path.exists(DATA_FILE):
        return {
            "dashboard": {
                "channel_id": None,
                "message_id": None,
            },
            "joysticks": {}  # { "1": {"status": "...", "by": "user#0001", "since": "...", "notes": "", "session": False}, ... }
        }
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

state = load_state()
update_lock = asyncio.Lock()
update_scheduled = False

# ------------------------ Rendering ------------------------

def render_dashboard():
    # Produce a nice monospaced table-like block
    rows = []
    rows.append("Joystick # | Status      | Session | Since                | Notes")
    rows.append("-----------+-------------+---------+----------------------+------")
    for jid in sorted(state["joysticks"].keys(), key=lambda x: int(x)):
        j = state["joysticks"][jid]
        status = j.get("status", "Working")
        session = "Yes" if j.get("session") else "No"
        since = j.get("since", "-")
        notes = (j.get("notes") or "")[:40]
        rows.append(f"{jid:<10} | {status:<11} | {session:^7} | {since:<20} | {notes}")
    if len(rows) == 2:
        rows.append("(no joysticks tracked yet)")
    return "```text\n" + "\n".join(rows) + "\n```"

async def schedule_dashboard_update():
    global update_scheduled
    async with update_lock:
        if update_scheduled:
            return
        update_scheduled = True

    # Small debounce window
    await asyncio.sleep(1.0)

    async with update_lock:
        update_scheduled = False

    ch_id = state["dashboard"]["channel_id"]
    msg_id = state["dashboard"]["message_id"]
    if not ch_id or not msg_id:
        return

    channel = bot.get_channel(ch_id)
    if not channel:
        channel = await bot.fetch_channel(ch_id)

    try:
        msg = await channel.fetch_message(msg_id)
        await msg.edit(content=render_dashboard())
    except discord.HTTPException as e:
        print("Failed to edit dashboard:", e)

def set_status(jid: str, status: str, by: str, notes: str | None = None, session: bool | None = None):
    j = state["joysticks"].setdefault(jid, {})
    j["status"] = status
    j["by"] = by
    j["since"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    if notes is not None:
        j["notes"] = notes
    if session is not None:
        j["session"] = session
    save_state(state)

# ------------------------ Slash commands ------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        await bot.tree.sync()
        print("Synced commands.")
    except Exception as e:
        print("Sync error:", e)

@bot.tree.command(description="Create the dashboard message here (or update where it lives).")
@app_commands.default_permissions(administrator=True)
async def dashboard_init(interaction: discord.Interaction):
    # Create or re-create the dashboard in the current channel
    content = render_dashboard()
    msg = await interaction.channel.send(content)
    state["dashboard"]["channel_id"] = interaction.channel.id
    state["dashboard"]["message_id"] = msg.id
    save_state(state)
    await interaction.response.send_message("Dashboard initialized & saved. Pin it if you like!", ephemeral=True)

@bot.tree.command(description="Mark joystick as being used by admins/devs")
@app_commands.describe(id="Joystick number")
async def devuse(interaction: discord.Interaction, id: int):
    set_status(str(id), "Admin Use", str(interaction.user), notes=None)
    await interaction.response.send_message(f"Joystick {id} marked as Admin Use.", ephemeral=True)
    await schedule_dashboard_update()

@bot.tree.command(description="Mark joystick as broken")
@app_commands.describe(id="Joystick number", reason="Optional reason")
async def broken(interaction: discord.Interaction, id: int, reason: str | None = None):
    set_status(str(id), "Broken", str(interaction.user), notes=reason)
    await interaction.response.send_message(f"Joystick {id} marked Broken.", ephemeral=True)
    await schedule_dashboard_update()

@bot.tree.command(description="Mark joystick as fixed/working")
@app_commands.describe(id="Joystick number")
async def fixed(interaction: discord.Interaction, id: int):
    set_status(str(id), "Working", str(interaction.user))
    await interaction.response.send_message(f"Joystick {id} marked Working.", ephemeral=True)
    await schedule_dashboard_update()

@bot.tree.command(description="Start an operating session")
@app_commands.describe(id="Joystick number")
async def start_session(interaction: discord.Interaction, id: int):
    j = state["joysticks"].setdefault(str(id), {})
    j["session"] = True
    j["status"] = "Operated"
    j["since"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    j["by"] = str(interaction.user)
    save_state(state)
    await interaction.response.send_message(f"Session started for joystick {id}.", ephemeral=True)
    await schedule_dashboard_update()

@bot.tree.command(description="Stop an operating session")
@app_commands.describe(id="Joystick number")
async def stop_session(interaction: discord.Interaction, id: int):
    j = state["joysticks"].setdefault(str(id), {})
    j["session"] = False
    # optionally revert to Working unless Broken/AdminUse
    if j.get("status") == "Operated":
        j["status"] = "Working"
    j["since"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    j["by"] = str(interaction.user)
    save_state(state)
    await interaction.response.send_message(f"Session stopped for joystick {id}.", ephemeral=True)
    await schedule_dashboard_update()

bot.run(TOKEN)
