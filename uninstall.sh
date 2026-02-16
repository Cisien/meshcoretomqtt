#!/bin/bash
# ============================================================================
# MeshCore to MQTT - Uninstaller
# ============================================================================
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════${NC}\n"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

prompt_yes_no() {
    local prompt="$1"
    local default="${2:-n}"
    local response

    if [ "$default" = "y" ]; then
        prompt="$prompt [Y/n]: "
    else
        prompt="$prompt [y/N]: "
    fi

    read -p "$prompt" response
    response=${response:-$default}

    case "$response" in
        [yY][eE][sS]|[yY]) return 0 ;;
        *) return 1 ;;
    esac
}

prompt_input() {
    local prompt="$1"
    local default="$2"
    local response

    if [ -n "$default" ]; then
        read -p "$prompt [$default]: " response
        echo "${response:-$default}"
    else
        read -p "$prompt: " response
        echo "$response"
    fi
}

# Default paths
DEFAULT_APP_DIR="/opt/mctomqtt"
DEFAULT_CONFIG_DIR="/etc/mctomqtt"
SYSTEMD_UNIT="/etc/systemd/system/mctomqtt.service"
LAUNCHD_PLIST="/Library/LaunchDaemons/com.meshcore.mctomqtt.plist"

# Detect system type
detect_system_type() {
    # Check for Docker container first
    if docker ps -a 2>/dev/null | grep -q mctomqtt; then
        echo "docker"
    elif command -v systemctl &> /dev/null; then
        echo "systemd"
    elif [ "$(uname)" = "Darwin" ]; then
        echo "launchd"
    else
        echo "unknown"
    fi
}

# Detect the service user from the installed systemd unit
detect_service_user() {
    if [ -f "$SYSTEMD_UNIT" ]; then
        grep -E '^User=' "$SYSTEMD_UNIT" 2>/dev/null | cut -d'=' -f2
    fi
}

# Remove systemd service
remove_systemd_service() {
    if [ -f "$SYSTEMD_UNIT" ]; then
        print_info "Stopping and removing systemd service (requires sudo)..."

        if sudo systemctl is-active --quiet mctomqtt.service; then
            sudo systemctl stop mctomqtt.service
            print_success "Service stopped"
        fi

        if sudo systemctl is-enabled --quiet mctomqtt.service; then
            sudo systemctl disable mctomqtt.service
            print_success "Service disabled"
        fi

        sudo rm -f "$SYSTEMD_UNIT"
        sudo systemctl daemon-reload
        print_success "Service removed"
    else
        print_info "No systemd service found"
    fi
}

# Remove launchd service (system-level daemon)
remove_launchd_service() {
    if [ -f "$LAUNCHD_PLIST" ]; then
        print_info "Stopping and removing launchd service (requires sudo)..."

        if launchctl list | grep -q com.meshcore.mctomqtt; then
            sudo launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
            print_success "Service unloaded"
        fi

        sudo rm -f "$LAUNCHD_PLIST"
        print_success "Service removed"

        # Clean up log files
        if prompt_yes_no "Remove log files?" "y"; then
            sudo rm -f /var/log/mctomqtt.log
            sudo rm -f /var/log/mctomqtt-error.log
            print_success "Log files removed"
        fi
    else
        print_info "No launchd service found"
    fi
}

# Remove Docker container and image
remove_docker() {
    if docker ps -a 2>/dev/null | grep -q mctomqtt; then
        print_info "Stopping and removing Docker container..."

        # Stop if running
        if docker ps | grep -q mctomqtt; then
            docker stop mctomqtt
            print_success "Container stopped"
        fi

        # Remove container
        docker rm mctomqtt
        print_success "Container removed"
    else
        print_info "No Docker container found"
    fi

    # Ask about removing image
    if docker images | grep -q "mctomqtt"; then
        if prompt_yes_no "Remove Docker image (mctomqtt:latest)?" "y"; then
            docker rmi mctomqtt:latest
            print_success "Docker image removed"
        fi
    fi
}

# Remove service user
remove_service_user() {
    local svc_user="$1"

    if [ -z "$svc_user" ]; then
        return
    fi

    # Don't offer to remove root or the current user
    if [ "$svc_user" = "root" ] || [ "$svc_user" = "$(whoami)" ]; then
        return
    fi

    # Check if the user actually exists
    if ! id "$svc_user" &>/dev/null; then
        return
    fi

    print_warning "Service was running as user: $svc_user"
    if prompt_yes_no "Remove service user '$svc_user'?" "n"; then
        if sudo userdel "$svc_user" 2>/dev/null; then
            print_success "User '$svc_user' removed"
        else
            print_error "Failed to remove user '$svc_user' - you may need to remove it manually"
        fi
    else
        print_info "Keeping user '$svc_user'"
    fi
}

# Remove configuration files
remove_config() {
    local config_dir="$DEFAULT_CONFIG_DIR"
    local user_toml="$config_dir/config.d/00-user.toml"

    if [ ! -d "$config_dir" ]; then
        print_info "No configuration directory found at $config_dir"
        return
    fi

    # Offer to back up 00-user.toml before removal
    if [ -f "$user_toml" ]; then
        echo "Your user configuration file:"
        echo ""
        cat "$user_toml" | head -20
        if [ $(wc -l < "$user_toml") -gt 20 ]; then
            echo "..."
        fi
        echo ""

        if prompt_yes_no "Do you want to back up 00-user.toml before uninstalling?" "y"; then
            BACKUP_FILE="$HOME/mctomqtt-user-toml-backup-$(date +%Y%m%d-%H%M%S).toml"
            sudo cp "$user_toml" "$BACKUP_FILE"
            sudo chown "$(whoami)" "$BACKUP_FILE"
            print_success "Configuration backed up to: $BACKUP_FILE"
        fi
    fi

    # Remove config files and directories
    if prompt_yes_no "Remove configuration directory ($config_dir)?" "y"; then
        if [ -f "$config_dir/config.toml" ]; then
            sudo rm -f "$config_dir/config.toml"
            print_success "Removed $config_dir/config.toml"
        fi

        if [ -d "$config_dir/config.d" ]; then
            sudo rm -rf "$config_dir/config.d"
            print_success "Removed $config_dir/config.d/"
        fi

        sudo rm -rf "$config_dir"
        print_success "Removed $config_dir/"
        KEEP_CONFIG=false
    else
        print_info "Keeping configuration directory: $config_dir"
        KEEP_CONFIG=true
    fi
}

# Main uninstallation
main() {
    print_header "MeshCore to MQTT Uninstaller"

    echo "This will remove MeshCore to MQTT from your system."
    echo ""

    # Determine application directory
    APP_DIR=$(prompt_input "Application directory" "$DEFAULT_APP_DIR")
    APP_DIR="${APP_DIR/#\~/$HOME}"  # Expand tilde

    if [ ! -d "$APP_DIR" ]; then
        print_error "Application directory not found: $APP_DIR"
        exit 1
    fi

    print_warning "This will remove:"
    echo "  Application files: $APP_DIR"
    echo "  Configuration:     $DEFAULT_CONFIG_DIR"
    echo ""

    if ! prompt_yes_no "Are you sure you want to continue?" "n"; then
        print_info "Uninstallation cancelled"
        exit 0
    fi

    # Detect service user from systemd unit before removing the service
    SVC_USER=""
    if [ -f "$SYSTEMD_UNIT" ]; then
        SVC_USER=$(detect_service_user)
        if [ -n "$SVC_USER" ]; then
            print_info "Detected service user: $SVC_USER"
        fi
    fi

    # Stop and remove service
    print_header "Removing Service"

    SYSTEM_TYPE=$(detect_system_type)
    print_info "Detected system type: $SYSTEM_TYPE"

    case "$SYSTEM_TYPE" in
        docker)
            remove_docker
            ;;
        systemd)
            remove_systemd_service
            ;;
        launchd)
            remove_launchd_service
            ;;
        *)
            print_info "Unknown system type - skipping service removal"
            ;;
    esac

    # Handle configuration files
    print_header "Configuration Files"

    KEEP_CONFIG=false
    remove_config

    # Remove application directory
    print_header "Removing Files"

    print_info "Removing application directory..."
    sudo rm -rf "$APP_DIR"
    print_success "Application directory removed: $APP_DIR"

    # Offer to remove the service user (systemd only)
    if [ "$SYSTEM_TYPE" = "systemd" ] && [ -n "$SVC_USER" ]; then
        print_header "Service User"
        remove_service_user "$SVC_USER"
    fi

    # Final message
    print_header "Uninstallation Complete"

    if [ "$KEEP_CONFIG" = true ]; then
        echo "MeshCore to MQTT has been removed (configuration kept)."
        echo "Configuration directory: $DEFAULT_CONFIG_DIR"
    else
        echo "MeshCore to MQTT has been completely removed."
    fi

    echo ""
    print_success "Uninstallation complete!"
}

# Run main
main "$@"
