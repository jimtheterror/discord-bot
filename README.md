# 🎯 Live Task Assignment System

A sophisticated Discord bot system for managing hourly task assignments with real-time monitoring, approval workflows, and comprehensive operator management.

## 📋 Table of Contents

- [🚀 System Overview](#-system-overview)
- [⚙️ Initial Setup](#️-initial-setup)
- [👥 For Operators](#-for-operators)
- [🛠️ For Admins](#️-for-admins)
- [📚 Commands Reference](#-commands-reference)
- [🔧 Troubleshooting](#-troubleshooting)

---

## 🚀 System Overview

### What This System Does

This Discord bot automatically manages hourly task assignments for operators working in shifts. It provides:

- **⏰ Automated Hourly Assignments**: Tasks are posted every hour to operator threads
- **🎯 Role-Based Task Distribution**: Comms Lead selection (rotating) + Data Labelling for others
- **📝 Interactive Task Management**: Edit, end early, and manage tasks through Discord UI
- **👨‍💼 Admin Approval Workflows**: Structured approval process for operator requests
- **🔍 Comprehensive Audit Trail**: All actions are logged for compliance and monitoring

### Key Concepts

- **🕐 Shifts**: 9-hour work periods with hourly task assignments (Hours 1-9)
- **👤 Operators**: Team members with the `@Operator` role who receive assignments
- **🗣️ Comms Lead**: Rotates among operators using "least recently used" selection
- **📊 Data Labelling**: Default task for non-Comms Lead operators
- **🎯 Task Templates**: Admin-configured tasks with priorities and time windows
- **🔒 Private Threads**: Each operator gets a private thread for their assignments

---

## ⚙️ Initial Setup

### Prerequisites

1. **Database**: PostgreSQL database (or SQLite for development)
2. **Environment Variables**: Configure in `.env` file
3. **Discord Permissions**: Bot needs message, thread, and role management permissions

### Required Environment Variables

```env
# Discord Bot Configuration
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_guild_id_here

# Database Configuration
DATABASE_URL=postgresql://username:password@localhost/database_name
# OR for SQLite: DATABASE_URL=sqlite:///./assignments.db

# Optional Settings
LOG_LEVEL=INFO
```

### First-Time Admin Setup

1. **Run Initial Configuration**:
   ```
   /settings
   ```
   Configure:
   - **Assignments Channel**: Where operator threads will be created
   - **Admin Channel**: Where approval requests are sent
   - **Operator Role**: Role for team members who receive assignments
   - **Timezone**: Your organization's base timezone
   - **Minimum On Duty**: Minimum operators required to be active

2. **Create Task Templates** (Optional):
   ```
   /task add name:"High Priority Monitoring" priority:1
   ```

3. **Test the System**:
   - Assign the Operator role to test users
   - Verify threads are created in the assignments channel
   - Check that hourly assignments are posted

---

## 👥 For Operators

### 🎯 Getting Started as an Operator

1. **Get the Operator Role**: Ask an admin to assign you the `@Operator` role
2. **Find Your Thread**: Look for your private thread in the assignments channel
3. **Wait for Assignments**: Tasks are automatically posted every hour at the top of the hour

### 📱 Your Assignment Widget

When you receive an assignment, you'll see a rich embed with:

```
📋 Hour 3 Assignment
🎯 Task: Comms Lead
⏰ Ends At: 15:00 UTC
🆔 Assignment ID: #12345

[🟢 Start Task] [✏️ Edit Task] [⏹️ End Early]
```

### 🔄 Task Workflow

#### 1. **Starting a Task** ✅
- Click **🟢 Start Task** to begin your assignment
- Task automatically ends at the next hour boundary
- Status changes from "Pending" to "Active"

#### 2. **Editing Task Parameters** ✏️
- Click **✏️ Edit Task** to modify task details
- Fill out the edit form with:
  - **Reason**: Why you need to edit (required)
  - **Parameter Changes**: What you want to modify
- Requires admin approval
- You'll be notified when approved/denied

#### 3. **Ending Tasks Early** ⏹️
- Click **⏹️ End Early** if you need to finish before the hour ends
- Provide a reason for ending early
- Requires admin approval
- Use sparingly and only when necessary

### 📋 Assignment Types

#### 🗣️ **Comms Lead**
- **Selection**: Rotates automatically using "least recently used"
- **Duration**: Full hour assignment
- **Responsibilities**: Handle communications, coordinate team activities
- **Priority**: Highest priority assignment

#### 📊 **Data Labelling**
- **Default Task**: Assigned to operators not selected for Comms Lead
- **Duration**: Full hour unless reassigned
- **Flexibility**: Can be overridden by admin-defined task templates

#### 🎯 **Custom Tasks**
- **Admin-Defined**: Special tasks created by admins with specific priorities
- **Time Windows**: May only be available during certain hours
- **Parameters**: Can include custom instructions and configuration

### ⚠️ Important Guidelines

#### ✅ **Do:**
- Start tasks promptly when assigned
- Use edit requests sparingly and provide clear reasons
- End tasks early only when absolutely necessary
- Stay active during your assigned hours

#### ❌ **Don't:**
- Ignore assignment notifications
- Request edits for trivial changes
- End tasks early without valid reasons
- Miss acknowledgments (you have 5 minutes to start)

### 🚨 Escalation Process

If you don't acknowledge an assignment within **5 minutes**:
1. **Reminder**: You'll receive a ping reminder
2. **Admin Alert**: Admins are notified of the delay
3. **Auto-Escalation**: After 10 minutes, non-Data Labelling tasks are reassigned

### 💡 Pro Tips

- **Check your thread regularly** for new assignments
- **Acknowledge tasks quickly** to avoid escalations
- **Provide detailed reasons** when requesting edits or early endings
- **Communicate with admins** if you're having issues with the system

---

## 🛠️ For Admins

### 🎮 Admin Dashboard & Monitoring

As an admin, you have access to powerful management tools and oversight capabilities.

### 🔧 System Configuration

#### Initial Setup Command: `/settings`

Configure core system parameters:

```
/settings 
  assignments_channel:#assignments
  admin_channel:#admin-alerts  
  operator_role:@Operator
  timezone:America/New_York
  min_on_duty:3
  cooldown_edit_sec:300
  cooldown_end_early_sec:600
```

**Parameters Explained:**
- **assignments_channel**: Where operator threads are created
- **admin_channel**: Where approval requests are sent to you
- **operator_role**: Role that identifies team operators
- **timezone**: Organization timezone (affects display times)
- **min_on_duty**: Minimum active operators required
- **cooldown_edit_sec**: Time between edit requests (seconds)
- **cooldown_end_early_sec**: Time between end early requests (seconds)

### 📋 Task Template Management

#### Creating Task Templates

```bash
# Basic task
/task add name:"Server Monitoring" priority:2

# Advanced task with time window
/task add name:"Peak Hours Support" priority:1 window_start:"09:00" window_end:"17:00"

# Task with custom parameters
/task add name:"Data Analysis" priority:3 params_schema:'{"location": "string", "dataset": "string"}'
```

#### Managing Tasks

```bash
# List all tasks
/task list

# Update existing task
/task update name:"Server Monitoring" priority:1

# Remove task
/task remove name:"Old Task Name"
```

### ⚡ Direct Assignment Control

#### Force Assign Tasks

For urgent situations or special assignments:

```bash
# Assign specific task to operator
/force_assign user:@operator task_name:"Emergency Response"

# Assign with custom parameters  
/force_assign user:@operator task_name:"Special Project" params:'{"urgency": "high", "client": "VIP"}'
```

**When to Use Force Assignment:**
- Emergency situations requiring immediate response
- Special projects outside normal rotation
- Covering for absent operators
- Testing new task configurations

### 👀 Approval Management

You'll receive approval requests in your admin channel for:

#### 📝 **Edit Requests**
- Operator wants to modify task parameters
- Includes current values → proposed changes
- Shows impact on system/other operators

**Approval Card Example:**
```
📝 Edit Task Approval Request
👤 Operator: John Doe
🎯 Task: Comms Lead  
🕐 Hour: 3
📄 Reason: Need to adjust monitoring frequency

Proposed Changes:
frequency: every 5min → every 2min
priority: normal → high

[✅ Approve] [❌ Deny]
```

#### ⏹️ **End Early Requests**
- Operator wants to finish task before hour boundary
- Requires justification
- May affect coverage/handoffs

**Decision Factors:**
- Validity of reason
- Impact on team coverage
- Urgency of situation
- Pattern of requests (frequent requests may indicate issues)

### 🎯 Best Practices for Admins

#### ✅ **Do:**
- Respond to approval requests promptly (within 15-30 minutes)
- Monitor system health regularly
- Keep task templates updated and relevant  
- Provide clear feedback when denying requests
- Document recurring issues and solutions

#### ❌ **Don't:**
- Let approval requests pile up without response
- Approve requests without reviewing context
- Ignore escalation patterns or system alerts
- Make configuration changes during peak hours
- Override system safeguards without good reason

---

## 📚 Commands Reference

### 👥 Operator Commands
*Operators primarily interact through the assignment widget buttons*

| Action | Method | Description |
|--------|---------|-------------|
| Start Task | 🟢 Button | Begin assigned task |
| Edit Task | ✏️ Button | Request parameter changes |  
| End Early | ⏹️ Button | Request early task completion |

### 🛠️ Admin Commands

#### System Configuration
| Command | Usage | Description |
|---------|--------|-------------|
| `/settings` | Various parameters | Configure system settings |

#### Task Management  
| Command | Usage | Description |
|---------|--------|-------------|
| `/task add` | `name:"Task Name" priority:1` | Create new task template |
| `/task list` | No parameters | Show all task templates |
| `/task update` | `name:"Task" priority:2` | Modify existing template |
| `/task remove` | `name:"Task Name"` | Delete task template |

#### Direct Control
| Command | Usage | Description |
|---------|--------|-------------|
| `/force_assign` | `user:@operator task_name:"Task"` | Directly assign task |

### 🔐 Permission Requirements

| Role | Required Permissions | Commands Available |
|------|---------------------|-------------------|
| **Operator** | `@Operator` role | Widget interactions only |
| **Admin** | `Manage Guild` OR `@Admin/@Manager` roles | All admin commands |

---

## 🔧 Troubleshooting

### 🚨 Common Issues & Solutions

#### **"Assignment not found" errors**
**Cause**: Database connectivity or sync issues  
**Solution**: 
1. Check database connection status
2. Restart bot if needed
3. Verify assignment IDs in logs

#### **Operators not receiving assignments**  
**Cause**: Role configuration or permissions  
**Solution**:
1. Verify `@Operator` role is correctly assigned
2. Check assignments channel permissions
3. Ensure bot has thread creation permissions

#### **Approval requests not appearing**
**Cause**: Admin channel misconfiguration  
**Solution**:
1. Run `/settings` to verify admin channel
2. Check bot permissions in admin channel
3. Test with a sample approval request

#### **Tasks not posting hourly**
**Cause**: Scheduler service issues  
**Solution**:
1. Check bot logs for scheduler errors
2. Verify system time and timezone settings
3. Restart scheduler service if needed

### 🔍 Debug Information

#### Useful Log Locations
- **Assignment Creation**: Look for "assignment_created" events
- **Approval Workflows**: Search for "approval_request" entries  
- **System Errors**: Filter for ERROR level messages
- **Performance Issues**: Check for timeout warnings

#### Status Check Commands
- `/settings` - Shows current configuration
- `/task list` - Displays all active task templates
- Check assignment threads for recent activity

### 📞 When to Contact Support

**Immediate Support Needed:**
- System completely down or unresponsive
- Data corruption or missing assignments
- Security concerns or unauthorized access
- Critical workflow failures during peak hours

**Standard Support Request:**
- Feature requests or enhancements
- Configuration questions
- Training for new admin users
- Performance optimization questions

---

## 🎯 Quick Start Checklist

### For New Operators
- [ ] Receive `@Operator` role from admin
- [ ] Locate your private thread in assignments channel  
- [ ] Wait for first assignment (posted hourly)
- [ ] Practice with Start/Edit/End Early buttons
- [ ] Read escalation guidelines (5-minute acknowledgment rule)

### For New Admins  
- [ ] Run `/settings` to configure system
- [ ] Set up assignments and admin channels
- [ ] Configure operator role and minimum staffing
- [ ] Create initial task templates with `/task add`
- [ ] Test approval workflow with sample requests
- [ ] Monitor system for first few hours of operation

---

*This system is designed to streamline task assignment and improve operational efficiency. For additional features or customization requests, please contact your system administrator.*