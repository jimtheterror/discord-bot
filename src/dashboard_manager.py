import json
import os
import re
import logging
from datetime import datetime
from typing import Dict, Optional, Tuple
import discord

logger = logging.getLogger(__name__)

class EquipmentDashboard:
    """Manages equipment state dashboard with persistent storage"""
    
    def __init__(self, state_file: str = "dashboard_state.json"):
        self.state_file = state_file
        self.dashboard_message_id = None
        self.dashboard_channel_id = None
        self.state = self.load_state()
        
    def load_state(self) -> Dict:
        """Load dashboard state from file"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.dashboard_message_id = data.get('dashboard_message_id')
                    self.dashboard_channel_id = data.get('dashboard_channel_id')
                    return data.get('equipment_state', self.get_default_state())
            return self.get_default_state()
        except Exception as e:
            logger.error(f"Failed to load dashboard state: {e}")
            return self.get_default_state()
    
    def save_state(self):
        """Save dashboard state to file"""
        try:
            data = {
                'equipment_state': self.state,
                'dashboard_message_id': self.dashboard_message_id,
                'dashboard_channel_id': self.dashboard_channel_id,
                'last_updated': datetime.now().isoformat()
            }
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save dashboard state: {e}")
    
    def get_default_state(self) -> Dict:
        """Initialize default equipment state"""
        state = {
            'robots': {},
            'joysticks': {},
            'vr': {}
        }
        
        # Initialize Robots (Prod 1-6)
        for i in range(1, 7):
            state['robots'][f'Prod {i}'] = {
                'status': 'Unknown',
                'since': datetime.now().isoformat(),
                'last_update': 'Initial state'
            }
        
        # Initialize Joysticks (Gello 51-60)
        for i in range(51, 61):
            state['joysticks'][f'Gello {i}'] = {
                'status': 'Unknown',
                'since': datetime.now().isoformat(),
                'last_update': 'Initial state'
            }
        
        # Initialize VR Headsets (Headset 1-5)
        for i in range(1, 6):
            state['vr'][f'Headset {i}'] = {
                'status': 'Unknown',
                'since': datetime.now().isoformat(),
                'last_update': 'Initial state'
            }
        
        return state
    
    def parse_equipment_update(self, message: str) -> Optional[Tuple[str, str, str]]:
        """
        Parse equipment update message
        Returns: (equipment_type, equipment_name, status) or None
        """
        message = message.strip()
        
        # Patterns for different equipment types
        patterns = [
            (r'^(Prod \d+)\s+(.+)$', 'robots'),
            (r'^(Gello \d+)\s+(.+)$', 'joysticks'), 
            (r'^(Headset \d+)\s+(.+)$', 'vr')
        ]
        
        for pattern, equipment_type in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                equipment_name = match.group(1)
                status = match.group(2)
                
                # Normalize equipment name formatting
                if equipment_type == 'robots':
                    equipment_name = equipment_name.replace('prod', 'Prod')
                elif equipment_type == 'joysticks':
                    equipment_name = equipment_name.replace('gello', 'Gello')
                elif equipment_type == 'vr':
                    equipment_name = equipment_name.replace('headset', 'Headset')
                
                return equipment_type, equipment_name, status
        
        return None
    
    def update_equipment(self, equipment_type: str, equipment_name: str, status: str, timestamp: datetime) -> bool:
        """Update equipment status"""
        try:
            if equipment_type in self.state:
                if equipment_name in self.state[equipment_type]:
                    self.state[equipment_type][equipment_name] = {
                        'status': status,
                        'since': timestamp.isoformat(),
                        'last_update': status
                    }
                    self.save_state()
                    logger.info(f"Updated {equipment_name}: {status}")
                    return True
                else:
                    logger.warning(f"Equipment {equipment_name} not found in {equipment_type}")
            else:
                logger.warning(f"Equipment type {equipment_type} not found")
            return False
        except Exception as e:
            logger.error(f"Failed to update equipment {equipment_name}: {e}")
            return False
    
    def generate_dashboard_content(self) -> str:
        """Generate formatted dashboard content"""
        content = "🤖 **EQUIPMENT STATUS DASHBOARD** 🤖\n"
        content += f"*Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
        
        # Robots Section
        content += "**🤖 ROBOTS**\n"
        content += "```\n"
        for name, info in sorted(self.state['robots'].items()):
            since_time = datetime.fromisoformat(info['since']).strftime('%m/%d %H:%M')
            status_line = f"{name:<8} | {info['status']:<20} | {since_time}"
            content += status_line + "\n"
        content += "```\n\n"
        
        # Joysticks Section  
        content += "**🕹️ JOYSTICKS**\n"
        content += "```\n"
        for name, info in sorted(self.state['joysticks'].items(), key=lambda x: int(x[0].split()[1])):
            since_time = datetime.fromisoformat(info['since']).strftime('%m/%d %H:%M')
            status_line = f"{name:<10} | {info['status']:<20} | {since_time}"
            content += status_line + "\n"
        content += "```\n\n"
        
        # VR Section
        content += "**🥽 VR HEADSETS**\n"
        content += "```\n" 
        for name, info in sorted(self.state['vr'].items(), key=lambda x: int(x[0].split()[1])):
            since_time = datetime.fromisoformat(info['since']).strftime('%m/%d %H:%M')
            status_line = f"{name:<12} | {info['status']:<20} | {since_time}"
            content += status_line + "\n"
        content += "```\n\n"
        
        content += "*💡 To update equipment status, post in #equipment-updates:*\n"
        content += "*Format: `Gello 55 operational` or `Prod 1 needs repair` etc.*"
        
        return content
    
    async def create_or_update_dashboard(self, channel: discord.TextChannel) -> bool:
        """Create or update the dashboard message"""
        try:
            content = self.generate_dashboard_content()
            
            # Try to find and update existing message
            if self.dashboard_message_id:
                try:
                    message = await channel.fetch_message(self.dashboard_message_id)
                    await message.edit(content=content)
                    logger.info("Updated existing dashboard message")
                    return True
                except discord.NotFound:
                    # Message was deleted, create new one
                    pass
                except Exception as e:
                    logger.warning(f"Failed to update existing message: {e}")
            
            # Create new dashboard message
            message = await channel.send(content)
            self.dashboard_message_id = message.id
            self.dashboard_channel_id = channel.id
            await message.pin()
            self.save_state()
            logger.info("Created new dashboard message")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create/update dashboard: {e}")
            return False
    
    async def handle_equipment_update(self, message: discord.Message) -> bool:
        """Process equipment update from message"""
        try:
            parsed = self.parse_equipment_update(message.content)
            if not parsed:
                return False
            
            equipment_type, equipment_name, status = parsed
            success = self.update_equipment(equipment_type, equipment_name, status, message.created_at)
            
            if success:
                # React to show the update was processed
                await message.add_reaction('✅')
                
                # Update dashboard if we have the channel
                if self.dashboard_channel_id:
                    try:
                        channel = message.guild.get_channel(self.dashboard_channel_id)
                        if channel:
                            await self.create_or_update_dashboard(channel)
                    except Exception as e:
                        logger.error(f"Failed to update dashboard after equipment update: {e}")
            
            return success
            
        except Exception as e:
            logger.error(f"Failed to handle equipment update: {e}")
            return False
