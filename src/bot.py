import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
import json
import logging
from datetime import datetime, time, timedelta, timezone
import pytz
from dataclasses import dataclass
from typing import Optional, Dict, List
import sys
import csv
import re
# Add the src directory to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
from scheduler.scheduler import ROBOT_IDS
from dashboard_manager import EquipmentDashboard
from dotenv import load_dotenv

# Task assignment system imports
from database import init_database, check_database_connection, get_db_session
from models import get_or_create_user, get_settings
from assignment_scheduler import AssignmentScheduler

# Load environment variables
load_dotenv()

# Check required environment variables
required_env_vars = ['DISCORD_TOKEN']
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    print(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
    print("Please create a .env file with the required variables.")
    exit(1)

# Discord bot setup
TOKEN = os.getenv('DISCORD_TOKEN')
TIMEZONE = pytz.timezone('America/Los_Angeles')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize bot with intents
intents = discord.Intents.default()
intents.message_content = True  # For reading message content
intents.members = True  # For accessing member information
bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize Equipment Dashboard manager
equipment_dashboard = EquipmentDashboard()

# Initialize Assignment Scheduler (will be set after bot is ready)
assignment_scheduler = None

# Nickname storage file
NICKNAME_STORAGE_FILE = "nickname_storage.json"

def load_nickname_storage():
    """Load stored nickname data from file"""
    try:
        if os.path.exists(NICKNAME_STORAGE_FILE):
            with open(NICKNAME_STORAGE_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Failed to load nickname storage: {e}")
        return {}

def save_nickname_storage(data):
    """Save nickname data to file"""
    try:
        with open(NICKNAME_STORAGE_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save nickname storage: {e}")

# Initialize nickname storage
nickname_storage = load_nickname_storage()

# Channel trigger configurations
CHANNEL_CONFIGS = {
    'gello-history': [
        {
            'start_pattern': r'starting gello (\d+)',
            'stop_pattern': r'stopping gello (\d+)',
            'role_name': 'Piloting',
            'nickname_format': 'Gello {number}',
            'action_log': 'gello_piloting'
        },
        {
            'start_pattern': r'fixing (.+)',
            'stop_pattern': r'done',
            'role_name': 'Fixing',
            'nickname_format': 'Fixing {item}',
            'action_log': 'fixing'
        }
    ],
    'breaks': [
        {
            'start_pattern': r'(break|lunch)',
            'stop_pattern': r'back',
            'role_name': 'On Break',
            'nickname_format': '{break_type}',
            'duration_minutes': {'break': 10, 'lunch': 60},
            'action_log': 'break'
        }
    ],
    'shift-changes': [
        {
            'start_pattern': r'starting shift',
            'stop_pattern': r'stopping shift',
            'role_name': 'Current Shift',
            'nickname_format': 'On Shift',
            'action_log': 'shift_status'
        }
    ]
}

async def process_channel_triggers(message):
    """Process message triggers for role/nickname management"""
    try:
        channel_name = message.channel.name
        if channel_name not in CHANNEL_CONFIGS:
            return
        
        content = message.content.lower().strip()
        user = message.author
        guild = message.guild
        
        for config in CHANNEL_CONFIGS[channel_name]:
            # Check start patterns
            start_match = re.search(config['start_pattern'], content, re.IGNORECASE)
            if start_match:
                # Initialize result variable
                result = None
                nickname_tag = None
                
                # Extract parameters from the match
                if 'number' in config['nickname_format']:
                    # For Gello operations
                    number = start_match.group(1)
                    nickname_tag = config['nickname_format'].replace('{number}', number)
                    result = await manage_role_and_nickname(
                        guild, user, 'start', config['role_name'], nickname_tag
                    )
                    
                elif 'item' in config['nickname_format']:
                    # For fixing operations
                    item = start_match.group(1)
                    nickname_tag = config['nickname_format'].replace('{item}', item)
                    result = await manage_role_and_nickname(
                        guild, user, 'start', config['role_name'], nickname_tag
                    )
                    
                elif 'break_type' in config['nickname_format']:
                    # For break operations - simple break tracking without quota checking
                    break_type = start_match.group(1)
                    duration = config.get('duration_minutes', {}).get(break_type, 10)
                    nickname_tag = break_type
                    
                    # Use duration for breaks
                    result = await manage_role_and_nickname(
                        guild, user, 'start', config['role_name'], 
                        nickname_tag, duration
                    )
                else:
                    # Default case
                    nickname_tag = config['nickname_format']
                    result = await manage_role_and_nickname(
                        guild, user, 'start', config['role_name'], nickname_tag
                    )
                
                # Log the action
                if result and result['success']:
                    details = nickname_tag if nickname_tag else config['role_name']
                    log_operator_action(
                        str(user.id),
                        user.display_name,
                        f"{config['action_log']}_start",
                        details
                    )
                    
                    # React with appropriate emoji based on action type
                    if config['role_name'] == 'Piloting':
                        await message.add_reaction('🚀')
                    elif config['role_name'] == 'On Break':
                        emoji = '☕' if nickname_tag == 'break' else '🍽️'
                        await message.add_reaction(emoji)
                    elif config['role_name'] == 'Fixing':
                        await message.add_reaction('🔧')
                    elif config['role_name'] == 'Current Shift':
                        await message.add_reaction('🎯')
                    else:
                        await message.add_reaction('✅')
                elif result:
                    logger.warning(f"Failed to start {config['role_name']} for {user.display_name}: {result['message']}")
                else:
                    logger.error(f"No result returned for {config['role_name']} operation for {user.display_name}")
                
                return  # Process only first matching pattern
            
            # Check stop patterns
            stop_match = re.search(config['stop_pattern'], content, re.IGNORECASE)
            if stop_match:
                result = await manage_role_and_nickname(
                    guild, user, 'stop', config['role_name']
                )
                
                # Log the action
                if result['success']:
                    log_operator_action(
                        str(user.id),
                        user.display_name,
                        f"{config['action_log']}_stop",
                        config['role_name']
                    )
                    # React with appropriate stop emoji
                    if config['role_name'] == 'On Break':
                        await message.add_reaction('✅')
                    elif config['role_name'] == 'Current Shift':
                        await message.add_reaction('👋')
                    else:
                        await message.add_reaction('🛑')
                else:
                    logger.warning(f"Failed to stop {config['role_name']} for {user.display_name}: {result['message']}")
                
                return  # Process only first matching pattern
                
    except Exception as e:
        logger.error(f"Error processing channel triggers: {e}")

# Role checking helper functions
def has_required_role(interaction, required_roles):
    """Check if user has any of the required roles"""
    user_roles = [role.name for role in interaction.user.roles]
    return any(role in user_roles for role in required_roles)

def log_operator_action(discord_id: str, username: str, action: str, details: str = ""):
    """Log operator actions to CSV file"""
    try:
        log_file = "operator_logs.csv"
        timestamp = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        
        # Create file with headers if it doesn't exist
        if not os.path.exists(log_file):
            with open(log_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Discord_ID", "Username", "Action", "Details"])
        
        # Append the log entry
        with open(log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, discord_id, username, action, details])
            
        logger.info(f"Logged action: {username} - {action}")
        
    except Exception as e:
        logger.error(f"Failed to log operator action: {e}")

async def rebuild_nickname_from_active_roles(guild, user, user_key):
    """
    Rebuild user's nickname based on their currently active roles
    This handles cases where user stops one activity but still has others active
    """
    global nickname_storage
    
    try:
        if user_key not in nickname_storage:
            return False
        
        stored_info = nickname_storage[user_key]
        base_nick = stored_info['original_nickname']
        active_roles = stored_info.get('roles', [])
        
        if not active_roles:
            # No active roles, restore original nickname
            await user.edit(nick=base_nick if base_nick != user.name else None)
            return True
        
        # Priority system for role display (most important first)
        role_priority = {
            'On Break': 1,      # Breaks are temporary and high priority
            'Piloting': 2,      # Piloting operations
            'Fixing': 3,        # Fixing tasks
            'Current Shift': 4  # General shift status
        }
        
        # Find the highest priority active role
        primary_role = None
        highest_priority = 999
        
        for role_name in active_roles:
            priority = role_priority.get(role_name, 5)
            if priority < highest_priority:
                highest_priority = priority
                primary_role = role_name
        
        if not primary_role:
            # Fallback to original nickname
            await user.edit(nick=base_nick if base_nick != user.name else None)
            return True
        
        # Build nickname based on primary role
        # For breaks, try to reconstruct ETA if we have timestamp info
        if primary_role == 'On Break' and 'current_tag' in stored_info and 'timestamp' in stored_info:
            try:
                # Try to reconstruct break ETA
                break_start = datetime.fromisoformat(stored_info['timestamp'])
                # Default to 10 minutes if we can't determine break type
                duration = 10
                eta_time = (break_start + timedelta(minutes=duration)).strftime("%H:%M")
                new_nick = f"[break - ETA: {eta_time}] {base_nick}"
            except:
                # Fallback if timestamp parsing fails
                new_nick = f"[{primary_role}] {base_nick}"
        else:
            # For other roles, use simple format
            display_tag = stored_info.get('current_tag', primary_role)
            new_nick = f"[{display_tag}] {base_nick}"
        
        # Handle Discord's 32 character limit
        if len(new_nick) > 32:
            display_tag = stored_info.get('current_tag', primary_role)
            tag_part = f"[{display_tag}] "
            available_chars = 32 - len(tag_part)
            
            if available_chars > 0:
                truncated_base = base_nick[:available_chars].strip()
                new_nick = tag_part + truncated_base
            else:
                # If tag is too long, just use the tag without base nickname
                new_nick = tag_part.strip()[:32]
        
        await user.edit(nick=new_nick)
        logger.info(f"Rebuilt nickname for {user.display_name}: {new_nick}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to rebuild nickname for {user.display_name}: {e}")
        return False

async def manage_role_and_nickname(guild, user, action_type, role_name, nickname_tag=None, duration_minutes=None):
    """
    Generalized function to manage roles and nicknames based on triggers
    
    Args:
        guild: Discord guild object
        user: Discord user object
        action_type: 'start' or 'stop' 
        role_name: Name of the role to assign/remove
        nickname_tag: Tag to add to nickname (e.g., "Gello 1", "break")
        duration_minutes: For time-based tags like breaks (optional)
    
    Returns:
        dict: Result with success status and message
    """
    global nickname_storage
    
    try:
        user_key = f"{guild.id}_{user.id}"
        
        if action_type == 'start':
            # Store original nickname if not already stored
            if user_key not in nickname_storage:
                nickname_storage[user_key] = {
                    'original_nickname': user.display_name,
                    'guild_id': guild.id,
                    'user_id': user.id,
                    'roles': []
                }
            
            # Find the role
            role = discord.utils.get(guild.roles, name=role_name)
            if not role:
                try:
                    # Create the role if it doesn't exist
                    role = await guild.create_role(name=role_name, mentionable=True)
                    logger.info(f"Created new role: {role_name}")
                except Exception as e:
                    logger.error(f"Failed to create role {role_name}: {e}")
                    return {"success": False, "message": f"Failed to create role {role_name}"}
            
            # Add role to user
            try:
                await user.add_roles(role)
                
                # Track this role assignment
                if role_name not in nickname_storage[user_key]['roles']:
                    nickname_storage[user_key]['roles'].append(role_name)
                
            except Exception as e:
                logger.error(f"Failed to add role {role_name} to {user.display_name}: {e}")
                return {"success": False, "message": f"Failed to add role"}
            
            # Update nickname
            if nickname_tag:
                try:
                    # Get base nickname (remove any existing tags)
                    current_nick = user.display_name
                    base_nick = nickname_storage[user_key]['original_nickname']
                    
                    # Create new nickname with tag as prefix
                    if duration_minutes:
                        pst_now = datetime.now(TIMEZONE)
                        eta_time = (pst_now + timedelta(minutes=duration_minutes)).strftime("%H:%M")
                        new_nick = f"[{nickname_tag} - ETA: {eta_time}] {base_nick}"
                    else:
                        new_nick = f"[{nickname_tag}] {base_nick}"
                    
                    # Discord nickname limit is 32 characters
                    if len(new_nick) > 32:
                        # Calculate space needed for the tag part
                        if duration_minutes:
                            pst_now = datetime.now(TIMEZONE)
                            eta_time = (pst_now + timedelta(minutes=duration_minutes)).strftime("%H:%M")
                            tag_part = f"[{nickname_tag} - ETA: {eta_time}] "
                        else:
                            tag_part = f"[{nickname_tag}] "
                        
                        # Calculate available space for base nickname
                        available_chars = 32 - len(tag_part)
                        
                        if available_chars > 0:
                            truncated_base = base_nick[:available_chars].strip()
                            new_nick = tag_part + truncated_base
                        else:
                            # If tag is too long, just use the tag without base nickname
                            new_nick = tag_part.strip()[:32]
                    
                    await user.edit(nick=new_nick)
                    
                    # Store current tag info
                    nickname_storage[user_key]['current_tag'] = nickname_tag
                    nickname_storage[user_key]['timestamp'] = datetime.now(TIMEZONE).isoformat()
                    
                except Exception as e:
                    logger.error(f"Failed to update nickname for {user.display_name}: {e}")
                    # Role was added successfully, so this is partial success
                    save_nickname_storage(nickname_storage)
                    return {"success": True, "message": f"Role added but nickname update failed"}
            
            save_nickname_storage(nickname_storage)
            return {"success": True, "message": f"Successfully started {role_name} with tag [{nickname_tag}]"}
            
        elif action_type == 'stop':
            # Check if user has stored info
            if user_key not in nickname_storage:
                return {"success": False, "message": "No active session found"}
            
            stored_info = nickname_storage[user_key]
            
            # Remove role
            role = discord.utils.get(guild.roles, name=role_name)
            if role and role in user.roles:
                try:
                    await user.remove_roles(role)
                    # Remove from tracked roles
                    if role_name in stored_info['roles']:
                        stored_info['roles'].remove(role_name)
                except Exception as e:
                    logger.error(f"Failed to remove role {role_name} from {user.display_name}: {e}")
                    return {"success": False, "message": f"Failed to remove role"}
            
            # Handle nickname updates based on remaining active roles
            try:
                if len(stored_info['roles']) == 0:
                    # No other roles active, restore original nickname
                    original_nick = stored_info['original_nickname']
                    await user.edit(nick=original_nick if original_nick != user.name else None)
                    
                    # Clean up storage completely
                    del nickname_storage[user_key]
                    save_nickname_storage(nickname_storage)
                    logger.info(f"Restored original nickname for {user.display_name}: {original_nick}")
                    
                else:
                    # Other roles still active, rebuild nickname based on remaining roles
                    success = await rebuild_nickname_from_active_roles(guild, user, user_key)
                    if success:
                        save_nickname_storage(nickname_storage)
                        logger.info(f"Rebuilt nickname for {user.display_name} with remaining roles: {stored_info['roles']}")
                    else:
                        # Fallback: just clear current tag info
                        if 'current_tag' in stored_info:
                            del stored_info['current_tag']
                        save_nickname_storage(nickname_storage)
                        logger.warning(f"Failed to rebuild nickname for {user.display_name}, cleared current tag")
                    
            except Exception as e:
                logger.error(f"Failed to update nickname for {user.display_name}: {e}")
                return {"success": True, "message": f"Role removed but nickname update failed"}
            
            return {"success": True, "message": f"Successfully stopped {role_name}"}
            
    except Exception as e:
        logger.error(f"Error in manage_role_and_nickname: {e}")
        return {"success": False, "message": f"An error occurred: {str(e)}"}

@bot.event
async def on_ready():
    """Called when the bot is ready"""
    logger.info(f'LakBay Bot is ready! Logged in as {bot.user}')
    
    # Initialize database
    logger.info("Initializing database...")
    if not check_database_connection():
        logger.error("Database connection failed - bot may not function properly")
    else:
        if not init_database():
            logger.error("Database initialization failed - bot may not function properly")
        else:
            logger.info("Database initialized successfully")
    
    # Initialize assignment scheduler
    global assignment_scheduler
    try:
        assignment_scheduler = AssignmentScheduler(bot)
        await assignment_scheduler.start()
        logger.info("Assignment scheduler started successfully")
    except Exception as e:
        logger.error(f"Failed to start assignment scheduler: {e}")
    
    # Sync slash commands to all guilds the bot is in
    try:
        # Global sync (takes up to 1 hour)
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} global command(s)")
        
        # Also sync to each guild for immediate availability
        for guild in bot.guilds:
            guild_synced = await bot.tree.sync(guild=guild)
            logger.info(f"Synced {len(guild_synced)} command(s) to guild: {guild.name}")
            
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")
    
    # Initialize equipment dashboard if needed
    try:
        for guild in bot.guilds:
            # Find robot-state channel
            robot_state_channel = None
            for channel in guild.channels:
                if channel.name == 'robot-state' and isinstance(channel, discord.TextChannel):
                    robot_state_channel = channel
                    break
            
            if robot_state_channel:
                # Check if we have an existing dashboard message
                if equipment_dashboard.dashboard_message_id:
                    try:
                        # Verify the message still exists
                        await robot_state_channel.fetch_message(equipment_dashboard.dashboard_message_id)
                        logger.info("Found existing equipment dashboard")
                    except discord.NotFound:
                        # Message was deleted, create new one
                        await equipment_dashboard.create_or_update_dashboard(robot_state_channel)
                        logger.info("Recreated equipment dashboard")
                else:
                    # No existing dashboard, create one
                    await equipment_dashboard.create_or_update_dashboard(robot_state_channel)
                    logger.info("Created initial equipment dashboard")
                    
    except Exception as e:
        logger.error(f"Failed to initialize equipment dashboard: {e}")

@bot.event
async def on_message(message):
    """Handle incoming messages"""
    if message.author.bot:
        return

    # Process commands first
    await bot.process_commands(message)

    # Handle equipment updates in #equipment-updates channel
    if message.channel.name == 'equipment-updates':
        await equipment_dashboard.handle_equipment_update(message)

    # Process role/nickname triggers for configured channels
    await process_channel_triggers(message)

    # Handle shift-changes channel (legacy system)
    if message.channel.name == 'shift-changes':
        await handle_shift_changes(message)

async def handle_shift_changes(message):
    """Handle start/stop keywords in shift-changes channel"""
    try:
        content = message.content.lower()
        mentions = message.mentions
        
        # Skip if no mentions
        if not mentions:
            return
        
        # Check for start keyword
        if 'start' in content:
            for mentioned_user in mentions:
                # Skip Admins/Managers
                user_roles = [role.name for role in mentioned_user.roles]
                if any(role in user_roles for role in ["Admin", "Manager"]):
                    continue
                
                # Log the action
                log_operator_action(
                    str(message.author.id),
                    message.author.display_name,
                    "shift_start",
                    f"Started shift for {mentioned_user.display_name}"
                )
            
            await message.add_reaction('🎯')
            
        # Check for stop keyword
        elif 'stop' in content:
            for mentioned_user in mentions:
                # Skip Admins/Managers
                user_roles = [role.name for role in mentioned_user.roles]
                if any(role in user_roles for role in ["Admin", "Manager"]):
                    continue
                
                # Log the action
                log_operator_action(
                    str(message.author.id),
                    message.author.display_name,
                    "shift_end",
                    f"Ended shift for {mentioned_user.display_name}"
                )
            
            await message.add_reaction('👋')
            
    except Exception as e:
        logger.error(f"Error in handle_shift_changes: {e}")


# Equipment dashboard command
@bot.tree.command(name="dashboard", description="[Admin/Manager] Create or refresh the equipment status dashboard")
async def dashboard_command(interaction: discord.Interaction):
    """Create or refresh the equipment dashboard (Admin/Manager only)"""
    
    # Check role permissions
    if not has_required_role(interaction, ["Admin", "Manager"]):
        await interaction.response.send_message(
            "❌ This command is restricted to Admin and Manager roles only.",
            ephemeral=True
        )
        return
    
    try:
        # Find the robot-state channel
        robot_state_channel = None
        for channel in interaction.guild.channels:
            if channel.name == 'robot-state' and isinstance(channel, discord.TextChannel):
                robot_state_channel = channel
                break
        
        if not robot_state_channel:
            await interaction.response.send_message(
                "❌ Could not find #robot-state channel. Please create it first.",
                ephemeral=True
            )
            return
        
        # Defer the response since dashboard creation might take time
        await interaction.response.defer(ephemeral=True)
        
        # Create or update the dashboard
        success = await equipment_dashboard.create_or_update_dashboard(robot_state_channel)
        
        if success:
            await interaction.followup.send(
                f"✅ Equipment dashboard created/updated in {robot_state_channel.mention}",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ Failed to create/update dashboard. Check bot logs for details.",
                ephemeral=True
            )
            
    except Exception as e:
        logger.error(f"Error in dashboard command: {e}")
        try:
            await interaction.followup.send(
                "❌ An error occurred while managing the dashboard.",
                ephemeral=True
            )
        except:
            pass

# Bulk cleanup command for admins
@bot.tree.command(name="cleanup_nicknames", description="[Admin/Manager] Clean up all stuck nicknames and activity roles")
async def cleanup_nicknames_command(interaction: discord.Interaction):
    """Clean up all stuck nicknames and activity roles (Admin/Manager only)"""
    
    # Check role permissions
    if not has_required_role(interaction, ["Admin", "Manager"]):
        await interaction.response.send_message(
            "❌ This command is restricted to Admin and Manager roles only.",
            ephemeral=True
        )
        return
    
    # Defer the response since this might take time
    await interaction.response.defer(ephemeral=True)
    
    try:
        global nickname_storage
        cleanup_count = 0
        role_cleanup_count = 0
        
        # Activity role names to check for
        activity_role_names = ['On Break', 'Piloting', 'Fixing', 'Current Shift', 'On Shift']  # Include legacy 'On Shift'
        
        # Clean up all members
        for member in interaction.guild.members:
            if member.bot:
                continue
                
            user_key = f"{interaction.guild.id}_{member.id}"
            member_updated = False
            
            # Check for activity roles
            member_activity_roles = []
            for role_name in activity_role_names:
                role = discord.utils.get(interaction.guild.roles, name=role_name)
                if role and role in member.roles:
                    member_activity_roles.append(role)
            
            # Check for activity tags in nickname
            has_activity_tag = any(tag in member.display_name for tag in ['[Gello', '[break', '[lunch', '[Fixing', '[On Shift', '[Current Shift', '[Piloting'])
            
            # If they have activity roles or tags but no stored data, or stored data is inconsistent
            if member_activity_roles or has_activity_tag:
                if user_key not in nickname_storage:
                    # No stored data but has roles/tags - clean them up
                    try:
                        # Remove all activity roles
                        for role in member_activity_roles:
                            await member.remove_roles(role)
                            role_cleanup_count += 1
                        
                        # Reset nickname (remove any tags)
                        await member.edit(nick=None)
                        cleanup_count += 1
                        member_updated = True
                        
                    except Exception as e:
                        logger.error(f"Failed to cleanup {member.display_name}: {e}")
                
                else:
                    # Has stored data - check if it's consistent
                    stored_info = nickname_storage[user_key]
                    stored_roles = set(stored_info.get('roles', []))
                    actual_roles = set(role.name for role in member_activity_roles)
                    
                    # If stored data doesn't match actual roles, clean it up
                    if stored_roles != actual_roles and not stored_roles:
                        try:
                            # Remove all activity roles
                            for role in member_activity_roles:
                                await member.remove_roles(role)
                                role_cleanup_count += 1
                            
                            # Reset nickname
                            await member.edit(nick=None)
                            cleanup_count += 1
                            
                            # Clear stored data
                            del nickname_storage[user_key]
                            member_updated = True
                            
                        except Exception as e:
                            logger.error(f"Failed to cleanup {member.display_name}: {e}")
        
        # Save updated nickname storage
        if cleanup_count > 0:
            save_nickname_storage(nickname_storage)
        
        await interaction.followup.send(
            f"✅ **Cleanup Complete!**\n" +
            f"• **Nicknames reset:** {cleanup_count}\n" +
            f"• **Roles removed:** {role_cleanup_count}\n" +
            f"• **Note:** Users with valid active sessions were left unchanged",
            ephemeral=True
        )
        
    except Exception as e:
        logger.error(f"Error in cleanup_nicknames command: {e}")
        try:
            await interaction.followup.send(
                "❌ An error occurred during cleanup. Check bot logs for details.",
                ephemeral=True
            )
        except:
            pass

# Test command for debugging slash command visibility
@bot.tree.command(name="test", description="Test command to verify slash commands are working")
async def test_command(interaction: discord.Interaction):
    """Simple test command to verify slash commands work"""
    await interaction.response.send_message(
        f"✅ Slash commands are working! User: {interaction.user.display_name}",
        ephemeral=True
    )

# Error handling for slash commands
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle slash command errors"""
    error_message = f"❌ An error occurred: {str(error)}"
    logger.error(f"Command error for {interaction.user}: {error}")
    
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(error_message, ephemeral=True)
        else:
            await interaction.followup.send(error_message, ephemeral=True)
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

# Assignment system settings commands
@bot.tree.command(name="settings", description="[Admin] Configure task assignment system settings")
@app_commands.describe(
    assignments_channel="Channel for assignment threads",
    admin_channel="Channel for admin notifications", 
    operator_role="Role identifying operators",
    admin_role="Role for admin permissions (optional)",
    timezone="Timezone for shift scheduling (default: America/Los_Angeles)",
    min_on_duty="Minimum operators required on duty (default: 3)",
    cooldown_edit_sec="Edit task cooldown in seconds (default: 300)",
    cooldown_end_early_sec="End task early cooldown in seconds (default: 300)"
)
async def settings_command(
    interaction: discord.Interaction,
    assignments_channel: Optional[discord.TextChannel] = None,
    admin_channel: Optional[discord.TextChannel] = None, 
    operator_role: Optional[discord.Role] = None,
    admin_role: Optional[discord.Role] = None,
    timezone: Optional[str] = None,
    min_on_duty: Optional[int] = None,
    cooldown_edit_sec: Optional[int] = None,
    cooldown_end_early_sec: Optional[int] = None
):
    """Configure assignment system settings (Admin only)"""
    
    # Check permissions - require Manage Guild permission
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need Manage Guild permission to configure settings.",
            ephemeral=True
        )
        return
        
    try:
        with get_db_session() as db:
            settings = get_settings(db)
            changes = []
            
            # Update settings based on provided parameters
            if assignments_channel:
                settings.assignments_channel_id = str(assignments_channel.id)
                changes.append(f"Assignments channel: {assignments_channel.mention}")
                
            if admin_channel:
                settings.admin_channel_id = str(admin_channel.id) 
                changes.append(f"Admin channel: {admin_channel.mention}")
                
            if operator_role:
                settings.operator_role_id = str(operator_role.id)
                changes.append(f"Operator role: {operator_role.mention}")
                
            if admin_role:
                settings.admin_role_id = str(admin_role.id)
                changes.append(f"Admin role: {admin_role.mention}")
                
            if timezone:
                # Validate timezone
                try:
                    pytz.timezone(timezone)
                    settings.timezone = timezone
                    changes.append(f"Timezone: {timezone}")
                except:
                    await interaction.response.send_message(
                        f"❌ Invalid timezone: {timezone}",
                        ephemeral=True
                    )
                    return
                    
            if min_on_duty is not None:
                if min_on_duty < 1 or min_on_duty > 20:
                    await interaction.response.send_message(
                        "❌ Minimum on duty must be between 1 and 20",
                        ephemeral=True
                    )
                    return
                settings.min_on_duty = min_on_duty
                changes.append(f"Minimum on duty: {min_on_duty}")
                
            if cooldown_edit_sec is not None:
                if cooldown_edit_sec < 0 or cooldown_edit_sec > 3600:
                    await interaction.response.send_message(
                        "❌ Edit cooldown must be between 0 and 3600 seconds",
                        ephemeral=True
                    )
                    return
                settings.cooldown_edit_sec = cooldown_edit_sec
                changes.append(f"Edit cooldown: {cooldown_edit_sec}s")
                
            if cooldown_end_early_sec is not None:
                if cooldown_end_early_sec < 0 or cooldown_end_early_sec > 3600:
                    await interaction.response.send_message(
                        "❌ End early cooldown must be between 0 and 3600 seconds",
                        ephemeral=True
                    )
                    return
                settings.cooldown_end_early_sec = cooldown_end_early_sec
                changes.append(f"End early cooldown: {cooldown_end_early_sec}s")
            
            if not changes:
                # Show current settings if no changes
                embed = discord.Embed(
                    title="📋 Current Assignment Settings",
                    color=0x3498db
                )
                
                assignments_ch = f"<#{settings.assignments_channel_id}>" if settings.assignments_channel_id else "Not set"
                admin_ch = f"<#{settings.admin_channel_id}>" if settings.admin_channel_id else "Not set"
                operator_r = f"<@&{settings.operator_role_id}>" if settings.operator_role_id else "Not set"
                admin_r = f"<@&{settings.admin_role_id}>" if settings.admin_role_id else "Not set"
                
                embed.add_field(name="Assignments Channel", value=assignments_ch, inline=True)
                embed.add_field(name="Admin Channel", value=admin_ch, inline=True)
                embed.add_field(name="Operator Role", value=operator_r, inline=True)
                embed.add_field(name="Admin Role", value=admin_r, inline=True)
                embed.add_field(name="Timezone", value=settings.timezone, inline=True)
                embed.add_field(name="Min On Duty", value=str(settings.min_on_duty), inline=True)
                embed.add_field(name="Edit Cooldown", value=f"{settings.cooldown_edit_sec}s", inline=True)
                embed.add_field(name="End Early Cooldown", value=f"{settings.cooldown_end_early_sec}s", inline=True)
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            
            # Save changes
            db.commit()
            
            # Log the configuration change
            from models import log_action
            log_action(
                db,
                action="settings_updated",
                actor_id=str(interaction.user.id),
                metadata={"changes": changes}
            )
            
            embed = discord.Embed(
                title="✅ Settings Updated",
                description="\n".join(f"• {change}" for change in changes),
                color=0x00ff00
            )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
    except Exception as e:
        logger.error(f"Error in settings command: {e}")
        await interaction.response.send_message(
            "❌ An error occurred while updating settings.",
            ephemeral=True
        )


@bot.tree.command(name="task", description="[Admin] Manage task templates")
@app_commands.describe(
    action="Action to perform",
    name="Task name",
    priority="Task priority (lower = higher priority)",
    window_start="Start time (ISO format: YYYY-MM-DDTHH:MM:SSZ)",
    window_end="End time (ISO format: YYYY-MM-DDTHH:MM:SSZ)",
    instructions="Task instructions",
    params_schema="JSON schema for parameters",
    is_active="Whether task is active"
)
async def task_command(
    interaction: discord.Interaction,
    action: str,
    name: Optional[str] = None,
    priority: Optional[int] = None,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
    instructions: Optional[str] = None,
    params_schema: Optional[str] = None,
    is_active: Optional[bool] = None
):
    """Manage task templates (Admin only)"""
    
    # Check admin permissions
    if not has_required_role(interaction, ["Admin", "Manager"]) and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need Admin/Manager role or Manage Guild permission to manage tasks.",
            ephemeral=True
        )
        return
    
    try:
        with get_db_session() as db:
            if action == "list":
                await list_tasks(interaction, db)
            elif action == "add":
                await add_task(interaction, db, name, priority, window_start, window_end, instructions, params_schema)
            elif action == "update":
                await update_task(interaction, db, name, priority, window_start, window_end, instructions, params_schema, is_active)
            elif action == "remove":
                await remove_task(interaction, db, name)
            else:
                await interaction.response.send_message(
                    "❌ Invalid action. Use: list, add, update, or remove",
                    ephemeral=True
                )
                
    except Exception as e:
        logger.error(f"Error in task command: {e}")
        await interaction.response.send_message(
            "❌ An error occurred while managing tasks.",
            ephemeral=True
        )


async def list_tasks(interaction: discord.Interaction, db):
    """List all task templates"""
    from models import TaskTemplate
    
    tasks = db.query(TaskTemplate).order_by(TaskTemplate.priority, TaskTemplate.name).all()
    
    if not tasks:
        await interaction.response.send_message("No tasks configured.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="📋 Task Templates",
        color=0x3498db
    )
    
    for task in tasks[:10]:  # Limit to first 10 to avoid embed size limits
        status = "🟢 Active" if task.is_active else "🔴 Inactive"
        window_info = ""
        if task.window_start or task.window_end:
            start = task.window_start.strftime('%m/%d %H:%M UTC') if task.window_start else "No start"
            end = task.window_end.strftime('%m/%d %H:%M UTC') if task.window_end else "No end"
            window_info = f"\n*Window: {start} - {end}*"
        
        embed.add_field(
            name=f"{task.name} (Priority: {task.priority})",
            value=f"{status}{window_info}\n{task.instructions[:100] + '...' if task.instructions and len(task.instructions) > 100 else task.instructions or 'No instructions'}",
            inline=False
        )
    
    if len(tasks) > 10:
        embed.set_footer(text=f"Showing 10 of {len(tasks)} tasks")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def add_task(interaction, db, name, priority, window_start, window_end, instructions, params_schema):
    """Add a new task template"""
    from models import TaskTemplate
    
    if not name:
        await interaction.response.send_message("❌ Task name is required.", ephemeral=True)
        return
        
    # Check if task already exists
    existing = db.query(TaskTemplate).filter(TaskTemplate.name == name).first()
    if existing:
        await interaction.response.send_message(f"❌ Task '{name}' already exists.", ephemeral=True)
        return
    
    # Parse time windows if provided
    start_dt = None
    end_dt = None
    
    if window_start:
        try:
            start_dt = datetime.fromisoformat(window_start.replace('Z', '+00:00'))
        except ValueError:
            await interaction.response.send_message("❌ Invalid window start format. Use ISO format: YYYY-MM-DDTHH:MM:SSZ", ephemeral=True)
            return
    
    if window_end:
        try:
            end_dt = datetime.fromisoformat(window_end.replace('Z', '+00:00'))
        except ValueError:
            await interaction.response.send_message("❌ Invalid window end format. Use ISO format: YYYY-MM-DDTHH:MM:SSZ", ephemeral=True)
            return
    
    # Parse params schema if provided
    schema_obj = None
    if params_schema:
        try:
            import json
            schema_obj = json.loads(params_schema)
        except json.JSONDecodeError:
            await interaction.response.send_message("❌ Invalid JSON schema format.", ephemeral=True)
            return
    
    # Create task
    task = TaskTemplate(
        name=name,
        priority=priority or 100,
        window_start=start_dt,
        window_end=end_dt,
        instructions=instructions,
        params_schema=schema_obj
    )
    
    db.add(task)
    db.commit()
    
    # Log the action
    from models import log_action
    log_action(
        db,
        action="task_template_added",
        actor_id=str(interaction.user.id),
        target=name,
        metadata={"priority": task.priority}
    )
    
    await interaction.response.send_message(f"✅ Task '{name}' added successfully.", ephemeral=True)


async def update_task(interaction, db, name, priority, window_start, window_end, instructions, params_schema, is_active):
    """Update existing task template"""
    from models import TaskTemplate
    
    if not name:
        await interaction.response.send_message("❌ Task name is required for updates.", ephemeral=True)
        return
        
    task = db.query(TaskTemplate).filter(TaskTemplate.name == name).first()
    if not task:
        await interaction.response.send_message(f"❌ Task '{name}' not found.", ephemeral=True)
        return
    
    changes = []
    
    if priority is not None:
        task.priority = priority
        changes.append(f"Priority: {priority}")
    
    if window_start is not None:
        if window_start:
            try:
                task.window_start = datetime.fromisoformat(window_start.replace('Z', '+00:00'))
                changes.append(f"Window start: {window_start}")
            except ValueError:
                await interaction.response.send_message("❌ Invalid window start format.", ephemeral=True)
                return
        else:
            task.window_start = None
            changes.append("Window start: cleared")
    
    if window_end is not None:
        if window_end:
            try:
                task.window_end = datetime.fromisoformat(window_end.replace('Z', '+00:00'))
                changes.append(f"Window end: {window_end}")
            except ValueError:
                await interaction.response.send_message("❌ Invalid window end format.", ephemeral=True)
                return
        else:
            task.window_end = None
            changes.append("Window end: cleared")
    
    if instructions is not None:
        task.instructions = instructions if instructions else None
        changes.append(f"Instructions: {'updated' if instructions else 'cleared'}")
    
    if params_schema is not None:
        if params_schema:
            try:
                import json
                task.params_schema = json.loads(params_schema)
                changes.append("Params schema: updated")
            except json.JSONDecodeError:
                await interaction.response.send_message("❌ Invalid JSON schema format.", ephemeral=True)
                return
        else:
            task.params_schema = None
            changes.append("Params schema: cleared")
    
    if is_active is not None:
        task.is_active = is_active
        changes.append(f"Active: {'Yes' if is_active else 'No'}")
    
    if not changes:
        await interaction.response.send_message("❌ No changes specified.", ephemeral=True)
        return
    
    db.commit()
    
    # Log the action
    from models import log_action
    log_action(
        db,
        action="task_template_updated",
        actor_id=str(interaction.user.id),
        target=name,
        metadata={"changes": changes}
    )
    
    embed = discord.Embed(
        title=f"✅ Task '{name}' Updated",
        description="\n".join(f"• {change}" for change in changes),
        color=0x00ff00
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def remove_task(interaction, db, name):
    """Remove a task template"""
    from models import TaskTemplate
    
    if not name:
        await interaction.response.send_message("❌ Task name is required.", ephemeral=True)
        return
        
    task = db.query(TaskTemplate).filter(TaskTemplate.name == name).first()
    if not task:
        await interaction.response.send_message(f"❌ Task '{name}' not found.", ephemeral=True)
        return
    
    # Check if task has active assignments
    from models import Assignment
    active_assignments = db.query(Assignment).filter(
        Assignment.template_id == task.id,
        Assignment.status.in_(['pending_ack', 'active', 'covering', 'paused_break', 'paused_lunch'])
    ).count()
    
    if active_assignments > 0:
        await interaction.response.send_message(
            f"❌ Cannot remove task '{name}' - it has {active_assignments} active assignments.",
            ephemeral=True
        )
        return
    
    db.delete(task)
    db.commit()
    
    # Log the action
    from models import log_action
    log_action(
        db,
        action="task_template_removed",
        actor_id=str(interaction.user.id),
        target=name
    )
    
    await interaction.response.send_message(f"✅ Task '{name}' removed successfully.", ephemeral=True)


if __name__ == '__main__':
    bot.run(TOKEN)