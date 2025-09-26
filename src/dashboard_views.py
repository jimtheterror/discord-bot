"""
Dashboard UI views and components.
"""
import logging
import discord

logger = logging.getLogger(__name__)


class DashboardView(discord.ui.View):
    """Interactive buttons for dashboard management"""
    
    def __init__(self):
        super().__init__(timeout=None)  # Persistent view
    
    @discord.ui.button(
        label="🔄 Refresh Now",
        style=discord.ButtonStyle.primary,
        custom_id="dashboard_refresh"
    )
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle manual refresh button"""
        try:
            await interaction.response.defer()
            
            # Trigger dashboard update
            from dashboard_core import DashboardManager
            dashboard_manager = DashboardManager(interaction.client)
            await dashboard_manager.update_dashboard()
            
            await interaction.followup.send("🔄 Dashboard refreshed!", ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in dashboard refresh: {e}")
            try:
                await interaction.followup.send("❌ Refresh failed", ephemeral=True)
            except:
                pass
    
    @discord.ui.button(
        label="📸 Snapshot",
        style=discord.ButtonStyle.secondary,
        custom_id="dashboard_snapshot"
    )
    async def snapshot_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle snapshot button"""
        try:
            # Check admin permissions
            user_roles = [role.name for role in interaction.user.roles]
            if not any(role in user_roles for role in ["Admin", "Manager"]) and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "❌ Only admins can create snapshots.",
                    ephemeral=True
                )
                return
            
            await interaction.response.defer()
            
            from dashboard_core import DashboardManager
            dashboard_manager = DashboardManager(interaction.client)
            success = await dashboard_manager.create_snapshot(interaction.channel)
            
            if success:
                await interaction.followup.send("📸 Snapshot created!", ephemeral=True)
            else:
                await interaction.followup.send("❌ Failed to create snapshot", ephemeral=True)
                
        except Exception as e:
            logger.error(f"Error in snapshot button: {e}")
            try:
                await interaction.followup.send("❌ Snapshot failed", ephemeral=True)
            except:
                pass
