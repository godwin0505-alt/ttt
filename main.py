#!/usr/bin/env python3
"""
Remote OTDR Automation System
- GPIO control for OTDR power and test triggering
- Automatic SOR file detection and conversion
- MariaDB database upload
- Dashboard communication
"""

import os
import csv
import struct
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime, timezone
import crcmod
import re
import json
import urllib.request
import urllib.error
import requests
import time
import glob
from pathlib import Path
import socket
import threading

# GPIO Libraries (for Raspberry Pi)
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    print("⚠️ RPi.GPIO not available - running in simulation mode")
    GPIO_AVAILABLE = False

# Watchdog for file monitoring
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    print("⚠️ Watchdog not available - install with: pip install watchdog")
    WATCHDOG_AVAILABLE = False

# HTTP Server (Flask) availability
try:
    from flask import Flask, request, jsonify
    from flask_cors import CORS
    FLASK_AVAILABLE = True
except Exception:
    print("⚠️ Flask or Flask-CORS not available - dashboard server disabled. Install with: pip3 install Flask Flask-CORS")
    FLASK_AVAILABLE = False

# ========================================
# RASPBERRY PI GPIO CONFIGURATION
# ===============================# GPIO Pin Assignments
RELAY_PIN = 2          # Controls USB power to OTDR (bind/unbind) (GPIO2)
SOLENOID_POWER_PIN  = 17 # Powers ON/OFF the OTDR (GPIO17)
SOLENOID_TEST_PIN = 18   # Triggers TEST on OTDR (GPIO18)

# Timing Configuration (seconds)
POWER_ON_DURATION = 5   # How long to hold power button (LOW pulse)
TEST_TRIGGER_DURATION = 3  # How long to hold test button (LOW pulse)
TEST_COMPLETION_WAIT = 30  # Wait for OTDR to test and store result
BIND_DELAY = 15         # Wait after binding for file access to mount
POWER_OFF_DURATION = 5  # How long to hold power button to turn off (LOW pulse) (LOW pulse)

# File Paths (Raspberry Pi)
OTDR_DATA_PATH = "/media/thejoofc/16 GB Volume/OTDRDATA"  # Where OTDR saves .sor files
JSON_OUTPUT_PATH = "/home/thejoofc/Documents/jsonfiles"    # Where to save .json files

# Dashboard Communication
DASHBOARD_PORT = 5000           # Port for receiving commands from dashboard
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://10.61.71.205:8080/api/status")  # Dashboard on laptop
ENABLE_DASHBOARD_SERVER = True  # Set False to disable HTTP server

# ========================================
# BACKEND SERVER CONFIGURATION
# ========================================
# Change these settings when you get company server details

# For LOCAL testing (your current setup):
USE_LOCAL_SERVER = True  # Set to False when using company server

# LOCAL SERVER SETTINGS (for testing)
# Determine backend upload URL. Priority:
# 1) BACKEND_URL env var (full URL)
# 2) BACKEND_HOST env var (host or ip, optional port)
# 3) BACKEND_DEFAULT env var (full URL)
# 4) fallback to user's laptop IP (10.17.211.205) on port 3000
_backend_env = os.environ.get("BACKEND_URL") or os.environ.get("BACKEND_HOST")
if _backend_env:
    # If BACKEND_URL supplied as full URL use that, otherwise build URL assuming port 3000 and /api/auth/upload
    if _backend_env.startswith("http"):
        LOCAL_SERVER_URL = _backend_env
    else:
        # allow passing host:port as BACKEND_HOST too
        if ":" in _backend_env:
            LOCAL_SERVER_URL = f"http://{_backend_env}/api/auth/upload"
        else:
            LOCAL_SERVER_URL = f"http://{_backend_env}:3000/api/auth/upload"
else:
    LOCAL_SERVER_URL = os.environ.get("BACKEND_DEFAULT", "http://10.172.2.205:3000/api/auth/upload")

print(f"🔗 Using backend upload URL: {LOCAL_SERVER_URL}")

LOCAL_SERVER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6NCwiaWF0IjoxNzcxNTgwNjk0LCJleHAiOjE3NzE2NjcwOTR9.MgSxn6CLcos0L4sEV12VaWsLCWg-KY9PQwDmZEOj0XA"

# COMPANY SERVER SETTINGS (fill these when you get credentials)
COMPANY_SERVER_URL = "https://company-server.com/api/otdr/upload"  # Replace with actual URL
COMPANY_SERVER_TOKEN = ""  # Paste company token here
COMPANY_API_KEY = ""  # If they provide separate API key
COMPANY_USERNAME = ""  # If username/password auth is needed
COMPANY_PASSWORD = ""  # If username/password auth is needed

# Additional headers (if company requires custom headers)
CUSTOM_HEADERS = {
    # Example: "X-API-Version": "1.0",
    # Example: "X-Client-ID": "your-client-id"
}

# ========================================
# GPIO CONTROLLER CLASS
# ========================================
class GPIOController:
    """Manages GPIO operations for OTDR hardware control."""
    
    def __init__(self):
        self.initialized = False
        
    def setup_gpio(self):
        """Initialize GPIO pins for solenoids and relay."""
        if not GPIO_AVAILABLE:
            print("⚠️  GPIO not available - running in simulation mode")
            return False
            
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            
            # Setup relay (LOW = unbound, HIGH = bound)
            GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)  # Start with OTDR unbound
            
            # Setup solenoids (LOW = ON/active, HIGH = OFF/inactive)
            GPIO.setup(SOLENOID_POWER_PIN, GPIO.OUT, initial=GPIO.HIGH)  # Start OFF
            GPIO.setup(SOLENOID_TEST_PIN, GPIO.OUT, initial=GPIO.HIGH)   # Start OFF
            
            self.initialized = True
            print("✅ GPIO initialized successfully")
            return True
            
        except Exception as e:
            print(f"❌ GPIO initialization failed: {e}")
            return False
    
    def cleanup_gpio(self):
        """Cleanup GPIO pins safely."""
        if GPIO_AVAILABLE and self.initialized:
            try:
                GPIO.cleanup()
                print("✅ GPIO cleanup completed")
            except Exception as e:
                print(f"⚠️  GPIO cleanup warning: {e}")
    
    def relay_control(self, bind_otdr):
        """
        Control USB power relay.
        Args:
            bind_otdr (bool): True to bind OTDR (relay ON), False to unbind (relay OFF)
        """
        if not GPIO_AVAILABLE:
            print(f"🔧 [SIMULATION] Relay {'ON (bind)' if bind_otdr else 'OFF (unbind)'}")
            return True
            
        try:
            # Relay: HIGH = bound, LOW = unbound
            GPIO.output(RELAY_PIN, GPIO.HIGH if bind_otdr else GPIO.LOW)
            status = "bound (ON)" if bind_otdr else "unbound (OFF)"
            print(f"🔌 OTDR {status}")
            return True
            
        except Exception as e:
            print(f"❌ Relay control failed: {e}")
            return False
    
    def solenoid_pulse(self, pin, duration, description=""):
        """
        Activate solenoid for specified duration.
        Args:
            pin (int): GPIO pin number
            duration (float): Duration in seconds
            description (str): Action description for logging
        """
        if not GPIO_AVAILABLE:
            print(f"🔧 [SIMULATION] {description} - pulse {duration}s")
            time.sleep(duration)
            return True
            
        try:
            print(f"⚡ {description} - activating...")
            GPIO.output(pin, GPIO.LOW)  # Activate solenoid (LOW = ON)
            time.sleep(duration)
            GPIO.output(pin, GPIO.HIGH)   # Deactivate solenoid (HIGH = OFF)
            print(f"✅ {description} - completed")
            return True
            
        except Exception as e:
            print(f"❌ Solenoid pulse failed: {e}")
            # Ensure solenoid is OFF even if error occurred
            try:
                GPIO.output(pin, GPIO.HIGH)
            except:
                pass
            return False

# ========================================
# FILE MONITORING SYSTEM
# ========================================
class SORFileHandler:
    """Handles detection and processing of new .sor files."""
    
    def __init__(self, json_output_path, upload_callback):
        self.json_output_path = json_output_path
        self.upload_callback = upload_callback
        self.processed_files = set()  # Track already processed files
        
    def find_latest_date_folder(self):
        """Find the most recent date folder in OTDR data path."""
        # Try the configured path first. If it fails (e.g., mount point name varies
        # or contains spaces), attempt to locate any OTDRDATA directory under /media
        try:
            base_path = OTDR_DATA_PATH
            if not os.path.exists(base_path):
                # Fallback: search for any OTDRDATA-like directory under /media/<user>
                user_media = os.path.join('/media', os.getlogin())
                candidates = glob.glob(os.path.join(user_media, '**', '*OTDRDATA*'), recursive=True)
                candidates = [c for c in candidates if os.path.isdir(c)]
                if candidates:
                    # Prefer exact match containing 'OTDRDATA' or pick the first
                    base_path = candidates[0]
                    print(f"🔎 Using fallback OTDR data path: {base_path}")
                else:
                    # No base path found
                    raise FileNotFoundError(f"OTDR data path not found: {OTDR_DATA_PATH}")

            date_folders = []
            for item in os.listdir(base_path):
                folder_path = os.path.join(base_path, item)
                if os.path.isdir(folder_path):
                    # Prefer folders matching YYYY_M_D pattern
                    parts = item.split('_')
                    if len(parts) == 3:
                        try:
                            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                            date_folders.append(((year, month, day), folder_path))
                        except ValueError:
                            # Not a date-style folder; collect by mtime instead
                            date_folders.append(((0, 0, 0, os.path.getmtime(folder_path)), folder_path))
                    else:
                        # Non-matching name - use mtime to allow selection of most recent
                        date_folders.append(((0, 0, 0, os.path.getmtime(folder_path)), folder_path))

            if not date_folders:
                return None

            # Sort: date-like tuples first (by year,month,day), otherwise by mtime
            def sort_key(tup):
                key, _ = tup
                if len(key) == 3:
                    return (key[0], key[1], key[2], 0)
                else:
                    return (0, 0, 0, key[3])

            date_folders.sort(key=sort_key, reverse=True)
            latest_folder = date_folders[0][1]
            print(f"📁 Latest folder: {os.path.basename(latest_folder)} (base: {base_path})")
            return latest_folder

        except Exception as e:
            print(f"❌ Error finding date folder: {e}")
            return None
    
    def find_unconverted_files(self, date_folder):
        """
        Find .sor files that haven't been converted to .json yet.
        Checks against *_complete_analysis.json files in the json output folder.
        Args:
            date_folder (str): Path to date folder containing .sor files
        Returns:
            list: Paths to unconverted .sor files
        """
        try:
            unconverted = []
            
            # Get all .sor files in date folder
            sor_pattern = os.path.join(date_folder, "*.sor")
            sor_files = glob.glob(sor_pattern)
            
            for sor_file in sor_files:
                base_name = os.path.splitext(os.path.basename(sor_file))[0]
                # Match the actual exported filename: {base_name}_complete_analysis.json
                json_file = os.path.join(self.json_output_path, f"{base_name}_complete_analysis.json")
                
                if os.path.exists(json_file):
                    print(f"⏭️  Skipping {base_name}.sor — already converted")
                elif sor_file in self.processed_files:
                    print(f"⏭️  Skipping {base_name}.sor — already processed this session")
                else:
                    unconverted.append(sor_file)
            
            return unconverted
            
        except Exception as e:
            print(f"❌ Error checking unconverted files: {e}")
            return []
    
    def convert_and_upload(self, sor_file_path):
        """
        Convert .sor file to .json and upload to database.
        Args:
            sor_file_path (str): Path to .sor file
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            print(f"\n{'='*60}")
            print(f"🔄 Processing: {os.path.basename(sor_file_path)}")
            print(f"{'='*60}")
            
            # Parse OTDR file (use parser.parse_sor_file() to validate)
            parser = MainOTDRParser(sor_file_path)
            if not parser.parse_sor_file():
                print(f"❌ Invalid or unparsable OTDR file: {sor_file_path}")
                return False
            
            # Export JSON
            base_name = os.path.splitext(os.path.basename(sor_file_path))[0]
            json_output_base = os.path.join(self.json_output_path, base_name)
            json_file_path = f"{json_output_base}_complete_analysis.json"
            
            # Ensure JSON output directory exists
            os.makedirs(self.json_output_path, exist_ok=True)
            
            # Export comprehensive JSON (pass base path; method appends _complete_analysis.json)
            parser.export_comprehensive_json(json_output_base)
            print(f"✅ JSON exported: {json_file_path}")
            
            # Upload to database
            if self.upload_callback:
                success = self.upload_callback(json_file_path)
                if success:
                    print(f"✅ Uploaded to database successfully")
                    self.processed_files.add(sor_file_path)
                    return True
                else:
                    print(f"⚠️  Database upload failed")
                    return False
            else:
                print(f"⚠️  No upload callback configured")
                self.processed_files.add(sor_file_path)
                return True
                
        except Exception as e:
            print(f"❌ Conversion/upload failed: {e}")
            import traceback
            traceback.print_exc()
            return False

# ========================================
# DASHBOARD COMMUNICATION
# ========================================
class DashboardCommunicator:
    """Manages status updates to dashboard."""
    
    def __init__(self, dashboard_url=None):
        self.dashboard_url = dashboard_url or DASHBOARD_URL
        self.status_queue = []
        self.current_status = "IDLE"
        
    def send_status(self, status, message="", progress=None):
        """
        Send status update to dashboard.
        Args:
            status (str): Status code (UNBINDING, POWERING_ON, TESTING, etc.)
            message (str): Optional detailed message
            progress (int): Optional progress percentage (0-100)
        """
        timestamp = datetime.now().isoformat()
        status_msg = {
            "timestamp": timestamp,
            "status": status,
            "message": message,
            "progress": progress
        }
        
        print(f"📡 [{status}] {message}")
        self.status_queue.append(status_msg)
        self.current_status = status
        
        # Send HTTP POST to dashboard
        if self.dashboard_url:
            try:
                response = requests.post(
                    self.dashboard_url,
                    json=status_msg,
                    timeout=5
                )
                if response.status_code == 200:
                    print(f"✅ Status sent to dashboard")
                else:
                    print(f"⚠️  Dashboard returned {response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"⚠️  Failed to send status to dashboard: {e}")
        
    def get_status_history(self):
        """Get all status messages."""
        return self.status_queue
    
    def get_current_status(self):
        """Get current status."""
        return self.current_status
    
    def clear_status(self):
        """Clear status queue."""
        self.status_queue = []
        self.current_status = "IDLE"

# SSL verification (set to False only if company uses self-signed cert)
VERIFY_SSL = True

# Request timeout (seconds)
REQUEST_TIMEOUT = 60

# AUTO-SELECTED CONFIGURATION (don't modify this part)
if USE_LOCAL_SERVER:
    BACKEND_URL = LOCAL_SERVER_URL
    BACKEND_TOKEN = LOCAL_SERVER_TOKEN
else:
    BACKEND_URL = COMPANY_SERVER_URL
    BACKEND_TOKEN = COMPANY_SERVER_TOKEN

# ========================================
def sorfile(filename):
    try:
        fh = open(filename, "rb")
        return FH(fh)
    except IOError as e:
        print(f"Failed to read {filename}")
        raise e

class FH:
    def __init__(self, filehandle):
        self.filehandle = filehandle
        self.bufsize = 2048
        self.buffer = b""
        self.spaceleft = self.bufsize
        self.crc16 = crcmod.predefined.Crc("crc-ccitt-false")
    
    def read(self, *args, **kwargs):
        buf = self.filehandle.read(*args, **kwargs)
        xlen = len(buf)
        if xlen > self.spaceleft:
            self.crc16.update(self.buffer)
            self.buffer = b""
            self.spaceleft = self.bufsize
        self.buffer += buf
        self.spaceleft -= xlen
        return buf
    
    def digest(self):
        self.crc16.update(self.buffer)
        return self.crc16.crcValue
    
    def seek(self, *args, **kwargs):
        if args[0] == 0:
            self.buffer = b""
            self.spaceleft = self.bufsize
            self.crc16 = crcmod.predefined.Crc("crc-ccitt-false")
        return self.filehandle.seek(*args, **kwargs)
    
    def tell(self):
        return self.filehandle.tell()
    
    def close(self):
        return self.filehandle.close()

def get_string(fh):
    mystr = b""
    byte = fh.read(1)
    while byte != b"":
        tt = struct.unpack("c", byte)[0]
        if tt == b"\x00":
            break
        mystr += tt
        byte = fh.read(1)
    return mystr.decode("utf-8")

def get_uint(fh, nbytes=2):
    word = fh.read(nbytes)
    if nbytes == 2:
        return struct.unpack("<H", word)[0]
    elif nbytes == 4:
        return struct.unpack("<I", word)[0]
    elif nbytes == 8:
        return struct.unpack("<Q", word)[0]
    else:
        raise ValueError("Trying to get uint of size > 8bytes")

def get_signed(fh, nbytes=2):
    word = fh.read(nbytes)
    if nbytes == 2:
        val = struct.unpack("<h", word)[0]
    elif nbytes == 4:
        val = struct.unpack("<i", word)[0]
    elif nbytes == 8:
        val = struct.unpack("<q", word)[0]
    else:
        raise ValueError("Trying to get int of size > 8bytes")
    return val

class MainOTDRParser:
    def __init__(self, filename):
        self.filename = filename
        self.results = {}
        self.events = []
        self.trace_data = []
        self.device_type = None  # Will be set to 'MICRO' or 'MINI' after parsing
        
    def parse_sor_file(self):
        """Parse SOR file using enhanced extraction"""
        try:
            fh = sorfile(self.filename)
            self.results = {}
            tracedata = []
            
            # Parse using the proven method
            status = self._parse_map_block(fh)
            if status != "ok":
                print(f"❌ Failed to parse map block")
                return False
            
            # Parse all blocks
            klist = sorted(self.results["blocks"], key=lambda x: self.results["blocks"][x]["order"])
            
            for bname in klist:
                ref = self.results["blocks"][bname]
                block_name = ref["name"]
                
                if block_name == "GenParams":
                    self._parse_gen_params(fh)
                elif block_name == "SupParams":
                    self._parse_sup_params(fh)
                elif block_name == "FxdParams":
                    self._parse_fxd_params(fh)
                elif block_name == "KeyEvents":
                    self._parse_key_events_enhanced(fh)
                elif block_name == "DataPts":
                    self._parse_data_points(fh, tracedata)
                else:
                    self._skip_block(fh, bname)
            
            fh.close()
            
            # Convert trace data
            self._convert_trace_data(tracedata)
            
            return True
            
        except Exception as e:
            print(f"❌ Error parsing SOR file: {e}")
            return False
    
    def _parse_map_block(self, fh):
        """Parse map block to understand file structure"""
        fh.seek(0)
        tt = get_string(fh)
        if tt == "Map":
            self.results["format"] = 2
        else:
            self.results["format"] = 1
            fh.seek(0)
        
        self.results["version"] = "%.2f" % (get_uint(fh, 2) * 0.01)
        self.results["mapblock"] = {}
        self.results["mapblock"]["nbytes"] = get_uint(fh, 4)
        self.results["mapblock"]["nblocks"] = get_uint(fh, 2) - 1
        self.results["blocks"] = {}
        
        startpos = self.results["mapblock"]["nbytes"]
        for i in range(self.results["mapblock"]["nblocks"]):
            bname = get_string(fh)
            bver = "%.2f" % (get_uint(fh, 2) * 0.01)
            bsize = get_uint(fh, 4)
            ref = {
                "name": bname,
                "version": bver,
                "size": bsize,
                "pos": startpos,
                "order": i,
            }
            self.results["blocks"][bname] = ref
            startpos += bsize
        
        return "ok"
    
    def _parse_gen_params(self, fh):
        """Parse general parameters"""
        bname = "GenParams"
        try:
            ref = self.results["blocks"][bname]
            fh.seek(ref["pos"])
            
            if self.results["format"] == 2:
                mystr = fh.read(len(bname) + 1).decode("ascii")
                if mystr != bname + "\0":
                    return
            
            self.results[bname] = {}
            xref = self.results[bname]
            
            lang = fh.read(2).decode("ascii")
            xref["language"] = lang
            
            # Read fields based on format
            if self.results["format"] == 1:
                fields = [
                    "cable ID", "fiber ID", "wavelength", "location A", "location B", 
                    "cable code/fiber type", "build condition", "user offset", "operator", "comments"
                ]
            else:
                fields = [
                    "cable ID", "fiber ID", "fiber type", "wavelength", "location A", "location B",
                    "cable code/fiber type", "build condition", "user offset", "user offset distance", 
                    "operator", "comments"
                ]
            
            for field in fields:
                if field == "build condition":
                    val = fh.read(2).decode("ascii")
                    xstr = self._build_condition(val)
                elif field == "fiber type":
                    val = get_uint(fh, 2)
                    xstr = self._fiber_type(val)
                elif field == "wavelength":
                    val = get_uint(fh, 2)
                    xstr = f"{val} nm"
                elif field in ["user offset", "user offset distance"]:
                    val = get_signed(fh, 4)
                    xstr = str(val)
                else:
                    xstr = get_string(fh)
                
                xref[field] = xstr
            
            print(f"✅ General Parameters: Wavelength {xref.get('wavelength', 'Unknown')}")
            
        except Exception as e:
            print(f"⚠️ Error parsing general parameters: {e}")
    
    def _parse_sup_params(self, fh):
        """Parse supplier parameters"""
        bname = "SupParams"
        try:
            ref = self.results["blocks"][bname]
            fh.seek(ref["pos"])
            
            if self.results["format"] == 2:
                mystr = fh.read(len(bname) + 1).decode("ascii")
                if mystr != bname + "\0":
                    return
            
            self.results[bname] = {}
            xref = self.results[bname]
            
            fields = ["supplier", "OTDR", "OTDR S/N", "module", "module S/N", "software", "other"]
            
            for field in fields:
                xstr = get_string(fh)
                xref[field] = xstr
            
            # Detect device type based on supplier/OTDR model
            supplier = xref.get('supplier', '').upper()
            otdr_model = xref.get('OTDR', '').upper()
            
            if 'GRANDWAY' in supplier or 'GRANDWAY' in otdr_model:
                self.device_type = 'MINI'
                print(f"✅ Supplier: {xref.get('supplier', 'Unknown')} {xref.get('OTDR', 'Unknown')} [Mini OTDR Detected]")
            elif 'FIBERCLOUD' in supplier or 'FC4000' in otdr_model or 'FIBERCLOUD' in otdr_model:
                self.device_type = 'MICRO'
                print(f"✅ Supplier: {xref.get('supplier', 'Unknown')} {xref.get('OTDR', 'Unknown')} [Micro OTDR Detected]")
            else:
                self.device_type = 'UNKNOWN'
                print(f"✅ Supplier: {xref.get('supplier', 'Unknown')} {xref.get('OTDR', 'Unknown')} [Unknown Device]")
            
        except Exception as e:
            print(f"⚠️ Error parsing supplier parameters: {e}")
    
    def _parse_fxd_params(self, fh):
        """Parse fixed parameters (test conditions)"""
        bname = "FxdParams"
        try:
            ref = self.results["blocks"][bname]
            fh.seek(ref["pos"])
            
            if self.results["format"] == 2:
                mystr = fh.read(len(bname) + 1).decode("ascii")
                if mystr != bname + "\0":
                    return
            
            self.results[bname] = {}
            xref = self.results[bname]
            
            # Parse fixed parameters based on format
            if self.results["format"] == 1:
                plist = [
                    ["date/time", 0, 4, "v", "", "", ""],
                    ["unit", 4, 2, "s", "", "", ""],
                    ["wavelength", 6, 2, "v", 0.1, 1, "nm"],
                    ["acquisition offset", 8, 4, "i", "", "", ""],
                    ["number of pulse width entries", 12, 2, "v", "", "", ""],
                    ["pulse width", 14, 2, "v", "", 0, "ns"],
                    ["sample spacing", 16, 4, "v", 1e-8, "", "usec"],
                    ["num data points", 20, 4, "v", "", "", ""],
                    ["index", 24, 4, "v", 1e-5, 6, ""],
                    ["BC", 28, 2, "v", -0.1, 2, "dB"],
                    ["num averages", 30, 4, "v", "", "", ""],
                    ["range", 34, 4, "v", 2e-5, 6, "km"],
                ]
            else:
                plist = [
                    ["date/time", 0, 4, "v", "", "", ""],
                    ["unit", 4, 2, "s", "", "", ""],
                    ["wavelength", 6, 2, "v", 0.1, 1, "nm"],
                    ["acquisition offset", 8, 4, "i", "", "", ""],
                    ["acquisition offset distance", 12, 4, "i", "", "", ""],
                    ["number of pulse width entries", 16, 2, "v", "", "", ""],
                    ["pulse width", 18, 2, "v", "", 0, "ns"],
                    ["sample spacing", 20, 4, "v", 1e-8, "", "usec"],
                    ["num data points", 24, 4, "v", "", "", ""],
                    ["index", 28, 4, "v", 1e-5, 6, ""],
                    ["BC", 32, 2, "v", -0.1, 2, "dB"],
                    ["num averages", 34, 4, "v", "", "", ""],
                    ["averaging time", 38, 2, "v", 0.1, 0, "sec"],
                    ["range", 40, 4, "v", 2e-5, 6, "km"],
                ]
            
            for field in plist:
                name = field[0]
                fsize = field[2]
                ftype = field[3]
                scale = field[4]
                dgt = field[5]
                unit = field[6]
                
                if ftype == "i":
                    val = get_signed(fh, fsize)
                    xstr = val
                elif ftype == "v":
                    val = get_uint(fh, fsize)
                    if scale != "":
                        val *= scale
                    if dgt != "":
                        fmt = "%%.%df" % dgt
                        xstr = fmt % val
                    else:
                        xstr = val
                elif ftype == "s":
                    xstr = fh.read(fsize).decode("utf-8")
                else:
                    val = fh.read(fsize)
                    xstr = val
                
                if name == "date/time":
                    xstr = datetime.fromtimestamp(val, timezone.utc).strftime(
                        "%a %b %d %H:%M:%S %Y"
                    ) + (" (%d sec)" % val)
                
                xref[name] = xstr if unit == "" else str(xstr) + " " + unit
            
            print(f"✅ Test Conditions: {xref.get('wavelength', 'Unknown')}, {xref.get('pulse width', 'Unknown')}, Range: {xref.get('range', 'Unknown')}")
            
        except Exception as e:
            print(f"⚠️ Error parsing fixed parameters: {e}")
    
    def _parse_key_events_enhanced(self, fh):
        """Parse key events with enhanced extraction"""
        bname = "KeyEvents"
        try:
            ref = self.results["blocks"][bname]
            fh.seek(ref["pos"])
            
            if self.results["format"] == 2:
                mystr = fh.read(len(bname) + 1).decode("ascii")
                if mystr != bname + "\0":
                    return
            
            self.results[bname] = {}
            xref = self.results[bname]
            
            # Number of events
            nev = get_uint(fh, 2)
            xref["num events"] = nev
            
            print(f"🔍 Found {nev} events")
            
            # Calculate distance factor
            if "FxdParams" in self.results:
                sol = 299792.458 / 1.0e6  # km/usec
                index = float(self.results["FxdParams"]["index"])
                factor = 1e-4 * sol / index
            else:
                factor = 1e-4 * 0.2041  # fallback
            
            # Parse each event
            events = []
            pat = re.compile("(.)(.)9999LS")
            
            for j in range(nev):
                event = {}
                
                # Event ID
                event_id = get_uint(fh, 2)
                event["event_id"] = event_id
                
                # Distance (time of travel)
                dist_raw = get_uint(fh, 4)
                distance = dist_raw * factor
                event["distance_km"] = distance
                
                # Slope (for Mini OTDR, this is the Att dB/km field)
                slope_raw = get_signed(fh, 2)
                slope = slope_raw * 0.001
                event["slope_db_km"] = slope
                event["slope_raw"] = slope_raw  # Store raw value for Mini OTDR
                
                # Splice loss (for Mini OTDR, this is the Loss dB field)
                splice_raw = get_signed(fh, 2)
                splice = splice_raw * 0.001
                event["splice_loss_db"] = splice
                event["splice_raw"] = splice_raw  # Store raw value
                
                # Debug for Mini OTDR
                if self.device_type == 'MINI':
                    print(f"    Raw values: slope_raw={slope_raw} ({slope:.3f} dB/km), splice_raw={splice_raw} ({splice:.3f} dB)")
                
                # Calculate actual fiber attenuation if this is a segment
                if j > 0:
                    prev_event = events[j-1] if events else None
                    if prev_event:
                        prev_distance = prev_event.get("distance_km", 0)
                        segment_length = distance - prev_distance
                        if segment_length > 0:
                            # Fiber attenuation calculation (typical values 0.2-0.4 dB/km for 1550nm)
                            if abs(splice) < 0.1:  # If splice loss is small, this might be fiber attenuation
                                calculated_attenuation = abs(splice) / segment_length
                                event["calculated_attenuation_db_km"] = calculated_attenuation
                            else:
                                event["calculated_attenuation_db_km"] = 0.0
                        else:
                            event["calculated_attenuation_db_km"] = 0.0
                    else:
                        event["calculated_attenuation_db_km"] = 0.0
                else:
                    event["calculated_attenuation_db_km"] = 0.0
                
                # Reflection loss
                refl = get_signed(fh, 4) * 0.001
                event["reflection_loss_db"] = refl
                
                # Event type
                xtype = fh.read(8).decode("ascii")
                event["event_type_raw"] = xtype
                
                # Decode event type
                mresults = pat.match(xtype)
                if mresults is not None:
                    subtype = mresults.groups(0)[0]
                    manual = mresults.groups(0)[1]
                    
                    if manual == "A":
                        mode = "Manual"
                    else:
                        mode = "Auto"
                    
                    if subtype == "1":
                        event_type = f"Reflection ({mode})"
                    elif subtype == "0":
                        event_type = f"Loss/Drop/Gain ({mode})"
                    elif subtype == "2":
                        event_type = f"Multiple Events ({mode})"
                    else:
                        event_type = f"Unknown {subtype} ({mode})"
                else:
                    event_type = f"Unknown [{xtype}]"
                
                event["event_type"] = event_type
                
                # Additional fields for format 2
                if self.results["format"] == 2:
                    end_prev = get_uint(fh, 4) * factor
                    start_curr = get_uint(fh, 4) * factor
                    end_curr = get_uint(fh, 4) * factor
                    start_next = get_uint(fh, 4) * factor
                    peak_pos = get_uint(fh, 4) * factor
                    
                    event["end_prev"] = end_prev
                    event["start_curr"] = start_curr
                    event["end_curr"] = end_curr
                    event["start_next"] = start_next
                    event["peak_pos"] = peak_pos
                    
                    # Debug: Print additional fields for Mini OTDR
                    if self.device_type == 'MINI':
                        print(f"    Format2 fields: end_prev={end_prev:.6f}, start_curr={start_curr:.6f}, end_curr={end_curr:.6f}, start_next={start_next:.6f}, peak_pos={peak_pos:.6f}")
                
                # Comments
                comments = get_string(fh)
                event["comments"] = comments
                
                events.append(event)
                
                print(f"  Event {j+1}: {event_type} at {distance:.6f} km, Loss: {splice:.3f} dB")
            
            self.events = events
            xref["events"] = events
            
            # Parse summary
            if fh.tell() < ref["pos"] + ref["size"]:
                total = get_signed(fh, 4) * 0.001
                loss_start = get_signed(fh, 4) * factor
                loss_finish = get_uint(fh, 4) * factor
                orl = get_uint(fh, 2) * 0.001
                orl_start = get_signed(fh, 4) * factor
                orl_finish = get_uint(fh, 4) * factor
                
                summary = {
                    "total_loss_db": total,
                    "loss_start_km": loss_start,
                    "loss_end_km": loss_finish,
                    "optical_return_loss_db": orl,
                    "orl_start_km": orl_start,
                    "orl_end_km": orl_finish
                }
                
                xref["summary"] = summary
                print(f"📊 Summary: Total Loss {total:.3f} dB, ORL {orl:.3f} dB")
            
            # For Mini OTDR: Calculate TL using the correct formula
            if self.device_type == 'MINI':
                previous_tl = 0.0
                previous_loss = 0.0
                
                for i, event in enumerate(events):
                    section_km = 0.0
                    current_loss = event.get("splice_loss_db", 0)
                    avg_att = event.get("slope_db_km", 0)
                    
                    # Calculate section km
                    if i == 0:
                        section_km = event.get("distance_km", 0)
                    else:
                        prev_distance = events[i-1].get("distance_km", 0)
                        section_km = event.get("distance_km", 0) - prev_distance
                    
                    # Formula: Total-L(dB) = ((Section(km) × Average-L dB/km) + Previous_Loss dB) + Previous_Total-L dB
                    mini_tl = ((section_km * avg_att) + previous_loss) + previous_tl
                    
                    event["mini_tl_db"] = mini_tl
                    print(f"  Mini OTDR TL for Event {i+1}: {mini_tl:.3f} dB (Section: {section_km:.6f} km, Att: {avg_att:.3f} dB/km, Loss: {current_loss:.3f} dB)")
                    
                    # Update for next iteration
                    previous_tl = mini_tl
                    previous_loss = current_loss
            
        except Exception as e:
            print(f"⚠️ Error parsing key events: {e}")
    
    def _parse_data_points(self, fh, tracedata):
        """Parse data points (trace data)"""
        bname = "DataPts"
        try:
            ref = self.results["blocks"][bname]
            fh.seek(ref["pos"])
            
            if self.results["format"] == 2:
                mystr = fh.read(len(bname) + 1).decode("ascii")
                if mystr != bname + "\0":
                    return
            
            self.results[bname] = {}
            xref = self.results[bname]
            xref["_datapts_params"] = {"xscaling": 1, "offset": "STV"}
            
            # Get OTDR model for scaling
            try:
                model = self.results["SupParams"]["OTDR"]
            except:
                model = ""
            
            if model == "OFL250":
                xref["_datapts_params"]["xscaling"] = 0.1
            
            # Number of data points
            N = get_uint(fh, 4)
            xref["num data points"] = N
            
            # Number of traces
            val = get_signed(fh, 2)
            xref["num traces"] = val
            
            if val > 1:
                print(f"⚠️ Multiple traces not supported ({val} traces)")
                return
            
            # Repeated number of data points
            val = get_uint(fh, 4)
            xref["num data points 2"] = val
            
            # Scaling factor
            val = get_uint(fh, 2)
            scaling_factor = val / 1000.0
            xref["scaling factor"] = scaling_factor
            
            # Get resolution - use same method as original pyotdr
            if "FxdParams" in self.results:
                index = float(self.results["FxdParams"]["index"])
                sample_spacing_str = self.results["FxdParams"]["sample spacing"]
                sample_spacing = float(sample_spacing_str.split(" ")[0])
                
                # Original pyotdr method: speed of light in km/usec divided by index
                sol = 299792.458 / 1.0e6  # km/usec
                dx = sample_spacing * sol / index
                
                print(f"🔍 Distance calculation: sample_spacing={sample_spacing} usec, index={index}, dx={dx:.8f} km/sample")
            else:
                dx = 0.000128  # fallback value in km/sample
            
            # Read data points
            dlist = []
            for i in range(N):
                val = get_uint(fh, 2)
                dlist.append(val)
            
            if dlist:
                ymax = max(dlist)
                ymin = min(dlist)
                fs = 0.001 * scaling_factor
                
                xref["max before offset"] = ymax * fs
                xref["min before offset"] = ymin * fs
                
                # Convert to trace data
                xscaling = xref["_datapts_params"]["xscaling"]
                nlist = [(ymax - x) * fs for x in dlist]
                
                for i in range(N):
                    # Distance in km (already properly calculated)
                    x = dx * i * xscaling
                    y = nlist[i]
                    tracedata.append(f"{x:f}\t{y:f}\n")
                
                print(f"✅ Parsed {N} trace data points")
            
        except Exception as e:
            print(f"⚠️ Error parsing data points: {e}")
    
    def _skip_block(self, fh, bname):
        """Skip a block we don't process"""
        try:
            ref = self.results["blocks"][bname]
            fh.seek(ref["pos"] + ref["size"])
        except:
            pass
    
    def _convert_trace_data(self, tracedata):
        """Convert trace data to our format"""
        self.trace_data = []
        for line in tracedata:
            try:
                parts = line.strip().split('\t')
                if len(parts) >= 2:
                    distance = float(parts[0])
                    power = float(parts[1])
                    self.trace_data.append({
                        'distance_km': distance,
                        'power_loss_db': power
                    })
            except:
                continue
    
    def _build_condition(self, bcstr):
        """Decode build condition"""
        if bcstr == "BC":
            return "As Built"
        elif bcstr == "CC":
            return "As Current"
        elif bcstr == "RC":
            return "As Repaired"
        elif bcstr == "OT":
            return "Other"
        else:
            return f"Unknown ({bcstr})"
    
    def _fiber_type(self, val):
        """Decode fiber type"""
        if val == 651:
            return "G.651 (50um core multimode)"
        elif val == 652:
            return "G.652 (standard SMF)"
        elif val == 653:
            return "G.653 (dispersion-shifted fiber)"
        elif val == 654:
            return "G.654 (1550nm loss-minimized fiber)"
        elif val == 655:
            return "G.655 (nonzero dispersion-shifted fiber)"
        else:
            return f"G.{val} (unknown)"
    
    def export_comprehensive_json(self, output_base=None):
        """Export all data into one comprehensive JSON file"""
        import json
        
        if output_base is None:
            output_base = os.path.splitext(self.filename)[0]
        
        try:
            comprehensive_file = f"{output_base}_complete_analysis.json"
            
            # Build the complete data structure
            export_data = {
                "metadata": {
                    "title": "OTDR COMPLETE ANALYSIS REPORT",
                    "filename": os.path.basename(self.filename),
                    "generated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "device_type": self.device_type
                },
                "test_conditions": {},
                "general_parameters": {},
                "equipment": {},
                "events": [],
                "fiber_summary": {},
                "trace_data": []
            }
            
            # Test Conditions
            if "FxdParams" in self.results:
                fp = self.results["FxdParams"]
                export_data["test_conditions"] = {
                    "wavelength": fp.get("wavelength", "Unknown"),
                    "pulse_width": fp.get("pulse width", "Unknown"),
                    "test_range": fp.get("range", "Unknown"),
                    "refraction_index": fp.get("index", "Unknown"),
                    "sample_points": fp.get("num data points", "Unknown"),
                    "averages": fp.get("num averages", "Unknown"),
                    "date_time": fp.get("date/time", "Unknown")
                }
            
            # General Parameters
            if "GenParams" in self.results:
                gp = self.results["GenParams"]
                export_data["general_parameters"] = {
                    "cable_id": gp.get("cable ID", ""),
                    "fiber_id": gp.get("fiber ID", ""),
                    "fiber_type": gp.get("fiber type", ""),
                    "wavelength": gp.get("wavelength", ""),
                    "location_a": gp.get("location A", ""),
                    "location_b": gp.get("location B", ""),
                    "cable_code_fiber_type": gp.get("cable code/fiber type", ""),
                    "build_condition": gp.get("build condition", ""),
                    "user_offset": gp.get("user offset", ""),
                    "operator": gp.get("operator", ""),
                    "comments": gp.get("comments", "")
                }
            
            # Equipment Information
            if "SupParams" in self.results:
                sp = self.results["SupParams"]
                export_data["equipment"] = {
                    "supplier": sp.get("supplier", ""),
                    "otdr_model": sp.get("OTDR", ""),
                    "otdr_serial_number": sp.get("OTDR S/N", ""),
                    "module": sp.get("module", ""),
                    "module_serial_number": sp.get("module S/N", ""),
                    "software": sp.get("software", ""),
                    "other": sp.get("other", "")
                }
            
            # Events Data
            if self.events:
                previous_total_l = 0
                
                print(f"📝 JSON Export - Device Type: {self.device_type}")
                
                for i, event in enumerate(self.events, 1):
                    distance = event.get("distance_km", 0)
                    splice_loss = event.get("splice_loss_db", 0)
                    reflection = event.get("reflection_loss_db", 0)
                    slope = event.get("slope_db_km", 0)
                    event_type = event.get("event_type", "Unknown")
                    
                    # Calculate segment
                    if i == 1:
                        segment = distance
                    else:
                        prev_distance = self.events[i-2].get("distance_km", 0)
                        segment = distance - prev_distance
                    
                    # Device-specific calculations
                    if self.device_type == 'MINI':
                        # Mini OTDR: Use slope field directly as Att (dB/km)
                        att_db_km = slope
                        # Mini OTDR: Use cumulative TL from parsing
                        total_l_db = event.get("mini_tl_db", 0)
                    else:
                        # Micro OTDR: Calculate slope for this segment
                        if i > 1 and segment > 0:
                            calculated_slope = splice_loss / segment if splice_loss != 0 else slope
                        else:
                            calculated_slope = slope
                        att_db_km = calculated_slope
                        
                        # Micro OTDR: Calculate Total-L(dB) = ((Section km × Average-L dB/km) + Loss dB) + previous Total-L(dB)
                        total_l_db = ((segment * att_db_km) + splice_loss) + previous_total_l
                        previous_total_l = total_l_db
                    
                    event_data = {
                        "event_number": i,
                        "event_type": event_type.replace(" (Auto)", "").replace(" (Manual)", ""),
                        "event_type_raw": event.get("event_type_raw", ""),
                        "distance_km": round(distance, 6),
                        "segment_km": round(segment, 6),
                        "loss_db": round(splice_loss, 3),
                        "total_loss_db": round(total_l_db, 3),
                        "average_loss_db_km": round(att_db_km, 3),
                        "reflection_db": round(abs(reflection), 3),
                        "comments": event.get("comments", ""),
                        "raw_data": {
                            "slope_raw": event.get("slope_raw", 0),
                            "splice_raw": event.get("splice_raw", 0),
                            "reflection_raw": reflection
                        }
                    }
                    
                    # Add format 2 specific fields if available
                    if "end_prev" in event:
                        event_data["format_2_fields"] = {
                            "end_prev_km": round(event.get("end_prev", 0), 6),
                            "start_curr_km": round(event.get("start_curr", 0), 6),
                            "end_curr_km": round(event.get("end_curr", 0), 6),
                            "start_next_km": round(event.get("start_next", 0), 6),
                            "peak_pos_km": round(event.get("peak_pos", 0), 6)
                        }
                    
                    export_data["events"].append(event_data)
            
            # Fiber Summary
            if len(self.events) > 1:
                last_event = self.events[-1]
                total_distance = last_event.get("distance_km", 0)
                
                # Calculate total splice losses
                total_splice_loss = sum(abs(e.get("splice_loss_db", 0)) for e in self.events if abs(e.get("splice_loss_db", 0)) > 0.001)
                
                # Average fiber attenuation
                non_zero_att = [e.get("calculated_attenuation_db_km", 0) for e in self.events if e.get("calculated_attenuation_db_km", 0) > 0]
                avg_attenuation = sum(non_zero_att) / len(non_zero_att) if non_zero_att else 0
                
                # Reflection analysis
                reflections = [e.get('reflection_loss_db', 0) for e in self.events]
                
                export_data["fiber_summary"] = {
                    "total_fiber_length_km": round(total_distance, 6),
                    "number_of_events": len(self.events),
                    "total_splice_loss_db": round(total_splice_loss, 3),
                    "average_fiber_attenuation_db_km": round(avg_attenuation, 3),
                    "strongest_reflection_db": round(min(reflections), 3) if reflections else 0,
                    "weakest_reflection_db": round(max(reflections), 3) if reflections else 0
                }
            
            # Trace Data
            if self.trace_data:
                for point in self.trace_data:
                    trace_point = {
                        "distance_km": round(point['distance_km'], 6),
                        "power_loss_db": round(point['power_loss_db'], 3)
                    }
                    export_data["trace_data"].append(trace_point)
            
            # Write to JSON file
            with open(comprehensive_file, 'w') as f:
                json.dump(export_data, f, indent=2)
            
            print(f"✅ Complete analysis exported to: {comprehensive_file}")
            return comprehensive_file
            
        except Exception as e:
            print(f"❌ Error creating comprehensive JSON: {e}")
            return None
    
    def upload_json_to_backend_legacy(self, json_file):
        """
        Legacy method - now calls global upload function.
        Maintained for backward compatibility.
        """
        return upload_json_to_backend(json_file)
    
    # plot_with_events method removed to disable PNG generation

# ========================================
# DASHBOARD HTTP SERVER
# ========================================

# Global variables for server state
test_in_progress = False
dashboard_communicator = None

if FLASK_AVAILABLE:
    app = Flask(__name__)
    CORS(app)  # Enable CORS for dashboard access
    
    @app.route('/health', methods=['GET'])
    def health_check():
        """Health check endpoint."""
        return jsonify({
            "status": "online",
            "timestamp": datetime.now().isoformat(),
            "gpio_available": GPIO_AVAILABLE,
            "test_in_progress": test_in_progress
        }), 200
    
    @app.route('/trigger', methods=['POST'])
    def trigger_test():
        """Trigger OTDR test sequence."""
        global test_in_progress
        
        if test_in_progress:
            return jsonify({
                "status": "error",
                "message": "Test already in progress"
            }), 409  # Conflict
        
        # Start test in background thread
        test_thread = threading.Thread(target=run_otdr_test_sequence)
        test_thread.daemon = True
        test_thread.start()
        
        return jsonify({
            "status": "success",
            "message": "OTDR test sequence started",
            "timestamp": datetime.now().isoformat()
        }), 200
    
    @app.route('/status', methods=['GET'])
    def get_status():
        """Get current status."""
        if dashboard_communicator:
            return jsonify({
                "status": dashboard_communicator.get_current_status(),
                "history": dashboard_communicator.get_status_history(),
                "test_in_progress": test_in_progress
            }), 200
        else:
            return jsonify({
                "status": "IDLE",
                "test_in_progress": test_in_progress
            }), 200
    
    @app.route('/stop', methods=['POST'])
    def stop_test():
        """Stop current test (emergency stop)."""
        global test_in_progress
        test_in_progress = False
        
        # Cleanup GPIO if available
        if GPIO_AVAILABLE:
            try:
                GPIO.output(RELAY_PIN, GPIO.LOW)  # Relay OFF (unbound)
                GPIO.output(SOLENOID_POWER_PIN, GPIO.HIGH)  # Solenoid OFF
                GPIO.output(SOLENOID_TEST_PIN, GPIO.HIGH)  # Solenoid OFF
            except:
                pass
        
        return jsonify({
            "status": "success",
            "message": "Test stopped"
        }), 200

def start_dashboard_server():
    """Start Flask dashboard server in background thread."""
    if not FLASK_AVAILABLE:
        print("⚠️  Flask not available - dashboard server disabled")
        print("   Install with: pip3 install Flask Flask-CORS")
        return
    
    if not ENABLE_DASHBOARD_SERVER:
        print("ℹ️  Dashboard server disabled in configuration")
        return
    
    print("\n" + "="*60)
    print("🌐 DASHBOARD SERVER STARTING")
    print("="*60)
    print(f"📡 Listening on: http://0.0.0.0:{DASHBOARD_PORT}")
    print(f"\nEndpoints:")
    print(f"  POST /trigger  → Start OTDR test")
    print(f"  GET  /status   → Get current status")
    print(f"  GET  /health   → Health check")
    print(f"  POST /stop     → Emergency stop")
    print("="*60 + "\n")
    
    try:
        app.run(host='0.0.0.0', port=DASHBOARD_PORT, debug=False, use_reloader=False)
    except Exception as e:
        print(f"❌ Dashboard server failed to start: {e}")

# ========================================
# MAIN AUTOMATION ORCHESTRATION
# ========================================
def run_otdr_test_sequence():
    """
    Main automation function - executes complete OTDR test cycle.
    
    Workflow Sequence:
    1. Init: GPIO2 LOW, GPIO17 HIGH, GPIO18 HIGH
    2. Power ON: GPIO17 LOW, hold 5s, HIGH
    3. Test: GPIO18 LOW, hold 3s, HIGH
    4. Wait: 30s for test completion
    5. Bind: GPIO2 HIGH
    6. Mount Delay: 15s delay
    7. Process files: Conversion and push links
    8. Power OFF: GPIO17 LOW, hold 5s, HIGH
    9. Reset to Initial State: GPIO2 LOW, GPIO17 HIGH, GPIO18 HIGH
    
    Returns:
        bool: True if successful, False otherwise
    """
    global test_in_progress
    
    test_in_progress = True
    gpio = GPIOController()
    
    try:
        # STEP 1: Initialize GPIO pins
        print("📡 [INITIALIZING] Setting up GPIO pins...")
        print("  -> GPIO2 LOW (unbound)")
        print("  -> GPIO17 HIGH (Power solenoid off)")
        print("  -> GPIO18 HIGH (Test solenoid off)")
        if not gpio.setup_gpio():
            print("❌ [ERROR] GPIO initialization failed")
            return False
        
        # STEP 2: Power ON OTDR
        print("📡 [POWERING_ON] Activating OTDR power button...")
        if not gpio.solenoid_pulse(SOLENOID_POWER_PIN, POWER_ON_DURATION, "Power ON"):
            print("❌ [ERROR] Failed to power ON OTDR")
            gpio.cleanup_gpio()
            return False
            
        print("📡 [WAITING] 5s delay before test trigger...")
        time.sleep(5)
            
        # STEP 3: Start test
        print("📡 [TESTING] Starting OTDR test sequence...")
        if not gpio.solenoid_pulse(SOLENOID_TEST_PIN, TEST_TRIGGER_DURATION, "Trigger TEST"):
            print("❌ [ERROR] Failed to trigger test")
            gpio.cleanup_gpio()
            return False
            
        # STEP 4: Delay for test completion
        print(f"📡 [TESTING] Waiting {TEST_COMPLETION_WAIT}s for test to complete...")
        time.sleep(TEST_COMPLETION_WAIT)
        
        # STEP 5: Bind OTDR
        print("📡 [BINDING] Connecting OTDR to Pi...")
        if not gpio.relay_control(bind_otdr=True):
            print("❌ [ERROR] Failed to bind OTDR")
            gpio.cleanup_gpio()
            return False
            
        # STEP 6: Delay to mount
        print(f"📡 [MONITORING] Waiting {BIND_DELAY}s for OTDR storage to mount...")
        time.sleep(BIND_DELAY)
        
        # STEP 7: Access files, convert, and push
        print("📡 [MONITORING] Checking for new OTDR files...")
        file_handler = SORFileHandler(JSON_OUTPUT_PATH, upload_json_to_backend)
        latest_folder = file_handler.find_latest_date_folder()
        if not latest_folder:
            print("⚠️ [WARNING] No date folders found in OTDR data path")
        else:
            unconverted = file_handler.find_unconverted_files(latest_folder)
            if not unconverted:
                print("⚠️ [WARNING] No new .sor files found")
            else:
                print(f"📡 [CONVERTING] Found {len(unconverted)} new file(s)")
                success_count = 0
                for sor_file in unconverted:
                    print(f"📡 [CONVERTING] Processing {os.path.basename(sor_file)}...")
                    if file_handler.convert_and_upload(sor_file):
                        success_count += 1
                print(f"📡 [UPLOADING] Successfully processed {success_count}/{len(unconverted)} files")
                
        print("📡 [UNBINDING] Disconnecting OTDR before power off...")
        gpio.relay_control(bind_otdr=False)
        print("📡 [WAITING] 5s delay before power off...")
        time.sleep(5)
                
        # STEP 8: Power OFF OTDR
        print("📡 [POWERING_OFF] Shutting down OTDR...")
        if not gpio.solenoid_pulse(SOLENOID_POWER_PIN, POWER_OFF_DURATION, "Power OFF"):
            print("⚠️ [WARNING] Failed to power OFF OTDR")
            
        # STEP 9: Reset to initial state
        print("📡 [RESET] Resetting to initial state...")
        print("  -> GPIO2 LOW (unbound)")
        print("  -> GPIO17 HIGH (Power solenoid off)")
        print("  -> GPIO18 HIGH (Test solenoid off)")
        gpio.relay_control(bind_otdr=False)
        if GPIO_AVAILABLE:
            try:
                GPIO.output(SOLENOID_POWER_PIN, GPIO.HIGH)
                GPIO.output(SOLENOID_TEST_PIN, GPIO.HIGH)
            except Exception as e:
                print(f"⚠️  Resetting pins failed: {e}")
                
        print("📡 [COMPLETE] OTDR test cycle completed successfully ✅")
        gpio.cleanup_gpio()
        test_in_progress = False
        return True
        
    except KeyboardInterrupt:
        print("📡 [INTERRUPTED] Test sequence interrupted by user")
        gpio.cleanup_gpio()
        test_in_progress = False
        return False
        
    except Exception as e:
        print(f"❌ [ERROR] Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        gpio.cleanup_gpio()
        test_in_progress = False
        return False


def upload_json_to_backend(json_file_path):
    """
    Upload JSON file to backend database.
    Args:
        json_file_path (str): Path to JSON file
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Read JSON file
        with open(json_file_path, 'r') as f:
            json_data = json.load(f)
        url = "https://test.api.thejotech.com/api/meta/postdata"
        headers = {
            "Content-Type": "application/json"
        }
        # If you have a token, add it here (uncomment and set value if needed)
        # headers["Authorization"] = f"Bearer {YOUR_TOKEN}"
        if CUSTOM_HEADERS:
            headers.update(CUSTOM_HEADERS)
        print(f"📤 Uploading to: {url}")
        response = requests.post(url, json=json_data, headers=headers, timeout=30)
        if response.status_code in (200, 201):
            print(f"✅ Upload successful! Response: {response.text}")
            return True
        else:
            print(f"❌ Upload failed: {response.status_code}")
            print(f"Response: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Upload error: {e}")
        import traceback
        traceback.print_exc()
        return False


def manual_parse_mode():
    """
    Manual mode - parse single .sor file (original functionality).
    Usage: python main.py <sor_file>
    """
    import sys
    
    if len(sys.argv) != 2:
        print("Usage: python main.py <sor_file>")
        return
    
    sor_file = sys.argv[1]
    
    if not os.path.exists(sor_file):
        print(f"❌ File '{sor_file}' not found")
        return
    
    print("🔍 MANUAL OTDR PARSER MODE")
    print("Complete analysis with backend database integration")
    print("=" * 60)
    
    parser = MainOTDRParser(sor_file)
    
    if parser.parse_sor_file():
        print("\n📊 PARSING COMPLETE!")
        print("=" * 40)
        
        # Export comprehensive JSON file
        json_file = parser.export_comprehensive_json()
        
        # Upload to backend
        if json_file:
            print("\n🗄️ UPLOADING TO BACKEND...")
            print("=" * 40)
            upload_json_to_backend(json_file)
        
    # Create enhanced plot
    # parser.plot_with_events()  # removed to disable PNG generation
        
        # Print summary
        print(f"\n📋 ANALYSIS SUMMARY:")
        print("-" * 30)
        
        if parser.events:
            print(f"✅ {len(parser.events)} events detected:")
            for i, event in enumerate(parser.events, 1):
                dist = event.get('distance_km', 0)
                event_type = event.get('event_type', 'Unknown')
                loss = event.get('splice_loss_db', 0)
                refl = event.get('reflection_loss_db', 0)
                print(f"  {i}. {event_type} at {dist:.6f} km (Loss: {loss:.3f} dB, Refl: {refl:.3f} dB)")
        else:
            print("No events detected")
        
        if parser.trace_data:
            print(f"✅ {len(parser.trace_data)} trace data points")
            print(f"✅ Range: 0 to {max(p['distance_km'] for p in parser.trace_data):.6f} km")
        
        print(f"\n🎯 Files generated:")
        base_name = os.path.splitext(sor_file)[0]
        print(f"  - {base_name}_complete_analysis.json")
        print(f"  - {base_name}_complete_analysis.csv")
        print(f"  - Backend database uploaded ✅")
        
    else:
        print("❌ Failed to parse SOR file")


def automated_mode():
    """
    Automated mode - run full OTDR test sequence with GPIO control.
    This is the primary mode for Raspberry Pi deployment.
    """
    print("🤖 AUTOMATED OTDR TEST SYSTEM")
    print("=" * 60)
    print("⚙️  GPIO Control: " + ("✅ Active" if GPIO_AVAILABLE else "❌ Simulation"))
    print("📁 OTDR Data Path: " + OTDR_DATA_PATH)
    print("📄 JSON Output Path: " + JSON_OUTPUT_PATH)
    print("🌐 Server: " + ("LOCAL" if USE_LOCAL_SERVER else "COMPANY"))
    print("=" * 60)
    print("\n🚀 Starting automated test sequence...\n")
    
    success = run_otdr_test_sequence()
    
    if success:
        print("\n" + "=" * 60)
        print("✅ AUTOMATED TEST CYCLE COMPLETED SUCCESSFULLY")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("❌ AUTOMATED TEST CYCLE FAILED")
        print("=" * 60)
    
    return success


def main():
    """
    Main entry point - determines mode based on command line arguments.
    
    Usage:
        python main.py                  # Server mode (wait for dashboard commands)
        python main.py <file.sor>       # Manual mode (parse single file)
        python main.py --auto           # Automated mode (run once)
        python main.py --server         # Server mode (explicit)
        python main.py --help           # Show help
    """
    import sys
    
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] == "--server"):
        # No arguments or --server flag: run dashboard server
        print("\n🌐 Starting in SERVER mode - waiting for dashboard commands...\n")
        start_dashboard_server()
        
    elif len(sys.argv) == 2 and sys.argv[1] == "--auto":
        # --auto flag: run automated mode once
        automated_mode()
        
    elif len(sys.argv) == 2 and sys.argv[1] == "--help":
        print("🔧 OTDR Automation System - Usage Guide")
        print("=" * 60)
        print("\nModes of Operation:")
        print("  1. SERVER MODE (Production - Raspberry Pi)")
        print("     python main.py")
        print("     python main.py --server")
        print("     • Starts HTTP server on port 5000")
        print("     • Waits for dashboard commands")
        print("     • Runs continuously until stopped")
        print("     • Primary mode for production deployment")
        print("\n  2. AUTOMATED MODE (Single Test)")
        print("     python main.py --auto")
        print("     • Runs full GPIO-controlled test sequence once")
        print("     • Powers OTDR, runs test, uploads results")
        print("     • Exits after completion")
        print("     • Good for testing")
        print("\n  3. MANUAL PARSE MODE (Development/Testing)")
        print("     python main.py <file.sor>")
        print("     • Parse single OTDR file")
        print("     • Generate JSON, CSV, plots")
        print("     • Upload to backend database")
        print("\nDashboard API Endpoints:")
        print("  POST http://raspberry-pi-ip:5000/trigger  → Start test")
        print("  GET  http://raspberry-pi-ip:5000/status   → Get status")
        print("  GET  http://raspberry-pi-ip:5000/health   → Health check")
        print("  POST http://raspberry-pi-ip:5000/stop     → Emergency stop")
        print("\nConfiguration:")
        print("  • Edit GPIO pins in CONFIGURATION section")
        print("  • Set USE_LOCAL_SERVER = True/False")
        print("  • Update DASHBOARD_URL for status updates")
        print("  • Update server URLs and tokens")
        print("\nHardware Setup:")
        print("  • GPIO 3  → Relay (USB power)")
        print("  • GPIO 18 → Solenoid (Power button)")
        print("  • GPIO 17 → Solenoid (Test button)")
        print("=" * 60)
        
    elif len(sys.argv) == 2:
        # Single argument: assume it's a .sor file
        manual_parse_mode()
        
    else:
        print("❌ Invalid arguments")
        print("Use 'python main.py --help' for usage information")


if __name__ == "__main__":
    main()
