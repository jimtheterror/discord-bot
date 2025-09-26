"""
Modal classes for task assignment system interactions.
"""
import logging
from typing import Optional, Dict, Any
import json
from datetime import datetime, timezone

import discord

from .database import get_db_session
from .models import Assignment, ApprovalRequest, ApprovalType, ApprovalStatus, get_settings, log_action
from .assignment_operations import AssignmentOperations

logger = logging.getLogger(__name__)


class EditTaskModal(discord.ui.Modal):
    """Modal for editing task parameters"""
    
    def __init__(self, assignment_id: int, current_params: Dict[str, Any]):
        super().__init__(title="📝 Edit Task Parameters", timeout=300)
        self.assignment_id = assignment_id
        self.current_params = current_params or {}
        
        # Add text inputs based on current parameters
        self._setup_inputs()
    
    def _setup_inputs(self):
        """Set up input fields based on current parameters"""
        # Always include a reason field
        self.reason = discord.ui.TextInput(
            label="Reason for Edit",
            placeholder="Why do you need to edit this task?",
            required=True,
            max_length=500,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.reason)
        
        # Add parameter fields (generic approach)
        self.param1_key = discord.ui.TextInput(
            label="Parameter Name (optional)",
            placeholder="e.g. 'priority', 'location', 'notes'",
            required=False,
            max_length=100
        )
        self.add_item(self.param1_key)
        
        self.param1_value = discord.ui.TextInput(
            label="Parameter Value (optional)",
            placeholder="New value for the parameter",
            required=False,
            max_length=200
        )
        self.add_item(self.param1_value)
        
        self.param2_key = discord.ui.TextInput(
            label="2nd Parameter Name (optional)",
            placeholder="Another parameter to change",
            required=False,
            max_length=100
        )
        self.add_item(self.param2_key)
        
        self.param2_value = discord.ui.TextInput(
            label="2nd Parameter Value (optional)",
            placeholder="Value for second parameter",
            required=False,
            max_length=200
        )
        self.add_item(self.param2_value)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        try:
            # Check if user can edit this assignment
            operations = AssignmentOperations(interaction.client)
            
            if not await operations.can_user_interact(self.assignment_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "❌ You can only edit your own tasks.",
                    ephemeral=True
                )
                return
            
            # Check cooldown
            if not await self._check_cooldown(str(interaction.user.id)):
                await interaction.response.send_message(
                    "⏱️ You must wait before making another edit request. Please try again later.",
                    ephemeral=True
                )
                return
            
            # Build proposed changes
            proposed_changes = {}
            
            if self.param1_key.value and self.param1_value.value:
                proposed_changes[self.param1_key.value.strip()] = self.param1_value.value.strip()
            
            if self.param2_key.value and self.param2_value.value:
                proposed_changes[self.param2_key.value.strip()] = self.param2_value.value.strip()
            
            if not proposed_changes:
                await interaction.response.send_message(
                    "❌ Please specify at least one parameter to change.",
                    ephemeral=True
                )
                return
            
            # Submit edit request
            success, message = await operations.request_edit(
                self.assignment_id,
                str(interaction.user.id),
                proposed_changes,
                self.reason.value
            )
            
            if success:
                # Send approval request to admins
                await self._send_admin_approval_request(interaction, proposed_changes)
                
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
            logger.error(f"Error in edit task modal: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while processing your edit request.",
                ephemeral=True
            )
    
    async def _check_cooldown(self, user_id: str) -> bool:
        """Check if user is still in cooldown period for edits"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                cooldown_seconds = settings.cooldown_edit_sec
                
                if cooldown_seconds <= 0:
                    return True  # No cooldown
                
                # Find most recent edit request
                recent_request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.user_id == user_id,
                    ApprovalRequest.type == ApprovalType.EDIT
                ).order_by(ApprovalRequest.requested_at.desc()).first()
                
                if not recent_request:
                    return True  # No previous requests
                
                # Check if cooldown period has passed
                time_since_last = datetime.now(timezone.utc) - recent_request.requested_at
                return time_since_last.total_seconds() >= cooldown_seconds
                
        except Exception as e:
            logger.error(f"Failed to check edit cooldown: {e}")
            return True  # Allow on error
    
    async def _send_admin_approval_request(self, interaction: discord.Interaction, proposed_changes: Dict[str, Any]):
        """Send approval request card to admin channel"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                if not settings.admin_channel_id:
                    logger.warning("No admin channel configured for approval requests")
                    return
                
                # Get assignment details
                assignment = db.query(Assignment).filter(Assignment.id == self.assignment_id).first()
                if not assignment:
                    return
                
                # Find admin channel
                admin_channel = interaction.guild.get_channel(int(settings.admin_channel_id))
                if not admin_channel:
                    try:
                        admin_channel = await interaction.guild.fetch_channel(int(settings.admin_channel_id))
                    except (discord.NotFound, discord.Forbidden):
                        logger.error(f"Admin channel {settings.admin_channel_id} not found")
                        return
                
                # Create approval embed
                embed = discord.Embed(
                    title="📝 Edit Task Approval Request",
                    color=0x3498db
                )
                
                embed.add_field(
                    name="Operator",
                    value=interaction.user.display_name,
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
                    value=self.reason.value[:200] + ("..." if len(self.reason.value) > 200 else ""),
                    inline=False
                )
                
                # Show proposed changes
                changes_text = ""
                for key, value in proposed_changes.items():
                    current_value = self.current_params.get(key, "None")
                    changes_text += f"**{key}:** `{current_value}` → `{value}`\n"
                
                embed.add_field(
                    name="Proposed Changes",
                    value=changes_text[:1000],
                    inline=False
                )
                
                embed.set_footer(
                    text=f"Assignment ID: {self.assignment_id} | User ID: {interaction.user.id}"
                )
                embed.timestamp = datetime.utcnow()
                
                # Create approval view
                view = EditApprovalView(self.assignment_id, str(interaction.user.id))
                
                await admin_channel.send(
                    content="🔔 **Edit Request Needs Approval**",
                    embed=embed,
                    view=view
                )
                
        except Exception as e:
            logger.error(f"Failed to send admin approval request: {e}")


class EndEarlyModal(discord.ui.Modal):
    """Modal for ending task early"""
    
    def __init__(self, assignment_id: int):
        super().__init__(title="⏹️ End Task Early", timeout=300)
        self.assignment_id = assignment_id
        
        # Reason field
        self.reason = discord.ui.TextInput(
            label="Reason for Ending Early",
            placeholder="Why do you need to end this task early?",
            required=True,
            max_length=500,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.reason)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        try:
            # Check if user can interact with this assignment
            operations = AssignmentOperations(interaction.client)
            
            if not await operations.can_user_interact(self.assignment_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "❌ You can only end your own tasks early.",
                    ephemeral=True
                )
                return
            
            # Check cooldown
            if not await self._check_cooldown(str(interaction.user.id)):
                await interaction.response.send_message(
                    "⏱️ You must wait before making another end early request. Please try again later.",
                    ephemeral=True
                )
                return
            
            # Submit end early request
            success, message = await operations.request_end_early(
                self.assignment_id,
                str(interaction.user.id),
                self.reason.value
            )
            
            if success:
                # Send approval request to admins
                await self._send_admin_approval_request(interaction)
                
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
            logger.error(f"Error in end early modal: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while processing your request.",
                ephemeral=True
            )
    
    async def _check_cooldown(self, user_id: str) -> bool:
        """Check if user is still in cooldown period for end early"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                cooldown_seconds = settings.cooldown_end_early_sec
                
                if cooldown_seconds <= 0:
                    return True
                
                # Find most recent end early request
                recent_request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.user_id == user_id,
                    ApprovalRequest.type == ApprovalType.END_EARLY
                ).order_by(ApprovalRequest.requested_at.desc()).first()
                
                if not recent_request:
                    return True
                
                # Check cooldown
                time_since_last = datetime.now(timezone.utc) - recent_request.requested_at
                return time_since_last.total_seconds() >= cooldown_seconds
                
        except Exception as e:
            logger.error(f"Failed to check end early cooldown: {e}")
            return True
    
    async def _send_admin_approval_request(self, interaction: discord.Interaction):
        """Send approval request to admin channel"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                if not settings.admin_channel_id:
                    return
                
                assignment = db.query(Assignment).filter(Assignment.id == self.assignment_id).first()
                if not assignment:
                    return
                
                admin_channel = interaction.guild.get_channel(int(settings.admin_channel_id))
                if not admin_channel:
                    return
                
                embed = discord.Embed(
                    title="⏹️ End Task Early Approval Request",
                    color=0xff6b6b
                )
                
                embed.add_field(name="Operator", value=interaction.user.display_name, inline=True)
                embed.add_field(name="Task", value=assignment.task_name, inline=True)  
                embed.add_field(name="Hour", value=str(assignment.hour_index), inline=True)
                embed.add_field(
                    name="Reason", 
                    value=self.reason.value[:200] + ("..." if len(self.reason.value) > 200 else ""),
                    inline=False
                )
                
                embed.set_footer(text=f"Assignment ID: {self.assignment_id}")
                embed.timestamp = datetime.utcnow()
                
                view = EndEarlyApprovalView(self.assignment_id, str(interaction.user.id))
                
                await admin_channel.send(
                    content="🔔 **End Early Request Needs Approval**",
                    embed=embed,
                    view=view
                )
                
        except Exception as e:
            logger.error(f"Failed to send end early approval request: {e}")


class EditApprovalView(discord.ui.View):
    """Admin approval view for edit requests"""
    
    def __init__(self, assignment_id: int, user_id: str):
        super().__init__(timeout=3600)  # 1 hour timeout
        self.assignment_id = assignment_id
        self.user_id = user_id
    
    @discord.ui.button(
        label="✅ Approve",
        style=discord.ButtonStyle.success,
        custom_id="approve_edit"
    )
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle approve button click"""
        await self._handle_approval(interaction, True, "")
    
    @discord.ui.button(
        label="❌ Deny",
        style=discord.ButtonStyle.danger,
        custom_id="deny_edit"
    )
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle deny button click"""
        # Show modal for denial reason
        modal = ApprovalReasonModal("deny_edit", self.assignment_id, self.user_id, interaction.message)
        await interaction.response.send_modal(modal)
    
    async def _handle_approval(self, interaction: discord.Interaction, approved: bool, reason: str):
        """Handle approval or denial"""
        try:
            with get_db_session() as db:
                # Find the pending approval request
                request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.assignment_id == self.assignment_id,
                    ApprovalRequest.user_id == self.user_id,
                    ApprovalRequest.type == ApprovalType.EDIT,
                    ApprovalRequest.status == ApprovalStatus.PENDING
                ).first()
                
                if not request:
                    await interaction.response.send_message(
                        "❌ Approval request not found or already processed.",
                        ephemeral=True
                    )
                    return
                
                # Update request
                request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
                request.resolved_at = datetime.now(timezone.utc)
                request.resolver_id = str(interaction.user.id)
                request.resolver_note = reason
                
                if approved:
                    # Apply the changes to the assignment
                    assignment = db.query(Assignment).filter(Assignment.id == self.assignment_id).first()
                    if assignment and request.payload.get("proposed_changes"):
                        # Update assignment parameters
                        current_params = assignment.params or {}
                        current_params.update(request.payload["proposed_changes"])
                        assignment.params = current_params
                
                db.commit()
                
                # Log the action
                log_action(
                    db,
                    action="edit_request_resolved",
                    actor_id=str(interaction.user.id),
                    target=str(self.assignment_id),
                    metadata={
                        "approved": approved,
                        "reason": reason,
                        "original_user": self.user_id
                    }
                )
                
                # Update the message to show resolution
                embed = interaction.message.embeds[0]
                embed.color = 0x00ff00 if approved else 0xff0000
                embed.title = f"{'✅ APPROVED' if approved else '❌ DENIED'} - {embed.title}"
                embed.add_field(
                    name="Resolved By",
                    value=f"{interaction.user.display_name}{f' - {reason}' if reason else ''}",
                    inline=False
                )
                
                # Disable buttons
                for item in self.children:
                    item.disabled = True
                
                await interaction.response.edit_message(embed=embed, view=self)
                
                # Notify the operator
                await self._notify_operator(interaction, approved, reason)
                
        except Exception as e:
            logger.error(f"Error handling edit approval: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while processing the approval.",
                ephemeral=True
            )
    
    async def _notify_operator(self, interaction: discord.Interaction, approved: bool, reason: str):
        """Notify the operator of the approval decision"""
        try:
            # This would send a message to the operator's thread
            # Implementation depends on having thread reference
            pass
        except Exception as e:
            logger.error(f"Failed to notify operator: {e}")


class EndEarlyApprovalView(discord.ui.View):
    """Admin approval view for end early requests"""
    
    def __init__(self, assignment_id: int, user_id: str):
        super().__init__(timeout=3600)
        self.assignment_id = assignment_id
        self.user_id = user_id
    
    @discord.ui.button(
        label="✅ Approve",
        style=discord.ButtonStyle.success
    )
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_approval(interaction, True, "")
    
    @discord.ui.button(
        label="❌ Deny", 
        style=discord.ButtonStyle.danger
    )
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ApprovalReasonModal("deny_end_early", self.assignment_id, self.user_id, interaction.message)
        await interaction.response.send_modal(modal)
    
    async def _handle_approval(self, interaction: discord.Interaction, approved: bool, reason: str):
        """Handle end early approval/denial"""
        try:
            with get_db_session() as db:
                request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.assignment_id == self.assignment_id,
                    ApprovalRequest.user_id == self.user_id,
                    ApprovalRequest.type == ApprovalType.END_EARLY,
                    ApprovalRequest.status == ApprovalStatus.PENDING
                ).first()
                
                if not request:
                    await interaction.response.send_message(
                        "❌ Request not found or already processed.",
                        ephemeral=True
                    )
                    return
                
                request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
                request.resolved_at = datetime.now(timezone.utc)
                request.resolver_id = str(interaction.user.id)
                request.resolver_note = reason
                
                if approved:
                    # End the assignment early
                    assignment = db.query(Assignment).filter(Assignment.id == self.assignment_id).first()
                    if assignment:
                        assignment.status = AssignmentStatus.ENDED_EARLY
                        assignment.ended_at = datetime.now(timezone.utc)
                
                db.commit()
                
                # Update the message
                embed = interaction.message.embeds[0]
                embed.color = 0x00ff00 if approved else 0xff0000
                embed.title = f"{'✅ APPROVED' if approved else '❌ DENIED'} - {embed.title}"
                
                for item in self.children:
                    item.disabled = True
                
                await interaction.response.edit_message(embed=embed, view=self)
                
        except Exception as e:
            logger.error(f"Error handling end early approval: {e}")


class ApprovalReasonModal(discord.ui.Modal):
    """Modal for entering approval/denial reasons"""
    
    def __init__(self, action_type: str, assignment_id: int, user_id: str, original_message: discord.Message):
        super().__init__(title=f"Reason for {action_type.replace('_', ' ').title()}")
        self.action_type = action_type
        self.assignment_id = assignment_id
        self.user_id = user_id
        self.original_message = original_message
        
        self.reason = discord.ui.TextInput(
            label="Reason (optional)",
            placeholder="Why are you denying this request?",
            required=False,
            max_length=500,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.reason)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle reason submission"""
        try:
            # Handle the denial directly
            await self._handle_denial(interaction, self.reason.value)
        except Exception as e:
            logger.error(f"Error in approval reason modal: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while processing the denial.",
                ephemeral=True
            )
    
    async def _handle_denial(self, interaction: discord.Interaction, reason: str):
        """Handle the denial with reason"""
        try:
            with get_db_session() as db:
                # Determine request type
                request_type = ApprovalType.EDIT if "edit" in self.action_type else ApprovalType.END_EARLY
                
                # Find and update the approval request
                request = db.query(ApprovalRequest).filter(
                    ApprovalRequest.assignment_id == self.assignment_id,
                    ApprovalRequest.user_id == self.user_id,
                    ApprovalRequest.type == request_type,
                    ApprovalRequest.status == ApprovalStatus.PENDING
                ).first()
                
                if not request:
                    await interaction.response.send_message(
                        "❌ Approval request not found or already processed.",
                        ephemeral=True
                    )
                    return
                
                # Update request
                request.status = ApprovalStatus.DENIED
                request.resolved_at = datetime.now(timezone.utc)
                request.resolver_id = str(interaction.user.id)
                request.resolver_note = reason
                
                db.commit()
                
                # Log the action
                log_action(
                    db,
                    action=f"{request_type.value}_request_denied",
                    actor_id=str(interaction.user.id),
                    target=str(self.assignment_id),
                    metadata={
                        "reason": reason,
                        "original_user": self.user_id
                    }
                )
                
                # Update the original message
                embed = self.original_message.embeds[0]
                embed.color = 0xff0000
                embed.title = f"❌ DENIED - {embed.title}"
                embed.add_field(
                    name="Denied By",
                    value=f"{interaction.user.display_name}{f' - {reason}' if reason else ''}",
                    inline=False
                )
                
                # Create disabled view
                if "edit" in self.action_type:
                    view = EditApprovalView(self.assignment_id, self.user_id)
                else:
                    view = EndEarlyApprovalView(self.assignment_id, self.user_id)
                
                for item in view.children:
                    item.disabled = True
                
                await self.original_message.edit(embed=embed, view=view)
                
                await interaction.response.send_message(
                    f"✅ Request denied{f': {reason}' if reason else ''}",
                    ephemeral=True
                )
                
                # TODO: Notify the operator about the denial
                
        except Exception as e:
            logger.error(f"Error handling denial: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while processing the denial.",
                ephemeral=True
            )
