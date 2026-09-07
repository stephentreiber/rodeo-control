#!/bin/bash
# Mac/Linux equivalent of run.bat -- see that file for the Windows version.
echo "Starting Rodeo Control..."
echo "Your browser will open automatically. Keep this window open while you work."
echo "Close this window (or press Ctrl+C) to stop the app."
echo "Check Settings > Updates in the app any time to look for a newer version."
python3 app.py

# Mirrors run.bat's `pause` -- if app.py exits (crash or Ctrl+C), keep the
# terminal window open long enough to actually read why, rather than it
# closing immediately (which some file managers' "Run in Terminal" does).
read -p "Press Enter to close..."
