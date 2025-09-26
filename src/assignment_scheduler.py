"""
Assignment scheduler for hourly task posting and escalation handling.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from .database import get_db_session
from .models import User, Shift, Assignment, AssignmentStatus, Settings, log_action
from .selectors import SelectionService

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
            # This will be implemented when we create the thread management
            # For now, just log what would happen
            logger.info(f"Would post assignment widget for user {assignment.user_id}, task {assignment.task_name}")
            
            # TODO: Implement actual widget posting
            # - Find or create private thread for user
            # - Post embed with task details
            # - Add interactive buttons (Start, Edit, End Early, etc.)
            
        except Exception as e:
            logger.error(f"Failed to post assignment widget for {assignment.id}: {e}")
            
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
            
            # TODO: Send reminder to operator thread
            # TODO: Send alert to admin channel
            
            with get_db_session() as db:
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
            
    async def escalate_unacknowledged_assignment(self, assignment: Assignment):
        """Escalate unacknowledged non-Data Labelling assignment"""
        try:
            logger.info(f"Escalating unacknowledged assignment {assignment.id}")
            
            with get_db_session() as db:
                # Try to find a Data Labelling operator to reassign to
                candidates = await self.get_reassignment_candidates(assignment)
                
                if candidates:
                    # Reassign to first candidate
                    new_assignee = candidates[0]
                    
                    # TODO: Implement actual reassignment
                    logger.info(f"Would reassign {assignment.task_name} from {assignment.user_id} to {new_assignee.id}")
                    
                else:
                    # No candidates available, mark as queued
                    logger.warning(f"No reassignment candidates available for assignment {assignment.id}")
                
                log_action(
                    db,
                    action="assignment_escalated",
                    target=str(assignment.id),
                    metadata={
                        "user_id": assignment.user_id,
                        "task_name": assignment.task_name,
                        "candidates_found": len(candidates),
                        "minutes_elapsed": 10
                    }
                )
            
        except Exception as e:
            logger.error(f"Failed to escalate assignment {assignment.id}: {e}")
            
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
