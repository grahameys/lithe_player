"""
Lithe Player - A modern audio player with FFT equalizer visualization.

This application provides:
- Audio playback using VLC backend
- Visual FFT-based equalizer
- Playlist management
- File browser with album art display
- Customizable accent colors
- Global media key support (Windows)

Author: grahameys
"""

import sys
import os
import numpy as np
import threading
import time
import ctypes
from collections import deque
import threading
from enum import Enum

from PySide6.QtCore import (
    Qt, QDir, QAbstractTableModel, QModelIndex, QSettings, QSize, QTimer, 
    QRect, QEvent, QAbstractNativeEventFilter, QObject, Signal, QThread
)
from PySide6.QtGui import (
    QAction, QFont, QColor, QIcon, QPalette, QPixmap, QPainter, QKeySequence, QShortcut
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QTreeView, QTableView,
    QVBoxLayout, QHBoxLayout, QPushButton, QFileSystemModel, QHeaderView,
    QLabel, QSlider, QFileDialog, QMessageBox, QColorDialog,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QSizePolicy,
    QAbstractItemView, QSplashScreen
)
# Import QtSvg to enable SVG icon support
from PySide6 import QtSvg

import vlc
import soundfile as sf
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_asset_path(filename):
    """Get absolute path to asset file, works for dev and PyInstaller."""
    if getattr(sys, 'frozen', False):
        # Running in PyInstaller bundle
        base_path = sys._MEIPASS
    else:
        # Running in normal Python environment
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, 'assets', filename)

def get_themed_icon(filename):
    """Get theme-aware icon by modifying SVG colors based on system theme."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon, QPixmap, QPalette, QColor, QImage
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QByteArray, Qt
    
    svg_path = get_asset_path(filename)
    
    # Read SVG file
    try:
        with open(svg_path, 'r', encoding='utf-8') as f:
            svg_content = f.read()
    except:
        # If we can't read it, return regular icon
        return QIcon(svg_path)
    
    # Check if we need light or dark icon
    app = QApplication.instance()
    use_light_icon = False
    if app:
        palette = app.palette()
        base_color = palette.color(QPalette.Base)
        use_light_icon = is_dark_color(base_color)
    
    # Replace dark colors with light colors for dark theme
    if use_light_icon:
        # Replace common dark colors with white/light colors
        svg_content = svg_content.replace('stroke="#1C274C"', 'stroke="#FFFFFF"')
        svg_content = svg_content.replace('fill="#1C274C"', 'fill="#FFFFFF"')
        svg_content = svg_content.replace('stroke="#000000"', 'stroke="#FFFFFF"')
        svg_content = svg_content.replace('fill="#000000"', 'fill="#FFFFFF"')
        svg_content = svg_content.replace('stroke="#000"', 'stroke="#FFF"')
        svg_content = svg_content.replace('fill="#000"', 'fill="#FFF"')
    
    # Create icon from modified SVG with proper alpha channel support
    renderer = QSvgRenderer(QByteArray(svg_content.encode('utf-8')))
    
    # Use QImage with alpha channel for proper transparency
    image = QImage(48, 48, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    
    from PySide6.QtGui import QPainter
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    
    pixmap = QPixmap.fromImage(image)
    return QIcon(pixmap)

# ============================================================================
# JSON SETTINGS MANAGER
# ============================================================================

import json
from pathlib import Path
import base64

class JsonSettings:
    """Cross-platform JSON-based settings manager compatible with QSettings API."""
    
    def __init__(self, config_name="config.json"):
        """Initialize settings with JSON file in the same directory as the script."""
        self.config_path = Path(__file__).parent / config_name
        self._settings = {}
        self._load()
    
    def _load(self):
        """Load settings from JSON file."""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._settings = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not load settings from {self.config_path}: {e}")
                self._settings = {}
    
    def _save(self):
        """Save settings to JSON file."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Warning: Could not save settings to {self.config_path}: {e}")
    
    def value(self, key, default=None):
        """Get a setting value (QSettings-compatible API)."""
        value = self._settings.get(key, default)
        
        # If it's a base64-encoded QByteArray, decode it
        if isinstance(value, str) and value.startswith("base64:"):
            from PySide6.QtCore import QByteArray
            try:
                decoded = base64.b64decode(value[7:])
                return QByteArray(decoded)
            except Exception:
                return default
        
        return value
    
    def setValue(self, key, value):
        """Set a setting value and save immediately (QSettings-compatible API)."""
        # Convert QByteArray to base64 string for JSON serialization
        if hasattr(value, 'toBase64'):
            # It's a QByteArray
            value = "base64:" + value.toBase64().data().decode('utf-8')
        
        self._settings[key] = value
        self._save()
    
    def allKeys(self):
        """Return all setting keys (QSettings-compatible API)."""
        return list(self._settings.keys())
    
    def fileName(self):
        """Return the config file path (QSettings-compatible API)."""
        return str(self.config_path)
    
    def contains(self, key):
        """Check if a key exists (QSettings-compatible API)."""
        return key in self._settings

# ============================================================================
# CONFIGURATION CONSTANTS
# ============================================================================

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg'}
DEFAULT_ANALYSIS_RATE = 44100
ANALYSIS_CHUNK_SAMPLES = 2048
TRACK_END_THRESHOLD_MS = 500  # Detect end within 500ms
TRACK_ADVANCE_DELAY_MS = 1000  # Reset flag after 1 second
PROGRESS_UPDATE_INTERVAL_MS = 500
EQUALIZER_UPDATE_INTERVAL_MS = 30
SPLASH_SCREEN_DURATION_MS = 3000

# ============================================================================
# VLC ENVIRONMENT SETUP
# ============================================================================

def check_system_vlc():
    """Check if VLC is installed system-wide and accessible."""
    try:
        # Try to create a VLC instance without specifying plugins
        test_instance = vlc.Instance()
        # If we can create an instance, VLC is available system-wide
        print("✓ System-wide VLC installation detected")
        return True
    except Exception as e:
        print(f"System-wide VLC not found: {e}")
        return False

def setup_vlc_environment():
    """
    Configure VLC environment.
    Prefers system-wide VLC installation, falls back to local plugins if needed.
    """
    # First, try to use system-wide VLC
    if check_system_vlc():
        print("Using system-wide VLC installation")
        return None  # Return None to indicate system VLC should be used
    
    # If system VLC not found, fall back to local plugins
    print("System-wide VLC not available, checking for local plugins...")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    plugins_dir = os.path.join(script_dir, "plugins")
    
    if not os.path.exists(plugins_dir):
        print(f"⚠ Warning: No system VLC found and local plugins directory not found at {plugins_dir}")
        print("VLC initialization may fail. Please install VLC or provide local plugins.")
        return None
    
    # Configure environment for local plugins
    os.environ['VLC_PLUGIN_PATH'] = plugins_dir
    print(f"VLC plugin path set to: {plugins_dir}")
    
    if sys.platform == 'win32':
        os.environ['PATH'] = plugins_dir + os.pathsep + os.environ.get('PATH', '')
        parent_dir = os.path.dirname(plugins_dir)
        if os.path.exists(os.path.join(parent_dir, 'libvlc.dll')):
            os.environ['PATH'] = parent_dir + os.pathsep + os.environ['PATH']
            print(f"Added to PATH: {parent_dir}")
    
    print(f"Using local VLC plugins from: {plugins_dir}")
    return plugins_dir

# ============================================================================
# SIGNAL EMITTER FOR THREAD-SAFE GAPLESS PLAYBACK
# ============================================================================

class GaplessSignals(QObject):
    """Qt signals for thread-safe communication from gapless manager."""
    track_changed = Signal(str)  # Emits filepath of new track
    start_equalizer = Signal(str)  # Start equalizer for filepath
    stop_equalizer = Signal()  # Stop equalizer

# ============================================================================
# GAPLESS PLAYBACK MANAGER
# ============================================================================

class PlayerState(Enum):
    """Player slot states for gapless playback."""
    IDLE = 0
    LOADING = 1
    READY = 2
    PLAYING = 3
    FINISHING = 4

class GaplessSignals(QObject):
    """Qt signals for thread-safe communication from gapless manager."""
    track_changed = Signal(str)  # Emits filepath of new track
    start_equalizer = Signal(str)  # Start equalizer for filepath
    stop_equalizer = Signal()  # Stop equalizer

class GaplessPlaybackManager:
    """
    Manages dual VLC players for true gapless multiformat playback.
    Uses two players that alternate - while one plays, the other preloads.
    """
    
    def __init__(self, vlc_instance, eq_widget=None):
        self.instance = vlc_instance
        self.eq_widget = eq_widget
        
        # Qt signals for thread-safe communication
        self.signals = GaplessSignals()
        
        # Create two players for alternating playback
        self.player_a = self.instance.media_player_new()
        self.player_b = self.instance.media_player_new()
        
        # Track which player is active
        self.active_player = None
        self.standby_player = None
        
        # State tracking
        self.player_a_state = PlayerState.IDLE
        self.player_b_state = PlayerState.IDLE
        
        # Volume tracking
        self._current_volume = 70  # Default volume
        
        # Preload management
        self.preload_lock = threading.Lock()
        self.next_track_path = None
        self.current_track_path = None
        
        # Gapless transition settings - trigger early enough to never miss
        self.transition_threshold_ms = 500  # Start transition 500ms before end
        self.monitoring = False
        self.monitor_thread = None
        self._stop_monitoring = threading.Event()
        
        # Transition flag to prevent double-triggers
        self._transition_triggered = False
        
    def setup_events(self):
        """Setup event handlers for both players."""
        # Player A events
        em_a = self.player_a.event_manager()
        em_a.event_attach(vlc.EventType.MediaPlayerEndReached, 
                         lambda e: self._on_player_end(self.player_a, 'A'))
        
        # Player B events
        em_b = self.player_b.event_manager()
        em_b.event_attach(vlc.EventType.MediaPlayerEndReached,
                         lambda e: self._on_player_end(self.player_b, 'B'))
    
    def play_track(self, filepath, preload_next=None):
        """
        Play a track with optional preloading of next track.
        
        Args:
            filepath: Path to audio file to play
            preload_next: Optional path to next track for gapless transition
        """
        # Reset transition flag for new track
        self._transition_triggered = False
        
        # Determine which player to use
        if self.active_player is None:
            # First track - use player A
            self.active_player = self.player_a
            self.standby_player = self.player_b
            self.player_a_state = PlayerState.LOADING
            
            # Load media
            media = self.instance.media_new(filepath)
            self.active_player.set_media(media)
            self.active_player.audio_set_volume(self._current_volume)
            
        elif self.standby_player and self._is_preloaded(filepath):
            # Next track is preloaded - swap players for gapless
            print(f"Using preloaded track: {os.path.basename(filepath)}")
            self.active_player, self.standby_player = self.standby_player, self.active_player
            self._update_states_after_swap()
        else:
            # Fallback - use standby player without preload
            print(f"Loading track without preload: {os.path.basename(filepath)}")
            if self.active_player == self.player_a:
                self.active_player = self.player_b
                self.standby_player = self.player_a
                self.player_b_state = PlayerState.LOADING
            else:
                self.active_player = self.player_a
                self.standby_player = self.player_b
                self.player_a_state = PlayerState.LOADING
            
            # Load and play
            media = self.instance.media_new(filepath)
            self.active_player.set_media(media)
            self.active_player.audio_set_volume(self._current_volume)
        
        # Start playback
        self.current_track_path = filepath
        self.active_player.play()
        
        print(f"Playing: {os.path.basename(filepath)} (Volume: {self.active_player.audio_get_volume()})")
        print(f"  Active player: {'A' if self.active_player == self.player_a else 'B'}")
        print(f"  Standby player: {'A' if self.standby_player == self.player_a else 'B'}")
        print(f"  Transition flag reset: {self._transition_triggered}")
        
        # Start monitoring for gapless transition
        self._start_monitoring()
        
        # Preload next track if provided (non-blocking)
        if preload_next:
            threading.Thread(target=self._preload_next_track, 
                           args=(preload_next,), daemon=True).start()
        
        # Start equalizer for current track - emit signal instead of direct call
        self.signals.start_equalizer.emit(filepath)
    
    def _is_preloaded(self, filepath):
        """Check if the given filepath is already preloaded."""
        return self.next_track_path == filepath and self.standby_player is not None
    
    def _update_states_after_swap(self):
        """Update player states after swapping active/standby."""
        if self.active_player == self.player_a:
            self.player_a_state = PlayerState.PLAYING
            self.player_b_state = PlayerState.IDLE
        else:
            self.player_b_state = PlayerState.PLAYING
            self.player_a_state = PlayerState.IDLE
    
    def _preload_next_track(self, filepath):
        """Preload the next track into standby player (runs in background thread)."""
        if not self.standby_player:
            print("❌ No standby player available for preloading")
            print(f"   Active: {self.active_player}, Standby: {self.standby_player}")
            return
        
        # Check if we're trying to preload the currently playing track
        if self.current_track_path == filepath:
            print(f"⚠️  Skipping preload - track is currently playing: {os.path.basename(filepath)}")
            return
        
        # If already preloaded and it's the same track, skip
        if self.next_track_path == filepath:
            print(f"✓ Track already preloaded: {os.path.basename(filepath)}")
            return
        
        try:
            print(f"⏳ Preloading into player {'A' if self.standby_player == self.player_a else 'B'}: {os.path.basename(filepath)}")
            
            # Create media
            media = self.instance.media_new(filepath)
            
            # Set media on standby player
            with self.preload_lock:
                # Clear old preload if it exists
                if self.next_track_path:
                    print(f"   Replacing old preload: {os.path.basename(self.next_track_path)}")
                
                self.standby_player.set_media(media)
                
                # Ensure volume is set
                self.standby_player.audio_set_volume(self._current_volume)
                
                # Update state
                if self.standby_player == self.player_a:
                    self.player_a_state = PlayerState.READY
                else:
                    self.player_b_state = PlayerState.READY
                
                self.next_track_path = filepath
                print(f"✓ Preloaded next track: {os.path.basename(filepath)}")
                print(f"  Standby player: {'A' if self.standby_player == self.player_a else 'B'}")
                print(f"  Standby player volume: {self._current_volume}")
        except Exception as e:
            print(f"❌ Error preloading track: {e}")
            import traceback
            traceback.print_exc()
    
    def _start_monitoring(self):
        """Start monitoring playback position for gapless transitions."""
        if not self.monitoring:
            self.monitoring = True
            self._stop_monitoring.clear()
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
    
    def _monitor_loop(self):
        """Monitor playback position and trigger gapless transition."""
        last_log_time = 0
        
        while not self._stop_monitoring.is_set():
            try:
                if self.active_player and self.active_player.is_playing():
                    length = self.active_player.get_length()
                    current = self.active_player.get_time()
                    
                    if length > 0 and current > 0:
                        remaining = length - current
                        
                        # Log every 5 seconds to track progress
                        if current - last_log_time > 5000:
                            print(f"Playback: {current}ms / {length}ms (remaining: {remaining}ms)")
                            last_log_time = current
                        
                        # Trigger transition near end (only once)
                        if (remaining <= self.transition_threshold_ms and 
                            remaining > 0 and 
                            not self._transition_triggered):
                            
                            if self.standby_player and self.next_track_path:
                                self._transition_triggered = True
                                print(f"▶ Triggering gapless transition with {remaining}ms remaining")
                                # Run transition immediately in this thread for precise timing
                                self._trigger_gapless_transition()
                            else:
                                print(f"Cannot trigger transition: standby={self.standby_player is not None}, next_track={self.next_track_path is not None}")
            except Exception as e:
                print(f"Monitor loop error: {e}")
            
            time.sleep(0.02)  # Check every 20ms for precise timing
    
    def _trigger_gapless_transition(self):
        """Trigger gapless transition to preloaded track."""
        try:
            with self.preload_lock:
                if not self.next_track_path or not self.standby_player:
                    print("Transition aborted: no next track or standby player")
                    self._transition_triggered = False
                    return
                
                print(f"🎵 Starting gapless transition to: {os.path.basename(self.next_track_path)}")
                
                # Get players
                old_player = self.active_player
                new_player = self.standby_player
                
                # Set volume and start new player immediately - no crossfade needed for true gapless
                new_player.audio_set_volume(self._current_volume)
                new_player.play()
                
                # Minimal wait for player to start
                time.sleep(0.01)
                
                # Verify new player is playing
                is_playing = new_player.is_playing()
                
                print(f"New player status: playing={is_playing}")
                
                if not is_playing:
                    print("⚠ WARNING: New player not playing, retrying...")
                    new_player.play()
                    time.sleep(0.02)
                    if not new_player.is_playing():
                        print("❌ ERROR: Failed to start new player!")
                        self._transition_triggered = False
                        return
                
                # Complete the swap
                self.active_player = new_player
                self.standby_player = old_player
                
                # Update tracking
                old_track = self.current_track_path
                self.current_track_path = self.next_track_path
                self.next_track_path = None
                self._update_states_after_swap()
                
                # Emit signals FIRST (before stopping old player)
                self.signals.track_changed.emit(self.current_track_path)
                self.signals.start_equalizer.emit(self.current_track_path)
                
                # Stop old player after new one is confirmed playing
                old_player.audio_set_volume(self._current_volume)  # Restore for next use
                old_player.stop()
                
                print(f"✓ Gapless transition complete: {os.path.basename(old_track)} -> {os.path.basename(self.current_track_path)}")
                print(f"  Active player: {'A' if self.active_player == self.player_a else 'B'}")
                print(f"  Standby player: {'A' if self.standby_player == self.player_a else 'B'}")
                    
        except Exception as e:
            print(f"Gapless transition error: {e}")
            import traceback
            traceback.print_exc()
            self._transition_triggered = False
    
    def _on_player_end(self, player, name):
        """Handle player end reached event."""
        print(f"\n!!! Player {name} reached end (transition_triggered={self._transition_triggered})")
        print(f"    Active player: {'A' if self.active_player == self.player_a else 'B'}")
        print(f"    Standby player: {'A' if self.standby_player == self.player_a else 'B' if self.standby_player else 'None'}")
        print(f"    next_track_path: {os.path.basename(self.next_track_path) if self.next_track_path else 'None'}")
        print(f"    current_track_path: {os.path.basename(self.current_track_path) if self.current_track_path else 'None'}")
        
        # Only use fallback if we haven't already transitioned
        if self._transition_triggered:
            print("Transition already completed, ignoring end event")
            return
        
        # Check if there's actually a next track to play
        if not self.next_track_path:
            print("No next track - playback complete")
            self._transition_triggered = True
            # Stop monitoring
            self._stop_monitoring.set()
            self.monitoring = False
            # Emit signal that playback ended
            self.signals.stop_equalizer.emit()
            return
        
        # Mark that we're handling the transition
        self._transition_triggered = True
        
        # If this fires, gapless transition didn't work - we need to manually advance
        print("WARNING: Gapless transition didn't fire, using fallback")
        
        # Don't just emit signal - actually play the next track if it's preloaded
        if self.next_track_path and self.standby_player:
            print(f"Fallback: Starting preloaded track {os.path.basename(self.next_track_path)}")
            
            # Swap to standby player and start it
            with self.preload_lock:
                old_player = self.active_player
                new_player = self.standby_player
                
                # Save the track path before clearing
                track_to_play = self.next_track_path
                
                # Ensure correct volume
                new_player.audio_set_volume(self._current_volume)
                
                # Start new player
                new_player.play()
                
                # Give it a moment to start
                time.sleep(0.05)
                
                # Verify it's playing
                if new_player.is_playing():
                    print("Fallback: New player started successfully")
                    
                    # Complete the swap
                    self.active_player = new_player
                    self.standby_player = old_player
                    
                    # Stop old player
                    old_player.stop()
                    
                    # Update tracking
                    self.current_track_path = track_to_play
                    # DON'T clear next_track_path yet - let the preload replace it
                    # self.next_track_path = None  # ← REMOVED THIS
                    self._update_states_after_swap()
                    
                    print(f"Fallback complete:")
                    print(f"  Active player: {'A' if self.active_player == self.player_a else 'B'}")
                    print(f"  Standby player: {'A' if self.standby_player == self.player_a else 'B'}")
                    
                    # Emit signals for UI update
                    # This will call _on_gapless_track_change which resets the flag AND preloads next
                    self.signals.track_changed.emit(self.current_track_path)
                    self.signals.start_equalizer.emit(self.current_track_path)
                else:
                    print("ERROR: Fallback player failed to start")
                    self._transition_triggered = False  # Reset on failure
        else:
            print(f"❌ No preloaded track available for fallback")
            print(f"    next_track_path: {self.next_track_path}")
            print(f"    standby_player: {self.standby_player}")
            self._transition_triggered = False  # Reset if no track to play
            
    def pause(self):
        """Pause active player."""
        if self.active_player and self.active_player.is_playing():
            self.active_player.pause()
            print("Playback paused")
            # Emit signal to stop equalizer timer
            self.signals.stop_equalizer.emit()
        else:
            print("Cannot pause: not playing")
    
    def resume(self):
        """Resume active player from paused state."""
        if self.active_player and self.current_track_path:
            # Check if paused (not playing but has media)
            if not self.active_player.is_playing():
                self.active_player.play()
                
                # Restart monitoring
                self._start_monitoring()
                
                # Emit signal to start equalizer
                self.signals.start_equalizer.emit(self.current_track_path)
                
                print(f"Resumed playback: {os.path.basename(self.current_track_path)}")
            else:
                print("Already playing")
        else:
            print("Cannot resume: no active player or track")
    
    def stop(self):
        """Stop all playback completely."""
        # Stop monitoring
        self.monitoring = False
        self._stop_monitoring.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=0.2)
        
        # Emit signal to stop equalizer
        self.signals.stop_equalizer.emit()
        
        # Stop both players completely
        try:
            if self.active_player:
                self.active_player.stop()
            if self.standby_player:
                self.standby_player.stop()
        except Exception as e:
            print(f"Error stopping players: {e}")
        
        # Reset everything for a fresh start
        self.active_player = None
        self.standby_player = None
        self.player_a_state = PlayerState.IDLE
        self.player_b_state = PlayerState.IDLE
        self.current_track_path = None
        self.next_track_path = None
        self._transition_triggered = False
        
        print("Playback stopped completely")
    
    def is_playing(self):
        """Check if currently playing."""
        return self.active_player and self.active_player.is_playing()
    
    def get_active_player(self):
        """Get the currently active player."""
        return self.active_player
    
    def set_volume(self, volume):
        """Set volume for both players."""
        self._current_volume = volume
        self.player_a.audio_set_volume(volume)
        self.player_b.audio_set_volume(volume)
    
    def get_time(self):
        """Get current playback time."""
        if self.active_player:
            return self.active_player.get_time()
        return 0
    
    def get_length(self):
        """Get total track length."""
        if self.active_player:
            return self.active_player.get_length()
        return 0
    
    def set_time(self, time_ms):
        """Seek to specific time."""
        if self.active_player:
            self.active_player.set_time(time_ms)
            
# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def is_dark_color(color: QColor) -> bool:
    """Determine if a color is dark based on perceived brightness."""
    brightness = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return brightness < 128

def extract_album_art(filepath):
    """Extract album art from audio file metadata."""
    ext = filepath.lower()
    pixmap = None
    
    try:
        if ext.endswith(".mp3"):
            audio = MP3(filepath)
            if isinstance(audio.tags, ID3):
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        pixmap = QPixmap()
                        pixmap.loadFromData(tag.data)
                        break
        elif ext.endswith(".flac"):
            audio = FLAC(filepath)
            if audio.pictures:
                pixmap = QPixmap()
                pixmap.loadFromData(audio.pictures[0].data)
        elif ext.endswith((".m4a", ".mp4", ".aac")):
            audio = MP4(filepath)
            if audio.tags and (covr := audio.tags.get("covr")):
                pixmap = QPixmap()
                pixmap.loadFromData(covr[0])
    except Exception as e:
        print(f"Album art extraction error: {e}")
    
    return pixmap

def extract_metadata(path, trackno):
    """Extract metadata from audio file."""
    title = os.path.splitext(os.path.basename(path))[0]
    artist = album = year = ""
    
    try:
        audio = MutagenFile(path, easy=True)
        if audio:
            title = audio.get("title", [title])[0]
            artist = audio.get("artist", [""])[0]
            album = audio.get("album", [""])[0]
            year = audio.get("date", audio.get("year", [""]))[0]
    except Exception:
        pass
    
    return {
        "trackno": trackno,
        "title": title,
        "artist": artist,
        "album": album,
        "year": year,
        "path": path
    }

# ============================================================================
# EQUALIZER WIDGET
# ============================================================================

class EqualizerWidget(QWidget):
    """FFT-driven equalizer with background audio decoder thread."""

    def __init__(self, bar_count=40, segments=15, parent=None):
        super().__init__(parent)
        self.bar_count = bar_count
        self.segments = segments
        self.levels = [0] * bar_count
        self.target_levels = [0] * bar_count  # Target levels for smooth interpolation
        self.peak_levels = [0] * bar_count    # Peak tracking for smoother decay
        self.peak_hold = [0] * bar_count      # Peak hold positions for visual indicator
        self.peak_hold_time = [0] * bar_count # Time counter for peak hold
        self.velocity = [0] * bar_count       # Velocity for gravity effect
        self.color = QColor("#00cc66")
        self.custom_peak_color = None         # Custom peak color (None = auto)
        self.peak_alpha = 255                 # Peak transparency (0-255, 255 = opaque)
        self.buffer_size = ANALYSIS_CHUNK_SAMPLES
        self.sample_buffer = np.zeros(self.buffer_size, dtype=np.float32)
        self._band_ema_max = [1e-6] * bar_count
        self._decoder_thread = None
        self._decoder_running = False
        self._stop_decoder = threading.Event()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_from_fft)

    def set_peak_color(self, color: QColor):
        """Set a custom peak indicator color."""
        if color and color.isValid():
            self.custom_peak_color = color
            self.update()
    
    def reset_peak_color(self):
        """Reset to automatic peak color."""
        self.custom_peak_color = None
        self.update()
        
    def set_peak_alpha(self, alpha: int):
        """Set peak indicator transparency (0-255, 255 = opaque)."""
        self.peak_alpha = max(0, min(255, alpha))
        self.update()        

    def start(self, filepath):
        """Start the equalizer decoder and animation."""
        # Stop existing decoder cleanly if running
        if self._decoder_running:
            self._decoder_running = False
            self._stop_decoder.set()
            # Don't wait for thread - let it die naturally
            self._decoder_thread = None
        
        # Reset stop event
        self._stop_decoder.clear()
        
        # Start new decoder thread
        self._decoder_running = True
        self._decoder_thread = threading.Thread(
            target=self._decode_loop, args=(filepath,), daemon=True
        )
        self._decoder_thread.start()
        
        # Start timer if not active (safe because we're on main thread)
        if not self.timer.isActive():
            self.timer.start(EQUALIZER_UPDATE_INTERVAL_MS)

    def stop(self, clear_display=True):
        """Stop the equalizer decoder and animation."""
        self._decoder_running = False
        self._stop_decoder.set()
        # Don't join - just let the thread die
        self._decoder_thread = None
        
        # Only stop timer if we're on the main thread
        # This check prevents the cross-thread timer error
        if QThread.currentThread() == QApplication.instance().thread():
            self.timer.stop()
            if clear_display:
                self.levels = [0] * self.bar_count
                self.target_levels = [0] * self.bar_count
                self.peak_levels = [0] * self.bar_count
                self.peak_hold = [0] * self.bar_count
                self.peak_hold_time = [0] * self.bar_count
                self.velocity = [0] * self.bar_count
                self.update()
        else:
            # We're on a background thread - use signal to stop timer
            QTimer.singleShot(0, lambda: self._stop_on_main_thread(clear_display))
    
    def _stop_on_main_thread(self, clear_display):
        """Stop timer on main thread."""
        self.timer.stop()
        if clear_display:
            self.levels = [0] * self.bar_count
            self.target_levels = [0] * self.bar_count
            self.peak_levels = [0] * self.bar_count
            self.peak_hold = [0] * self.bar_count
            self.peak_hold_time = [0] * self.bar_count
            self.velocity = [0] * self.bar_count
            self.update()

    def _decode_loop(self, filepath):
        """Background thread for audio decoding."""
        try:
            with sf.SoundFile(filepath) as f:
                while self._decoder_running and not self._stop_decoder.is_set():
                    frames = f.read(ANALYSIS_CHUNK_SAMPLES, dtype="float32", always_2d=True)
                    if len(frames) == 0:
                        f.seek(0)
                        continue
                    
                    samples = frames[:, 0]
                    if len(samples) < self.buffer_size:
                        padded = np.zeros(self.buffer_size, dtype=np.float32)
                        padded[-len(samples):] = samples
                        samples = padded
                    
                    self.sample_buffer = samples
                    
                    # Check stop event more frequently
                    sleep_time = len(samples) / DEFAULT_ANALYSIS_RATE
                    elapsed = 0
                    while elapsed < sleep_time and not self._stop_decoder.is_set():
                        time.sleep(0.01)
                        elapsed += 0.01
                        
        except Exception as e:
            print(f"Decoder thread error: {e}")

    def update_from_fft(self):
        """Update equalizer bars from FFT analysis."""
        if not self._decoder_running:
            return
            
        fft = np.fft.rfft(self.sample_buffer * np.hanning(len(self.sample_buffer)))
        magnitude = np.abs(fft)

        freqs_hz = np.fft.rfftfreq(len(self.sample_buffer), 1.0 / DEFAULT_ANALYSIS_RATE)
        mask = (freqs_hz >= 60) & (freqs_hz <= 17000)
        magnitude = magnitude[mask]

        bars_raw = self._calculate_bar_values(magnitude)
        bars_norm = self._normalize_bars(bars_raw)
        
        # Set target levels with slightly increased height
        self.target_levels = [max(0, min(self.segments, v * 0.82)) for v in bars_norm]
        
        # Smooth interpolation with gravity effect
        self._smooth_levels_with_gravity()
        
        self.update()

    def _smooth_levels_with_gravity(self):
        """Apply smooth interpolation with gravity physics and peak hold."""
        gravity = 0.4  # Gravity acceleration
        peak_hold_frames = 12  # Hold peak for ~12 frames (360ms at 30fps) - reduced from 15
        
        for i in range(self.bar_count):
            target = self.target_levels[i]
            current = self.levels[i]
            
            # If target is higher than current, rise quickly
            if target > current:
                # Quick rise with 95% interpolation
                self.levels[i] = current + (target - current) * 0.95
                self.velocity[i] = 0  # Reset velocity on rise
                
                # Update peak hold if we've reached a new peak
                if self.levels[i] > self.peak_hold[i]:
                    self.peak_hold[i] = self.levels[i]
                    self.peak_hold_time[i] = peak_hold_frames
            else:
                # Apply gravity for natural fall
                self.velocity[i] += gravity
                self.levels[i] = max(target, current - self.velocity[i])
                
                # Ensure level doesn't go below target
                if self.levels[i] <= target:
                    self.levels[i] = target
                    self.velocity[i] = 0
            
            # Handle peak hold indicator
            if self.peak_hold_time[i] > 0:
                # Peak is held - decrease hold time
                self.peak_hold_time[i] -= 1
            else:
                # Peak hold expired - let it fall faster
                if self.peak_hold[i] > self.levels[i]:
                    # Peak falls faster than before
                    self.peak_hold[i] = max(self.levels[i], self.peak_hold[i] - 0.5)  # Increased from 0.3
                else:
                    self.peak_hold[i] = self.levels[i]

    def _calculate_bar_values(self, magnitude):
        """Calculate raw bar values from FFT magnitude."""
        bars_raw = [0.0] * self.bar_count
        if len(magnitude) == 0:
            return bars_raw
        
        chunk_size = len(magnitude) / self.bar_count
        for i in range(self.bar_count):
            start = int(i * chunk_size)
            end = int((i + 1) * chunk_size)
            band = magnitude[start:end]
            bars_raw[i] = float(np.mean(band)) if len(band) else 0.0
        
        return bars_raw

    def _normalize_bars(self, bars_raw):
        """Normalize bars using exponential moving average."""
        decay = 0.95  # Fast decay for very quick response
        eps = 1e-6
        bars_norm = []
        
        for i, val in enumerate(bars_raw):
            ema_candidate = self._band_ema_max[i] * decay
            self._band_ema_max[i] = max(val, ema_candidate)
            
            norm = val / (self._band_ema_max[i] + eps)
            
            # Moderate high frequency emphasis
            hf_tilt = 1.0 + 0.22 * (i / max(1, self.bar_count - 1))
            norm *= hf_tilt
            
            # Good bass boost
            if i < 2:
                norm *= 1.18
            
            # Increased scaling for 2-3 bars more height
            scaled = norm * (self.segments * 0.87)
            bars_norm.append(int(scaled))
        
        return bars_norm

    def update_color(self, color: QColor):
        """Update equalizer color."""
        if color:
            self.color = color
            self.update()

    def paintEvent(self, event):
        """Paint the equalizer bars with peak hold indicators."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        
        bar_width = self.width() / self.bar_count
        segment_height = self.height() / self.segments
        
        for i, level in enumerate(self.levels):
            # Draw main bars
            for seg in range(int(level)):
                fade_factor = 1.0 - (seg / self.segments)
                faded_color = QColor(self.color)
                faded_color.setAlpha(int(255 * fade_factor))
                
                rect = QRect(
                    int(i * bar_width),
                    int(self.height() - (seg + 1) * segment_height),
                    int(bar_width * 0.85),
                    int(segment_height * 0.8)
                )
                painter.fillRect(rect, faded_color)
            
            # Draw peak hold indicator with smart complementary color and custom alpha
            if self.peak_hold[i] > 0:
                peak_seg = int(self.peak_hold[i])
                if peak_seg < self.segments:
                    # Calculate intelligent peak color based on accent color
                    peak_color = self._get_peak_color()
                    # Apply custom alpha/transparency
                    peak_color.setAlpha(self.peak_alpha)
                    
                    peak_rect = QRect(
                        int(i * bar_width),
                        int(self.height() - (peak_seg + 1) * segment_height),
                        int(bar_width * 0.85),
                        int(segment_height * 0.4)
                    )
                    painter.fillRect(peak_rect, peak_color)
            
            # Draw peak hold indicator with smart complementary color
            if self.peak_hold[i] > 0:
                peak_seg = int(self.peak_hold[i])
                if peak_seg < self.segments:
                    # Calculate intelligent peak color based on accent color
                    peak_color = self._get_peak_color()
                    
                    peak_rect = QRect(
                        int(i * bar_width),
                        int(self.height() - (peak_seg + 1) * segment_height),
                        int(bar_width * 0.85),
                        int(segment_height * 0.4)
                    )
                    painter.fillRect(peak_rect, peak_color)
    
    def _get_peak_color(self):
        """
        Calculate an intelligent peak color that complements the accent color.
        Uses color theory to create visually appealing contrast.
        """
        # If custom color is set, use it
        if self.custom_peak_color:
            return self.custom_peak_color
        
        # Otherwise, calculate automatic complementary color
        h, s, v, a = self.color.getHsv()
        
        # Get luminance (brightness) of the base color
        r, g, b = self.color.red(), self.color.green(), self.color.blue()
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        
        # Strategy 1: If color is dark, make peak much brighter (light version)
        if luminance < 128:
            # Dark color - use a much lighter, more saturated version
            peak_color = QColor.fromHsv(
                h,  # Same hue
                max(180, s),  # High saturation
                min(255, v + 120)  # Much brighter
            )
        # Strategy 2: If color is light, make peak slightly darker but more saturated
        elif luminance > 180:
            # Light color - use a richer, more saturated version
            peak_color = QColor.fromHsv(
                h,  # Same hue
                min(255, s + 80),  # More saturated
                max(150, v - 30)  # Slightly darker for contrast
            )
        # Strategy 3: Medium colors - shift hue slightly and increase saturation
        else:
            # Medium brightness - use analogous color (shift hue slightly)
            new_hue = (h + 20) % 360  # Shift hue by 20 degrees
            peak_color = QColor.fromHsv(
                new_hue,
                min(255, s + 60),  # More saturated
                min(255, v + 60)  # Brighter
            )
        
        return peak_color

# ============================================================================
# PLAYLIST MODEL
# ============================================================================

class PlaylistModel(QAbstractTableModel):
    """Table model for the playlist."""
    
    HEADERS = ["#", "Title", "Artist", "Album", "Year"]

    def __init__(self, controller=None, icons=None):
        super().__init__()
        self._tracks = []
        self.current_index = -1
        self.highlight_color = None
        self.controller = controller
        self.icons = icons or {}

    def rowCount(self, parent=QModelIndex()):
        return len(self._tracks)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def data(self, index, role=Qt.DisplayRole):
        """Return data for the given index and role."""
        if not index.isValid():
            return None
        
        track = self._tracks[index.row()]
        col = index.column()
        
        if role == Qt.DisplayRole:
            return self._get_display_data(track, col, index.row())
        elif role == Qt.FontRole and index.row() == self.current_index:
            font = QFont()
            font.setBold(True)
            return font
        elif role == Qt.DecorationRole and col == 1 and index.row() == self.current_index:
            return self._get_playback_icon()
           
        return None

    def _get_display_data(self, track, col, row):
        """Get display data for a specific column."""
        if col == 0:
            return f"{track.get('trackno', row + 1):02d}"
        elif col == 1:
            return track.get("title", "")
        elif col == 2:
            return track.get("artist", "")
        elif col == 3:
            return track.get("album", "")
        elif col == 4:
            return track.get("year", "")
        return None

    def _get_playback_icon(self):
        """Get the appropriate playback icon based on state and theme."""
        if not self.controller:
            return None
        
        # Always check system theme for icon color
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        
        use_white_icon = False
        app = QApplication.instance()
        if app:
            base_color = app.palette().color(QPalette.Base)
            use_white_icon = is_dark_color(base_color)
        
        # If there's a highlight color, also check if that's dark
        # (for the currently playing row with custom highlight)
        if self.highlight_color:
            use_white_icon = is_dark_color(self.highlight_color)
        
        is_playing = self.controller.player.is_playing()
        
        if is_playing:
            return self.icons.get("row_play_white" if use_white_icon else "row_play")
        else:
            return self.icons.get("row_pause_white" if use_white_icon else "row_pause")

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        """Return header data."""
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def add_tracks(self, paths, clear=False):
        """Add tracks to the playlist."""
        if clear:
            self.clear()
        
        new_items = [
            extract_metadata(path, i)
            for i, path in enumerate(paths, start=1)
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS
        ]
        
        if new_items:
            start_row = len(self._tracks)
            end_row = start_row + len(new_items) - 1
            self.beginInsertRows(QModelIndex(), start_row, end_row)
            self._tracks.extend(new_items)
            self.endInsertRows()
            if clear:
                self.set_current_index(-1)

    def clear(self):
        """Clear all tracks from the playlist."""
        if self._tracks:
            self.beginRemoveRows(QModelIndex(), 0, len(self._tracks) - 1)
            self._tracks.clear()
            self.endRemoveRows()
            self.set_current_index(-1)

    def path_at(self, row):
        """Get the file path at the given row."""
        return self._tracks[row]["path"] if 0 <= row < len(self._tracks) else None

    def set_current_index(self, row):
        """Set the currently playing track index."""
        if self.current_index == row:
            return
        
        self.current_index = row
        if self.rowCount() > 0:
            top_left = self.index(0, 0)
            bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)

# ============================================================================
# AUDIO PLAYER CONTROLLER
# ============================================================================

class AudioPlayerController:
    """Controller for gapless audio playback."""

    def __init__(self, view=None, eq_widget=None):
        # Setup VLC environment (checks system VLC first, falls back to local plugins)
        plugins_dir = setup_vlc_environment()
        
        # VLC options to suppress verbose warnings and errors
        vlc_options = [
            '--quiet',                      # Suppress most messages
            '--no-video-title-show',        # Don't show video title on playback
            '--no-stats',                   # Disable statistics
            '--no-snapshot-preview',        # Disable snapshot preview
            '--ignore-config',              # Don't use VLC's config file
            '--no-plugins-cache',           # Completely disable plugin cache (prevents stale cache errors)
            '--verbose=0',                  # Set verbosity to minimum (0 = errors and warnings off)
        ]
        
        # Create VLC instance
        # If plugins_dir is None, use system VLC (no custom plugin path)
        # If plugins_dir is set, use local plugins with explicit path
        try:
            if plugins_dir:
                vlc_options.append(f'--plugin-path={plugins_dir}')
                self.instance = vlc.Instance(vlc_options)
                print("✓ VLC instance created with local plugins")
            else:
                self.instance = vlc.Instance(vlc_options)
                print("✓ VLC instance created using system installation")
        except Exception as e:
            print(f"❌ Failed to create VLC instance: {e}")
            print("Please ensure VLC is installed on your system or provide local plugins.")
            raise
        
        # Use gapless playback manager instead of single player
        self.gapless_manager = GaplessPlaybackManager(self.instance, eq_widget)
        self.gapless_manager.setup_events()
        
        # Connect signals for thread-safe updates
        self.gapless_manager.signals.track_changed.connect(self._on_gapless_track_change)
        self.gapless_manager.signals.start_equalizer.connect(self._start_equalizer)
        self.gapless_manager.signals.stop_equalizer.connect(self._stop_equalizer)
        
        # Legacy compatibility - point to active player
        self.player = self.gapless_manager.player_a
        
        self.current_index = -1
        self.model = None
        self.view = view
        self.eq_widget = eq_widget

    def _start_equalizer(self, filepath):
        """Start equalizer for new track (slot for signal) - runs on main thread."""
        if self.eq_widget:
            self.eq_widget.start(filepath)

    def _stop_equalizer(self):
        """Stop equalizer (slot for signal) - runs on main thread."""
        if self.eq_widget:
            self.eq_widget.timer.stop()

    def _on_gapless_track_change(self, filepath):
        """Handle automatic track change from gapless transition (slot for signal)."""
        # Reset the transition flag in the gapless manager when track actually changes
        self.gapless_manager._transition_triggered = False
        
        if self.model:
            for i in range(self.model.rowCount()):
                if self.model.path_at(i) == filepath:
                    self.current_index = i
                    self.model.set_current_index(i)
                    
                    # Update player reference so icon checks the correct active player
                    self.player = self.gapless_manager.get_active_player()
                    
                    if self.view:
                        self.view.clearSelection()
                        self.view.selectRow(i)
                        self.view.viewport().update()
                    
                    # Update UI
                    main_window = self.view.window()
                    if hasattr(main_window, "update_album_art"):
                        main_window.update_album_art(filepath)
                    
                    if main_window:
                        track = self.model._tracks[i]
                        artist = track.get("artist", "Unknown Artist")
                        title = track.get("title", "Unknown Track")
                        main_window.setWindowTitle(f"{artist} - {title}")
                    
                    # Update current track path in gapless manager
                    self.gapless_manager.current_track_path = filepath
                    
                    # CRITICAL: Preload next track for continuous gapless playback
                    print(f"Track changed to index {i}, preloading next...")
                    self._preload_next()
                    
                    # Update UI to show correct playback state and icon
                    if main_window:
                        main_window.update_playback_ui()
                        main_window.update_playpause_icon()
                    
                    break

    def set_model(self, model):
        """Set the playlist model."""
        self.model = model

    def set_view(self, view):
        """Set the playlist view."""
        self.view = view

    def set_equalizer(self, eq_widget):
        """Set the equalizer widget."""
        self.eq_widget = eq_widget
        self.gapless_manager.eq_widget = eq_widget

    def play_index(self, index):
        """Play the track at the given index with gapless preloading."""
        if not self.model:
            return
        
        path = self.model.path_at(index)
        if not path:
            return
        
        # Get next track for preloading
        next_path = None
        if index + 1 < self.model.rowCount():
            next_path = self.model.path_at(index + 1)
        
        # Use gapless manager
        self.gapless_manager.play_track(path, preload_next=next_path)
        
        self.current_index = index
        self.model.set_current_index(index)
        
        # CRITICAL: If this is the last track, clear preload state
        if next_path is None:
            print(f"Playing last track (index {index}), clearing preload state...")
            self._preload_next()
        
        # Update player reference for legacy code
        self.player = self.gapless_manager.get_active_player()
        
        if self.view:
            self.view.clearSelection()
            self.view.selectRow(index)
            self.view.viewport().update()
        
        main_window = self.view.window()
        if hasattr(main_window, "update_album_art"):
            main_window.update_album_art(path)
        
        if main_window:
            track = self.model._tracks[index]
            artist = track.get("artist", "Unknown Artist")
            title = track.get("title", "Unknown Track")
            main_window.setWindowTitle(f"{artist} - {title}")

    def _preload_next(self):
        """Preload the next track in playlist."""
        if not self.model or self.current_index < 0:
            print("Cannot preload: no model or invalid index")
            return
            
        next_index = self.current_index + 1
        if next_index < self.model.rowCount():
            next_path = self.model.path_at(next_index)
            if next_path:
                print(f"Preloading track {next_index}: {os.path.basename(next_path)}")
                # Run preload in background thread
                threading.Thread(
                    target=self.gapless_manager._preload_next_track,
                    args=(next_path,),
                    daemon=True
                ).start()
            else:
                print(f"No valid path for track {next_index}")
        else:
            # No more tracks to preload - clear the preload state completely
            print("!!! Reached last track - CLEARING ALL PRELOAD STATE !!!")
            with self.gapless_manager.preload_lock:
                self.gapless_manager.next_track_path = None
                # Stop the standby player and clear its media completely
                if self.gapless_manager.standby_player:
                    print(f"    Stopping and clearing standby player {'A' if self.gapless_manager.standby_player == self.gapless_manager.player_a else 'B'}")
                    self.gapless_manager.standby_player.stop()
                    # Set to None/empty media to truly clear it
                    self.gapless_manager.standby_player.set_media(None)
                print(f"    next_track_path is now: {self.gapless_manager.next_track_path}")
                print(f"    Standby player media cleared")

    def pause(self):
        """Pause playback."""
        self.gapless_manager.pause()

    def play(self):
        """Resume playback or restart from current index."""
        # Check if we're resuming from pause
        if (self.gapless_manager.current_track_path and 
            self.gapless_manager.active_player and 
            not self.gapless_manager.is_playing()):
            # Resume paused playback
            self.gapless_manager.resume()
        elif self.current_index >= 0:
            # Restart from current index after stop
            self.play_index(self.current_index)
        else:
            print("Cannot play: no track selected")

    def stop(self):
        """Stop playback."""
        self.gapless_manager.stop()

    def next(self):
        """Play the next track."""
        if self.model and self.current_index is not None:
            next_index = self.current_index + 1
            if next_index < self.model.rowCount():
                self.play_index(next_index)

    def previous(self):
        """Play the previous track."""
        if self.model and self.current_index is not None:
            prev_index = self.current_index - 1
            if prev_index >= 0:
                self.play_index(prev_index)

    def set_volume(self, volume):
        """Set the playback volume."""
        self.gapless_manager.set_volume(volume)

# ============================================================================
# CUSTOM DELEGATES
# ============================================================================

class PlayingRowDelegate(QStyledItemDelegate):
    """Custom delegate for playlist row highlighting."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.hover_row = -1

    def set_hover_row(self, row):
        """Set the currently hovered row."""
        if self.hover_row != row:
            self.hover_row = row
            if self.parent():
                self.parent().viewport().update()

    def paint(self, painter, option, index):
        """Paint the delegate."""
        opt = QStyleOptionViewItem(option)

        if index.row() == self.model.current_index and self.model.highlight_color:
            painter.save()
            painter.fillRect(opt.rect, self.model.highlight_color)
            painter.restore()

            opt.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver | QStyle.State_HasFocus)
            
            palette = opt.palette
            text_color = Qt.white if is_dark_color(self.model.highlight_color) else Qt.black
            palette.setColor(QPalette.Text, text_color)
            palette.setColor(QPalette.HighlightedText, text_color)
            opt.palette = palette

            super().paint(painter, opt, index)
            return

        if index.row() == self.hover_row:
            painter.save()
            # Use theme-aware hover color
            from PySide6.QtWidgets import QApplication
            from PySide6.QtGui import QPalette as QtPalette
            app = QApplication.instance()
            if app:
                app_palette = app.palette()
                base_color = app_palette.color(QtPalette.Base)
                if is_dark_color(base_color):
                    # Dark theme - lighter semi-transparent overlay
                    hover_color = QColor(base_color.lighter(130))
                    hover_color.setAlpha(100)
                else:
                    # Light theme - blue semi-transparent overlay
                    hover_color = QColor(220, 238, 255, 100)
            else:
                hover_color = QColor(220, 238, 255, 100)
            painter.fillRect(opt.rect, hover_color)
            painter.restore()

        if (option.state & QStyle.State_Selected) and index.row() != self.model.current_index:
            opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus | QStyle.State_MouseOver)
            font = opt.font
            font.setItalic(True)
            font.setBold(True)
            opt.font = font

        super().paint(painter, opt, index)

class DirectoryBrowserDelegate(QStyledItemDelegate):
    """Custom delegate for directory browser highlighting."""

    def __init__(self, tree_view, parent=None):
        super().__init__(parent)
        self.tree_view = tree_view
        self.highlight_color = None
    
    def paint(self, painter, option, index):
        """Paint the delegate."""
        opt = QStyleOptionViewItem(option)
        
        # Check if this is a file (not a directory) by checking if it has children
        model = index.model()
        is_directory = model.isDir(index)
        
        # If it's a file (not a directory), reduce indentation by one level
        # so files align with their parent folder instead of being indented further
        if not is_directory and self.tree_view:
            # Get the indentation amount
            indentation = self.tree_view.indentation()
            
            # Shift the rect left by one indentation level
            # This makes files align with their parent folder
            opt.rect.adjust(-indentation, 0, 0, 0)

        if (option.state & QStyle.State_Selected) and self.highlight_color:
            painter.save()
            painter.fillRect(opt.rect, self.highlight_color)
            painter.restore()

            opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus | 
                          QStyle.State_MouseOver | QStyle.State_Active)

            palette = opt.palette
            text_color = Qt.white if is_dark_color(self.highlight_color) else Qt.black
            palette.setColor(QPalette.Text, text_color)
            palette.setColor(QPalette.HighlightedText, text_color)
            opt.palette = palette

        super().paint(painter, opt, index)
    
    def set_highlight_color(self, color):
        """Update the highlight color."""
        self.highlight_color = color
        if self.tree_view:
            self.tree_view.viewport().update()

# ============================================================================
# CUSTOM WIDGETS
# ============================================================================

class AlbumArtLabel(QLabel):
    """QLabel that rescales pixmap with aspect ratio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(100, 100)
        self._original_pixmap = None

    def set_album_pixmap(self, pixmap: QPixmap):
        """Set the album art pixmap."""
        self._original_pixmap = pixmap
        self._update_scaled_pixmap()

    def resizeEvent(self, event):
        """Handle resize events."""
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self):
        """Update the scaled pixmap."""
        if self._original_pixmap:
            target_size = self.size().boundedTo(self._original_pixmap.size())
            scaled = self._original_pixmap.scaled(
                target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            super().setPixmap(scaled)

class PlaylistView(QTableView):
    """QTableView with watermark and row-wide hover support."""

    def __init__(self, logo_path=None, parent=None):
        super().__init__(parent)
        if logo_path is None:
            logo_path = get_asset_path("logo.png")
        self.logo = QPixmap(logo_path)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        """Handle mouse move events for hover tracking."""
        index = self.indexAt(event.position().toPoint())
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_hover_row"):
            delegate.set_hover_row(index.row() if index.isValid() else -1)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        """Handle mouse leave events."""
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_hover_row"):
            delegate.set_hover_row(-1)
        super().leaveEvent(event)
        
    def mousePressEvent(self, event):
        """Handle mouse press events for toggle selection."""
        if event.button() == Qt.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                if self.selectionModel().isSelected(index):
                    self.clearSelection()
                    return
        super().mousePressEvent(event)        
        
    def viewportEvent(self, event):
        """Handle viewport events for watermark drawing."""
        if event.type() == QEvent.Paint and not self.logo.isNull():
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            
            target_size = self.viewport().size() * 0.6
            scaled = self.logo.scaled(
                target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            
            x = (self.viewport().width() - scaled.width()) // 2
            y = (self.viewport().height() - scaled.height()) // 2
            painter.setOpacity(0.25)
            painter.drawPixmap(x, y, scaled)
            painter.end()
        
        return super().viewportEvent(event)

class PeakTransparencyDialog(QWidget):
    """Dialog for adjusting peak indicator transparency."""
    
    transparency_changed = Signal(int)
    
    def __init__(self, current_alpha=255, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Peak Indicator Transparency")
        self.setWindowIcon(QIcon(get_asset_path("icon.ico")))
        self.resize(400, 150)
        
        layout = QVBoxLayout(self)
        
        # Title label
        title = QLabel("Adjust Peak Indicator Transparency")
        title.setStyleSheet("font-size: 14px; font-weight: bold; padding: 10px;")
        layout.addWidget(title)
        
        # Slider with labels
        slider_layout = QHBoxLayout()
        
        self.label_transparent = QLabel("Transparent")
        self.label_transparent.setStyleSheet("color: #666;")
        slider_layout.addWidget(self.label_transparent)
        
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 255)
        self.slider.setValue(current_alpha)
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setTickInterval(25)
        self.slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #bbb;
                height: 8px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(100, 100, 100, 50),
                    stop:1 rgba(100, 100, 100, 255));
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #3399ff;
                border: 1px solid #777;
                width: 18px;
                margin: -5px 0;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:pressed { background: #2277dd; }
        """)
        self.slider.valueChanged.connect(self._on_slider_changed)
        slider_layout.addWidget(self.slider, 1)
        
        self.label_opaque = QLabel("Opaque")
        self.label_opaque.setStyleSheet("color: #666;")
        slider_layout.addWidget(self.label_opaque)
        
        layout.addLayout(slider_layout)
        
        # Current value display
        self.value_label = QLabel(f"Current: {int((current_alpha / 255) * 100)}%")
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setStyleSheet("font-size: 12px; color: #555; padding: 10px;")
        layout.addWidget(self.value_label)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        reset_btn = QPushButton("Reset to Default")
        reset_btn.clicked.connect(self._reset_to_default)
        button_layout.addWidget(reset_btn)
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        button_layout.addWidget(close_btn)
        
        layout.addLayout(button_layout)
    
    def _on_slider_changed(self, value):
        """Handle slider value change."""
        percentage = int((value / 255) * 100)
        self.value_label.setText(f"Current: {percentage}%")
        self.transparency_changed.emit(value)
    
    def _reset_to_default(self):
        """Reset to default transparency (fully opaque)."""
        self.slider.setValue(255)
        
class FontSelectionDialog(QWidget):
    """Dialog for selecting font family and size."""
    
    font_changed = Signal(QFont)
    
    def __init__(self, current_font=None, title="Font Selection", parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(title)
        self.setWindowIcon(QIcon(get_asset_path("icon.ico")))
        self.resize(450, 250)
        
        if current_font is None:
            current_font = QFont()
        
        self.current_font = QFont(current_font)
        
        layout = QVBoxLayout(self)
        
        # Title label
        title_label = QLabel(f"{title}")
        title_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 10px;")
        layout.addWidget(title_label)
        
        # Font family selection
        family_layout = QHBoxLayout()
        family_label = QLabel("Font Family:")
        family_label.setStyleSheet("color: #555; font-weight: bold;")
        family_layout.addWidget(family_label)
        
        from PySide6.QtWidgets import QFontComboBox
        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(self.current_font)
        self.font_combo.currentFontChanged.connect(self._on_font_changed)
        family_layout.addWidget(self.font_combo, 1)
        layout.addLayout(family_layout)
        
        # Font size selection
        size_layout = QHBoxLayout()
        size_label = QLabel("Font Size:")
        size_label.setStyleSheet("color: #555; font-weight: bold;")
        size_layout.addWidget(size_label)
        
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(6, 24)
        self.size_slider.setValue(self.current_font.pointSize())
        self.size_slider.setTickPosition(QSlider.TicksBelow)
        self.size_slider.setTickInterval(2)
        self.size_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #bbb;
                height: 8px;
                background: #e0e0e0;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #3399ff;
                border: 1px solid #777;
                width: 18px;
                margin: -5px 0;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:pressed { background: #2277dd; }
        """)
        self.size_slider.valueChanged.connect(self._on_size_changed)
        size_layout.addWidget(self.size_slider, 1)
        
        self.size_label = QLabel(f"{self.current_font.pointSize()} pt")
        self.size_label.setStyleSheet("color: #555; min-width: 50px;")
        size_layout.addWidget(self.size_label)
        layout.addLayout(size_layout)
        
        # Preview
        preview_group = QWidget()
        preview_group.setStyleSheet("background: white; border: 1px solid #ccc; border-radius: 4px;")
        preview_layout = QVBoxLayout(preview_group)
        
        preview_title = QLabel("Preview:")
        preview_title.setStyleSheet("font-weight: bold; color: #555;")
        preview_layout.addWidget(preview_title)
        
        self.preview_label = QLabel("The quick brown fox jumps over the lazy dog\n0123456789")
        self.preview_label.setFont(self.current_font)
        self.preview_label.setStyleSheet("padding: 10px;")
        preview_layout.addWidget(self.preview_label)
        
        layout.addWidget(preview_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        reset_btn = QPushButton("Reset to Default")
        reset_btn.clicked.connect(self._reset_to_default)
        button_layout.addWidget(reset_btn)
        
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_font)
        button_layout.addWidget(apply_btn)
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        button_layout.addWidget(close_btn)
        
        layout.addLayout(button_layout)
    
    def _on_font_changed(self, font):
        """Handle font family change."""
        self.current_font.setFamily(font.family())
        self._update_preview()
    
    def _on_size_changed(self, size):
        """Handle font size change."""
        self.current_font.setPointSize(size)
        self.size_label.setText(f"{size} pt")
        self._update_preview()
    
    def _update_preview(self):
        """Update the preview label."""
        self.preview_label.setFont(self.current_font)
    
    def _apply_font(self):
        """Apply the selected font."""
        self.font_changed.emit(self.current_font)
    
    def _reset_to_default(self):
        """Reset to default system font."""
        default_font = QFont()
        self.font_combo.setCurrentFont(default_font)
        self.size_slider.setValue(default_font.pointSize())
                
# ============================================================================
# GLOBAL MEDIA KEY HANDLER
# ============================================================================

if sys.platform == 'win32':
    from ctypes import wintypes
    import ctypes.wintypes

class GlobalMediaKeyHandler(QAbstractNativeEventFilter):
    """Cross-platform global media key handler."""
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.hwnd = None
        self.setup_platform_handler()
    
    def setup_platform_handler(self):
        """Setup platform-specific media key handling."""
        if sys.platform == 'win32':
            self._setup_windows_handler()
        elif sys.platform == 'darwin':
            self._setup_macos_handler()
        else:
            self._setup_linux_handler()
    
    def _setup_windows_handler(self):
        """Setup Windows media key handling using RegisterHotKey."""
        try:
            self.hwnd = int(self.main_window.winId())
            
            self.HOTKEY_PLAY_PAUSE = 1
            self.HOTKEY_STOP = 2
            self.HOTKEY_NEXT = 3
            self.HOTKEY_PREV = 4
            
            VK_MEDIA_PLAY_PAUSE = 0xB3
            VK_MEDIA_STOP = 0xB2
            VK_MEDIA_NEXT_TRACK = 0xB0
            VK_MEDIA_PREV_TRACK = 0xB1
            
            user32 = ctypes.windll.user32
            
            result1 = user32.RegisterHotKey(self.hwnd, self.HOTKEY_PLAY_PAUSE, 0, VK_MEDIA_PLAY_PAUSE)
            result2 = user32.RegisterHotKey(self.hwnd, self.HOTKEY_STOP, 0, VK_MEDIA_STOP)
            result3 = user32.RegisterHotKey(self.hwnd, self.HOTKEY_NEXT, 0, VK_MEDIA_NEXT_TRACK)
            result4 = user32.RegisterHotKey(self.hwnd, self.HOTKEY_PREV, 0, VK_MEDIA_PREV_TRACK)
            
            if result1 and result2 and result3 and result4:
                print("Windows global media keys registered successfully")
            else:
                print("Some media keys could not be registered (may be in use by another application)")
            
        except Exception as e:
            print(f"Failed to setup Windows media keys: {e}")
            print("Falling back to application-level shortcuts")
    
    def _setup_macos_handler(self):
        """Setup macOS media key handling."""
        print("macOS media key support requires additional configuration")
        print("Using application-level shortcuts instead")
    
    def _setup_linux_handler(self):
        """Setup Linux media key handling using DBus (MPRIS)."""
        print("Linux media key support via MPRIS not fully implemented")
        print("Using application-level shortcuts instead")
    
    def nativeEventFilter(self, eventType, message):
        """Handle native events for Windows."""
        if sys.platform == 'win32':
            try:
                WM_HOTKEY = 0x0312
                
                if eventType == b"windows_generic_MSG" or eventType == b"windows_dispatcher_MSG":
                    msg = wintypes.MSG.from_address(int(message))
                    
                    if msg.message == WM_HOTKEY:
                        if msg.wParam == self.HOTKEY_PLAY_PAUSE:
                            QTimer.singleShot(0, self.main_window.on_playpause_clicked)
                            return True, 0
                        elif msg.wParam == self.HOTKEY_STOP:
                            QTimer.singleShot(0, self.main_window.on_stop_clicked)
                            return True, 0
                        elif msg.wParam == self.HOTKEY_NEXT:
                            QTimer.singleShot(0, self.main_window.on_next_clicked)
                            return True, 0
                        elif msg.wParam == self.HOTKEY_PREV:
                            QTimer.singleShot(0, self.main_window.on_prev_clicked)
                            return True, 0
            except Exception as e:
                print(f"Error processing native event: {e}")
        
        return False, 0
    
    def cleanup(self):
        """Cleanup registered hotkeys."""
        if sys.platform == 'win32' and self.hwnd:
            try:
                user32 = ctypes.windll.user32
                user32.UnregisterHotKey(self.hwnd, self.HOTKEY_PLAY_PAUSE)
                user32.UnregisterHotKey(self.hwnd, self.HOTKEY_STOP)
                user32.UnregisterHotKey(self.hwnd, self.HOTKEY_NEXT)
                user32.UnregisterHotKey(self.hwnd, self.HOTKEY_PREV)
                print("Windows global media keys unregistered")
            except Exception as e:
                print(f"Error unregistering hotkeys: {e}")

# ============================================================================
# MAIN WINDOW - PART 1: Class Definition and Styles
# ============================================================================

class MainWindow(QMainWindow):
    """Main application window."""

    @staticmethod
    def get_tree_style(highlight_color, highlight_text_color):
        """Generate theme-aware tree view stylesheet."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        
        app = QApplication.instance()
        if not app:
            # Default to light theme
            return f"""
                QTreeView {{
                    background-color: #fafafa;
                    alternate-background-color: #f0f0f0;
                    border: none;
                    color: #000000;
                }}
                QTreeView::item {{
                    padding: 0px 4px;
                    min-height: 12px;
                    border: none;
                    outline: none;
                    color: #000000;
                }}
                QTreeView::item:hover {{
                    background: #dceeff;
                    color: black;
                    border: none;
                    outline: none;
                    border-radius: 0px;
                }}
                QTreeView::item:selected {{
                    background: {highlight_color};
                    color: {highlight_text_color};
                    border: 1px solid transparent;
                    outline: transparent;
                    border-radius: 0px;
                }}
                QTreeView::item:selected:hover,
                QTreeView::item:selected:active,
                QTreeView::item:selected:!active,
                QTreeView::item:selected:pressed {{
                    background: {highlight_color};
                    color: {highlight_text_color};
                    border: 1px solid transparent;
                    outline: transparent;
                    border-radius: 0px;
                }}
                QTreeView::item:focus {{
                    border: 1px solid transparent;
                    outline: transparent;
                }}
                QTreeView::branch {{
                    background: transparent;
                    width: 0px;
                    border: none;
                }}
                QTreeView::branch:has-siblings:!adjoins-item,
                QTreeView::branch:has-siblings:adjoins-item,
                QTreeView::branch:!has-children:!has-siblings:adjoins-item {{
                    border-image: none;
                    image: none;
                    width: 0px;
                }}
                QTreeView::branch:has-children:!has-siblings:closed,
                QTreeView::branch:closed:has-children:has-siblings {{
                    border-image: none;
                    image: none;
                    width: 0px;
                }}
                QTreeView::branch:open:has-children:!has-siblings,
                QTreeView::branch:open:has-children:has-siblings {{
                    border-image: none;
                    image: none;
                    width: 0px;
                }}
            """
        
        palette = app.palette()
        base_color = palette.color(QPalette.Base)
        text_color = palette.color(QPalette.Text)
        is_dark = is_dark_color(base_color)
        
        if is_dark:
            hover_bg = base_color.lighter(120).name()
        else:
            hover_bg = "#dceeff"
        
        return f"""
            QTreeView {{
                background-color: {base_color.name()};
                alternate-background-color: {base_color.lighter(105).name() if not is_dark else base_color.darker(105).name()};
                border: none;
                color: {text_color.name()};
            }}
            QTreeView::item {{
                padding: 0px 4px;
                min-height: 12px;
                border: none;
                outline: none;
                color: {text_color.name()};
            }}
            QTreeView::item:hover {{
                background: {hover_bg};
                color: {text_color.name()};
                border: none;
                outline: none;
                border-radius: 0px;
            }}
            QTreeView::item:selected {{
                background: {highlight_color};
                color: {highlight_text_color};
                border: 1px solid transparent;
                outline: transparent;
                border-radius: 0px;
            }}
            QTreeView::item:selected:hover,
            QTreeView::item:selected:active,
            QTreeView::item:selected:!active,
            QTreeView::item:selected:pressed {{
                background: {highlight_color};
                color: {highlight_text_color};
                border: 1px solid transparent;
                outline: transparent;
                border-radius: 0px;
            }}
            QTreeView::item:focus {{
                border: 1px solid transparent;
                outline: transparent;
            }}
            QTreeView::branch {{
                background: transparent;
                width: 0px;
                border: none;
            }}
            QTreeView::branch:has-siblings:!adjoins-item,
            QTreeView::branch:has-siblings:adjoins-item,
            QTreeView::branch:!has-children:!has-siblings:adjoins-item {{
                border-image: none;
                image: none;
                width: 0px;
            }}
            QTreeView::branch:has-children:!has-siblings:closed,
            QTreeView::branch:closed:has-children:has-siblings {{
                border-image: none;
                image: none;
                width: 0px;
            }}
            QTreeView::branch:open:has-children:!has-siblings,
            QTreeView::branch:open:has-children:has-siblings {{
                border-image: none;
                image: none;
                width: 0px;
            }}
        """

    @staticmethod
    def get_button_style():
        """Generate theme-aware button stylesheet."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        
        app = QApplication.instance()
        if not app:
            # Default to light theme
            return """
                QPushButton {
                    background-color: #f0f0f0;
                    border: none;
                    border-radius: 6px;
                    padding: 6px;
                }
                QPushButton:hover { background-color: #e0e0e0; }
                QPushButton:pressed { background-color: #d0d0d0; }
            """
        
        palette = app.palette()
        button_color = palette.color(QPalette.Button)
        base_color = palette.color(QPalette.Base)
        is_dark = is_dark_color(base_color)
        
        # Create more noticeable hover effect
        if is_dark:
            hover_color = button_color.lighter(130)
            pressed_color = button_color.darker(120)
        else:
            hover_color = button_color.darker(110)
            pressed_color = button_color.darker(120)
        
        return f"""
            QPushButton {{
                background-color: {button_color.name()};
                border: none;
                border-radius: 6px;
                padding: 6px;
            }}
            QPushButton:hover {{ background-color: {hover_color.name()}; }}
            QPushButton:pressed {{ background-color: {pressed_color.name()}; }}
        """

    @staticmethod
    def get_slider_style():
        """Generate theme-aware slider stylesheet."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        
        app = QApplication.instance()
        if not app:
            # Default to light theme
            return """
                QSlider::groove:horizontal {
                    border: 1px solid #bbb;
                    height: 8px;
                    background: #e0e0e0;
                    border-radius: 4px;
                }
                QSlider::sub-page:horizontal {
                    background: #3399ff;
                    border: 1px solid #777;
                    height: 8px;
                    border-radius: 4px;
                }
                QSlider::add-page:horizontal {
                    background: #e0e0e0;
                    border: 1px solid #777;
                    height: 8px;
                    border-radius: 4px;
                }
                QSlider::handle:horizontal {
                    background: #ffffff;
                    border: 1px solid #777;
                    width: 16px;
                    margin: -5px 0;
                    border-radius: 8px;
                }
                QSlider::handle:horizontal:pressed { background: #cccccc; }
            """
        
        palette = app.palette()
        base_color = palette.color(QPalette.Base)
        highlight_color = palette.color(QPalette.Highlight)
        button_color = palette.color(QPalette.Button)
        
        return f"""
            QSlider::groove:horizontal {{
                border: 1px solid palette(mid);
                height: 8px;
                background: {base_color.darker(110).name()};
                border-radius: 4px;
            }}
            QSlider::sub-page:horizontal {{
                background: {highlight_color.name()};
                border: 1px solid palette(dark);
                height: 8px;
                border-radius: 4px;
            }}
            QSlider::add-page:horizontal {{
                background: {base_color.darker(110).name()};
                border: 1px solid palette(dark);
                height: 8px;
                border-radius: 4px;
            }}
            QSlider::handle:horizontal {{
                background: {button_color.name()};
                border: 1px solid palette(dark);
                width: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }}
            QSlider::handle:horizontal:pressed {{ background: {button_color.darker(110).name()}; }}
        """

    @staticmethod
    def get_playlist_style():
        """Generate theme-aware playlist stylesheet."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        
        app = QApplication.instance()
        if not app:
            # Default to light theme
            return """
                QTableView {
                    background-color: rgba(255, 255, 255, 150);
                    alternate-background-color: rgba(240, 240, 240, 150);
                    border: none;
                    gridline-color: #ddd;
                    selection-background-color: transparent;
                    selection-color: inherit;
                    outline: none;
                }
                QTableView::item {
                    background-color: transparent;
                    padding: 4px 6px;
                    border: none;
                    outline: none;
                }
                QTableView::item:selected {
                    background: transparent;
                    color: inherit;
                    border: none;
                    outline: none;
                }
                QTableView::item:focus {
                    border: none;
                    outline: none;
                }
            """
        
        palette = app.palette()
        base_color = palette.color(QPalette.Base)
        is_dark = is_dark_color(base_color)
        
        if is_dark:
            # Dark theme - semi-transparent dark backgrounds
            return f"""
                QTableView {{
                    background-color: rgba({base_color.red()}, {base_color.green()}, {base_color.blue()}, 150);
                    alternate-background-color: rgba({base_color.lighter(110).red()}, {base_color.lighter(110).green()}, {base_color.lighter(110).blue()}, 150);
                    border: none;
                    gridline-color: palette(mid);
                    selection-background-color: transparent;
                    selection-color: inherit;
                    outline: none;
                    color: palette(text);
                }}
                QTableView::item {{
                    background-color: transparent;
                    padding: 4px 6px;
                    border: none;
                    outline: none;
                    color: palette(text);
                }}
                QTableView::item:selected {{
                    background: transparent;
                    color: inherit;
                    border: none;
                    outline: none;
                }}
                QTableView::item:focus {{
                    border: none;
                    outline: none;
                }}
            """
        else:
            # Light theme - semi-transparent light backgrounds
            return """
                QTableView {
                    background-color: rgba(255, 255, 255, 150);
                    alternate-background-color: rgba(240, 240, 240, 150);
                    border: none;
                    gridline-color: #ddd;
                    selection-background-color: transparent;
                    selection-color: inherit;
                    outline: none;
                }
                QTableView::item {
                    background-color: transparent;
                    padding: 4px 6px;
                    border: none;
                    outline: none;
                }
                QTableView::item:selected {
                    background: transparent;
                    color: inherit;
                    border: none;
                    outline: none;
                }
                QTableView::item:focus {
                    border: none;
                    outline: none;
                }
            """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lithe Player")
        self.resize(1100, 700)
        self.setWindowIcon(QIcon(get_asset_path("icon.ico")))

        # Use JSON-based settings instead of Windows Registry
        self.settings = JsonSettings("lithe_player_config.json")

        self.icons = {
            "row_play": QIcon(get_asset_path("plplay.svg")),
            "row_play_white": QIcon(get_asset_path("plplaywhite.svg")),
            "row_pause": QIcon(get_asset_path("plpause.svg")),
            "row_pause_white": QIcon(get_asset_path("plpausewhite.svg")),
            "ctrl_play": get_themed_icon("play.svg"),
            "ctrl_pause": get_themed_icon("pause.svg"),
        }

        self._setup_ui()
        self._setup_connections()
        self._setup_vlc_events()
        self._setup_keyboard_shortcuts()

        self.global_media_handler = None
        self.peak_transparency_dialog = None
        self.playlist_font_dialog = None
        self.browser_font_dialog = None
        self._setup_global_media_keys()

        self.restore_settings()

    def _setup_global_media_keys(self):
        """Setup global media key handling."""
        try:
            self.global_media_handler = GlobalMediaKeyHandler(self)
            
            if sys.platform == 'win32':
                QApplication.instance().installNativeEventFilter(self.global_media_handler)
                print("Global media key support enabled")
            else:
                print("Global media keys available for application focus only")
                
        except Exception as e:
            print(f"Could not setup global media keys: {e}")
            print("Falling back to application-level shortcuts")

    # UI SETUP METHODS
    
    def _setup_ui(self):
        """Initialize all UI components."""
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(self.splitter)

        self._setup_left_panel()
        self._setup_right_panel()
        self._setup_bottom_controls(main_layout)
        self._setup_menu_bar()

        default_path = self.settings.value("default_dir", QDir.rootPath())
        self.fs_model.setRootPath(default_path)
        self.tree.setRootIndex(self.fs_model.index(default_path))
        self.update_reset_action_state()

    def _setup_left_panel(self):
        """Setup file browser and album art display."""
        self.fs_model = QFileSystemModel()
        self.fs_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)

        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        self.tree.setSortingEnabled(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.header().hide()
        
        # Set indentation for subfolder hierarchy visualization
        self.tree.setIndentation(15)
        
        # Disable root decoration (removes the space for branch indicators)
        self.tree.setRootIsDecorated(False)

        for col in range(1, self.fs_model.columnCount()):
            self.tree.hideColumn(col)

        self.tree_delegate = DirectoryBrowserDelegate(self.tree, self.tree)
        self.tree.setItemDelegate(self.tree_delegate)
        self.tree.setStyleSheet(self.get_tree_style("#3399ff", "white"))
        self.tree.expanded.connect(self._on_tree_expanded)

        self.album_art = AlbumArtLabel()
        self.album_art.setStyleSheet("""
            QLabel {
                background: palette(base);
                border: 1px solid palette(mid);
            }
        """)

        self.left_splitter = QSplitter(Qt.Vertical)
        self.left_splitter.addWidget(self.tree)
        self.left_splitter.addWidget(self.album_art)
        self.left_splitter.setSizes([400, 200])

        self.splitter.addWidget(self.left_splitter)

    def _on_tree_expanded(self, index):
        """Ensure expanded folder stays visible."""
        QTimer.singleShot(0, lambda: self.tree.scrollTo(
            index, QAbstractItemView.PositionAtCenter
        ))

    def _setup_right_panel(self):
        """Setup playlist table."""
        playlist_container = QWidget()
        playlist_layout = QVBoxLayout(playlist_container)
        playlist_layout.setContentsMargins(0, 0, 0, 0)

        self.playlist_model = PlaylistModel(controller=None, icons=self.icons)
        self.playlist = PlaylistView(get_asset_path("logo.png"))
        self.playlist.setModel(self.playlist_model)
        self.playlist.setSelectionBehavior(QTableView.SelectRows)
        self.playlist.setSelectionMode(QTableView.SingleSelection)
        self.playlist.setAlternatingRowColors(True)
        self.playlist.setIconSize(QSize(16, 16))
        self.playlist.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.playlist.setStyleSheet(self.get_playlist_style())

        header = self.playlist.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(False)
        header.setSectionsClickable(False)
        for col in range(len(PlaylistModel.HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)

        self.delegate = PlayingRowDelegate(self.playlist_model, self.playlist)
        self.playlist.setItemDelegate(self.delegate)

        playlist_layout.addWidget(self.playlist)
        self.splitter.addWidget(playlist_container)

        self.controller = AudioPlayerController(self.playlist)
        self.controller.set_model(self.playlist_model)
        self.playlist_model.controller = self.controller

    def _setup_bottom_controls(self, parent_layout):
        """Setup playback controls, progress bar, volume, and equalizer."""
        bottom_layout = QVBoxLayout()
        parent_layout.addLayout(bottom_layout)

        controls = QHBoxLayout()
        controls.addStretch(1)
        
        self.btn_prev = self._create_button(get_themed_icon("prev.svg"), 24)
        self.btn_playpause = self._create_button(get_themed_icon("play.svg"), 24)
        self.btn_stop = self._create_button(get_themed_icon("stop.svg"), 24)
        self.btn_next = self._create_button(get_themed_icon("next.svg"), 24)
        
        for btn in [self.btn_prev, self.btn_playpause, self.btn_stop, self.btn_next]:
            btn.setStyleSheet(self.get_button_style())
            controls.addWidget(btn)
        
        controls.addStretch(1)
        bottom_layout.addLayout(controls)

        progress_row = QHBoxLayout()
        
        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.setStyleSheet(self.get_slider_style())
        
        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setStyleSheet("QLabel { color: #555; font-size: 12px; font-weight: 500; }")
        
        progress_row.addWidget(self.progress_slider, 3)
        progress_row.addWidget(self.lbl_time)
        progress_row.addSpacing(15)

        self.lbl_vol = QLabel("Volume:")
        self.lbl_vol.setStyleSheet("QLabel { color: #555; font-size: 12px; font-weight: 500; }")
        
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(70)
        self.slider_vol.setStyleSheet(self.get_slider_style())
        
        progress_row.addWidget(self.lbl_vol)
        progress_row.addWidget(self.slider_vol)
        bottom_layout.addLayout(progress_row)

        self.equalizer = EqualizerWidget(bar_count=70, segments=15)
        self.equalizer.setFixedHeight(120)
        bottom_layout.addWidget(self.equalizer)

        self.controller.set_equalizer(self.equalizer)

        self.timer = QTimer(self)
        self.timer.setInterval(PROGRESS_UPDATE_INTERVAL_MS)
        self.timer.timeout.connect(self.update_progress)
        self.timer.start()

    def _setup_menu_bar(self):
        """Setup application menu bar."""
        file_menu = self.menuBar().addMenu("&File")
        
        act_open = QAction("Open folder…", self)
        act_open.triggered.connect(self.on_add_folder_clicked)
        file_menu.addAction(act_open)

        choose_default_act = QAction("Choose default folder…", self)
        choose_default_act.triggered.connect(self.on_choose_default_folder)
        file_menu.addAction(choose_default_act)

        self.reset_default_act = QAction("Reset default folder", self)
        self.reset_default_act.triggered.connect(self.on_reset_default_folder)
        file_menu.addAction(self.reset_default_act)

        view_menu = self.menuBar().addMenu("&View")
        
        act_color = QAction("Set accent colour", self)
        act_color.triggered.connect(self.on_choose_highlight_color)
        view_menu.addAction(act_color)
        
        view_menu.addSeparator()
        
        act_peak_color = QAction("Set peak indicator colour…", self)
        act_peak_color.triggered.connect(self.on_choose_peak_color)
        view_menu.addAction(act_peak_color)
        
        self.reset_peak_color_act = QAction("Reset peak indicator colour", self)
        self.reset_peak_color_act.triggered.connect(self.on_reset_peak_color)
        self.reset_peak_color_act.setEnabled(False)
        view_menu.addAction(self.reset_peak_color_act)
        
        act_peak_transparency = QAction("Adjust peak indicator transparency…", self)
        act_peak_transparency.triggered.connect(self.on_adjust_peak_transparency)
        view_menu.addAction(act_peak_transparency)
        
        view_menu.addSeparator()
        
        act_playlist_font = QAction("Set playlist font…", self)
        act_playlist_font.triggered.connect(self.on_set_playlist_font)
        view_menu.addAction(act_playlist_font)
        
        act_browser_font = QAction("Set directory browser font…", self)
        act_browser_font.triggered.connect(self.on_set_browser_font)
        view_menu.addAction(act_browser_font)

    def _setup_connections(self):
        """Connect signals and slots."""
        self.btn_playpause.clicked.connect(self.on_playpause_clicked)
        self.btn_stop.clicked.connect(self.on_stop_clicked)
        self.btn_prev.clicked.connect(self.on_prev_clicked)
        self.btn_next.clicked.connect(self.on_next_clicked)
        self.slider_vol.valueChanged.connect(self.on_volume_changed)
        self.progress_slider.sliderReleased.connect(self.on_seek)
        self.tree.doubleClicked.connect(self.on_tree_double_click)
        self.tree.expanded.connect(self.on_tree_expanded)
        self.playlist.doubleClicked.connect(self.on_playlist_double_click)
        
        self.on_volume_changed(self.slider_vol.value())

    def _setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts."""
        space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        space_shortcut.activated.connect(self.on_playpause_clicked)
        
        left_shortcut = QShortcut(QKeySequence(Qt.Key_Left), self)
        left_shortcut.activated.connect(self.on_prev_clicked)
        
        right_shortcut = QShortcut(QKeySequence(Qt.Key_Right), self)
        right_shortcut.activated.connect(self.on_next_clicked)
        
        try:
            play_pause_shortcut = QShortcut(QKeySequence(Qt.Key_MediaPlay), self)
            play_pause_shortcut.activated.connect(self.on_playpause_clicked)
            
            toggle_shortcut = QShortcut(QKeySequence(Qt.Key_MediaTogglePlayPause), self)
            toggle_shortcut.activated.connect(self.on_playpause_clicked)
            
            pause_shortcut = QShortcut(QKeySequence(Qt.Key_MediaPause), self)
            pause_shortcut.activated.connect(self.on_playpause_clicked)
            
            stop_shortcut = QShortcut(QKeySequence(Qt.Key_MediaStop), self)
            stop_shortcut.activated.connect(self.on_stop_clicked)
            
            next_shortcut = QShortcut(QKeySequence(Qt.Key_MediaNext), self)
            next_shortcut.activated.connect(self.on_next_clicked)
            
            prev_shortcut = QShortcut(QKeySequence(Qt.Key_MediaPrevious), self)
            prev_shortcut.activated.connect(self.on_prev_clicked)
            
        except Exception as e:
            print(f"Some media key shortcuts unavailable: {e}")
        
        print("Keyboard shortcuts configured")

    def _setup_vlc_events(self):
        """Setup VLC player event handlers."""
        event_manager = self.controller.player.event_manager()
        event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, 
                                   lambda e: self.on_playing())
        event_manager.event_attach(vlc.EventType.MediaPlayerPaused, 
                                   lambda e: self.on_paused())
        event_manager.event_attach(vlc.EventType.MediaPlayerStopped, 
                                   lambda e: self.on_stopped())

    def _create_button(self, icon_or_path, icon_size):
        """Helper to create a button with an icon."""
        button = QPushButton()
        if isinstance(icon_or_path, QIcon):
            button.setIcon(icon_or_path)
        else:
            button.setIcon(QIcon(icon_or_path))
        button.setIconSize(QSize(icon_size, icon_size))
        return button

    # PLAYBACK EVENT HANDLERS
    
    def on_playing(self):
        """Handle playing event."""
        self.update_playback_ui()
        self.update_playpause_icon()

    def on_paused(self):
        """Handle paused event."""
        self.update_playback_ui()
        self.update_playpause_icon()

    def on_stopped(self):
        """Handle stopped event."""
        self.update_playback_ui()
        self.update_playpause_icon()
        # Stop equalizer safely via timer to ensure main thread execution
        if self.equalizer:
            QTimer.singleShot(0, lambda: self.equalizer.stop(clear_display=True))

    # UI UPDATE METHODS
    
    def update_album_art(self, filepath):
        """Update the album art display."""
        pixmap = extract_album_art(filepath)
        if pixmap:
            self.album_art.set_album_pixmap(pixmap)
        else:
            self.album_art.clear()
            self.album_art._original_pixmap = None

    def update_playpause_icon(self):
        """Update play/pause button icon based on playback state."""
        if self.controller.player.is_playing():
            self.btn_playpause.setIcon(self.icons["ctrl_pause"])
        else:
            self.btn_playpause.setIcon(self.icons["ctrl_play"])

    def update_playback_ui(self):
        """Update UI elements related to playback."""
        self.playlist.viewport().update()

    def update_slider_colors(self):
        """Apply highlight color to sliders and equalizer."""
        if not self.playlist_model.highlight_color:
            return
        
        color_name = self.playlist_model.highlight_color.name()
        slider_style = f"""
            QSlider::groove:horizontal {{
                border: 1px solid #999;
                height: 6px;
                background: {color_name};
                border-radius: 4px;
            }}
            QSlider::handle:horizontal {{
                background: {color_name};
                border: 1px solid #666;
                width: 18px;
                margin: -5px 0;
                border-radius: 9px;
            }}
        """
        self.progress_slider.setStyleSheet(slider_style)
        self.slider_vol.setStyleSheet(slider_style)
        self.equalizer.update_color(self.playlist_model.highlight_color)

    def update_tree_stylesheet(self, color):
        """Update tree view stylesheet with selected highlight color."""
        text_color = "white" if is_dark_color(color) else "black"
        self.tree.setStyleSheet(
            self.get_tree_style(color.name(), text_color)
        )

    def update_reset_action_state(self):
        """Enable/disable reset default folder action."""
        self.reset_default_act.setEnabled(self.settings.contains("default_dir"))

    # PLAYBACK CONTROL HANDLERS
    
    def on_playpause_clicked(self):
        """Handle play/pause button click."""
        if self.controller.gapless_manager.is_playing():
            # Currently playing - pause it
            self.controller.pause()
        else:
            # Check if we have a valid current index
            if self.playlist_model.rowCount() > 0:
                if self.controller.current_index == -1:
                    # No track selected - start from beginning
                    self.controller.play_index(0)
                else:
                    # Resume or replay current track
                    self.controller.play()
            else:
                print("No tracks in playlist")
        
        self.update_playback_ui()
        self.update_playpause_icon()

    def on_stop_clicked(self):
        """Handle stop button click."""
        self.controller.stop()
        self.update_playback_ui()

    def on_prev_clicked(self):
        """Handle previous button click."""
        self.controller.previous()
        self.update_playback_ui()

    def on_next_clicked(self):
        """Handle next button click."""
        self.controller.next()
        self.update_playback_ui()

    def on_volume_changed(self, volume):
        """Handle volume slider change."""
        self.controller.set_volume(volume)

    def on_seek(self):
        """Handle progress slider seek."""
        if self.controller.player and self.controller.player.is_playing():
            length = self.controller.player.get_length()
            if length > 0:
                position = self.progress_slider.value() / 1000.0
                self.controller.player.set_time(int(length * position))

    def update_progress(self):
        """Update progress slider and time label."""
        if not self.controller.gapless_manager:
            return
        
        length = self.controller.gapless_manager.get_length()
        current = self.controller.gapless_manager.get_time()
        
        if length > 0 and current >= 0:
            position = current / length
            self.progress_slider.blockSignals(True)
            self.progress_slider.setValue(int(position * 1000))
            self.progress_slider.blockSignals(False)
            
            current_str = self.format_time(current)
            length_str = self.format_time(length)
            self.lbl_time.setText(f"{current_str} / {length_str}")

    def on_seek(self):
        """Handle progress slider seek."""
        if self.controller.gapless_manager and self.controller.gapless_manager.is_playing():
            length = self.controller.gapless_manager.get_length()
            if length > 0:
                position = self.progress_slider.value() / 1000.0
                self.controller.gapless_manager.set_time(int(length * position))

    @staticmethod
    def format_time(milliseconds):
        """Format time from milliseconds to MM:SS."""
        seconds = milliseconds // 1000
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d}"

    # FILE BROWSER HANDLERS
    
    def on_tree_double_click(self, index):
        """Handle double-click on file browser."""
        path = self.fs_model.filePath(index)
        
        if os.path.isfile(path):
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS:
                self.playlist_model.add_tracks([path])
                row = self.playlist_model.rowCount() - 1
                self.controller.play_index(row)
        elif os.path.isdir(path):
            files = self._get_audio_files_from_directory(path)
            if files:
                self.playlist_model.add_tracks(files, clear=True)
                self.controller.play_index(0)
        
        self.update_playback_ui()

    def on_tree_expanded(self, index):
        """Handle tree expansion - collapse siblings."""
        parent = index.parent()
        for row in range(self.fs_model.rowCount(parent)):
            sibling = self.fs_model.index(row, 0, parent)
            if sibling != index and self.tree.isExpanded(sibling):
                self.tree.collapse(sibling)

    # PLAYLIST HANDLERS
    
    def on_playlist_double_click(self, index):
        """Handle double-click on playlist."""
        self.controller.play_index(index.row())
        self.update_playback_ui()

    # MENU ACTION HANDLERS
    
    def on_set_playlist_font(self):
        """Handle 'Set playlist font' menu action."""
        if self.playlist_font_dialog is None or not self.playlist_font_dialog.isVisible():
            current_font = self.playlist.font()
            self.playlist_font_dialog = FontSelectionDialog(
                current_font, "Playlist Font Selection", self
            )
            self.playlist_font_dialog.font_changed.connect(self._on_playlist_font_changed)
        
        self.playlist_font_dialog.show()
        self.playlist_font_dialog.raise_()
        self.playlist_font_dialog.activateWindow()
    
    def _on_playlist_font_changed(self, font):
        """Handle playlist font change from dialog."""
        self.playlist.setFont(font)
        # Save font settings
        self.settings.setValue("playlistFontFamily", font.family())
        self.settings.setValue("playlistFontSize", font.pointSize())
        # Force refresh
        self.playlist.viewport().update()
    
    def on_set_browser_font(self):
        """Handle 'Set directory browser font' menu action."""
        if self.browser_font_dialog is None or not self.browser_font_dialog.isVisible():
            current_font = self.tree.font()
            self.browser_font_dialog = FontSelectionDialog(
                current_font, "Directory Browser Font Selection", self
            )
            self.browser_font_dialog.font_changed.connect(self._on_browser_font_changed)
        
        self.browser_font_dialog.show()
        self.browser_font_dialog.raise_()
        self.browser_font_dialog.activateWindow()
    
    def _on_browser_font_changed(self, font):
        """Handle browser font change from dialog."""
        self.tree.setFont(font)
        # Save font settings
        self.settings.setValue("browserFontFamily", font.family())
        self.settings.setValue("browserFontSize", font.pointSize())
        # Force refresh
        self.tree.viewport().update()    
    
    def on_adjust_peak_transparency(self):
        """Handle 'Adjust peak indicator transparency' menu action."""
        if self.peak_transparency_dialog is None or not self.peak_transparency_dialog.isVisible():
            current_alpha = self.equalizer.peak_alpha
            self.peak_transparency_dialog = PeakTransparencyDialog(current_alpha, self)
            self.peak_transparency_dialog.transparency_changed.connect(self._on_peak_transparency_changed)
        
        self.peak_transparency_dialog.show()
        self.peak_transparency_dialog.raise_()
        self.peak_transparency_dialog.activateWindow()
    
    def _on_peak_transparency_changed(self, alpha):
        """Handle peak transparency change from dialog."""
        self.equalizer.set_peak_alpha(alpha)
        self.settings.setValue("peakAlpha", alpha)    
    
    def on_choose_peak_color(self):
        """Handle 'Set peak indicator colour' menu action."""
        color = QColorDialog.getColor()
        if color.isValid():
            self.equalizer.set_peak_color(color)
            self.settings.setValue("peakColor", color.name())
            self.reset_peak_color_act.setEnabled(True)
            self.statusBar().showMessage(f"Peak indicator colour set to {color.name()}", 3000)
                
    def on_reset_peak_color(self):
        """Handle 'Reset peak indicator colour' menu action."""
        reply = QMessageBox.question(
            self, "Reset Peak Indicator Colour",
            "Reset peak indicator colour to automatic?\n\n"
            "The peaks will automatically complement the accent colour.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.equalizer.reset_peak_color()
            self.settings.remove("peakColor")
            self.reset_peak_color_act.setEnabled(False)    
    
    def on_add_folder_clicked(self):
        """Handle 'Open folder' menu action."""
        folder = QFileDialog.getExistingDirectory(
            self, "Choose music folder", QDir.homePath()
        )
        if not folder:
            return
        
        files = self._get_audio_files_from_directory(folder)
        if files:
            self.playlist_model.add_tracks(files, clear=True)
            if self.playlist_model.rowCount() > 0:
                self.controller.play_index(0)
        
        self.update_playback_ui()

    def on_choose_default_folder(self):
        """Handle 'Choose default folder' menu action."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select default music folder", QDir.rootPath()
        )
        if folder:
            self.settings.setValue("default_dir", folder)
            self.fs_model.setRootPath(folder)
            self.tree.setRootIndex(self.fs_model.index(folder))
            self.statusBar().showMessage(f"Default folder set to {folder}", 3000)
            self.update_reset_action_state()

    def on_reset_default_folder(self):
        """Handle 'Reset default folder' menu action."""
        if not self.settings.contains("default_dir"):
            self.statusBar().showMessage("No default folder set", 3000)
            return
        
        reply = QMessageBox.question(
            self, "Reset Default Folder",
            "Are you sure you want to reset the default folder?\n\n"
            "This will revert the browser to showing all drives.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.settings.remove("default_dir")
            root = QDir.rootPath()
            self.fs_model.setRootPath(root)
            self.tree.setRootIndex(self.fs_model.index(root))
            self.statusBar().showMessage("Default folder reset – showing all drives", 3000)
            self.update_reset_action_state()

    def on_choose_highlight_color(self):
        """Handle 'Set accent colour' menu action."""
        color = QColorDialog.getColor()
        if color.isValid():
            self.playlist_model.highlight_color = color
            self.tree_delegate.set_highlight_color(color)
            self.update_tree_stylesheet(color)
            self.settings.setValue("highlightColor", color.name())
            self.update_playback_ui()
            self.update_slider_colors()
            self.equalizer.update_color(color)
    # HELPER METHODS
    
    def _get_audio_files_from_directory(self, directory):
        """Get sorted list of audio files from directory."""
        files = []
        try:
            for name in sorted(os.listdir(directory)):
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS:
                        files.append(path)
        except PermissionError:
            self.statusBar().showMessage("Permission denied for this folder", 3000)
        return files

    # SETTINGS PERSISTENCE
    
    def restore_settings(self):
        """Restore saved settings from previous session."""
        # Restore highlight color
        color_name = self.settings.value("highlightColor")
        if color_name:
            color = QColor(color_name)
            if color.isValid():
                self.playlist_model.highlight_color = color
                self.tree_delegate.set_highlight_color(color)
                self.update_tree_stylesheet(color)
                self.update_slider_colors()
                self.equalizer.update_color(color)
        
        # Restore peak indicator color
        peak_color_name = self.settings.value("peakColor")
        if peak_color_name:
            peak_color = QColor(peak_color_name)
            if peak_color.isValid():
                self.equalizer.set_peak_color(peak_color)
                self.reset_peak_color_act.setEnabled(True)
        else:
            self.reset_peak_color_act.setEnabled(False)
            
                    # Restore peak indicator color
        peak_color_name = self.settings.value("peakColor")
        if peak_color_name:
            peak_color = QColor(peak_color_name)
            if peak_color.isValid():
                self.equalizer.set_peak_color(peak_color)
                self.reset_peak_color_act.setEnabled(True)
        else:
            self.reset_peak_color_act.setEnabled(False)
        
        # Restore peak indicator transparency
        if self.settings.contains("peakAlpha"):
            peak_alpha = int(self.settings.value("peakAlpha"))
            self.equalizer.set_peak_alpha(peak_alpha)

        # Restore playlist font
        if self.settings.contains("playlistFontFamily"):
            font_family = self.settings.value("playlistFontFamily")
            font_size = int(self.settings.value("playlistFontSize", 10))
            playlist_font = QFont(font_family, font_size)
            self.playlist.setFont(playlist_font)
        
        # Restore directory browser font
        if self.settings.contains("browserFontFamily"):
            font_family = self.settings.value("browserFontFamily")
            font_size = int(self.settings.value("browserFontSize", 10))
            browser_font = QFont(font_family, font_size)
            self.tree.setFont(browser_font)

        # Restore window geometry and state
        if self.settings.contains("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        if self.settings.contains("leftSplitterState"):
            self.left_splitter.restoreState(self.settings.value("leftSplitterState"))
        if self.settings.contains("windowState"):
            self.restoreState(self.settings.value("windowState"))
        if self.settings.contains("splitterState"):
            self.splitter.restoreState(self.settings.value("splitterState"))
        if self.settings.contains("playlistHeader"):
            self.playlist.horizontalHeader().restoreState(
                self.settings.value("playlistHeader")
            )
        
        # Restore volume
        if self.settings.contains("volume"):
            vol = int(self.settings.value("volume"))
            self.slider_vol.setValue(vol)
            self.on_volume_changed(vol)

    def closeEvent(self, event):
        """Save settings on application close."""
        # Cleanup global media key handler
        if self.global_media_handler:
            self.global_media_handler.cleanup()
            if sys.platform == 'win32':
                QApplication.instance().removeNativeEventFilter(self.global_media_handler)
        
        # Save settings
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("leftSplitterState", self.left_splitter.saveState())
        self.settings.setValue("windowState", self.saveState())
        self.settings.setValue("splitterState", self.splitter.saveState())
        self.settings.setValue("playlistHeader", 
                               self.playlist.horizontalHeader().saveState())
        self.settings.setValue("volume", self.slider_vol.value())
        super().closeEvent(event)

# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================

def main():
    """Main application entry point."""
    # Windows taskbar icon fix
    if sys.platform == 'win32':
        try:
            myappid = u"litheplayer.audio.app"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("Lithe Player")
    app.setWindowIcon(QIcon(get_asset_path("icon.ico")))

    # Create splash screen
    splash_pix = QPixmap(get_asset_path("splash.png"))
    splash = QSplashScreen(splash_pix)
    splash.show()
    app.processEvents()

    # Create main window
    window = MainWindow()

    # Show main window after splash delay
    QTimer.singleShot(SPLASH_SCREEN_DURATION_MS, 
                     lambda: (splash.finish(window), window.show()))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()             