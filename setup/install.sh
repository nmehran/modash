#!/bin/bash

# Set up strict error handling
set -euo pipefail

# Constants
BASE_DIR="/opt/modash"
USER_HOME="$BASE_DIR/.user"
SCRIPT_DIR="$BASE_DIR/scripts"
MODASH_SHELL_PATH="$SCRIPT_DIR/modash_shell.sh"

# Include uninstall script to clean up any previous installations
echo "Checking for previous installations..."
if [ -f "$(dirname "$0")/uninstall.sh" ]; then
    echo "Running uninstallation of previous setup..."
    bash "$(dirname "$0")/uninstall.sh"
else
    echo "No uninstall script found, skipping..."
fi

# Create directories
echo "Creating directories..."
mkdir -p "$USER_HOME" "$SCRIPT_DIR"

# Copy the custom shell script to the appropriate directory
echo "Setting up the shell script..."
cp "$(dirname "$0")/modash_shell.sh" "$SCRIPT_DIR/"

# Make the shell script executable
chmod +x "$MODASH_SHELL_PATH"

# Check if the group exists and create if not
if ! getent group modash >/dev/null; then
    echo "Creating group 'modash'..."
    groupadd modash
fi

# Create the user with the restricted shell and no standard home directory
echo "Creating user 'modash'..."
useradd modash -s "$MODASH_SHELL_PATH" -d "$USER_HOME" -M -g modash

# Set ownership and permissions
chown -R modash:modash "$BASE_DIR"
chmod -R 755 "$BASE_DIR"
chmod 700 "$USER_HOME"

# Create and configure .bashrc file
echo "Configuring .bashrc for restricted environment..."
touch "$USER_HOME/.bashrc"
echo "# Restricted environment settings" > "$USER_HOME/.bashrc"
chmod 644 "$USER_HOME/.bashrc"
chown modash:modash "$USER_HOME/.bashrc"

# Make user home directory and script immutable
chattr +i "$USER_HOME"
chattr +i "$MODASH_SHELL_PATH"

# Ensure no other unnecessary files exist
rm -f "$USER_HOME/.bash_profile" "$USER_HOME/.profile"

echo "Installation complete: 'modash' is now configured as a restricted user."
