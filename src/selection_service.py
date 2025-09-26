"""
Pure function selectors for task assignment logic.
All functions here should be stateless and easily testable.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from models import User, TaskTemplate, Assignment, AssignmentStatus

logger = logging.getLogger(__name__)


@dataclass
class TaskCandidate:
    """Represents a task that could be assigned"""
    template: TaskTemplate
    priority: int
    in_window: bool
    window_warning: Optional[str] = None


class SelectionService:
    """Service for selecting assignments and operators"""
    
    def select_comms_lead(self, operators: List[User]) -> Optional[User]:
        """
        Select Comms Lead using Least Recently Used (LRU) algorithm.
        
        Args:
            operators: List of on-shift operators
            
        Returns:
            User selected as Comms Lead, or None if no operators
        """
        if not operators:
            return None
            
        if len(operators) == 1:
            return operators[0]
            
        # Sort by last_comms_lead_at (None values first = never been Comms Lead)
        # Then by user ID for consistent tie-breaking
        sorted_operators = sorted(
            operators,
            key=lambda u: (
                u.last_comms_lead_at or datetime.min.replace(tzinfo=timezone.utc),
                u.id
            )
        )
        
        selected = sorted_operators[0]
        
        logger.info(
            f"Selected Comms Lead: {selected.display_name} "
            f"(last served: {selected.last_comms_lead_at or 'never'})"
        )
        
        return selected
        
    def select_task_from_pool(
        self, 
        templates: List[TaskTemplate], 
        current_time: Optional[datetime] = None
    ) -> Optional[TaskTemplate]:
        """
        Select task from pool based on priority, time windows, and FIFO.
        
        Priority rules:
        1. Explicit priority (lower number = higher priority)
        2. Time window filter (soft: warn if outside, don't block)
        3. FIFO among equals (created_at)
        
        Args:
            templates: Available task templates
            current_time: Current time for window checking (defaults to now)
            
        Returns:
            Selected TaskTemplate or None
        """
        if not templates:
            return None
            
        if current_time is None:
            current_time = datetime.now(timezone.utc)
            
        # Filter active templates and evaluate time windows
        candidates = []
        for template in templates:
            if not template.is_active:
                continue
                
            # Check time window
            in_window = True
            window_warning = None
            
            if template.window_start and current_time < template.window_start:
                in_window = False
                window_warning = f"Task starts at {template.window_start.strftime('%Y-%m-%d %H:%M UTC')}"
                
            elif template.window_end and current_time > template.window_end:
                in_window = False
                window_warning = f"Task ended at {template.window_end.strftime('%Y-%m-%d %H:%M UTC')}"
                
            candidates.append(TaskCandidate(
                template=template,
                priority=template.priority,
                in_window=in_window,
                window_warning=window_warning
            ))
        
        if not candidates:
            return None
            
        # Sort by: priority (lower first), in_window (True first), created_at (older first)
        candidates.sort(key=lambda c: (
            c.priority,
            not c.in_window,  # False (in window) sorts before True (out of window)
            c.template.created_at
        ))
        
        selected = candidates[0]
        
        if selected.window_warning:
            logger.warning(f"Selected task outside time window: {selected.template.name} - {selected.window_warning}")
        
        logger.info(f"Selected task from pool: {selected.template.name} (priority: {selected.priority})")
        
        return selected.template
        
    def select_reassignment_candidate(
        self, 
        available_operators: List[User],
        original_assignment: Assignment
    ) -> Optional[User]:
        """
        Select operator for reassignment from available Data Labellers.
        Uses least recent reassignment logic.
        
        Args:
            available_operators: Operators currently on Data Labelling
            original_assignment: The assignment being reassigned
            
        Returns:
            Selected User or None if no candidates
        """
        if not available_operators:
            return None
            
        # For now, use simple selection - first available
        # TODO: Implement more sophisticated logic based on recent reassignments
        selected = available_operators[0]
        
        logger.info(f"Selected reassignment candidate: {selected.display_name}")
        return selected
        
    def check_minimum_staffing(
        self,
        current_active: List[Assignment],
        proposed_break_user_id: str,
        min_required: int
    ) -> bool:
        """
        Check if allowing a break would violate minimum staffing requirements.
        
        Args:
            current_active: Currently active assignments
            proposed_break_user_id: User requesting break
            min_required: Minimum operators required on duty
            
        Returns:
            True if break can be allowed, False if it would violate staffing
        """
        # Count currently active operators (excluding the one requesting break)
        active_count = sum(
            1 for assignment in current_active
            if (assignment.status in [AssignmentStatus.ACTIVE, AssignmentStatus.COVERING] 
                and assignment.user_id != proposed_break_user_id)
        )
        
        would_violate = active_count < min_required
        
        logger.info(
            f"Staffing check: {active_count} active after break, "
            f"minimum required: {min_required}, "
            f"would violate: {would_violate}"
        )
        
        return not would_violate
        
    def calculate_break_impact(
        self,
        current_assignments: List[Assignment],
        break_user_id: str
    ) -> Dict[str, Any]:
        """
        Calculate the impact of a user taking a break.
        
        Args:
            current_assignments: All current assignments
            break_user_id: User requesting break
            
        Returns:
            Dictionary with impact analysis
        """
        user_assignment = None
        total_active = 0
        data_labellers = []
        
        for assignment in current_assignments:
            if assignment.user_id == break_user_id:
                user_assignment = assignment
            
            if assignment.status == AssignmentStatus.ACTIVE:
                total_active += 1
                if assignment.task_name == "Data Labelling":
                    data_labellers.append(assignment.user_id)
        
        needs_coverage = (
            user_assignment and 
            user_assignment.task_name != "Data Labelling" and
            user_assignment.status == AssignmentStatus.ACTIVE
        )
        
        return {
            "user_task": user_assignment.task_name if user_assignment else None,
            "needs_coverage": needs_coverage,
            "available_for_coverage": len(data_labellers),
            "total_active_before": total_active,
            "total_active_after": total_active - 1 if user_assignment else total_active
        }
        
    def select_coverage_operator(
        self,
        available_data_labellers: List[User],
        task_to_cover: Assignment
    ) -> Optional[User]:
        """
        Select a Data Labeller to cover a task while someone is on break.
        
        Args:
            available_data_labellers: Users currently on Data Labelling
            task_to_cover: The assignment that needs coverage
            
        Returns:
            Selected User or None
        """
        if not available_data_labellers:
            return None
            
        # For now, select first available
        # TODO: Consider factors like recent coverage assignments, workload, etc.
        selected = available_data_labellers[0]
        
        logger.info(
            f"Selected coverage operator: {selected.display_name} "
            f"to cover {task_to_cover.task_name}"
        )
        
        return selected
        
    def validate_task_params(
        self, 
        template: TaskTemplate, 
        provided_params: Dict[str, Any]
    ) -> tuple[bool, Optional[str]]:
        """
        Validate task parameters against template schema.
        
        Args:
            template: Task template with params schema
            provided_params: Parameters provided by user
            
        Returns:
            (is_valid, error_message)
        """
        if not template.params_schema:
            # No schema means any params are allowed
            return True, None
            
        try:
            # TODO: Implement JSON Schema validation
            # For now, just basic checks
            
            if not isinstance(provided_params, dict):
                return False, "Parameters must be a JSON object"
                
            return True, None
            
        except Exception as e:
            logger.error(f"Parameter validation error: {e}")
            return False, f"Parameter validation failed: {str(e)}"
            
    def get_shift_hours_remaining(
        self,
        shift_start: datetime,
        current_time: Optional[datetime] = None
    ) -> tuple[int, int]:
        """
        Calculate remaining hours and minutes in shift.
        
        Args:
            shift_start: When the shift started
            current_time: Current time (defaults to now)
            
        Returns:
            (hours_remaining, minutes_remaining)
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)
            
        # 9-hour shifts
        shift_end = shift_start + timedelta(hours=9)
        
        if current_time >= shift_end:
            return 0, 0
            
        remaining = shift_end - current_time
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)
        
        return hours, minutes
