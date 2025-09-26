"""
Core dashboard system for real-time assignment monitoring.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

import discord

from .database import get_db_session
from .models import (
    Assignment, AssignmentStatus, User, Shift, Settings, DashState,
    TaskTemplate, get_settings, log_action
)

logger = logging.getLogger(__name__)


@dataclass
class DashboardStats:
    """Statistics for dashboard display"""
    total_on_duty: int
    total_active: int
    total_pending_ack: int
    pending_over_5min: int
    on_break: int
    on_lunch: int
    covering: int
    min_on_duty: int
    timed_tasks_active: int


@dataclass
class OperatorStatus:
    """Individual operator status for dashboard"""
    user_id: str
    display_name: str
    hour_index: int
    task_name: str
    status: AssignmentStatus
    status_display: str
    ends_at: Optional[datetime]
    ends_at_display: str
    covering_for: Optional[str]


class DashboardManager:
    """Manages the persistent live dashboard"""
    
    def __init__(self, bot):
        self.bot = bot
        self.dashboard_message_id = None
        self.dashboard_channel_id = None
        self._load_dashboard_state()
    
    def _load_dashboard_state(self):
        """Load dashboard state from database"""
        try:
            with get_db_session() as db:
                dash_state = db.query(DashState).first()
                if dash_state:
                    self.dashboard_message_id = dash_state.dashboard_message_id
        except Exception as e:
            logger.error(f"Failed to load dashboard state: {e}")
    
    def _save_dashboard_state(self):
        """Save dashboard state to database"""
        try:
            with get_db_session() as db:
                dash_state = db.query(DashState).first()
                if not dash_state:
                    dash_state = DashState(dashboard_message_id=self.dashboard_message_id)
                    db.add(dash_state)
                else:
                    dash_state.dashboard_message_id = self.dashboard_message_id
                db.commit()
        except Exception as e:
            logger.error(f"Failed to save dashboard state: {e}")
    
    async def create_or_update_dashboard(self, channel: discord.TextChannel) -> bool:
        """Create or update the persistent dashboard"""
        try:
            embed = await self._generate_dashboard_embed()
            
            # Try to update existing dashboard
            if self.dashboard_message_id:
                try:
                    message = await channel.fetch_message(int(self.dashboard_message_id))
                    await message.edit(embed=embed)
                    logger.info("Updated existing dashboard")
                    return True
                except discord.NotFound:
                    # Message was deleted, create new one
                    self.dashboard_message_id = None
                except Exception as e:
                    logger.warning(f"Failed to update existing dashboard: {e}")
            
            # Create new dashboard
            from .dashboard_views import DashboardView
            view = DashboardView()
            message = await channel.send(embed=embed, view=view)
            
            # Pin the message
            try:
                await message.pin()
            except discord.Forbidden:
                logger.warning("Failed to pin dashboard message - missing permissions")
            
            self.dashboard_message_id = str(message.id)
            self.dashboard_channel_id = str(channel.id)
            self._save_dashboard_state()
            
            logger.info(f"Created new dashboard message: {message.id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create/update dashboard: {e}")
            return False
    
    async def _generate_dashboard_embed(self) -> discord.Embed:
        """Generate the dashboard embed with current data"""
        try:
            stats = await self._gather_dashboard_stats()
            operators = await self._gather_operator_statuses()
            
            embed = discord.Embed(
                title="📊 **LIVE ASSIGNMENT DASHBOARD**",
                color=0x00ff00 if stats.total_active >= stats.min_on_duty else 0xffa500,
                timestamp=datetime.utcnow()
            )
            
            # Summary statistics
            embed.add_field(
                name="📈 **System Status**",
                value=(
                    f"**On Duty:** {stats.total_on_duty}\n"
                    f"**Active:** {stats.total_active}\n"
                    f"**Pending Ack:** {stats.total_pending_ack}\n"
                    f"**⚠️ Overdue (>5m):** {stats.pending_over_5min}"
                ),
                inline=True
            )
            
            embed.add_field(
                name="🔄 **Break Status**",
                value=(
                    f"**On Break:** {stats.on_break}\n"
                    f"**At Lunch:** {stats.on_lunch}\n"
                    f"**Covering:** {stats.covering}\n"
                    f"**Min Required:** {stats.min_on_duty}"
                ),
                inline=True
            )
            
            embed.add_field(
                name="⏰ **Task Types**",
                value=(
                    f"**Timed Tasks:** {stats.timed_tasks_active}\n"
                    f"**Force Assigned:** {len([o for o in operators if 'forced' in o.status_display.lower()])}\n"
                    f"**Auto-Assigned:** {stats.total_active - stats.timed_tasks_active}\n"
                    f"**Total Assignments:** {len(operators)}"
                ),
                inline=True
            )
            
            # Operator table
            if operators:
                table_text = self._format_operator_table(operators)
                embed.add_field(
                    name="👥 **Operator Status**",
                    value=f"```\n{table_text}\n```",
                    inline=False
                )
            else:
                embed.add_field(
                    name="👥 **Operator Status**",
                    value="```\nNo operators currently on duty\n```",
                    inline=False
                )
            
            # Status legend
            embed.add_field(
                name="📋 **Status Legend**",
                value=(
                    "🟢 **Active** | 🔵 **Pending** | 🟣 **Done** | 🟠 **Break/Lunch**\n"
                    "🔄 **Covering** | ⏹️ **Ended Early** | ⚠️ **Overdue** | 🚀 **Forced**"
                ),
                inline=False
            )
            
            embed.set_footer(
                text=f"🔄 Auto-updates every minute | Last: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
            )
            
            return embed
            
        except Exception as e:
            logger.error(f"Failed to generate dashboard embed: {e}")
            return discord.Embed(
                title="❌ Dashboard Error",
                description="Failed to generate dashboard data. Check logs for details.",
                color=0xff0000
            )
    
    async def _gather_dashboard_stats(self) -> DashboardStats:
        """Gather statistics for dashboard"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                now_utc = datetime.now(timezone.utc)
                
                # Get current assignments (last 2 hours)
                current_assignments = db.query(Assignment).filter(
                    Assignment.created_at >= now_utc - timedelta(hours=2),
                    Assignment.status.in_([
                        AssignmentStatus.PENDING_ACK,
                        AssignmentStatus.ACTIVE,
                        AssignmentStatus.COVERING,
                        AssignmentStatus.PAUSED_BREAK,
                        AssignmentStatus.PAUSED_LUNCH
                    ])
                ).all()
                
                total_on_duty = len(set(a.user_id for a in current_assignments))
                total_active = len([a for a in current_assignments if a.status == AssignmentStatus.ACTIVE])
                total_pending = len([a for a in current_assignments if a.status == AssignmentStatus.PENDING_ACK])
                
                # Count overdue pending (>5 minutes)
                pending_over_5min = 0
                for assignment in current_assignments:
                    if (assignment.status == AssignmentStatus.PENDING_ACK and 
                        assignment.created_at <= now_utc - timedelta(minutes=5)):
                        pending_over_5min += 1
                
                on_break = len([a for a in current_assignments if a.status == AssignmentStatus.PAUSED_BREAK])
                on_lunch = len([a for a in current_assignments if a.status == AssignmentStatus.PAUSED_LUNCH])
                covering = len([a for a in current_assignments if a.status == AssignmentStatus.COVERING])
                
                # Count timed tasks
                timed_tasks = 0
                for assignment in current_assignments:
                    if assignment.template_id:
                        template = db.query(TaskTemplate).filter(TaskTemplate.id == assignment.template_id).first()
                        if template and (template.window_start or template.window_end):
                            timed_tasks += 1
                
                return DashboardStats(
                    total_on_duty=total_on_duty,
                    total_active=total_active,
                    total_pending_ack=total_pending,
                    pending_over_5min=pending_over_5min,
                    on_break=on_break,
                    on_lunch=on_lunch,
                    covering=covering,
                    min_on_duty=settings.min_on_duty,
                    timed_tasks_active=timed_tasks
                )
                
        except Exception as e:
            logger.error(f"Failed to gather dashboard stats: {e}")
            return DashboardStats(0, 0, 0, 0, 0, 0, 0, 3, 0)
    
    async def _gather_operator_statuses(self) -> List[OperatorStatus]:
        """Gather individual operator statuses"""
        try:
            with get_db_session() as db:
                now_utc = datetime.now(timezone.utc)
                statuses = []
                
                current_assignments = db.query(Assignment).filter(
                    Assignment.created_at >= now_utc - timedelta(hours=2),
                    Assignment.status.in_([
                        AssignmentStatus.PENDING_ACK,
                        AssignmentStatus.ACTIVE,
                        AssignmentStatus.COVERING,
                        AssignmentStatus.PAUSED_BREAK,
                        AssignmentStatus.PAUSED_LUNCH
                    ])
                ).order_by(Assignment.user_id, Assignment.hour_index).all()
                
                for assignment in current_assignments:
                    user = db.query(User).filter(User.id == assignment.user_id).first()
                    if not user:
                        continue
                    
                    # Format status display
                    status_display = self._format_status_display(assignment, now_utc)
                    
                    # Format end time
                    ends_at_display = "Unknown"
                    if assignment.ends_at:
                        if assignment.status in [AssignmentStatus.PAUSED_BREAK, AssignmentStatus.PAUSED_LUNCH]:
                            ends_at_display = "On Break"
                        else:
                            ends_at_display = assignment.ends_at.strftime("%H:%M")
                    
                    # Check for covering
                    covering_for = None
                    if assignment.covering_for_user_id:
                        covering_user = db.query(User).filter(User.id == assignment.covering_for_user_id).first()
                        if covering_user:
                            covering_for = covering_user.display_name
                    
                    statuses.append(OperatorStatus(
                        user_id=assignment.user_id,
                        display_name=user.display_name[:15],
                        hour_index=assignment.hour_index,
                        task_name=assignment.task_name[:18],
                        status=assignment.status,
                        status_display=status_display,
                        ends_at=assignment.ends_at,
                        ends_at_display=ends_at_display,
                        covering_for=covering_for[:10] if covering_for else None
                    ))
                
                return sorted(statuses, key=lambda x: x.display_name)
                
        except Exception as e:
            logger.error(f"Failed to gather operator statuses: {e}")
            return []
    
    def _format_status_display(self, assignment: Assignment, now_utc: datetime) -> str:
        """Format status display with emoji"""
        status_map = {
            AssignmentStatus.PENDING_ACK: "🔵 Pending",
            AssignmentStatus.ACTIVE: "🟢 Active",
            AssignmentStatus.COVERING: "🔄 Covering",
            AssignmentStatus.PAUSED_BREAK: "🟠 Break",
            AssignmentStatus.PAUSED_LUNCH: "🟠 Lunch",
            AssignmentStatus.COMPLETED: "🟣 Done",
            AssignmentStatus.ENDED_EARLY: "⏹️ Ended"
        }
        
        base_status = status_map.get(assignment.status, "❓ Unknown")
        
        if assignment.status == AssignmentStatus.PENDING_ACK:
            elapsed = now_utc - assignment.created_at
            if elapsed >= timedelta(minutes=5):
                base_status = "⚠️ Overdue"
        
        if assignment.forced:
            base_status += " 🚀"
        
        return base_status
    
    def _format_operator_table(self, operators: List[OperatorStatus]) -> str:
        """Format operators into a table"""
        if not operators:
            return "No operators on duty"
        
        lines = ["Operator        | Hr | Task              | Status      | Ends  "]
        lines.append("----------------+----+-------------------+-------------+-------")
        
        for op in operators[:20]:  # Limit to prevent embed size issues
            name = op.display_name[:15].ljust(15)
            hour = str(op.hour_index).rjust(2)
            task = op.task_name[:17].ljust(17)
            status = op.status_display[:11].ljust(11)
            ends = op.ends_at_display[:5].ljust(5)
            
            lines.append(f"{name} | {hour} | {task} | {status} | {ends}")
        
        if len(operators) > 20:
            lines.append(f"... and {len(operators) - 20} more operators")
        
        return "\n".join(lines)
    
    async def update_dashboard(self):
        """Update the dashboard if it exists"""
        if not self.dashboard_message_id:
            return
        
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                if not settings.admin_channel_id:
                    return
            
            for guild in self.bot.guilds:
                try:
                    channel = guild.get_channel(int(settings.admin_channel_id))
                    if channel:
                        await self.create_or_update_dashboard(channel)
                        return
                except (ValueError, AttributeError):
                    continue
                    
        except Exception as e:
            logger.error(f"Failed to update dashboard: {e}")
    
    async def create_snapshot(self, channel: discord.TextChannel) -> bool:
        """Create a static snapshot for audit purposes"""
        try:
            embed = await self._generate_dashboard_embed()
            embed.title = "📸 **ASSIGNMENT DASHBOARD SNAPSHOT**"
            embed.color = 0x9932cc
            embed.set_footer(text=f"Snapshot: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
            
            await channel.send(embed=embed)
            
            with get_db_session() as db:
                log_action(db, action="dashboard_snapshot_created", metadata={"channel_id": str(channel.id)})
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to create snapshot: {e}")
            return False
