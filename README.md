# LakBay Bot

A Discord bot for managing operator shifts, schedules, and joystick tracking.

## Setup

1. Create a `.env` file in the root directory with your bot token:
```
BOT_TOKEN=your_bot_token_here
```

2. Create the required channels in your Discord server:
   - `shift-changes`: Where operators announce their shift start
   - `shift-assignments`: Where individual schedules are posted
   - `stats`: Where operators can check their performance stats

3. Create an `operator` role in your Discord server and assign it to your operators.

4. Install dependencies:
```bash
python3 -m pip install -e .
```

5. Run the bot:

   ### Option 1: Basic Run (will stop when computer sleeps)
   ```bash
   python3 src/bot.py
   ```

   ### Option 2: Keep Running During Sleep (macOS)
   ```bash
   caffeinate -i python3 src/bot.py
   ```
   This prevents your Mac from idle sleeping while the bot is running.

   ### Option 3: Run in Background (recommended for production)
   ```bash
   nohup python3 src/bot.py &
   ```
   This runs the bot in the background and keeps it running even if you close the terminal.
   To stop the bot, use `ps aux | grep "python3 src/bot.py"` to find its process ID,
   then `kill <process_id>` to stop it.

## Usage

1. When operators start their shift, they should post in the `shift-changes` channel and mention all operators starting the shift. For example:
```
Hi everyone,
@operator1, @operator2, @operator3, @operator4, and I are starting this shift
```

2. The bot will:
   - Generate schedules for all mentioned operators
   - Send individual schedules to each operator in the `shift-assignments` channel
   - Send reminders before each assignment

3. Operators can check their stats in the `stats` channel:
   - Use `/stats` to see your current shift and all-time stats
   - Stats are shown only to you (ephemeral message)
   - Stats include:
     - Hours piloted and piloting score
     - Hours of data labelled and labelling score
     - Breaks remaining (max 2 per shift)
     - All-time totals and averages

4. Joystick Dashboard:
   - Use `/dashboard_init` (admin only) to create a joystick status dashboard
   - Use `/devuse`, `/broken`, `/fixed` to manage joystick status
   - Use `/start_session` and `/stop_session` to track operating sessions
   - Dashboard shows real-time status of all joysticks

## Features

- Automatic schedule generation based on group assignments
- Individual schedule visibility (operators only see their own schedules)
- Automatic reminders before assignments
- Comm lead assignments for DL activities
- Persistent shift data storage
- Performance tracking and statistics
- Real-time joystick status dashboard

## Requirements

- Python 3.8 or higher
- discord.py
- python-dotenv
- pytz 