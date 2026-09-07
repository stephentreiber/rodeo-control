#!/bin/bash
# Mac/Linux equivalent of install.bat -- see that file for the Windows version.
echo "Installing dependencies for Rodeo Control..."
python3 -m pip install -r requirements.txt

if [ $? -eq 0 ]; then
    echo ""
    echo "Done. You can now run ./run.sh to start the app."
    echo "Once it's running, you can check Settings > Updates any time to install a newer version."
else
    echo ""
    echo "That didn't complete successfully. On some Mac/Linux setups, pip"
    echo "refuses to install packages system-wide (an 'externally-managed-environment'"
    echo "error). If you saw that, try either of these instead:"
    echo "  python3 -m pip install --break-system-packages -r requirements.txt"
    echo "  -- or --"
    echo "  python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    echo "(if using the venv option, activate it with 'source venv/bin/activate' before"
    echo "running run.sh each time)"
fi

read -p "Press Enter to close..."
