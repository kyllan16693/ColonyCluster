# shared_utils.py
import math
import json

# --- Constants ---
# World & Display
WORLD_WIDTH = 1400
WORLD_HEIGHT = 800
COLONY_RADIUS = 15
FOOD_RADIUS = 20
ANT_RADIUS = 4
UPDATE_DELAY_MS = 150 # Server GUI update rate

# Simulation & Ant Behavior
ANT_SPEED = 2.0
MAX_ANTS_PER_COLONY = 10 # Reduced default for network traffic
MAX_FOOD_SOURCES = 1 # Maximum number of food sources allowed at one time
INITIAL_FOOD_AMOUNT = 250 # Amount of food in each food source when created
FOOD_TAKE_AMOUNT = 1

# Ant Movement Parameters
DIRECTION_STEPS = 5
DIRECTION_CHANGE_ANGLE = 30
MAX_DIRECTION_ATTEMPTS = 5
INTERACTION_RADIUS_SQ = (FOOD_RADIUS + ANT_RADIUS)**2
COLONY_INTERACTION_RADIUS_SQ = (COLONY_RADIUS + ANT_RADIUS * 2)
RANDOM_WALK_STRENGTH = 0.5
FOOD_BIAS_STRENGTH = 1.0
COLONY_BIAS_STRENGTH = 1.5
TRAIL_TIMEOUT = 30.0 # Seconds before known food location is forgotten by colony

# Pheromone Trail Parameters
TRAIL_POINT_DISTANCE = 10
MAX_TRAIL_POINTS = 100
TRAIL_DECAY_RATE = 0.008 # Slightly faster decay
TRAIL_STRENGTH_INITIAL = 1.0
TRAIL_DETECTION_RADIUS = 15
TRAIL_FOLLOW_CHANCE = 0.8
PATH_INTERSECTION_RADIUS = 10

# Path Memory Parameters
REUSE_SUCCESSFUL_PATH = True
PATH_MEMORY_LENGTH = 150
SHORTER_PATH_THRESHOLD = 0.8

# Ant States
STATE_WANDERING = 0
STATE_RETURNING = 1
STATE_GOING_TO_FOOD = 2  # New state: Actively following colony-provided path
STATE_WAITING_FOR_INSTRUCTIONS = 3 # New: At colony, waiting for task

# Timeout Parameters
WANDER_TIMEOUT_MIN = 60
WANDER_TIMEOUT_MAX = 120
COLONY_PATH_SHARING = True # Within a client's ants

# --- Network Settings ---
SERVER_HOST = '0.0.0.0' # Listen on all available interfaces
SERVER_PORT_BASE = 23000 # Base port for the server listener
MAX_CLIENTS = 7 # Max colonies
HEADER_LENGTH = 10 # Fixed length for message size header

# --- Message Types (Client -> Server) ---
MSG_C_CONNECT_REQUEST = "CONNECT_REQ"
MSG_C_ANT_UPDATES = "ANT_UPDATES"
MSG_C_NEW_TRAILS = "NEW_TRAILS" # Can bundle with ANT_UPDATES

# --- Message Types (Server -> Client) ---
MSG_S_WELCOME = "WELCOME" # Response to connect, assigns ID, color, initial state
MSG_S_WORLD_UPDATE = "WORLD_UPDATE" # Regular state broadcast
MSG_S_FOOD_DEPLETED = "FOOD_DEPLETED"
MSG_S_NEW_FOOD = "NEW_FOOD"
MSG_S_ASSIGN_COLOR = "ASSIGN_COLOR" # Included in WELCOME
MSG_S_KICK = "KICK" # Force disconnect
MSG_S_SHUTDOWN = "SHUTDOWN" # Server telling clients it's closing

# --- Data Keys ---
KEY_MSG_TYPE = "type"
KEY_DATA = "data"
KEY_COLONY_ID = "colony_id"
KEY_COLOR = "color"
KEY_ANTS = "ants"
KEY_ANT_ID = "id"
KEY_X = "x"
KEY_Y = "y"
KEY_STATE = "state"
KEY_HAS_FOOD = "has_food"
KEY_FOOD_SOURCES = "food_sources"
KEY_FOOD_ID = "food_id"
KEY_FOOD_AMOUNT = "amount"
KEY_FOOD_TAKEN = "food_taken" # Amount taken by this colony this step
KEY_FOOD_DROPPED = "food_dropped" # Amount dropped by this colony this step
KEY_COLONIES = "colonies" # List of colony info (id, x, y, color, score)
KEY_TRAILS = "trails" # List of trail data dictionaries
KEY_TIMESTAMP = "timestamp"
KEY_SCORE = "score"


# --- Helper Functions ---
def distance_sq(x1, y1, x2, y2):
    """Calculates the squared distance between two points."""
    return (x1 - x2)**2 + (y1 - y2)**2

def normalize_vector(dx, dy):
    """Normalizes a vector (dx, dy) to unit length."""
    length = math.sqrt(dx**2 + dy**2)
    if length == 0:
        return 0, 0
    return dx / length, dy / length

# --- Network Message Handling ---
def encode_message(msg_type, data):
    """Encodes a message type and data dict into JSON bytes with a header."""
    payload = {KEY_MSG_TYPE: msg_type, KEY_DATA: data}
    json_payload = json.dumps(payload).encode('utf-8')
    header = f"{len(json_payload):<{HEADER_LENGTH}}".encode('utf-8')
    return header + json_payload

def decode_message(header, payload_bytes):
    """Decodes JSON bytes back into a message type and data dict."""
    try:
        # Validate header if needed (e.g., check length matches expected)
        json_payload = payload_bytes.decode('utf-8')
        payload = json.loads(json_payload)
        
        # Special handling for SHUTDOWN message format which might be differently structured
        if '"type": "SHUTDOWN"' in json_payload:
            return "SHUTDOWN", {}
            
        msg_type = payload.get(KEY_MSG_TYPE)
        data = payload.get(KEY_DATA)
        return msg_type, data
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as e:
        print(f"Error decoding message: {e}")
        print(f"Header: {header}, Payload Bytes: {payload_bytes[:100]}...") # Log problematic data
        
        # Additional check for SHUTDOWN in raw bytes as a fallback
        if b'"type": "SHUTDOWN"' in payload_bytes:
            print("Detected SHUTDOWN message in raw payload")
            return "SHUTDOWN", {}
            
        return None, None

# Assign colors cyclically
COLONY_COLORS = ['#FF0000', '#0000FF', '#00AA00', '#FF9900', '#FF00FF', '#00FFFF', '#AA00AA', '#FFA500']
def get_colony_color(colony_id):
    return COLONY_COLORS[colony_id % len(COLONY_COLORS)]

# UI Colors for worker status (kept for server display concept)
STATUS_COLORS = {
    'connecting': 'orange',
    'connected': 'lime green',
    'active': 'yellow',
    'error': 'red',
    'disconnected': 'gray'
}
