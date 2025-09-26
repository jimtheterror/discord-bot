"""
Assignment operations service for handling task state transitions.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple

from sqlalchemy.orm import Session
from .database import get_db_session
from .models import Assignment, AssignmentStatus, User, ApprovalRequest, ApprovalType, ApprovalStatus, log_action

logger = logging.getLogger(__name__)


class AssignmentOperations:
    """Service for handling assignment state transitions and operations"""
    
    def __init__(self, bot):
        self.bot = bot
        
    async def start_task(self, assignment_id: int, user_id: str) -> Tuple[bool, str]:
        """
        Start a task by transitioning from PENDING_ACK to ACTIVE.
        
        Args:
            assignment_id: ID of the assignment to start
            user_id: ID of the user starting the task (for validation)
            
        Returns:
            (success, message) tuple
        """
        try:
            with get_db_session() as db:
                # Get the assignment
                assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
                if not assignment:
                    return False, "Assignment not found"
                
                # Validate user ownership
                if assignment.user_id != user_id:
                    return False, "You can only start your own tasks"
                
                # Check current status
                if assignment.status != AssignmentStatus.PENDING_ACK:
                    return False, f"Task is already {assignment.status.value.replace('_', ' ')}"
                
                # Update assignment status
                now_utc = datetime.now(timezone.utc)
                assignment.status = AssignmentStatus.ACTIVE
                assignment.started_at = now_utc
                
                # Ensure ends_at is set to hour boundary if not already set
                if not assignment.ends_at:
                    assignment.ends_at = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                
                db.commit()
                
                # Log the action
                log_action(
                    db,
                    action="task_started",
                    actor_id=user_id,
                    target=str(assignment_id),
                    metadata={
                        "task_name": assignment.task_name,
                        "hour_index": assignment.hour_index,
                        "started_at": now_utc.isoformat()
                    }
                )
                
                logger.info(f"Task started: assignment {assignment_id} by user {user_id}")
                return True, "Task started successfully!"
                
        except Exception as e:
            logger.error(f"Failed to start task {assignment_id}: {e}")
            return False, "An error occurred while starting the task"
            
    async def complete_task(self, assignment_id: int, user_id: str) -> Tuple[bool, str]:
        """
        Complete a task naturally at the hour boundary.
        
        Args:
            assignment_id: ID of the assignment to complete
            user_id: ID of the user completing the task
            
        Returns:
            (success, message) tuple
        """
        try:
            with get_db_session() as db:
                assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
                if not assignment:
                    return False, "Assignment not found"
                
                if assignment.user_id != user_id:
                    return False, "You can only complete your own tasks"
                
                if assignment.status not in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]:
                    return False, f"Task is not active (current status: {assignment.status.value})"
                
                # Update assignment
                now_utc = datetime.now(timezone.utc)
                assignment.status = AssignmentStatus.COMPLETED
                assignment.ended_at = now_utc
                
                db.commit()
                
                # Log the action
                log_action(
                    db,
                    action="task_completed",
                    actor_id=user_id,
                    target=str(assignment_id),
                    metadata={
                        "task_name": assignment.task_name,
                        "hour_index": assignment.hour_index,
                        "completed_at": now_utc.isoformat(),
                        "duration_minutes": int((now_utc - assignment.started_at).total_seconds() / 60) if assignment.started_at else None
                    }
                )
                
                logger.info(f"Task completed: assignment {assignment_id} by user {user_id}")
                return True, "🎉 Task completed successfully! Great work!"
                
        except Exception as e:
            logger.error(f"Failed to complete task {assignment_id}: {e}")
            return False, "An error occurred while completing the task"
            
    async def request_edit(
        self, 
        assignment_id: int, 
        user_id: str, 
        proposed_changes: Dict[str, Any],
        reason: str
    ) -> Tuple[bool, str]:
        """
        Request to edit task parameters (requires admin approval).
        
        Args:
            assignment_id: ID of the assignment to edit
            user_id: ID of the user requesting edit
            proposed_changes: Dict of proposed parameter changes
            reason: Reason for the edit request
            
        Returns:
            (success, message) tuple
        """
        try:
            with get_db_session() as db:
                assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
                if not assignment:
                    return False, "Assignment not found"
                
                if assignment.user_id != user_id:
                    return False, "You can only edit your own tasks"
                
                if assignment.status not in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]:
                    return False, "Task must be active to request edits"
                
                # Check for existing pending edit requests
                existing_request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.assignment_id == assignment_id,
                    ApprovalRequest.type == ApprovalType.EDIT,
                    ApprovalRequest.status == ApprovalStatus.PENDING
                ).first()
                
                if existing_request:
                    return False, "You already have a pending edit request for this task"
                
                # TODO: Check cooldown
                
                # Create approval request
                approval_request = ApprovalRequest(
                    user_id=user_id,
                    assignment_id=assignment_id,
                    type=ApprovalType.EDIT,
                    payload={
                        "proposed_changes": proposed_changes,
                        "reason": reason
                    }
                )
                
                db.add(approval_request)
                db.commit()
                
                # Log the action
                log_action(
                    db,
                    action="edit_request_created",
                    actor_id=user_id,
                    target=str(assignment_id),
                    metadata={
                        "request_id": approval_request.id,
                        "reason": reason,
                        "proposed_changes": proposed_changes
                    }
                )
                
                # TODO: Send admin notification
                
                logger.info(f"Edit request created: assignment {assignment_id} by user {user_id}")
                return True, "📝 Edit request submitted and sent to admins for approval"
                
        except Exception as e:
            logger.error(f"Failed to create edit request for {assignment_id}: {e}")
            return False, "An error occurred while creating the edit request"
            
    async def request_end_early(
        self,
        assignment_id: int,
        user_id: str, 
        reason: str
    ) -> Tuple[bool, str]:
        """
        Request to end task early (requires admin approval).
        
        Args:
            assignment_id: ID of the assignment to end early
            user_id: ID of the user requesting early end
            reason: Reason for ending early
            
        Returns:
            (success, message) tuple
        """
        try:
            with get_db_session() as db:
                assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
                if not assignment:
                    return False, "Assignment not found"
                
                if assignment.user_id != user_id:
                    return False, "You can only end your own tasks early"
                
                if assignment.status not in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING]:
                    return False, "Task must be active to request early end"
                
                # Check for existing pending end early requests
                existing_request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.assignment_id == assignment_id,
                    ApprovalRequest.type == ApprovalType.END_EARLY,
                    ApprovalRequest.status == ApprovalStatus.PENDING
                ).first()
                
                if existing_request:
                    return False, "You already have a pending end early request for this task"
                
                # TODO: Check cooldown
                
                # Create approval request
                approval_request = ApprovalRequest(
                    user_id=user_id,
                    assignment_id=assignment_id,
                    type=ApprovalType.END_EARLY,
                    payload={
                        "reason": reason
                    }
                )
                
                db.add(approval_request)
                db.commit()
                
                # Log the action
                log_action(
                    db,
                    action="end_early_request_created",
                    actor_id=user_id,
                    target=str(assignment_id),
                    metadata={
                        "request_id": approval_request.id,
                        "reason": reason
                    }
                )
                
                # TODO: Send admin notification
                
                logger.info(f"End early request created: assignment {assignment_id} by user {user_id}")
                return True, "⏹️ End early request submitted and sent to admins for approval"
                
        except Exception as e:
            logger.error(f"Failed to create end early request for {assignment_id}: {e}")
            return False, "An error occurred while creating the end early request"
            
    async def get_assignment_details(self, assignment_id: int) -> Optional[Assignment]:
        """Get assignment details by ID"""
        try:
            with get_db_session() as db:
                return db.query(Assignment).filter(Assignment.id == assignment_id).first()
        except Exception as e:
            logger.error(f"Failed to get assignment {assignment_id}: {e}")
            return None
            
    async def can_user_interact(self, assignment_id: int, user_id: str) -> bool:
        """Check if user can interact with the assignment"""
        try:
            with get_db_session() as db:
                assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
                return assignment and assignment.user_id == user_id
        except Exception as e:
            logger.error(f"Failed to check user permissions for assignment {assignment_id}: {e}")
            return False
