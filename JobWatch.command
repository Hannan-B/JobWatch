#!/bin/bash
# ============================================================================
# JobWatch.command  —  the double-click launcher (Phase E.1)
# ----------------------------------------------------------------------------
# Double-click this file in Finder to start JobWatch. It opens a small window,
# starts the app, and opens your browser. To stop JobWatch, close that window
# (or press Ctrl-C in it). You never have to type anything.
#
# It simply moves into the project folder and runs the app module with the
# Python that's already on your Mac. Nothing to install.
# ============================================================================

# Move to the folder this launcher lives in (the project folder), wherever it is.
cd "$(dirname "$0")" || exit 1

echo "Starting JobWatch…"
echo

# Run the app. python3 is the Python you confirmed in Phase 0.
python3 -m jobwatch.app

# If the app exits or errors, keep the window open so any message is readable.
echo
echo "JobWatch has stopped. You can close this window."
