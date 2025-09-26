"""
Assignment scheduler for hourly task posting and escalation handling.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

import discord
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from .database import get_db_session
from .models import User, Shift, Assignment, AssignmentStatus, Settings, TaskTemplate, log_action
from .selectors import SelectionService
from .thread_manager import ThreadManager

logger = logging.getLogger(__name__)


@dataclass
class ShiftInfo:
    """Information about a shift time period"""
    start_utc: datetime
    end_utc: datetime
    tz_name: str


class AssignmentScheduler:
    """Manages hourly task assignments and escalations"""
    
    def __init__(self, bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self.selection_service = SelectionService()
        self.thread_manager = ThreadManager(bot)
        self.running = False
        
        # Track pending acknowledgments for escalation
        self.pending_acks: Dict[int, datetime] = {}  # assignment_id -> posted_at
        
    async def start(self):
        """Start the scheduler"""
        if self.running:
            return
            
        logger.info("Starting assignment scheduler...")
        
        # Schedule hourly assignment posting at the top of each hour
        self.scheduler.add_job(
            self.post_hourly_assignments,
            CronTrigger(minute=0, second=0),
            id="hourly_assignments",
            max_instances=1,
            coalesce=True
        )
        
        # Schedule reminder checks every minute
        self.scheduler.add_job(
            self.check_pending_acknowledgments,
            CronTrigger(second=0),
            id="check_reminders",
            max_instances=1,
            coalesce=True
        )
        
        # Schedule dashboard updates every minute  
        self.scheduler.add_job(
            self.update_dashboard,
            CronTrigger(second=30),  # Offset by 30s to avoid conflicts
            id="update_dashboard",
            max_instances=1,
            coalesce=True
        )
        
        self.scheduler.start()
        self.running = True
        logger.info("Assignment scheduler started")
        
    async def stop(self):
        """Stop the scheduler"""
        if not self.running:
            return
            
        logger.info("Stopping assignment scheduler...")
        self.scheduler.shutdown(wait=True)
        self.running = False
        logger.info("Assignment scheduler stopped")
        
    def get_shift_times(self, timezone_str: str = "America/Los_Angeles") -> List[ShiftInfo]:
        """Get the three shift time periods in UTC"""
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        today = now.date()
        
        # Define shift start times in local timezone
        shift_starts_local = [
            tz.localize(datetime.combine(today, datetime.min.time().replace(hour=6))),   # 06:00 PST
            tz.localize(datetime.combine(today, datetime.min.time().replace(hour=14))),  # 14:00 PST  
            tz.localize(datetime.combine(today, datetime.min.time().replace(hour=22))),  # 22:00 PST
        ]
        
        shifts = []
        for start_local in shift_starts_local:
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = start_utc + timedelta(hours=9)  # 9-hour shifts
            
            shifts.append(ShiftInfo(
                start_utc=start_utc,
                end_utc=end_utc,
                tz_name=timezone_str
            ))
            
        return shifts
        
    def calculate_hour_index(self, shift_start: datetime, current_time: Optional[datetime] = None) -> int:
        """Calculate hour index (1-9) within a shift"""
        if current_time is None:
            current_time = datetime.now(timezone.utc)
            
        # Ensure both times are UTC
        if shift_start.tzinfo is None:
            shift_start = shift_start.replace(tzinfo=timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
            
        elapsed = current_time - shift_start
        hour_index = int(elapsed.total_seconds() // 3600) + 1
        
        # Clamp to valid range
        return max(1, min(9, hour_index))
        
    async def get_on_shift_operators(self) -> List[tuple[User, Shift, int]]:
        """Get all operators currently on shift with their hour index"""
        try:
            with get_db_session() as db:
                now_utc = datetime.now(timezone.utc)
                operators = []
                
                # Find all active shifts
                active_shifts = db.query(Shift).filter(
                    Shift.end_at.is_(None),  # Active shifts
                    Shift.start_at <= now_utc,  # Already started
                    Shift.start_at >= now_utc - timedelta(hours=9)  # Within 9-hour window
                ).all()
                
                for shift in active_shifts:
                    user = shift.user
                    if not user.is_operator:
                        continue
                        
                    # Calculate current hour index
                    hour_index = self.calculate_hour_index(shift.start_at, now_utc)
                    
                    # Skip if past shift end (hour 9)
                    if hour_index > 9:
                        continue
                        
                    operators.append((user, shift, hour_index))
                
                logger.info(f"Found {len(operators)} operators on shift")
                return operators
                
        except Exception as e:
            logger.error(f"Failed to get on-shift operators: {e}")
            return []
            
    async def post_hourly_assignments(self):
        """Post assignments for the current hour to all on-shift operators"""
        try:
            logger.info("Posting hourly assignments...")
            
            operators = await self.get_on_shift_operators()
            if not operators:
                logger.info("No operators on shift, skipping assignment posting")
                return
                
            with get_db_session() as db:
                settings = db.query(Settings).first()
                if not settings or not settings.assignments_channel_id:
                    logger.warning("Assignment channel not configured")
                    return
                    
                # Select Comms Lead using LRU
                comms_lead = self.selection_service.select_comms_lead([op[0] for op in operators])
                logger.info(f"Selected Comms Lead: {comms_lead.display_name if comms_lead else 'None'}")
                
                # Create assignments
                assignments_created = []
                for user, shift, hour_index in operators:
                    # Check if assignment already exists for this hour
                    existing = db.query(Assignment).filter(
                        Assignment.user_id == user.id,
                        Assignment.shift_id == shift.id,
                        Assignment.hour_index == hour_index
                    ).first()
                    
                    if existing:
                        logger.info(f"Assignment already exists for {user.display_name} hour {hour_index}")
                        continue
                    
                    # Determine task assignment
                    if user.id == comms_lead.id:
                        task_name = "Comms Lead"
                    else:
                        task_name = "Data Labelling"
                        
                    # Calculate expected end time (next hour boundary)
                    now_utc = datetime.now(timezone.utc)
                    ends_at = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    
                    # Create assignment
                    assignment = Assignment(
                        user_id=user.id,
                        shift_id=shift.id,
                        template_id=None,  # No template for default tasks
                        task_name=task_name,
                        params={},
                        status=AssignmentStatus.PENDING_ACK,
                        hour_index=hour_index,
                        ends_at=ends_at
                    )
                    
                    db.add(assignment)
                    assignments_created.append(assignment)
                
                # Commit all assignments
                if assignments_created:
                    db.commit()
                    
                    # Update Comms Lead timestamp
                    if comms_lead:
                        comms_lead.last_comms_lead_at = datetime.now(timezone.utc)
                        db.commit()
                    
                    # Log the assignment posting
                    log_action(
                        db,
                        action="assignments_posted",
                        metadata={
                            "count": len(assignments_created),
                            "comms_lead_id": comms_lead.id if comms_lead else None,
                            "assignments": [
                                {
                                    "user_id": a.user_id,
                                    "task_name": a.task_name,
                                    "hour_index": a.hour_index
                                }
                                for a in assignments_created
                            ]
                        }
                    )
                
                # Post assignment widgets to threads
                for assignment in assignments_created:
                    try:
                        await self.post_assignment_widget(assignment)
                        # Track for acknowledgment checking
                        self.pending_acks[assignment.id] = datetime.now(timezone.utc)
                    except Exception as e:
                        logger.error(f"Failed to post widget for assignment {assignment.id}: {e}")
                        
                logger.info(f"Posted {len(assignments_created)} new assignments")
                
        except Exception as e:
            logger.error(f"Failed to post hourly assignments: {e}")
            
    async def post_assignment_widget(self, assignment: Assignment):
        """Post assignment widget to operator's thread"""
        try:
            # Get user information
            with get_db_session() as db:
                user = db.query(User).filter(User.id == assignment.user_id).first()
                if not user:
                    logger.error(f"User {assignment.user_id} not found for assignment {assignment.id}")
                    return
                
                # Get the guild (assuming single guild for now)
                guild = None
                for g in self.bot.guilds:
                    if g.get_member(int(assignment.user_id)):
                        guild = g
                        break
                
                if not guild:
                    logger.error(f"Could not find guild for user {assignment.user_id}")
                    return
                
                # Get or create thread
                thread = await self.thread_manager.get_or_create_operator_thread(
                    guild, assignment.user_id, user.display_name
                )
                
                if not thread:
                    logger.error(f"Could not get/create thread for user {assignment.user_id}")
                    return
                
                # Create assignment widget embed and view
                embed, view = await self.create_assignment_widget(assignment, user)
                
                # Post the widget
                message = await thread.send(embed=embed, view=view)
                
                logger.info(f"Posted assignment widget for {user.display_name}, task {assignment.task_name}")
                
        except Exception as e:
            logger.error(f"Failed to post assignment widget for {assignment.id}: {e}")
            
    async def create_assignment_widget(self, assignment: Assignment, user: User):
        """Create embed and view for assignment widget"""
        import discord
        from datetime import timezone
        
        # Calculate time remaining
        time_remaining = "Unknown"
        if assignment.ends_at:
            now = datetime.now(timezone.utc)
            if assignment.ends_at > now:
                remaining = assignment.ends_at - now
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                if hours > 0:
                    time_remaining = f"{hours}h {minutes}m"
                else:
                    time_remaining = f"{minutes}m"
            else:
                time_remaining = "Overdue"
        
        # Create embed
        embed = discord.Embed(
            title=f"📋 Hour {assignment.hour_index} Assignment",
            color=0x3498db if assignment.status == AssignmentStatus.PENDING_ACK else 0x00ff00
        )
        
        embed.add_field(
            name="Task",
            value=assignment.task_name,
            inline=True
        )
        
        embed.add_field(
            name="Status", 
            value=assignment.status.value.replace('_', ' ').title(),
            inline=True
        )
        
        embed.add_field(
            name="Time Remaining",
            value=time_remaining,
            inline=True
        )
        
        if assignment.started_at:
            embed.add_field(
                name="Started At",
                value=f"<t:{int(assignment.started_at.timestamp())}:t>",
                inline=True
            )
        
        if assignment.ends_at:
            embed.add_field(
                name="Ends At",
                value=f"<t:{int(assignment.ends_at.timestamp())}:t>",
                inline=True
            )
        
        # Add instructions if available
        with get_db_session() as db:
            if assignment.template_id:
                template = db.query(TaskTemplate).filter(TaskTemplate.id == assignment.template_id).first()
                if template and template.instructions:
                    embed.add_field(
                        name="Instructions",
                        value=template.instructions[:200] + ("..." if len(template.instructions) > 200 else ""),
                        inline=False
                    )
        
        embed.set_footer(text=f"Assignment ID: {assignment.id}")
        embed.timestamp = datetime.utcnow()
        
        # Create view with buttons
        view = AssignmentView(assignment.id, assignment.hour_index, assignment.status)
        
        return embed, view
            
    async def check_pending_acknowledgments(self):
        """Check for pending acknowledgments and send reminders/escalate"""
        try:
            now_utc = datetime.now(timezone.utc)
            
            with get_db_session() as db:
                # Find assignments pending acknowledgment
                pending_assignments = db.query(Assignment).filter(
                    Assignment.status == AssignmentStatus.PENDING_ACK,
                    Assignment.created_at <= now_utc - timedelta(minutes=1)  # At least 1 minute old
                ).all()
                
                for assignment in pending_assignments:
                    assignment_age = now_utc - assignment.created_at
                    
                    # 5-minute reminder
                    if assignment_age >= timedelta(minutes=5) and assignment_age < timedelta(minutes=6):
                        await self.send_acknowledgment_reminder(assignment)
                        
                    # 10-minute escalation for non-Data Labelling
                    elif assignment_age >= timedelta(minutes=10) and assignment.task_name != "Data Labelling":
                        await self.escalate_unacknowledged_assignment(assignment)
                        
        except Exception as e:
            logger.error(f"Failed to check pending acknowledgments: {e}")
            
    async def send_acknowledgment_reminder(self, assignment: Assignment):
        """Send 5-minute reminder to operator and alert admins"""
        try:
            logger.info(f"Sending 5-minute reminder for assignment {assignment.id}")
            
            # Get user details
            with get_db_session() as db:
                user = db.query(User).filter(User.id == assignment.user_id).first()
                if not user:
                    logger.error(f"User {assignment.user_id} not found for reminder")
                    return
                
                settings = get_settings(db)
                
                # Send reminder to operator thread
                await self._send_operator_reminder(assignment, user, settings)
                
                # Send alert to admin channel
                await self._send_admin_alert(assignment, user, settings, "5-minute reminder")
                
                log_action(
                    db,
                    action="acknowledgment_reminder_sent",
                    target=str(assignment.id),
                    metadata={
                        "user_id": assignment.user_id,
                        "task_name": assignment.task_name,
                        "minutes_elapsed": 5
                    }
                )
            
        except Exception as e:
            logger.error(f"Failed to send acknowledgment reminder for {assignment.id}: {e}")
            
    async def _send_operator_reminder(self, assignment: Assignment, user: User, settings):
        """Send reminder message to operator's thread"""
        try:
            # Find guild and get thread
            guild = None
            for g in self.bot.guilds:
                if g.get_member(int(assignment.user_id)):
                    guild = g
                    break
            
            if not guild:
                logger.error(f"Could not find guild for user {assignment.user_id}")
                return
                
            thread = await self.thread_manager.get_or_create_operator_thread(
                guild, assignment.user_id, user.display_name
            )
            
            if not thread:
                logger.error(f"Could not get thread for user {assignment.user_id}")
                return
            
            # Create reminder embed
            embed = discord.Embed(
                title="⏰ Task Acknowledgment Reminder",
                description=(
                    f"Hey {user.display_name}! You have a task waiting for acknowledgment.\n\n"
                    f"**Task:** {assignment.task_name}\n"
                    f"**Hour:** {assignment.hour_index}\n"
                    f"**Time since posted:** 5 minutes\n\n"
                    "Please click the **▶️ Start Task** button to begin working."
                ),
                color=0xffa500,
                timestamp=datetime.utcnow()
            )
            
            # Ping the user
            discord_user = guild.get_member(int(assignment.user_id))
            content = f"{discord_user.mention} 📋 Task acknowledgment needed!" if discord_user else "📋 Task acknowledgment needed!"
            
            await thread.send(content=content, embed=embed)
            logger.info(f"Sent reminder to {user.display_name}")
            
        except Exception as e:
            logger.error(f"Failed to send operator reminder: {e}")
            
    async def _send_admin_alert(self, assignment: Assignment, user: User, settings, alert_type: str):
        """Send alert to admin channel"""
        try:
            if not settings.admin_channel_id:
                logger.warning("No admin channel configured for alerts")
                return
            
            # Find guild and admin channel
            guild = None
            for g in self.bot.guilds:
                if g.get_member(int(assignment.user_id)):
                    guild = g
                    break
            
            if not guild:
                return
                
            admin_channel = guild.get_channel(int(settings.admin_channel_id))
            if not admin_channel:
                try:
                    admin_channel = await guild.fetch_channel(int(settings.admin_channel_id))
                except (discord.NotFound, discord.Forbidden):
                    logger.error(f"Admin channel {settings.admin_channel_id} not found")
                    return
            
            # Create alert embed
            embed = discord.Embed(
                title=f"🚨 Assignment Alert: {alert_type}",
                color=0xff6b6b
            )
            
            embed.add_field(
                name="Operator",
                value=user.display_name,
                inline=True
            )
            
            embed.add_field(
                name="Task", 
                value=assignment.task_name,
                inline=True
            )
            
            embed.add_field(
                name="Hour",
                value=str(assignment.hour_index),
                inline=True
            )
            
            embed.add_field(
                name="Status",
                value=assignment.status.value.replace('_', ' ').title(),
                inline=True
            )
            
            # Add time elapsed
            if assignment.created_at:
                elapsed = datetime.now(timezone.utc) - assignment.created_at
                elapsed_minutes = int(elapsed.total_seconds() / 60)
                embed.add_field(
                    name="Time Elapsed",
                    value=f"{elapsed_minutes} minutes",
                    inline=True
                )
            
            embed.set_footer(text=f"Assignment ID: {assignment.id}")
            embed.timestamp = datetime.utcnow()
            
            await admin_channel.send(embed=embed)
            logger.info(f"Sent admin alert for assignment {assignment.id}")
            
        except Exception as e:
            logger.error(f"Failed to send admin alert: {e}")
            
    async def escalate_unacknowledged_assignment(self, assignment: Assignment):
        """Escalate unacknowledged non-Data Labelling assignment"""
        try:
            logger.info(f"Escalating unacknowledged assignment {assignment.id}")
            
            with get_db_session() as db:
                user = db.query(User).filter(User.id == assignment.user_id).first()
                settings = get_settings(db)
                
                if not user:
                    logger.error(f"User {assignment.user_id} not found for escalation")
                    return
                
                # Try to find a Data Labelling operator to reassign to
                candidates = await self.get_reassignment_candidates(assignment)
                
                if candidates:
                    # Reassign to first candidate
                    new_assignee = candidates[0]
                    
                    success = await self._perform_reassignment(assignment, new_assignee, db)
                    
                    if success:
                        # Send notifications about successful reassignment
                        await self._send_reassignment_notifications(
                            assignment, user, new_assignee, settings, "escalation"
                        )
                    else:
                        # Fallback: send admin alert about failed reassignment
                        await self._send_admin_alert(
                            assignment, user, settings, "escalation - reassignment failed"
                        )
                else:
                    # No candidates available, send admin alert
                    await self._send_admin_alert(
                        assignment, user, settings, "escalation - no candidates available"
                    )
                    logger.warning(f"No reassignment candidates available for assignment {assignment.id}")
                
                log_action(
                    db,
                    action="assignment_escalated",
                    target=str(assignment.id),
                    metadata={
                        "user_id": assignment.user_id,
                        "task_name": assignment.task_name,
                        "candidates_found": len(candidates),
                        "reassigned_to": candidates[0].id if candidates else None,
                        "minutes_elapsed": 10
                    }
                )
            
        except Exception as e:
            logger.error(f"Failed to escalate assignment {assignment.id}: {e}")
            
    async def _perform_reassignment(
        self, 
        original_assignment: Assignment, 
        new_assignee: User, 
        db: Session
    ) -> bool:
        """Perform the actual reassignment of a task"""
        try:
            # Find the new assignee's current Data Labelling assignment  
            current_assignment = db.query(Assignment).filter(
                Assignment.user_id == new_assignee.id,
                Assignment.hour_index == original_assignment.hour_index,
                Assignment.status == AssignmentStatus.ACTIVE,
                Assignment.task_name == "Data Labelling"
            ).first()
            
            if not current_assignment:
                logger.warning(f"Could not find current Data Labelling assignment for {new_assignee.id}")
                return False
            
            # Update the original assignment to mark it as ended early (unacknowledged)
            original_assignment.status = AssignmentStatus.ENDED_EARLY
            original_assignment.ended_at = datetime.now(timezone.utc)
            
            # Update the new assignee's assignment to the escalated task
            current_assignment.task_name = original_assignment.task_name
            current_assignment.template_id = original_assignment.template_id
            current_assignment.params = original_assignment.params
            current_assignment.covering_for_user_id = original_assignment.user_id
            # Keep it as ACTIVE since they were already working
            
            db.commit()
            
            logger.info(
                f"Reassigned {original_assignment.task_name} from {original_assignment.user_id} "
                f"to {new_assignee.id} due to escalation"
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to perform reassignment: {e}")
            db.rollback()
            return False
            
    async def _send_reassignment_notifications(
        self,
        original_assignment: Assignment,
        original_user: User, 
        new_assignee: User,
        settings,
        reason: str
    ):
        """Send notifications about task reassignment"""
        try:
            # Find guild
            guild = None
            for g in self.bot.guilds:
                if g.get_member(int(new_assignee.id)):
                    guild = g
                    break
            
            if not guild:
                logger.error("Could not find guild for reassignment notifications")
                return
            
            # Notify new assignee in their thread
            new_thread = await self.thread_manager.get_or_create_operator_thread(
                guild, new_assignee.id, new_assignee.display_name
            )
            
            if new_thread:
                embed = discord.Embed(
                    title="🔄 Task Reassignment",
                    description=(
                        f"You've been assigned a new task due to {reason}.\n\n"
                        f"**New Task:** {original_assignment.task_name}\n"
                        f"**Hour:** {original_assignment.hour_index}\n"
                        f"**Covering for:** {original_user.display_name}\n\n"
                        "This task is now active - please begin working on it."
                    ),
                    color=0x3498db,
                    timestamp=datetime.utcnow()
                )
                
                discord_user = guild.get_member(int(new_assignee.id))
                content = f"{discord_user.mention} 📋 New task assignment!" if discord_user else "📋 New task assignment!"
                
                await new_thread.send(content=content, embed=embed)
            
            # Notify original user in their thread
            orig_thread = await self.thread_manager.get_or_create_operator_thread(
                guild, original_assignment.user_id, original_user.display_name
            )
            
            if orig_thread:
                embed = discord.Embed(
                    title="⚠️ Task Reassigned",
                    description=(
                        f"Your task has been reassigned due to no acknowledgment.\n\n"
                        f"**Task:** {original_assignment.task_name}\n"
                        f"**Hour:** {original_assignment.hour_index}\n"
                        f"**Reassigned to:** {new_assignee.display_name}\n\n"
                        "Please make sure to acknowledge future tasks promptly."
                    ),
                    color=0xff6b6b,
                    timestamp=datetime.utcnow()
                )
                
                await orig_thread.send(embed=embed)
            
            # Send admin notification
            await self._send_admin_reassignment_alert(
                original_assignment, original_user, new_assignee, settings, reason
            )
            
        except Exception as e:
            logger.error(f"Failed to send reassignment notifications: {e}")
            
    async def _send_admin_reassignment_alert(
        self,
        assignment: Assignment,
        original_user: User, 
        new_assignee: User,
        settings,
        reason: str
    ):
        """Send admin alert about task reassignment"""
        try:
            if not settings.admin_channel_id:
                return
            
            # Find guild and admin channel
            guild = None
            for g in self.bot.guilds:
                if g.get_member(int(original_user.id)):
                    guild = g
                    break
            
            if not guild:
                return
            
            admin_channel = guild.get_channel(int(settings.admin_channel_id))
            if not admin_channel:
                return
            
            embed = discord.Embed(
                title="🔄 Task Reassigned",
                description=f"Task reassignment due to {reason}",
                color=0xffa500
            )
            
            embed.add_field(
                name="Original Operator",
                value=original_user.display_name,
                inline=True
            )
            
            embed.add_field(
                name="New Operator", 
                value=new_assignee.display_name,
                inline=True
            )
            
            embed.add_field(
                name="Task",
                value=assignment.task_name,
                inline=True
            )
            
            embed.add_field(
                name="Hour",
                value=str(assignment.hour_index),
                inline=True
            )
            
            embed.add_field(
                name="Reason",
                value=reason.title(),
                inline=True
            )
            
            embed.set_footer(text=f"Assignment ID: {assignment.id}")
            embed.timestamp = datetime.utcnow()
            
            await admin_channel.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Failed to send admin reassignment alert: {e}")
            
    async def get_reassignment_candidates(self, assignment: Assignment) -> List[User]:
        """Find operators available for reassignment"""
        try:
            with get_db_session() as db:
                now_utc = datetime.now(timezone.utc)
                
                # Find operators with Data Labelling assignments in the same hour
                candidates = db.query(User).join(Assignment).filter(
                    Assignment.hour_index == assignment.hour_index,
                    Assignment.status == AssignmentStatus.ACTIVE,
                    Assignment.task_name == "Data Labelling",
                    Assignment.user_id != assignment.user_id,
                    User.is_operator == True
                ).all()
                
                return candidates
                
        except Exception as e:
            logger.error(f"Failed to get reassignment candidates: {e}")
            return []
            
    async def update_dashboard(self):
        """Update the persistent dashboard message"""
        try:
            # TODO: Implement dashboard updating
            # This will be implemented when we create the dashboard module
            pass
            
        except Exception as e:
            logger.error(f"Failed to update dashboard: {e}")


class AssignmentView(discord.ui.View):
    """Interactive view for assignment widgets with buttons"""
    
    def __init__(self, assignment_id: int, hour_index: int, status: AssignmentStatus):
        super().__init__(timeout=3600)  # 1 hour timeout
        self.assignment_id = assignment_id
        self.hour_index = hour_index
        self.assignment_status = status
        
        # Configure buttons based on current status
        self._configure_buttons()
    
    def _configure_buttons(self):
        """Configure which buttons are enabled based on assignment status"""
        # Start button - only available if pending acknowledgment
        self.start_button.disabled = self.assignment_status != AssignmentStatus.PENDING_ACK
        
        # Edit/End Early buttons - only available if active
        can_edit = self.assignment_status in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]
        self.edit_button.disabled = not can_edit
        self.end_early_button.disabled = not can_edit
        
        # Break buttons - available if active and not already on break/lunch
        can_break = self.assignment_status in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]
        self.break_button.disabled = not can_break
        
        # Lunch button - only available hours 3-5 and if active
        can_lunch = (can_break and self.hour_index in [3, 4, 5])
        self.lunch_button.disabled = not can_lunch
    
    @discord.ui.button(
        label="▶️ Start Task",
        style=discord.ButtonStyle.primary,
        custom_id="start_task"
    )
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle start task button click"""
        try:
            from .assignment_operations import AssignmentOperations
            
            # Initialize operations service
            operations = AssignmentOperations(interaction.client)
            
            # Start the task
            success, message = await operations.start_task(
                self.assignment_id,
                str(interaction.user.id)
            )
            
            if success:
                # Update button states
                self.assignment_status = AssignmentStatus.ACTIVE
                self._configure_buttons()
                
                # Update the widget message
                await self._update_widget_message(interaction, operations)
                
                await interaction.response.send_message(
                    f"✅ {message}",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ {message}",
                    ephemeral=True
                )
            
        except Exception as e:
            logger.error(f"Error in start task button: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while starting the task.",
                ephemeral=True
            )
    
    @discord.ui.button(
        label="✏️ Edit Task",
        style=discord.ButtonStyle.secondary,
        custom_id="edit_task"
    )
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle edit task button click"""
        try:
            from .assignment_operations import AssignmentOperations
            from .modals import EditTaskModal
            
            # Check if user can edit this assignment
            operations = AssignmentOperations(interaction.client)
            if not await operations.can_user_interact(self.assignment_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "❌ You can only edit your own tasks.",
                    ephemeral=True
                )
                return
            
            # Get current assignment parameters
            assignment = await operations.get_assignment_details(self.assignment_id)
            if not assignment:
                await interaction.response.send_message(
                    "❌ Assignment not found.",
                    ephemeral=True
                )
                return
            
            # Show edit modal
            modal = EditTaskModal(self.assignment_id, assignment.params or {})
            await interaction.response.send_modal(modal)
            
        except Exception as e:
            logger.error(f"Error in edit task button: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while opening the edit dialog.",
                ephemeral=True
            )
    
    @discord.ui.button(
        label="⏹️ End Early",
        style=discord.ButtonStyle.secondary,
        custom_id="end_early"
    )
    async def end_early_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle end early button click"""
        try:
            from .assignment_operations import AssignmentOperations
            from .modals import EndEarlyModal
            
            # Check if user can end this assignment early
            operations = AssignmentOperations(interaction.client)
            if not await operations.can_user_interact(self.assignment_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "❌ You can only end your own tasks early.",
                    ephemeral=True
                )
                return
            
            # Verify assignment is in correct state
            assignment = await operations.get_assignment_details(self.assignment_id)
            if not assignment:
                await interaction.response.send_message(
                    "❌ Assignment not found.",
                    ephemeral=True
                )
                return
            
            if assignment.status not in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]:
                await interaction.response.send_message(
                    "❌ Task must be active to request early end.",
                    ephemeral=True
                )
                return
            
            # Show end early modal
            modal = EndEarlyModal(self.assignment_id)
            await interaction.response.send_modal(modal)
            
        except Exception as e:
            logger.error(f"Error in end early button: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while opening the end early dialog.",
                ephemeral=True
            )
    
    @discord.ui.button(
        label="☕ Break (15m)",
        style=discord.ButtonStyle.secondary,
        custom_id="break_15m",
        row=1
    )
    async def break_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle break button click"""
        try:
            # TODO: Implement break modal and approval
            await interaction.response.send_message(
                "☕ Break request (Implementation coming soon)",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Error in break button: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while requesting a break.",
                ephemeral=True
            )
    
    @discord.ui.button(
        label="🍽️ Lunch (60m)",
        style=discord.ButtonStyle.secondary,
        custom_id="lunch_60m",
        row=1
    )
    async def lunch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle lunch button click"""
        try:
            # TODO: Implement lunch modal and approval
            await interaction.response.send_message(
                "🍽️ Lunch request (Implementation coming soon)",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Error in lunch button: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while requesting lunch.",
                ephemeral=True
            )
    
    async def _update_widget_message(self, interaction: discord.Interaction, operations):
        """Update the widget message with current assignment state"""
        try:
            # Get updated assignment details
            assignment = await operations.get_assignment_details(self.assignment_id)
            if not assignment:
                return
            
            # Get user details
            with get_db_session() as db:
                user = db.query(User).filter(User.id == assignment.user_id).first()
                if not user:
                    return
            
            # Recreate embed with updated information
            embed = await self._create_updated_embed(assignment, user)
            
            # Update the message (this will be deferred if interaction was already responded to)
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=embed, view=self)
                else:
                    await interaction.edit_original_response(embed=embed, view=self)
            except discord.NotFound:
                # Message might have been deleted, ignore
                pass
            except discord.HTTPException as e:
                logger.warning(f"Failed to update widget message: {e}")
                
        except Exception as e:
            logger.error(f"Failed to update widget message: {e}")
    
    async def _create_updated_embed(self, assignment: Assignment, user: User):
        """Create updated embed with current assignment state"""
        # Calculate time remaining
        time_remaining = "Unknown"
        if assignment.ends_at:
            now = datetime.now(timezone.utc)
            if assignment.ends_at > now:
                remaining = assignment.ends_at - now
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                if hours > 0:
                    time_remaining = f"{hours}h {minutes}m"
                else:
                    time_remaining = f"{minutes}m"
            else:
                time_remaining = "Overdue"
        
        # Status-based coloring
        color = 0x3498db  # Blue for pending
        if assignment.status == AssignmentStatus.ACTIVE:
            color = 0x00ff00  # Green for active
        elif assignment.status == AssignmentStatus.COMPLETED:
            color = 0x9932cc  # Purple for completed
        elif assignment.status in [AssignmentStatus.PAUSED_BREAK, AssignmentStatus.PAUSED_LUNCH]:
            color = 0xffa500  # Orange for paused
        
        # Create embed
        embed = discord.Embed(
            title=f"📋 Hour {assignment.hour_index} Assignment",
            color=color
        )
        
        embed.add_field(
            name="Task",
            value=assignment.task_name,
            inline=True
        )
        
        embed.add_field(
            name="Status", 
            value=assignment.status.value.replace('_', ' ').title(),
            inline=True
        )
        
        embed.add_field(
            name="Time Remaining",
            value=time_remaining,
            inline=True
        )
        
        if assignment.started_at:
            embed.add_field(
                name="Started At",
                value=f"<t:{int(assignment.started_at.timestamp())}:t>",
                inline=True
            )
        
        if assignment.ends_at:
            embed.add_field(
                name="Ends At",
                value=f"<t:{int(assignment.ends_at.timestamp())}:t>",
                inline=True
            )
        
        # Add task completion indicator
        if assignment.status == AssignmentStatus.COMPLETED:
            if assignment.ended_at and assignment.started_at:
                duration_minutes = int((assignment.ended_at - assignment.started_at).total_seconds() / 60)
                embed.add_field(
                    name="Duration",
                    value=f"{duration_minutes} minutes",
                    inline=True
                )
            embed.add_field(
                name="🎉 Completed!",
                value="Great work completing this task!",
                inline=False
            )
        
        # Add instructions if available
        with get_db_session() as db:
            if assignment.template_id:
                template = db.query(TaskTemplate).filter(TaskTemplate.id == assignment.template_id).first()
                if template and template.instructions:
                    embed.add_field(
                        name="Instructions",
                        value=template.instructions[:200] + ("..." if len(template.instructions) > 200 else ""),
                        inline=False
                    )
        
        embed.set_footer(text=f"Assignment ID: {assignment.id}")
        embed.timestamp = datetime.utcnow()
        
        return embed
    
    async def on_timeout(self):
        """Called when the view times out"""
        # Disable all buttons
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        
        # Note: We can't edit the message here without storing a reference to it
        # The message editing will be handled by the scheduler's periodic updates
