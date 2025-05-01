# colony_client.py
import socket
import json
import time
import random
import math
import sys
import select
import threading # To handle server messages without blocking ant logic
import queue  # Added import for queue

from food_validator import cached_is_valid_food_source, clear_validation_cache

# Import from shared utils
from shared_utils import (
    ANT_RADIUS, ANT_SPEED, MAX_ANTS_PER_COLONY, FOOD_TAKE_AMOUNT, FOOD_RADIUS,
    DIRECTION_STEPS, DIRECTION_CHANGE_ANGLE, MAX_DIRECTION_ATTEMPTS,
    INTERACTION_RADIUS_SQ, COLONY_INTERACTION_RADIUS_SQ, COLONY_RADIUS,
    TRAIL_POINT_DISTANCE, MAX_TRAIL_POINTS, TRAIL_DECAY_RATE, TRAIL_STRENGTH_INITIAL,
    REUSE_SUCCESSFUL_PATH, PATH_MEMORY_LENGTH, SHORTER_PATH_THRESHOLD,
    STATE_WANDERING, STATE_RETURNING, STATE_GOING_TO_FOOD, STATE_WAITING_FOR_INSTRUCTIONS, WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX, COLONY_PATH_SHARING,
    HEADER_LENGTH, encode_message, decode_message, distance_sq, normalize_vector,
    MSG_C_ANT_UPDATES, MSG_C_NEW_TRAILS,
    MSG_S_WELCOME, MSG_S_WORLD_UPDATE, MSG_S_FOOD_DEPLETED, MSG_S_NEW_FOOD, MSG_S_SHUTDOWN,
    KEY_MSG_TYPE, KEY_DATA, KEY_COLONY_ID, KEY_COLOR, KEY_ANTS, KEY_ANT_ID, KEY_X, KEY_Y,
    KEY_STATE, KEY_HAS_FOOD, KEY_FOOD_SOURCES, KEY_FOOD_ID, KEY_FOOD_AMOUNT,
    KEY_FOOD_TAKEN, KEY_FOOD_DROPPED, KEY_COLONIES, KEY_TRAILS, KEY_TIMESTAMP, PATH_INTERSECTION_RADIUS,
    MAX_FOOD_SOURCES, KEY_SCORE
)

# Client-side constants
FOOD_INTERACTION_RADIUS_SQ = 900  # Square of radius for food interaction (30^2)
FOOD_REUSE_THRESHOLD = 10        # Only reuse path if at least this much food remains

class ColonyClient:
    def __init__(self, server_host, server_port):
        self.server_host = server_host
        self.server_port = server_port
        self.sock = None
        
        # Colony state
        self.colony_id = None
        self.colony_color = None
        self.colony_x = 0.0  # Initialize as float
        self.colony_y = 0.0  # Initialize as float
        self.my_ants = {}  # Dict of all ants belonging to this colony
        self.ant_counter = 0  # Used for generating ant IDs
        self.known_food_paths = {}  # Paths to known food sources
        self.path_stats = {}  # Statistics on path quality
        
        # Food sources and exploration
        self.current_food_sources = {}  # All known food sources
        self.active_food_target = None  # Current food source being targeted
        self.colony_exploration_mode = True  # Whether colony is in exploration mode
        self.colony_food_depletion_tracker = {}  # Track food sources for local depletion detection
        
        # Ant tracking state
        self.ants_assigned_to_food = set() # Ants currently assigned to collect food
        self.ants_assigned_to_explore = set() # Ants exploring for new food
        self.ant_movement_state = {} # Movement parameters for each ant
        
        # World state
        self.world_width = 1000
        self.world_height = 800
        self.other_colonies = {} # Other colonies in the world
        
        # Threading control
        self.network_thread = None
        self.network_running = False
        self.message_queue = queue.Queue()  # Thread-safe queue for network->logic messages
        self.simulation_active = False
        
        # For ant movement 
        self.next_direction_change_times = {}
        
        # Buffered data from network
        self.receive_buffer = b""
        
        # Pending updates to be sent to the server
        self.pending_updates = []
        
        # Pending trails from ants dropping food
        self.pending_trails = []

    def connect(self):
        """Establish connection with the server."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            #print(f"[DEBUG] Attempting to connect to {self.server_host}:{self.server_port}...")
            self.sock.connect((self.server_host, self.server_port))
            self.sock.setblocking(False) # Use non-blocking sockets
            self.network_running = True
            #print(f"[DEBUG] Connection successful! Socket: {self.sock}")

            # Start network listener thread
            self.network_thread = threading.Thread(target=self._network_loop, daemon=True, name="NetworkThread")
            self.network_thread.start()
            #print(f"[DEBUG] Started network thread: {self.network_thread.name}")
            
            # Start ant logic thread
            self.logic_thread = threading.Thread(target=self._ant_logic_loop, daemon=True, name="LogicThread")
            self.logic_thread.start()
            #print(f"[DEBUG] Started logic thread: {self.logic_thread.name}")

            return True
        except ConnectionRefusedError:
            #print(f"[ERROR] Connection refused to {self.server_host}:{self.server_port}")
            return False
        except Exception as e:
            #print(f"[ERROR] Connection error: {e}")
            return False

    def disconnect(self):
        """Cleanly disconnect from the server."""
        self.network_running = False # Signal threads to stop
        if self.sock:
            try:
                self.sock.close()
                #print("Socket closed.")
            except OSError as e:
                 #print(f"Error closing socket: {e}")
                 pass  # Add pass statement to maintain block structure
        self.sock = None

        # Wait for threads to finish
        if self.network_thread and self.network_thread.is_alive():
            self.network_thread.join(timeout=1.0)
        if self.logic_thread and self.logic_thread.is_alive():
            self.logic_thread.join(timeout=1.0)
        #print("Client disconnected.")

        return True

    # --- Network Handling (Runs in network_thread) ---
    def _network_loop(self):
        """Handles sending and receiving data with the server."""
        #print(f"[DEBUG-{threading.current_thread().name}] Network loop starting")
        while self.network_running and self.sock:
            sockets_to_read = [self.sock]
            sockets_to_write = [self.sock] if self.pending_updates else []

            try:
                readable, writable, exceptional = select.select(
                    sockets_to_read,
                    sockets_to_write,
                    sockets_to_read, # Also check for errors on the socket
                    0.1 # Timeout
                )

                # Handle received data
                for sock in readable:
                    try:
                        data = sock.recv(4096)
                        if data:
                             #print(f"[DEBUG-{threading.current_thread().name}] Received {len(data)} bytes of data")
                             self.receive_buffer += data
                             self._process_received_data()
                        else:
                            # Server closed connection gracefully
                            #print(f"[DEBUG-{threading.current_thread().name}] Server closed the connection (zero bytes received)")
                            self.network_running = False # Signal main loop/other threads
                            break # Exit inner loop
                    except (ConnectionResetError, OSError) as e:
                        #print(f"[ERROR-{threading.current_thread().name}] Network read error: {e}")
                        self.network_running = False
                        break # Exit inner loop
                    except BlockingIOError:
                         pass # No data available right now, that's fine

                if not self.network_running: break # Exit outer loop if disconnected

                # Handle sending data
                for sock in writable:
                    try:
                        if self.pending_updates:
                            next_msg = self.pending_updates.pop(0) # Get first message
                            
                            # Ensure next_msg is bytes - try to encode if it's a dict
                            if isinstance(next_msg, dict):
                                #print(f"[WARNING-{threading.current_thread().name}] Found unencoded dict in pending_updates. Attempting to encode.")
                                try:
                                    # Since we don't know the message type, we'll use a generic type
                                    next_msg = encode_message(MSG_C_ANT_UPDATES, next_msg)
                                except Exception as e:
                                    #print(f"[ERROR-{threading.current_thread().name}] Failed to encode dict message: {e}")
                                    continue  # Skip this message and move to the next
                            
                            if not isinstance(next_msg, bytes):
                                #print(f"[ERROR-{threading.current_thread().name}] Invalid message type in pending_updates: {type(next_msg)}. Skipping.")
                                continue  # Skip this message and move to the next
                                
                            #print(f"[DEBUG-{threading.current_thread().name}] Sending message of {len(next_msg)} bytes")
                            sent = sock.send(next_msg)
                            if sent < len(next_msg):
                                # Didn't send full message, put remainder back at front
                                self.pending_updates.insert(0, next_msg[sent:])
                                #print(f"[DEBUG-{threading.current_thread().name}] Partial send ({sent}/{len(next_msg)} bytes). Will retry.")
                            else:
                                #print(f"[DEBUG-{threading.current_thread().name}] Successfully sent {sent} bytes")
                                pass  # Added pass to fix empty else block
                    except (ConnectionResetError, BrokenPipeError, OSError) as e:
                        #print(f"[ERROR-{threading.current_thread().name}] Network write error: {e}")
                        self.network_running = False
                        break # Exit inner loop
                    except IndexError:
                        pass # Send buffer became empty between check and pop

                if not self.network_running: break # Exit outer loop if disconnected

                # Handle exceptional conditions (errors)
                for sock in exceptional:
                    #print(f"[ERROR-{threading.current_thread().name}] Socket exceptional condition.")
                    self.network_running = False
                    break # Exit inner loop

            except ValueError: # Socket closed?
                 #print(f"[ERROR-{threading.current_thread().name}] Network loop: Socket seems closed.")
                 self.network_running = False
            except Exception as e:
                #print(f"[ERROR-{threading.current_thread().name}] Unexpected error in network loop: {e}")
                import traceback
                traceback.print_exc()
                self.network_running = False

        #print(f"[DEBUG-{threading.current_thread().name}] Network loop finished.")
        if self.network_running: # If loop exited unexpectedly while running=True
             self.disconnect()


    def _process_received_data(self):
        """Process the receive buffer for complete messages."""
        #print(f"[DEBUG-{threading.current_thread().name}] Processing received data buffer of {len(self.receive_buffer)} bytes")
        while len(self.receive_buffer) >= HEADER_LENGTH:
            header = self.receive_buffer[:HEADER_LENGTH]
            try:
                msg_len = int(header.decode('utf-8').strip())
                #print(f"[DEBUG-{threading.current_thread().name}] Message header indicates {msg_len} bytes payload")
            except ValueError:
                #print(f"[ERROR-{threading.current_thread().name}] Invalid header received: {header}. Clearing buffer.")
                self.receive_buffer = b'' # Clear potentially corrupted buffer
                return

            if len(self.receive_buffer) < HEADER_LENGTH + msg_len:
                #print(f"[DEBUG-{threading.current_thread().name}] Not enough data yet. Have {len(self.receive_buffer)} bytes, need {HEADER_LENGTH + msg_len}")
                # Not enough data for the full message yet
                break

            # We have a complete message
            payload_bytes = self.receive_buffer[HEADER_LENGTH : HEADER_LENGTH + msg_len]
            self.receive_buffer = self.receive_buffer[HEADER_LENGTH + msg_len:] # Consume message
            #print(f"[DEBUG-{threading.current_thread().name}] Extracted complete message of {msg_len} bytes, {len(self.receive_buffer)} bytes remaining in buffer")

            msg_type, data = decode_message(header, payload_bytes)

            if msg_type and data:
                #print(f"[DEBUG-{threading.current_thread().name}] Successfully decoded message of type: {msg_type}")
                self._handle_server_message(msg_type, data)
            else:
                #print(f"[ERROR-{threading.current_thread().name}] Failed to decode server message. Payload snippet: {payload_bytes[:100]}")
                pass  # Added pass to fix empty else block

    def _handle_server_message(self, msg_type, data):
        """Process a message received from the server."""
        try:
            # Handle WELCOME immediately to get ID, color, initial state needed for setup
            if msg_type == MSG_S_WELCOME:
                # Store essential data immediately for initial setup
                self.colony_id = data.get(KEY_COLONY_ID, 0)
                self.colony_color = data.get(KEY_COLOR, '#FF0000')  # Default red if not provided
                
                # Store world dimensions
                self.world_width = data.get('world_width', 1000)
                self.world_height = data.get('height', 800)
                
                #print(f"[DEBUG] WELCOME message colony field KEY_COLONY_ID={KEY_COLONY_ID}, received value={self.colony_id}")
                
                # Store colony position - ensure coordinates are floating point numbers
                if KEY_COLONIES in data and data[KEY_COLONIES]:
                    #print(f"[DEBUG] WELCOME contains {len(data[KEY_COLONIES])} colonies: {data[KEY_COLONIES]}")
                    for colony in data[KEY_COLONIES]:
                        colony_id_in_list = colony.get('id')
                        #print(f"[DEBUG] Checking colony in list with id={colony_id_in_list}, my id={self.colony_id}, match: {colony_id_in_list == self.colony_id}")
                        if colony_id_in_list == self.colony_id:
                            # Explicitly convert to float to ensure correct positioning
                            self.colony_x = float(colony.get(KEY_X, self.world_width/2))
                            self.colony_y = float(colony.get(KEY_Y, self.world_height/2))
                            #print(f"🏠 Colony {self.colony_id} position: ({self.colony_x}, {self.colony_y})")
                            break
                    else:  # No matching colony found
                        # Default to center of world if not specified
                        self.colony_x = float(self.world_width/2)
                        self.colony_y = float(self.world_height/2)
                        #print(f"🏠 Colony {self.colony_id} position defaulting to center: ({self.colony_x}, {self.colony_y})")
                else:
                    # Default to center of world if not specified
                    self.colony_x = float(self.world_width/2)
                    self.colony_y = float(self.world_height/2)
                    #print(f"🏠 Colony {self.colony_id} position defaulting to center: ({self.colony_x}, {self.colony_y})")
                
                #print(f"Received welcome message. Colony ID: {self.colony_id}, Color: {self.colony_color}")
                #print(f"World dimensions: {self.world_width}x{self.world_height}")
                
                # Add debug logging for colony position verification
                #print(f"[DEBUG] Colony {self.colony_id} position confirmed BEFORE ant init: ({self.colony_x}, {self.colony_y})")
                #print(f"[DEBUG] World dimensions received: {self.world_width}x{self.world_height}")
                
                # Put the welcome message on the queue for the logic thread to process
                #print(f"[NetworkThread] Queuing WELCOME message for logic thread")
                self.message_queue.put((msg_type, data))
                
            # Put other message types on the queue for the logic thread to process
            elif msg_type in [MSG_S_WORLD_UPDATE, MSG_S_FOOD_DEPLETED, MSG_S_NEW_FOOD, MSG_S_SHUTDOWN]:
                #print(f"[NetworkThread] Queuing message type: {msg_type}")
                self.message_queue.put((msg_type, data))
                
            else:
                #print(f"[NetworkThread] Received unknown message type: {msg_type}")
                pass  # Added pass to fix empty else block
                
        except Exception as e:
            #print(f"[NetworkThread] Error handling server message: {e}")
            import traceback
            traceback.print_exc()

    def _filter_to_single_food_source(self, food_sources):
        """Filter food sources to respect MAX_FOOD_SOURCES from shared_utils.
        When MAX_FOOD_SOURCES is 1, only keep the nearest food source to the colony.
        Otherwise, return all valid food sources up to MAX_FOOD_SOURCES."""
        try:
            if not food_sources:
                return {}
                
            # First, filter out any depleted or invalid food sources
            valid_sources = {}
            for food_id, food in food_sources.items():
                try:
                    # Skip depleted or invalid food sources
                    if food.get('depleted', False) or food.get('amount', 0) <= 0:
                        continue
                    # Ensure the food source has all required fields
                    if not all(key in food for key in [KEY_X, KEY_Y, 'amount']):
                        #print(f"WARNING: Food source {food_id} missing required fields, skipping")
                        continue
                    valid_sources[food_id] = food
                except Exception as e:
                    #print(f"ERROR processing food source {food_id}: {e}")
                    continue
                
            # If no limit on food sources or we have fewer than the max, return all valid sources
            if MAX_FOOD_SOURCES <= 0 or len(valid_sources) <= MAX_FOOD_SOURCES:
                if valid_sources:
                    #print(f"Using all {len(valid_sources)} valid food sources")
                    pass  # Added pass statement
                else:
                    #print("No valid food sources found")
                    pass  # Added pass to fix empty else block
                return valid_sources
                
            # If we need to filter down to MAX_FOOD_SOURCES, prioritize by distance
            closest_sources = {}
            sources_with_distances = []
            
            for food_id, food in valid_sources.items():
                try:
                    dist_sq = distance_sq(self.colony_x, self.colony_y, food[KEY_X], food[KEY_Y])
                    sources_with_distances.append((food_id, food, dist_sq))
                except Exception as e:
                    #print(f"ERROR calculating distance to food {food_id}: {e}")
                    continue
            
            # Sort by distance (closest first)
            sources_with_distances.sort(key=lambda x: x[2])
            
            # Take only up to MAX_FOOD_SOURCES
            for i in range(min(MAX_FOOD_SOURCES, len(sources_with_distances))):
                food_id, food, _ = sources_with_distances[i]
                closest_sources[food_id] = food
            
            #print(f"Selected {len(closest_sources)}/{len(valid_sources)} food sources based on MAX_FOOD_SOURCES={MAX_FOOD_SOURCES}")
            return closest_sources
        except Exception as e:
            #print(f"ERROR in _filter_to_single_food_source: {str(e)}")
            # Return current food sources if there's an error
            return self.current_food_sources

    def _handle_food_depletion(self, depleted_food_id):
        """Handle local data updates when server notifies food source is depleted."""
        try:
            # Convert to integer if needed
            if isinstance(depleted_food_id, str): depleted_food_id = int(depleted_food_id)
            #print(f"--- Handling Depletion for Food Source: {depleted_food_id} ---")

            # === COMPLETE CLEANUP PROCEDURE (Run Every Time Called) ===

            # 1. Always remove from known paths
            path_removed = False
            if depleted_food_id in self.known_food_paths:
                del self.known_food_paths[depleted_food_id]
                path_removed = True
            #print(f"  🗑️ Known path removed: {path_removed}")

            stats_removed = False
            if depleted_food_id in self.path_stats:
                del self.path_stats[depleted_food_id]
                stats_removed = True
            #print(f"  🗑️ Path stats removed: {stats_removed}")

            # 2. Mark (or re-mark) as depleted in current food sources if it exists
            marked_depleted = False
            if depleted_food_id in self.current_food_sources:
                self.current_food_sources[depleted_food_id]['depleted'] = True
                self.current_food_sources[depleted_food_id]['amount'] = 0
                marked_depleted = True
            #print(f"  🏁 Marked as depleted in current_food_sources: {marked_depleted}")

            # 3. Reset ALL ants that reference this food source in ANY way
            ants_reset_count = 0
            for ant_id, ant_state in list(self.my_ants.items()):
                referenced_food = False
                move_state = self.ant_movement_state.get(ant_id) # Use .get() for safety

                # Check direct target
                if ant_state.get('target_food_id') == depleted_food_id:
                    referenced_food = True

                # Check movement state references
                if move_state:
                    if move_state.get('preferred_food_id') == depleted_food_id:
                        referenced_food = True
                        move_state.pop('preferred_food_id', None) # Clean up reference
                    if move_state.get('last_food_id') == depleted_food_id:
                        # Don't set referenced_food=True here, as this is historical
                        move_state.pop('last_food_id', None) # Just clean up reference
                    if move_state.get('next_target_food_id') == depleted_food_id:
                        referenced_food = True
                        move_state.pop('next_target_food_id', None) # Clean up reference

                # If referenced AND exists in move_state, reset (unless     clear_validation_cache()  # Clear cache after critical food state changes
#returning w/ food)
                if referenced_food and move_state:
                    if ant_state.get('state') != STATE_RETURNING or not ant_state.get('has_food', False):
                        #print(f"    🔄 Resetting ant {ant_id} (State: {ant_state.get('state')}) -> WANDER")
                        ant_state['state'] = STATE_WANDERING
                        ant_state['target_food_id'] = None
                        move_state['path_points'] = [(ant_state['x'], ant_state['y'])]
                        move_state['last_recorded_point'] = (ant_state['x'], ant_state['y'])
                        move_state['current_path_target_index'] = 0
                        move_state['reusing_path'] = False
                        move_state['wander_start_time'] = time.time()
                        move_state['direction'] = random.uniform(0, 2 * math.pi)
                        self._queue_ant_position_update(ant_id)
                        ants_reset_count += 1
                    else:
                         #print(f"    Ant {ant_id} referenced depleted food {depleted_food_id}, but is returning with food. Allowing return.")
                         pass  # Added pass to fix empty else block


            #print(f"  🐜 Reset {ants_reset_count} ants with active reference to depleted food {depleted_food_id}")

            # 4. If this was the active target, select a new one
            if self.active_food_target == depleted_food_id:
                #print(f"  🎯 Active target {depleted_food_id} was depleted. Selecting new target.")
                self.active_food_target = None
                self._select_best_food_target() # This will re-evaluate based on remaining valid sources/paths
            
            #print(f"--- Finished Depletion Handling for Food Source: {depleted_food_id} ---")
            return True
        except Exception as e:
            #print(f"❌❌ ERROR in _handle_food_depletion for {depleted_food_id}: {e} ❌❌")
            import traceback
            traceback.print_exc()
            return False

    def send_message(self, msg_type, data):
        """Encodes and queues a message to be sent."""
        if self.network_running:
            try:
                msg_bytes = encode_message(msg_type, data)
                #print(f"[DEBUG-{threading.current_thread().name}] Queued message of type {msg_type} ({len(msg_bytes)} bytes)")
                self.pending_updates.append(msg_bytes)
            except Exception as e:
                #print(f"[ERROR] Failed to encode message: {e}")
                pass  # Add pass to fix empty except block
        else:
            #print(f"[WARNING-{threading.current_thread().name}] Attempted to send message of type {msg_type} but not connected")
            pass  # Added pass to fix empty else block


    # --- Ant Logic (Runs in logic_thread) ---
    def _initialize_ants(self):
        """Create the initial set of ants for this colony."""
        if not self.my_ants: # Only initialize if empty
            #print(f"Initializing {MAX_ANTS_PER_COLONY} ants for colony {self.colony_id}...")
            
            # Log the exact colony position for debugging
            #print(f"[DEBUG] Colony position for initialization: ({self.colony_x}, {self.colony_y})")
            
            for i in range(MAX_ANTS_PER_COLONY):
                ant_id = f"{self.colony_id}_{i}" # Create unique ID
                self.ant_counter = max(self.ant_counter, i + 1)

                # Start ants near colony center with small random offsets
                offset_angle = random.uniform(0, 2 * math.pi)
                offset_dist = random.uniform(0, COLONY_RADIUS * 0.75)  # Use 75% of radius for better spread
                ax = self.colony_x + offset_dist * math.cos(offset_angle)
                ay = self.colony_y + offset_dist * math.sin(offset_angle)

                #print(f"[DEBUG] Creating ant {ant_id} at pos=({ax}, {ay}) from colony ({self.colony_x}, {self.colony_y})")
                
                self.my_ants[ant_id] = {
                    KEY_ANT_ID: ant_id,
                    KEY_COLONY_ID: self.colony_id, # Include colony ID in ant state
                    KEY_X: ax, KEY_Y: ay,
                    KEY_COLOR: self.colony_color, # Use assigned colony color
                    KEY_STATE: STATE_WANDERING,  # Start wandering
                    KEY_HAS_FOOD: False,
                    'target_food_id': None,    # Which food source this ant is targeting (for STATE_GOING_TO_FOOD)
                    'carrying_food_from': None # Which food source this ant is carrying food from
                }

                # Initialize movement state for the new ant
                self.ant_movement_state[ant_id] = {
                    'direction': random.uniform(0, 2 * math.pi),
                    'steps_in_direction': 0,
                    'direction_changes': 0,
                    'last_recorded_point': (ax, ay), # Start recording path from initial position
                    'path_points': [(ax, ay)], # Stores the path the ant has taken since its last instruction/reset
                    'reusing_path': False, # Will be true when STATE_GOING_TO_FOOD or STATE_RETURNING
                    'wander_start_time': time.time(), # Track wander duration
                    'wander_timeout': random.uniform(WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX),
                    'current_path_target_index': 0 # Index for following path points
                }
                
                # Add to colony exploration set
                self.ants_assigned_to_explore.add(ant_id)
                
            # Set colony to exploration mode initially
            self.colony_exploration_mode = True
            self.active_food_target = None
            
            # Immediately send an update with all ants to make sure they appear
            initial_ant_updates = []
            for ant_id, ant_state in self.my_ants.items():
                update = {
                    KEY_ANT_ID: ant_id,
                    KEY_X: ant_state['x'],
                    KEY_Y: ant_state['y'],
                    KEY_HAS_FOOD: ant_state.get('has_food', False),
                    KEY_STATE: ant_state.get('state', STATE_WANDERING),  # Make sure state is set
                    KEY_COLOR: self.colony_color,
                    KEY_COLONY_ID: self.colony_id
                }
                initial_ant_updates.append(update)
                
            if initial_ant_updates:
                initial_update_message = {
                    KEY_COLONY_ID: self.colony_id,
                    KEY_ANTS: initial_ant_updates,
                    KEY_FOOD_TAKEN: 0,
                    KEY_FOOD_DROPPED: 0,
                    KEY_TRAILS: []
                }
                self.send_message(MSG_C_ANT_UPDATES, initial_update_message)
                #print(f"[DEBUG] Sent initial positions for {len(initial_ant_updates)} ants")
                
            #print(f"Ant initialization complete for colony {self.colony_id}. Created {len(self.my_ants)} ants at positions:")
            #print(f"All ants assigned to exploration mode. Colony is in exploration mode.")
            if not self.simulation_active:
                #print(f"SIMULATION IS PAUSED - Ants will not move until server starts the simulation")
                pass  # Add pass to fix empty if block
            for ant_id, ant in list(self.my_ants.items())[:5]:  # Show first 5 ants for debugging
                #print(f"  Ant {ant_id}: position=({ant['x']:.1f}, {ant['y']:.1f}), state={ant['state']}")
                pass  # Add pass to fix empty for block
            if len(self.my_ants) > 5:
                #print(f"  ... and {len(self.my_ants)-5} more ants")
                pass  # Add pass to fix empty if block

    def _ant_logic_loop(self):
        """Main loop for ant logic - runs in a separate thread."""
        #print(f"[DEBUG-{threading.current_thread().name}] Ant logic loop starting")
        
        step_interval = 0.05  # 50ms update interval (~20 fps)
        ping_interval = 0.5   # Send ping every 0.5 seconds regardless
        last_ping_time = 0
        full_update_interval = 0.2  # Force a full update every 0.2 seconds
        last_full_update_time = 0
        
        while self.network_running:
            start_time = time.perf_counter()
            current_time = time.time()
            
            # --- Process messages from network thread ---
            while not self.message_queue.empty():
                try:
                    msg_type, data = self.message_queue.get_nowait()
                    #print(f"[LogicThread] Processing queued message: {msg_type}")

                    if msg_type == MSG_S_WELCOME:
                        # Initial setup based on welcome data
                        self.world_width = data.get('world_width', self.world_width)
                        self.world_height = data.get('world_height', self.world_height)
                        #print(f"[LogicThread] Processing WELCOME: world_dim={self.world_width}x{self.world_height}")
                        # Debug the colony data in welcome
                        if KEY_COLONIES in data:
                            #print(f"[LogicThread] WELCOME contains colonies: {data[KEY_COLONIES]}")
                            for colony in data[KEY_COLONIES]:
                                colony_id_in_list = colony.get('id')
                                #print(f"[LogicThread] Colony in list: id={colony_id_in_list}, x={colony.get(KEY_X)}, y={colony.get(KEY_Y)}")
                                
                        # Update initial food/colonies from welcome message
                        self._update_food_sources(data.get(KEY_FOOD_SOURCES, []))
                        self._update_other_colonies(data.get(KEY_COLONIES, []))
                        #print(f"[LogicThread] BEFORE ant init, colony position: ({self.colony_x}, {self.colony_y})")
                        # Initialize ants now that position is confirmed
                        self._initialize_ants()
                        self.simulation_active = data.get('simulation_active', False)
                        #print(f"[LogicThread] AFTER ant init, colony position: ({self.colony_x}, {self.colony_y})")

                    elif msg_type == MSG_S_WORLD_UPDATE:
                        # Update food and colony state based on server update
                        self._update_food_sources(data.get(KEY_FOOD_SOURCES, []))
                        self._update_other_colonies(data.get(KEY_COLONIES, []))
                        
                        # Check for food sources marked as depleted in the update
                        # and process them immediately (belt and suspenders approach)
                        for food_data in data.get(KEY_FOOD_SOURCES, []):
                            if isinstance(food_data, dict):
                                food_id = food_data.get(KEY_FOOD_ID)
                                if food_id is not None and food_data.get('depleted', False):
                                    #print(f"⚠️ DEPLETION CHECK: Found depleted food {food_id} in WORLD_UPDATE")
                                    # Handle through the established pipeline
                                    self._handle_food_depletion(food_id)
                        
                        # Update simulation active state
                        if 'simulation_active' in data:
                            new_active_state = data['simulation_active']
                            if new_active_state != self.simulation_active:
                                self.simulation_active = new_active_state
                                #print(f"Simulation active state changed to: {self.simulation_active}")
                            elif not self.simulation_active: # reminder
                                #print(f"Simulation is PAUSED")
                                pass  # Add pass to fix empty elif block
                                
                        # Update server time for potential sync operations
                        if KEY_TIMESTAMP in data:
                            self.server_time = data[KEY_TIMESTAMP]

                    elif msg_type == MSG_S_FOOD_DEPLETED:
                        # Extract food ID that's been depleted and handle it
                        depleted_food_id = data.get(KEY_FOOD_ID)
                        if depleted_food_id is not None:
                            #print(f"⚠️ DEPLETION EVENT: Received MSG_S_FOOD_DEPLETED for food {depleted_food_id}")
                            # Immediate priority handling - make this synchronous to ensure it's processed
                            # before any other ant logic or drawing updates
                            self._handle_food_depletion(depleted_food_id)
                        else:
                            #print(f"Warning: Received MSG_S_FOOD_DEPLETED but no food ID included")
                            pass  # Add pass to fix empty else block
                    elif msg_type == MSG_S_NEW_FOOD:
                        # Add or update the new food source
                        food_id = data.get(KEY_FOOD_ID)
                        if food_id is not None:
                            # Store/Update in self.current_food_sources
                            self.current_food_sources[food_id] = {
                                KEY_FOOD_ID: food_id,
                                KEY_X: data.get(KEY_X), 
                                KEY_Y: data.get(KEY_Y),
                                'amount': data.get(KEY_FOOD_AMOUNT),
                                'radius': FOOD_RADIUS,  # Use constant
                                'depleted': data.get('depleted', False)  # Sync depleted status
                            }
                            #print(f"Logic thread updated/added food {food_id}")
                            # Trigger target re-evaluation if needed
                            if self.colony_exploration_mode or self.active_food_target == food_id:
                                self._select_best_food_target()

                    elif msg_type == MSG_S_SHUTDOWN:
                        #print("[LogicThread] Received SHUTDOWN via queue. Stopping.")
                        self.network_running = False  # Signal loop stop
                        break  # Exit inner message processing loop

                except queue.Empty:
                    break  # No more messages for now
                except Exception as e:
                    #print(f"[LogicThread] Error processing message queue: {e}")
                    import traceback
                    traceback.print_exc()

            if not self.network_running:  # Check if SHUTDOWN was received
                break  # Exit outer logic loop
            
            if not self.network_running or self.colony_id is None:
                # Wait until connected and received welcome message
                time.sleep(0.1)
                continue
            
            # Update world state variables that might change
            my_colony_x = self.colony_x
            my_colony_y = self.colony_y
            current_food_sources_list = list(self.current_food_sources.values())
            world_w = self.world_width
            world_h = self.world_height
            is_simulation_active = self.simulation_active
            
            # If simulation is not active, don't move ants
            if not is_simulation_active:
                # Still initialize ants if needed, but don't update positions
                if not self.my_ants and self.colony_id is not None:
                    self._initialize_ants()
                    
                    # Send initial positions of all ants
                    initial_ant_updates = []
                    for ant_id, ant_state in self.my_ants.items():
                        update = {
                            KEY_ANT_ID: ant_id,
                            KEY_X: ant_state['x'],
                            KEY_Y: ant_state['y'],
                            KEY_HAS_FOOD: ant_state.get('has_food', False),
                            KEY_STATE: ant_state.get('state', STATE_WANDERING),  # Make sure state is set
                            KEY_COLOR: self.colony_color,
                            KEY_COLONY_ID: self.colony_id
                        }
                        initial_ant_updates.append(update)
                        
                    if initial_ant_updates:
                        initial_update_message = {
                            KEY_COLONY_ID: self.colony_id,
                            KEY_ANTS: initial_ant_updates,
                            KEY_FOOD_TAKEN: 0,
                            KEY_FOOD_DROPPED: 0,
                            KEY_TRAILS: []
                        }
                        self.send_message(MSG_C_ANT_UPDATES, initial_update_message)
                        #print(f"[DEBUG] Sent initial positions for {len(initial_ant_updates)} ants")
                        
                # Skip ant movement logic
                time.sleep(step_interval)
                continue
                
            # Variables to collect ant updates for the server
            ant_updates_for_server = []
            food_taken_details = {}  # {food_id: amount_taken}
            # ants_that_dropped_food list is removed, use helper function instead
            new_trails_generated = []
            if hasattr(self, 'pending_trails') and self.pending_trails:
                new_trails_generated.extend(self.pending_trails)
                self.pending_trails = []
            
            # Check if we need to do a full update (all ants regardless of movement)
            force_full_update = current_time - last_full_update_time >= full_update_interval
            if force_full_update:
                last_full_update_time = current_time
            
            # Process all ants
            current_ant_ids = list(self.my_ants.keys())  # Make a copy of the keys, allowing modification

            for ant_id in current_ant_ids:
                # Skip if ant no longer exists
                if ant_id not in self.my_ants or ant_id not in self.ant_movement_state:
                    continue

                ant_state = self.my_ants[ant_id]
                move_state = self.ant_movement_state[ant_id]
                
                # Process ant_logic for each ant - run the actual ant AI
                self._process_ant_logic(ant_id, ant_state, move_state, current_time, 
                                      self.current_food_sources, 
                                      my_colony_x, my_colony_y,
                                      world_w, world_h,
                                      ant_updates_for_server,
                                      food_taken_details, 
                                      new_trails_generated)
                
                # If we're doing a full update, make sure all ants are included
                if force_full_update and not any(update.get(KEY_ANT_ID) == ant_id for update in ant_updates_for_server):
                    # Add this ant to the update even if it didn't move
                    self._queue_ant_position_update(ant_id, ant_updates_for_server)
            
            # Collect total dropped food *after* processing all ants
            total_food_dropped_this_step = self._collect_dropped_food_info()
                
            # Send an update to the server if:
            # 1. We have ants that moved or changed state, or
            # 2. Food was taken/dropped, or
            # 3. New trails were generated, or 
            # 4. It's been more than ping_interval since our last update
            if ant_updates_for_server or food_taken_details or total_food_dropped_this_step > 0 or new_trails_generated or (current_time - last_ping_time >= ping_interval):
                last_ping_time = current_time
                
                # If we don't have any updates but need to send a ping,
                # include the first few ants to show they're active
                if not ant_updates_for_server:
                    # Add a few ants to the update to ensure the server sees activity
                    for ant_id in list(self.my_ants.keys())[:min(5, len(self.my_ants))]:
                        self._queue_ant_position_update(ant_id, ant_updates_for_server)
                
                # Create the update message
                update_message = {
                    KEY_COLONY_ID: self.colony_id,
                    KEY_ANTS: ant_updates_for_server,
                    'food_sources_taken': food_taken_details, # Use correct key
                    KEY_FOOD_DROPPED: total_food_dropped_this_step # Use collected amount
                }
                if new_trails_generated:
                    update_message[KEY_TRAILS] = new_trails_generated
                    
                # Send update to server
                self.send_message(MSG_C_ANT_UPDATES, update_message)
                # Add more detail to the debug log
                debug_msg = f"[DEBUG] Sent update: {len(ant_updates_for_server)} ants"
                if food_taken_details:
                    debug_msg += f", FoodTaken={food_taken_details}"
                if total_food_dropped_this_step > 0:
                    debug_msg += f", FoodDropped={total_food_dropped_this_step}"
                if new_trails_generated:
                    debug_msg += f", Trails={len(new_trails_generated)}"
                #print(debug_msg)
            
            # Regulate timing to achieve target update rate
            elapsed = time.perf_counter() - start_time
            sleep_time = max(0, step_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _queue_ant_position_update(self, ant_id, updates_list=None):
        """Add an ant position update to be sent to the server."""
        if ant_id not in self.my_ants:
            return
        
        ant = self.my_ants[ant_id]
        
        update = {
            KEY_ANT_ID: ant_id,
            KEY_X: ant['x'],
            KEY_Y: ant['y'],
            KEY_HAS_FOOD: ant.get('has_food', False),
            KEY_STATE: ant.get('state', STATE_WANDERING),
            KEY_COLOR: self.colony_color,
            KEY_COLONY_ID: self.colony_id
        }
        
        if updates_list is not None:
            # Add to the list of updates that will be sent in a batch
            updates_list.append(update)
        elif hasattr(self, 'pending_updates'):
            # Create a proper message to send to the server
            message = {
                KEY_COLONY_ID: self.colony_id,
                KEY_ANTS: [update],
                KEY_FOOD_TAKEN: 0,
                KEY_FOOD_DROPPED: 0,
                KEY_TRAILS: []
            }
            # Encode the message for sending
            encoded_message = encode_message(MSG_C_ANT_UPDATES, message)
            self.pending_updates.append(encoded_message)

    def _process_ant_logic(self, ant_id, ant_state, move_state, current_time, 
                           current_food_sources, colony_x, colony_y, world_w, world_h,
                           ant_updates, food_taken_details, new_trails):
        """Process logic for a single ant based on its state."""
        x, y = ant_state['x'], ant_state['y']
        state = ant_state['state']
        has_food = ant_state.get('has_food', False)

        # DIAGNOSTIC: Log detailed state info for a few sample ants (every 10th ant)
        if int(ant_id.split('_')[1]) % 10 == 0:
            #print(f"DIAGNOSTIC: Ant {ant_id} - State: {state}, Has Food: {has_food}, Pos: ({x:.1f}, {y:.1f}), Colony: ({colony_x:.1f}, {colony_y:.1f})")
            #print(f"           Distance to colony: {math.sqrt(distance_sq(x, y, colony_x, colony_y)):.1f}, Interaction radius: {math.sqrt(COLONY_INTERACTION_RADIUS_SQ):.1f}")
            if 'last_colony_interaction_time' in move_state:
                time_since = current_time - move_state.get('last_colony_interaction_time', 0)
                #print(f"           Time since last colony interaction: {time_since:.1f}s")

        position_changed = False
        state_changed = False
        new_x, new_y = x, y # Start with current position

        # --- Universal Checks ---

        # Check for arrival at colony (only trigger handler if not already waiting)
        dist_sq_to_colony = distance_sq(x, y, colony_x, colony_y)
        # Increased interaction radius to fix bug where ants don't register arriving at colony
        colony_in_range = dist_sq_to_colony <= COLONY_INTERACTION_RADIUS_SQ * 2.0  # 2x larger radius for normal detection

        # Use a significantly larger radius for ants carrying food to ensure they can reach the colony
        if has_food:
            colony_in_range = dist_sq_to_colony <= COLONY_INTERACTION_RADIUS_SQ * 6.0  # 6x larger radius for food-carrying ants (increased from 5x)
        
        # DIAGNOSTIC: Log colony proximity checks for food-carrying ants
        if has_food and int(ant_id.split('_')[1]) % 5 == 0:  # Check every 5th ant with food
            interaction_radius = math.sqrt(COLONY_INTERACTION_RADIUS_SQ * 6.0) if has_food else math.sqrt(COLONY_INTERACTION_RADIUS_SQ * 2.0)
            #print(f"COLONY CHECK: Ant {ant_id} with food - Distance to colony: {math.sqrt(dist_sq_to_colony):.1f}, In range: {colony_in_range}, Interaction radius: {interaction_radius:.1f}")
            if colony_in_range:
                in_cooldown = move_state.get('last_colony_interaction_time') and current_time - move_state.get('last_colony_interaction_time') < 4.0
                #print(f"           Colony in range but skipping? {in_cooldown} (Would trigger: {colony_in_range and not in_cooldown and state != STATE_WAITING_FOR_INSTRUCTIONS})")
        
        if colony_in_range and state != STATE_WAITING_FOR_INSTRUCTIONS:
            # Don't re-trigger colony interaction for ants that just left, UNLESS they have food
            if not has_food and move_state.get('last_colony_interaction_time') and current_time - move_state.get('last_colony_interaction_time') < 4.0:
                # Skip colony interaction if we were just at the colony (prevent oscillation)
                #print(f"⏭️ Ant {ant_id} skipping colony interaction - too soon since last visit")
                pass
            else:
                # Let _handle_ant_at_colony manage the state change to WAITING
                #print(f"🐜 Ant {ant_id} triggering colony arrival handler.")
                if has_food:
                    #print(f"🍎 Ant {ant_id} carrying food to colony - GUARANTEED handling")
                    pass  # Add pass to fix empty if block
                return_path = list(move_state.get('path_points', []))
                self._handle_ant_at_colony(ant_id, return_path)
                return # Stop further processing for this ant this step

        # Check for food pickup (only if not carrying food and in a state that allows pickup)
        if not has_food and (state == STATE_WANDERING or state == STATE_GOING_TO_FOOD):
            pickup_success, food_id_picked_up = self._check_and_pickup_food(ant_id, ant_state, move_state, current_food_sources)
            if pickup_success:
                # Food picked up, state changed to RETURNING, path reversed.
                if food_id_picked_up is not None:
                    food_taken_details[food_id_picked_up] = food_taken_details.get(food_id_picked_up, 0) + FOOD_TAKE_AMOUNT
                self._queue_ant_position_update(ant_id, ant_updates)
                return # Stop further processing

        # --- State-Specific Logic ---

        if state == STATE_WANDERING:
            # Wander logic
            if random.random() < 0.05:
                move_state['direction'] = random.uniform(0, 2 * math.pi)

            direction = move_state['direction']
            dx = ANT_SPEED * math.cos(direction)
            dy = ANT_SPEED * math.sin(direction)
            new_x = x + dx
            new_y = y + dy
            position_changed = True

            # Check wander timeout
            if current_time - move_state.get('wander_start_time', current_time) > move_state.get('wander_timeout', WANDER_TIMEOUT_MAX):
                #print(f"⏳ Ant {ant_id} wander timeout. Setting state to RETURNING.")
                ant_state['state'] = STATE_RETURNING
                move_state['reusing_path'] = True
                move_state['path_points'] = list(reversed(move_state.get('path_points', [])))
                move_state['current_path_target_index'] = 0
                move_state['wander_start_time'] = None # No longer wandering
                state_changed = True
                # Allow movement calculation for this step before returning next step

        elif state == STATE_RETURNING or state == STATE_GOING_TO_FOOD:
            # CRITICAL FIX: Re-validate target food source while en route (only for GOING_TO_FOOD state)
            if state == STATE_GOING_TO_FOOD:
                target_food_id = ant_state.get('target_food_id')
                if target_food_id is not None and not self._is_valid_food_source(target_food_id):
                    #print(f"🚨 Ant {ant_id} en route to food {target_food_id} that is no longer valid! Switching to WANDERING.")
                    ant_state['state'] = STATE_WANDERING
                    ant_state['target_food_id'] = None
                    move_state['reusing_path'] = False
                    move_state['wander_start_time'] = current_time
                    move_state['wander_timeout'] = random.uniform(WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX)
                    move_state['direction'] = random.uniform(0, 2 * math.pi)
                    state_changed = True
                    # Skip path following logic for this step, will wander on next cycle
                    return
            
            # Path following logic
            path = move_state.get('path_points', [])
            target_index = move_state.get('current_path_target_index', 0)

            if path and target_index < len(path):
                target_x, target_y = path[target_index]
                dx = target_x - x
                dy = target_y - y
                dist_sq_to_target = dx*dx + dy*dy
                move_dist = ANT_SPEED

                if dist_sq_to_target > 0:
                    dist_to_target = math.sqrt(dist_sq_to_target)
                    if dist_to_target <= move_dist:
                        # Reach or pass the target point exactly
                        new_x, new_y = target_x, target_y
                        move_state['current_path_target_index'] += 1
                        position_changed = True
                        #print(f"🐜 Ant {ant_id} reached path point {target_index}/{len(path)-1}")
                        
                        # Check if path ended
                        if move_state['current_path_target_index'] >= len(path):
                            #print(f"🏁 Ant {ant_id} reached end of path in state {state}.")
                            if state == STATE_GOING_TO_FOOD:
                                # Re-validate target food now that we're at the end of the path
                                target_food_id = ant_state.get('target_food_id')
                                if target_food_id is not None and self._is_valid_food_source(target_food_id):
                                    # Target still valid - keep going to food, but now wander near it
                                    # Get food position
                                    food_data = current_food_sources.get(target_food_id, {})
                                    food_x = food_data.get(KEY_X)
                                    food_y = food_data.get(KEY_Y)
                                    if food_x is not None and food_y is not None:
                                        # Calculated direction should be toward food
                                        dx = food_x - x
                                        dy = food_y - y
                                        if dx*dx + dy*dy > 0:  # Avoid division by zero
                                            direction = math.atan2(dy, dx)
                                            # Add a small random offset to prevent stuck ants
                                            direction += random.uniform(-0.5, 0.5)
                                            move_state['direction'] = direction
                                        else:
                                            # We're exactly at the food position, but didn't pick up?
                                            # Set random direction
                                            move_state['direction'] = random.uniform(0, 2 * math.pi)
                                    else:
                                        # Invalid food data, randomize direction
                                        move_state['direction'] = random.uniform(0, 2 * math.pi)
                                else:
                                    # Target no longer valid - switch to pure wandering
                                    ant_state['state'] = STATE_WANDERING
                                    ant_state['target_food_id'] = None
                                    move_state['wander_start_time'] = current_time
                                    move_state['wander_timeout'] = random.uniform(WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX)
                                    move_state['direction'] = random.uniform(0, 2 * math.pi)
                                    state_changed = True
                            elif state == STATE_RETURNING:
                                # Colony proximity check will handle arrival next step
                                pass # Just use current position
                    else:
                        # Move toward target point
                        scale = move_dist / dist_to_target
                        new_x = x + dx * scale
                        new_y = y + dy * scale
                        position_changed = True
            else:
                # No path or path ended unexpectedly
                #print(f"⚠️ Ant {ant_id} in state {state} has invalid/missing path.")
                if state == STATE_RETURNING:
                    # Head directly to colony as fallback
                    dx = colony_x - x
                    dy = colony_y - y
                    dist_sq = dx*dx + dy*dy
                    if dist_sq > 0:
                        dist = math.sqrt(dist_sq)
                        norm_dx = dx / dist
                        norm_dy = dy / dist
                        new_x = x + norm_dx * ANT_SPEED
                        new_y = y + norm_dy * ANT_SPEED
                        position_changed = True
                elif state == STATE_GOING_TO_FOOD:
                    # Switch to wandering if path is missing
                    ant_state['state'] = STATE_WANDERING
                    ant_state['target_food_id'] = None
                    move_state['wander_start_time'] = current_time
                    move_state['wander_timeout'] = random.uniform(WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX)
                    move_state['direction'] = random.uniform(0, 2 * math.pi)
                    state_changed = True

        elif state == STATE_RETURNING:
                    # Head directly to colony as fallback
            dx = colony_x - x
            dy = colony_y - y
            dist = math.sqrt(dx*dx + dy*dy)
            if dist > ANT_SPEED:
                norm_dx, norm_dy = normalize_vector(dx, dy)
                new_x = x + norm_dx * ANT_SPEED
                new_y = y + norm_dy * ANT_SPEED
                position_changed = True
            elif dist > 0: # Close enough
                new_x, new_y = colony_x, colony_y
                position_changed = True
            # Keep state as RETURNING
            else: # GOING_TO_FOOD or other invalid path state -> Wander
                ant_state['state'] = STATE_WANDERING
                move_state['reusing_path'] = False
                move_state['path_points'] = [(x, y)]
                move_state['last_recorded_point'] = (x, y)
                move_state['wander_start_time'] = current_time
                move_state['wander_timeout'] = random.uniform(WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX)
                move_state['direction'] = random.uniform(0, 2 * math.pi)
                state_changed = True


        elif state == STATE_WAITING_FOR_INSTRUCTIONS:
            # Safety check: ants in waiting state should never have food
            # If they do, it's an inconsistent state we need to fix
            if has_food:
                #print(f"⚠️ SAFETY FIX: Ant {ant_id} in WAITING state had food! Removing food to fix inconsistent state.")
                ant_state['has_food'] = False
                ant_state['carrying_food_from'] = None
                state_changed = True
                
            # Ant is at the colony, check move_state for the task assigned by _handle_ant_at_colony
            #print(f"⏳ Ant {ant_id} is WAITING for instructions...")
            task_type = move_state.get('next_task_type')

            if task_type == 'goto_food':
                path_to_follow = move_state.get('next_task_path')
                target_food_id = move_state.get('next_target_food_id')

                # ====> CRITICAL FIX: Re-validate target *before* leaving WAITING state <====
                if target_food_id is None or not self._is_valid_food_source(target_food_id):
                    #print(f"❌ Re-validation FAILED: Target food {target_food_id} is no longer valid for ant {ant_id}. Switching task to wander.")
                    task_type = 'wander' # Force wander if target became invalid
                    path_to_follow = None # Clear invalid path
                    # Ensure wander direction is set if not already
                    if move_state.get('next_task_direction') is None:
                         move_state['next_task_direction'] = random.uniform(0, 2 * math.pi)
                # ====> END CRITICAL FIX <====

                # Proceed only if task is still 'goto_food' after validation
                if task_type == 'goto_food' and path_to_follow:
                    #print(f"🚀 Ant {ant_id} receiving VALIDATED GO_TO_FOOD {target_food_id} instruction.")
                    ant_state['state'] = STATE_GOING_TO_FOOD
                    ant_state['target_food_id'] = target_food_id # Set the active target
                    move_state['path_points'] = path_to_follow
                    move_state['current_path_target_index'] = 0
                    move_state['reusing_path'] = True
                    move_state['wander_start_time'] = None

                    # Push ant out towards first path point
                    if path_to_follow:
                        target_x, target_y = path_to_follow[0]
                        dx, dy = target_x - colony_x, target_y - colony_y
                        push_dist = COLONY_RADIUS * 2.0  # Increased from 1.5 to 2.0 to ensure they exit completely
                        dist = math.sqrt(dx*dx + dy*dy)
                        if dist > 0:
                            ant_state['x'] = colony_x + (dx / dist) * push_dist
                            ant_state['y'] = colony_y + (dy / dist) * push_dist
                            new_x, new_y = ant_state['x'], ant_state['y'] # Update local vars for boundary check
                            position_changed = True # Position WILL change due to push
                        # Reset last recorded point after push
                        move_state['last_recorded_point'] = (ant_state['x'], ant_state['y'])

                    state_changed = True
                else:
                    #print(f"⚠️ Ant {ant_id} had 'goto_food' task but task was changed to wander after validation")
                    task_type = 'wander' # Fallback to wander

            # Separate 'if' handles the 'wander' task (either initially decided or fallback)
            if task_type == 'wander':
                #print(f"🚀 Ant {ant_id} receiving WANDER instruction.")
                ant_state['state'] = STATE_WANDERING
                ant_state['target_food_id'] = None
                move_state['path_points'] = [(colony_x, colony_y)] # Start path history at colony
                move_state['current_path_target_index'] = 0
                move_state['reusing_path'] = False
                move_state['wander_start_time'] = current_time
                move_state['wander_timeout'] = random.uniform(WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX)
                move_state['direction'] = move_state.get('next_task_direction', random.uniform(0, 2 * math.pi))

                # Push ant out in wander direction
                push_dist = COLONY_RADIUS * 2.0  # Increased from 1.5 to 2.0 to ensure they exit completely
                ant_state['x'] = colony_x + math.cos(move_state['direction']) * push_dist
                ant_state['y'] = colony_y + math.sin(move_state['direction']) * push_dist
                new_x, new_y = ant_state['x'], ant_state['y'] # Update local vars
                position_changed = True # Position WILL change due to push
                # Reset last recorded point after push
                move_state['last_recorded_point'] = (ant_state['x'], ant_state['y'])

                state_changed = True

            # Clear the temporary task instructions from move_state
            move_state.pop('next_task_type', None)
            move_state.pop('next_task_path', None)
            move_state.pop('next_task_direction', None)
            move_state.pop('next_target_food_id', None)

            # DO NOT calculate movement here - the push is the movement for this step


        # --- Boundary Check & Path Recording ---
        if position_changed:
            # Keep within world boundaries (apply to new_x, new_y before final assignment)
            final_x = max(ANT_RADIUS, min(world_w - ANT_RADIUS, new_x))
            final_y = max(ANT_RADIUS, min(world_h - ANT_RADIUS, new_y))

            # Update ant state position
            ant_state['x'] = final_x
            ant_state['y'] = final_y

            # Path Recording (only record if wandering and moved sufficiently)
            if state == STATE_WANDERING:
                last_point = move_state.get('last_recorded_point')
                # Ensure last_point is valid before calculating distance
                if last_point and isinstance(last_point, tuple) and len(last_point) == 2:
                    dist_sq_from_last = distance_sq(final_x, final_y, last_point[0], last_point[1])
                    if dist_sq_from_last >= TRAIL_POINT_DISTANCE**2:
                        if not isinstance(move_state.get('path_points'), list):
                            move_state['path_points'] = []
                        move_state['path_points'].append((final_x, final_y))
                        move_state['last_recorded_point'] = (final_x, final_y)
                        # Limit path memory during wandering
                        if len(move_state['path_points']) > PATH_MEMORY_LENGTH:
                            move_state['path_points'].pop(0)
                    else:
                        # If last_recorded_point is invalid, initialize it
                        move_state['last_recorded_point'] = (final_x, final_y)
                if not isinstance(move_state.get('path_points'), list):
                    move_state['path_points'] = [(final_x, final_y)]
                else:
                    # Avoid appending if just initialized
                    pass


        # --- Queue Update ---
        # Send update if position or state changed significantly
        if position_changed or state_changed:
            self._queue_ant_position_update(ant_id, ant_updates)

    def _check_and_pickup_food(self, ant_id, ant_state, move_state, current_food_sources):
        """Checks if the ant is close enough to any valid food source to pick it up."""
        if ant_state.get('has_food', False):
            return False, None # Already has food

        ant_x, ant_y = ant_state['x'], ant_state['y']
        ant_state_val = ant_state.get('state')
        
        # DIAGNOSTIC: Log food pickup attempts for ants going to food
        if ant_state_val == STATE_GOING_TO_FOOD and int(ant_id.split('_')[1]) % 5 == 0:  # Check every 5th ant going to food
            target_food_id = ant_state.get('target_food_id')
            if target_food_id in current_food_sources:
                food_data = current_food_sources[target_food_id]
                food_x, food_y = food_data.get(KEY_X), food_data.get(KEY_Y)
                if food_x is not None and food_y is not None:
                    dist_to_food = math.sqrt(distance_sq(ant_x, ant_y, food_x, food_y))
                    interaction_radius = math.sqrt((FOOD_RADIUS + ANT_RADIUS)**2)
                    #print(f"FOOD CHECK: Ant {ant_id} going to food {target_food_id} - Distance: {dist_to_food:.1f}, Interaction radius: {interaction_radius:.1f}")
                    #print(f"            Would pickup: {dist_to_food <= interaction_radius}")

        # First check if the ant was specifically targeting a food source
        targeted_food_id = ant_state.get('target_food_id')
        if targeted_food_id is not None:
            # Extra safety - verify the target is still valid before checking distance
            if not self._is_valid_food_source(targeted_food_id):
                #print(f"🚫 Ant {ant_id} was targeting food {targeted_food_id} but it's no longer valid - switching to WANDERING")
                ant_state['state'] = STATE_WANDERING
                ant_state['target_food_id'] = None
                move_state['reusing_path'] = False
                move_state['wander_start_time'] = time.time()
                move_state['direction'] = random.uniform(0, 2 * math.pi)
                return False, None  # Could not pick up food
            
            # Target is valid, check if in range
            food_data = current_food_sources.get(targeted_food_id)
            if food_data:
                food_x, food_y = food_data.get(KEY_X), food_data.get(KEY_Y)
                if food_x is not None and food_y is not None:
                    dist_sq = distance_sq(ant_x, ant_y, food_x, food_y)
                    pickup_radius_sq = (FOOD_RADIUS + ANT_RADIUS)**2
                    
                    if dist_sq <= pickup_radius_sq:
                        # ====> CRITICAL VALIDATION BEFORE PICKUP <====
                        # Double-check food is still valid at the very last moment
                        if not self._is_valid_food_source(targeted_food_id):
                            #print(f"🚫 PICKUP BLOCKED (Targeted): Ant {ant_id} near food {targeted_food_id}, but re-validation failed.")
                            # Force ant to wander if its target disappeared
                            ant_state['state'] = STATE_WANDERING
                            ant_state['target_food_id'] = None
                            move_state['reusing_path'] = False
                            move_state['wander_start_time'] = time.time()
                            move_state['direction'] = random.uniform(0, 2 * math.pi)
                            return False, None
                        # ====> END VALIDATION <====
                            
                        #print(f"🍎 Ant {ant_id} picking up food at targeted source {targeted_food_id}")
                        # Perform pickup logic as normal
                        ant_state['has_food'] = True
                        ant_state['carrying_food_from'] = targeted_food_id
                        ant_state['state'] = STATE_RETURNING
                        ant_state['target_food_id'] = None # No longer targeting food

                        # Decrement food amount locally (client prediction)
                        previous_amount = food_data.get('amount', 0)
                        food_data['amount'] = max(0, previous_amount - FOOD_TAKE_AMOUNT)
                        
                        # CRITICAL: If this was the last bit of food, mark as depleted immediately
                        # This prevents other ants from thinking it's still valid
                        if food_data['amount'] <= 0:
                            food_data['depleted'] = True
                            #print(f"📉 Ant {ant_id} pickup DEPLETES food source {targeted_food_id} (client prediction)")
                            # Trigger a refresh of the active target if needed
                            if self.active_food_target == targeted_food_id:
                                #print(f"🎯 Active target {targeted_food_id} depleted locally. Scheduling target refresh.")
                                self.active_food_target = None
                                # Schedule target refresh on next step
                        
                        # Reverse the path the ant took to get here
                        current_path = list(move_state.get('path_points', []))
                        move_state['path_points'] = list(reversed(current_path))
                        move_state['current_path_target_index'] = 0 # Start following from beginning
                        move_state['reusing_path'] = True # Now following the return path
                        move_state['wander_start_time'] = None # Not wandering
                        
                        #print(f"✅ Ant {ant_id} picked up food from {targeted_food_id}. Reversing path ({len(move_state['path_points'])} points). State -> RETURNING.")
                        return True, targeted_food_id # Success with targeted food
        
        # If not targeting a specific food, or target wasn't in range, check any food sources nearby
        for food_id, food_data in current_food_sources.items():
            # Skip the targeted food if we already checked it
            if food_id == targeted_food_id:
                continue
                
            # Validate food source before distance check
            if not self._is_valid_food_source(food_id):
                continue
                        
            food_x = food_data.get(KEY_X)
            food_y = food_data.get(KEY_Y)

            if food_x is None or food_y is None: 
                continue # Skip invalid data

            # Check distance - Use a smaller radius for "touching"
            pickup_radius_sq = (FOOD_RADIUS + ANT_RADIUS)**2 # Original interaction radius

            dist_sq = distance_sq(ant_x, ant_y, food_x, food_y)

            if dist_sq <= pickup_radius_sq:
                # ====> CRITICAL VALIDATION BEFORE PICKUP <====
                # Double-check food is still valid at the very last moment
                if not self._is_valid_food_source(food_id):
                    #print(f"🚫 PICKUP BLOCKED (Nearby): Ant {ant_id} near food {food_id}, but re-validation failed.")
                    continue # Check next food source
                # ====> END VALIDATION <====
                    
                #print(f"🍎 Ant {ant_id} attempting pickup at food {food_id}")

                # --- Perform Pickup ---
                ant_state['has_food'] = True
                ant_state['carrying_food_from'] = food_id
                ant_state['state'] = STATE_RETURNING
                ant_state['target_food_id'] = None # No longer targeting food

                # Decrement food amount locally (client prediction)
                previous_amount = food_data.get('amount', 0)
                food_data['amount'] = max(0, previous_amount - FOOD_TAKE_AMOUNT)
                
                # CRITICAL: If this was the last bit of food, mark as depleted immediately
                if food_data['amount'] <= 0:
                    food_data['depleted'] = True
                    #print(f"📉 Ant {ant_id} pickup DEPLETES food source {food_id} (client prediction)")
                    # Trigger a refresh of the active target if needed
                    if self.active_food_target == food_id:
                        #print(f"🎯 Active target {food_id} depleted locally. Scheduling target refresh.")
                        self.active_food_target = None

                # Reverse the path the ant took to get here
                current_path = list(move_state.get('path_points', []))
                move_state['path_points'] = list(reversed(current_path))
                move_state['current_path_target_index'] = 0 # Start following from beginning
                move_state['reusing_path'] = True # Now following the return path
                move_state['wander_start_time'] = None # Not wandering

                #print(f"✅ Ant {ant_id} picked up food from {food_id}. Reversing path ({len(move_state['path_points'])} points). State -> RETURNING.")
                return True, food_id # Success

        return False, None # No food picked up

    def _is_valid_food_source(self, food_id):
        """Check if a food source exists, is not marked depleted, and has amount > 0."""
        try:
            # Validate food_id value
            if food_id is None: 
                #print(f"  ❌ _is_valid: Invalid food_id is None")
                return False # Cannot be valid
            
            # Convert to integer if string
            try:
                food_id_int = int(food_id) if isinstance(food_id, str) else food_id
            except ValueError:
                #print(f"  ❌ _is_valid: Invalid food_id format '{food_id}'")
                return False

            # CRITICAL: Check if food source exists
            if food_id_int not in self.current_food_sources:
                # More verbose logging for debugging
                #print(f"  ❌ _is_valid: ID {food_id_int} not found in current_food_sources dictionary")
                if hasattr(self, 'current_food_sources'):
                    #print(f"       Available food sources: {list(self.current_food_sources.keys())}")
                    pass  # Add pass to fix empty if block
                return False

            # Get food data and check essential fields
            food_data = self.current_food_sources[food_id_int]
            
            # Check for required fields
            required_fields = [KEY_X, KEY_Y, 'amount']
            for field in required_fields:
                if field not in food_data:
                    #print(f"  ❌ _is_valid: ID {food_id_int} missing required field: {field}")
                    return False

            # Check 'depleted' flag *first* - most important check
            is_depleted = food_data.get('depleted', False)
            if is_depleted:
                #print(f"  ❌ _is_valid: ID {food_id_int} is explicitly marked depleted=True")
                return False

            # Check 'amount' *second* - also critical
            food_amount = food_data.get('amount', 0)
            if food_amount <= 0:
                #print(f"  ❌ _is_valid: ID {food_id_int} has amount {food_amount} <= 0")
                # If amount is zero/negative but not marked depleted, mark it now (client self-correction)
                if not is_depleted:
                     #print(f"     ⚠️ Auto-marking ID {food_id_int} as depleted due to zero amount")
                     food_data['depleted'] = True # Correct local state
                     
                     # Consider calling _handle_food_depletion to ensure full cleanup
                     #print(f"     ⚠️ Self-triggering depletion handling for food {food_id_int}")
                     self._handle_food_depletion(food_id_int)
                return False

            # If we reach here, food source is valid
            food_coords = (food_data.get(KEY_X), food_data.get(KEY_Y))
            # #print(f"  ✅ _is_valid: ID {food_id_int} is valid (amount={food_amount}, pos={food_coords})")
            return True

        except Exception as e:
            #print(f"  ❌ ERROR in _is_valid_food_source for {food_id}: {e}")
            import traceback
            traceback.print_exc()
            return False

    # --- Colony Coordination Methods ---
    def _calculate_path_length(self, path):
        """Calculate the total length of a path."""
        if not path or len(path) < 2:
            return 0
            
        total_length = 0
        for i in range(1, len(path)):
            x1, y1 = path[i-1]
            x2, y2 = path[i]
            segment_length = math.sqrt(distance_sq(x1, y1, x2, y2))
            total_length += segment_length
            
        return total_length
        
    def _register_food_path(self, food_id, path_points=None):
        """Registers a path to a food source, keeping only the shortest known path."""
        try:
            if food_id is None or not path_points: return False
            if not self._is_valid_food_source(food_id): return False # Don't register paths to invalid sources

            # Calculate new path length
            new_path_length = self._calculate_path_length(path_points)
            if new_path_length <= 0: return False # Invalid path
            
            food_data = self.current_food_sources[food_id]
            food_coords = (food_data.get(KEY_X, 0), food_data.get(KEY_Y, 0))
            
            # Check against existing path
            current_shortest_length = float('inf')
            if food_id in self.known_food_paths:
                current_shortest_length = self.known_food_paths[food_id].get('length', float('inf'))

            # If new path is shorter (or no path exists yet)
            # Use a threshold to avoid tiny fluctuations replacing paths constantly
            if new_path_length < current_shortest_length * SHORTER_PATH_THRESHOLD or food_id not in self.known_food_paths:
                #print(f"📝 Registering NEW SHORTEST path to food {food_id}: {len(path_points)} points, length: {new_path_length:.1f} (was {current_shortest_length:.1f})")
                self.known_food_paths[food_id] = {
                        'path': list(path_points), # Store a copy
                        'length': new_path_length,
                    'food_coords': food_coords,
                        'timestamp': time.time() # Track when path was found/updated
                    }

                # If this newly registered path is for our active target, or we have no target,
                # potentially re-evaluate assignments.
                if self.active_food_target is None or self.active_food_target == food_id:
                    self._select_best_food_target() # Re-confirm best target
                    # Maybe trigger re-assignment of ants at colony?
                    # self._reassign_ants_at_colony(food_id) # Could cause rapid switching

                return True # Path was updated/added
            else:
                # #print(f"Path to {food_id} (len {new_path_length:.1f}) not shorter than existing ({current_shortest_length:.1f}). Ignoring.")
                return False # Path not updated
            
        except Exception as e:
            #print(f"❌ Error registering food path for food {food_id}: {str(e)}")
            return False
    
    def _select_best_food_target(self):
        """Select the best food source to target based on SHORTEST KNOWN PATH length."""
        #print("💡 Selecting best food target...")
        best_food_id = None
        shortest_len = float('inf')

        valid_known_paths = {}
        # Prune old/invalid paths first
        current_time = time.time()
        ids_to_prune = []
        for food_id, path_info in self.known_food_paths.items():
            # Check if food source still exists and is valid
            if not self._is_valid_food_source(food_id):
                ids_to_prune.append(food_id)
                continue
            # Optional: Prune very old paths if desired (e.g., > TRAIL_TIMEOUT)
            # if current_time - path_info.get('timestamp', 0) > TRAIL_TIMEOUT * 2:
            #    ids_to_prune.append(food_id)
            #    continue
            valid_known_paths[food_id] = path_info['length']

        for food_id in ids_to_prune:
            #print(f"  prune Known path to invalid/old food {food_id}")
            self.known_food_paths.pop(food_id, None)

        # Find the shortest among valid known paths
        if valid_known_paths:
            sorted_paths = sorted(valid_known_paths.items(), key=lambda item: item[1])
            best_food_id = sorted_paths[0][0]
            shortest_len = sorted_paths[0][1]
            #print(f"🏆 Best target from KNOWN PATHS: {best_food_id} (length: {shortest_len:.1f})")

        else:
            # Fallback: If no known paths, find the *closest* valid food source by direct distance
            #print("  No valid known paths. Falling back to closest food source...")
            nearest_dist_sq = float('inf')
            closest_fallback_id = None
            for food_id, food_data in self.current_food_sources.items():
                if self._is_valid_food_source(food_id):
                     dist_sq = distance_sq(self.colony_x, self.colony_y, food_data[KEY_X], food_data[KEY_Y])
                     if dist_sq < nearest_dist_sq:
                         nearest_dist_sq = dist_sq
                         closest_fallback_id = food_id

            if closest_fallback_id is not None:
                 best_food_id = closest_fallback_id
                 #print(f"🧭 Best target by fallback (closest): {best_food_id} (distance: {math.sqrt(nearest_dist_sq):.1f})")
                 # We don't have a known *path* yet for this one. Ants sent here will wander towards it initially.
            else:
                 #print("❌ No valid food sources found at all.")
                 pass  # Add pass statement for empty else block

        # Update colony state
        if best_food_id != self.active_food_target:
             #print(f"🎯 Setting active food target to: {best_food_id}")
             self.active_food_target = best_food_id

        self.colony_exploration_mode = (best_food_id is None) # Explore if no target

        return best_food_id # Return the selected ID
        
    def _assign_ant_to_food(self, ant_id, food_id=None):
        """Assign an ant to collect food from a specific food source."""
        if ant_id not in self.my_ants:
            #print(f"❌ Cannot assign ant {ant_id} to food - ant doesn't exist")
            return False
            
        # Check if this is the ant's preferred source
        preferred_food_id = None
        if ant_id in self.ant_movement_state:
            preferred_food_id = self.ant_movement_state[ant_id].get('preferred_food_id')
            
        # If no food_id provided, use the active target
        if food_id is None:
            if not self.active_food_target:
                #print(f"❌ Cannot assign ant {ant_id} to food - no active food target")
                return False
            food_id = self.active_food_target
        
        # Log if we're sending to preferred source
        if preferred_food_id == food_id:
            #print(f"🔄 Assigning ant {ant_id} to its PREFERRED food source {food_id}")
            pass  # Add pass statement for empty if block
        
        # Verify food source is valid
        if not self._is_valid_food_source(food_id):
            #print(f"❌ Cannot assign ant {ant_id} to food - food source {food_id} not valid")
            return False
            
        # Get the data for the food source
        food_data = self.current_food_sources.get(food_id)
        if not food_data:
            #print(f"❌ Cannot find data for food source {food_id}")
            return False
            
        target_x, target_y = food_data.get(KEY_X, 0), food_data.get(KEY_Y, 0)
        
        # Set up the ant to collect from the food source
        ant_state = self.my_ants[ant_id]
        move_state = self.ant_movement_state[ant_id]
        
        # IMPORTANT FIX: Check if ant is at the colony - if so, push it slightly away
        ant_x, ant_y = ant_state['x'], ant_state['y'] 
        dist_to_colony_sq = distance_sq(ant_x, ant_y, self.colony_x, self.colony_y)
        if dist_to_colony_sq <= COLONY_INTERACTION_RADIUS_SQ:
            # Push the ant slightly away from colony toward food target
            direction_to_food = math.atan2(target_y - self.colony_y, target_x - self.colony_x)
            push_distance = COLONY_RADIUS * 1.2  # Push slightly beyond the colony radius
            
            # Calculate new position outside colony radius
            new_x = self.colony_x + push_distance * math.cos(direction_to_food)
            new_y = self.colony_y + push_distance * math.sin(direction_to_food)
            
            # Update ant position to be outside colony
            ant_state['x'] = new_x
            ant_state['y'] = new_y
            #print(f"🚀 Pushing ant {ant_id} away from colony toward food {food_id}")
        
        # Add to assigned set and remove from explorers
        self.ants_assigned_to_food.add(ant_id)
        self.ants_assigned_to_explore.discard(ant_id)
        
        # Configure the ant to go to the food source
        ant_state['state'] = STATE_WANDERING  # Use wandering state for travel to food
        ant_state['has_food'] = False
        ant_state['target_food_id'] = food_id
        
        # Store the exact food target location
        move_state['target_food_x'] = target_x
        move_state['target_food_y'] = target_y
        move_state['is_approaching_food'] = True
        
        # Check if we have a known path to this food source
        has_path = food_id in self.known_food_paths
        
        #print(f"🎯 Assigning ant {ant_id} to collect food from source {food_id} (has_path: {has_path})")
        
        # If we have a path, use it
        if has_path:
            path = self.known_food_paths[food_id]['path']
            #print(f"🛣️ Using known path to food {food_id}: {len(path)} points")
            
            # Make sure path ends exactly at the food location
            if len(path) > 0:
                # Replace the last point with exact food location
                path = list(path)  # Ensure we have a new copy
                path[-1] = (target_x, target_y)
                
            # Set path following state
            ant_state['following_trail'] = True
            move_state['path_points'] = path  # Use the modified path
            move_state['reusing_path'] = True
            move_state['last_recorded_point'] = None
            move_state['last_food_id'] = food_id
        else:
            # No path, just head in the right direction
            #print(f"🧭 Direct travel to food {food_id} at ({target_x}, {target_y})")
            ant_state['following_trail'] = False
            
            # Calculate direction to food
            dx = target_x - ant_state['x']
            dy = target_y - ant_state['y']
            angle = math.atan2(dy, dx)
            
            # Set direction toward food source
            move_state['direction'] = angle
            move_state['steps_in_direction'] = 0
            move_state['direction_changes'] = 0
            move_state['path_points'] = []
            move_state['reusing_path'] = False
            move_state['last_recorded_point'] = None
        
        # Reset the wander timeout to give this ant plenty of time to reach food
        move_state['wander_start_time'] = time.time()
        move_state['wander_timeout'] = random.uniform(WANDER_TIMEOUT_MIN * 3, WANDER_TIMEOUT_MAX * 3)
        move_state['returning_to_colony_timeout'] = False
        
        # Queue an update to show the ant's new state immediately
        self._queue_ant_position_update(ant_id)
        
        #print(f"✅ Ant {ant_id} assigned to collect food from source {food_id}")
        return True
        
    def _assign_ant_to_explore(self, ant_id):
        """Assign an ant to explore the world."""
        if ant_id not in self.my_ants:
            #print(f"❌ Ant {ant_id} not found for exploration assignment")
            return False
            
        ant_state = self.my_ants[ant_id]
        move_state = self.ant_movement_state[ant_id]
        
        # IMPORTANT FIX: Check if ant is at the colony - if so, push it slightly away
        ant_x, ant_y = ant_state['x'], ant_state['y'] 
        dist_to_colony_sq = distance_sq(ant_x, ant_y, self.colony_x, self.colony_y)
        if dist_to_colony_sq <= COLONY_INTERACTION_RADIUS_SQ:
            # For exploration, push in a random direction away from colony
            random_angle = random.uniform(0, 2 * math.pi)
            push_distance = COLONY_RADIUS * 1.2  # Push slightly beyond the colony radius
            
            # Calculate new position outside colony radius
            new_x = self.colony_x + push_distance * math.cos(random_angle)
            new_y = self.colony_y + push_distance * math.sin(random_angle)
            
            # Update ant position to be outside colony
            ant_state['x'] = new_x
            ant_state['y'] = new_y
            #print(f"🚀 Pushing ant {ant_id} away from colony for exploration")
            
            # Use this same direction for initial movement
            move_state['direction'] = random_angle
        else:
            # Set a random direction
            random_angle = random.uniform(0, 2 * math.pi)
            move_state['direction'] = random_angle
        
        # Remove from any food assignments
        self.ants_assigned_to_food.discard(ant_id)
        self.ants_assigned_to_explore.add(ant_id)
        
        # Reset ant state for exploration
        ant_state['state'] = STATE_WANDERING
        ant_state['has_food'] = False
        ant_state['target_food_id'] = None
        ant_state['carrying_food_from'] = None
        ant_state['following_trail'] = False
        
        move_state['steps_in_direction'] = 0
        move_state['direction_changes'] = 0
        
        # Clear any path
        move_state['path_points'] = []
        move_state['reusing_path'] = False
        move_state['last_recorded_point'] = None
        
        # Set exploration timeout
        move_state['wander_start_time'] = time.time()
        move_state['wander_timeout'] = random.uniform(WANDER_TIMEOUT_MIN, WANDER_TIMEOUT_MAX)
        move_state['returning_to_colony_timeout'] = False
        
        # Queue an update to show the ant's new state immediately
        self._queue_ant_position_update(ant_id)
        
        #print(f"🔍 Ant {ant_id} assigned to exploration")
        return True
        
    def _handle_ant_at_colony(self, ant_id, path_taken_to_colony=None):
        """Handles ant arrival, food drop, determines next task, and sets state to WAITING."""
        if ant_id not in self.my_ants or ant_id not in self.ant_movement_state:
            #print(f"❌ Ant {ant_id} not found for colony interaction!")
            return False
            
        ant_state = self.my_ants[ant_id]
        move_state = self.ant_movement_state[ant_id]
        # Record this interaction time for cooldown
        move_state['last_colony_interaction_time'] = time.time()
        origin_food_id = None
        current_state = ant_state.get('state', STATE_WANDERING)
        has_food = ant_state.get('has_food', False)

        #print(f"🏠 Ant {ant_id} processing arrival at colony. State: {current_state}, Has food: {has_food}")

        # --- Step 1: Handle Food Drop & Path Registration ---
        if has_food: # Drop food regardless of state if it arrives with food
            origin_food_id = ant_state.get('carrying_food_from')
            #print(f"💰 Ant {ant_id} dropping food (origin: {origin_food_id}).")
            #print(f"💰 FOOD DROP DETAILS: Ant position: ({ant_state['x']:.1f}, {ant_state['y']:.1f}), Colony: ({self.colony_x:.1f}, {self.colony_y:.1f})")
            #print(f"💰 Distance to colony: {math.sqrt(distance_sq(ant_state['x'], ant_state['y'], self.colony_x, self.colony_y)):.1f}")

            # Track the drop for server update
            if origin_food_id is not None:
                if not hasattr(self, 'just_dropped_food_ids'):
                    self.just_dropped_food_ids = []
                    #print(f"💰 INITIALIZED: Created new food drop tracking list")
                
                if ant_id not in self.just_dropped_food_ids: # Avoid double counting if called rapidly
                     self.just_dropped_food_ids.append(ant_id)
                     #print(f"💰 TRACKED: Food drop from ant {ant_id} added to tracking list (total: {len(self.just_dropped_food_ids)})")
                else:
                     #print(f"⚠️ DUPLICATE: Ant {ant_id} already in drop list, not counting twice")
                     pass  # Add pass statement for empty else block
            else:
                #print(f"⚠️ NO ORIGIN: Ant {ant_id} has food but no origin_food_id")
                pass  # Add pass statement for empty else block

            # Clear ant's food state *immediately* to prevent duplicate drops
            ant_state['has_food'] = False
            ant_state['carrying_food_from'] = None
            #print(f"✅ RESET: Cleared food carrying state for ant {ant_id}")

            # Register path only if it came from a valid food source
            if origin_food_id is not None:
                path_from_colony_to_food = list(reversed(path_taken_to_colony)) if path_taken_to_colony else None
                if path_from_colony_to_food:
                    self._register_food_path(origin_food_id, path_from_colony_to_food)
                    # Generate trail data for server visualization
                    trail_data = { 'points': path_from_colony_to_food, 'strength': TRAIL_STRENGTH_INITIAL, 'to_food': True, 'food_id': origin_food_id }
                    if not hasattr(self, 'pending_trails'):
                        self.pending_trails = []
                    self.pending_trails.append(trail_data)
                else:
                    #print(f"⚠️ WARNING: Ant {ant_id} returned with food but has no path history")
                    pass  # Add pass statement for empty else block
        
        # --- Step 2: Determine Next Task ---
        # First, check if we have any valid food target
        best_food_target = self._select_best_food_target()
        
        # Check if the ant still has food (safety)
        if ant_state.get('has_food', False):
            #print(f"⚠️ SAFETY FIX: Ant {ant_id} still had food before transitioning to WAITING state! Fixing...")
            ant_state['has_food'] = False
            ant_state['carrying_food_from'] = None
        
        # Set the ant into waiting state immediately
        ant_state['state'] = STATE_WAITING_FOR_INSTRUCTIONS
        
        # If we have a valid food target, prepare the ant to go to it
        if best_food_target is not None and self._is_valid_food_source(best_food_target):
            #print(f"🎯 Found valid food target {best_food_target} for ant {ant_id}")
            
            # Get the path information
            path_points = None
            if best_food_target in self.known_food_paths:
                path_points = self.known_food_paths[best_food_target].get('path')
                #print(f"🛣️ Known path to food {best_food_target} has {len(path_points)} points")
            else:
                # If no known path, create a direct path
                if best_food_target in self.current_food_sources:
                    food_data = self.current_food_sources[best_food_target]
                    food_x, food_y = food_data.get(KEY_X), food_data.get(KEY_Y)
                    path_points = [(self.colony_x, self.colony_y), (food_x, food_y)]
                    #print(f"📝 Created direct path to food {best_food_target}")
            
            # Set the next task in the move state for _process_ant_logic to handle
            if path_points:
                move_state['next_task_type'] = 'goto_food'
                move_state['next_task_path'] = path_points
                move_state['next_target_food_id'] = best_food_target
                #print(f"🚀 Preparing ant {ant_id} to go to food source {best_food_target} with {len(path_points)} path points")
            else:
                # Default to wander if we can't construct a path
                move_state['next_task_type'] = 'wander'
                move_state['next_task_direction'] = random.uniform(0, 2 * math.pi)
                #print(f"🧭 No path to food {best_food_target}, setting ant {ant_id} to wander")
        else:
            # No valid food target, set to wander
            #print(f"⏳ No valid food target available. Setting ant {ant_id} to wander")
            move_state['next_task_type'] = 'wander'
            move_state['next_task_direction'] = random.uniform(0, 2 * math.pi)
        
        # Queue an update to show the ant's new state
        self._queue_ant_position_update(ant_id)
        
        return True

    # Helper to get dropped food info into the main loop's update message
    def _collect_dropped_food_info(self):
         """Collects info about ants that dropped food in the last step."""
         count = 0
         if hasattr(self, 'just_dropped_food_ids') and self.just_dropped_food_ids:
             # Log the ant IDs that dropped food for debugging
             ids = list(self.just_dropped_food_ids)
             #print(f"🔢 Counting food drops from {len(ids)} ants: {ids}")
             
             # Detailed per-ant reporting
             for ant_id in ids:
                 #print(f"   - Ant {ant_id} food drop confirmed for colony score")
                 pass  # Add pass statement for empty for loop
                 
             count = len(self.just_dropped_food_ids)
             self.just_dropped_food_ids = [] # Clear for next step
             #print(f"🧹 Cleared food drop tracking list for next step")
         else:
             #print(f"ℹ️ No food drops to count this step")
             pass  # Add pass statement for empty else block
         
         total_food = count * FOOD_TAKE_AMOUNT
         if total_food > 0:
             #print(f"📊 Total food dropped this step: {total_food}")
             #print(f"🏆 This will increase colony score by {total_food}")
             pass  # Add pass statement for empty if block
         return total_food
    
    def _reassign_ants_at_colony(self, priority_food_id=None):
        """Reassign all ants at the colony to target the active food source.
        Called when a new path is discovered or updated."""
        target_food_id = priority_food_id if priority_food_id is not None else self.active_food_target
        
        if target_food_id is None:
            # No target, nothing to do
            return
            
        # Verify the target is valid
        if not self._is_valid_food_source(target_food_id):
            # Try to select a new target
            new_target = self._select_best_food_target()
            if new_target is None:
                # No valid targets, can't reassign
                return
            target_food_id = new_target
        
        # Count of ants reassigned
        ants_reassigned = 0
        
        # Find all ants that are near the colony
        for ant_id, ant_state in self.my_ants.items():
            x, y = ant_state.get('x', 0), ant_state.get('y', 0)
            dist_to_colony_sq = distance_sq(x, y, self.colony_x, self.colony_y)
            
            # Check if ant is near colony
            if dist_to_colony_sq < COLONY_INTERACTION_RADIUS_SQ * 2:  # Larger radius to catch more ants
                # Don't reassign ants that are already carrying food
                if ant_state.get('has_food', False):
                    continue
                    
                # Force this ant to go to the food source
                success = self._assign_ant_to_food(ant_id, target_food_id)
                if success:
                    ants_reassigned += 1
                    
        if ants_reassigned > 0:
            #print(f"🔄 Reassigned {ants_reassigned} ants at colony to target food source {target_food_id}")
            pass  # Add pass statement for empty if block
        
        return ants_reassigned

    def _reset_colony_state_for_exploration(self):
        """Reset colony to exploration mode when all known food sources are depleted."""
        #print("=== RESETTING COLONY TO EXPLORATION MODE ===")
        self.colony_exploration_mode = True
        self.active_food_target = None
        
        # Clear paths to depleted food sources
        food_ids_to_remove = []
        for food_id in list(self.known_food_paths.keys()):
            if not self._is_valid_food_source(food_id):
                food_ids_to_remove.append(food_id)
                
        for food_id in food_ids_to_remove:
            self.known_food_paths.pop(food_id, None)
            self.path_stats.pop(food_id, None)
            #print(f"Removed depleted food source {food_id} from known paths")
        
        # Reset all ants to exploration mode
        ants_reset = 0
        for ant_id in self.my_ants:
            if self._assign_ant_to_explore(ant_id):
                ants_reset += 1
                
            # Make sure they get a random walking direction
            if ant_id in self.ant_movement_state:
                self.ant_movement_state[ant_id]['direction'] = random.uniform(0, 2 * math.pi)
                self.ant_movement_state[ant_id]['reusing_path'] = False
                self.ant_movement_state[ant_id]['path_points'] = []
                
                # Clear any preferred food sources
                if 'preferred_food_id' in self.ant_movement_state[ant_id]:
                    self.ant_movement_state[ant_id].pop('preferred_food_id', None)
            
        #print(f"Reset complete. {ants_reset} ants reset to exploration mode.")
        #print(f"All known paths cleared. Colony is now in exploration mode.")
        #print(f"Known valid food sources: {[fid for fid in self.known_food_paths if self._is_valid_food_source(fid)]}")
        #print("=" * 40)

    def _assign_food_to_ant(self, ant_id, food_id):
        """Assign food to an ant and set its state to return to colony."""
        try:
            if ant_id not in self.my_ants or not self._is_valid_food_source(food_id):
                return False, None
                
            ant = self.my_ants[ant_id]
            food = self.current_food_sources.get(food_id)
            move_state = self.ant_movement_state[ant_id]
            
            if not food:
                return False, None
                
            # Mark ant as carrying food
            ant['has_food'] = True
            ant['carrying_food_from'] = food_id
            ant['state'] = STATE_RETURNING
            
            # Save this food source as the ant's preferred food source
            move_state['preferred_food_id'] = food_id
            #print(f"🔖 Setting ant {ant_id}'s preferred food source to {food_id}")
            
            # Set target to colony
            target_x, target_y = self.colony_x, self.colony_y
            
            # If we have a known path to this food, use the reverse path to return
            if food_id in self.known_food_paths:
                try:
                    original_path = self.known_food_paths[food_id]['path']
                    return_path = list(reversed(original_path))
                    #print(f"🛣️ Ant {ant_id} using known return path from food {food_id} to colony: {len(return_path)} points")
                    
                    move_state['path_points'] = return_path
                    move_state['reusing_path'] = True
                    ant['following_trail'] = True
                except Exception as e:
                    #print(f"ERROR creating return path from food {food_id}: {e}")
                    # Fall back to direct movement
                    #print(f"Falling back to direct movement to colony")
                    move_state['path_points'] = []
                    move_state['reusing_path'] = False
                    ant['following_trail'] = False
            else:
                # No path, direct movement to colony
                #print(f"🧭 Ant {ant_id} returning directly to colony from food {food_id}")
                move_state['path_points'] = []
                move_state['reusing_path'] = False
                ant['following_trail'] = False
                
            # Set direction toward colony
            dx = target_x - ant['x']
            dy = target_y - ant['y']
            angle = math.atan2(dy, dx)
            move_state['direction'] = angle
            
            # Register a new path if this is a successful food find
            if food_id not in self.known_food_paths:
                #print(f"📝 Recording new path to food source {food_id}")
                # Generate a direct path from colony to food
                food_coords = (food.get(KEY_X, 0), food.get(KEY_Y, 0))
                direct_path = [
                    (self.colony_x, self.colony_y),  # Start at colony
                    food_coords  # End at food
                ]
                # Register the path
                self._register_food_path(food_id, direct_path)
                
            # Record that this ant found a path to food
            if 'successful_path_to_food' not in move_state or not move_state['successful_path_to_food']:
                # If we made a path to get here, save it for future ants
                if move_state.get('path_points') and len(move_state['path_points']) > 0:
                    move_state['successful_path_to_food'] = list(move_state['path_points'])
                
            # Record this food location
            move_state['last_food_location'] = (food.get(KEY_X, 0), food.get(KEY_Y, 0))
            move_state['last_food_id'] = food_id
            
            #print(f"✅ Ant {ant_id} successfully picked up food from source {food_id}")
            
            # If this is the first ant to find this food source, make it the active target
            if not self.active_food_target or not self._is_valid_food_source(self.active_food_target):
                #print(f"🎯 Setting active food target to {food_id} (discovered by ant {ant_id})")
                self.active_food_target = food_id
                self.colony_exploration_mode = False
                
            return True, food_id
        except Exception as e:
            #print(f"ERROR in _assign_food_to_ant for ant {ant_id} and food {food_id}: {e}")
            return False, None

    def _update_food_sources(self, food_sources_list):
        """Updates local food knowledge based on server list, handling implicit depletion."""
        if not isinstance(food_sources_list, list):
            #print(f"Warning: Expected list for food sources, got {type(food_sources_list)}")
            clear_validation_cache()  # Clear cache after critical food state changes
        return

        # STEP 1: Process incoming food sources from the update
        received_food_sources = {}
        received_valid_ids = set()
        for food_data in food_sources_list:
            if isinstance(food_data, dict):
                food_id = food_data.get(KEY_FOOD_ID)
                if food_id is not None:
                    received_valid_ids.add(food_id)
                    # Store data received from server
                    received_food_sources[food_id] = {
                        KEY_FOOD_ID: food_id,
                        KEY_X: food_data.get(KEY_X),
                        KEY_Y: food_data.get(KEY_Y),
                        'amount': food_data.get('amount'),
                        'radius': food_data.get('radius', FOOD_RADIUS),
                        'depleted': food_data.get('depleted', False) # Use server's depleted status
                    }
                    # Handle newly appeared or re-appearing sources
                    if food_id not in self.current_food_sources and not received_food_sources[food_id]['depleted']:
                         #print(f"✨ New/Reappeared valid food source {food_id} detected in update.")
                         # Add to current sources if it's truly new and not depleted
                         self.current_food_sources[food_id] = received_food_sources[food_id]
                         # Consider selecting as target if needed
                         if self.active_food_target is None:
                             self._select_best_food_target()
                    elif food_id in self.current_food_sources:
                         # Update existing entry with fresh data from server
                         self.current_food_sources[food_id].update(received_food_sources[food_id])
                         # If server says it's depleted now, ensure we know
                         if self.current_food_sources[food_id]['depleted']:
                              #print(f"🏁 Food source {food_id} confirmed depleted via WORLD_UPDATE.")
                              # Trigger cleanup if we didn't know before
                              # self._handle_food_depletion(food_id) # Might be redundant if MSG_S_FOOD_DEPLETED is reliable
                              pass  # Add pass statement for empty if block

            else:
                #print(f"Warning: Invalid item in food sources list: {food_data}")
                pass  # Add pass statement for empty else block


        # STEP 2: Check existing known sources against the received IDs
        current_known_ids = set(self.current_food_sources.keys())
        ids_now_missing = current_known_ids - received_valid_ids

        # STEP 3: Mark missing sources as depleted and trigger full cleanup
        for food_id in ids_now_missing:
            if food_id in self.current_food_sources and not self.current_food_sources[food_id].get('depleted', False):
                #print(f"👻 Food source {food_id} is known locally but MISSING from server update. Marking as depleted and cleaning up.")
                # Mark as depleted first
                self.current_food_sources[food_id]['depleted'] = True
                self.current_food_sources[food_id]['amount'] = 0
                # Trigger the full depletion handling logic to remove paths and reset ants
                self._handle_food_depletion(food_id) # Ensures consistency
            elif food_id not in self.current_food_sources:
                 # This case should ideally not happen if logic is correct elsewhere
                 #print(f"Logic Warning: Food ID {food_id} was missing from server update but also not in local current_food_sources.")
                 pass  # Add pass statement for empty elif block


        # STEP 4: Optionally re-evaluate target (can be redundant if _handle_food_depletion does it)
        # if self.active_food_target is None or self.active_food_target not in self.current_food_sources or self.current_food_sources.get(self.active_food_target, {}).get('depleted', False):
        #    #print(f"Re-selecting best food target after processing WORLD_UPDATE.")
        #    self._select_best_food_target()

    def _update_other_colonies(self, colonies_list):
        """Updates local knowledge of other colonies."""
        if not isinstance(colonies_list, list):
            #print(f"Warning: Expected list for colonies, got {type(colonies_list)}")
            return

        #print(f"[DEBUG] Updating other colonies from list: {colonies_list}")
        self.other_colonies = {}  # Reset and update
        for colony_data in colonies_list:
            if isinstance(colony_data, dict):
                # Check for 'id' first, then fall back to KEY_COLONY_ID
                colony_id = colony_data.get('id')
                if colony_id is None:
                    colony_id = colony_data.get(KEY_COLONY_ID)
                
                if colony_id is not None and colony_id != self.colony_id:
                    self.other_colonies[colony_id] = {
                        KEY_X: colony_data.get(KEY_X),
                        KEY_Y: colony_data.get(KEY_Y),
                        KEY_COLOR: colony_data.get(KEY_COLOR),
                        KEY_SCORE: colony_data.get(KEY_SCORE)  # Store score too
                    }
                    #print(f"[DEBUG] Added other colony {colony_id} at ({colony_data.get(KEY_X)}, {colony_data.get(KEY_Y)})")
                elif colony_id == self.colony_id:
                    # This is MY colony - update my position based on server
                    self.colony_x = float(colony_data.get(KEY_X, self.colony_x))
                    self.colony_y = float(colony_data.get(KEY_Y, self.colony_y))
                    #print(f"[DEBUG] Updated my colony {colony_id} position to ({self.colony_x}, {self.colony_y})")
            else:
                #print(f"Warning: Invalid item in colonies list: {colony_data}")
                pass  # Add pass statement for empty else block


# --- Main Execution ---
if __name__ == '__main__':
    # Basic argument parsing (can be improved with argparse)
    server_ip = '127.0.0.1' # Default to localhost
    server_port_num = 23000 # Default port

    if len(sys.argv) > 1:
        server_ip = sys.argv[1]
    if len(sys.argv) > 2:
        try:
            server_port_num = int(sys.argv[2])
        except ValueError:
            #print(f"Invalid port number: {sys.argv[2]}. Using default {server_port_num}.")
            pass  # Add pass statement for empty except block

    client = ColonyClient(server_ip, server_port_num)

    if client.connect():
        #print("Client connected and running. Press Ctrl+C to stop.")
        try:
            # Keep main thread alive while logic/network threads run
            while client.network_running:
                time.sleep(1)
        except KeyboardInterrupt:
            #print("Ctrl+C detected. Stopping client...")
            pass  # Add pass statement for empty except block
        finally:
            client.disconnect()
    else:
        #print("Failed to connect to the server.")
        pass  # Add pass statement for empty else block

    #print("Client terminated.")

# Add at the very end of the file

if __name__ == "__main__":
    try:
        if len(sys.argv) < 3:
            #print("Usage: python colony_client.py <server_host> <server_port>")
            sys.exit(1)
        
        host = sys.argv[1]
        port = int(sys.argv[2])
        
        client = ColonyClient(host, port)
        client.connect()
        
    except IndentationError as e:
        line_num = e.lineno
        #print(f"IndentationError at line {line_num}: {e}")
        #print("\nTry fixing the indentation in the code around this line.")
        #print("You may need to check the following functions which have known indentation issues:")
        #print("1. _check_and_pickup_food (around line 958)")
        #print("2. _process_ant_logic (around line 789)")
        #print("3. _handle_ant_at_colony (around line 1460)")
        #print("4. _select_best_food_target (around line 1126)")
        #print("5. _register_food_path (around line 1101)")
        sys.exit(1)
    except Exception as e:
        #print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)