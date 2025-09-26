"""
Enhanced audit logging system for comprehensive compliance tracking.
"""
import logging
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict

import discord

from .database import get_db_session
from .models import AuditLog, get_settings, log_action

logger = logging.getLogger(__name__)


@dataclass
class InteractionEvent:
    """Structured interaction event for logging"""
    event_type: str
    user_id: str
    user_name: str
    guild_id: str
    channel_id: str
    interaction_type: str
    command_name: Optional[str]
    custom_id: Optional[str]
    metadata: Dict[str, Any]
    timestamp: datetime
    session_id: Optional[str] = None


class EnhancedAuditLogger:
    """Enhanced audit logging with Discord channel mirroring and structured events"""
    
    def __init__(self, bot):
        self.bot = bot
        self.audit_channel_id = None
        self._load_audit_settings()
    
    def _load_audit_settings(self):
        """Load audit channel settings"""
        try:
            with get_db_session() as db:
                settings = get_settings(db)
                # For now, use admin channel as audit channel
                # Later can be extended to separate audit channel
                self.audit_channel_id = settings.admin_channel_id
        except Exception as e:
            logger.error(f"Failed to load audit settings: {e}")
    
    async def log_interaction_event(
        self,
        event_type: str,
        interaction: discord.Interaction,
        metadata: Dict[str, Any] = None
    ):
        """Log a structured interaction event"""
        try:
            event = InteractionEvent(
                event_type=event_type,
                user_id=str(interaction.user.id),
                user_name=interaction.user.display_name,
                guild_id=str(interaction.guild_id) if interaction.guild_id else "",
                channel_id=str(interaction.channel_id) if interaction.channel_id else "",
                interaction_type=interaction.type.name if hasattr(interaction, 'type') else "unknown",
                command_name=getattr(interaction, 'command', {}).get('name') if hasattr(interaction, 'command') and interaction.command else None,
                custom_id=getattr(interaction, 'data', {}).get('custom_id') if hasattr(interaction, 'data') else None,
                metadata=metadata or {},
                timestamp=datetime.now(timezone.utc)
            )
            
            await self._process_audit_event(event)
            
        except Exception as e:
            logger.error(f"Failed to log interaction event: {e}")
    
    async def log_scheduler_event(
        self,
        event_type: str,
        metadata: Dict[str, Any] = None,
        user_id: Optional[str] = None
    ):
        """Log a scheduler or system event"""
        try:
            event = InteractionEvent(
                event_type=event_type,
                user_id=user_id or "system",
                user_name="System Scheduler",
                guild_id="",
                channel_id="",
                interaction_type="scheduler",
                command_name=None,
                custom_id=None,
                metadata=metadata or {},
                timestamp=datetime.now(timezone.utc)
            )
            
            await self._process_audit_event(event)
            
        except Exception as e:
            logger.error(f"Failed to log scheduler event: {e}")
    
    async def _process_audit_event(self, event: InteractionEvent):
        """Process and store audit event"""
        try:
            # Store in database
            with get_db_session() as db:
                log_action(
                    db,
                    action=event.event_type,
                    actor_id=event.user_id,
                    target=event.metadata.get('target', ''),
                    metadata={
                        **asdict(event),
                        'structured_event': True
                    }
                )
            
            # Mirror to Discord if configured and important
            if self._should_mirror_to_discord(event):
                await self._mirror_to_discord(event)
                
        except Exception as e:
            logger.error(f"Failed to process audit event: {e}")
    
    def _should_mirror_to_discord(self, event: InteractionEvent) -> bool:
        """Determine if event should be mirrored to Discord"""
        # Mirror important events to Discord
        important_events = {
            'assignment_created',
            'assignment_escalated', 
            'break_request_approved',
            'break_request_denied',
            'edit_request_approved',
            'edit_request_denied',
            'end_early_approved',
            'end_early_denied',
            'force_assignment',
            'settings_updated',
            'task_template_created',
            'task_template_deleted',
            'dashboard_snapshot_created',
            'system_error',
            'security_violation'
        }
        
        return event.event_type in important_events
    
    async def _mirror_to_discord(self, event: InteractionEvent):
        """Mirror important events to Discord audit channel"""
        try:
            if not self.audit_channel_id:
                return
            
            # Find the audit channel
            audit_channel = None
            for guild in self.bot.guilds:
                channel = guild.get_channel(int(self.audit_channel_id))
                if channel:
                    audit_channel = channel
                    break
            
            if not audit_channel:
                return
            
            embed = self._create_audit_embed(event)
            await audit_channel.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Failed to mirror to Discord: {e}")
    
    def _create_audit_embed(self, event: InteractionEvent) -> discord.Embed:
        """Create audit embed for Discord"""
        # Color based on event type
        color = 0x3498db  # Default blue
        if 'error' in event.event_type or 'violation' in event.event_type:
            color = 0xff0000  # Red for errors
        elif 'approved' in event.event_type or 'created' in event.event_type:
            color = 0x00ff00  # Green for success
        elif 'denied' in event.event_type or 'escalated' in event.event_type:
            color = 0xffa500  # Orange for warnings
        
        embed = discord.Embed(
            title=f"🔍 Audit: {event.event_type.replace('_', ' ').title()}",
            color=color,
            timestamp=event.timestamp
        )
        
        embed.add_field(
            name="User",
            value=f"{event.user_name} (`{event.user_id}`)",
            inline=True
        )
        
        embed.add_field(
            name="Event Type",
            value=event.interaction_type,
            inline=True
        )
        
        if event.command_name:
            embed.add_field(
                name="Command",
                value=event.command_name,
                inline=True
            )
        
        # Add relevant metadata
        if event.metadata:
            metadata_text = ""
            for key, value in event.metadata.items():
                if key not in ['structured_event', 'timestamp']:
                    if isinstance(value, dict):
                        metadata_text += f"**{key}:** {json.dumps(value, indent=2)[:100]}...\n"
                    else:
                        metadata_text += f"**{key}:** {str(value)[:50]}\n"
            
            if metadata_text:
                embed.add_field(
                    name="Details",
                    value=metadata_text[:1000],
                    inline=False
                )
        
        embed.set_footer(text=f"Event ID: {hash(str(event.timestamp) + event.user_id)}")
        
        return embed
    
    async def export_audit_logs(
        self,
        start_date: datetime,
        end_date: datetime,
        event_types: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Export audit logs for a date range"""
        try:
            with get_db_session() as db:
                query = db.query(AuditLog).filter(
                    AuditLog.timestamp >= start_date,
                    AuditLog.timestamp <= end_date
                )
                
                if event_types:
                    query = query.filter(AuditLog.action.in_(event_types))
                
                logs = query.order_by(AuditLog.timestamp.desc()).all()
                
                export_data = []
                for log in logs:
                    export_data.append({
                        'id': log.id,
                        'timestamp': log.timestamp.isoformat(),
                        'action': log.action,
                        'actor_id': log.actor_id,
                        'target': log.target,
                        'metadata': log.metadata
                    })
                
                return export_data
                
        except Exception as e:
            logger.error(f"Failed to export audit logs: {e}")
            return []
    
    async def get_user_activity_summary(self, user_id: str, days: int = 7) -> Dict[str, Any]:
        """Get activity summary for a user"""
        try:
            with get_db_session() as db:
                start_date = datetime.now(timezone.utc) - timedelta(days=days)
                
                logs = db.query(AuditLog).filter(
                    AuditLog.actor_id == user_id,
                    AuditLog.timestamp >= start_date
                ).all()
                
                # Aggregate activity
                activity_counts = {}
                recent_actions = []
                
                for log in logs:
                    activity_counts[log.action] = activity_counts.get(log.action, 0) + 1
                    recent_actions.append({
                        'action': log.action,
                        'timestamp': log.timestamp.isoformat(),
                        'target': log.target
                    })
                
                return {
                    'user_id': user_id,
                    'period_days': days,
                    'total_actions': len(logs),
                    'activity_breakdown': activity_counts,
                    'recent_actions': sorted(recent_actions, key=lambda x: x['timestamp'], reverse=True)[:20]
                }
                
        except Exception as e:
            logger.error(f"Failed to get user activity summary: {e}")
            return {}


# Global audit logger instance
enhanced_audit_logger = None


def init_enhanced_audit_logger(bot):
    """Initialize the enhanced audit logger"""
    global enhanced_audit_logger
    enhanced_audit_logger = EnhancedAuditLogger(bot)
    return enhanced_audit_logger


async def log_interaction(
    event_type: str,
    interaction: discord.Interaction,
    metadata: Dict[str, Any] = None
):
    """Convenience function for logging interactions"""
    if enhanced_audit_logger:
        await enhanced_audit_logger.log_interaction_event(event_type, interaction, metadata)


async def log_scheduler_event(
    event_type: str,
    metadata: Dict[str, Any] = None,
    user_id: Optional[str] = None
):
    """Convenience function for logging scheduler events"""
    if enhanced_audit_logger:
        await enhanced_audit_logger.log_scheduler_event(event_type, metadata, user_id)


# Decorator for automatic interaction logging
def audit_interaction(event_type: str, include_metadata: bool = True):
    """Decorator to automatically audit command interactions"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            # Extract interaction from args
            interaction = None
            for arg in args:
                if isinstance(arg, discord.Interaction):
                    interaction = arg
                    break
            
            if interaction and enhanced_audit_logger:
                metadata = {}
                if include_metadata:
                    # Extract command parameters
                    if hasattr(interaction, 'namespace') and interaction.namespace:
                        for key, value in interaction.namespace.__dict__.items():
                            if not key.startswith('_'):
                                metadata[key] = str(value)
                
                await enhanced_audit_logger.log_interaction_event(event_type, interaction, metadata)
            
            # Call original function
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator
