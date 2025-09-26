"""
Break management system for handling operator breaks and lunch periods.
"""
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

import discord

from database import get_db_session
from models import (
    Assignment, AssignmentStatus, User, ApprovalRequest, ApprovalType, 
    ApprovalStatus, Settings, get_settings, log_action
)
from selection_service import SelectionService

logger = logging.getLogger(__name__)


class BreakManager:
    """Manages break requests, approvals, and coverage assignments"""
    
    def __init__(self, bot):
        self.bot = bot
        self.selection_service = SelectionService()
        self.break_timers = {}  # Track active break countdowns
        
    async def request_break(
        self,
        assignment_id: int,
        user_id: str,
        break_type: str,
        reason: str,
        duration_minutes: int
    ) -> Tuple[bool, str]:
        """
        Request a break (15m) or lunch (60m).
        
        Args:
            assignment_id: ID of the current assignment
            user_id: ID of the user requesting break
            break_type: 'break15' or 'lunch60'
            reason: Reason for the break
            duration_minutes: Duration in minutes (15 or 60)
            
        Returns:
            (success, message) tuple
        """
        try:
            with get_db_session() as db:
                # Get assignment and validate
                assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
                if not assignment:
                    return False, "Assignment not found"
                
                if assignment.user_id != user_id:
                    return False, "You can only request breaks for your own tasks"
                
                if assignment.status not in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]:
                    return False, "Task must be active to request a break"
                
                # Check if user already has a pending break request
                existing_request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.user_id == user_id,
                    ApprovalRequest.type.in_([ApprovalType.BREAK15, ApprovalType.LUNCH60]),
                    ApprovalRequest.status.in_([ApprovalStatus.PENDING, ApprovalStatus.QUEUED_FOR_CAPACITY])
                ).first()
                
                if existing_request:
                    return False, "You already have a pending break request"
                
                # Check minimum staffing requirements
                settings = get_settings(db)
                current_active = self._get_current_active_assignments(db, assignment.hour_index)
                
                # Check if allowing this break would violate minimum staffing
                if not self.selection_service.check_minimum_staffing(
                    current_active, user_id, settings.min_on_duty
                ):
                    # Queue the request instead of denying it
                    approval_type = ApprovalType.BREAK15 if break_type == "break15" else ApprovalType.LUNCH60
                    
                    approval_request = ApprovalRequest(
                        user_id=user_id,
                        assignment_id=assignment_id,
                        type=approval_type,
                        payload={
                            "reason": reason,
                            "duration_minutes": duration_minutes
                        },
                        status=ApprovalStatus.QUEUED_FOR_CAPACITY
                    )
                    
                    db.add(approval_request)
                    db.commit()
                    
                    log_action(
                        db,
                        action="break_request_queued",
                        actor_id=user_id,
                        target=str(assignment_id),
                        metadata={
                            "break_type": break_type,
                            "reason": reason,
                            "queue_reason": "minimum_staffing"
                        }
                    )
                    
                    return True, f"⏳ Break request queued due to minimum staffing requirements. You'll be notified when capacity allows."
                
                # Create approval request
                approval_type = ApprovalType.BREAK15 if break_type == "break15" else ApprovalType.LUNCH60
                
                approval_request = ApprovalRequest(
                    user_id=user_id,
                    assignment_id=assignment_id,
                    type=approval_type,
                    payload={
                        "reason": reason,
                        "duration_minutes": duration_minutes
                    }
                )
                
                db.add(approval_request)
                db.commit()
                
                # Send admin approval request
                await self._send_break_approval_request(assignment, approval_request, reason, break_type)
                
                log_action(
                    db,
                    action="break_request_created",
                    actor_id=user_id,
                    target=str(assignment_id),
                    metadata={
                        "break_type": break_type,
                        "reason": reason,
                        "duration_minutes": duration_minutes
                    }
                )
                
                return True, f"📋 {break_type.title()} request sent to admins for approval"
                
        except Exception as e:
            logger.error(f"Failed to request break: {e}")
            return False, "An error occurred while processing your break request"
    
    def _get_current_active_assignments(self, db, hour_index: int) -> List[Assignment]:
        """Get all currently active assignments for the given hour"""
        return db.query(Assignment).filter(
            Assignment.hour_index == hour_index,
            Assignment.status.in_([
                AssignmentStatus.ACTIVE,
                AssignmentStatus.COVERING,
                AssignmentStatus.PAUSED_BREAK,
                AssignmentStatus.PAUSED_LUNCH
            ])
        ).all()
    
    async def resolve_break_request(
        self,
        assignment_id: int,
        user_id: str,
        break_type: ApprovalType,
        approved: bool,
        resolver_id: str,
        reason: str = ""
    ) -> Tuple[bool, str]:
        """
        Resolve a break approval request.
        
        Args:
            assignment_id: Assignment ID
            user_id: User requesting break
            break_type: BREAK15 or LUNCH60
            approved: Whether request was approved
            resolver_id: Admin who resolved the request
            reason: Optional reason for decision
            
        Returns:
            (success, message) tuple
        """
        try:
            with get_db_session() as db:
                # Find the approval request
                request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.assignment_id == assignment_id,
                    ApprovalRequest.user_id == user_id,
                    ApprovalRequest.type == break_type,
                    ApprovalRequest.status == ApprovalStatus.PENDING
                ).first()
                
                if not request:
                    return False, "Approval request not found or already processed"
                
                # Update the request
                request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
                request.resolved_at = datetime.now(timezone.utc)
                request.resolver_id = resolver_id
                request.resolver_note = reason
                
                if approved:
                    # Start the break
                    success = await self._start_break(db, assignment_id, user_id, request.payload)
                    if not success:
                        db.rollback()
                        return False, "Failed to start break - assignment may no longer be active"
                else:
                    # Log the denial
                    log_action(
                        db,
                        action="break_request_denied",
                        actor_id=resolver_id,
                        target=str(assignment_id),
                        metadata={
                            "original_user": user_id,
                            "break_type": break_type.value,
                            "reason": reason
                        }
                    )
                
                db.commit()
                
                # Notify the operator (TODO: implement notification)
                
                action = "approved" if approved else "denied"
                return True, f"Break request {action} successfully"
                
        except Exception as e:
            logger.error(f"Failed to resolve break request: {e}")
            return False, "An error occurred while resolving the break request"
    
    async def _start_break(self, db, assignment_id: int, user_id: str, break_payload: dict) -> bool:
        """Start a break by updating assignment status and setting up coverage"""
        try:
            assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
            if not assignment or assignment.status not in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]:
                return False
            
            duration_minutes = break_payload.get("duration_minutes", 15)
            break_type = "break" if duration_minutes == 15 else "lunch"
            
            # Update assignment status
            if break_type == "break":
                assignment.status = AssignmentStatus.PAUSED_BREAK
            else:
                assignment.status = AssignmentStatus.PAUSED_LUNCH
            
            # Set up coverage if needed for non-Data Labelling tasks
            coverage_assignment = None
            if assignment.task_name != "Data Labelling":
                coverage_assignment = await self._setup_break_coverage(db, assignment)
            
            # Start break countdown
            break_end_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
            
            # Schedule auto-resume
            asyncio.create_task(self._break_countdown(
                assignment_id, user_id, duration_minutes, coverage_assignment
            ))
            
            log_action(
                db,
                action="break_started",
                actor_id=user_id,
                target=str(assignment_id),
                metadata={
                    "duration_minutes": duration_minutes,
                    "break_type": break_type,
                    "coverage_assignment_id": coverage_assignment.id if coverage_assignment else None,
                    "break_end_time": break_end_time.isoformat()
                }
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start break: {e}")
            return False
    
    async def _setup_break_coverage(self, db, original_assignment: Assignment) -> Optional[Assignment]:
        """Set up coverage assignment for break"""
        try:
            # Find available Data Labellers in the same hour
            data_labellers = db.query(Assignment).filter(
                Assignment.hour_index == original_assignment.hour_index,
                Assignment.task_name == "Data Labelling",
                Assignment.status == AssignmentStatus.ACTIVE,
                Assignment.user_id != original_assignment.user_id
            ).all()
            
            if not data_labellers:
                logger.warning(f"No Data Labellers available for coverage of assignment {original_assignment.id}")
                return None
            
            # Select coverage operator (first available for now)
            coverage_assignment = data_labellers[0]
            
            # Update the Data Labeller's assignment to cover the original task
            coverage_assignment.task_name = original_assignment.task_name
            coverage_assignment.template_id = original_assignment.template_id
            coverage_assignment.params = original_assignment.params
            coverage_assignment.covering_for_user_id = original_assignment.user_id
            coverage_assignment.status = AssignmentStatus.COVERING
            
            logger.info(
                f"Set up coverage: {coverage_assignment.user_id} covering {original_assignment.task_name} "
                f"for {original_assignment.user_id}"
            )
            
            # TODO: Notify the coverage operator
            
            return coverage_assignment
            
        except Exception as e:
            logger.error(f"Failed to setup break coverage: {e}")
            return None
    
    async def _break_countdown(self, assignment_id: int, user_id: str, duration_minutes: int, coverage_assignment: Optional[Assignment]):
        """Handle break countdown and auto-resume"""
        try:
            # Store break info for tracking
            self.break_timers[assignment_id] = {
                'user_id': user_id,
                'start_time': datetime.now(timezone.utc),
                'duration_minutes': duration_minutes,
                'coverage_assignment': coverage_assignment
            }
            
            # Wait for break duration
            await asyncio.sleep(duration_minutes * 60)
            
            # Auto-resume the break
            await self._resume_from_break(assignment_id, user_id, coverage_assignment)
            
            # Clean up timer
            self.break_timers.pop(assignment_id, None)
            
        except asyncio.CancelledError:
            logger.info(f"Break countdown cancelled for assignment {assignment_id}")
        except Exception as e:
            logger.error(f"Error in break countdown: {e}")
    
    async def _resume_from_break(self, assignment_id: int, user_id: str, coverage_assignment: Optional[Assignment]):
        """Resume assignment after break ends"""
        try:
            with get_db_session() as db:
                assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
                if not assignment:
                    return
                
                # Check if assignment is still paused
                if assignment.status not in [AssignmentStatus.PAUSED_BREAK, AssignmentStatus.PAUSED_LUNCH]:
                    return  # Already resumed or ended
                
                # Resume original assignment
                assignment.status = AssignmentStatus.ACTIVE
                
                # Handle coverage cleanup
                if coverage_assignment:
                    # Return coverage operator to Data Labelling
                    with get_db_session() as db2:
                        current_coverage = db2.query(Assignment).filter(Assignment.id == coverage_assignment.id).first()
                        if current_coverage and current_coverage.status == AssignmentStatus.COVERING:
                            current_coverage.task_name = "Data Labelling"
                            current_coverage.template_id = None
                            current_coverage.params = {}
                            current_coverage.covering_for_user_id = None
                            current_coverage.status = AssignmentStatus.ACTIVE
                        db2.commit()
                
                db.commit()
                
                log_action(
                    db,
                    action="break_auto_resumed",
                    actor_id=user_id,
                    target=str(assignment_id),
                    metadata={
                        "coverage_returned": coverage_assignment is not None
                    }
                )
                
                # TODO: Notify operator that break is over
                # TODO: Send updated widget
                
                logger.info(f"Auto-resumed break for assignment {assignment_id}")
                
        except Exception as e:
            logger.error(f"Failed to resume from break: {e}")
    
    async def _send_break_approval_request(self, assignment: Assignment, request: ApprovalRequest, reason: str, break_type: str):
        """Send break approval request to admin channel"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                if not settings.admin_channel_id:
                    logger.warning("No admin channel configured for break approval")
                    return
                
                user = db.query(User).filter(User.id == assignment.user_id).first()
                if not user:
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
                    return
                
                # Create approval embed
                embed = discord.Embed(
                    title=f"{'☕ Break (15m)' if break_type == 'break15' else '🍽️ Lunch (60m)'} Approval Request",
                    color=0x3498db
                )
                
                embed.add_field(name="Operator", value=user.display_name, inline=True)
                embed.add_field(name="Task", value=assignment.task_name, inline=True)
                embed.add_field(name="Hour", value=str(assignment.hour_index), inline=True)
                embed.add_field(name="Reason", value=reason[:200], inline=False)
                
                # Add staffing impact
                current_active = self._get_current_active_assignments(db, assignment.hour_index)
                active_count = len([a for a in current_active if a.status in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]])
                
                embed.add_field(
                    name="Staffing Impact",
                    value=f"Currently {active_count} active → {active_count - 1} after break\nMinimum required: {settings.min_on_duty}",
                    inline=True
                )
                
                # Coverage info
                if assignment.task_name != "Data Labelling":
                    embed.add_field(
                        name="Coverage",
                        value="Will assign Data Labeller to cover this task",
                        inline=True
                    )
                
                embed.set_footer(text=f"Assignment ID: {assignment.id} | Request ID: {request.id}")
                embed.timestamp = datetime.utcnow()
                
                # Create approval view
                from .modals import BreakApprovalView
                view = BreakApprovalView(assignment.id, assignment.user_id, break_type)
                
                await admin_channel.send(
                    content=f"🔔 **{break_type.title()} Request Needs Approval**",
                    embed=embed,
                    view=view
                )
                
        except Exception as e:
            logger.error(f"Failed to send break approval request: {e}")
    
    async def check_queued_break_requests(self):
        """Check if any queued break requests can now be approved due to increased capacity"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                
                # Find queued requests
                queued_requests = db.query(ApprovalRequest).filter(
                    ApprovalRequest.status == ApprovalStatus.QUEUED_FOR_CAPACITY,
                    ApprovalRequest.type.in_([ApprovalType.BREAK15, ApprovalType.LUNCH60])
                ).order_by(ApprovalRequest.requested_at).all()  # FIFO
                
                for request in queued_requests:
                    # Check if staffing now allows this break
                    assignment = db.query(Assignment).filter(Assignment.id == request.assignment_id).first()
                    if not assignment or assignment.status not in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]:
                        # Assignment no longer active, remove from queue
                        request.status = ApprovalStatus.DENIED
                        request.resolved_at = datetime.now(timezone.utc)
                        request.resolver_note = "Assignment no longer active"
                        continue
                    
                    current_active = self._get_current_active_assignments(db, assignment.hour_index)
                    
                    if self.selection_service.check_minimum_staffing(
                        current_active, request.user_id, settings.min_on_duty
                    ):
                        # Capacity now available, move to pending approval
                        request.status = ApprovalStatus.PENDING
                        
                        # Send approval request to admins
                        break_type = "break15" if request.type == ApprovalType.BREAK15 else "lunch60"
                        await self._send_break_approval_request(
                            assignment, request, request.payload.get("reason", ""), break_type
                        )
                        
                        log_action(
                            db,
                            action="break_request_unqueued",
                            target=str(assignment.id),
                            metadata={
                                "user_id": request.user_id,
                                "break_type": request.type.value
                            }
                        )
                        
                        # TODO: Notify operator that their request is now pending approval
                        break  # Only process one at a time to avoid overwhelming admins
                
                db.commit()
                
        except Exception as e:
            logger.error(f"Failed to check queued break requests: {e}")
    
    def get_break_status(self, assignment_id: int) -> Optional[dict]:
        """Get current break status for an assignment"""
        return self.break_timers.get(assignment_id)
    
    async def cancel_break(self, assignment_id: int, user_id: str) -> Tuple[bool, str]:
        """Cancel an ongoing break early"""
        try:
            if assignment_id in self.break_timers:
                break_info = self.break_timers[assignment_id]
                if break_info['user_id'] == user_id:
                    # Resume from break immediately
                    await self._resume_from_break(
                        assignment_id, 
                        user_id, 
                        break_info.get('coverage_assignment')
                    )
                    
                    # Cancel the timer task (this will be handled by the countdown coroutine)
                    self.break_timers.pop(assignment_id, None)
                    
                    return True, "Break cancelled and assignment resumed"
                else:
                    return False, "You can only cancel your own breaks"
            else:
                return False, "No active break found for this assignment"
                
        except Exception as e:
            logger.error(f"Failed to cancel break: {e}")
            return False, "An error occurred while cancelling the break"
