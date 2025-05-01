# simulation_server.py
import tkinter as tk
from tkinter import messagebox
import socket
import selectors
import types # For selector data
import time
import math
import random
import json
import threading # For accepting connections without blocking GUI
import struct

# Import from shared utils
from shared_utils import (
    WORLD_WIDTH, WORLD_HEIGHT, COLONY_RADIUS, FOOD_RADIUS, ANT_RADIUS,
    INITIAL_FOOD_AMOUNT, UPDATE_DELAY_MS, SERVER_HOST, SERVER_PORT_BASE, MAX_CLIENTS,
    HEADER_LENGTH, encode_message, decode_message, MSG_C_ANT_UPDATES, MSG_C_NEW_TRAILS,
    MSG_S_WELCOME, MSG_S_WORLD_UPDATE, MSG_S_FOOD_DEPLETED, MSG_S_NEW_FOOD, MSG_S_SHUTDOWN,
    KEY_MSG_TYPE, KEY_DATA, KEY_COLONY_ID, KEY_COLOR, KEY_ANTS, KEY_ANT_ID, KEY_X, KEY_Y,
    KEY_STATE, KEY_HAS_FOOD, KEY_FOOD_SOURCES, KEY_FOOD_ID, KEY_FOOD_AMOUNT,
    KEY_FOOD_TAKEN, KEY_FOOD_DROPPED, KEY_COLONIES, KEY_TRAILS, KEY_SCORE, KEY_TIMESTAMP,
    get_colony_color, distance_sq, STATUS_COLORS, MSG_C_CONNECT_REQUEST,
    TRAIL_STRENGTH_INITIAL, TRAIL_DECAY_RATE, MAX_FOOD_SOURCES
)

MAX_CLIENTS = 7

class SimulationServer:
    def __init__(self, master):
        self.master = master
        self.master.title("Ant Colony Simulation Server")
        self.master.geometry(f"{WORLD_WIDTH}x{WORLD_HEIGHT+150}")  # Extra space for controls
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Add these attributes for world dimensions
        self.world_width = WORLD_WIDTH
        self.world_height = WORLD_HEIGHT
        
        # --- Simulation State ---
        self.running = False
        self.server_active = False
        self.simulation_active = False
        self.colonies = {}  # {colony_id: {x, y, color, score, 'trails':[]}}
        self.food_sources = {} # {food_id: {x, y, amount, radius, 'depleted': False, canvas_id, text_id}}
        self.ants = {}      # {ant_id: {x, y, colony_id, color, has_food, state, canvas_id}}
        self.next_colony_id = 0
        self.next_food_id = 0
        self.last_update_time = time.time()

        # --- Networking State ---
        self.server_socket = None
        self.selector = selectors.DefaultSelector()
        self.client_connections = {} # {conn_socket: {'colony_id': id, 'status': '...', 'addr': addr}}
        self._lock = threading.Lock() # To protect shared resources accessed by network thread

        # Dummy status_label for compatibility with existing code that references it
        self.status_label = type('DummyLabel', (), {'config': lambda **kwargs: None})()
        
        # Dummy add_food_label for compatibility
        self.add_food_label = type('DummyLabel', (), {'config': lambda **kwargs: None})()
        
        # --- GUI Setup ---
        # Control Frame
        control_frame = tk.Frame(master)
        control_frame.pack(pady=5)
        self.btn_start = tk.Button(control_frame, text="Start", width=10, command=self.start_simulation)
        self.btn_start.pack(side=tk.LEFT, padx=5)
        
        # Create Add Food button but don't display it initially
        self.btn_add_food = tk.Button(control_frame, text="Add Food", width=10, command=self.enable_add_food_mode)
        # Don't pack it initially: self.btn_add_food.pack(side=tk.LEFT, padx=5)
        self.add_food_mode = False
        
        # World Canvas
        self.canvas = tk.Canvas(master, width=WORLD_WIDTH, height=WORLD_HEIGHT, bg="lightyellow")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.handle_canvas_click) # For adding food

        # Status Frame
        self.status_frame = tk.Frame(master)
        self.status_frame.pack(fill=tk.X, padx=10)
        self.score_labels = {} # {colony_id: Label}
        self.client_node_canvas = tk.Canvas(master, height=40, bg='lightgrey')
        self.client_node_canvas.pack(fill=tk.X, padx=10, pady=5)
        self.client_node_ids = {} # {colony_id: (rect_id, text_id)}

        # Initialize and start server automatically
        self.reset_simulation_state() # Sets up initial state
        self.start_server() # Auto-start the server, but not the simulation


    # --- Server Control ---
    def _is_port_in_use(self, port):
        """Check if the specified port is already in use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((SERVER_HOST, port))
                return False
            except OSError:
                return True

    def start_server(self):
        if self.server_active: return
        ##print(f"[Thread: {threading.current_thread().name}] Starting server...")
        
        # Check if port is already in use
        if self._is_port_in_use(SERVER_PORT_BASE):
            ##print(f"[Thread: {threading.current_thread().name}] Port {SERVER_PORT_BASE} already in use")
            messagebox.showerror("Server Error", f"Port {SERVER_PORT_BASE} is already in use. Is another instance running?")
            return
            
        self.reset_simulation_state() # Clear previous state
        # No initial food sources - start with 0 food piles
        
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Set socket option to allow reuse of the address
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Add a timeout to ensure socket operations don't block indefinitely
            self.server_socket.settimeout(1.0)
            self.server_socket.bind((SERVER_HOST, SERVER_PORT_BASE))
            self.server_socket.listen(MAX_CLIENTS)
            self.server_socket.setblocking(False) # Important for selector
            self.selector.register(self.server_socket, selectors.EVENT_READ, data=None) # Data is None for listener
            ##print(f"[Thread: {threading.current_thread().name}] Server listening on {SERVER_HOST}:{SERVER_PORT_BASE}")

            # No longer need separate accept thread - now handled in _run_step

            self.server_active = True
            self.running = True # Keep event loop running
            self.simulation_active = False # Don't advance simulation until button is pressed
            self.last_update_time = time.time()
            
            # Configure UI buttons for initial state
            self.btn_start.config(text="Start", state=tk.NORMAL)
            
            # Start the update loop immediately to show the UI
            self.master.after(UPDATE_DELAY_MS, self._run_step)
            ##print(f"[Thread: {threading.current_thread().name}] Server running, waiting for simulation start.")

        except OSError as e:
            ##print(f"[Thread: {threading.current_thread().name}] Error starting server: {e}")
            messagebox.showerror("Server Error", f"Could not bind to port {SERVER_PORT_BASE}. Is another instance running?")
            self._cleanup_network()

    def _notify_clients_state_change(self, message_type=None, additional_data=None):
        """Send a state update to all clients."""
        if not self.server_active or not self.client_connections:
            return  # No clients to notify
            
        # Create basic update data
        if message_type is None:
            message_type = MSG_S_WORLD_UPDATE
            
        # Prepare data for a world update
        active_food_sources = [f for f in self.food_sources.values() if not f.get('depleted', False)]
        update_data = {
            KEY_FOOD_SOURCES: active_food_sources,
            KEY_COLONIES: list(self.colonies.values()),  # Include all colony data
            KEY_TIMESTAMP: time.time(),
            'simulation_active': self.simulation_active
        }
        
        # Add any additional data
        if additional_data and isinstance(additional_data, dict):
            update_data.update(additional_data)
            
        # Encode the message
        update_msg = encode_message(message_type, update_data)
        
        # Send to all clients
        client_sockets = list(self.client_connections.keys())
        successful_sends = 0
        for sock in client_sockets:
            try:
                sock.sendall(update_msg)
                successful_sends += 1
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                ##print(f"Error sending update to client: {e}")
                # Don't close connection here - let the normal error handling do it
                pass  # Add pass statement for empty except block
        
        ##print(f"Sent {message_type} update to {successful_sends}/{len(client_sockets)} clients")
        return successful_sends

    def _show_temp_message(self, message, duration_ms=3000):
        """This method no longer shows messages (UI simplified)."""
        # All messages have been removed from the UI
        pass

    def reset_simulation(self):
        """Reset the simulation to initial state but keep colonies and connections."""
        #print("Resetting simulation...")
        
        with self._lock:
            # Keep track of existing colonies
            existing_colonies = self.colonies.copy()
            
            # Clear ants but preserve colonies
            self.ants = {}  # Reset all ants
            
            # Clear food sources
            self.food_sources = {}
            self.next_food_id = 0
            
            # Reset colony scores but maintain colony positions and colors
            for colony_id, colony in existing_colonies.items():
                colony['score'] = 0  # Reset score
                colony['trails'] = []  # Clear trails
            
            # Keep colonies but reset their state
            self.colonies = existing_colonies
            
            # Clear canvas but maintain client connections
            self.canvas.delete("all")
            
            # Force redraw of everything
            self._draw_simulation()
            self._update_scores()
            self._update_client_nodes()
        
        # Update UI state
        self.simulation_active = False
        self.btn_start.config(text="Start", state=tk.NORMAL)
        
        # Hide Add Food button after reset
        self.btn_add_food.pack_forget()
        
        # Show confirmation message
        self._show_temp_message("Simulation reset complete", 3000)
        
        # Notify all clients that simulation has been reset
        self._notify_clients_state_change(additional_data={'reset': True})
        
        #print("Simulation reset complete - colonies maintained, ants reset.")

    def start_simulation(self):
        """Start or pause/resume the simulation after server is running."""
        if not self.server_active:
            # If server not running, start it first
            self.start_server()
            return
            
        if self.simulation_active:
            # If already running, this is a pause button
            self.simulation_active = False
            self.btn_start.config(text="Resume")
            #print("Simulation paused.")
            
            # Hide Add Food button when paused
            self.btn_add_food.pack_forget()
            
            # Add a right-click function for reset
            self.btn_start.bind("<Button-3>", lambda event: self.reset_simulation())
            # Show instructions with our helper method
            self._show_temp_message("Right-click Resume to reset", 5000)
        else:
            # Start or resume the simulation
            self.simulation_active = True
            self.btn_start.config(text="Pause")
            #print("Simulation started/resumed.")
            
            # Show Add Food button when simulation is running
            self.btn_add_food.pack(side=tk.LEFT, padx=5)
            
            # Remove right-click binding and tooltip when running
            self.btn_start.unbind("<Button-3>")
            
            self.last_update_time = time.time()  # Reset time to avoid large time jumps

    def stop_server(self):
        if not self.server_active: return
        #print("Stopping server...")
        self.running = False # Stop simulation loop first
        self.server_active = False
        self.simulation_active = False

        # Send shutdown message to all clients
        #print(f"Notifying {len(self.client_connections)} clients of shutdown...")
        # Using proper message format for SHUTDOWN
        shutdown_msg = encode_message(MSG_S_SHUTDOWN, {})
        client_sockets = list(self.client_connections.keys()) # Copy keys
        for sock in client_sockets:
             try:
                 #print(f"  Sending SHUTDOWN to {self.client_connections.get(sock, {}).get('addr')}")
                 sock.sendall(shutdown_msg)
             except (BrokenPipeError, ConnectionResetError, OSError) as e:
                 #print(f"    Error sending shutdown to a client: {e}")
                 pass  # Add pass statement for empty except block
             finally:
                self._close_client_connection(sock) # Close immediately after notifying

        # Cleanup network resources
        self._cleanup_network()

        self.btn_start.config(text="Start", state=tk.NORMAL)
        
        # Hide Add Food button when server is stopped
        self.btn_add_food.pack_forget()
        
        self._update_client_nodes() # Show all as disconnected
        #print("Server stopped.")

    def _cleanup_network(self):
        """Safely close sockets and selector."""
        #print("Cleaning up network resources...")
        if self.selector:
            # Unregister and close client sockets
            client_sockets = list(self.client_connections.keys())
            for sock in client_sockets:
                self._close_client_connection(sock)

            # Unregister and close server socket
            if self.server_socket:
                try:
                    self.selector.unregister(self.server_socket)
                except (KeyError, ValueError, OSError) as e: 
                    #print(f"Error unregistering server socket: {e}")
                    pass # Ignore if already unregistered or closed
                try:
                    # Close socket with a linger value of 0 to force close
                    self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, 
                                               struct.pack('ii', 1, 0))
                    self.server_socket.close()
                    #print("Server socket closed.")
                except OSError as e: 
                    #print(f"Error closing server socket: {e}")
                    pass # Ignore if already closed
                self.server_socket = None

            # Close the selector itself
            try:
                self.selector.close()
                #print("Selector closed.")
            except Exception as e:
                #print(f"Error closing selector: {e}")
                pass  # Add pass statement for empty except block
            self.selector = selectors.DefaultSelector() # Create a fresh selector

        self.client_connections = {}


    # --- Simulation Step (Main Thread) ---
    def _run_step(self):
        if not self.running: return

        current_time = time.time()
        
        # Only update simulation time and physics if simulation_active
        if self.simulation_active:
            delta_time = current_time - self.last_update_time
            self.last_update_time = current_time
        else:
            # When paused, we still want to process network events
            # but we don't advance the simulation
            delta_time = 0
        
        # Add thread ID to log
        #print(f"[Thread: {threading.current_thread().name}] Run step starting {'(ACTIVE)' if self.simulation_active else '(PAUSED)'}")

        # --- 1. Network Operations (always active) ---
        # Process network events even when simulation is paused, so clients can connect/disconnect

        # Create a snapshot of the current state to send to clients
        # CRITICAL FIX: Only include non-depleted food sources in WORLD_UPDATE
        active_food_sources = [f for f in self.food_sources.values() if not f.get('depleted', False)]
        world_update_data = {
            # Only send non-depleted food sources in regular updates
            KEY_FOOD_SOURCES: active_food_sources,
            KEY_COLONIES: list(self.colonies.values()), # Include scores, positions
            KEY_TIMESTAMP: current_time,
            'simulation_active': self.simulation_active  # Tell clients if simulation is running
        }
        #print(f"Preparing WORLD_UPDATE with {len(active_food_sources)} active food sources")
        world_update_msg = encode_message(MSG_S_WORLD_UPDATE, world_update_data)

        # --- Process Network Events (Receive Data) ---
        #print(f"[Thread: {threading.current_thread().name}] Run step - checking {len(self.client_connections)} client connections")
        events = []
        try:
            if self.selector and self.selector.get_map(): # Check if selector is valid and has registrations
                #print(f"[Thread: {threading.current_thread().name}] Run step - selector has {len(self.selector.get_map())} registered objects")
                events = self.selector.select(timeout=0.001) # Short timeout, don't block GUI
                if events:
                    #print(f"[Thread: {threading.current_thread().name}] Run step - Got {len(events)} selector events")
                    pass  # Add pass statement for empty if block
        except ValueError: # Selector closed
            #print(f"[Thread: {threading.current_thread().name}] _run_step: Selector seems closed.")
            self.stop_server()
            return
        except Exception as e:
            #print(f"[Thread: {threading.current_thread().name}] Error during selector.select: {e}")
            self.stop_server()
            return

        received_updates = {} # {colony_id: {'ant_updates': [], 'new_trails': [], 'food_taken': 0, 'food_dropped': 0}}
        sockets_to_close = []

        for key, mask in events:
            if key.fileobj == self.server_socket:
                # NEW: Accept new connection in the main thread
                #print(f"[Thread: {threading.current_thread().name}] New connection event - handling in main thread")
                self._accept_new_connection(key.fileobj)
                continue
            elif key.data is not None:
                # Existing client connection sent data
                sock = key.fileobj
                client_info = key.data
                colony_id = client_info.get('colony_id')
                if colony_id is None:
                    #print(f"[Thread: {threading.current_thread().name}] Warning: Client without colony_id")
                    sockets_to_close.append(sock)
                    continue
                
                #print(f"[Thread: {threading.current_thread().name}] Processing data from colony {colony_id}")

                try:
                    # Receive data
                    recv_data = sock.recv(4096) # Receive chunk
                    if recv_data:
                        #print(f"[Thread: {threading.current_thread().name}] Received {len(recv_data)} bytes from colony {colony_id}")
                        client_info['buffer'] += recv_data
                        # Process buffer for complete messages
                        while True:
                            buf = client_info['buffer']
                            if len(buf) < HEADER_LENGTH:
                                break # Not enough data for header

                            header = buf[:HEADER_LENGTH]
                            try:
                                msg_len = int(header.decode('utf-8').strip())
                            except ValueError:
                                #print(f"Error: Invalid header from {client_info['addr']}: {header}. Closing connection.")
                                sockets_to_close.append(sock)
                                break # Stop processing buffer for this client

                            if len(buf) < HEADER_LENGTH + msg_len:
                                break # Not enough data for the full message yet

                            # We have a complete message
                            payload_bytes = buf[HEADER_LENGTH : HEADER_LENGTH + msg_len]
                            client_info['buffer'] = buf[HEADER_LENGTH + msg_len:] # Remove processed message

                            msg_type, data = decode_message(header, payload_bytes)
                            #print(f"[Thread: {threading.current_thread().name}] Decoded message type: {msg_type} from colony {colony_id}")

                            if msg_type and data:
                                # --- Handle specific message types ---
                                if msg_type == MSG_C_ANT_UPDATES:
                                    if colony_id not in received_updates:
                                        received_updates[colony_id] = {'ant_updates': [], 'new_trails': [], KEY_FOOD_TAKEN: 0, KEY_FOOD_DROPPED: 0}
                                    
                                    ant_count = len(data.get(KEY_ANTS, []))
                                    received_updates[colony_id]['ant_updates'].extend(data.get(KEY_ANTS, []))
                                    received_updates[colony_id][KEY_FOOD_TAKEN] += data.get(KEY_FOOD_TAKEN, 0)
                                    received_updates[colony_id][KEY_FOOD_DROPPED] += data.get(KEY_FOOD_DROPPED, 0)
                                    received_updates[colony_id]['new_trails'].extend(data.get(KEY_TRAILS, [])) # Allow trails in update message
                                    
                                    # Process food_sources_taken directly
                                    food_sources_taken = data.get('food_sources_taken', {})
                                    if food_sources_taken:
                                        if 'food_sources_taken' not in received_updates[colony_id]:
                                            received_updates[colony_id]['food_sources_taken'] = {}
                                        for food_id_str, amount in food_sources_taken.items():
                                            food_id_key = str(food_id_str)  # Ensure it's a string key
                                            received_updates[colony_id]['food_sources_taken'][food_id_key] = \
                                                received_updates[colony_id]['food_sources_taken'].get(food_id_key, 0) + amount
                                            #print(f"[Thread: {threading.current_thread().name}] Processed food taken from colony {colony_id}: Food {food_id_key} = {amount}")
                                            
                                    client_info['status'] = 'active' # Mark as active upon receiving data
                                    #print(f"[Thread: {threading.current_thread().name}] Processed ANT_UPDATES from colony {colony_id} with {ant_count} ants")

                                elif msg_type == MSG_C_NEW_TRAILS: # If sent separately
                                     if colony_id not in received_updates:
                                         received_updates[colony_id] = {'ant_updates': [], 'new_trails': [], KEY_FOOD_TAKEN: 0, KEY_FOOD_DROPPED: 0}
                                     trail_count = len(data.get(KEY_TRAILS, []))
                                     received_updates[colony_id]['new_trails'].extend(data.get(KEY_TRAILS, []))
                                     #print(f"[Thread: {threading.current_thread().name}] Processed NEW_TRAILS from colony {colony_id} with {trail_count} trails")

                                elif msg_type == MSG_C_CONNECT_REQUEST:
                                    # Should have been handled during accept, but handle gracefully if resent
                                    #print(f"Received unexpected CONNECT_REQUEST from already connected colony {colony_id}")
                                    # Maybe resend welcome?
                                    initial_state = {
                                        KEY_COLONY_ID: colony_id, KEY_COLOR: self.colonies[colony_id]['color'],
                                        KEY_COLONIES: list(self.colonies.values()), KEY_FOOD_SOURCES: list(self.food_sources.values()),
                                        'world_width': WORLD_WIDTH, 'world_height': WORLD_HEIGHT, 'server_time': time.time()
                                    }
                                    welcome_msg = encode_message(MSG_S_WELCOME, initial_state)
                                    sock.sendall(welcome_msg)

                                else:
                                    #print(f"Received unknown message type '{msg_type}' from colony {colony_id}")
                                    pass  # Add pass statement for empty else block
                            else:
                                 # Decoding failed
                                 #print(f"Failed to decode message from {client_info['addr']}. Payload snippet: {payload_bytes[:100]}")
                                 sockets_to_close.append(sock) # Close connection on bad data

                    else:
                        # Client disconnected gracefully
                        #print(f"Client {client_info['addr']} (Colony {colony_id}) disconnected.")
                        sockets_to_close.append(sock)

                except (ConnectionResetError, TimeoutError, OSError) as e:
                    #print(f"Network error with client {client_info.get('addr', 'unknown')}: {e}")
                    sockets_to_close.append(sock)
                except Exception as e:
                    #print(f"Unhandled error processing client {client_info.get('addr', 'unknown')} data: {e}")
                    import traceback
                    traceback.print_exc()
                    sockets_to_close.append(sock)
            else:
                #print(f"[Thread: {threading.current_thread().name}] Warning: Event for socket with no data")
                pass  # Add pass statement for empty else block

        # Close sockets marked for closure
        for sock in sockets_to_close:
            self._close_client_connection(sock)

        # --- 2. Simulation Update (only if active) ---
        if self.simulation_active:
            depleted_food_ids = [] # Keep track of newly depleted sources in this step

            for colony_id, updates in received_updates.items():
                if colony_id not in self.colonies: continue

                # Update Ant positions and states
                for ant_update in updates.get('ant_updates', []):
                    ant_id = ant_update.get(KEY_ANT_ID)
                    col_id_check = ant_update.get(KEY_COLONY_ID) # Client should include this
                    if ant_id is not None and col_id_check == colony_id: # Ensure ant belongs to sender
                        # If ant doesn't exist server-side yet, create it
                        if ant_id not in self.ants:
                            self.ants[ant_id] = {'canvas_id': None} # Initialize basic dict

                        # Update server's knowledge of the ant
                        self.ants[ant_id].update({
                            'id': ant_id, # Ensure ID is set
                            KEY_X: ant_update.get(KEY_X),
                            KEY_Y: ant_update.get(KEY_Y),
                            KEY_COLONY_ID: colony_id,
                            KEY_COLOR: self.colonies[colony_id]['color'],
                            KEY_HAS_FOOD: ant_update.get(KEY_HAS_FOOD),
                            KEY_STATE: ant_update.get(KEY_STATE)
                        })

                # Update Colony Score
                food_dropped = updates.get(KEY_FOOD_DROPPED, 0)
                if food_dropped > 0:
                    # Use lock for thread safety when modifying shared colony data
                    with self._lock:
                        self.colonies[colony_id]['score'] += food_dropped

                # --- Process Food Taken Directly ---
                client_food_taken = updates.get('food_sources_taken', {})
                if client_food_taken:
                    #print(f"SERVER: Processing food taken by Colony {colony_id}: {client_food_taken}")
                    for food_id_str, amount_taken in client_food_taken.items():
                        try:
                            food_id = int(food_id_str)
                            # Use lock for thread safety when modifying shared food data
                            with self._lock:
                                if food_id in self.food_sources:
                                    fs = self.food_sources[food_id]
                                    # Check if already depleted *before* trying to take more
                                    if not fs.get('depleted', False) and fs['amount'] > 0:
                                        previous_amount = fs['amount']
                                        fs['amount'] = max(0, fs['amount'] - amount_taken)
                                        #print(f"SERVER UPDATE: Food {food_id} amount: {previous_amount} -> {fs['amount']} (Colony {colony_id} took {amount_taken})")

                                        # Check for depletion *immediately* after taking
                                        if fs['amount'] <= 0:
                                            if food_id not in depleted_food_ids: # Only add once per step
                                                depleted_food_ids.append(food_id)
                                            fs['depleted'] = True
                                            fs['amount'] = 0  # Ensure it's exactly zero
                                            #print(f"!!! SERVER DETECTED DEPLETION: Food {food_id}")
                                    elif fs.get('depleted', False):
                                         #print(f"SERVER WARNING: Colony {colony_id} tried to take {amount_taken} from already depleted food {food_id}")
                                         pass  # Add pass statement for empty elif block
                                    # else: amount was already 0, do nothing
                                else:
                                    #print(f"SERVER ERROR: Colony {colony_id} referenced non-existent food {food_id}")
                                    pass  # Add pass statement for empty else block
                        except (ValueError, TypeError) as e:
                            #print(f"SERVER ERROR processing food taken {food_id_str} from Colony {colony_id}: {e}")
                            pass  # Add pass statement for empty except block
                # --- END Food Taken Processing ---

                # --- Trail Management ---
                # 1. Initialize trails if needed
                if 'trails' not in self.colonies[colony_id]: 
                    self.colonies[colony_id]['trails'] = []
                
                # 2. Add new trails from this update
                self.colonies[colony_id]['trails'].extend(updates.get('new_trails', []))
                
                # 3. Filter out trails to depleted food sources
                updated_trails = []
                for trail in self.colonies[colony_id]['trails']:
                    # Keep trail if:
                    # 1. It's not to food, or
                    # 2. It's to food but the food source is not depleted
                    is_to_food = trail.get('to_food', True)
                    food_id = trail.get('food_id')
                    if not is_to_food or food_id is None or (food_id in self.food_sources and not self.food_sources[food_id].get('depleted', False)):
                        # 4. Apply decay to kept trails
                        trail['strength'] = trail.get('strength', TRAIL_STRENGTH_INITIAL) - (TRAIL_DECAY_RATE * 10) # Decay faster on server view
                        if trail['strength'] > 0.1: # Keep if reasonably strong
                            updated_trails.append(trail)
                
                # 5. Update colony trails with filtered and decayed list, limiting to prevent memory bloat
                self.colonies[colony_id]['trails'] = updated_trails[-20:] # Limit stored trails per colony

            # Notify about depleted food
            if depleted_food_ids:
                #print(f"DEPLETION NOTIFICATION - Processing {len(depleted_food_ids)} depleted food sources: {depleted_food_ids}")
                # Use lock for safety when iterating connections while potentially modifying them elsewhere
                client_sockets_copy = []
                with self._lock:
                    client_sockets_copy = list(self.client_connections.keys())

                # Send notification for each depleted food source
                for food_id in depleted_food_ids:
                    #print(f"SENDING DEPLETION NOTIFICATION TO ALL CLIENTS FOR FOOD {food_id}")
                    
                    # Create depletion message (outside the client loop)
                    depletion_data = {KEY_FOOD_ID: food_id}
                    depletion_msg = encode_message(MSG_S_FOOD_DEPLETED, depletion_data)

                    # Send to all connected clients
                    sent_count = 0
                    for sock in client_sockets_copy:
                        # Check if socket is still valid before sending
                        if sock in self.client_connections:
                            colony_info = self.client_connections.get(sock, {})
                            colony_id = colony_info.get('colony_id', '?')
                            try:
                                sock.sendall(depletion_msg)
                                sent_count += 1
                                #print(f"  - Sent depletion notification for food {food_id} to colony {colony_id}")
                            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                                #print(f"Error sending FOOD_DEPLETED to client {colony_info.get('addr', '?')}: {e}")
                                # Mark for potential closure later if needed, but don't disrupt this loop
                                pass  # Add pass statement for empty except block
                    
                    #print(f"Successfully sent food depletion notification for food {food_id} to {sent_count}/{len(client_sockets_copy)} clients")
                    
                    # Also remove this food source from the food_sources dictionary
                    with self._lock:
                        if food_id in self.food_sources:
                            # Mark as depleted but don't remove until next draw
                            self.food_sources[food_id]['depleted'] = True
                            self.food_sources[food_id]['amount'] = 0

        # --- 3. Send Updates to Clients (always active) ---
        # Send regular world state update to all clients
        client_sockets = list(self.client_connections.keys()) # Iterate over copy
        for sock in client_sockets:
            try:
                sock.sendall(world_update_msg)
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                #print(f"Error sending WORLD_UPDATE to client: {e}")
                # Don't close immediately, let next receive handle it
                pass  # Add pass statement for empty except block

        # --- 4. Update GUI (always active) ---
        # Use after_idle to ensure UI updates happen in the main thread
        self.master.after_idle(self._draw_simulation)
        self.master.after_idle(self._update_scores)
        self.master.after_idle(self._update_client_nodes)
        
        # --- 5. Schedule Next Step ---
        if self.running:
            self.master.after(UPDATE_DELAY_MS, self._run_step)

    # --- GUI Drawing and Updates ---

    def reset_simulation_state(self):
        """Clears colonies, food, ants state."""
        #print("Resetting simulation state...")
        with self._lock:
            self.colonies = {}
            self.food_sources = {}
            self.ants = {}
            self.next_colony_id = 0
            self.next_food_id = 0
            # Clear GUI elements related to dynamic state
            self.canvas.delete("all")
            for label in self.score_labels.values(): label.destroy()
            self.score_labels = {}
            self.client_node_canvas.delete("all")
            self.client_node_ids = {}


    def _add_food_source(self, x, y, amount):
        """Adds a new food source to the simulation state."""
        # Check if we've reached the maximum allowed food sources (excluding depleted ones)
        active_food_sources = sum(1 for food in self.food_sources.values() if not food.get('depleted', False))
        
        if active_food_sources >= MAX_FOOD_SOURCES:
            #print(f"Maximum number of food sources ({MAX_FOOD_SOURCES}) reached. Cannot add more.")
            messagebox.showinfo("Food Sources Limit", f"Maximum number of food sources ({MAX_FOOD_SOURCES}) reached.")
            return None
            
        food_id = self.next_food_id
        self.next_food_id += 1
        self.food_sources[food_id] = {
            KEY_FOOD_ID: food_id,
            'x': x, 'y': y,
            'amount': amount,
            'radius': FOOD_RADIUS,
            'depleted': False,
            'canvas_id': None, # Will be created in draw
            'text_id': None,   # Will be created in draw
            'text_bg': None
        }
        #print(f"Added food source {food_id} at ({x:.1f}, {y:.1f}) with amount {amount}. {active_food_sources + 1}/{MAX_FOOD_SOURCES} sources active.")
        
        # Notify clients about the new food source immediately
        if self.running:
            new_food_data = self.food_sources[food_id].copy() # Send copy
            new_food_msg = encode_message(MSG_S_NEW_FOOD, new_food_data)
            client_sockets = list(self.client_connections.keys()) # Iterate over copy
            for sock in client_sockets:
                 try:
                     sock.sendall(new_food_msg)
                 except (BrokenPipeError, ConnectionResetError, OSError) as e:
                     #print(f"Error sending NEW_FOOD update: {e}")
                     pass  # Add pass statement for empty except block
                     
        return food_id


    def enable_add_food_mode(self):
        """Activates mode where next canvas click adds food."""
        self.add_food_mode = True
        self._show_temp_message("Click canvas to place food...")

    def handle_canvas_click(self, event):
        """Handles clicks on the canvas, primarily for adding food."""
        if self.add_food_mode:
            x, y = event.x, event.y
            
            # Check if we've reached the maximum allowed food sources
            active_food_count = sum(1 for food in self.food_sources.values() if not food.get('depleted', False))
            if active_food_count >= MAX_FOOD_SOURCES:
                #print(f"Cannot add more food: Maximum number of food sources ({MAX_FOOD_SOURCES}) reached")
                messagebox.showinfo("Food Limit Reached", f"Maximum number of food sources ({MAX_FOOD_SOURCES}) reached. Wait for some to be depleted.")
                self.add_food_mode = False
                return
                
            # Ensure food isn't placed right on top of a colony
            valid_placement = True
            for cid, colony in self.colonies.items():
                if distance_sq(x, y, colony['x'], colony['y']) < (COLONY_RADIUS * 2)**2:
                    valid_placement = False
                    #print("Cannot place food too close to colony.")
                    messagebox.showinfo("Invalid Placement", "Cannot place food too close to a colony.")
                    break
                    
            if valid_placement:
                 food_id = self._add_food_source(x, y, INITIAL_FOOD_AMOUNT)
                 if food_id is not None:
                     #print(f"Added new food source {food_id} at ({x}, {y})")
                     self._show_temp_message(f"Added food at ({int(x)}, {int(y)})")
                     # Redraw immediately to show the new food
                     self._draw_simulation()
                 else:
                     #print("Failed to add food source.")
                     pass  # Add pass statement for empty else block

            # Disable add food mode after click
            self.add_food_mode = False
        else:
             # Handle other clicks if needed
             pass


    def _setup_status_ui_if_needed(self):
         """Creates/updates score labels and client node visuals if changed."""
         # Check if score labels or node visuals need update
         update_scores = False
         update_nodes = False

         current_ids_in_ui = set(self.score_labels.keys())
         active_colony_ids = set(self.colonies.keys())
         
         #print(f"Setup UI - Current IDs in UI: {current_ids_in_ui}")
         #print(f"Setup UI - Active colony IDs: {active_colony_ids}")
         
         if current_ids_in_ui != active_colony_ids:
             #print(f"UI needs update - colonies changed: {current_ids_in_ui} vs {active_colony_ids}")
             update_scores = True
             update_nodes = True # Nodes likely need update too

         # Also check if node count changed
         if len(self.client_node_ids) != len(self.client_connections):
             #print(f"UI needs update - connection count changed: {len(self.client_node_ids)} vs {len(self.client_connections)}")
             update_nodes = True

         if update_scores:
             # Clear old labels
             for label in self.score_labels.values(): label.destroy()
             self.score_labels = {}
             # Create score labels for current colonies
             sorted_colonies = sorted(self.colonies.items())
             for i, (cid, colony) in enumerate(sorted_colonies):
                 color = colony['color']
                 score = colony['score']
                 lbl = tk.Label(self.status_frame, text=f"Colony {cid}: {score}", fg=color, font=("Arial", 10, "bold"))
                 # Pack dynamically
                 lbl.pack(side=tk.LEFT, padx=10, pady=2) # Adjust packing if needed
                 self.score_labels[cid] = lbl
                 #print(f"Created score label for colony {cid} with score {score}")

         if update_nodes:
             self.client_node_canvas.delete("all")
             self.client_node_ids = {}
             num_clients = len(self.client_connections)
             node_width=50; node_height=30; padding=8
             canvas_width = self.client_node_canvas.winfo_width() # Get actual width
             if canvas_width <= 1: canvas_width = WORLD_WIDTH # Estimate if not drawn yet
             total_width_needed = num_clients * node_width + max(0, num_clients - 1) * padding
             start_x = max(padding, (canvas_width - total_width_needed) / 2)
             start_y = (40 - node_height) / 2

             # Draw nodes for currently connected clients
             sorted_clients = sorted(self.client_connections.items(), key=lambda item: item[1]['colony_id'])
             #print(f"Creating UI nodes for {len(sorted_clients)} clients")
             for i, (sock, client_info) in enumerate(sorted_clients):
                 cid = client_info['colony_id']
                 status = client_info.get('status', 'disconnected')
                 # Use colony color for outline? Or status color for fill?
                 fill_color = STATUS_COLORS.get(status, 'gray')
                 outline_color = self.colonies[cid]['color'] if cid in self.colonies else 'black'

                 x0 = start_x + i * (node_width + padding); x1 = x0 + node_width
                 y0, y1 = start_y, start_y + node_height
                 rect_id = self.client_node_canvas.create_rectangle(x0, y0, x1, y1, fill=fill_color, outline=outline_color, width=2, tags=f'node_{cid}')
                 text_id = self.client_node_canvas.create_text((x0+x1)/2, (y0+y1)/2, text=f"C{cid}", fill='black')
                 self.client_node_ids[cid] = (rect_id, text_id)
                 #print(f"Created UI node for client {cid} with status {status}")


    def _draw_simulation(self):
        """Draw all entities to the canvas."""
        current_time = time.time() # Get current time for grace period check
        with self._lock:
            try:
                # Clear dynamic elements first
                self.canvas.delete("trails")
                self.canvas.delete("ants")
                self.canvas.delete("food_amount_text")
                self.canvas.delete("border")  # Clear previous border if it exists
                
                # Draw border around the perimeter of the simulation world
                self.canvas.create_rectangle(
                    0, 0, WORLD_WIDTH, WORLD_HEIGHT,
                    outline="black", width=1, tags="border"
                )
                
                # Debug drawing info
                #print(f"Drawing simulation with {len(self.colonies)} colonies and {len(self.ants)} ants.")
                #print(f"Food sources: {list(self.food_sources.keys())}")
                
                # --- Draw Colonies ---
                for colony_id, colony in self.colonies.items():
                    color = colony.get('color', '#888888')
                    x, y = colony.get('x', 0), colony.get('y', 0) # Default to origin if missing
                    r = COLONY_RADIUS

                    # Create colony circle if it doesn't exist or recreate if necessary
                    if colony.get('canvas_id') is None or not self.canvas.coords(colony.get('canvas_id')):
                        colony['canvas_id'] = self.canvas.create_oval(x-r, y-r, x+r, y+r, fill=color, outline="black", width=2, tags="colonies")
                        #print(f"Drew colony {colony_id} at ({x}, {y}) with color {color}, canvas ID: {colony['canvas_id']}")
                    else:
                        self.canvas.coords(colony['canvas_id'], x-r, y-r, x+r, y+r)
                        self.canvas.itemconfig(colony['canvas_id'], fill=color)

                # --- Draw Trails ---
                for colony_id, colony in self.colonies.items():
                    color = colony.get('color', '#888888')
                    trails = colony.get('trails', [])
                    
                    for i, trail in enumerate(trails):
                        points = trail.get('points', [])
                        strength = trail.get('strength', 0.1)
                        is_to_food = trail.get('to_food', True)
                        food_id = trail.get('food_id')  # Get the food ID this trail leads to
                        
                        # Skip drawing trails to depleted food sources
                        if is_to_food and food_id is not None and (food_id not in self.food_sources or self.food_sources[food_id].get('depleted', False)):
                            continue
                            
                        # Convert points to flat coords for canvas
                        if len(points) > 1:
                            # Calculate color based on strength (don't use alpha channel)
                            strength = min(1.0, max(0.1, strength))
                            r = int(int(color[1:3], 16) * strength)
                            g = int(int(color[3:5], 16) * strength)
                            b = int(int(color[5:7], 16) * strength)
                            trail_color = f"#{r:02x}{g:02x}{b:02x}" # RGB format without alpha
                            
                            # Draw line segments
                            for j in range(len(points) - 1):
                                x1, y1 = points[j]
                                x2, y2 = points[j+1]
                                # Draw line for this segment
                                self.canvas.create_line(x1, y1, x2, y2, fill=trail_color, width=2, tags="trails")

                # --- Draw Food Sources ---
                food_items_to_delete = []
                active_food_count = 0
                
                #print(f"Drawing food sources: {list(self.food_sources.keys())}")
                for food_id, food in self.food_sources.items():
                    if food.get('depleted', False):
                        # Ensure depleted food is removed from canvas but NOT from dictionary yet
                        if food.get('canvas_id'): self.canvas.delete(food['canvas_id']); food['canvas_id'] = None
                        if food.get('text_id'): self.canvas.delete(food['text_id']); food['text_id'] = None
                        if food.get('text_bg'): self.canvas.delete(food['text_bg']); food['text_bg'] = None
                        
                        # Check if grace period has passed for permanent removal
                        if 'depleted_at' not in food:
                             food['depleted_at'] = current_time # Mark time first time we see it depleted here
                             #print(f"🕒 Food {food_id} marked depleted internally at {current_time:.1f}")
                        elif current_time - food.get('depleted_at', current_time) > 10.0: # 10 second grace period
                             food_items_to_delete.append(food_id)
                             #print(f"⌛ Grace period over for food {food_id}. Marking for permanent removal.")
                        
                        #print(f"Skipping drawing of depleted food source {food_id} - keeping in state dictionary")
                        continue # Skip drawing depleted food

                    active_food_count += 1
                    x, y = food['x'], food['y']; r = food['radius']
                    amount = food['amount']
                    
                    #print(f"Drawing food source {food_id} with amount {amount}")

                    # Draw circle if it doesn't exist or recreate if necessary
                    if food.get('canvas_id') is None or not self.canvas.coords(food['canvas_id']):
                        food['canvas_id'] = self.canvas.create_oval(x-r, y-r, x+r, y+r, fill="orange", outline="darkorange", width=3, tags="food")
                        food['text_bg'] = None # Ensure text gets recreated too
                        food['text_id'] = None
                        #print(f"Created new canvas elements for food {food_id}")

                    # Update fill color based on amount
                    fill_c = "orange" if amount > INITIAL_FOOD_AMOUNT * 0.1 else "lightgrey"
                    self.canvas.itemconfig(food['canvas_id'], fill=fill_c)

                    # Update amount text
                    if food.get('text_id'): self.canvas.delete(food['text_id'])
                    if food.get('text_bg'): self.canvas.delete(food['text_bg'])

                    bg_size = r * 0.7
                    food['text_bg'] = self.canvas.create_oval(x-bg_size, y-bg_size, x+bg_size, y+bg_size, fill="white", outline="", tags="food_amount_text")
                    food['text_id'] = self.canvas.create_text(x, y, text=f"{int(amount)}", fill="black", font=("Arial", 12, "bold"), tags="food_amount_text")
                    self.canvas.tag_raise(food['text_bg'])
                    self.canvas.tag_raise(food['text_id']) # Ensure text is on top
                    #print(f"Updated display for food {food_id} to show amount {int(amount)}")

                # --- Permanently remove food sources after grace period ---
                if food_items_to_delete:
                    #print(f"🧹 Permanently removing {len(food_items_to_delete)} depleted food sources from server state: {food_items_to_delete}")
                    for fid in food_items_to_delete:
                        self.food_sources.pop(fid, None) # Actually remove from dict
                
                # Just log active count
                #print(f"Active food sources: {active_food_count}/{MAX_FOOD_SOURCES} (Total sources including depleted: {len(self.food_sources)})")
                    
                # --- Draw Ants ---
                self.canvas.delete("ants") # Clear old ant drawings
                for ant_id, ant in self.ants.items():
                    try:
                        # Get ant properties
                        x, y = ant.get('x', 0), ant.get('y', 0)
                        color = ant.get('color', 'gray')
                        has_food = ant.get('has_food', False)
                        r = ANT_RADIUS
                        
                        # Skip drawing ants that are out of bounds
                        if not (0 <= x <= WORLD_WIDTH and 0 <= y <= WORLD_HEIGHT):
                            continue
                            
                        # Customize appearance based on state
                        outline_color = "yellow" if has_food else "black"
                        outline_width = 2 if has_food else 1
                        
                        # Draw ant as a circle
                        ant_canvas_id = self.canvas.create_oval(
                            x-r, y-r, x+r, y+r, 
                            fill=color, 
                            outline=outline_color, 
                            width=outline_width,
                            tags="ants"
                        )
                        
                        # Store canvas ID for future reference
                        ant['canvas_id'] = ant_canvas_id
                        
                    except Exception as e:
                        #print(f"Error drawing ant {ant_id}: {e}")
                        import traceback
                        traceback.print_exc()

                # Ensure proper drawing order
                self.canvas.tag_raise("trails")
                self.canvas.tag_raise("food")
                self.canvas.tag_raise("ants")
                self.canvas.tag_raise("food_amount_text")
                self.canvas.tag_raise("colonies")

            except Exception as e:
                #print(f"Error drawing simulation: {e}")
                import traceback
                traceback.print_exc()


    def _update_scores(self):
        """Updates the score labels."""
        for cid, label in self.score_labels.items():
            score = self.colonies.get(cid, {}).get('score', 0)
            label.config(text=f"Colony {cid}: {score}")

    def _update_client_nodes(self):
        """Updates client node colors based on connection status."""
        self._setup_status_ui_if_needed() # Rebuild if structure changed

        client_statuses = {info['colony_id']: info['status'] for info in self.client_connections.values()}

        for colony_id, (rect_id, text_id) in self.client_node_ids.items():
             status = client_statuses.get(colony_id, 'disconnected') # Default to disconnected if not in active connections
             fill_color = STATUS_COLORS.get(status, 'gray')
             self.client_node_canvas.itemconfig(rect_id, fill=fill_color)
             # Adjust text color for visibility
             text_color = 'white' if fill_color in ['blue','black','purple','darkgreen','red','lime green', 'yellow'] else 'black'
             self.client_node_canvas.itemconfig(text_id, fill=text_color)
         # self.client_node_canvas.update_idletasks() # Might cause flicker, try without


    # --- Cleanup ---
    def on_close(self):
        #print("Window closing...")
        self.stop_server()
        # Ensure cleanup is complete before destroying master
        if hasattr(self, 'accept_thread') and self.accept_thread.is_alive():
            try:
                self.accept_thread.join(timeout=1.0)
            except Exception as e:
                #print(f"Error joining accept thread: {e}")
                pass  # Add pass statement for empty except block
        
        # Final network cleanup
        self._cleanup_network()
        
        self.master.destroy()

    def _accept_new_connection(self, server_sock):
        """Accept a new client connection in the main thread."""
        try:
            conn, addr = server_sock.accept()
            #print(f"[Thread: {threading.current_thread().name}] Accepted new connection from {addr}")
            conn.setblocking(False)

            # Use lock for safety, though less critical now accept is in main thread
            with self._lock:
                if len(self.client_connections) >= MAX_CLIENTS:
                    #print(f"Max clients ({MAX_CLIENTS}) reached. Rejecting {addr}.")
                    conn.close()
                    return # Use return instead of False

                # Assign colony ID and basic state
                colony_id = self.next_colony_id
                self.next_colony_id += 1
                color = get_colony_color(colony_id)

                # Determine starting position
                angle = (2 * math.pi * colony_id) / max(1, MAX_CLIENTS)
                dist_from_center = min(WORLD_WIDTH, WORLD_HEIGHT) * 0.4
                cx = WORLD_WIDTH / 2 + dist_from_center * math.cos(angle + math.pi/4)
                cy = WORLD_HEIGHT / 2 + dist_from_center * math.sin(angle + math.pi/4)
                cx = max(COLONY_RADIUS, min(WORLD_WIDTH - COLONY_RADIUS, cx))
                cy = max(COLONY_RADIUS, min(WORLD_HEIGHT - COLONY_RADIUS, cy))

                colony_data = {
                    'id': colony_id, 'x': cx, 'y': cy, 'color': color, 'score': 0, 'trails': []
                }
                self.colonies[colony_id] = colony_data

                client_info = {'colony_id': colony_id, 'status': 'connecting', 'addr': addr, 'buffer': b''}
                self.client_connections[conn] = client_info

                # Register the NEW client socket for reading
                self.selector.register(conn, selectors.EVENT_READ, data=client_info)
                #print(f"Registered client socket {addr} with selector for colony {colony_id}")

            # Send WELCOME message (outside lock to avoid blocking GUI if send takes time)
            try:
                # Temporarily blocking for reliable welcome message send
                conn.setblocking(True)
                # CRITICAL FIX: Only send non-depleted food sources in initial welcome
                valid_food_sources = [f for f in self.food_sources.values() if not f.get('depleted', False)]
                initial_state = {
                    KEY_COLONY_ID: colony_id,
                    KEY_COLOR: color,
                    # Send ALL colonies and food sources currently known
                    KEY_COLONIES: [c for c in self.colonies.values()],
                    KEY_FOOD_SOURCES: valid_food_sources,
                    'world_width': WORLD_WIDTH,
                    'world_height': WORLD_HEIGHT,
                    'server_time': time.time(),
                    'simulation_active': self.simulation_active # Let client know if sim is running
                }
                #print(f"Sending WELCOME with {len(valid_food_sources)} valid food sources (depleted ones excluded)")
                welcome_msg = encode_message(MSG_S_WELCOME, initial_state)
                conn.sendall(welcome_msg)
                conn.setblocking(False) # Back to non-blocking

                # Update status only after successful send
                with self._lock:
                    self.client_connections[conn]['status'] = 'connected'
                #print(f"Sent WELCOME to colony {colony_id} at {addr}")

                # Schedule UI updates
                self.master.after_idle(self._draw_simulation)
                self.master.after_idle(self._update_scores)
                self.master.after_idle(self._update_client_nodes)

            except Exception as e:
                #print(f"Error sending WELCOME or registering client {addr}: {e}")
                self._close_client_connection(conn) # Cleanup if welcome fails

        except BlockingIOError:
            # No connection waiting, which is normal for non-blocking sockets
            pass
        except Exception as e:
            #print(f"Error accepting new connection: {e}")
            # Potentially close the problematic socket if 'conn' was assigned
            if 'conn' in locals() and conn:
                 try: conn.close()
                 except Exception: pass

    def _close_client_connection(self, sock):
        """Safely unregister and close a client socket."""
        client_info = self.client_connections.pop(sock, None)
        if client_info:
            colony_id = client_info['colony_id']
            addr = client_info['addr']
            #print(f"Closing connection to colony {colony_id} ({addr})")

            # Remove ants associated with this colony
            ants_to_remove = [ant_id for ant_id, ant in self.ants.items() if ant['colony_id'] == colony_id]
            for ant_id in ants_to_remove:
                self.ants.pop(ant_id, None)

            # Remove colony state
            self.colonies.pop(colony_id, None)

            # Remove UI elements
            if colony_id in self.score_labels:
                self.score_labels.pop(colony_id).destroy()
            if colony_id in self.client_node_ids:
                 rect_id, text_id = self.client_node_ids.pop(colony_id)
                 self.client_node_canvas.delete(rect_id)
                 self.client_node_canvas.delete(text_id)

            try:
                self.selector.unregister(sock)
            except (KeyError, ValueError, OSError): pass # Ignore if already unregistered
            try:
                sock.close()
            except OSError: pass # Ignore if already closed

            self._update_client_nodes() # Refresh UI
            # Notify other clients that this colony is gone? Maybe via next WORLD_UPDATE.
        else:
            # Socket might not be in our dict if closed abruptly
            try:
                 self.selector.unregister(sock)
            except (KeyError, ValueError, OSError): pass
            try:
                sock.close()
            except OSError: pass


# --- Main Execution ---
if __name__ == '__main__':
    root = tk.Tk()
    app = SimulationServer(root)
    root.mainloop()