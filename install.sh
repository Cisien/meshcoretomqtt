#!/bin/bash
# ============================================================================
# MeshCore to MQTT - Interactive Installer
# ============================================================================
set -e

SCRIPT_VERSION="1.0.6"
DEFAULT_REPO="Cisien/meshcoretomqtt"
DEFAULT_BRANCH="main"

# Parse command line arguments
CONFIG_URL=""
UPDATE_MODE=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --config)
            CONFIG_URL="$2"
            shift 2
            ;;
        --repo)
            DEFAULT_REPO="$2"
            shift 2
            ;;
        --branch)
            DEFAULT_BRANCH="$2"
            shift 2
            ;;
        --update)
            UPDATE_MODE=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--config URL] [--repo owner/repo] [--branch branch-name] [--update]"
            exit 1
            ;;
    esac
done

# Use environment variables if set, otherwise use defaults/args
REPO="${INSTALL_REPO:-$DEFAULT_REPO}"
BRANCH="${INSTALL_BRANCH:-$DEFAULT_BRANCH}"

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

# Download file with retry
download_file() {
    local url="$1"
    local dest="$2"
    local name="$3"
    print_info "Downloading $name..."
    curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$dest" || { print_error "Failed to download $name"; return 1; }
}

# Check service/container health after start
check_service_health() {
    local service_type="$1"
    print_info "Waiting for service to start..."
    sleep 5
    
    case "$service_type" in
        docker)
            DOCKER=$(docker_cmd) || DOCKER="docker"
            if $DOCKER ps | grep -q mctomqtt && $DOCKER logs mctomqtt 2>&1 | tail -20 | grep -qE "(Re)?connected to.*MQTT broker"; then
                print_success "Container started and connected successfully"
            else
                print_warning "Container started but may not be connected yet"
            fi
            echo ""
            print_info "Recent logs:"
            $DOCKER logs mctomqtt 2>&1 | tail -10
            ;;
        systemd)
            if sudo systemctl is-active --quiet mctomqtt.service && sudo journalctl -u mctomqtt.service --since "10 seconds ago" | grep -qE "(Re)?connected to.*MQTT broker"; then
                print_success "Service started and connected successfully"
            else
                print_warning "Service started but may not be connected yet"
            fi
            echo ""
            print_info "Recent logs:"
            sudo journalctl -u mctomqtt.service -n 10 --no-pager
            ;;
        launchd)
            if launchctl list | grep -q com.meshcore.mctomqtt; then
                print_success "Service started successfully"
            else
                print_error "Service may not be running"
            fi
            echo ""
            print_info "Recent logs:"
            tail -10 ~/Library/Logs/mctomqtt.log 2>/dev/null || print_info "No logs available yet"
            ;;
    esac
}

# Detect if docker needs sudo
docker_cmd() {
    if docker info &> /dev/null 2>&1; then
        echo "docker"
    elif sudo docker info &> /dev/null 2>&1; then
        echo "sudo docker"
    else
        return 1
    fi
}

# Prompt and validate IATA code
prompt_iata() {
    local existing="$1"
    local iata=""
    
    echo "" >&2
    print_info "IATA code is a 3-letter airport code (e.g., SEA, LAX, NYC, LON)" >&2
    echo "" >&2
    
    while [ -z "$iata" ] || [ "$iata" = "XXX" ]; do
        iata=$(prompt_input "Enter your IATA code (3 letters)" "${existing:-}")
        iata=$(echo "$iata" | tr '[:lower:]' '[:upper:]' | tr -d ' ')
        
        if [ -z "$iata" ] || [ "$iata" = "XXX" ]; then
            print_error "Please enter a valid IATA code"
        elif [ ${#iata} -ne 3 ] && ! prompt_yes_no "Use '$iata' anyway?" "n"; then
            iata=""
        fi
    done
    
    echo "$iata"
}

# Validate and normalize MeshCore public key
validate_meshcore_pubkey() {
    local key="$1"
    
    # Remove spaces and convert to uppercase
    key=$(echo "$key" | tr -d ' ' | tr '[:lower:]' '[:upper:]')
    
    # Check if it's exactly 64 hex characters (32 bytes)
    if [ ${#key} -ne 64 ]; then
        return 1
    fi
    
    # Verify it's valid hex
    if ! echo "$key" | grep -qE '^[0-9A-F]{64}$'; then
        return 1
    fi
    
    echo "$key"
    return 0
}

# Validate email format and normalize to lowercase
validate_email() {
    local email="$1"
    
    # Basic sanity checks - must contain @ and at least one dot after @
    if [[ ! "$email" =~ @ ]] || [[ ! "$email" =~ @.*\. ]]; then
        return 1
    fi
    
    # Check for basic invalid patterns
    # - Must not start or end with @ or .
    # - Must not have consecutive dots
    # - Must not have spaces
    if [[ "$email" =~ ^[.@] ]] || [[ "$email" =~ [.@]$ ]] || [[ "$email" =~ \.\. ]] || [[ "$email" =~ [[:space:]] ]]; then
        return 1
    fi
    
    # Must have at least one character before @
    # Must have at least one character between @ and last dot
    # Must have at least 2 characters after last dot (TLD)
    local local_part="${email%%@*}"
    local domain="${email#*@}"
    
    if [ ${#local_part} -lt 1 ] || [ ${#domain} -lt 3 ]; then
        return 1
    fi
    
    # Domain must have at least one dot and valid TLD
    if [[ ! "$domain" =~ \. ]]; then
        return 1
    fi
    
    # Normalize to lowercase
    echo "$email" | tr '[:upper:]' '[:lower:]'
    return 0
}

# Prompt for owner email with validation
prompt_owner_email() {
    local existing="$1"
    local email=""
    local validated=""
    
    echo "" >&2
    print_info "Owner email" >&2
    echo "" >&2
    
    while true; do
        email=$(prompt_input "Enter owner email (or leave empty to skip)" "${existing:-}")
        
        # Allow empty (skip)
        if [ -z "$email" ]; then
            echo ""
            return 0
        fi
        
        # Validate and normalize
        validated=$(validate_email "$email")
        if [ $? -eq 0 ]; then
            echo "$validated"
            return 0
        else
            print_error "Invalid email format"
            if ! prompt_yes_no "Try again?" "y"; then
                echo ""
                return 0
            fi
        fi
    done
}

# Prompt for owner public key with validation
prompt_owner_pubkey() {
    local existing="$1"
    local owner=""
    local validated=""
    
    echo "" >&2
    print_info "Owner public key is a 64-character hex string (MeshCore public key)" >&2
    print_info "Example: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" >&2
    echo "" >&2
    
    while true; do
        owner=$(prompt_input "Enter owner public key (or leave empty to skip)" "${existing:-}")
        
        # Allow empty (skip)
        if [ -z "$owner" ]; then
            echo ""
            return 0
        fi
        
        # Validate and normalize
        validated=$(validate_meshcore_pubkey "$owner")
        if [ $? -eq 0 ]; then
            echo "$validated"
            return 0
        else
            print_error "Invalid public key format. Must be 64 hex characters (32 bytes)"
            if ! prompt_yes_no "Try again?" "y"; then
                echo ""
                return 0
            fi
        fi
    done
}

# Update owner public key and email for existing brokers using auth tokens
update_owner_info() {
    ENV_LOCAL="$INSTALL_DIR/.env.local"
    
    if [ ! -f "$ENV_LOCAL" ]; then
        print_error "No configuration file found"
        return 1
    fi
    
    # Find all brokers using auth tokens
    local auth_token_brokers=()
    for broker_num in {1..4}; do
        if grep -q "^MCTOMQTT_MQTT${broker_num}_USE_AUTH_TOKEN=true" "$ENV_LOCAL" 2>/dev/null; then
            auth_token_brokers+=($broker_num)
        fi
    done
    
    if [ ${#auth_token_brokers[@]} -eq 0 ]; then
        print_warning "No brokers configured with auth token authentication"
        return 0
    fi
    
    echo ""
    print_header "Update Owner Information"
    print_info "Found ${#auth_token_brokers[@]} broker(s) using auth token authentication:"
    echo ""
    print_info "Current configuration:"
    for broker_num in "${auth_token_brokers[@]}"; do
        local server=$(grep "^MCTOMQTT_MQTT${broker_num}_SERVER=" "$ENV_LOCAL" 2>/dev/null | cut -d'=' -f2)
        local existing_owner=$(grep "^MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=" "$ENV_LOCAL" 2>/dev/null | cut -d'=' -f2)
        local existing_email=$(grep "^MCTOMQTT_MQTT${broker_num}_TOKEN_EMAIL=" "$ENV_LOCAL" 2>/dev/null | cut -d'=' -f2)
        echo "  MQTT${broker_num} ($server):"
        if [ -n "$existing_owner" ]; then
            echo "    Owner: $existing_owner"
        else
            echo "    Owner: (not set)"
        fi
        if [ -n "$existing_email" ]; then
            echo "    Email: $existing_email"
        else
            echo "    Email: (not set)"
        fi
    done
    echo ""
    
    # Get first existing owner and email or empty
    local first_existing_owner=$(grep "^MCTOMQTT_MQTT[0-9]_TOKEN_OWNER=" "$ENV_LOCAL" 2>/dev/null | head -1 | cut -d'=' -f2)
    local first_existing_email=$(grep "^MCTOMQTT_MQTT[0-9]_TOKEN_EMAIL=" "$ENV_LOCAL" 2>/dev/null | head -1 | cut -d'=' -f2)
    
    # Prompt for new owner
    local new_owner=$(prompt_owner_pubkey "$first_existing_owner")
    
    # Prompt for new email
    local new_email=$(prompt_owner_email "$first_existing_email")
    
    # Back up config
    cp "$ENV_LOCAL" "$ENV_LOCAL.backup-$(date +%Y%m%d-%H%M%S)"
    
    # Update all auth token brokers
    for broker_num in "${auth_token_brokers[@]}"; do
        # Remove existing owner line if present
        if grep -q "^MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=" "$ENV_LOCAL"; then
            sed -i.tmp "/^MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=/d" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.tmp"
        fi
        
        # Remove existing email line if present
        if grep -q "^MCTOMQTT_MQTT${broker_num}_TOKEN_EMAIL=" "$ENV_LOCAL"; then
            sed -i.tmp "/^MCTOMQTT_MQTT${broker_num}_TOKEN_EMAIL=/d" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.tmp"
        fi
        
        # Add new owner if provided
        if [ -n "$new_owner" ]; then
            # Find the line with TOKEN_AUDIENCE or USE_AUTH_TOKEN and add owner after it
            if grep -q "^MCTOMQTT_MQTT${broker_num}_TOKEN_AUDIENCE=" "$ENV_LOCAL"; then
                sed -i.tmp "/^MCTOMQTT_MQTT${broker_num}_TOKEN_AUDIENCE=/a\\
MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=$new_owner" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.tmp"
            else
                sed -i.tmp "/^MCTOMQTT_MQTT${broker_num}_USE_AUTH_TOKEN=/a\\
MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=$new_owner" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.tmp"
            fi
        fi
        
        # Add new email if provided
        if [ -n "$new_email" ]; then
            # Find the line with TOKEN_OWNER or TOKEN_AUDIENCE or USE_AUTH_TOKEN and add email after it
            if grep -q "^MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=" "$ENV_LOCAL"; then
                sed -i.tmp "/^MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=/a\\
MCTOMQTT_MQTT${broker_num}_TOKEN_EMAIL=$new_email" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.tmp"
            elif grep -q "^MCTOMQTT_MQTT${broker_num}_TOKEN_AUDIENCE=" "$ENV_LOCAL"; then
                sed -i.tmp "/^MCTOMQTT_MQTT${broker_num}_TOKEN_AUDIENCE=/a\\
MCTOMQTT_MQTT${broker_num}_TOKEN_EMAIL=$new_email" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.tmp"
            else
                sed -i.tmp "/^MCTOMQTT_MQTT${broker_num}_USE_AUTH_TOKEN=/a\\
MCTOMQTT_MQTT${broker_num}_TOKEN_EMAIL=$new_email" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.tmp"
            fi
        fi
    done
    
    # Display summary
    local changes=()
    if [ -n "$new_owner" ]; then
        changes+=("owner: $new_owner")
    fi
    if [ -n "$new_email" ]; then
        changes+=("email: $new_email")
    fi
    
    if [ ${#changes[@]} -gt 0 ]; then
        print_success "Updated ${#auth_token_brokers[@]} broker(s) with ${changes[*]}"
    else
        print_success "No changes made"
    fi
}

# Detect available serial devices
detect_serial_devices() {
    local devices=()
    
    if [ "$(uname)" = "Darwin" ]; then
        # macOS: Use /dev/cu.* devices (callout devices, preferred over tty.*)
        # Look for common USB serial adapters
        while IFS= read -r device; do
            devices+=("$device")
        done < <(ls /dev/cu.usb* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART* 2>/dev/null | sort)
    else
        # Linux: Prefer /dev/serial/by-id/ for persistent naming
        if [ -d /dev/serial/by-id ]; then
            while IFS= read -r device; do
                devices+=("$device")
            done < <(ls -1 /dev/serial/by-id/ 2>/dev/null | sed 's|^|/dev/serial/by-id/|')
        fi
        
        # Also check /dev/ttyACM* and /dev/ttyUSB* as fallback
        while IFS= read -r device; do
            # Only add if not already in list via by-id
            local already_added=false
            for existing in "${devices[@]}"; do
                if [ "$(readlink -f "$existing" 2>/dev/null)" = "$device" ]; then
                    already_added=true
                    break
                fi
            done
            if [ "$already_added" = false ]; then
                devices+=("$device")
            fi
        done < <(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | sort)
    fi
    
    printf '%s\n' "${devices[@]}"
}

# Interactive device selection
# Sets SELECTED_SERIAL_DEVICE variable
select_serial_device() {
    local devices=()
    mapfile -t devices < <(detect_serial_devices)
    
    echo ""
    print_header "Serial Device Selection"
    echo ""
    
    if [ ${#devices[@]} -eq 0 ]; then
        print_warning "No serial devices detected"
        echo ""
        echo "  1) Enter path manually"
        echo ""
        local choice=$(prompt_input "Select option [1]" "1")
        SELECTED_SERIAL_DEVICE=$(prompt_input "Enter serial device path" "/dev/ttyACM0")
        return
    fi
    
    if [ ${#devices[@]} -eq 1 ]; then
        print_info "Found 1 serial device:"
    else
        print_info "Found ${#devices[@]} serial devices:"
    fi
    echo ""
    
    local i=1
    for device in "${devices[@]}"; do
        # Try to get device info
        local info=""
        if [ "$(uname)" = "Darwin" ]; then
            # macOS: device name is usually descriptive
            info="$device"
        else
            # Linux: show both by-id path and resolved device
            if [[ "$device" == /dev/serial/by-id/* ]]; then
                local resolved=$(readlink -f "$device" 2>/dev/null)
                info="$device -> $resolved"
            else
                info="$device"
            fi
        fi
        echo "  $i) $info"
        ((i++))
    done
    
    echo "  $i) Enter path manually"
    echo ""
    
    while true; do
        local choice=$(prompt_input "Select device [1-$i]" "1")
        
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le $i ]; then
            if [ "$choice" -eq $i ]; then
                # Manual entry
                SELECTED_SERIAL_DEVICE=$(prompt_input "Enter serial device path" "/dev/ttyACM0")
                return
            else
                # Selected from list
                SELECTED_SERIAL_DEVICE="${devices[$((choice-1))]}"
                return
            fi
        else
            print_error "Invalid selection. Please enter a number between 1 and $i"
        fi
    done
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
    
    # Read from /dev/tty to work when stdin is piped
    read -p "$prompt" response </dev/tty
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
    
    # Read from /dev/tty to work when stdin is piped
    if [ -n "$default" ]; then
        read -p "$prompt [$default]: " response </dev/tty
        echo "${response:-$default}"
    else
        read -p "$prompt: " response </dev/tty
        echo "$response"
    fi
}

# Configure MQTT brokers (smart - offers LetsMesh by default)
configure_mqtt_brokers() {
    ENV_LOCAL="$INSTALL_DIR/.env.local"
    
    # Ensure .env.local exists with update source info
    if [ ! -f "$ENV_LOCAL" ]; then
        # Interactive device selection
        select_serial_device
        
        cat > "$ENV_LOCAL" << EOF
# MeshCore to MQTT Configuration
# This file contains your local overrides to the defaults in .env

# Update source (configured by installer)
MCTOMQTT_UPDATE_REPO=$REPO
MCTOMQTT_UPDATE_BRANCH=$BRANCH

# Serial Configuration
MCTOMQTT_SERIAL_PORTS=$SELECTED_SERIAL_DEVICE

# Location Code
MCTOMQTT_IATA=XXX
EOF
    fi
    
    # Prompt for IATA if needed
    IATA=$(grep "^MCTOMQTT_IATA=" "$ENV_LOCAL" 2>/dev/null | cut -d'=' -f2)
    if [ -z "$IATA" ] || [ "$IATA" = "XXX" ]; then
        IATA=$(prompt_iata "")
        sed -i.bak "s/^MCTOMQTT_IATA=.*/MCTOMQTT_IATA=$IATA/" "$ENV_LOCAL" && rm -f "$ENV_LOCAL.bak"
        print_success "IATA code set to: $IATA"
    fi
    
    echo ""
    print_header "MQTT Broker Configuration"
    echo ""
    print_info "Enable the LetsMesh.net Packet Analyzer MQTT servers?"
    echo "  • Real-time packet analysis and visualization"
    echo "  • Network health monitoring"
    echo "  • Includes US and EU regional brokers for redundancy"
    echo "  • Requires meshcore-decoder for authentication"
    echo ""
    
    if [ "$DECODER_AVAILABLE" = true ]; then
        if prompt_yes_no "Enable LetsMesh Packet Analyzer MQTT servers?" "y"; then
            # Prompt for owner public key and email once for LetsMesh brokers
            echo ""
            print_info "LetsMesh Packet Analyzer supports optional owner identification"
            print_info "This links your observer to your MeshCore public key and email"
            OWNER_PUBKEY=$(prompt_owner_pubkey "")
            OWNER_EMAIL=$(prompt_owner_email "")
            
            # Configure MQTT1 (US)
            cat >> "$ENV_LOCAL" << EOF

# MQTT Broker 1 - LetsMesh.net Packet Analyzer (US)
MCTOMQTT_MQTT1_ENABLED=true
MCTOMQTT_MQTT1_SERVER=mqtt-us-v1.letsmesh.net
MCTOMQTT_MQTT1_PORT=443
MCTOMQTT_MQTT1_TRANSPORT=websockets
MCTOMQTT_MQTT1_USE_TLS=true
MCTOMQTT_MQTT1_USE_AUTH_TOKEN=true
MCTOMQTT_MQTT1_TOKEN_AUDIENCE=mqtt-us-v1.letsmesh.net
EOF
            
            if [ -n "$OWNER_PUBKEY" ]; then
                echo "MCTOMQTT_MQTT1_TOKEN_OWNER=$OWNER_PUBKEY" >> "$ENV_LOCAL"
            fi
            if [ -n "$OWNER_EMAIL" ]; then
                echo "MCTOMQTT_MQTT1_TOKEN_EMAIL=$OWNER_EMAIL" >> "$ENV_LOCAL"
            fi
            
            # Configure MQTT2 (EU)
            cat >> "$ENV_LOCAL" << EOF

# MQTT Broker 2 - LetsMesh.net Packet Analyzer (EU)
MCTOMQTT_MQTT2_ENABLED=true
MCTOMQTT_MQTT2_SERVER=mqtt-eu-v1.letsmesh.net
MCTOMQTT_MQTT2_PORT=443
MCTOMQTT_MQTT2_TRANSPORT=websockets
MCTOMQTT_MQTT2_USE_TLS=true
MCTOMQTT_MQTT2_USE_AUTH_TOKEN=true
MCTOMQTT_MQTT2_TOKEN_AUDIENCE=mqtt-eu-v1.letsmesh.net
EOF
            
            if [ -n "$OWNER_PUBKEY" ]; then
                echo "MCTOMQTT_MQTT2_TOKEN_OWNER=$OWNER_PUBKEY" >> "$ENV_LOCAL"
            fi
            if [ -n "$OWNER_EMAIL" ]; then
                echo "MCTOMQTT_MQTT2_TOKEN_EMAIL=$OWNER_EMAIL" >> "$ENV_LOCAL"
            fi
            
            # Build success message
            local owner_info=""
            if [ -n "$OWNER_PUBKEY" ] && [ -n "$OWNER_EMAIL" ]; then
                owner_info=" with owner: $OWNER_PUBKEY ($OWNER_EMAIL)"
            elif [ -n "$OWNER_PUBKEY" ]; then
                owner_info=" with owner: $OWNER_PUBKEY"
            elif [ -n "$OWNER_EMAIL" ]; then
                owner_info=" with email: $OWNER_EMAIL"
            fi
            print_success "LetsMesh Packet Analyzer MQTT servers enabled${owner_info}"
            
            if prompt_yes_no "Would you like to configure additional MQTT brokers?" "n"; then
                configure_additional_brokers
            fi
        else
            # User declined LetsMesh, ask if they want to configure a custom broker
            if prompt_yes_no "Would you like to configure a custom MQTT broker?" "y"; then
                configure_custom_broker 1
                
                if prompt_yes_no "Would you like to configure additional MQTT brokers?" "n"; then
                    configure_additional_brokers
                fi
            else
                print_warning "No MQTT brokers configured - you'll need to edit .env.local manually"
            fi
        fi
    else
        # No decoder available, can't use LetsMesh
        print_warning "meshcore-decoder not available - cannot use LetsMesh auth token authentication"
        
        if prompt_yes_no "Would you like to configure a custom MQTT broker with username/password?" "y"; then
            configure_custom_broker 1
            
            if prompt_yes_no "Would you like to configure additional MQTT brokers?" "n"; then
                configure_additional_brokers
            fi
        else
            print_warning "No MQTT brokers configured - you'll need to edit .env.local manually"
        fi
    fi
}

# Configure additional brokers (auto-detects next available number)
configure_additional_brokers() {
    # Find next available broker number (starts from 2, or 3 if LetsMesh is enabled)
    NEXT_BROKER=2
    while grep -q "^MCTOMQTT_MQTT${NEXT_BROKER}_ENABLED=" "$INSTALL_DIR/.env.local" 2>/dev/null; do
        NEXT_BROKER=$((NEXT_BROKER + 1))
    done
    
    NUM_ADDITIONAL=$(prompt_input "How many additional brokers?" "1")
    
    for i in $(seq 1 $NUM_ADDITIONAL); do
        BROKER_NUM=$((NEXT_BROKER + i - 1))
        configure_custom_broker $BROKER_NUM
    done
}

# Configure a single custom MQTT broker
configure_custom_broker() {
    local BROKER_NUM=$1
    ENV_LOCAL="$INSTALL_DIR/.env.local"
    
    echo ""
    print_header "Configuring MQTT Broker $BROKER_NUM"
    
    SERVER=$(prompt_input "Server hostname/IP")
    if [ -z "$SERVER" ]; then
        print_warning "Server hostname required - skipping broker $BROKER_NUM"
        return
    fi
    
    echo "" >> "$ENV_LOCAL"
    echo "# MQTT Broker $BROKER_NUM" >> "$ENV_LOCAL"
    echo "MCTOMQTT_MQTT${BROKER_NUM}_ENABLED=true" >> "$ENV_LOCAL"
    echo "MCTOMQTT_MQTT${BROKER_NUM}_SERVER=$SERVER" >> "$ENV_LOCAL"
    
    PORT=$(prompt_input "Port" "1883")
    echo "MCTOMQTT_MQTT${BROKER_NUM}_PORT=$PORT" >> "$ENV_LOCAL"
    
    # Transport
    if prompt_yes_no "Use WebSockets transport?" "n"; then
        echo "MCTOMQTT_MQTT${BROKER_NUM}_TRANSPORT=websockets" >> "$ENV_LOCAL"
    fi
    
    # TLS
    if prompt_yes_no "Use TLS/SSL encryption?" "n"; then
        echo "MCTOMQTT_MQTT${BROKER_NUM}_USE_TLS=true" >> "$ENV_LOCAL"
        
        if ! prompt_yes_no "Verify TLS certificates?" "y"; then
            echo "MCTOMQTT_MQTT${BROKER_NUM}_TLS_VERIFY=false" >> "$ENV_LOCAL"
        fi
    fi
    
    # Authentication
    echo ""
    print_info "Authentication method:"
    echo "  1) Username/Password"
    echo "  2) MeshCore Auth Token (requires meshcore-decoder)"
    echo "  3) None (anonymous)"
    AUTH_TYPE=$(prompt_input "Choose authentication method [1-3]" "1")
    
    if [ "$AUTH_TYPE" = "2" ]; then
        if [ "$DECODER_AVAILABLE" = false ]; then
            print_error "meshcore-decoder not available - using username/password instead"
            AUTH_TYPE=1
        else
            echo "MCTOMQTT_MQTT${BROKER_NUM}_USE_AUTH_TOKEN=true" >> "$ENV_LOCAL"
            TOKEN_AUDIENCE=$(prompt_input "Token audience (optional)" "")
            if [ -n "$TOKEN_AUDIENCE" ]; then
                echo "MCTOMQTT_MQTT${BROKER_NUM}_TOKEN_AUDIENCE=$TOKEN_AUDIENCE" >> "$ENV_LOCAL"
            fi
            
            # Prompt for owner public key
            OWNER_PUBKEY=$(prompt_owner_pubkey "")
            if [ -n "$OWNER_PUBKEY" ]; then
                echo "MCTOMQTT_MQTT${BROKER_NUM}_TOKEN_OWNER=$OWNER_PUBKEY" >> "$ENV_LOCAL"
            fi
            
            # Prompt for owner email
            OWNER_EMAIL=$(prompt_owner_email "")
            if [ -n "$OWNER_EMAIL" ]; then
                echo "MCTOMQTT_MQTT${BROKER_NUM}_TOKEN_EMAIL=$OWNER_EMAIL" >> "$ENV_LOCAL"
            fi
            
            # Build success message
            if [ -n "$OWNER_PUBKEY" ] && [ -n "$OWNER_EMAIL" ]; then
                print_success "Owner info set: $OWNER_PUBKEY ($OWNER_EMAIL)"
            elif [ -n "$OWNER_PUBKEY" ]; then
                print_success "Owner public key set: $OWNER_PUBKEY"
            elif [ -n "$OWNER_EMAIL" ]; then
                print_success "Owner email set: $OWNER_EMAIL"
            fi
        fi
    fi
    
    if [ "$AUTH_TYPE" = "1" ]; then
        USERNAME=$(prompt_input "Username" "")
        if [ -n "$USERNAME" ]; then
            echo "MCTOMQTT_MQTT${BROKER_NUM}_USERNAME=$USERNAME" >> "$ENV_LOCAL"
            PASSWORD=$(prompt_input "Password" "")
            if [ -n "$PASSWORD" ]; then
                echo "MCTOMQTT_MQTT${BROKER_NUM}_PASSWORD=$PASSWORD" >> "$ENV_LOCAL"
            fi
        fi
    fi
    
    print_success "Broker $BROKER_NUM configured"
}

# Try to migrate old config.ini to .env.local
migrate_config_ini() {
    local config_ini="$INSTALL_DIR/config.ini"
    local env_local="$INSTALL_DIR/.env.local"
    
    if [ ! -f "$config_ini" ]; then
        return 0  # No config.ini, nothing to migrate
    fi
    
    if [ -f "$env_local" ]; then
        # .env.local already exists, ask user
        echo ""
        print_warning "Found both config.ini and .env.local"
        if ! prompt_yes_no "Attempt to migrate settings from config.ini to .env.local?" "n"; then
            return 0
        fi
    fi
    
    print_info "Attempting to migrate config.ini to .env.local format..."
    
    # Create backup
    cp "$config_ini" "$config_ini.backup-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    
    # Try to extract values (simple grep-based parsing)
    local serial_ports=$(grep -A5 "^\[serial\]" "$config_ini" | grep "^ports" | cut -d'=' -f2- | tr -d ' ')
    local iata=$(grep -A10 "^\[topics\]" "$config_ini" | grep "^status.*meshcore/" | sed -E 's/.*meshcore\/([A-Z]{3})\/.*/\1/' | head -1)
    
    # MQTT1 settings
    local mqtt1_server=$(grep -A20 "^\[mqtt\]" "$config_ini" | grep "^server" | head -1 | cut -d'=' -f2- | tr -d ' ')
    local mqtt1_port=$(grep -A20 "^\[mqtt\]" "$config_ini" | grep "^port" | head -1 | cut -d'=' -f2- | tr -d ' ')
    local mqtt1_username=$(grep -A20 "^\[mqtt\]" "$config_ini" | grep "^username" | head -1 | cut -d'=' -f2- | tr -d ' ')
    local mqtt1_password=$(grep -A20 "^\[mqtt\]" "$config_ini" | grep "^password" | head -1 | cut -d'=' -f2- | tr -d ' ')
    local mqtt1_use_auth=$(grep -A20 "^\[mqtt\]" "$config_ini" | grep "^use_auth_token" | head -1 | cut -d'=' -f2- | tr -d ' ')
    local mqtt1_transport=$(grep -A20 "^\[mqtt\]" "$config_ini" | grep "^transport" | head -1 | cut -d'=' -f2- | tr -d ' ')
    
    # Check if we got anything useful
    if [ -z "$serial_ports" ] && [ -z "$mqtt1_server" ]; then
        print_error "Could not extract settings from config.ini"
        if prompt_yes_no "Continue with manual configuration?" "y"; then
            return 0
        else
            exit 1
        fi
    fi
    
    # Create .env.local with migrated settings
    cat > "$env_local" << EOF
# MeshCore to MQTT Configuration
# Migrated from config.ini on $(date)

# Serial Configuration
MCTOMQTT_SERIAL_PORTS=${serial_ports:-/dev/ttyACM0}

# Location Code
MCTOMQTT_IATA=${iata:-XXX}
EOF
    
    if [ -n "$mqtt1_server" ]; then
        cat >> "$env_local" << EOF

# MQTT Broker 1
MCTOMQTT_MQTT1_ENABLED=true
MCTOMQTT_MQTT1_SERVER=$mqtt1_server
EOF
        
        [ -n "$mqtt1_port" ] && echo "MCTOMQTT_MQTT1_PORT=$mqtt1_port" >> "$env_local"
        [ -n "$mqtt1_transport" ] && echo "MCTOMQTT_MQTT1_TRANSPORT=$mqtt1_transport" >> "$env_local"
        
        if [ "$mqtt1_use_auth" = "true" ]; then
            cat >> "$env_local" << EOF
MCTOMQTT_MQTT1_USE_AUTH_TOKEN=true
MCTOMQTT_MQTT1_TOKEN_AUDIENCE=$mqtt1_server
EOF
        elif [ -n "$mqtt1_username" ]; then
            cat >> "$env_local" << EOF
MCTOMQTT_MQTT1_USERNAME=$mqtt1_username
MCTOMQTT_MQTT1_PASSWORD=$mqtt1_password
EOF
        fi
    fi
    
    echo ""
    print_success "Migration complete! Review the generated .env.local:"
    echo ""
    cat "$env_local"
    echo ""
    
    if prompt_yes_no "Does this look correct?" "y"; then
        print_success "Using migrated configuration"
        if prompt_yes_no "Archive old config.ini?" "y"; then
            mv "$config_ini" "$config_ini.archived-$(date +%Y%m%d-%H%M%S)"
            print_success "config.ini archived"
        fi
        return 0
    else
        print_warning "Migration didn't work as expected"
        rm -f "$env_local"
        if prompt_yes_no "Continue with new configuration setup?" "y"; then
            return 0
        else
            exit 1
        fi
    fi
}

# Check for old installations
check_old_installation() {
    # Try to migrate config.ini if it exists
    migrate_config_ini
    
    # Only check for old systemd service - simple and non-blocking
    if [ -f /etc/systemd/system/mctomqtt.service ]; then
        local working_dir=$(grep "WorkingDirectory=" /etc/systemd/system/mctomqtt.service 2>/dev/null | cut -d'=' -f2)
        
        if [ -n "$working_dir" ] && [ "$working_dir" != "$HOME/.meshcoretomqtt" ]; then
            echo ""
            print_warning "Old mctomqtt systemd service detected at: $working_dir"
            echo ""
            
            if prompt_yes_no "Would you like to stop and remove the old service?" "y"; then
                if sudo systemctl stop mctomqtt.service && sudo systemctl disable mctomqtt.service && sudo rm -f /etc/systemd/system/mctomqtt.service && sudo systemctl daemon-reload; then
                    print_success "Old service removed"
                else
                    print_error "Failed to remove old service - please remove manually"
                fi
            else
                print_warning "Old service left in place - may conflict with new installation"
            fi
            echo ""
        fi
    fi
    
    # Check for launchd on macOS
    if [ "$(uname)" = "Darwin" ]; then
        local plist_file="$HOME/Library/LaunchAgents/com.meshcore.mctomqtt.plist"
        if [ -f "$plist_file" ] && ! grep -q "$HOME/.meshcoretomqtt" "$plist_file" 2>/dev/null; then
            echo ""
            print_warning "Old mctomqtt launchd service detected"
            echo ""
            
            if prompt_yes_no "Would you like to unload and remove the old service?" "y"; then
                launchctl unload "$plist_file" 2>/dev/null || true
                rm -f "$plist_file"
                print_success "Old service removed"
            else
                print_warning "Old service left in place - may conflict with new installation"
            fi
            echo ""
        fi
    fi
}

# Main installation function
main() {
    print_header "MeshCore to MQTT Installer v${SCRIPT_VERSION}"
    
    echo "This installer will help you set up MeshCore to MQTT relay."
    echo ""
    
    # Check for old installations and offer to clean up
    check_old_installation
    
    # Determine installation directory
    DEFAULT_INSTALL_DIR="$HOME/.meshcoretomqtt"
    INSTALL_DIR=$(prompt_input "Installation directory" "$DEFAULT_INSTALL_DIR")
    INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"  # Expand tilde
    
    print_info "Installation directory: $INSTALL_DIR"
    
    # Check if directory exists
    UPDATING_EXISTING=false
    if [ -d "$INSTALL_DIR" ]; then
        if [ "$UPDATE_MODE" = true ]; then
            print_info "Update mode - updating existing installation..."
            UPDATING_EXISTING=true
        elif prompt_yes_no "Directory already exists. Reinstall/update?" "y"; then
            print_info "Updating existing installation..."
            UPDATING_EXISTING=true
        else
            print_error "Installation cancelled."
            exit 1
        fi
    fi
    
    # Create installation directory
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    
    # Download or copy files
    print_header "Installing Files"
    
    if [ -n "${LOCAL_INSTALL}" ]; then
        # Local install for testing
        print_info "Installing from local directory: ${LOCAL_INSTALL}"
        cp "${LOCAL_INSTALL}/mctomqtt.py" "$INSTALL_DIR/"
        cp "${LOCAL_INSTALL}/auth_token.py" "$INSTALL_DIR/"
        cp "${LOCAL_INSTALL}/.env" "$INSTALL_DIR/"
        cp "${LOCAL_INSTALL}/uninstall.sh" "$INSTALL_DIR/"
        if [ -f "${LOCAL_INSTALL}/.env.local" ]; then
            print_warning ".env.local found in source - copying as .env.local.example"
            cp "${LOCAL_INSTALL}/.env.local" "$INSTALL_DIR/.env.local.example"
        fi
        chmod +x "$INSTALL_DIR/mctomqtt.py"
        chmod +x "$INSTALL_DIR/uninstall.sh"
        print_success "Files copied from local directory"
    else
        # Download from GitHub
        print_info "Downloading from GitHub ($REPO @ $BRANCH)..."
        BASE_URL="https://raw.githubusercontent.com/$REPO/$BRANCH"
        TMP_DIR=$(mktemp -d)
        trap "rm -rf $TMP_DIR" EXIT
        
        # Download all files
        download_file "$BASE_URL/mctomqtt.py" "$TMP_DIR/mctomqtt.py" "mctomqtt.py" || exit 1
        download_file "$BASE_URL/auth_token.py" "$TMP_DIR/auth_token.py" "auth_token.py" || exit 1
        download_file "$BASE_URL/.env" "$TMP_DIR/.env" ".env" || exit 1
        download_file "$BASE_URL/uninstall.sh" "$TMP_DIR/uninstall.sh" "uninstall.sh" || exit 1
        
        # Verify and install
        print_info "Verifying Python syntax..."
        python3 -m py_compile "$TMP_DIR/mctomqtt.py" 2>/dev/null || { print_error "Syntax errors in mctomqtt.py"; exit 1; }
        
        # Move files (including hidden files like .env)
        mv "$TMP_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
        mv "$TMP_DIR"/.env "$INSTALL_DIR/" 2>/dev/null || true
        chmod +x "$INSTALL_DIR/mctomqtt.py" "$INSTALL_DIR/uninstall.sh"
        print_success "Files downloaded and verified"
    fi
    
    # Determine installation method first
    INSTALL_METHOD=""
    EXISTING_INSTALL_TYPE=""
    
    if [ "$UPDATING_EXISTING" = true ]; then
        # For updates, detect existing installation type
        EXISTING_INSTALL_TYPE=$(detect_system_type)
        
        # Skip dependency installation for Docker updates
        if [ "$EXISTING_INSTALL_TYPE" = "docker" ]; then
            DECODER_AVAILABLE=true
            INSTALL_METHOD="2"
        fi
    else
        # For new installations, prompt for method
        print_header "Installation Method"
        echo ""
        print_info "Choose installation method:"
        echo "  1) System service (systemd/launchd) - installs Python dependencies on host"
        echo "  2) Docker container - all dependencies in container (requires docker to be installed)"
        echo "  3) Manual run only (install files, no auto-start)"
        echo ""
        INSTALL_METHOD=$(prompt_input "Choose installation method [1-3]" "1")
    fi
    
    # Docker containers include meshcore-decoder by default
    if [ "$INSTALL_METHOD" = "2" ]; then
        DECODER_AVAILABLE=true
    else
        # Only install host dependencies if NOT using Docker
        # Check Python
        print_header "Checking Dependencies"
        
        if ! command -v python3 &> /dev/null; then
            print_error "Python 3 is not installed. Please install Python 3 and try again."
            exit 1
        fi
        print_success "Python 3 found: $(python3 --version)"
        
        # Set up virtual environment
        print_info "Setting up Python virtual environment..."
        if [ ! -d "$INSTALL_DIR/venv" ]; then
            python3 -m venv "$INSTALL_DIR/venv"
            print_success "Virtual environment created"
        else
            print_success "Using existing virtual environment"
        fi
        
        # Install Python dependencies
        print_info "Installing Python dependencies..."
        source "$INSTALL_DIR/venv/bin/activate"
        pip install --quiet --upgrade pip
        pip install --quiet pyserial paho-mqtt
        print_success "Python dependencies installed"
        
        # Check for meshcore-decoder (optional)
        DECODER_AVAILABLE=false
        if command -v meshcore-decoder &> /dev/null; then
            print_success "meshcore-decoder found: $(which meshcore-decoder)"
            DECODER_AVAILABLE=true
        elif prompt_yes_no "Install meshcore-decoder for auth token support?" "y"; then
            print_info "Installing nvm and Node.js..."
            export NVM_DIR="$HOME/.nvm"
            [ -s "$NVM_DIR/nvm.sh" ] || curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
            . "$NVM_DIR/nvm.sh" && nvm install --lts && nvm use --lts
            npm install -g @michaelhart/meshcore-decoder && DECODER_AVAILABLE=true
            [ "$DECODER_AVAILABLE" = true ] && print_success "meshcore-decoder installed" || print_warning "May require shell restart"
        fi
    fi
    
    # Configuration
    print_header "Configuration"
    
    # Check if config URL was provided
    if [ -n "$CONFIG_URL" ]; then
        print_info "Downloading configuration from: $CONFIG_URL"
        if curl -fsSL "$CONFIG_URL" -o "$INSTALL_DIR/.env.local"; then
            print_success "Configuration downloaded successfully"
            
            # Show what was downloaded
            echo ""
            print_info "Downloaded configuration:"
            cat "$INSTALL_DIR/.env.local" | grep -v '^#' | grep -v '^$' | head -20
            if [ $(cat "$INSTALL_DIR/.env.local" | grep -v '^#' | grep -v '^$' | wc -l) -gt 20 ]; then
                echo "..."
            fi
            echo ""
            
            if prompt_yes_no "Use this configuration?" "y"; then
                print_success "Using downloaded configuration"
                
                # Prompt for IATA
                EXISTING_IATA=$(grep "^MCTOMQTT_IATA=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2)
                IATA=$(prompt_iata "$EXISTING_IATA")
                
                if grep -q "^MCTOMQTT_IATA=" "$INSTALL_DIR/.env.local"; then
                    sed -i.bak "s/^MCTOMQTT_IATA=.*/MCTOMQTT_IATA=$IATA/" "$INSTALL_DIR/.env.local" && rm -f "$INSTALL_DIR/.env.local.bak"
                else
                    echo "MCTOMQTT_IATA=$IATA" >> "$INSTALL_DIR/.env.local"
                fi
                print_success "IATA code set to: $IATA"
                
                # Check if MQTT brokers are already configured and offer additional brokers
                if grep -q "^MCTOMQTT_MQTT1_ENABLED=true" "$INSTALL_DIR/.env.local" 2>/dev/null; then
                    MQTT1_SERVER=$(grep "^MCTOMQTT_MQTT1_SERVER=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2)
                    MQTT2_SERVER=$(grep "^MCTOMQTT_MQTT2_SERVER=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2)
                    echo ""
                    if [ -n "$MQTT2_SERVER" ]; then
                        print_success "MQTT Brokers already configured: $MQTT1_SERVER, $MQTT2_SERVER"
                    else
                        print_success "MQTT Broker 1 already configured: $MQTT1_SERVER"
                    fi
                    
                    if prompt_yes_no "Would you like to configure additional MQTT brokers?" "n"; then
                        configure_additional_brokers
                    fi
                else
                    # No MQTT configured, offer options
                    configure_mqtt_brokers
                fi
            else
                rm -f "$INSTALL_DIR/.env.local"
                configure_mqtt_brokers
            fi
        else
            print_error "Failed to download configuration from URL"
            if prompt_yes_no "Continue with interactive configuration?" "y"; then
                configure_mqtt_brokers
            else
                exit 1
            fi
        fi
    elif [ "$UPDATING_EXISTING" = true ] && [ -f "$INSTALL_DIR/.env.local" ]; then
        if [ "$UPDATE_MODE" = true ]; then
            print_info "Keeping existing configuration"
        elif prompt_yes_no "Existing configuration found. Reconfigure?" "n"; then
            # Back up existing config before reconfiguring
            cp "$INSTALL_DIR/.env.local" "$INSTALL_DIR/.env.local.backup-$(date +%Y%m%d-%H%M%S)"
            rm -f "$INSTALL_DIR/.env.local"
            configure_mqtt_brokers
        else
            print_info "Keeping existing configuration"
            
            # Check for auth token brokers and show current owner info
            local auth_token_brokers=()
            for broker_num in {1..4}; do
                if grep -q "^MCTOMQTT_MQTT${broker_num}_USE_AUTH_TOKEN=true" "$INSTALL_DIR/.env.local" 2>/dev/null; then
                    auth_token_brokers+=($broker_num)
                fi
            done
            
            if [ ${#auth_token_brokers[@]} -gt 0 ]; then
                echo ""
                print_info "Current owner information for auth token brokers:"
                for broker_num in "${auth_token_brokers[@]}"; do
                    local server=$(grep "^MCTOMQTT_MQTT${broker_num}_SERVER=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2)
                    local existing_owner=$(grep "^MCTOMQTT_MQTT${broker_num}_TOKEN_OWNER=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2)
                    local existing_email=$(grep "^MCTOMQTT_MQTT${broker_num}_TOKEN_EMAIL=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2)
                    echo "  MQTT${broker_num} ($server):"
                    if [ -n "$existing_owner" ]; then
                        echo "    Owner: $existing_owner"
                    else
                        echo "    Owner: (not set)"
                    fi
                    if [ -n "$existing_email" ]; then
                        echo "    Email: $existing_email"
                    else
                        echo "    Email: (not set)"
                    fi
                done
                echo ""
                
                # Offer to update owner information
                if prompt_yes_no "Update owner information for auth token brokers?" "n"; then
                    update_owner_info
                fi
            fi
        fi
    elif [ ! -f "$INSTALL_DIR/.env.local" ]; then
        configure_mqtt_brokers
    fi
    
    # Create version info file
    create_version_info
    
    # Service installation/update
    if [ "$UPDATING_EXISTING" = true ]; then
        print_header "Service Restart"
        
        # Detect existing service type and restart
        SYSTEM_TYPE=$(detect_system_type)
        print_info "Detected existing installation type: $SYSTEM_TYPE"
        
        case "$SYSTEM_TYPE" in
            docker)
                if [ "$UPDATE_MODE" = true ] || prompt_yes_no "Rebuild and restart Docker container?" "y"; then
                    # Detect docker command
                    DOCKER=$(docker_cmd) || DOCKER="docker"
                    
                    # Download latest Dockerfile
                    print_info "Downloading latest Dockerfile..."
                    BASE_URL="https://raw.githubusercontent.com/$REPO/$BRANCH"
                    if ! curl -fsSL "$BASE_URL/Dockerfile" -o "$INSTALL_DIR/Dockerfile"; then
                        print_error "Failed to download Dockerfile"
                    fi
                    
                    # Rebuild Docker image
                    print_info "Rebuilding Docker image..."
                    if [ -f "$INSTALL_DIR/Dockerfile" ]; then
                        echo ""
                        if $DOCKER build -t mctomqtt:latest "$INSTALL_DIR"; then
                            print_success "Docker image rebuilt"
                        else
                            print_error "Failed to rebuild Docker image"
                        fi
                        echo ""
                    fi
                    
                    # Restart container
                    if $DOCKER ps -a | grep -q mctomqtt; then
                        print_info "Restarting container..."
                        $DOCKER stop mctomqtt 2>/dev/null || true
                        $DOCKER rm mctomqtt 2>/dev/null || true
                        
                        # Recreate container
                        SERIAL_DEVICE=$(grep "^MCTOMQTT_SERIAL_PORTS=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2 | cut -d',' -f1)
                        SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyACM0}"
                        DOCKER_RUN_CMD="$DOCKER run -d --name mctomqtt --restart unless-stopped -v $INSTALL_DIR/.env.local:/opt/.env.local"
                        if [ -e "$SERIAL_DEVICE" ]; then
                            DOCKER_RUN_CMD="$DOCKER_RUN_CMD --device=$SERIAL_DEVICE"
                        fi
                        DOCKER_RUN_CMD="$DOCKER_RUN_CMD mctomqtt:latest"
                        
                        if eval "$DOCKER_RUN_CMD"; then
                            check_service_health "docker"
                            DOCKER_INSTALLED=true
                        fi
                    fi
                fi
                ;;
            systemd)
                if systemctl is-active --quiet mctomqtt.service 2>/dev/null; then
                    if [ "$UPDATE_MODE" = true ] || prompt_yes_no "Restart systemd service?" "y"; then
                        sudo systemctl restart mctomqtt.service
                        check_service_health "systemd"
                    fi
                    SERVICE_INSTALLED=true
                fi
                ;;
            launchd)
                if launchctl list | grep -q com.meshcore.mctomqtt 2>/dev/null; then
                    if [ "$UPDATE_MODE" = true ] || prompt_yes_no "Restart launchd service?" "y"; then
                        launchctl stop com.meshcore.mctomqtt 2>/dev/null || true
                        sleep 2
                        launchctl start com.meshcore.mctomqtt 2>/dev/null || true
                        check_service_health "launchd"
                    fi
                    SERVICE_INSTALLED=true
                fi
                ;;
            *)
                print_info "No existing service found - you can install one now"
                if prompt_yes_no "Install service?" "n"; then
                    # Fall through to new installation below
                    UPDATING_EXISTING=false
                fi
                ;;
        esac
    fi
    
    # New service installation (only if not updating or no service found)
    if [ "$UPDATING_EXISTING" = false ]; then
        print_header "Service Installation"
        
        case "$INSTALL_METHOD" in
            1)
                SYSTEM_TYPE=$(detect_system_type_native)
                print_info "Detected system type: $SYSTEM_TYPE"
                install_service "$SYSTEM_TYPE"
                ;;
            2)
                install_docker
                ;;
            3)
                print_info "Skipping service installation"
                print_info "To run manually: cd $INSTALL_DIR && ./venv/bin/python3 mctomqtt.py"
                SERVICE_INSTALLED=false
                
                # Save installation type marker
                echo "manual" > "$INSTALL_DIR/.install_type"
                ;;
            *)
                print_warning "Invalid selection, skipping service installation"
                print_info "To run manually: cd $INSTALL_DIR && ./venv/bin/python3 mctomqtt.py"
                SERVICE_INSTALLED=false
                ;;
        esac
    fi
    
    # Final summary
    print_header "Installation Complete!"
    echo "Installation directory: $INSTALL_DIR"
    echo ""
    echo "Configuration file: $INSTALL_DIR/.env.local"
    echo ""
    
    if [ "$DOCKER_INSTALLED" = true ]; then
        echo "Docker container management:"
        echo "  Start:   docker start mctomqtt"
        echo "  Stop:    docker stop mctomqtt"
        echo "  Status:  docker ps -a | grep mctomqtt"
        echo "  Logs:    docker logs -f mctomqtt"
        echo "  Restart: docker restart mctomqtt"
    elif [ "$SERVICE_INSTALLED" = true ]; then
        case "$SYSTEM_TYPE" in
            systemd)
                echo "Service management:"
                echo "  Start:   sudo systemctl start mctomqtt"
                echo "  Stop:    sudo systemctl stop mctomqtt"
                echo "  Status:  sudo systemctl status mctomqtt"
                echo "  Logs:    sudo journalctl -u mctomqtt -f"
                ;;
            launchd)
                echo "Service management:"
                echo "  Start:   launchctl start com.meshcore.mctomqtt"
                echo "  Stop:    launchctl stop com.meshcore.mctomqtt"
                echo "  Status:  launchctl list | grep mctomqtt"
                echo "  Logs:    tail -f ~/Library/Logs/mctomqtt.log"
                ;;
        esac
    else
        echo "Manual run: cd $INSTALL_DIR && ./venv/bin/python3 mctomqtt.py"
    fi
    
    echo ""
    print_success "Installation complete!"
}

# Detect system type (checks for installation marker file first)
detect_system_type() {
    # Check for installation type marker file
    if [ -f "$INSTALL_DIR/.install_type" ]; then
        cat "$INSTALL_DIR/.install_type"
        return 0
    fi
    
    # Fallback: try to detect from running services (for legacy installations)
    DOCKER=$(docker_cmd 2>/dev/null) || DOCKER="docker"
    if $DOCKER ps -a 2>/dev/null | grep -q mctomqtt; then
        echo "docker"
    elif systemctl is-active --quiet mctomqtt.service 2>/dev/null || [ -f /etc/systemd/system/mctomqtt.service ]; then
        echo "systemd"
    elif launchctl list 2>/dev/null | grep -q com.meshcore.mctomqtt || [ -f "$HOME/Library/LaunchAgents/com.meshcore.mctomqtt.plist" ]; then
        echo "launchd"
    elif command -v systemctl &> /dev/null; then
        echo "systemd"
    elif [ "$(uname)" = "Darwin" ]; then
        echo "launchd"
    else
        echo "unknown"
    fi
}

# Detect native system type (ignores existing Docker/services)
detect_system_type_native() {
    if command -v systemctl &> /dev/null; then
        echo "systemd"
    elif [ "$(uname)" = "Darwin" ]; then
        echo "launchd"
    else
        echo "unknown"
    fi
}

# Install service
install_service() {
    local system_type="$1"
    
    case "$system_type" in
        systemd)
            install_systemd_service
            ;;
        launchd)
            install_launchd_service
            ;;
        *)
            print_error "Unsupported system type: $system_type"
            print_info "You'll need to manually configure the service"
            SERVICE_INSTALLED=false
            return 1
            ;;
    esac
}

# Install systemd service (Linux)
install_systemd_service() {
    print_info "Installing systemd service..."
    
    # Check if service already exists (update scenario)
    local service_exists=false
    local service_was_enabled=false
    local service_was_running=false
    
    if [ -f /etc/systemd/system/mctomqtt.service ]; then
        service_exists=true
        print_info "Existing service detected - will update"
        
        # Check if service was enabled
        if sudo systemctl is-enabled mctomqtt.service &>/dev/null; then
            service_was_enabled=true
        fi
        
        # Check if service was running
        if sudo systemctl is-active mctomqtt.service &>/dev/null; then
            service_was_running=true
            print_info "Stopping running service..."
            sudo systemctl stop mctomqtt.service
        fi
    fi
    
    local service_file="/tmp/mctomqtt.service"
    local current_user=$(whoami)
    
    # Build PATH with meshcore-decoder if available
    local service_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if command -v meshcore-decoder &> /dev/null; then
        local decoder_dir=$(dirname "$(which meshcore-decoder)")
        service_path="${decoder_dir}:${service_path}"
    fi
    
    cat > "$service_file" << EOF
[Unit]
Description=MeshCore to MQTT Relay
After=time-sync.target network.target
Wants=time-sync.target

[Service]
User=$current_user
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$service_path"
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/mctomqtt.py
KillMode=process
Restart=always
RestartSec=10
Type=exec

[Install]
WantedBy=multi-user.target
EOF
    
    print_info "Service file created. Installing (requires sudo)..."
    
    if sudo cp "$service_file" /etc/systemd/system/mctomqtt.service; then
        sudo systemctl daemon-reload
        
        # If updating, preserve previous state
        if [ "$service_exists" = true ]; then
            if [ "$service_was_enabled" = true ]; then
                sudo systemctl enable mctomqtt.service
                print_success "Service re-enabled"
            fi
            
            if [ "$service_was_running" = true ]; then
                print_info "Restarting service..."
                sudo systemctl start mctomqtt.service
                check_service_health "systemd"
            fi
            
            print_success "Systemd service updated"
        else
            # New installation - prompt user
            if prompt_yes_no "Enable service to start on boot?" "y"; then
                sudo systemctl enable mctomqtt.service
                print_success "Service enabled"
            fi
            
            if prompt_yes_no "Start service now?" "y"; then
                sudo systemctl start mctomqtt.service
                check_service_health "systemd"
            fi
            
            print_success "Systemd service installed"
        fi
        
        SERVICE_INSTALLED=true
        
        # Save installation type marker
        echo "systemd" > "$INSTALL_DIR/.install_type"
    else
        print_error "Failed to install service (sudo required)"
        SERVICE_INSTALLED=false
    fi
    
    rm -f "$service_file"
}

# Install launchd service (macOS)
install_launchd_service() {
    print_info "Installing launchd service..."
    
    local plist_file="$HOME/Library/LaunchAgents/com.meshcore.mctomqtt.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    
    # Build PATH with meshcore-decoder if available
    local service_path="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    if command -v meshcore-decoder &> /dev/null; then
        local decoder_dir=$(dirname "$(which meshcore-decoder)")
        service_path="${decoder_dir}:${service_path}"
    fi
    
    cat > "$plist_file" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.meshcore.mctomqtt</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/venv/bin/python3</string>
        <string>$INSTALL_DIR/mctomqtt.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$service_path</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/mctomqtt.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/mctomqtt-error.log</string>
</dict>
</plist>
EOF
    
    if prompt_yes_no "Load service now?" "y"; then
        launchctl load "$plist_file"
        print_success "Service loaded"
    fi
    
    SERVICE_INSTALLED=true
    print_success "Launchd service installed"
    
    # Save installation type marker
    echo "launchd" > "$INSTALL_DIR/.install_type"
}

# Install Docker container
install_docker() {
    print_info "Setting up Docker installation..."
    
    # Check if Docker is available
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed. Please install Docker first:"
        echo "  macOS: https://docs.docker.com/desktop/install/mac-install/"
        echo "  Linux: https://docs.docker.com/engine/install/"
        return 1
    fi
    
    # Detect docker command (with or without sudo)
    DOCKER=$(docker_cmd)
    if [ $? -ne 0 ]; then
        print_error "Docker daemon is not running. Please start Docker and try again."
        return 1
    fi
    
    print_success "Docker found: $($DOCKER --version)"
    
    # Build Docker image
    print_header "Building Docker Image"
    
    # Create Dockerfile in install directory if not present
    if [ ! -f "$INSTALL_DIR/Dockerfile" ]; then
        print_info "Downloading Dockerfile..."
        BASE_URL="https://raw.githubusercontent.com/$REPO/$BRANCH"
        if ! curl -fsSL "$BASE_URL/Dockerfile" -o "$INSTALL_DIR/Dockerfile"; then
            print_error "Failed to download Dockerfile"
            return 1
        fi
    fi
    
    print_info "Building mctomqtt:latest image..."
    echo ""
    if $DOCKER build -t mctomqtt:latest "$INSTALL_DIR"; then
        print_success "Docker image built successfully"
    else
        print_error "Failed to build Docker image"
        return 1
    fi
    echo ""
    
    # Get serial device from .env.local
    SERIAL_DEVICE=$(grep "^MCTOMQTT_SERIAL_PORTS=" "$INSTALL_DIR/.env.local" 2>/dev/null | cut -d'=' -f2 | cut -d',' -f1)
    SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyACM0}"
    
    # Generate docker run command
    DOCKER_RUN_CMD="$DOCKER run -d --name mctomqtt --restart unless-stopped -v $INSTALL_DIR/.env.local:/opt/.env.local"
    
    # Add device mapping if device exists
    if [ -e "$SERIAL_DEVICE" ]; then
        DOCKER_RUN_CMD="$DOCKER_RUN_CMD --device=$SERIAL_DEVICE"
    else
        print_warning "Serial device $SERIAL_DEVICE not found - container will start but may not connect"
    fi
    
    DOCKER_RUN_CMD="$DOCKER_RUN_CMD mctomqtt:latest"
    
    echo ""
    print_info "Docker run command:"
    echo "  $DOCKER_RUN_CMD"
    echo ""
    
    if prompt_yes_no "Start Docker container now?" "y"; then
        # Remove existing container if present
        if $DOCKER ps -a | grep -q mctomqtt; then
            print_info "Removing existing mctomqtt container..."
            $DOCKER rm -f mctomqtt &> /dev/null || true
        fi
        
        # Start container
        if eval "$DOCKER_RUN_CMD"; then
            print_success "Docker container started"
            check_service_health "docker"
        else
            print_error "Failed to start Docker container"
            return 1
        fi
    fi
    
    DOCKER_INSTALLED=true
    SERVICE_INSTALLED=false
    
    # Save installation type marker
    echo "docker" > "$INSTALL_DIR/.install_type"
}

# Create version info file with installer version and git hash
create_version_info() {
    local git_hash="unknown"
    local git_branch="${BRANCH}"
    local git_repo="${REPO}"
    
    # Try to resolve the branch/tag to a specific commit hash via GitHub API
    if command -v curl >/dev/null 2>&1; then
        # Try to get commit SHA from GitHub API
        local api_url="https://api.github.com/repos/${git_repo}/commits/${git_branch}"
        git_hash=$(curl -fsSL "$api_url" 2>/dev/null | grep -m1 '"sha"' | cut -d'"' -f4 | head -c7)
        [ -z "$git_hash" ] && git_hash="unknown"
    fi
    
    # Create version info JSON file
    cat > "$INSTALL_DIR/.version_info" <<EOF
{
  "installer_version": "${SCRIPT_VERSION}",
  "git_hash": "${git_hash}",
  "git_branch": "${git_branch}",
  "git_repo": "${git_repo}",
  "install_date": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF
    
    print_info "Version info saved: ${SCRIPT_VERSION}-${git_hash} (${git_repo}@${git_branch})"
}

# Run main
main "$@"
