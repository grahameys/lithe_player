"""
Display where Lithe Player stores its configuration settings.
"""
import json
from pathlib import Path

config_path = Path(__file__).parent / "lithe_player_config.json"

print("=" * 70)
print("Lithe Player Configuration Settings Location")
print("=" * 70)
print()
print("Settings are stored in a JSON file for cross-platform compatibility.")
print()
print("Location:")
print(f"  {config_path}")
print()
print("=" * 70)
print()

if config_path.exists():
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        
        print("Current stored settings:")
        print("-" * 70)
        
        for key in sorted(settings.keys()):
            value = settings[key]
            # Truncate long values
            value_str = str(value)
            if len(value_str) > 60:
                value_str = value_str[:57] + "..."
            print(f"  {key:30} = {value_str}")
    except Exception as e:
        print(f"Error reading settings: {e}")
else:
    print("No settings file found yet.")
    print("Settings will be created when you run the player and change preferences.")

print()
print("=" * 70)
print()
print("To clear all settings, simply delete:")
print(f"  {config_path}")
print()
