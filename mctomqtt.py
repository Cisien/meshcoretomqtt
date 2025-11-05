#!/usr/bin/env python3
import sys
import os
import json
import serial
import threading
import argparse
import re
import time
import calendar
import logging
import random
from datetime import datetime
from time import sleep
from auth_token import create_auth_token, read_private_key_file

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not installed. Install with:")
    print("pip install paho-mqtt")
    sys.exit(1)

def load_env_files():
    """Load environment variables from .env and .env.local files"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(script_dir, '.env')
    env_local_file = os.path.join(script_dir, '.env.local')
    
    def parse_env_file(filepath):
        """Parse a .env file and return a dictionary"""
        env_vars = {}
        if not os.path.exists(filepath):
            return env_vars
        
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith('#'):
                    continue
                # Parse KEY=VALUE
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    # Remove quotes if present
                    if value and value[0] in ('"', "'") and value[-1] == value[0]:
                        value = value[1:-1]
                    env_vars[key] = value
        return env_vars
    
    # Load .env first (defaults)
    env_vars = parse_env_file(env_file)
    
    # Load .env.local (overrides)
    local_vars = parse_env_file(env_local_file)
    env_vars.update(local_vars)
    
    # Set environment variables
    for key, value in env_vars.items():
        if key not in os.environ:
            os.environ[key] = value
    
    return env_vars

def log_config_sources():
    """Log configuration file sources and contents"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(script_dir, '.env')
    env_local_file = os.path.join(script_dir, '.env.local')
    
    logger.info(f"Config directory: {script_dir}")
    logger.debug(f".env file: {env_file} (exists: {os.path.exists(env_file)})")
    logger.debug(f".env.local file: {env_local_file} (exists: {os.path.exists(env_local_file)})")
    
    if not os.path.exists(env_local_file):
        logger.warning(".env.local file not found - using defaults from .env only")
    else:
        logger.debug("=== .env.local configuration ===")
        try:
            with open(env_local_file, 'r') as f:
                for line in f:
                    line = line.rstrip()
                    if line and not line.startswith('#'):
                        logger.debug(f"  {line}")
        except Exception as e:
            logger.error(f"Error reading .env.local: {e}")
        logger.debug("================================")

# Load environment configuration
load_env_files()

# Regex patterns for message parsing
RAW_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U RAW: (.*)")
PACKET_PATTERN = re.compile(
    r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U: (RX|TX), len=(\d+) \(type=(\d+), route=([A-Z]), payload_len=(\d+)\)"
    r"(?: SNR=(-?\d+) RSSI=(-?\d+) score=(\d+)( time=(\d+))? hash=([0-9A-F]+)(?: \[(.*)\])?)?"
)

# Initialize logging (console only)
log_level_str = os.getenv('MCTOMQTT_LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

class MeshCoreBridge:
    last_raw: bytes = None

    def __init__(self, debug=False):
        self.debug = debug
        self.repeater_name = None
        self.repeater_pub_key = None
        self.repeater_priv_key = None
        self.radio_info = None
        self.firmware_version = None
        self.model = None
        self.client_version = self._load_client_version()
        self.ser = None
        self.ser_lock = threading.Lock()  # Lock for thread-safe serial access
        self.mqtt_clients = []
        self.mqtt_connected = False
        self.connection_events = {}  # Track connection completion per broker
        self.should_exit = False
        self.global_iata = os.getenv('MCTOMQTT_IATA', 'XXX')
        self.reconnect_delay = 1.0  # Start with 1 second
        self.max_reconnect_delay = 120.0  # Max 2 minutes
        self.reconnect_backoff = 1.5  # Exponential backoff multiplier
        self.reconnect_attempts = {}  # Track consecutive failed reconnect attempts per broker
        self.max_reconnect_attempts = 12  # Exit after this many consecutive failures
        self.token_cache = {}  # Cache tokens with their creation time
        self.token_ttl = 3600  # 1 hour token TTL
        self.ws_ping_threads = {}  # Track WebSocket ping threads per broker
        
        # Statistics tracking
        self.stats = {
            'start_time': time.time(),
            'packets_rx': 0,
            'packets_tx': 0,
            'packets_rx_prev': 0,
            'packets_tx_prev': 0,
            'bytes_processed': 0,
            'publish_failures': 0,
            'last_stats_log': time.time(),
            'reconnects': {},  # {broker_num: [timestamp1, timestamp2, ...]}
            'device': {},  # Device stats from serial (battery, uptime, errors, etc.)
            'device_prev': {}  # Previous device stats for delta calculation
        }
        
        logger.info("Configuration loaded from environment variables")
    
    def _load_client_version(self):
        """Load client version from .version_info file"""
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            version_file = os.path.join(script_dir, '.version_info')
            if os.path.exists(version_file):
                with open(version_file, 'r') as f:
                    version_data = json.load(f)
                    installer_ver = version_data.get('installer_version', 'unknown')
                    git_hash = version_data.get('git_hash', 'unknown')
                    return f"meshcoretomqtt/{installer_ver}-{git_hash}"
        except Exception as e:
            logger.debug(f"Could not load version info: {e}")
        return "meshcoretomqtt/unknown"
    
    def get_env(self, key, fallback=''):
        """Get environment variable with fallback (all vars are MCTOMQTT_ prefixed)"""
        return os.getenv(f"MCTOMQTT_{key}", fallback)
    
    def get_env_bool(self, key, fallback=False):
        """Get boolean environment variable, checking MCTOMQTT_ prefix first"""
        value = self.get_env(key, str(fallback)).lower()
        return value in ('true', '1', 'yes', 'on')
    
    def get_env_int(self, key, fallback=0):
        """Get integer environment variable, checking MCTOMQTT_ prefix first"""
        try:
            return int(self.get_env(key, str(fallback)))
        except ValueError:
            return fallback
    
    def resolve_topic_template(self, template, broker_num=None):
        """Resolve topic template with {IATA} and {PUBLIC_KEY} placeholders"""
        if not template:
            return template
        
        # Get IATA - broker-specific or global
        iata = self.global_iata
        if broker_num:
            broker_iata = self.get_env(f'MQTT{broker_num}_IATA', '')
            if broker_iata:
                iata = broker_iata
        
        # Replace template variables
        resolved = template.replace('{IATA}', iata)
        resolved = resolved.replace('{PUBLIC_KEY}', self.repeater_pub_key if self.repeater_pub_key else 'UNKNOWN')
        return resolved
    
    def get_topic(self, topic_type, broker_num=None):
        """Get topic with template resolution, checking broker-specific override first"""
        topic_type_upper = topic_type.upper()
        
        # Check broker-specific topic override
        if broker_num:
            broker_topic = self.get_env(f'MQTT{broker_num}_TOPIC_{topic_type_upper}', '')
            if broker_topic:
                return self.resolve_topic_template(broker_topic, broker_num)
        
        # Fall back to global topic
        global_topic = self.get_env(f'TOPIC_{topic_type_upper}', '')
        return self.resolve_topic_template(global_topic, broker_num)

    def sanitize_client_id(self, name):
        """Convert repeater name to valid MQTT client ID"""
        prefix = self.get_env("MQTT1_CLIENT_ID_PREFIX", "meshcore_")
        client_id = prefix + name.replace(" ", "_")
        client_id = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)
        return client_id[:23]
    
    def generate_auth_credentials(self, broker_num, force_refresh=False):
        """Generate authentication credentials for a broker on-demand"""
        use_auth_token = self.get_env_bool(f"MQTT{broker_num}_USE_AUTH_TOKEN", False)
        
        if use_auth_token:
            if not self.repeater_priv_key:
                logger.error(f"[MQTT{broker_num}] Private key not available from device for auth token")
                return None, None
            
            # Check if we have a cached token that's still fresh
            current_time = time.time()
            if not force_refresh and broker_num in self.token_cache:
                cached_token, created_at = self.token_cache[broker_num]
                age = current_time - created_at
                if age < (self.token_ttl - 300):  # Use cached token if it has >5min remaining
                    logger.debug(f"[MQTT{broker_num}] Using cached auth token (age: {age:.0f}s)")
                    username = f"v1_{self.repeater_pub_key.upper()}"
                    return username, cached_token
            
            # Generate fresh token
            try:
                username = f"v1_{self.repeater_pub_key.upper()}"
                audience = self.get_env(f"MQTT{broker_num}_TOKEN_AUDIENCE", "")
                
                # Security check: Only include email/owner if using TLS with verification
                # NEVER send email/owner over plaintext or unverified connections
                use_tls = self.get_env_bool(f"MQTT{broker_num}_USE_TLS", False)
                tls_verify = self.get_env_bool(f"MQTT{broker_num}_TLS_VERIFY", True)
                secure_connection = use_tls and tls_verify
                
                owner = self.get_env(f"MQTT{broker_num}_TOKEN_OWNER", "")
                email = self.get_env(f"MQTT{broker_num}_TOKEN_EMAIL", "")
                
                claims = {}
                if audience:
                    claims['aud'] = audience
                
                if secure_connection:
                    if owner:
                        claims['owner'] = owner
                    if email:
                        claims['email'] = email.lower()
                else:
                    if owner or email:
                        logger.debug(f"[MQTT{broker_num}] Skipping email/owner in JWT - TLS and TLS_VERIFY must both be enabled for secure transmission")
                
                claims['client'] = self.client_version
                
                # Generate token with 1 hour expiry
                password = create_auth_token(self.repeater_pub_key, self.repeater_priv_key, expiry_seconds=self.token_ttl, **claims)
                self.token_cache[broker_num] = (password, current_time)
                logger.debug(f"[MQTT{broker_num}] Generated fresh auth token (1h expiry)")
                return username, password
            except Exception as e:
                logger.error(f"[MQTT{broker_num}] Failed to generate auth token: {e}")
                return None, None
        else:
            username = self.get_env(f"MQTT{broker_num}_USERNAME", "")
            password = self.get_env(f"MQTT{broker_num}_PASSWORD", "")
            return username, password

    def connect_serial(self):
        ports = self.get_env("SERIAL_PORTS", "/dev/ttyACM0").split(",")
        baud_rate = self.get_env_int("SERIAL_BAUD_RATE", 115200)
        timeout = self.get_env_int("SERIAL_TIMEOUT", 2)

        for port in ports:
            try:
                self.ser = serial.Serial(
                    port=port,
                    baudrate=baud_rate,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    bytesize=serial.EIGHTBITS,
                    timeout=timeout,
                    rtscts=False
                )
                self.ser.write(b"\r\n\r\n")
                self.ser.flushInput()
                self.ser.flushOutput()
                logger.info(f"Connected to {port}")
                return True
            except (serial.SerialException, OSError) as e:
                logger.warning(f"Failed to connect to {port}: {str(e)}")
                continue
        logger.error("Failed to connect to any serial port")
        return False

    def set_repeater_time(self):
        self.ser.flushInput()
        self.ser.flushOutput()
        epoc_time = int(calendar.timegm(time.gmtime()))
        timecmd=f'time {epoc_time}\r\n'
        self.ser.write(timecmd.encode())
        logger.debug(f"Sent '{timecmd}' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw response: {response}")

    def get_repeater_name(self):
        if not self.ser:
            return False

        self.ser.flushInput()
        self.ser.flushOutput()
        self.ser.write(b"get name\r\n")
        logger.debug("Sent 'get name' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            name = response.split("-> >")[1].strip()            
            if '\n' in name:
                name = name.split('\n')[0]            
            name = name.replace('\r', '').strip()
            self.repeater_name = name
            logger.info(f"Repeater name: {self.repeater_name}")
            return True
        
        logger.error("Failed to get repeater name from response")
        return False

    def get_repeater_pubkey(self):
        if not self.ser:
            return False
        self.ser.flushInput()
        self.ser.flushOutput()
        self.ser.write(b"get public.key\r\n")
        logger.debug("Sent 'get public.key' command")

        sleep(1.0)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            pub_key = response.split("-> >")[1].strip()            
            if '\n' in pub_key:
                pub_key = pub_key.split('\n')[0]            
            pub_key_clean = pub_key.replace(' ', '').replace('\r', '').replace('\n', '')
            
            # Validate public key format (should be 64 hex characters)
            if not pub_key_clean or len(pub_key_clean) != 64 or not all(c in '0123456789ABCDEFabcdef' for c in pub_key_clean):
                logger.error(f"Invalid public key format: {repr(pub_key_clean)} (extracted from: {repr(pub_key)})")
                return False
            
            # Normalize to uppercase
            self.repeater_pub_key = pub_key_clean.upper()
            logger.info(f"Repeater pub key: {self.repeater_pub_key}")
            return True
        
        logger.error("Failed to get repeater pub key from response")
        return False

    def get_repeater_privkey(self):
        if not self.ser:
            return False
        
        self.ser.flushInput()
        self.ser.flushOutput()
        self.ser.write(b"get prv.key\r\n")
        logger.debug("Sent 'get prv.key' command")

        sleep(1.0)
        response = self.ser.read_all().decode(errors='replace')
        if "-> >" in response:
            priv_key = response.split("-> >")[1].strip()
            if '\n' in priv_key:
                priv_key = priv_key.split('\n')[0]

            priv_key_clean = priv_key.replace(' ', '').replace('\r', '').replace('\n', '')
            if len(priv_key_clean) == 128:
                try:
                    int(priv_key_clean, 16)  # Validate it's hex
                    self.repeater_priv_key = priv_key_clean
                    logger.info(f"Repeater priv key: {self.repeater_priv_key[:4]}... (truncated for security)")
                    return True
                except ValueError as e:
                    logger.error(f"Response not valid hex: {priv_key_clean[:32]}... Error: {e}")
            else:
                logger.error(f"Response wrong length: {len(priv_key_clean)} (expected 128)")
        
        logger.error("Failed to get repeater priv key from response - command may not be supported by firmware")
        return False

    def get_radio_info(self):
        """Query the repeater for radio information"""
        if not self.ser:
            return None

        self.ser.flushInput()
        self.ser.flushOutput()
        self.ser.write(b"get radio\r\n")
        logger.debug("Sent 'get radio' command")

        sleep(0.5)  # Adjust delay if necessary
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw radio response: {response}")

        if "-> >" in response:
            radio_info = response.split("-> >")[1].strip()
            if '\n' in radio_info:
                radio_info = radio_info.split('\n')[0]
            logger.debug(f"Parsed radio info: {radio_info}")
            return radio_info
        
        logger.error("Failed to get radio info from response")
        return None

    def get_firmware_version(self):
        """Query the repeater for firmware version"""
        if not self.ser:
            return None

        self.ser.flushInput()
        self.ser.flushOutput()
        self.ser.write(b"ver\r\n")
        logger.debug("Sent 'ver' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw version response: {response}")

        # Response format: "ver\n  -> 1.8.2-dev-834c700 (Build: 04-Sep-2025)\n"
        if "-> " in response:
            version = response.split("-> ", 1)[1]
            version = version.split('\n')[0].replace('\r', '').strip()
            logger.info(f"Firmware version: {version}")
            return version
        
        logger.warning("Failed to get firmware version from response")
        return None

    def get_board_type(self):
        """Query the repeater for board/hardware type"""
        if not self.ser:
            return None

        self.ser.flushInput()
        self.ser.flushOutput()
        self.ser.write(b"board\r\n")
        logger.debug("Sent 'board' command")

        sleep(0.5)
        response = self.ser.read_all().decode(errors='replace')
        logger.debug(f"Raw board response: {response}")

        # Response format: "board\n  -> Station G2\n"
        if "-> " in response:
            board_type = response.split("-> ", 1)[1]
            board_type = board_type.split('\n')[0].replace('\r', '').strip()
            if board_type == "Unknown command":
                board_type = "unknown"
            logger.info(f"Board type: {board_type}")
            return board_type
        
        logger.warning("Failed to get board type from response")
        return None

    def get_device_stats(self):
        """Query the repeater for device statistics (battery, uptime, errors, queue, radio stats)"""
        if not self.ser:
            return {}
        
        stats = {}
        
        with self.ser_lock:
            # Get stats-core: battery_mv, uptime_secs, errors, queue_len
            self.ser.flushInput()
            self.ser.flushOutput()
            self.ser.write(b"stats-core\r\n")
            logger.debug("Sent 'stats-core' command")
            
            sleep(0.5)
            response = self.ser.read_all().decode(errors='replace')
            logger.debug(f"Raw stats-core response: {response}")
            
            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    core_stats = json.loads(json_str)
                    if 'battery_mv' in core_stats:
                        stats['battery_mv'] = core_stats['battery_mv']
                    if 'uptime_secs' in core_stats:
                        stats['uptime_secs'] = core_stats['uptime_secs']
                    if 'errors' in core_stats:
                        stats['errors'] = core_stats['errors']
                    if 'queue_len' in core_stats:
                        stats['queue_len'] = core_stats['queue_len']
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-core: {e}")
            
            # Get stats-radio: noise_floor, tx_air_secs, rx_air_secs
            self.ser.flushInput()
            self.ser.flushOutput()
            self.ser.write(b"stats-radio\r\n")
            logger.debug("Sent 'stats-radio' command")
            
            sleep(0.5)
            response = self.ser.read_all().decode(errors='replace')
            logger.debug(f"Raw stats-radio response: {response}")
            
            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    radio_stats = json.loads(json_str)
                    if 'noise_floor' in radio_stats:
                        stats['noise_floor'] = radio_stats['noise_floor']
                    if 'tx_air_secs' in radio_stats:
                        stats['tx_air_secs'] = radio_stats['tx_air_secs']
                    if 'rx_air_secs' in radio_stats:
                        stats['rx_air_secs'] = radio_stats['rx_air_secs']
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-radio: {e}")
        
        return stats

    def _websocket_ping_loop(self, broker_num, mqtt_client, transport):
        """Send WebSocket PING frames periodically to keep connection alive"""
        if transport != "websockets":
            return
        
        ping_interval = 45  # Send WebSocket ping every 45 seconds
        
        while broker_num in self.ws_ping_threads and self.ws_ping_threads[broker_num].get('active', False):
            sleep(ping_interval)
            
            try:
                # Access the underlying WebSocket object in paho-mqtt
                if hasattr(mqtt_client, '_sock') and mqtt_client._sock:
                    sock = mqtt_client._sock
                    # Check if it's a WebSocket
                    if hasattr(sock, 'ping'):
                        sock.ping()
                        logger.debug(f"[MQTT{broker_num}] Sent WebSocket PING")
            except Exception as e:
                logger.debug(f"[MQTT{broker_num}] WebSocket PING failed: {e}")
                # Don't break the loop - connection might recover
    
    def _stats_logging_loop(self):
        """Log statistics every 5 minutes"""
        stats_interval = 300
        
        while not self.should_exit:
            sleep(stats_interval)
            
            if self.should_exit:
                break
            
            # Fetch fresh device stats from serial
            logger.debug("[STATS] Fetching fresh device stats from serial...")
            device_stats = self.get_device_stats()
            if device_stats:
                self.stats['device'] = device_stats
                logger.debug(f"[STATS] Updated device stats: {device_stats}")
                # Publish updated status with new stats
                self.publish_status("online")
            else:
                logger.debug("[STATS] No device stats received")
            
            # Calculate uptime
            uptime_seconds = int(time.time() - self.stats['start_time'])
            uptime_hours = uptime_seconds // 3600
            uptime_minutes = (uptime_seconds % 3600) // 60
            
            if uptime_hours > 0:
                uptime_str = f"{uptime_hours}h {uptime_minutes}m"
            else:
                uptime_str = f"{uptime_minutes}m"
            
            # Calculate data volume with appropriate units
            bytes_actual = self.stats['bytes_processed']
            if bytes_actual < 1024:
                data_str = f"{bytes_actual}B"
            elif bytes_actual < 1024 * 1024:
                data_str = f"{bytes_actual / 1024:.1f}KB"
            elif bytes_actual < 1024 * 1024 * 1024:
                data_str = f"{bytes_actual / (1024 * 1024):.1f}MB"
            else:
                data_str = f"{bytes_actual / (1024 * 1024 * 1024):.2f}GB"
            
            total_brokers = len(self.mqtt_clients)
            connected_brokers = sum(1 for info in self.mqtt_clients if info.get('connected', False))
            
            # Calculate packets per minute over the last interval (5 minutes)
            time_elapsed = time.time() - self.stats['last_stats_log']
            packets_rx_delta = self.stats['packets_rx'] - self.stats['packets_rx_prev']
            packets_tx_delta = self.stats['packets_tx'] - self.stats['packets_tx_prev']
            packets_per_min = ((packets_rx_delta + packets_tx_delta) / time_elapsed) * 60 if time_elapsed > 0 else 0
            
            # Store current counts for next interval
            self.stats['packets_rx_prev'] = self.stats['packets_rx']
            self.stats['packets_tx_prev'] = self.stats['packets_tx']
            
            # Prune reconnect timestamps older than 24 hours and build reconnect stats
            current_time = time.time()
            cutoff_time = current_time - 86400  # 24 hours in seconds
            reconnect_stats = []
            
            for broker_num in sorted(self.stats['reconnects'].keys()):
                # Prune old timestamps
                self.stats['reconnects'][broker_num] = [
                    ts for ts in self.stats['reconnects'][broker_num] if ts > cutoff_time
                ]
                
                # Count reconnects in last 24 hours
                reconnect_count = len(self.stats['reconnects'][broker_num])
                if reconnect_count > 0:
                    reconnect_stats.append(f"MQTT{broker_num}:{reconnect_count}")
            
            reconnect_str = ", ".join(reconnect_stats) if reconnect_stats else "none"
            
            # Log the main stats
            logger.info(
                f"[SERVICE] Uptime: {uptime_str} | "
                f"RX/TX: {self.stats['packets_rx']}/{self.stats['packets_tx']} (5m: {packets_per_min:.1f}/min) | "
                f"RX bytes: {data_str} | "
                f"MQTT: {connected_brokers}/{total_brokers} | "
                f"Reconnects/24h: {reconnect_str} | "
                f"Failures: {self.stats['publish_failures']}"
            )
            
            # Log device stats separately if available
            if self.stats['device']:
                ds = self.stats['device']
                parts = []
                
                if 'noise_floor' in ds:
                    parts.append(f"Noise: {ds['noise_floor']}dB")
                
                # Radio airtime stats with utilization (calculated over interval, not total uptime)
                if 'tx_air_secs' in ds and 'rx_air_secs' in ds and 'uptime_secs' in ds:
                    tx_secs_total = ds['tx_air_secs']
                    rx_secs_total = ds['rx_air_secs']
                    uptime_secs = ds['uptime_secs']
                    
                    # Calculate delta from previous reading
                    prev = self.stats.get('device_prev', {})
                    if prev and 'tx_air_secs' in prev and 'rx_air_secs' in prev and 'uptime_secs' in prev:
                        # Delta calculation (airtime since last reading)
                        tx_delta = tx_secs_total - prev['tx_air_secs']
                        rx_delta = rx_secs_total - prev['rx_air_secs']
                        uptime_delta = uptime_secs - prev['uptime_secs']
                        
                        if uptime_delta > 0:
                            tx_util = (tx_delta / uptime_delta) * 100
                            rx_util = (rx_delta / uptime_delta) * 100
                            parts.append(f"Air (5m): Tx {tx_delta:.1f}s ({tx_util:.2f}%), Rx {rx_delta:.1f}s ({rx_util:.2f}%)")
                        else:
                            parts.append(f"Air (5m): Tx {tx_delta:.1f}s, Rx {rx_delta:.1f}s")
                    else:
                        # Initial reading - show totals
                        parts.append(f"Air (5m): Tx {tx_secs_total}s, Rx {rx_secs_total}s")
                elif 'tx_air_secs' in ds and 'rx_air_secs' in ds:
                    parts.append(f"Air (5m): Tx {ds['tx_air_secs']}s, Rx {ds['rx_air_secs']}s")
                
                # Battery
                if 'battery_mv' in ds:
                    parts.append(f"Battery: {ds['battery_mv']}mV")
                
                # Device uptime
                if 'uptime_secs' in ds:
                    dev_uptime_secs = ds['uptime_secs']
                    dev_uptime_hours = dev_uptime_secs // 3600
                    dev_uptime_minutes = (dev_uptime_secs % 3600) // 60
                    
                    if dev_uptime_hours > 0:
                        dev_uptime_str = f"{dev_uptime_hours}h {dev_uptime_minutes}m"
                    else:
                        dev_uptime_str = f"{dev_uptime_minutes}m"
                    
                    parts.append(f"Uptime: {dev_uptime_str}")
                
                # Errors
                if 'errors' in ds:
                    parts.append(f"Errors: {ds['errors']}")
                
                # Queue
                if 'queue_len' in ds:
                    parts.append(f"Queue: {ds['queue_len']}")
                
                if parts:
                    logger.info(f"[DEVICE] {' | '.join(parts)}")
            
            # Save current device stats as previous for next interval calculation
            if self.stats['device']:
                self.stats['device_prev'] = self.stats['device'].copy()
            
            self.stats['last_stats_log'] = time.time()
    
    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        broker_name = userdata.get('name', 'unknown') if userdata else 'unknown'
        broker_num = userdata.get('broker_num', None) if userdata else None
        
        # Signal that this broker has completed its connection attempt
        if broker_num in self.connection_events:
            self.connection_events[broker_num].set()
        
        if rc == 0:
            # Reset reconnect delay on successful connection
            self.reconnect_delay = 1.0
            
            # Find the mqtt_info for this broker
            mqtt_info = None
            for info in self.mqtt_clients:
                if info['broker_num'] == broker_num:
                    mqtt_info = info
                    break
            
            if not mqtt_info:
                logger.error(f"[MQTT{broker_num}] on_connect fired but broker not in mqtt_clients list")
                return
            
            current_time = time.time()
            was_connected = mqtt_info.get('connected', False)
            is_first_connect = mqtt_info.get('connect_time', 0) == 0
            
            # Set connected state
            mqtt_info['connected'] = True
            mqtt_info['connecting_since'] = 0  # Clear connecting timestamp
            mqtt_info['connect_time'] = current_time
            
            if was_connected and not is_first_connect:
                logger.info(f"[MQTT{broker_num}] Reconnected to broker")
            elif is_first_connect:
                logger.info(f"[MQTT{broker_num}] Connected to broker")
            else:
                # was_connected=False but connect_time > 0 means we already logged this connection
                logger.debug(f"[MQTT{broker_num}] Connection state updated")
            
            # Track global connected state
            if not self.mqtt_connected:
                self.mqtt_connected = True
            
            # Publish online status
            status_topic = self.get_topic("status", broker_num)
            status_payload = json.dumps(self.build_status_message("online"))
            qos = self.get_env_int(f"MQTT{broker_num}_QOS", 0)
            retain = self.get_env_bool(f"MQTT{broker_num}_RETAIN", True)
            
            try:
                result = client.publish(status_topic, status_payload, qos=qos, retain=retain)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    # Only reset failed_attempts if we had a previous successful connection
                    # that lasted >= 120 seconds. This prevents rapid connect/disconnect cycles
                    # from resetting the counter and avoiding the max_reconnect_attempts exit.
                    # On first connection, we'll reset it after 120 seconds of stability.
                    pass  # Don't reset failed_attempts here - let it reset after 120s of stability
            except Exception as e:
                logger.error(f"[MQTT{broker_num}] Failed to publish online status: {e}")
        else:
            logger.error(f"[MQTT{broker_num}] Connection failed with code: {rc}")


    def on_mqtt_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        broker_name = userdata.get('name', 'unknown') if userdata else 'unknown'
        broker_num = userdata.get('broker_num', None) if userdata else None
        
        # Stop WebSocket ping thread for this broker (don't delete yet, reconnect might reuse broker_num)
        if broker_num in self.ws_ping_threads:
            self.ws_ping_threads[broker_num]['active'] = False
        
        # Mark this specific client as disconnected
        already_disconnected = False
        for mqtt_info in self.mqtt_clients:
            if mqtt_info['broker_num'] == broker_num:
                already_disconnected = not mqtt_info.get('connected', False)
                mqtt_info['connected'] = False
                mqtt_info['connecting_since'] = 0  # Clear connecting timestamp on disconnect
                mqtt_info['reconnect_at'] = time.time() + self.reconnect_delay
                
                # If connection was short-lived (< 120 seconds), count it as a failure
                connect_time = mqtt_info.get('connect_time', 0)
                if connect_time > 0 and (time.time() - connect_time) < 120:
                    mqtt_info['failed_attempts'] = mqtt_info.get('failed_attempts', 0) + 1
                    logger.warning(f"[MQTT{broker_num}] Short-lived connection detected (failed_attempts: {mqtt_info['failed_attempts']})")
                elif connect_time > 0:
                    # Connection lasted >= 120 seconds - this was a stable connection
                    # Reset failed_attempts counter
                    if mqtt_info.get('failed_attempts', 0) > 0:
                        logger.info(f"[MQTT{broker_num}] Stable connection ended after {int(time.time() - connect_time)}s - resetting failure counter")
                        mqtt_info['failed_attempts'] = 0
                
                break
        
        # Only log if this is the first disconnect event (avoid duplicate logs from paho-mqtt)
        if not already_disconnected:
            logger.warning(f"[MQTT{broker_num}] Disconnected (code: {reason_code}, flags: {disconnect_flags}, properties: {properties})")
            
            # Track disconnect event for stats (only if this was a connected broker)
            if mqtt_info.get('connect_time', 0) > 0:
                current_time = time.time()
                if 'reconnects' not in self.stats:
                    self.stats['reconnects'] = {}
                if broker_num not in self.stats['reconnects']:
                    self.stats['reconnects'][broker_num] = []
                self.stats['reconnects'][broker_num].append(current_time)
        
        # Check if ALL brokers are disconnected
        all_disconnected = all(not info.get('connected', False) for info in self.mqtt_clients)
        if all_disconnected:
            self.mqtt_connected = False

    def build_status_message(self, status, include_stats=True):
        """Build a status message with all required fields"""
        message = {
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "origin": self.repeater_name,
            "origin_id": self.repeater_pub_key,
            "radio": self.radio_info if self.radio_info else "unknown",
            "model": self.model if self.model else "unknown",
            "firmware_version": self.firmware_version if self.firmware_version else "unknown",
            "client_version": self.client_version
        }
        
        # Add device stats if available and requested
        if include_stats and self.stats['device']:
            message['stats'] = self.stats['device']
        
        return message
    
    def publish_status(self, status, client=None, broker_num=None):
        """Publish online status with stats (NOT retained)"""
        status_msg = self.build_status_message(status, include_stats=True)
        status_topic = self.get_topic("status", broker_num)
        
        if client:
            self.safe_publish(status_topic, json.dumps(status_msg), retain=False, client=client, broker_num=broker_num)
        else:
            self.safe_publish(status_topic, json.dumps(status_msg), retain=False)
        
        logger.debug(f"Published status: {status}")

    def safe_publish(self, topic, payload, retain=False, client=None, broker_num=None):
        """Publish to one or all MQTT brokers"""
        if not self.mqtt_connected:
            logger.warning(f"Not connected - skipping publish to {topic}")
            self.stats['publish_failures'] += 1
            return False

        success = False
        
        if client:
            clients_to_publish = [info for info in self.mqtt_clients if info['client'] == client]
        else:
            clients_to_publish = self.mqtt_clients
        
        for mqtt_client_info in clients_to_publish:
            broker_num = mqtt_client_info['broker_num']
            try:
                mqtt_client = mqtt_client_info['client']
                qos = self.get_env_int(f"MQTT{broker_num}_QOS", 0)
                if qos == 1:
                    qos = 0  # force qos=1 to 0 because qos 1 can cause retry storms
                
                result = mqtt_client.publish(topic, payload, qos=qos, retain=retain)
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    logger.error(f"[MQTT{broker_num}] Publish failed to {topic}: {mqtt.error_string(result.rc)}")
                    self.stats['publish_failures'] += 1
                else:
                    logger.debug(f"[MQTT{broker_num}] Published to {topic}")
                    success = True
            except Exception as e:
                logger.error(f"[MQTT{broker_num}] Publish error to {topic}: {str(e)}")
                self.stats['publish_failures'] += 1
        
        return success

    def _create_mqtt_client(self, broker_num):
        """
        Internal: Create and configure an MQTT client (doesn't connect).
        Separated for clarity and testing.
        """
        client_id = self.sanitize_client_id(self.repeater_pub_key)
        if broker_num > 1:
            client_id += f"_{broker_num}"
        
        transport = self.get_env(f"MQTT{broker_num}_TRANSPORT", "tcp")
        
        mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            transport=transport
        )
        
        mqtt_client.user_data_set({
            'name': f"MQTT{broker_num}",
            'broker_num': broker_num
        })
        
        # Set credentials
        username, password = self.generate_auth_credentials(broker_num)
        if username is None:
            return None
        if username:
            mqtt_client.username_pw_set(username, password)
        
        # Set LWT
        lwt_topic = self.get_topic("status", broker_num)
        lwt_payload = json.dumps(self.build_status_message("offline", include_stats=False))
        lwt_qos = self.get_env_int(f"MQTT{broker_num}_QOS", 0)
        lwt_retain = self.get_env_bool(f"MQTT{broker_num}_RETAIN", True)
        mqtt_client.will_set(lwt_topic, lwt_payload, qos=lwt_qos, retain=lwt_retain)
        
        # Set callbacks
        mqtt_client.on_connect = self.on_mqtt_connect
        mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
        # Configure TLS
        use_tls = self.get_env_bool(f"MQTT{broker_num}_USE_TLS", False)
        if use_tls:
            import ssl
            tls_verify = self.get_env_bool(f"MQTT{broker_num}_TLS_VERIFY", True)
            if tls_verify:
                mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                mqtt_client.tls_insecure_set(False)
            else:
                mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
                mqtt_client.tls_insecure_set(True)
                logger.warning(f"[MQTT{broker_num}] TLS verification disabled")
        
        # Configure WebSocket
        if transport == "websockets":
            mqtt_client.ws_set_options(path="/", headers=None)
        
        return mqtt_client
    
    def create_and_connect_broker(self, broker_num):
        """
        Create a fresh MQTT client and connect it.
        This is the ONLY way to create/connect a broker.
        Returns client_info dict on success, None on failure.
        """
        if not self.repeater_name:
            logger.error("[MQTT] Cannot connect without repeater name")
            return None

        if not self.get_env_bool(f"MQTT{broker_num}_ENABLED", False):
            logger.debug(f"[MQTT{broker_num}] Disabled, skipping")
            return None

        # Get config
        server = self.get_env(f"MQTT{broker_num}_SERVER", "")
        if not server:
            logger.error(f"[MQTT{broker_num}] No server configured")
            return None
        
        port = self.get_env_int(f"MQTT{broker_num}_PORT", 1883)
        transport = self.get_env(f"MQTT{broker_num}_TRANSPORT", "tcp")
        keepalive = self.get_env_int(f"MQTT{broker_num}_KEEPALIVE", 60)
        use_tls = self.get_env_bool(f"MQTT{broker_num}_USE_TLS", False)
        
        logger.debug(f"[MQTT{broker_num}] Creating fresh client")
        
        # Create client
        mqtt_client = self._create_mqtt_client(broker_num)
        if not mqtt_client:
            return None
        
        # Connect
        try:
            mqtt_client.connect(server, port, keepalive=keepalive)
            mqtt_client.loop_start()
            
            # Start WebSocket ping thread if needed
            if transport == "websockets":
                self.ws_ping_threads[broker_num] = {'active': True}
                ping_thread = threading.Thread(
                    target=self._websocket_ping_loop,
                    args=(broker_num, mqtt_client, transport),
                    daemon=True,
                    name=f"WS-Ping-MQTT{broker_num}"
                )
                ping_thread.start()
            
            logger.info(f"[MQTT{broker_num}] Connecting to {server}:{port} (transport={transport}, tls={use_tls}, keepalive={keepalive}s)")
            
            return {
                'client': mqtt_client,
                'broker_num': broker_num,
                'server': server,
                'port': port,
                'connected': False,
                'connecting_since': time.time(),
                'connect_time': 0,
                'reconnect_at': 0,
                'failed_attempts': 0
            }
        except Exception as e:
            logger.error(f"[MQTT{broker_num}] Failed to connect: {e}")
            return None

    def connect_mqtt(self):
        """Initial connection to all configured MQTT brokers"""
        logger.debug("=== MQTT Broker Configuration ===")
        for broker_num in range(1, 5):
            enabled = self.get_env_bool(f"MQTT{broker_num}_ENABLED", False)
            if enabled:
                server = self.get_env(f"MQTT{broker_num}_SERVER", "")
                port = self.get_env_int(f"MQTT{broker_num}_PORT", 1883)
                transport = self.get_env(f"MQTT{broker_num}_TRANSPORT", "tcp")
                use_tls = self.get_env_bool(f"MQTT{broker_num}_USE_TLS", False)
                use_auth_token = self.get_env_bool(f"MQTT{broker_num}_USE_AUTH_TOKEN", False)
                logger.debug(f"[MQTT{broker_num}] ENABLED - {server}:{port} (transport={transport}, tls={use_tls}, auth_token={use_auth_token})")
            else:
                logger.debug(f"[MQTT{broker_num}] DISABLED")
        logger.debug("=================================")
        
        # Connect to all enabled brokers
        for broker_num in range(1, 5):
            self.connection_events[broker_num] = threading.Event()
            
            client_info = self.create_and_connect_broker(broker_num)
            if client_info:
                self.mqtt_clients.append(client_info)
        
        if len(self.mqtt_clients) == 0:
            logger.error("[MQTT] Failed to connect to any broker")
            return False
        
        logger.info(f"[MQTT] Initiated connection to {len(self.mqtt_clients)} broker(s)")
        
        # Wait for all brokers to complete initial connection attempt
        max_wait = 10  # seconds per broker
        for mqtt_info in self.mqtt_clients:
            broker_num = mqtt_info['broker_num']
            event = self.connection_events.get(broker_num)
            if event:
                event.wait(timeout=max_wait)
        
        # Check if at least one connected
        if not self.mqtt_connected:
            logger.error("[MQTT] No brokers connected after initial connection attempts")
            return False
        
        return True
    
    def _stop_websocket_ping_thread(self, broker_num):
        """Cleanly stop the WebSocket ping thread for a broker"""
        if broker_num in self.ws_ping_threads:
            self.ws_ping_threads[broker_num]['active'] = False
            # Give thread a moment to exit cleanly
            time.sleep(0.1)
            # Remove from dict to prevent memory leak
            del self.ws_ping_threads[broker_num]
            logger.debug(f"[MQTT{broker_num}] Stopped WebSocket ping thread")
    
    def reconnect_disconnected_brokers(self):
        """
        Check for disconnected brokers and recreate them.
        Simple: throw away old client, create fresh one.
        Exit after max_reconnect_attempts consecutive failures per broker.
        """
        current_time = time.time()
        
        for i, mqtt_info in enumerate(self.mqtt_clients):
            # Skip if already connected
            if mqtt_info.get('connected', False):
                continue
            
            # Skip if currently connecting (but only if it's been < 10 seconds)
            connecting_since = mqtt_info.get('connecting_since', 0)
            if connecting_since > 0 and (current_time - connecting_since) < 10:
                continue
            
            # Check if it's time to attempt reconnect
            if current_time < mqtt_info.get('reconnect_at', 0):
                continue
            
            broker_num = mqtt_info['broker_num']
            failed_attempts = mqtt_info.get('failed_attempts', 0)
            
            # Exit if too many failures
            if failed_attempts >= self.max_reconnect_attempts:
                logger.critical(f"[MQTT{broker_num}] {self.max_reconnect_attempts} consecutive failures - exiting for service restart")
                sys.exit(1)
            
            logger.info(f"[MQTT{broker_num}] Reconnecting (attempt #{failed_attempts + 1})")
            
            # Stop old client cleanly
            old_client = mqtt_info.get('client')
            if old_client:
                try:
                    # Stop WebSocket ping thread cleanly
                    self._stop_websocket_ping_thread(broker_num)
                    # Stop paho loop and disconnect
                    old_client.loop_stop()
                    old_client.disconnect()
                except Exception as e:
                    logger.debug(f"[MQTT{broker_num}] Error stopping old client: {e}")
            
            # Clear token cache to force fresh token
            if broker_num in self.token_cache:
                del self.token_cache[broker_num]
            
            # Create fresh client
            new_client_info = self.create_and_connect_broker(broker_num)
            
            if new_client_info:
                # Success - replace old client
                self.mqtt_clients[i] = new_client_info
                logger.debug(f"[MQTT{broker_num}] Recreated client successfully")
            else:
                # Failure - increment counter and schedule retry
                mqtt_info['failed_attempts'] = failed_attempts + 1
                jitter = random.uniform(-0.5, 0.5)
                delay = max(0, self.reconnect_delay + jitter)
                mqtt_info['reconnect_at'] = current_time + delay
                self.reconnect_delay = min(self.reconnect_delay * self.reconnect_backoff, self.max_reconnect_delay)
                logger.warning(f"[MQTT{broker_num}] Failed to recreate client (attempt #{failed_attempts + 1}/{self.max_reconnect_attempts})")
        
    def parse_and_publish(self, line):
        if not line:
            return
        logger.debug(f"From Radio: {line}")
        message = {
            "origin": self.repeater_name,
            "origin_id": self.repeater_pub_key,
            "timestamp": datetime.now().isoformat()
        }

        # Handle RAW messages
        if "U RAW:" in line:
            parts = line.split("U RAW:")
            if len(parts) > 1:
                raw_hex = parts[1].strip()
                self.last_raw = raw_hex
                # Count actual bytes (hex string is 2x the actual byte count)
                self.stats['bytes_processed'] += len(raw_hex) // 2

        # Handle DEBUG messages
        if self.debug:
            if line.startswith("DEBUG"):
                message.update({
                    "type": "DEBUG",
                    "message": line
                })
                debug_topic = self.get_topic("debug")
                if debug_topic:
                    self.safe_publish(debug_topic, json.dumps(message))
                return

        # Handle Packet messages (RX and TX)
        packet_match = PACKET_PATTERN.match(line)
        if packet_match:
            direction = packet_match.group(3).lower()  # rx or tx
            
            # Update packet counters
            if direction == "rx":
                self.stats['packets_rx'] += 1
            else:
                self.stats['packets_tx'] += 1
            
            packet_type = packet_match.group(5)
            payload = {
                "type": "PACKET",
                "direction": direction,
                "time": packet_match.group(1),
                "date": packet_match.group(2),
                "len": packet_match.group(4),
                "packet_type": packet_type,
                "route": packet_match.group(6),
                "payload_len": packet_match.group(7),
                "raw": self.last_raw
            }

            # Add SNR, RSSI, score, and hash for RX packets
            if direction == "rx":
                payload.update({
                    "SNR": packet_match.group(8),
                    "RSSI": packet_match.group(9),
                    "score": packet_match.group(10),
                    "duration": packet_match.group(12),
                    "hash": packet_match.group(13)
                })

                # Add path for route=D
                if packet_match.group(6) == "D" and packet_match.group(14):
                    payload["path"] = packet_match.group(14)

            message.update(payload)
            packets_topic = self.get_topic("packets")
            if packets_topic:
                self.safe_publish(packets_topic, json.dumps(message))
            return

    def run(self):
        log_config_sources()
        
        if not self.connect_serial():
            return

        self.set_repeater_time()

        if not self.get_repeater_name():
            logger.error("Failed to get repeater name")
            return
        
        if not self.get_repeater_pubkey():
            logger.error("Failed to get the repeater id (public key)")
            return
        
        if not self.get_repeater_privkey():
            logger.warning("Failed to get repeater private key - auth token authentication will not be available")
        
        # Get radio info before connecting to MQTT
        self.radio_info = self.get_radio_info()
        if not self.radio_info:
            logger.error("Failed to get radio info")
            return
        
        # Get firmware version
        self.firmware_version = self.get_firmware_version()
        if not self.firmware_version:
            logger.warning("Failed to get firmware version - will continue without it")
        
        # Get board type
        self.model = self.get_board_type()
        if not self.model:
            logger.warning("Failed to get board type - will continue without it")
        
        # Get initial device stats
        device_stats = self.get_device_stats()
        if device_stats:
            self.stats['device'] = device_stats
            self.stats['device_prev'] = device_stats.copy()
            logger.info(f"Device stats: {device_stats}")
        else:
            logger.debug("Device stats not available (firmware may not support stats commands)")
        
        # Log client version
        logger.info(f"Client version: {self.client_version}")
        
        # Initial MQTT connection
        retry_count = 0
        max_initial_retries = 10
        while retry_count < max_initial_retries:
            if self.connect_mqtt():
                break
            else:
                retry_count += 1
                wait_time = min(retry_count * 2, 30)  # Max 30 seconds between initial retries
                logger.warning(f"[MQTT] Initial connection failed. Retrying in {wait_time}s... (attempt {retry_count}/{max_initial_retries})")
                sleep(wait_time)
        
        if retry_count >= max_initial_retries:
            logger.error("[MQTT] Failed to establish initial connection after maximum retries")
            sys.exit(1)
        
        # Start stats logging thread
        stats_thread = threading.Thread(
            target=self._stats_logging_loop,
            daemon=True,
            name="Stats-Logger"
        )
        stats_thread.start()
        logger.debug("[STATS] Started statistics logging thread")
        
        try:
            while True:
                if self.should_exit:
                    sys.exit(-1)
                
                # Check and reconnect any disconnected brokers
                self.reconnect_disconnected_brokers()
                
                try:
                    # Check for serial data (with lock for thread safety)
                    with self.ser_lock:
                        if self.ser.in_waiting > 0:
                            line = self.ser.readline().decode(errors='replace').strip()
                            logger.debug(f"RX: {line}")
                            self.parse_and_publish(line)
                except OSError:
                   logger.warning("Serial connection unavailable, trying to reconnect")
                   self.connect_serial()
                   sleep(0.5)
                sleep(0.01)
                
        except KeyboardInterrupt:
            logger.info("\nExiting...")
            for mqtt_client_info in self.mqtt_clients:
                try:
                    mqtt_client_info['client'].disconnect()
                except:
                    pass
            self.ser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-debug", action="store_true", help="Enable debug output")
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)

    bridge = MeshCoreBridge(debug=args.debug)
    bridge.run()