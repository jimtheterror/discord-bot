"""
Thread management for operator assignment system.
Handles creation and management of private threads for each operator.
"""
import logging
from typing import Optional, Dict, List
import discord
from datetime import datetime, timedelta

from .database import get_db_session
from .models import get_settings, get_or_create_user

logger = logging.getLogger(__name__)


class ThreadManager:
    """Manages private threads for operators"""
    
    def __init__(self, bot: discord.Client):
        self.bot = bot
        # Cache of thread objects by user_id for performance
        self._thread_cache: Dict[str, discord.Thread] = {}
        
    async def get_or_create_operator_thread(
        self, 
        guild: discord.Guild, 
        user_id: str,
        display_name: str
    ) -> Optional[discord.Thread]:
        """
        Get or create a private thread for an operator.
        
        Args:
            guild: Discord guild
            user_id: Discord user ID
            display_name: User's display name
            
        Returns:
            Thread object or None if failed
        """
        try:
            # Check cache first
            if user_id in self._thread_cache:
                thread = self._thread_cache[user_id]
                try:
                    # Verify thread still exists and is accessible
                    await thread.fetch()
                    return thread
                except (discord.NotFound, discord.Forbidden):
                    # Thread was deleted or inaccessible, remove from cache
                    del self._thread_cache[user_id]
            
            # Get settings to find the assignments channel
            with get_db_session() as db:
                settings = get_settings(db)
                if not settings.assignments_channel_id:
                    logger.error("No assignments channel configured")
                    return None
            
            # Get the assignments channel
            assignments_channel = guild.get_channel(int(settings.assignments_channel_id))
            if not assignments_channel:
                try:
                    assignments_channel = await guild.fetch_channel(int(settings.assignments_channel_id))
                except (discord.NotFound, discord.Forbidden):
                    logger.error(f"Assignments channel {settings.assignments_channel_id} not found")
                    return None
            
            if not isinstance(assignments_channel, discord.TextChannel):
                logger.error(f"Assignments channel {settings.assignments_channel_id} is not a text channel")
                return None
            
            # Look for existing thread for this user
            existing_thread = await self._find_existing_thread(assignments_channel, user_id, display_name)
            if existing_thread:
                self._thread_cache[user_id] = existing_thread
                return existing_thread
            
            # Create new private thread
            thread = await self._create_operator_thread(assignments_channel, user_id, display_name, settings)
            if thread:
                self._thread_cache[user_id] = thread
                return thread
                
            return None
            
        except Exception as e:
            logger.error(f"Failed to get/create thread for user {user_id}: {e}")
            return None
            
    async def _find_existing_thread(
        self,
        channel: discord.TextChannel,
        user_id: str,
        display_name: str
    ) -> Optional[discord.Thread]:
        """Find existing thread for the user"""
        try:
            # Check active threads first
            for thread in channel.threads:
                if await self._is_user_thread(thread, user_id, display_name):
                    return thread
            
            # Check archived threads
            async for thread in channel.archived_threads(limit=100):
                if await self._is_user_thread(thread, user_id, display_name):
                    # Unarchive if needed
                    if thread.archived:
                        try:
                            await thread.edit(archived=False)
                        except discord.Forbidden:
                            logger.warning(f"Could not unarchive thread {thread.name}")
                    return thread
                    
            return None
            
        except Exception as e:
            logger.error(f"Error finding existing thread: {e}")
            return None
            
    async def _is_user_thread(self, thread: discord.Thread, user_id: str, display_name: str) -> bool:
        """Check if a thread belongs to the specified user"""
        try:
            # Check thread name patterns
            thread_name = thread.name.lower()
            user_patterns = [
                f"assignments-{user_id}",
                f"{display_name.lower()}-assignments",
                f"tasks-{user_id}",
                user_id  # Simple user ID match
            ]
            
            if any(pattern in thread_name for pattern in user_patterns):
                return True
            
            # Check if user is in thread members
            try:
                member = await thread.fetch_member(int(user_id))
                return member is not None
            except (discord.NotFound, discord.HTTPException):
                pass
                
            return False
            
        except Exception as e:
            logger.error(f"Error checking if thread belongs to user: {e}")
            return False
            
    async def _create_operator_thread(
        self,
        channel: discord.TextChannel,
        user_id: str, 
        display_name: str,
        settings
    ) -> Optional[discord.Thread]:
        """Create a new private thread for an operator"""
        try:
            # Get the user object
            user = self.bot.get_user(int(user_id))
            if not user:
                try:
                    user = await self.bot.fetch_user(int(user_id))
                except (discord.NotFound, discord.HTTPException):
                    logger.error(f"Could not fetch user {user_id}")
                    return None
            
            # Create thread name
            safe_name = "".join(c for c in display_name if c.isalnum() or c in (' ', '-', '_'))[:50]
            thread_name = f"📋 {safe_name} - Task Assignments"
            
            # Create the thread
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                reason=f"Task assignment thread for {display_name}"
            )
            
            # Add the operator to the thread
            await thread.add_user(user)
            
            # Add admin users to the thread if configured
            if settings.admin_role_id:
                guild = channel.guild
                admin_role = guild.get_role(int(settings.admin_role_id))
                if admin_role:
                    for member in admin_role.members:
                        try:
                            await thread.add_user(member)
                        except (discord.Forbidden, discord.HTTPException) as e:
                            logger.warning(f"Could not add admin {member.display_name} to thread: {e}")
            
            # Send welcome message
            embed = discord.Embed(
                title="📋 Welcome to Your Task Assignment Thread",
                description=(
                    f"Hi {user.mention}! This is your private thread for task assignments.\n\n"
                    "**What happens here:**\n"
                    "• You'll receive hourly task assignments\n"
                    "• Use the buttons to start, edit, or manage your tasks\n"
                    "• Request breaks and lunch when needed\n"
                    "• Only you and admins can see this thread\n\n"
                    "**Need help?** Contact an admin or check the documentation."
                ),
                color=0x3498db,
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="LakBay Task Assignment System")
            
            await thread.send(embed=embed)
            
            logger.info(f"Created new assignment thread for {display_name}: {thread.name}")
            return thread
            
        except Exception as e:
            logger.error(f"Failed to create thread for {display_name}: {e}")
            return None
            
    async def ensure_thread_permissions(
        self,
        thread: discord.Thread,
        user_id: str,
        settings
    ) -> bool:
        """Ensure thread has correct permissions (user + admins only)"""
        try:
            guild = thread.guild
            user = guild.get_member(int(user_id))
            if not user:
                return False
            
            # Check if user is in thread
            try:
                thread_member = await thread.fetch_member(int(user_id))
                if not thread_member:
                    await thread.add_user(user)
            except discord.NotFound:
                await thread.add_user(user)
            
            # Ensure admin access if configured
            if settings.admin_role_id:
                admin_role = guild.get_role(int(settings.admin_role_id))
                if admin_role:
                    current_members = [m.id for m in thread.members]
                    for admin in admin_role.members:
                        if admin.id not in current_members:
                            try:
                                await thread.add_user(admin)
                            except (discord.Forbidden, discord.HTTPException):
                                logger.warning(f"Could not add admin {admin.display_name} to thread")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to ensure thread permissions: {e}")
            return False
            
    async def get_all_operator_threads(self, guild: discord.Guild) -> List[discord.Thread]:
        """Get all operator assignment threads in the guild"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                if not settings.assignments_channel_id:
                    return []
            
            assignments_channel = guild.get_channel(int(settings.assignments_channel_id))
            if not assignments_channel:
                return []
            
            operator_threads = []
            
            # Check active threads
            for thread in assignments_channel.threads:
                if await self._is_assignment_thread(thread):
                    operator_threads.append(thread)
            
            # Check archived threads
            async for thread in assignments_channel.archived_threads(limit=200):
                if await self._is_assignment_thread(thread):
                    operator_threads.append(thread)
                    
            return operator_threads
            
        except Exception as e:
            logger.error(f"Failed to get operator threads: {e}")
            return []
            
    async def _is_assignment_thread(self, thread: discord.Thread) -> bool:
        """Check if a thread is an assignment thread"""
        try:
            thread_name = thread.name.lower()
            assignment_indicators = [
                "assignment",
                "task",
                "📋"
            ]
            
            return any(indicator in thread_name for indicator in assignment_indicators)
            
        except Exception:
            return False
            
    async def cleanup_inactive_threads(self, guild: discord.Guild, days_inactive: int = 7) -> int:
        """Clean up threads that have been inactive for too long"""
        try:
            threads = await self.get_all_operator_threads(guild)
            cleaned_count = 0
            
            cutoff_time = datetime.utcnow() - timedelta(days=days_inactive)
            
            for thread in threads:
                try:
                    # Check last message time
                    last_message = None
                    async for message in thread.history(limit=1):
                        last_message = message
                        break
                    
                    if last_message and last_message.created_at < cutoff_time:
                        # Archive the thread instead of deleting
                        if not thread.archived:
                            await thread.edit(archived=True, locked=True)
                            cleaned_count += 1
                            logger.info(f"Archived inactive thread: {thread.name}")
                        
                except Exception as e:
                    logger.error(f"Error cleaning up thread {thread.name}: {e}")
                    
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Failed to cleanup inactive threads: {e}")
            return 0
            
    def clear_cache(self, user_id: Optional[str] = None):
        """Clear thread cache for user or all users"""
        if user_id:
            self._thread_cache.pop(user_id, None)
        else:
            self._thread_cache.clear()
