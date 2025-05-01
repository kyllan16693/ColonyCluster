"""
Cached food validation module for the ant colony simulation.
This module provides a cached version of the food validation 
function to reduce redundant checks of depleted food sources.
"""

import time
import random
import math
import traceback

# Import required constants from shared_utils
from shared_utils import KEY_X, KEY_Y, KEY_FOOD_AMOUNT

# Cache for food source validation results
_food_validation_cache = {}  # {food_id: (result, timestamp)}
_validation_cache_lifetime = 0.5  # seconds for valid sources - REDUCED from 2.0 for faster invalidation
_depleted_cache_lifetime = 2.0  # seconds for invalid/depleted sources - REDUCED from 10.0

def cached_is_valid_food_source(current_food_sources, food_id):
    """
    Cached wrapper to check if a food source exists, is not marked depleted, and has amount > 0.
    
    This function caches results to avoid redundant expensive validation checks,
    especially for depleted food sources that will remain invalid.
    
    Args:
        current_food_sources: Dictionary of known food sources
        food_id: The ID of the food source to validate
        
    Returns:
        bool: True if the food source is valid, False otherwise
    """
    global _food_validation_cache, _validation_cache_lifetime, _depleted_cache_lifetime
    
    try:
        # Check if we have a recent cached result
        current_time = time.time()
        
        # Check cache for this food_id
        if food_id in _food_validation_cache:
            is_valid, timestamp = _food_validation_cache[food_id]
            
            # Use longer cache lifetime for invalid results
            cache_lifetime = _depleted_cache_lifetime if not is_valid else _validation_cache_lifetime
            
            if current_time - timestamp < cache_lifetime:
                # Only log occasionally to reduce spam
                if random.random() < 0.01:  # 1% chance - REDUCED from 5%
                    pass  # Add pass statement for empty if block
                return is_valid
        
        # Validate food_id value
        if food_id is None: 
            #print(f"  ❌ _is_valid: Invalid food_id is None")
            _food_validation_cache[food_id] = (False, current_time)
            return False # Cannot be valid
        
        # Convert to integer if string
        try:
            food_id_int = int(food_id) if isinstance(food_id, str) else food_id
        except ValueError:
            #print(f"  ❌ _is_valid: Invalid food_id format '{food_id}'")
            _food_validation_cache[food_id] = (False, current_time)
            return False

        # CRITICAL: Check if food source exists
        if food_id_int not in current_food_sources:
            # More verbose logging for debugging
            #print(f"  ❌ _is_valid: ID {food_id_int} not found in current_food_sources dictionary")
            if current_food_sources:
                #print(f"       Available food sources: {list(current_food_sources.keys())}")
                pass  # Add pass statement for empty if block
            _food_validation_cache[food_id] = (False, current_time)
            return False

        # Get food data and check essential fields
        food_data = current_food_sources[food_id_int]
        
        # FLEXIBLE CHECK FOR COORDINATES (support both 'x'/'y' and KEY_X/KEY_Y)
        # These are common keys used by different implementations
        x_keys = ['x', 'X', 'pos_x', KEY_X]
        y_keys = ['y', 'Y', 'pos_y', KEY_Y]
        
        # Check if at least one of the x keys is present
        has_x = any(key in food_data for key in x_keys)
        has_y = any(key in food_data for key in y_keys)
        has_amount = 'amount' in food_data or KEY_FOOD_AMOUNT in food_data
        
        if not (has_x and has_y and has_amount):
            #print(f"  ❌ _is_valid: ID {food_id_int} missing required fields. Has x: {has_x}, Has y: {has_y}, Has amount: {has_amount}")
            #print(f"       Available fields: {list(food_data.keys())}")
            _food_validation_cache[food_id] = (False, current_time)
            return False

        # Check 'depleted' flag *first* - most important check
        is_depleted = food_data.get('depleted', False)
        if is_depleted:
            #print(f"  ❌ _is_valid: ID {food_id_int} is explicitly marked depleted=True")
            _food_validation_cache[food_id] = (False, current_time)
            return False

        # Check 'amount' *second* - also critical
        food_amount = food_data.get('amount', 0)
        if food_amount <= 0:
            #print(f"  ❌ _is_valid: ID {food_id_int} has amount {food_amount} <= 0")
            # Note: We don't call _handle_food_depletion here because this function 
            # is meant to be stateless and doesn't have access to the colony client instance
            _food_validation_cache[food_id] = (False, current_time)
            return False

        # If we reach here, food source is valid
        # Determine coordinates based on available keys
        x_val = next((food_data.get(key) for key in x_keys if key in food_data), None)
        y_val = next((food_data.get(key) for key in y_keys if key in food_data), None)
        food_coords = (x_val, y_val)
        
        # Only log occasionally to reduce spam
        if random.random() < 0.05:  # 5% chance to log
            #print(f"  ✅ _is_valid: ID {food_id_int} is valid (amount={food_amount}, pos={food_coords})")
            pass  # Add pass statement for empty if block
        
        _food_validation_cache[food_id] = (True, current_time)
        return True

    except Exception as e:
        #print(f"  ❌ ERROR in cached_is_valid_food_source for {food_id}: {e}")
        traceback.print_exc()
        return False

# Clear the cache function
def clear_validation_cache():
    """Clear the food validation cache completely."""
    global _food_validation_cache
    _food_validation_cache.clear()
    #print("Food validation cache cleared.") 