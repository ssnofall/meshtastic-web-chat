from flask import Flask, render_template, request, redirect, url_for, jsonify, session, Response
from meshtastic.serial_interface import SerialInterface
from pubsub import pub
import re
import threading
import time
import os
import queue
import glob
from collections import deque
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
import bleach
import logging

app = Flask(__name__)
# Fix Issue #1: Secure secret key from environment
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32))

# Fix Issue #5: CSRF Protection
csrf = CSRFProtect(app)

# Fix Issue #10: Rate Limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Fix Issue #8: Thread-safe message storage using deque and lock
chat_messages = deque(maxlen=100)
messages_lock = threading.Lock()

# Fix Issue #9: Track SSE clients for proper cleanup
class SSEClient:
    def __init__(self):
        self.queue = queue.Queue(maxsize=50)
        self.last_index = 0
        
sse_clients = []
sse_clients_lock = threading.Lock()

# Meshtastic interface management with hotswap support
interface = None
interface_lock = threading.Lock()
current_device = None
failed_messages = queue.Queue()
device_monitor_thread = None
message_retry_thread = None
is_shutting_down = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_available_devices():
    """Scan for available Meshtastic serial devices"""
    if os.name == 'nt':  # Windows
        import serial.tools.list_ports
        return sorted([port.device for port in serial.tools.list_ports.comports()])
    
    # Linux / Mac
    devices = []
    patterns = [
        '/dev/ttyUSB*',
        '/dev/ttyACM*',
        '/dev/cu.usbserial*',
        '/dev/cu.usbmodem*',
        '/dev/cu.SLAB_USBtoUART*',
        '/dev/cu.wchusbserial*',
    ]
    for pattern in patterns:
        devices.extend(glob.glob(pattern))
    return sorted(devices)

def init_interface(device_path=None):
    """Initialize or reinitialize the Meshtastic interface"""
    global interface, current_device
    
    with interface_lock:
        # Close existing interface gracefully
        if interface:
            try:
                # Unsubscribe from pubsub to avoid duplicate handlers
                pub.unsubscribe(on_receive, "meshtastic.receive")
                interface.close()
                logger.info(f"Closed connection to {current_device}")
            except Exception as e:
                logger.error(f"Error closing interface: {e}")
            interface = None
            current_device = None
        
        # Try to connect to a device
        if device_path:
            devices_to_try = [device_path]
        else:
            devices_to_try = get_available_devices()
        
        for device in devices_to_try:
            try:
                logger.info(f"Attempting to connect to {device}")
                new_interface = SerialInterface(device)
                
                # Test if the interface is actually working
                time.sleep(2)  # Give it time to initialize
                
                interface = new_interface
                current_device = device
                
                # Subscribe to messages
                pub.subscribe(on_receive, "meshtastic.receive")
                
                logger.info(f"Successfully connected to {device}")
                return True
                
            except Exception as e:
                logger.error(f"Failed to connect to {device}: {e}")
                continue
        
        logger.error("No Meshtastic devices available")
        return False

def send_message_safe(message):
    """Thread-safe message sending with retry queue"""
    global interface, current_device
    
    with interface_lock:
        if not interface or not current_device:
            logger.warning("No active interface, queuing message")
            failed_messages.put(message)
            return False
        
        try:
            interface.sendText(message)
            logger.info(f"Sent message via {current_device}: {message[:50]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            failed_messages.put(message)
            # Trigger reconnection
            threading.Thread(target=attempt_reconnect, daemon=True).start()
            return False

def attempt_reconnect():
    """Attempt to reconnect to a device"""
    global interface, current_device
    
    logger.info("Attempting to reconnect...")
    
    # Wait a bit before attempting reconnection
    time.sleep(2)
    
    # Try current device first, then others
    devices = get_available_devices()
    
    # Prioritize current device if it's still available
    if current_device and current_device in devices:
        devices.remove(current_device)
        devices.insert(0, current_device)
    
    for device in devices:
        if init_interface(device):
            logger.info(f"Reconnected to {device}")
            # Trigger retry of failed messages
            threading.Thread(target=retry_failed_messages, daemon=True).start()
            return True
    
    logger.error("Reconnection failed, will retry...")
    return False

def retry_failed_messages():
    """Retry sending failed messages from the queue"""
    retry_count = 0
    max_retries_per_batch = 10
    
    while not failed_messages.empty() and retry_count < max_retries_per_batch:
        try:
            message = failed_messages.get_nowait()
            if send_message_safe(message):
                logger.info(f"Successfully retried message: {message[:50]}...")
                retry_count += 1
            else:
                # Put it back if it failed
                failed_messages.put(message)
                break
            time.sleep(1)  # Don't flood the device
        except queue.Empty:
            break

def monitor_device():
    """Background thread to monitor device connection and attempt reconnection"""
    global interface, current_device, is_shutting_down
    
    last_check_time = time.time()
    reconnect_interval = 5  # Check every 5 seconds
    
    while not is_shutting_down:
        time.sleep(1)
        
        current_time = time.time()
        if current_time - last_check_time < reconnect_interval:
            continue
        
        last_check_time = current_time
        
        with interface_lock:
            # Check if interface exists and is responsive
            if not interface or not current_device:
                logger.warning("No active interface detected")
                threading.Thread(target=attempt_reconnect, daemon=True).start()
                continue
            
            # Try to check if device is still connected
            try:
                # Simple check - see if the device path still exists
                available_devices = get_available_devices()
                if current_device not in available_devices:
                    logger.warning(f"Device {current_device} disconnected")
                    interface = None
                    current_device = None
                    threading.Thread(target=attempt_reconnect, daemon=True).start()
            except Exception as e:
                logger.error(f"Error monitoring device: {e}")

def clean_username(name):
    """Fix Issue #4: Sanitize username"""
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '', name)[:20]
    if len(cleaned) < 2:
        return None
    return cleaned

@app.route('/set_username', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def set_username():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        username = clean_username(username)
        if username:
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template('set_username.html', error="Username must be 2-20 alphanumeric characters")
    return render_template('set_username.html')

@app.route('/', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
def index():
    if 'username' not in session:
        return redirect(url_for('set_username'))

    username = session['username']

    if request.method == 'POST':
        msg = request.form.get('message', '').strip()
        if msg:
            # Fix Issue #4: Sanitize message content
            msg = bleach.clean(msg, tags=[], strip=True)[:200]
            
            try:
                full_message = f"{username}: {msg}"
                
                # Add to chat immediately for display
                with messages_lock:
                    chat_messages.append(full_message)
                
                # Broadcast to SSE clients
                broadcast_to_sse_clients(full_message)
                
                # Send via Meshtastic (with retry queue)
                send_message_safe(full_message)
                
            except Exception as e:
                logger.error(f"Error processing message: {e}")
        
        return redirect(url_for('index'))

    return render_template('index_sse.html', username=username)

@app.route('/stream')
@limiter.exempt
def stream():
    """Fix Issue #9: Proper SSE with client cleanup"""
    def event_stream(client):
        try:
            # Send existing messages
            with messages_lock:
                for msg in list(chat_messages):
                    yield f'data: {msg}\n\n'
            
            # Stream new messages
            while True:
                try:
                    # Wait for new message with timeout
                    msg = client.queue.get(timeout=30)
                    yield f'data: {msg}\n\n'
                except queue.Empty:
                    # Send heartbeat to keep connection alive
                    yield ': heartbeat\n\n'
                except GeneratorExit:
                    break
        finally:
            # Cleanup
            with sse_clients_lock:
                if client in sse_clients:
                    sse_clients.remove(client)
    
    # Create new client
    client = SSEClient()
    
    with sse_clients_lock:
        sse_clients.append(client)
    
    return Response(event_stream(client), mimetype="text/event-stream")

def broadcast_to_sse_clients(message):
    """Broadcast message to all connected SSE clients"""
    with sse_clients_lock:
        disconnected_clients = []
        for client in sse_clients:
            try:
                client.queue.put_nowait(message)
            except queue.Full:
                disconnected_clients.append(client)
        
        # Remove disconnected clients
        for client in disconnected_clients:
            sse_clients.remove(client)

@app.route('/device/status')
@limiter.limit("60 per minute")
def device_status():
    """Get current device connection status"""
    with interface_lock:
        node_count = 0
        if interface:
            try:
                node_count = len(interface.nodes) if hasattr(interface, 'nodes') else 0
            except:
                pass
        
        return jsonify({
            'connected': interface is not None and current_device is not None,
            'device': current_device,
            'nodes': node_count,
            'failed_queue': failed_messages.qsize(),
            'available_devices': get_available_devices()
        })

@app.route('/device/reconnect', methods=['POST'])
@limiter.limit("5 per minute")
def force_reconnect():
    """Manually trigger device reconnection"""
    threading.Thread(target=attempt_reconnect, daemon=True).start()
    return jsonify({'status': 'reconnection initiated'})

def on_receive(packet, interface=None):
    """Handle incoming Meshtastic packets"""
    try:
        decoded = packet.get('decoded')
        from_node_id = packet.get('from')
        node_name = None

        # Use the global interface if none provided
        interface_obj = interface if interface else globals().get('interface')
        
        if from_node_id is not None and interface_obj:
            node = interface_obj.nodes.get(from_node_id)
            if node:
                node_name = node.get('user', {}).get('short_name')

        if decoded and 'payload' in decoded:
            payload = decoded['payload']
            try:
                message = payload.decode('utf-8').strip()
                if all(32 <= ord(c) <= 126 or c in '\r\n\t' for c in message):
                    display_name = node_name if node_name else f"Node {from_node_id}"
                    
                    # Fix Issue #4: Sanitize incoming messages
                    message = bleach.clean(message, tags=[], strip=True)
                    
                    full_message = f"{display_name}: {message}"
                    
                    logger.info(f"Received from {display_name}: {message[:50]}...")
                    
                    with messages_lock:
                        chat_messages.append(full_message)
                    
                    # Broadcast to SSE clients
                    broadcast_to_sse_clients(full_message)
                    
            except UnicodeDecodeError:
                pass
    except Exception as e:
        logger.error(f"Error processing packet: {e}")


if __name__ == '__main__':
    print("Starting Meshtastic Web Chat Server with SSE...")
    
    # Initialize first device
    init_interface()
    
    # Start device monitor thread
    device_monitor_thread = threading.Thread(target=monitor_device, daemon=True)
    device_monitor_thread.start()
    
    try:
        app.run(host='0.0.0.0', port=5000, threaded=True)
    finally:
        is_shutting_down = True
        if interface:
            try:
                pub.unsubscribe(on_receive, "meshtastic.receive")
                interface.close()
            except:
                pass
