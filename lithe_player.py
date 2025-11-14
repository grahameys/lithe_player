"""
Lithe Player - A modern audio player with FFT equalizer visualization.

Features:
- Gapless audio playback using dual VLC players
- Visual FFT-based equalizer with customizable colors
- Playlist management with drag-and-drop
- File browser with album art display
- Customizable accent colors and fonts
- Global media key support (Windows)

Author: grahameys
"""

import sys
import os
import json
import base64
import threading
import time
import ctypes
from enum import Enum
from pathlib import Path
from collections import deque

import numpy as np
import vlc
import soundfile as sf
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

from PySide6.QtCore import (
    Qt, QDir, QAbstractTableModel, QAbstractItemModel, QModelIndex, QSize, QTimer, 
    QRect, QEvent, QAbstractNativeEventFilter, QObject, Signal, QThread,
    QByteArray, QUrl, QMimeData, QRectF
)
from PySide6.QtGui import (
    QAction, QFont, QColor, QIcon, QPalette, QPixmap, QPainter, 
    QKeySequence, QShortcut, QImage, QRegion, QPainterPath, QDrag, QBrush
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QTreeView, QTableView,
    QVBoxLayout, QHBoxLayout, QPushButton, QFileSystemModel, QHeaderView,
    QLabel, QSlider, QFileDialog, QMessageBox, QColorDialog,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QSizePolicy, QStyleOption,
    QAbstractItemView, QSplashScreen, QMenu, QFontComboBox, QFileIconProvider,
    QLineEdit
)
from PySide6 import QtSvg
from PySide6.QtSvg import QSvgRenderer

if sys.platform == 'win32':
    from ctypes import wintypes
    import ctypes.wintypes

# ============================================================================
# CONFIGURATION
# ============================================================================

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg'}
DEFAULT_ANALYSIS_RATE = 44100
ANALYSIS_CHUNK_SAMPLES = 2048
PROGRESS_UPDATE_INTERVAL_MS = 500
EQUALIZER_UPDATE_INTERVAL_MS = 30
SPLASH_SCREEN_DURATION_MS = 1500

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_asset_path(filename):
    """Get absolute path to asset file (works for dev and PyInstaller)."""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, 'assets', filename)

def is_dark_color(color: QColor) -> bool:
    """Determine if a color is dark based on perceived brightness."""
    brightness = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return brightness < 128

def get_themed_icon(filename):
    """Get theme-aware icon by modifying SVG colors based on system theme."""
    svg_path = get_asset_path(filename)
    
    try:
        with open(svg_path, 'r', encoding='utf-8') as f:
            svg_content = f.read()
    except:
        return QIcon(svg_path)
    
    app = QApplication.instance()
    use_light_icon = False
    if app:
        palette = app.palette()
        base_color = palette.color(QPalette.Base)
        use_light_icon = is_dark_color(base_color)
    
    if use_light_icon:
        svg_content = svg_content.replace('stroke="#1C274C"', 'stroke="#FFFFFF"')
        svg_content = svg_content.replace('fill="#1C274C"', 'fill="#FFFFFF"')
        svg_content = svg_content.replace('stroke="#000000"', 'stroke="#FFFFFF"')
        svg_content = svg_content.replace('fill="#000000"', 'fill="#FFFFFF"')
        svg_content = svg_content.replace('stroke="#000"', 'stroke="#FFF"')
        svg_content = svg_content.replace('fill="#000"', 'fill="#FFF"')
    
    renderer = QSvgRenderer(QByteArray(svg_content.encode('utf-8')))
    image = QImage(48, 48, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    
    return QIcon(QPixmap.fromImage(image))

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
    metadata_trackno = None
    file_format = ""
    bitrate = ""
    
    try:
        audio = MutagenFile(path, easy=True)
        if audio:
            title = audio.get("title", [title])[0]
            artist = audio.get("artist", [""])[0]
            album = audio.get("album", [""])[0]
            year = audio.get("date", audio.get("year", [""]))[0]
            
            # Extract track number from metadata
            tracknumber = audio.get("tracknumber", [""])[0]
            if tracknumber:
                # Handle formats like "5/12" or "5"
                if '/' in tracknumber:
                    tracknumber = tracknumber.split('/')[0]
                try:
                    metadata_trackno = int(tracknumber)
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    
    # Extract format and bitrate
    try:
        ext = os.path.splitext(path)[1].lower()
        file_format = ext.lstrip('.').upper()
        
        # Try to get bitrate information
        try:
            if path.lower().endswith(".mp3"):
                audio = MP3(path)
                if audio.info.bitrate:
                    bitrate = f"{audio.info.bitrate // 1000} kbps"
            elif path.lower().endswith(".flac"):
                audio = FLAC(path)
                if audio.info.bitrate:
                    bitrate = f"{audio.info.bitrate // 1000} kbps"
            elif path.lower().endswith((".m4a", ".mp4", ".aac")):
                audio = MP4(path)
                if audio.info.bitrate:
                    bitrate = f"{audio.info.bitrate // 1000} kbps"
        except Exception:
            pass
    except Exception:
        pass
    
    return {
        "trackno": metadata_trackno if metadata_trackno is not None else trackno,
        "title": title,
        "artist": artist,
        "album": album,
        "year": year,
        "path": path,
        "format": file_format,
        "bitrate": bitrate
    }

# ============================================================================
# JSON SETTINGS MANAGER
# ============================================================================

class JsonSettings:
    """Cross-platform JSON-based settings manager compatible with QSettings API."""
    
    def __init__(self, config_name="config.json"):
        self.config_path = Path(__file__).parent / config_name
        self._settings = {}
        self._load()
    
    def _load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._settings = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not load settings: {e}")
                self._settings = {}
    
    def _save(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Warning: Could not save settings: {e}")
    
    def value(self, key, default=None):
        value = self._settings.get(key, default)
        if isinstance(value, str) and value.startswith("base64:"):
            try:
                decoded = base64.b64decode(value[7:])
                return QByteArray(decoded)
            except Exception:
                return default
        return value
    
    def setValue(self, key, value):
        if hasattr(value, 'toBase64'):
            value = "base64:" + value.toBase64().data().decode('utf-8')
        self._settings[key] = value
        self._save()
    
    def allKeys(self):
        return list(self._settings.keys())
    
    def fileName(self):
        return str(self.config_path)
    
    def contains(self, key):
        return key in self._settings
    
    def remove(self, key):
        if key in self._settings:
            del self._settings[key]
            self._save()

# ============================================================================
# CUSTOM FILE ICON PROVIDER
# ============================================================================

class CustomFileIconProvider(QFileIconProvider):
    """Custom icon provider for file browser with theme-aware icons."""
    
    def __init__(self):
        super().__init__()
        self._update_icons()
    
    def _update_icons(self):
        """Load icons based on current theme."""
        app = QApplication.instance()
        if app:
            palette = app.palette()
            base_color = palette.color(QPalette.Base)
            is_dark = is_dark_color(base_color)
        else:
            is_dark = False
        
        # Load appropriate icons based on theme
        if is_dark:
            self.dir_icon = QIcon(get_asset_path("dirwhite.svg"))
            self.file_icon = QIcon(get_asset_path("filewhite.svg"))
        else:
            self.dir_icon = QIcon(get_asset_path("dir.svg"))
            self.file_icon = QIcon(get_asset_path("file.svg"))
    
    def icon(self, type_or_info):
        """Return custom icon for directories and audio files."""
        # Handle QFileInfo parameter
        if hasattr(type_or_info, 'isDir'):
            file_info = type_or_info
            
            # Directory icon
            if file_info.isDir():
                return self.dir_icon
            
            # Audio file icon
            suffix = file_info.suffix().lower()
            if suffix in ['mp3', 'flac', 'wav', 'ogg', 'opus', 'aac']:
                return self.file_icon
        
        # Fall back to default icons for other file types
        return super().icon(type_or_info)
    
    def update_theme(self):
        """Update icons when theme changes."""
        self._update_icons()

# ============================================================================
# VLC SETUP
# ============================================================================

def setup_vlc_environment():
    """Configure VLC environment (prefers system-wide VLC)."""
    try:
        test_instance = vlc.Instance()
        print("✓ System-wide VLC installation detected")
        return None
    except Exception:
        print("System-wide VLC not available, checking for local plugins...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        plugins_dir = os.path.join(script_dir, "plugins")
        
        if not os.path.exists(plugins_dir):
            print("⚠ Warning: No VLC found. Please install VLC.")
            return None
        
        os.environ['VLC_PLUGIN_PATH'] = plugins_dir
        if sys.platform == 'win32':
            os.environ['PATH'] = plugins_dir + os.pathsep + os.environ.get('PATH', '')
        
        print(f"Using local VLC plugins from: {plugins_dir}")
        return plugins_dir

# ============================================================================
# GAPLESS PLAYBACK
# ============================================================================

class PlayerState(Enum):
    """Player slot states for gapless playback."""
    IDLE = 0
    LOADING = 1
    READY = 2
    PLAYING = 3
    FINISHING = 4

class GaplessSignals(QObject):
    """Qt signals for thread-safe communication."""
    track_changed = Signal(str)
    start_equalizer = Signal(str)
    stop_equalizer = Signal()
    pause_equalizer = Signal()
    resume_equalizer = Signal(str)

class GaplessPlaybackManager:
    """Manages dual VLC players for true gapless multiformat playback."""
    
    def __init__(self, vlc_instance, eq_widget=None):
        self.instance = vlc_instance
        self.eq_widget = eq_widget
        self.signals = GaplessSignals()
        
        # Create two players for alternating playback
        self.player_a = self.instance.media_player_new()
        self.player_b = self.instance.media_player_new()
        
        self.active_player = None
        self.standby_player = None
        self.player_a_state = PlayerState.IDLE
        self.player_b_state = PlayerState.IDLE
        
        self._current_volume = 70
        self.preload_lock = threading.Lock()
        self.next_track_path = None
        self.current_track_path = None
        
        self.transition_threshold_ms = 500
        self.monitoring = False
        self.monitor_thread = None
        self._stop_monitoring = threading.Event()
        self._transition_triggered = False
        
    def setup_events(self):
        """Setup event handlers for both players."""
        em_a = self.player_a.event_manager()
        em_a.event_attach(vlc.EventType.MediaPlayerEndReached, 
                         lambda e: self._on_player_end(self.player_a, 'A'))
        
        em_b = self.player_b.event_manager()
        em_b.event_attach(vlc.EventType.MediaPlayerEndReached,
                         lambda e: self._on_player_end(self.player_b, 'B'))
    
    def play_track(self, filepath, preload_next=None):
        """Play a track with optional preloading of next track."""
        self._transition_triggered = False
        
        # Stop the monitoring thread to prevent stale signals if a track is playing
        if self.active_player and self.active_player.is_playing():
            # Don't stop equalizer here - let start() handle the transition
            # Stopping and immediately starting causes timer race conditions
            # Stop the monitoring thread to prevent stale signals
            self.monitoring = False
            self._stop_monitoring.set()
        
        if self.active_player is None:
            self.active_player = self.player_a
            self.standby_player = self.player_b
            self.player_a_state = PlayerState.LOADING
            media = self.instance.media_new(filepath)
            self.active_player.set_media(media)
            self.active_player.audio_set_volume(self._current_volume)
            
        elif self.standby_player and self._is_preloaded(filepath):
            print(f"Using preloaded track: {os.path.basename(filepath)}")
            self.active_player, self.standby_player = self.standby_player, self.active_player
            self._update_states_after_swap()
        else:
            print(f"Loading track without preload: {os.path.basename(filepath)}")
            if self.active_player == self.player_a:
                self.active_player = self.player_b
                self.standby_player = self.player_a
                self.player_b_state = PlayerState.LOADING
            else:
                self.active_player = self.player_a
                self.standby_player = self.player_b
                self.player_a_state = PlayerState.LOADING
            
            media = self.instance.media_new(filepath)
            self.active_player.set_media(media)
            self.active_player.audio_set_volume(self._current_volume)
        
        self.current_track_path = filepath
        self.active_player.play()
        
        self._start_monitoring()
        
        if preload_next:
            threading.Thread(target=self._preload_next_track, 
                           args=(preload_next,), daemon=True).start()
        
        self.signals.start_equalizer.emit(filepath)
    
    def _is_preloaded(self, filepath):
        return self.next_track_path == filepath and self.standby_player is not None
    
    def _update_states_after_swap(self):
        if self.active_player == self.player_a:
            self.player_a_state = PlayerState.PLAYING
            self.player_b_state = PlayerState.IDLE
        else:
            self.player_b_state = PlayerState.PLAYING
            self.player_a_state = PlayerState.IDLE
    
    def _preload_next_track(self, filepath):
        """Preload the next track into standby player."""
        if not self.standby_player or self.current_track_path == filepath:
            return
        
        if self.next_track_path == filepath:
            print(f"✓ Track already preloaded: {os.path.basename(filepath)}")
            return
        
        try:
            print(f"⏳ Preloading: {os.path.basename(filepath)}")
            media = self.instance.media_new(filepath)
            
            with self.preload_lock:
                self.standby_player.set_media(media)
                self.standby_player.audio_set_volume(self._current_volume)
                
                if self.standby_player == self.player_a:
                    self.player_a_state = PlayerState.READY
                else:
                    self.player_b_state = PlayerState.READY
                
                self.next_track_path = filepath
                print(f"✓ Preloaded next track: {os.path.basename(filepath)}")
        except Exception as e:
            print(f"❌ Error preloading track: {e}")
    
    def _start_monitoring(self):
        if not self.monitoring:
            self.monitoring = True
            self._stop_monitoring.clear()
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
    
    def _monitor_loop(self):
        """Monitor playback position and trigger gapless transition."""
        while not self._stop_monitoring.is_set():
            try:
                if self.active_player and self.active_player.is_playing():
                    length = self.active_player.get_length()
                    current = self.active_player.get_time()
                    
                    if length > 0 and current > 0:
                        remaining = length - current
                        
                        if (remaining <= self.transition_threshold_ms and 
                            remaining > 0 and 
                            not self._transition_triggered):
                            
                            if self.standby_player and self.next_track_path:
                                self._transition_triggered = True
                                self._trigger_gapless_transition()
            except Exception as e:
                print(f"Monitor loop error: {e}")
            
            time.sleep(0.02)
    
    def _trigger_gapless_transition(self):
        """Trigger gapless transition to preloaded track."""
        try:
            with self.preload_lock:
                if not self.next_track_path or not self.standby_player:
                    self._transition_triggered = False
                    return
                
                print(f"🎵 Gapless transition to: {os.path.basename(self.next_track_path)}")
                
                old_player = self.active_player
                new_player = self.standby_player
                
                new_player.audio_set_volume(self._current_volume)
                new_player.play()
                time.sleep(0.01)
                
                if not new_player.is_playing():
                    new_player.play()
                    time.sleep(0.02)
                    if not new_player.is_playing():
                        print("❌ ERROR: Failed to start new player!")
                        self._transition_triggered = False
                        return
                
                self.active_player = new_player
                self.standby_player = old_player
                
                old_track = self.current_track_path
                self.current_track_path = self.next_track_path
                self.next_track_path = None
                self._update_states_after_swap()
                
                self.signals.track_changed.emit(self.current_track_path)
                self.signals.start_equalizer.emit(self.current_track_path)
                
                old_player.audio_set_volume(self._current_volume)
                old_player.stop()
                
                print(f"✓ Transition complete: {os.path.basename(old_track)} -> {os.path.basename(self.current_track_path)}")
                    
        except Exception as e:
            print(f"Gapless transition error: {e}")
            self._transition_triggered = False
    
    def _on_player_end(self, player, name):
        """Handle player end reached event."""
        if player != self.active_player or self._transition_triggered:
            return
        
        if not self.next_track_path:
            self._transition_triggered = True
            self._stop_monitoring.set()
            self.monitoring = False
            self.signals.stop_equalizer.emit()
            return
        
        self._transition_triggered = True
        print("WARNING: Gapless transition didn't fire, using fallback")
        
        if self.next_track_path and self.standby_player:
            with self.preload_lock:
                old_player = self.active_player
                new_player = self.standby_player
                track_to_play = self.next_track_path
                
                new_player.audio_set_volume(self._current_volume)
                new_player.play()
                time.sleep(0.05)
                
                if new_player.is_playing():
                    self.active_player = new_player
                    self.standby_player = old_player
                    old_player.stop()
                    self.current_track_path = track_to_play
                    self._update_states_after_swap()
                    self.signals.track_changed.emit(self.current_track_path)
                    self.signals.start_equalizer.emit(self.current_track_path)
                else:
                    self._transition_triggered = False
            
    def pause(self):
        if self.active_player and self.active_player.is_playing():
            self.active_player.pause()
            self.signals.pause_equalizer.emit()
    
    def resume(self):
        if self.active_player and self.current_track_path:
            if not self.active_player.is_playing():
                self.active_player.play()
                self._start_monitoring()
                self.signals.resume_equalizer.emit(self.current_track_path)
    
    def stop(self):
        self.monitoring = False
        self._stop_monitoring.set()
        self.signals.stop_equalizer.emit()
        
        try:
            if self.active_player:
                self.active_player.stop()
            if self.standby_player:
                self.standby_player.stop()
        except Exception as e:
            print(f"Error stopping players: {e}")
        
        self.active_player = None
        self.standby_player = None
        self.player_a_state = PlayerState.IDLE
        self.player_b_state = PlayerState.IDLE
        self.current_track_path = None
        self.next_track_path = None
        self._transition_triggered = False
    
    def is_playing(self):
        return self.active_player and self.active_player.is_playing()
    
    def get_active_player(self):
        return self.active_player
    
    def set_volume(self, volume):
        self._current_volume = volume
        self.player_a.audio_set_volume(volume)
        self.player_b.audio_set_volume(volume)
    
    def get_time(self):
        return self.active_player.get_time() if self.active_player else 0
    
    def get_length(self):
        return self.active_player.get_length() if self.active_player else 0
    
    def set_time(self, time_ms):
        if self.active_player:
            self.active_player.set_time(time_ms)

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
        self.target_levels = [0] * bar_count
        self.peak_hold = [0] * bar_count
        self.peak_hold_time = [0] * bar_count
        self.velocity = [0] * bar_count
        self.color = QColor("#00cc66")
        self.custom_peak_color = None
        self.peak_alpha = 255
        self.buffer_size = ANALYSIS_CHUNK_SAMPLES
        self.sample_buffer = np.zeros(self.buffer_size, dtype=np.float32)
        self._band_ema_max = [1e-6] * bar_count
        self._decoder_thread = None
        self._decoder_running = False
        self._stop_decoder = threading.Event()
        self._decoder_generation = 0  # Track which decoder instance is current
        self._pending_filepath = None
        self._pending_generation = 0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_from_fft)

    def set_peak_color(self, color: QColor):
        if color and color.isValid():
            self.custom_peak_color = color
            self.update()
    
    def reset_peak_color(self):
        self.custom_peak_color = None
        self.update()
        
    def set_peak_alpha(self, alpha: int):
        self.peak_alpha = max(0, min(255, alpha))
        self.update()

    def start(self, filepath):
        # Store filepath for deferred start
        self._pending_filepath = filepath
        self._pending_generation = self._decoder_generation + 1
        
        # Use QTimer to defer the actual decoder creation
        # This ensures the Qt event loop has processed everything before we start the new decoder
        QTimer.singleShot(50, self._deferred_start)
    
    def _deferred_start(self):
        """Actually start the decoder after a short delay to avoid Qt event loop issues."""
        filepath = self._pending_filepath
        
        # Increment generation to invalidate old decoder thread
        self._decoder_generation = self._pending_generation
        current_generation = self._decoder_generation
        
        # Signal old decoder to stop (it will exit on its own when it sees generation changed)
        if self._decoder_running:
            self._decoder_running = False
            self._stop_decoder.set()
        
        # Create and start new decoder thread immediately
        # Don't wait for old thread - it will exit on its own (daemon thread)
        self._stop_decoder.clear()
        self._decoder_running = True
        self._decoder_thread = threading.Thread(
            target=self._decode_loop, args=(filepath, current_generation), daemon=True
        )
        self._decoder_thread.start()
        
        # Always restart the timer to ensure it continues working
        if self.timer.isActive():
            self.timer.stop()
        self.timer.start(EQUALIZER_UPDATE_INTERVAL_MS)
    
    def _restart_timer(self):
        """Restart the timer (not used anymore, kept for compatibility)."""
        pass

    def pause(self):
        """Pause equalizer - freeze display without clearing."""
        self.timer.stop()
    
    def resume(self, filepath):
        """Resume equalizer - restart timer without changing decoder."""
        if not self.timer.isActive():
            self.timer.start(EQUALIZER_UPDATE_INTERVAL_MS)

    def stop(self, clear_display=True):
        # Don't increment generation - let start() handle that
        # Just set flags for cleanup
        self._decoder_running = False
        self._stop_decoder.set()
        self._decoder_thread = None
        
        if QThread.currentThread() == QApplication.instance().thread():
            self.timer.stop()
            if clear_display:
                self._clear_display()
        else:
            QTimer.singleShot(0, lambda: self._stop_on_main_thread(clear_display))
    
    def _stop_on_main_thread(self, clear_display):
        self.timer.stop()
        if clear_display:
            self._clear_display()
    
    def _clear_display(self):
        self.levels = [0] * self.bar_count
        self.target_levels = [0] * self.bar_count
        self.peak_hold = [0] * self.bar_count
        self.peak_hold_time = [0] * self.bar_count
        self.velocity = [0] * self.bar_count
        self.update()

    def _decode_loop(self, filepath, generation):
        try:
            with sf.SoundFile(filepath) as f:
                # Only check generation - don't check _stop_decoder to avoid race conditions
                while self._decoder_generation == generation:
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
                    
                    sleep_time = len(samples) / DEFAULT_ANALYSIS_RATE
                    elapsed = 0
                    while elapsed < sleep_time and self._decoder_generation == generation:
                        time.sleep(0.01)
                        elapsed += 0.01
                        
        except Exception as e:
            print(f"Decoder thread error: {e}")

    def update_from_fft(self):
        try:
            # Use generation number to validate decoder, not thread object
            # This avoids race conditions during thread transitions
            current_gen = self._decoder_generation
            if current_gen == 0:
                return
            
            # Check if decoder is running by verifying the thread exists and matches generation
            thread_obj = self._decoder_thread
            if not thread_obj:
                return
            
            # The thread must be alive
            if not thread_obj.is_alive():
                return
            
            fft = np.fft.rfft(self.sample_buffer * np.hanning(len(self.sample_buffer)))
            magnitude = np.abs(fft)
            freqs_hz = np.fft.rfftfreq(len(self.sample_buffer), 1.0 / DEFAULT_ANALYSIS_RATE)
            mask = (freqs_hz >= 60) & (freqs_hz <= 17000)
            magnitude = magnitude[mask]

            bars_raw = self._calculate_bar_values(magnitude)
            bars_norm = self._normalize_bars(bars_raw)
            self.target_levels = [max(0, min(self.segments, v * 0.82)) for v in bars_norm]
            
            self._smooth_levels_with_gravity()
            
            self.update()
        except Exception as e:
            print(f"Error in update_from_fft: {e}")

    def _smooth_levels_with_gravity(self):
        gravity = 0.4
        peak_hold_frames = 12
        
        for i in range(self.bar_count):
            target = self.target_levels[i]
            current = self.levels[i]
            
            if target > current:
                self.levels[i] = current + (target - current) * 0.95
                self.velocity[i] = 0
                if self.levels[i] > self.peak_hold[i]:
                    self.peak_hold[i] = self.levels[i]
                    self.peak_hold_time[i] = peak_hold_frames
            else:
                self.velocity[i] += gravity
                self.levels[i] = max(target, current - self.velocity[i])
                if self.levels[i] <= target:
                    self.levels[i] = target
                    self.velocity[i] = 0
            
            if self.peak_hold_time[i] > 0:
                self.peak_hold_time[i] -= 1
            else:
                if self.peak_hold[i] > self.levels[i]:
                    self.peak_hold[i] = max(self.levels[i], self.peak_hold[i] - 0.5)
                else:
                    self.peak_hold[i] = self.levels[i]

    def _calculate_bar_values(self, magnitude):
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
        decay = 0.95
        eps = 1e-6
        bars_norm = []
        
        for i, val in enumerate(bars_raw):
            ema_candidate = self._band_ema_max[i] * decay
            self._band_ema_max[i] = max(val, ema_candidate)
            norm = val / (self._band_ema_max[i] + eps)
            hf_tilt = 1.0 + 0.22 * (i / max(1, self.bar_count - 1))
            norm *= hf_tilt
            if i < 2:
                norm *= 1.18
            scaled = norm * (self.segments * 0.87)
            bars_norm.append(int(scaled))
        return bars_norm

    def update_color(self, color: QColor):
        if color:
            self.color = color
            self.update()

    def paintEvent(self, event):
        try:
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
                
                # Draw peak hold indicator
                if self.peak_hold[i] > 0:
                    peak_seg = int(self.peak_hold[i])
                    if peak_seg < self.segments:
                        peak_color = self._get_peak_color()
                        peak_color.setAlpha(self.peak_alpha)
                        
                        peak_rect = QRect(
                            int(i * bar_width),
                            int(self.height() - (peak_seg + 1) * segment_height),
                            int(bar_width * 0.85),
                            int(segment_height * 0.4)
                        )
                        painter.fillRect(peak_rect, peak_color)
        except Exception as e:
            print(f"Error in paintEvent: {e}")
            import traceback
            traceback.print_exc()
    
    def _get_peak_color(self):
        if self.custom_peak_color:
            return self.custom_peak_color
        
        h, s, v, a = self.color.getHsv()
        r, g, b = self.color.red(), self.color.green(), self.color.blue()
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        
        if luminance < 128:
            return QColor.fromHsv(h, max(180, s), min(255, v + 120))
        elif luminance > 180:
            return QColor.fromHsv(h, min(255, s + 80), max(150, v - 30))
        else:
            new_hue = (h + 20) % 360
            return QColor.fromHsv(new_hue, min(255, s + 60), min(255, v + 60))

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
        if not index.isValid():
            return None
        
        track = self._tracks[index.row()]
        col = index.column()
        
        if role == Qt.DisplayRole:
            if col == 0:
                return f"{track.get('trackno', index.row() + 1):02d}"
            elif col == 1:
                return track.get("title", "")
            elif col == 2:
                return track.get("artist", "")
            elif col == 3:
                return track.get("album", "")
            elif col == 4:
                return track.get("year", "")
        elif role == Qt.FontRole and index.row() == self.current_index:
            font = QFont()
            font.setBold(True)
            return font
        elif role == Qt.DecorationRole and col == 1 and index.row() == self.current_index:
            return self._get_playback_icon()
           
        return None

    def _get_playback_icon(self):
        if not self.controller:
            return None
        
        use_white_icon = False
        app = QApplication.instance()
        if app:
            base_color = app.palette().color(QPalette.Base)
            use_white_icon = is_dark_color(base_color)
        
        if self.highlight_color:
            use_white_icon = is_dark_color(self.highlight_color)
        
        is_playing = self.controller.player.is_playing()
        
        if is_playing:
            return self.icons.get("row_play_white" if use_white_icon else "row_play")
        else:
            return self.icons.get("row_pause_white" if use_white_icon else "row_pause")

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def add_tracks(self, paths, clear=False):
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
        if self._tracks:
            self.beginRemoveRows(QModelIndex(), 0, len(self._tracks) - 1)
            self._tracks.clear()
            self.endRemoveRows()
            self.set_current_index(-1)

    def path_at(self, row):
        return self._tracks[row]["path"] if 0 <= row < len(self._tracks) else None
    
    def get_filepath(self, row):
        """Get the file path for a track at the given row."""
        return self.path_at(row)

    def set_current_index(self, row):
        if self.current_index == row:
            return
        self.current_index = row
        if self.rowCount() > 0:
            top_left = self.index(0, 0)
            bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)
    
    def supportedDropActions(self):
        return Qt.MoveAction | Qt.CopyAction
    
    def flags(self, index):
        default_flags = super().flags(index)
        if index.isValid():
            return default_flags | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
        return default_flags | Qt.ItemIsDropEnabled
    
    def mimeTypes(self):
        return ['application/x-playlist-track-index', 'text/uri-list']
    
    def mimeData(self, indexes):
        mime_data = QMimeData()
        rows = sorted(set(index.row() for index in indexes if index.isValid()))
        if rows:
            mime_data.setData('application/x-playlist-track-index', str(rows[0]).encode())
        return mime_data
    
    def canDropMimeData(self, data, action, row, column, parent):
        if data.hasFormat('application/x-playlist-track-index'):
            return True
        if data.hasUrls():
            return True
        return False
    
    def dropMimeData(self, data, action, row, column, parent):
        if data.hasFormat('application/x-playlist-track-index'):
            source_row = int(data.data('application/x-playlist-track-index').data().decode())
            if row == -1:
                target_row = parent.row() if parent.isValid() else self.rowCount()
            else:
                target_row = row
            
            if source_row == target_row or source_row == target_row - 1:
                return False
            
            self.moveRow(QModelIndex(), source_row, QModelIndex(), target_row)
            return True
        return False
    
    def moveRow(self, sourceParent, sourceRow, destinationParent, destinationRow):
        if sourceRow < 0 or sourceRow >= len(self._tracks):
            return False
        
        actual_dest = destinationRow - 1 if destinationRow > sourceRow else destinationRow
        
        if actual_dest < 0 or actual_dest > len(self._tracks):
            return False
        
        self.beginMoveRows(sourceParent, sourceRow, sourceRow, destinationParent, destinationRow)
        track = self._tracks.pop(sourceRow)
        self._tracks.insert(actual_dest, track)
        
        if self.current_index == sourceRow:
            self.current_index = actual_dest
        elif sourceRow < self.current_index <= actual_dest:
            self.current_index -= 1
        elif actual_dest <= self.current_index < sourceRow:
            self.current_index += 1
        
        self.endMoveRows()
        
        if self.controller:
            self.controller.refresh_preload()
        return True

# ============================================================================
# AUDIO PLAYER CONTROLLER
# ============================================================================

class AudioPlayerController:
    """Controller for gapless audio playback."""

    def __init__(self, view=None, eq_widget=None):
        plugins_dir = setup_vlc_environment()
        
        vlc_options = [
            '--quiet',
            '--no-video-title-show',
            '--no-stats',
            '--no-snapshot-preview',
            '--ignore-config',
            '--no-plugins-cache',
            '--verbose=0',
        ]
        
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
            raise
        
        self.gapless_manager = GaplessPlaybackManager(self.instance, eq_widget)
        self.gapless_manager.setup_events()
        self.gapless_manager.signals.track_changed.connect(self._on_gapless_track_change, Qt.QueuedConnection)
        self.gapless_manager.signals.start_equalizer.connect(self._start_equalizer, Qt.QueuedConnection)
        self.gapless_manager.signals.stop_equalizer.connect(self._stop_equalizer, Qt.QueuedConnection)
        self.gapless_manager.signals.pause_equalizer.connect(self._pause_equalizer, Qt.QueuedConnection)
        self.gapless_manager.signals.resume_equalizer.connect(self._resume_equalizer, Qt.QueuedConnection)
        
        self.player = self.gapless_manager.player_a
        self.current_index = -1
        self.model = None
        self.view = view
        self.eq_widget = eq_widget

    def _start_equalizer(self, filepath):
        if self.eq_widget:
            self.eq_widget.start(filepath)

    def _stop_equalizer(self):
        if self.eq_widget:
            self.eq_widget.stop()
    
    def _pause_equalizer(self):
        if self.eq_widget:
            self.eq_widget.pause()
    
    def _resume_equalizer(self, filepath):
        if self.eq_widget:
            self.eq_widget.resume(filepath)

    def _on_gapless_track_change(self, filepath):
        self.gapless_manager._transition_triggered = False
        
        if self.model:
            for i in range(self.model.rowCount()):
                if self.model.path_at(i) == filepath:
                    self.current_index = i
                    self.model.set_current_index(i)
                    self.player = self.gapless_manager.get_active_player()
                    
                    if self.view:
                        self.view.clearSelection()
                        self.view.selectRow(i)
                        self.view.viewport().update()
                    
                    main_window = self.view.window()
                    if hasattr(main_window, "update_album_art"):
                        main_window.update_album_art(filepath)
                    
                    if main_window:
                        track = self.model._tracks[i]
                        artist = track.get("artist", "Unknown Artist")
                        title = track.get("title", "Unknown Track")
                        format_str = track.get("format", "")
                        bitrate_str = track.get("bitrate", "")
                        info_str = f"{artist} - {title}"
                        if format_str or bitrate_str:
                            info_parts = []
                            if format_str:
                                info_parts.append(format_str)
                            if bitrate_str:
                                info_parts.append(bitrate_str)
                            info_str += f" [{', '.join(info_parts)}]"
                        main_window.setWindowTitle(info_str)
                    
                    self.gapless_manager.current_track_path = filepath
                    self._preload_next()
                    
                    if main_window:
                        main_window.update_playback_ui()
                        main_window.update_playpause_icon()
                    break

    def set_model(self, model):
        self.model = model

    def set_view(self, view):
        self.view = view

    def set_equalizer(self, eq_widget):
        self.eq_widget = eq_widget
        self.gapless_manager.eq_widget = eq_widget

    def play_index(self, index):
        if not self.model:
            return
        
        path = self.model.path_at(index)
        if not path:
            return
        
        next_path = None
        if index + 1 < self.model.rowCount():
            next_path = self.model.path_at(index + 1)
        
        self.gapless_manager.play_track(path, preload_next=next_path)
        self.current_index = index
        self.model.set_current_index(index)
        
        if next_path is None:
            self._preload_next()
        
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
            format_str = track.get("format", "")
            bitrate_str = track.get("bitrate", "")
            info_str = f"{artist} - {title}"
            if format_str or bitrate_str:
                info_parts = []
                if format_str:
                    info_parts.append(format_str)
                if bitrate_str:
                    info_parts.append(bitrate_str)
                info_str += f" [{', '.join(info_parts)}]"
            main_window.setWindowTitle(info_str)

    def _preload_next(self):
        if not self.model or self.current_index < 0:
            return
            
        next_index = self.current_index + 1
        if next_index < self.model.rowCount():
            next_path = self.model.path_at(next_index)
            if next_path:
                threading.Thread(
                    target=self.gapless_manager._preload_next_track,
                    args=(next_path,),
                    daemon=True
                ).start()
        else:
            with self.gapless_manager.preload_lock:
                self.gapless_manager.next_track_path = None
                if self.gapless_manager.standby_player:
                    self.gapless_manager.standby_player.stop()
                    self.gapless_manager.standby_player.set_media(None)

    def refresh_preload(self):
        if self.current_index < 0 or not self.model:
            return
        
        with self.gapless_manager.preload_lock:
            old_preload = self.gapless_manager.next_track_path
            self.gapless_manager.next_track_path = None
            if self.gapless_manager.standby_player:
                self.gapless_manager.standby_player.stop()
                self.gapless_manager.standby_player.set_media(None)
        
        self._preload_next()

    def pause(self):
        self.gapless_manager.pause()

    def play(self):
        if (self.gapless_manager.current_track_path and 
            self.gapless_manager.active_player and 
            not self.gapless_manager.is_playing()):
            self.gapless_manager.resume()
        elif self.current_index >= 0:
            self.play_index(self.current_index)

    def stop(self):
        self.gapless_manager.stop()

    def next(self):
        if self.model and self.current_index is not None:
            next_index = self.current_index + 1
            if next_index < self.model.rowCount():
                self.play_index(next_index)

    def previous(self):
        if self.model and self.current_index is not None:
            prev_index = self.current_index - 1
            if prev_index >= 0:
                self.play_index(prev_index)

    def set_volume(self, volume):
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
        self.custom_hover_color = None  # Custom hover color

    def set_hover_row(self, row):
        if self.hover_row != row:
            self.hover_row = row
            if self.parent():
                self.parent().viewport().update()
    
    def set_hover_color(self, color):
        """Set custom hover color."""
        self.custom_hover_color = color
        if self.parent():
            self.parent().viewport().update()

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)

        if index.row() == self.model.current_index and self.model.highlight_color:
            painter.save()
            # Paint semi-transparent highlight color directly (will blend with whatever is underneath)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
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
            # Paint semi-transparent hover color directly (will blend with whatever is underneath)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            
            if self.custom_hover_color:
                hover_color = QColor(self.custom_hover_color)
                hover_color.setAlpha(100)
            else:
                app = QApplication.instance()
                if app:
                    app_palette = app.palette()
                    base_color = app_palette.color(QPalette.Base)
                    if is_dark_color(base_color):
                        hover_color = QColor(base_color.lighter(130))
                        hover_color.setAlpha(100)
                    else:
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

        # Draw the expand/collapse arrow manually so it appears on top of the custom background
        tree_view = getattr(self, 'tree_view', None)
        if tree_view:
            style = tree_view.style()
            branch_rect = style.subElementRect(QStyle.SE_TreeViewDisclosureItem, opt, tree_view)
            if branch_rect.isValid() and not branch_rect.isEmpty():
                # Only draw if this is a folder (has children)
                model = index.model()
                if model and model.hasChildren(index):
                    # Determine expanded/collapsed state
                    expanded = tree_view.isExpanded(index)
                    # Use the style to draw the arrow
                    branch_option = QStyleOption()
                    branch_option.rect = branch_rect
                    branch_option.state = QStyle.State_Children
                    if expanded:
                        branch_option.state |= QStyle.State_Open
                    style.drawPrimitive(QStyle.PE_IndicatorBranch, branch_option, painter, tree_view)

        super().paint(painter, opt, index)

class DirectoryBrowserDelegate(QStyledItemDelegate):
    """Custom delegate for directory browser highlighting."""

    def __init__(self, tree_view, parent=None):
        super().__init__(parent)
        self.tree_view = tree_view
        self.highlight_color = None
        self.custom_hover_color = None
        self.hover_index = None
    
    def set_hover_color(self, color):
        """Set custom hover color."""
        self.custom_hover_color = color
        if self.tree_view:
            self.tree_view.viewport().update()
    
    def set_hover_index(self, index):
        """Set the currently hovered index."""
        if self.hover_index != index:
            self.hover_index = index
            if self.tree_view:
                self.tree_view.viewport().update()
    
    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        original_rect = QRect(option.rect)  # Save the original rect
        
        model = index.model()
        is_directory = model.isDir(index)
        
        # Calculate depth for indentation clearing
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        
        # For any item with depth > 0, paint base color over the indentation area
        if depth > 0 and self.tree_view:
            indentation_width = self.tree_view.indentation()
            total_indent = depth * indentation_width
            
            painter.save()
            app = QApplication.instance()
            if app:
                base_color = app.palette().color(QPalette.Base)
                # Paint only the indentation strip
                indent_rect = QRect(0, original_rect.y(), total_indent, original_rect.height())
                painter.fillRect(indent_rect, base_color)
            painter.restore()

        # Paint hover state - use original_rect
        if (option.state & QStyle.State_MouseOver) and self.hover_index == index:
            painter.save()
            # Paint semi-transparent hover color directly (will blend with whatever is underneath)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            
            if self.custom_hover_color:
                hover_color = QColor(self.custom_hover_color)
                hover_color.setAlpha(100)
            else:
                app = QApplication.instance()
                if app:
                    app_palette = app.palette()
                    base_color = app_palette.color(QPalette.Base)
                    if is_dark_color(base_color):
                        hover_color = QColor(base_color.lighter(120))
                        hover_color.setAlpha(100)
                    else:
                        hover_color = QColor(220, 238, 255, 100)
                else:
                    hover_color = QColor(220, 238, 255, 100)
            painter.fillRect(original_rect, hover_color)
            painter.restore()

        if (option.state & QStyle.State_Selected) and self.highlight_color:
            painter.save()
            # Paint semi-transparent highlight color directly (will blend with whatever is underneath)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.fillRect(original_rect, self.highlight_color)
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
        self.highlight_color = color
        if self.tree_view:
            self.tree_view.viewport().update()

# ============================================================================
# CUSTOM WIDGETS
# ============================================================================

class AlbumArtLabel(QLabel):
    """QLabel that rescales pixmap with aspect ratio and rounded corners."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(100, 100)
        self._original_pixmap = None
        self._scaled_pixmap = None
        self.border_radius = 8

    def set_album_pixmap(self, pixmap: QPixmap):
        self._original_pixmap = pixmap
        self._update_scaled_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self):
        if self._original_pixmap:
            target_size = self.size().boundedTo(self._original_pixmap.size())
            scaled = self._original_pixmap.scaled(
                target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self._scaled_pixmap = scaled
            self.update()
    
    def paintEvent(self, event):
        if self._scaled_pixmap:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            
            x = (self.width() - self._scaled_pixmap.width()) // 2
            y = (self.height() - self._scaled_pixmap.height()) // 2
            
            path = QPainterPath()
            path.addRoundedRect(
                x, y, 
                self._scaled_pixmap.width(), 
                self._scaled_pixmap.height(),
                self.border_radius, 
                self.border_radius
            )
            
            painter.setClipPath(path)
            painter.drawPixmap(x, y, self._scaled_pixmap)
        else:
            super().paintEvent(event)

class DirectoryTreeView(QTreeView):
    """QTreeView with drag support and context menu."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.playlist_model = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)  # Disable focus rectangle
    
    def showEvent(self, event):
        super().showEvent(event)
        self._apply_rounded_mask()
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_rounded_mask()
    
    def _apply_rounded_mask(self):
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 8, 8)
        region = QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)
    
    def _show_context_menu(self, position):
        index = self.indexAt(position)
        if not index.isValid():
            return
        
        model = self.model()
        selected_indexes = self.selectedIndexes()
        if not selected_indexes:
            return
        
        has_files = False
        has_folders = False
        
        for idx in selected_indexes:
            if model.isDir(idx):
                has_folders = True
            else:
                ext = os.path.splitext(model.filePath(idx))[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    has_files = True
        
        if not (has_files or has_folders):
            return
        
        menu = QMenu(self)
        play_next_action = menu.addAction("Play Next")
        add_to_playlist_action = menu.addAction("Add to Playlist")
        menu.addSeparator()
        overwrite_playlist_action = menu.addAction("Add and Overwrite Playlist")
        
        action = menu.exec(self.viewport().mapToGlobal(position))
        
        if action == play_next_action:
            self._add_selected_play_next()
        elif action == add_to_playlist_action:
            self._add_selected_to_playlist()
        elif action == overwrite_playlist_action:
            self._overwrite_playlist_with_selected()
    
    def _add_selected_play_next(self):
        paths = self._get_paths_from_selection()
        if paths and self.playlist_model:
            controller = self.playlist_model.controller
            if controller and controller.model:
                insert_pos = controller.current_index + 1 if controller.current_index >= 0 else 0
                self._insert_files_at_position(paths, insert_pos)
    
    def _add_selected_to_playlist(self):
        paths = self._get_paths_from_selection()
        if paths and self.playlist_model:
            self.playlist_model.add_tracks(paths, clear=False)
    
    def _overwrite_playlist_with_selected(self):
        paths = self._get_paths_from_selection()
        if paths and self.playlist_model:
            controller = self.playlist_model.controller
            # Stop playback first, then replace playlist and start new track
            # Use a delay to ensure stop completes before starting new track
            if controller:
                controller.stop()
            self.playlist_model.add_tracks(paths, clear=True)
            if controller and len(paths) > 0:
                # Use 200ms delay to ensure stop_equalizer completes before start_equalizer
                QTimer.singleShot(200, lambda: self._start_playback_after_overwrite(controller))
    
    def _start_playback_after_overwrite(self, controller):
        if controller and controller.model and controller.model.rowCount() > 0:
            controller.play_index(0)
    
    def _get_paths_from_selection(self):
        model = self.model()
        selected_indexes = self.selectedIndexes()
        paths = []
        processed_paths = set()
        
        for index in selected_indexes:
            if not index.isValid():
                continue
            
            file_path = model.filePath(index)
            if file_path in processed_paths:
                continue
            processed_paths.add(file_path)
            
            if model.isDir(index):
                folder_paths = self._get_audio_files_from_folder(file_path)
                paths.extend(folder_paths)
            else:
                ext = os.path.splitext(file_path)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    paths.append(file_path)
        return paths
    
    def _get_audio_files_from_folder(self, folder_path):
        paths = []
        for root, dirs, files in os.walk(folder_path):
            for file in sorted(files):
                file_path = os.path.join(root, file)
                if os.path.splitext(file_path)[1].lower() in SUPPORTED_EXTENSIONS:
                    paths.append(file_path)
        return paths
    
    def _insert_files_at_position(self, paths, position):
        if not self.playlist_model or not paths:
            return
        
        new_items = [
            extract_metadata(path, i)
            for i, path in enumerate(paths, start=1)
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS
        ]
        
        if not new_items:
            return
        
        position = max(0, min(position, self.playlist_model.rowCount()))
        
        self.playlist_model.beginInsertRows(QModelIndex(), position, position + len(new_items) - 1)
        for i, item in enumerate(new_items):
            self.playlist_model._tracks.insert(position + i, item)
        self.playlist_model.endInsertRows()
        
        if self.playlist_model.current_index >= position:
            self.playlist_model.current_index += len(new_items)
        
        if self.playlist_model.controller:
            self.playlist_model.controller.refresh_preload()
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_start_position = event.position().toPoint()
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        # Update hover state
        index = self.indexAt(event.position().toPoint())
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_hover_index"):
            delegate.set_hover_index(index if index.isValid() else None)
        
        if not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return
        
        if not hasattr(self, 'drag_start_position'):
            super().mouseMoveEvent(event)
            return
        
        if (event.position().toPoint() - self.drag_start_position).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return
        
        selected_indexes = self.selectedIndexes()
        if not selected_indexes:
            super().mouseMoveEvent(event)
            return
        
        model = self.model()
        urls = []
        for index in selected_indexes:
            if index.isValid():
                file_path = model.filePath(index)
                if os.path.isfile(file_path) or os.path.isdir(file_path):
                    urls.append(QUrl.fromLocalFile(file_path))
        
        if not urls:
            super().mouseMoveEvent(event)
            return
        
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setUrls(urls)
        drag.setMimeData(mime_data)
        drag.exec(Qt.CopyAction)
    
    def leaveEvent(self, event):
        """Clear hover state when mouse leaves the tree view."""
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_hover_index"):
            delegate.set_hover_index(None)
        super().leaveEvent(event)

class PlaylistView(QTableView):
    """QTableView with watermark, hover support, and drag-and-drop."""

    def __init__(self, logo_path=None, parent=None):
        super().__init__(parent)
        if logo_path is None:
            logo_path = get_asset_path("logo.png")
        self.logo = QPixmap(logo_path)
        self.setMouseTracking(True)
        
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        
        self._drag_selecting = False
        self._drag_start_row = -1
    
    def showEvent(self, event):
        super().showEvent(event)
        self._apply_rounded_mask()
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_rounded_mask()
    
    def _apply_rounded_mask(self):
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 8, 8)
        region = QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)
    
    def _show_context_menu(self, position):
        selected_rows = sorted(set(index.row() for index in self.selectedIndexes()))
        if not selected_rows:
            return
        
        menu = QMenu(self)
        if len(selected_rows) == 1:
            remove_action = menu.addAction("Remove from Playlist")
        else:
            remove_action = menu.addAction(f"Remove {len(selected_rows)} Tracks from Playlist")
        
        action = menu.exec(self.viewport().mapToGlobal(position))
        if action == remove_action:
            self._remove_selected_tracks(selected_rows)
    
    def _remove_selected_tracks(self, rows):
        model = self.model()
        if not model or not rows:
            return
        
        for row in reversed(rows):
            if 0 <= row < model.rowCount():
                model.beginRemoveRows(QModelIndex(), row, row)
                model._tracks.pop(row)
                
                if model.current_index == row:
                    if model.controller:
                        model.controller.stop()
                    model.current_index = -1
                elif model.current_index > row:
                    model.current_index -= 1
                
                model.endRemoveRows()
        
        if model.controller:
            model.controller.refresh_preload()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                modifiers = event.modifiers()
                if modifiers & (Qt.ControlModifier | Qt.ShiftModifier):
                    # Let default handler manage Ctrl/Shift selection
                    super().mousePressEvent(event)
                    return
                else:
                    row = index.row()
                    # Check if this row is already selected - toggle if so
                    if self.selectionModel().isRowSelected(row, QModelIndex()):
                        # Row is selected - deselect it (don't call super to avoid re-selection)
                        self.clearSelection()
                        return
                    else:
                        # Row is not selected - select it
                        self._drag_selecting = True
                        self._drag_start_row = row
                        self.clearSelection()
                        self.selectRow(row)
                        # Fall through to call super() for double-click detection
            else:
                # Clicked on empty area - clear selection
                self.clearSelection()
                return
        
        # Call parent to let Qt track double-clicks properly
        super().mousePressEvent(event)
    
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_selecting = False
            self._drag_start_row = -1
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_hover_row"):
            delegate.set_hover_row(index.row() if index.isValid() else -1)
        
        if self._drag_selecting and index.isValid():
            current_row = index.row()
            if current_row != self._drag_start_row:
                self.clearSelection()
                start = min(self._drag_start_row, current_row)
                end = max(self._drag_start_row, current_row)
                for row in range(start, end + 1):
                    self.selectRow(row)
        
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        delegate = self.itemDelegate()
        if hasattr(delegate, "set_hover_row"):
            delegate.set_hover_row(-1)
        super().leaveEvent(event)
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-playlist-track-index') or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat('application/x-playlist-track-index') or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dropEvent(self, event):
        mime_data = event.mimeData()
        
        if mime_data.hasFormat('application/x-playlist-track-index'):
            super().dropEvent(event)
            return
        
        if not mime_data.hasUrls():
            event.ignore()
            return
        
        paths = []
        for url in mime_data.urls():
            path = url.toLocalFile()
            if os.path.isfile(path):
                if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS:
                    paths.append(path)
            elif os.path.isdir(path):
                for root, dirs, files in os.walk(path):
                    for file in sorted(files):
                        file_path = os.path.join(root, file)
                        if os.path.splitext(file_path)[1].lower() in SUPPORTED_EXTENSIONS:
                            paths.append(file_path)
        
        if paths:
            model = self.model()
            if model:
                model.add_tracks(paths, clear=False)
        event.acceptProposedAction()
        
    def viewportEvent(self, event):
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
        
        title = QLabel("Adjust Peak Indicator Transparency")
        title.setStyleSheet("font-size: 14px; font-weight: bold; padding: 10px;")
        layout.addWidget(title)
        
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
        
        self.value_label = QLabel(f"Current: {int((current_alpha / 255) * 100)}%")
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setStyleSheet("font-size: 12px; color: #555; padding: 10px;")
        layout.addWidget(self.value_label)
        
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
        percentage = int((value / 255) * 100)
        self.value_label.setText(f"Current: {percentage}%")
        self.transparency_changed.emit(value)
    
    def _reset_to_default(self):
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
        
        title_label = QLabel(f"{title}")
        title_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 10px;")
        layout.addWidget(title_label)
        
        family_layout = QHBoxLayout()
        family_label = QLabel("Font Family:")
        family_label.setStyleSheet("color: #555; font-weight: bold;")
        family_layout.addWidget(family_label)
        
        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(self.current_font)
        self.font_combo.currentFontChanged.connect(self._on_font_changed)
        family_layout.addWidget(self.font_combo, 1)
        layout.addLayout(family_layout)
        
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
        self.current_font.setFamily(font.family())
        self._update_preview()
    
    def _on_size_changed(self, size):
        self.current_font.setPointSize(size)
        self.size_label.setText(f"{size} pt")
        self._update_preview()
    
    def _update_preview(self):
        self.preview_label.setFont(self.current_font)
    
    def _apply_font(self):
        self.font_changed.emit(self.current_font)
    
    def _reset_to_default(self):
        default_font = QFont()
        self.font_combo.setCurrentFont(default_font)
        self.size_slider.setValue(default_font.pointSize())

class SearchWorker(QThread):
    """Background worker thread for searching library."""
    
    progress = Signal(list, str)  # Progressive results, base_directory
    finished = Signal(list, str)  # Final results, base_directory
    
    def __init__(self, directory, query, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.query = query.lower()
        self.results = []
        self.batch_size = 50  # Emit results every 50 matches
    
    def run(self):
        """Perform the search in background."""
        try:
            matched_folders = set()
            batch = []
            
            # Search for matching files and folders
            for root, dirs, files in os.walk(self.directory):
                # Check if any part of the path matches (any parent or current folder)
                path_parts = root.split(os.sep)
                current_folder_matched = any(self.query in part.lower() for part in path_parts)
                
                if current_folder_matched:
                    matched_folders.add(root)
                
                # Check if any child folder name matches
                for dirname in dirs:
                    if self.query in dirname.lower():
                        folder_path = os.path.join(root, dirname)
                        matched_folders.add(folder_path)
                
                # Search files
                for filename in files:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        # Include file if:
                        # 1. Its name matches the query
                        # 2. OR any part of its path matches the query
                        filepath = os.path.join(root, filename)
                        
                        if (self.query in filename.lower() or 
                            current_folder_matched or
                            root in matched_folders):
                            metadata = extract_metadata(filepath, 0)
                            self.results.append(metadata)
                            batch.append(metadata)
                            
                            # Emit progress every batch_size results
                            if len(batch) >= self.batch_size:
                                self.progress.emit(batch[:], self.directory)
                                batch.clear()
            
            # Emit any remaining results in the final batch
            if batch:
                self.progress.emit(batch[:], self.directory)
                        
        except Exception as e:
            print(f"Search error: {e}")
        
        self.finished.emit(self.results, self.directory)

class SearchResultsModel(QAbstractItemModel):
    """Model for search results with folder grouping."""
    
    FOLDER_ID = 0xFFFFFFFF  # Use max uint to represent folder items
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.folders = []  # List of (folder_name, [tracks])
        self.flat_results = []  # Flat list for easy access
        self.base_directory = None  # Store base directory for relative paths
    
    def set_results(self, results, base_directory=None):
        """Group results by folder/album."""
        self.beginResetModel()
        self.base_directory = base_directory
        
        # Group by folder path
        from collections import defaultdict
        folder_dict = defaultdict(list)
        
        for track in results:
            folder_path = os.path.dirname(track["path"])
            folder_name = os.path.basename(folder_path)
            folder_dict[folder_path].append(track)
        
        # Convert to list of (folder_name, folder_path, tracks)
        self.folders = []
        for folder_path, tracks in sorted(folder_dict.items()):
            folder_name = os.path.basename(folder_path)
            self.folders.append((folder_name, folder_path, tracks))
        
        self.flat_results = results
        self.endResetModel()
    
    def add_results(self, batch_results, base_directory=None):
        """Add a batch of results progressively."""
        if base_directory:
            self.base_directory = base_directory
        
        # Add new tracks to flat results
        self.flat_results.extend(batch_results)
        
        # Group new batch by folder
        from collections import defaultdict
        existing_folders = {fp: (fn, tracks) for fn, fp, tracks in self.folders}
        
        for track in batch_results:
            folder_path = os.path.dirname(track["path"])
            folder_name = os.path.basename(folder_path)
            
            if folder_path in existing_folders:
                # Add to existing folder
                existing_folders[folder_path][1].append(track)
            else:
                # Create new folder
                existing_folders[folder_path] = (folder_name, [track])
        
        # Rebuild folders list
        self.beginResetModel()
        self.folders = []
        for folder_path, (folder_name, tracks) in sorted(existing_folders.items()):
            self.folders.append((folder_name, folder_path, tracks))
        self.endResetModel()
    
    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        
        if not parent.isValid():
            # Top level (folders) - use FOLDER_ID
            return self.createIndex(row, column, self.FOLDER_ID)
        else:
            # Child level (tracks in folder) - use folder index
            folder_idx = parent.row()
            return self.createIndex(row, column, folder_idx)
    
    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        
        internal_id = index.internalId()
        if internal_id == self.FOLDER_ID:
            # Top level item (folder)
            return QModelIndex()
        else:
            # Child item (track) - parent is the folder
            folder_idx = internal_id
            return self.createIndex(folder_idx, 0, self.FOLDER_ID)
    
    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            # Root level - number of folders
            return len(self.folders)
        elif parent.internalId() == self.FOLDER_ID:
            # Folder level - number of tracks in folder
            folder_idx = parent.row()
            if folder_idx < len(self.folders):
                return len(self.folders[folder_idx][2])
        return 0
    
    def columnCount(self, parent=QModelIndex()):
        return 3
    
    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            headers = ["Title/Folder", "Artist", "Album"]
            return headers[section] if section < len(headers) else None
        return None
    
    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        
        internal_id = index.internalId()
        row = index.row()
        col = index.column()
        
        if internal_id == self.FOLDER_ID:
            # Folder row
            if row >= len(self.folders):
                return None
            folder_name, folder_path, tracks = self.folders[row]
            
            if role == Qt.DisplayRole:
                if col == 0:
                    # Show relative path from base directory
                    if self.base_directory and folder_path.startswith(self.base_directory):
                        rel_path = os.path.relpath(folder_path, self.base_directory)
                        return rel_path
                    else:
                        return folder_name
                return ""
            elif role == Qt.DecorationRole:
                if col == 0:
                    # Return folder icon based on theme
                    app = QApplication.instance()
                    if app:
                        palette = app.palette()
                        base_color = palette.color(QPalette.Base)
                        if is_dark_color(base_color):
                            return QIcon(get_asset_path("dirwhite.svg"))
                        else:
                            return QIcon(get_asset_path("dir.svg"))
                return None
            elif role == Qt.FontRole:
                font = QFont()
                font.setBold(True)
                return font
        else:
            # Track row - internal_id is the folder index
            folder_idx = internal_id
            if folder_idx >= len(self.folders):
                return None
            tracks = self.folders[folder_idx][2]
            if row >= len(tracks):
                return None
            
            track = tracks[row]
            
            if role == Qt.DisplayRole:
                if col == 0:
                    return track.get("title", "")
                elif col == 1:
                    return track.get("artist", "")
                elif col == 2:
                    return track.get("album", "")
            elif role == Qt.DecorationRole:
                if col == 0:
                    # Return file icon based on theme
                    app = QApplication.instance()
                    if app:
                        palette = app.palette()
                        base_color = palette.color(QPalette.Base)
                        if is_dark_color(base_color):
                            return QIcon(get_asset_path("filewhite.svg"))
                        else:
                            return QIcon(get_asset_path("file.svg"))
                return None
        
        return None
    
    def get_track_at_index(self, index):
        """Get track metadata at index."""
        if not index.isValid():
            return None
        
        internal_id = index.internalId()
        if internal_id == self.FOLDER_ID:
            # This is a folder, not a track
            return None
        
        # internal_id is the folder index
        folder_idx = internal_id
        row = index.row()
        if folder_idx < len(self.folders):
            tracks = self.folders[folder_idx][2]
            if row < len(tracks):
                return tracks[row]
        return None
    
    def get_folder_tracks(self, folder_row):
        """Get all tracks in a folder."""
        if folder_row < len(self.folders):
            return self.folders[folder_row][2]
        return []

class SearchResultsDelegate(QStyledItemDelegate):
    """Custom delegate for search results tree view highlighting."""

    def __init__(self, tree_view, parent=None):
        super().__init__(parent)
        self.tree_view = tree_view
        self.highlight_color = None
        self.custom_hover_color = None
        self.hover_index = None
    
    def set_hover_color(self, color):
        """Set custom hover color."""
        self.custom_hover_color = color
        if self.tree_view:
            self.tree_view.viewport().update()
    
    def set_highlight_color(self, color):
        """Set custom selection color."""
        self.highlight_color = color
        if self.tree_view:
            self.tree_view.viewport().update()
    
    def set_hover_index(self, index):
        """Set the currently hovered index."""
        if self.hover_index != index:
            old_index = self.hover_index
            self.hover_index = index
            if self.tree_view:
                # Repaint the old row
                if old_index and old_index.isValid():
                    model = self.tree_view.model()
                    if model:
                        col_count = model.columnCount(old_index.parent())
                        for col in range(col_count):
                            sibling = model.index(old_index.row(), col, old_index.parent())
                            self.tree_view.update(sibling)
                # Repaint the new row
                if index and index.isValid():
                    model = self.tree_view.model()
                    if model:
                        col_count = model.columnCount(index.parent())
                        for col in range(col_count):
                            sibling = model.index(index.row(), col, index.parent())
                            self.tree_view.update(sibling)
    
    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        
        # Check if this is a folder or track item
        is_folder = index.internalId() == 0xFFFFFFFF  # FOLDER_ID
        
        # For track items (child items), clear the indentation area
        if not is_folder and self.tree_view:
            indentation_width = self.tree_view.indentation()
            
            # Clear the indentation area to base color
            painter.save()
            app = QApplication.instance()
            if app:
                base_color = app.palette().color(QPalette.Base)
                indent_rect = QRect(0, opt.rect.y(), indentation_width, opt.rect.height())
                painter.fillRect(indent_rect, base_color)
            painter.restore()
        
        # Paint folder rows with accent color background spanning all columns
        if is_folder and self.highlight_color:
            # Paint accent background in ALL columns
            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.fillRect(opt.rect, self.highlight_color)
            painter.restore()
            
            # Only paint text in column 0, spanning across all columns
            if index.column() == 0:
                # Calculate full row width
                model = index.model()
                col_count = model.columnCount(index.parent()) if model else 1
                
                if self.tree_view:
                    header = self.tree_view.header()
                    full_width = sum(header.sectionSize(col) for col in range(col_count))
                    text_rect = QRect(opt.rect.left(), opt.rect.top(), full_width, opt.rect.height())
                else:
                    text_rect = opt.rect
                
                # Get the text to display
                display_text = index.data(Qt.DisplayRole)
                if display_text:
                    # Get icon
                    icon = index.data(Qt.DecorationRole)
                    
                    # Set text color
                    text_color = Qt.white if is_dark_color(self.highlight_color) else Qt.black
                    
                    painter.save()
                    painter.setPen(text_color)
                    
                    # Paint icon if present
                    icon_width = 0
                    if icon and not icon.isNull():
                        icon_size = opt.decorationSize
                        icon_rect = QRect(text_rect.left() + 4, text_rect.top() + (text_rect.height() - icon_size.height()) // 2,
                                         icon_size.width(), icon_size.height())
                        icon.paint(painter, icon_rect)
                        icon_width = icon_size.width() + 8
                    
                    # Paint text
                    text_rect.setLeft(text_rect.left() + icon_width)
                    painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, display_text)
                    painter.restore()
                
                return  # Don't call super().paint() for folder column 0
            else:
                return  # Don't paint any content in columns > 0 for folders
            
        else:
            # For track items, don't manually paint background - let Qt handle alternating rows
            # Just clear the indentation area (already done above)
            pass

        # Paint hover state BEFORE super().paint() so text renders on top
        if self.hover_index and self.hover_index.row() == index.row() and self.hover_index.parent() == index.parent():
            painter.save()
            # Paint semi-transparent hover color directly (will blend with whatever is underneath)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            
            if self.custom_hover_color:
                hover_color = QColor(self.custom_hover_color)
                hover_color.setAlpha(100)
            else:
                app = QApplication.instance()
                if app:
                    app_palette = app.palette()
                    base_color = app_palette.color(QPalette.Base)
                    if is_dark_color(base_color):
                        hover_color = QColor(base_color.lighter(120))
                        hover_color.setAlpha(100)
                    else:
                        hover_color = QColor(220, 238, 255, 100)
                else:
                    hover_color = QColor(220, 238, 255, 100)
            painter.fillRect(opt.rect, hover_color)
            painter.restore()


        # Paint selection state - always paint the full row
        if (option.state & QStyle.State_Selected) and self.highlight_color and not is_folder:
            painter.save()
            # Paint semi-transparent highlight color directly (will blend with whatever is underneath)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
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
        
        # Draw cell borders for track items (not folders) using the same color as the playlist
        if not is_folder and self.highlight_color:
            painter.save()
            
            # Determine the grid color based on theme
            app = QApplication.instance()
            if app:
                palette_obj = app.palette()
                base_color = palette_obj.color(QPalette.Base)
                if is_dark_color(base_color):
                    # Use mid color for dark themes
                    grid_color = palette_obj.color(QPalette.Mid)
                else:
                    # Use light gray for light themes
                    grid_color = QColor("#ddd")
            else:
                grid_color = QColor("#ddd")
            
            # Draw the right border
            painter.setPen(grid_color)
            painter.drawLine(opt.rect.topRight(), opt.rect.bottomRight())
            
            # Draw the bottom border
            painter.drawLine(opt.rect.bottomLeft(), opt.rect.bottomRight())
            
            painter.restore()


class SearchResultsTreeView(QTreeView):
    """Custom tree view for search results that handles double-clicks properly."""
    
    double_clicked = Signal(QModelIndex)
    
    def mouseDoubleClickEvent(self, event):
        """Handle mouse double-click events."""
        index = self.indexAt(event.pos())
        if index.isValid():
            self.double_clicked.emit(index)
        super().mouseDoubleClickEvent(event)

class SearchResultsDialog(QWidget):
    """Dialog for displaying and interacting with search results."""
    
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Search Results")
        self.setWindowIcon(QIcon(get_asset_path("icon.ico")))
        self.resize(800, 500)
        
        self.playlist_model = None
        self.controller = None
        self.settings = None  # Will be set by MainWindow
        
        layout = QVBoxLayout(self)
        
        # Results count label
        self.count_label = QLabel("No results")
        self.count_label.setStyleSheet("font-weight: bold; padding: 5px;")
        layout.addWidget(self.count_label)
        
        # Results tree view
        self.results_tree = SearchResultsTreeView()
        self.results_tree.double_clicked.connect(self._on_double_click)
        self.results_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_tree.setAlternatingRowColors(False)
        self.results_tree.setHeaderHidden(False)
        self.results_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results_tree.customContextMenuRequested.connect(self._show_context_menu)
        # Don't use doubleClicked signal - we'll handle it via mouse events instead
        self.results_tree.setExpandsOnDoubleClick(True)
        self.results_tree.setMouseTracking(True)
        self.results_tree.setItemsExpandable(False)  # Disable expand/collapse
        self.results_tree.setRootIsDecorated(False)  # Hide expand indicators
        self.results_tree.setIndentation(20)  # Set indentation for child items
        self.results_tree.setUniformRowHeights(True)  # Enable uniform row heights for performance
        
        # Create model and delegate
        self.model = SearchResultsModel(self)
        self.results_tree.setModel(self.model)
        
        # Create delegate for custom hover/selection colors
        self.delegate = SearchResultsDelegate(self.results_tree, self)
        self.results_tree.setItemDelegate(self.delegate)
        
        # Set icon size to make rows more compact
        self.results_tree.setIconSize(QSize(14, 14))
        
        # Track hover state
        self.results_tree.viewport().installEventFilter(self)
        
        # Store colors
        self.accent_color = None
        self.hover_color = None
        
        layout.addWidget(self.results_tree)
        
        # Button layout
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)
        
        # Configure column widths - make them interactive (resizable)
        header = self.results_tree.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setStretchLastSection(True)
        
        # Restore column widths from settings will be called after settings is set
    
    def _restore_column_widths(self):
        """Restore column widths from settings."""
        if self.settings and self.settings.contains("searchResultsHeader"):
            self.results_tree.header().restoreState(self.settings.value("searchResultsHeader"))
    
    def _restore_geometry(self):
        """Restore window geometry from settings."""
        if self.settings and self.settings.contains("searchResultsGeometry"):
            self.restoreGeometry(self.settings.value("searchResultsGeometry"))
    
    def _save_column_widths(self):
        """Save column widths to settings."""
        if self.settings:
            self.settings.setValue("searchResultsHeader", self.results_tree.header().saveState())
    
    def _save_geometry(self):
        """Save window geometry to settings."""
        if self.settings:
            self.settings.setValue("searchResultsGeometry", self.saveGeometry())
    
    def closeEvent(self, event):
        """Save column widths and geometry when closing."""
        self._save_column_widths()
        self._save_geometry()
        # Stop the search worker if it's running
        if hasattr(self.parent(), 'search_worker') and self.parent().search_worker and self.parent().search_worker.isRunning():
            self.parent().search_worker.quit()
            self.parent().search_worker.wait()
        # Notify parent that user closed the dialog so search doesn't reopen it
        if self.parent():
            self.parent().search_results_closed = True
        super().closeEvent(event)
    
    def set_results(self, results, base_directory=None):
        """Update the search results."""
        self.model.set_results(results, base_directory)
        
        # Expand all folders by default
        self.results_tree.expandAll()
        
        # Scroll to the top
        self.results_tree.scrollToTop()
        
        count = len(results)
        if count == 0:
            self.count_label.setText("No results found")
        elif count == 1:
            self.count_label.setText("1 track found")
        else:
            self.count_label.setText(f"{count} tracks found")
    
    def add_results(self, batch_results, base_directory=None):
        """Add a batch of results progressively."""
        self.model.add_results(batch_results, base_directory)
        
        # Expand new folders
        self.results_tree.expandAll()
        
        # Update count label
        count = len(self.model.flat_results)
        if count == 1:
            self.count_label.setText("1 track found")
        else:
            self.count_label.setText(f"{count} tracks found...")
    
    def set_playlist_model(self, model):
        """Set the playlist model for adding tracks."""
        self.playlist_model = model
    
    def set_controller(self, controller):
        """Set the controller for playback."""
        self.controller = controller
    
    def set_colors(self, accent_color, hover_color):
        """Set accent and hover colors for the search results."""
        self.accent_color = accent_color
        self.hover_color = hover_color
        
        # Apply colors to delegate
        if self.delegate:
            self.delegate.set_highlight_color(accent_color)
            self.delegate.set_hover_color(hover_color)
        
        self._update_stylesheet()
    
    def eventFilter(self, obj, event):
        """Track mouse movements for hover effects."""
        if obj == self.results_tree.viewport():
            if event.type() == QEvent.MouseMove:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                index = self.results_tree.indexAt(pos)
                if self.delegate:
                    self.delegate.set_hover_index(index if index.isValid() else None)
            elif event.type() == QEvent.Leave:
                if self.delegate:
                    self.delegate.set_hover_index(None)
        return super().eventFilter(obj, event)
    
    def _update_stylesheet(self):
        """Apply custom colors to the tree view."""
        app = QApplication.instance()
        if not app:
            return
        
        palette = app.palette()
        text_color = palette.color(QPalette.Text)
        
        # Basic styling only - let Qt handle backgrounds naturally so hover works
        stylesheet = f"""
            QTreeView {{
                border: none;
                color: {text_color.name()};
            }}
            QTreeView::item {{
                padding: 4px;
                border: none;
            }}
        """
        
        self.results_tree.setStyleSheet(stylesheet)
    
    def _show_context_menu(self, position):
        """Show context menu for selected tracks."""
        selected = self.results_tree.selectedIndexes()
        if not selected:
            return
        
        menu = QMenu(self)
        replace_play_action = menu.addAction("Replace Playlist and Play")
        add_action = menu.addAction("Add to Playlist")
        play_next_action = menu.addAction("Play Next")
        
        action = menu.exec(self.results_tree.viewport().mapToGlobal(position))
        
        if action == replace_play_action:
            self._replace_and_play(selected)
        elif action == add_action:
            self._add_to_playlist(selected)
        elif action == play_next_action:
            self._play_next(selected)
    
    def _on_double_click(self, index):
        """Handle double-click to replace playlist and play."""
        if index.isValid():
            # Set flag to prevent search dialog from reopening
            if self.parent():
                self.parent().search_results_closed = True
            # Always play the track (folders can't be collapsed anymore)
            self._replace_and_play([index])
    
    def _get_tracks_from_selection(self, selected_indexes):
        """Get all track paths from selected indexes (including folder expansions)."""
        paths = []
        processed_folders = set()
        
        for index in selected_indexes:
            if not index.isValid():
                continue
            
            # Get the index at column 0 for this row (to ensure we're on the first column)
            if index.column() != 0:
                index = index.sibling(index.row(), 0)
            
            internal_id = index.internalId()
            
            if internal_id == self.model.FOLDER_ID:
                # This is a folder - add all its tracks
                folder_row = index.row()
                if folder_row not in processed_folders:
                    processed_folders.add(folder_row)
                    if folder_row < len(self.model.folders):
                        folder_tuple = self.model.folders[folder_row]
                        # folder_tuple is (folder_name, folder_path, tracks)
                        if len(folder_tuple) >= 3:
                            tracks = folder_tuple[2]
                            for track in tracks:
                                if isinstance(track, dict) and "path" in track:
                                    paths.append(track["path"])
            else:
                # This is a track
                folder_idx = internal_id
                row = index.row()
                if folder_idx < len(self.model.folders):
                    folder_tuple = self.model.folders[folder_idx]
                    # folder_tuple is (folder_name, folder_path, tracks)
                    if len(folder_tuple) >= 3:
                        tracks = folder_tuple[2]
                        if row < len(tracks):
                            track = tracks[row]
                            if isinstance(track, dict) and "path" in track:
                                paths.append(track["path"])
        
        return paths
    
    def _replace_and_play(self, selected_indexes):
        """Replace playlist with selected tracks and start playback."""
        if not self.playlist_model or not self.controller:
            return
        
        paths = self._get_tracks_from_selection(selected_indexes)
        if not paths:
            return
        
        # Stop playback first, then replace playlist and start new track
        self.controller.stop()
        self.playlist_model.add_tracks(paths, clear=True)
        
        # Use delay to ensure stop_equalizer completes before start_equalizer
        if self.controller.model.rowCount() > 0:
            QTimer.singleShot(200, lambda: self.controller.play_index(0))
        
        # Set flag to prevent search dialog from reopening
        if self.parent():
            self.parent().search_results_closed = True
        
        self.close()
    
    def _add_to_playlist(self, selected_indexes):
        """Add selected tracks to playlist."""
        if not self.playlist_model:
            return
        
        paths = self._get_tracks_from_selection(selected_indexes)
        if paths:
            self.playlist_model.add_tracks(paths, clear=False)
    
    def _play_next(self, selected_indexes):
        """Insert selected tracks to play next."""
        if not self.playlist_model or not self.controller:
            return
        
        paths = self._get_tracks_from_selection(selected_indexes)
        
        # Insert after current track
        insert_position = self.controller.current_index + 1 if self.controller.current_index >= 0 else 0
        
        new_items = [
            extract_metadata(path, i)
            for i, path in enumerate(paths, start=1)
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS
        ]
        
        if new_items:
            self.playlist_model.beginInsertRows(QModelIndex(), insert_position, insert_position + len(new_items) - 1)
            for i, item in enumerate(new_items):
                self.playlist_model._tracks.insert(insert_position + i, item)
            self.playlist_model.endInsertRows()
            
            if self.playlist_model.current_index >= insert_position:
                self.playlist_model.current_index += len(new_items)
            
            if self.controller:
                self.controller.refresh_preload()

# ============================================================================
# GLOBAL MEDIA KEY HANDLER
# ============================================================================

class GlobalMediaKeyHandler(QAbstractNativeEventFilter):
    """Cross-platform global media key handler."""
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.hwnd = None
        self.setup_platform_handler()
    
    def setup_platform_handler(self):
        if sys.platform == 'win32':
            self._setup_windows_handler()
        elif sys.platform == 'darwin':
            print("macOS media key support requires additional configuration")
        else:
            print("Linux media key support via MPRIS not fully implemented")
    
    def _setup_windows_handler(self):
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
                print("Some media keys could not be registered")
        except Exception as e:
            print(f"Failed to setup Windows media keys: {e}")
    
    def nativeEventFilter(self, eventType, message):
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
# MAIN WINDOW
# ============================================================================

class MainWindow(QMainWindow):
    """Main application window."""

    @staticmethod
    def get_tree_style(highlight_color, highlight_text_color, hover_color=None):
        """Generate theme-aware tree view stylesheet."""
        app = QApplication.instance()
        if not app:
            return f"""
                QTreeView {{
                    background-color: rgba(255, 255, 255, 150);
                    alternate-background-color: rgba(240, 240, 240, 150);
                    border: none;
                    border-radius: 8px;
                    color: #000000;
                }}
                QTreeView::viewport {{ border: none; border-radius: 8px; background: transparent; }}
                QTreeView::item {{ padding: 0px 4px; min-height: 12px; border: none; outline: none; }}
                QTreeView::item:selected {{ background: {highlight_color}; color: {highlight_text_color}; }}
                QTreeView::branch {{ background: transparent; width: 0px; border: none; }}
            """
        
        palette = app.palette()
        base_color = palette.color(QPalette.Base)
        text_color = palette.color(QPalette.Text)
        is_dark = is_dark_color(base_color)
        
        if is_dark:
            alternate_bg = f"rgba({base_color.lighter(110).red()}, {base_color.lighter(110).green()}, {base_color.lighter(110).blue()}, 150)"
            bg = f"rgba({base_color.red()}, {base_color.green()}, {base_color.blue()}, 150)"
        else:
            alternate_bg = "rgba(240, 240, 240, 150)"
            bg = "rgba(255, 255, 255, 150)"
        
        return f"""
            QTreeView {{
                background-color: {bg};
                alternate-background-color: {alternate_bg};
                border: none;
                border-radius: 8px;
                color: {text_color.name()};
            }}
            QTreeView::viewport {{ border: none; border-radius: 8px; background: transparent; }}
            QTreeView::item {{ padding: 0px 4px; min-height: 12px; border: none; outline: none; }}
            QTreeView::item:selected {{ background: {highlight_color}; color: {highlight_text_color}; }}
            QTreeView::branch {{ background: transparent; width: 0px; border: none; }}
        """

    @staticmethod
    def get_button_style():
        """Generate theme-aware button stylesheet."""
        app = QApplication.instance()
        if not app:
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
        app = QApplication.instance()
        if not app:
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
                    width: 14px;
                    height: 14px;
                    margin: -4px 0;
                    border-radius: 7px;
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
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:pressed {{ background: {button_color.darker(110).name()}; }}
        """

    @staticmethod
    def get_playlist_style():
        """Generate theme-aware playlist stylesheet."""
        app = QApplication.instance()
        if not app:
            return """
                QTableView {
                    background-color: rgba(255, 255, 255, 150);
                    alternate-background-color: rgba(240, 240, 240, 150);
                    border: none;
                    border-radius: 8px;
                    gridline-color: #ddd;
                    selection-background-color: transparent;
                    selection-color: inherit;
                    outline: none;
                }
                QTableView::viewport { border: none; border-radius: 8px; background: transparent; }
                QTableView::item { background-color: transparent; padding: 4px 6px; border: none; outline: none; }
                QTableView::item:selected { background: transparent; color: inherit; }
            """
        
        palette = app.palette()
        base_color = palette.color(QPalette.Base)
        is_dark = is_dark_color(base_color)
        
        if is_dark:
            return f"""
                QTableView {{
                    background-color: rgba({base_color.red()}, {base_color.green()}, {base_color.blue()}, 150);
                    alternate-background-color: rgba({base_color.lighter(110).red()}, {base_color.lighter(110).green()}, {base_color.lighter(110).blue()}, 150);
                    border: none;
                    border-radius: 8px;
                    gridline-color: palette(mid);
                    selection-background-color: transparent;
                    selection-color: inherit;
                    outline: none;
                    color: palette(text);
                }}
                QTableView::viewport {{ border: none; border-radius: 8px; background: transparent; }}
                QTableView::item {{ background-color: transparent; padding: 4px 6px; border: none; outline: none; }}
                QTableView::item:selected {{ background: transparent; color: inherit; }}
            """
        else:
            return """
                QTableView {
                    background-color: rgba(255, 255, 255, 150);
                    alternate-background-color: rgba(240, 240, 240, 150);
                    border: none;
                    border-radius: 8px;
                    gridline-color: #ddd;
                    selection-background-color: transparent;
                    selection-color: inherit;
                    outline: none;
                }
                QTableView::viewport { border: none; border-radius: 8px; background: transparent; }
                QTableView::item { background-color: transparent; padding: 4px 6px; border: none; outline: none; }
                QTableView::item:selected { background: transparent; color: inherit; }
            """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lithe Player")
        self.resize(1100, 700)
        self.setWindowIcon(QIcon(get_asset_path("icon.ico")))

        self.settings = JsonSettings("lithe_player_config.json")
        self.hover_color = None  # Will be set from settings or default

        # Determine theme for search icon
        app = QApplication.instance()
        use_white_icon = False
        if app:
            palette = app.palette()
            base_color = palette.color(QPalette.Base)
            use_white_icon = is_dark_color(base_color)
        
        self.icons = {
            "row_play": QIcon(get_asset_path("plplay.svg")),
            "row_play_white": QIcon(get_asset_path("plplaywhite.svg")),
            "row_pause": QIcon(get_asset_path("plpause.svg")),
            "row_pause_white": QIcon(get_asset_path("plpausewhite.svg")),
            "ctrl_play": get_themed_icon("play.svg"),
            "ctrl_pause": get_themed_icon("pause.svg"),
            "search": QIcon(get_asset_path("searchwhite.svg" if use_white_icon else "search.svg")),
        }

        self._setup_ui()
        self._setup_connections()
        self._setup_vlc_events()
        self._setup_keyboard_shortcuts()

        self.global_media_handler = None
        self.peak_transparency_dialog = None
        self.playlist_font_dialog = None
        self.browser_font_dialog = None
        self.search_results_closed = False  # Track if user closed search dialog
        self._setup_global_media_keys()

        self.restore_settings()

    def _setup_global_media_keys(self):
        try:
            self.global_media_handler = GlobalMediaKeyHandler(self)
            if sys.platform == 'win32':
                QApplication.instance().installNativeEventFilter(self.global_media_handler)
                print("Global media key support enabled")
        except Exception as e:
            print(f"Could not setup global media keys: {e}")

    def _setup_ui(self):
        """Initialize all UI components."""
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(7)
        self.splitter.setStyleSheet("QSplitter::handle { background-color: transparent; }")
        main_layout.addWidget(self.splitter)

        self._setup_left_panel()
        self._setup_right_panel()
        self._setup_bottom_controls(main_layout)
        self._setup_menu_bar()

        default_path = self.settings.value("default_dir", QDir.rootPath())
        self.fs_model.setRootPath(default_path)
        self.tree.setRootIndex(self.fs_model.index(default_path))
        self.update_reset_action_state()
        self._auto_populate_playlist_on_startup(default_path)

    def _setup_left_panel(self):
        """Setup file browser and album art display."""
        self.fs_model = QFileSystemModel()
        self.fs_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)
        
        # Set custom icon provider for directories and audio files
        self.icon_provider = CustomFileIconProvider()
        self.fs_model.setIconProvider(self.icon_provider)

        self.tree = DirectoryTreeView()
        self.tree.setModel(self.fs_model)
        self.tree.setSortingEnabled(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.header().hide()
        self.tree.setAttribute(Qt.WA_StyledBackground, True)
        self.tree.setFrameShape(QTreeView.NoFrame)
        self.tree.setIndentation(15)
        self.tree.setRootIsDecorated(False)

        for col in range(1, self.fs_model.columnCount()):
            self.tree.hideColumn(col)

        self.tree_delegate = DirectoryBrowserDelegate(self.tree, self.tree)
        self.tree.setItemDelegate(self.tree_delegate)
        self.tree.setStyleSheet(self.get_tree_style("#3399ff", "white"))
        self.tree.viewport().setAttribute(Qt.WA_StyledBackground, True)
        self.tree.expanded.connect(self._on_tree_expanded)
        self.tree.doubleClicked.connect(self._on_tree_double_clicked)

        self.album_art = AlbumArtLabel()
        self.album_art.setStyleSheet("QLabel { background: palette(base); border: none; border-radius: 8px; }")

        self.left_splitter = QSplitter(Qt.Vertical)
        self.left_splitter.setHandleWidth(7)
        self.left_splitter.setStyleSheet("QSplitter::handle { background-color: transparent; }")
        self.left_splitter.addWidget(self.tree)
        self.left_splitter.addWidget(self.album_art)
        self.left_splitter.setSizes([400, 200])
        self.left_splitter.setContentsMargins(0, 0, 0, 0)

        self.splitter.addWidget(self.left_splitter)

    def _on_tree_expanded(self, index):
        QTimer.singleShot(0, lambda: self.tree.scrollTo(index, QAbstractItemView.PositionAtCenter))
    
    def _on_tree_double_clicked(self, index):
        """Handle double-click on tree items. If folder is expanded, load it to playlist."""
        if not index.isValid():
            return
        
        path = self.fs_model.filePath(index)
        
        # Handle files
        if os.path.isfile(path):
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS:
                self.controller.stop()
                self.playlist_model.add_tracks([path], clear=True)
                QTimer.singleShot(200, lambda: self.controller.play_index(0))
                self.update_playback_ui()
            return
        
        # Handle directories
        if self.fs_model.isDir(index):
            playlist_is_empty = self.playlist_model.rowCount() == 0
            
            # If playlist is empty, always load the folder and play
            if playlist_is_empty:
                folder_path = self.fs_model.filePath(index)
                files = self._get_audio_files_from_directory(folder_path)
                if files:
                    self.controller.stop()
                    self.playlist_model.add_tracks(files, clear=True)
                    QTimer.singleShot(200, lambda: self.controller.play_index(0))
                    self.update_playback_ui()
                return
            
            # If already expanded, load the folder to playlist and keep it expanded
            if self.tree.isExpanded(index):
                folder_path = self.fs_model.filePath(index)
                files = self._get_audio_files_from_directory(folder_path)
                if files:
                    self.controller.stop()
                    self.playlist_model.add_tracks(files, clear=True)
                    QTimer.singleShot(200, lambda: self.controller.play_index(0))
                    self.update_playback_ui()
                    # Keep folder expanded
                    QTimer.singleShot(0, lambda: self.tree.setExpanded(index, True))
            # If not expanded, it will expand automatically (default behavior)
    
    def _load_folder_to_playlist(self, folder_path):
        """Load all audio files from a folder into the playlist and start playback."""
        import os
        
        # Collect all audio files recursively
        audio_files = []
        audio_extensions = ('.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.wma', '.opus')
        
        for root, dirs, files in os.walk(folder_path):
            for file in sorted(files):
                if file.lower().endswith(audio_extensions):
                    audio_files.append(os.path.join(root, file))
        
        # Add files to playlist (clear=True will clear existing playlist)
        if audio_files:
            self.playlist_model.add_tracks(audio_files, clear=True)
            
            # Start playback from the first track
            self.controller.play_index(0)

    def _setup_right_panel(self):
        """Setup playlist table."""
        playlist_container = QWidget()
        playlist_layout = QVBoxLayout(playlist_container)
        playlist_layout.setContentsMargins(0, 0, 0, 0)

        self.playlist_model = PlaylistModel(controller=None, icons=self.icons)
        self.playlist = PlaylistView(get_asset_path("logo.png"))
        self.playlist.setModel(self.playlist_model)
        self.playlist.setSelectionBehavior(QTableView.SelectRows)
        self.playlist.setAlternatingRowColors(True)
        self.playlist.setIconSize(QSize(16, 16))
        self.playlist.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.playlist.setAttribute(Qt.WA_StyledBackground, True)
        self.playlist.setFrameShape(QTableView.NoFrame)
        self.playlist.setStyleSheet(self.get_playlist_style())
        self.playlist.viewport().setAttribute(Qt.WA_StyledBackground, True)

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
        self.tree.playlist_model = self.playlist_model

    def _setup_bottom_controls(self, parent_layout):
        """Setup playback controls, progress bar, volume, and equalizer."""
        bottom_layout = QVBoxLayout()
        parent_layout.addLayout(bottom_layout)

        controls = QHBoxLayout()
        
        # Add left padding to balance search box on right (adjusted for 5 buttons instead of 4)
        controls.addSpacing(240)
        
        controls.addStretch(1)
        
        self.btn_prev = self._create_button(get_themed_icon("prev.svg"), 24)
        self.btn_playpause = self._create_button(get_themed_icon("play.svg"), 24)
        self.btn_stop = self._create_button(get_themed_icon("stop.svg"), 24)
        self.btn_next = self._create_button(get_themed_icon("next.svg"), 24)
        self.btn_shuffle = self._create_button(get_themed_icon("shuffle.svg"), 24)
        self.btn_shuffle.setToolTip("Shuffle playlist")
        
        for btn in [self.btn_prev, self.btn_playpause, self.btn_stop, self.btn_next, self.btn_shuffle]:
            btn.setStyleSheet(self.get_button_style())
            controls.addWidget(btn)
        
        controls.addStretch(1)
        
        # Search box on the right with spacing
        controls.addSpacing(20)
        self.search_icon_label = QLabel()
        self.search_icon_label.setPixmap(self.icons["search"].pixmap(16, 16))
        controls.addWidget(self.search_icon_label)
        
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search library...")
        self.search_box.setMaximumWidth(200)
        self.search_box.returnPressed.connect(self.on_search)
        self.search_box.setStyleSheet("""
            QLineEdit {
                padding: 4px 10px;
                border: 1px solid palette(mid);
                border-radius: 10px;
                background: palette(base);
                font-size: 9pt;
            }
            QLineEdit:focus {
                border: 1px solid palette(highlight);
            }
        """)
        controls.addWidget(self.search_box)
        
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
        self.menuBar().setStyleSheet("""
            QMenuBar {
                font-size: 9pt;
            }
            QMenu {
                border: 1px solid palette(mid);
                background-color: palette(base);
                font-size: 9pt;
            }
            QMenu::item {
                padding: 4px 20px;
            }
        """)
        
        # File menu
        file_menu = self.menuBar().addMenu("&File")
        
        act_open = QAction("&Open Folder...", self)
        act_open.setStatusTip("Browse and open a music folder")
        act_open.triggered.connect(self.on_add_folder_clicked)
        file_menu.addAction(act_open)
        
        file_menu.addSeparator()
        
        choose_default_act = QAction("Set &Default Folder...", self)
        choose_default_act.setStatusTip("Choose the default startup folder")
        choose_default_act.triggered.connect(self.on_choose_default_folder)
        file_menu.addAction(choose_default_act)

        self.reset_default_act = QAction("&Reset Default Folder", self)
        self.reset_default_act.setStatusTip("Clear the default folder setting")
        self.reset_default_act.triggered.connect(self.on_reset_default_folder)
        file_menu.addAction(self.reset_default_act)
        
        file_menu.addSeparator()
        
        exit_act = QAction("E&xit", self)
        exit_act.setShortcut(QKeySequence("Ctrl+Q"))
        exit_act.setStatusTip("Exit the application")
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        # Appearance menu
        appearance_menu = self.menuBar().addMenu("&Appearance")
        
        # Theme submenu
        theme_submenu = appearance_menu.addMenu("&Theme")
        
        act_color = QAction("&Accent Color...", self)
        act_color.setStatusTip("Customize the accent color")
        act_color.triggered.connect(self.on_choose_highlight_color)
        theme_submenu.addAction(act_color)
        
        act_hover_color = QAction("&Hover Color...", self)
        act_hover_color.setStatusTip("Customize the hover highlight color")
        act_hover_color.triggered.connect(self.on_choose_hover_color)
        theme_submenu.addAction(act_hover_color)
        
        # Equalizer submenu
        equalizer_submenu = appearance_menu.addMenu("&Equalizer")
        
        act_peak_color = QAction("Peak &Color...", self)
        act_peak_color.setStatusTip("Set custom color for peak indicators")
        act_peak_color.triggered.connect(self.on_choose_peak_color)
        equalizer_submenu.addAction(act_peak_color)
        
        self.reset_peak_color_act = QAction("&Reset Peak Color", self)
        self.reset_peak_color_act.setStatusTip("Reset peak color to automatic")
        self.reset_peak_color_act.triggered.connect(self.on_reset_peak_color)
        self.reset_peak_color_act.setEnabled(False)
        equalizer_submenu.addAction(self.reset_peak_color_act)
        
        equalizer_submenu.addSeparator()
        
        act_peak_transparency = QAction("Peak &Transparency...", self)
        act_peak_transparency.setStatusTip("Adjust the transparency of peak indicators")
        act_peak_transparency.triggered.connect(self.on_adjust_peak_transparency)
        equalizer_submenu.addAction(act_peak_transparency)
        
        # Fonts submenu
        fonts_submenu = appearance_menu.addMenu("&Fonts")
        
        act_playlist_font = QAction("&Playlist Font...", self)
        act_playlist_font.setStatusTip("Change the playlist font")
        act_playlist_font.triggered.connect(self.on_set_playlist_font)
        fonts_submenu.addAction(act_playlist_font)
        
        act_browser_font = QAction("&Browser Font...", self)
        act_browser_font.setStatusTip("Change the file browser font")
        act_browser_font.triggered.connect(self.on_set_browser_font)
        fonts_submenu.addAction(act_browser_font)
        
        # Create search results dialog
        self.search_results_dialog = SearchResultsDialog(self)
        self.search_results_dialog.settings = self.settings  # Share settings object
        self.search_results_dialog.set_playlist_model(self.playlist_model)
        self.search_results_dialog.set_controller(self.controller)
        self.search_results_dialog._restore_column_widths()  # Restore saved column widths
        self.search_results_dialog._restore_geometry()  # Restore saved position and size

    def _setup_connections(self):
        """Connect signals and slots."""
        self.btn_playpause.clicked.connect(self.on_playpause_clicked)
        self.btn_stop.clicked.connect(self.on_stop_clicked)
        self.btn_prev.clicked.connect(self.on_prev_clicked)
        self.btn_next.clicked.connect(self.on_next_clicked)
        self.btn_shuffle.clicked.connect(self.on_shuffle_clicked)
        self.slider_vol.valueChanged.connect(self.on_volume_changed)
        self.progress_slider.sliderReleased.connect(self.on_seek)
        self.tree.expanded.connect(self.on_tree_expanded)
        self.playlist.doubleClicked.connect(self.on_playlist_double_click)
        self.on_volume_changed(self.slider_vol.value())

    def _setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts."""
        QShortcut(QKeySequence(Qt.Key_Space), self).activated.connect(self.on_playpause_clicked)
        QShortcut(QKeySequence(Qt.Key_Left), self).activated.connect(self.on_prev_clicked)
        QShortcut(QKeySequence(Qt.Key_Right), self).activated.connect(self.on_next_clicked)
        
        try:
            QShortcut(QKeySequence(Qt.Key_MediaPlay), self).activated.connect(self.on_playpause_clicked)
            QShortcut(QKeySequence(Qt.Key_MediaTogglePlayPause), self).activated.connect(self.on_playpause_clicked)
            QShortcut(QKeySequence(Qt.Key_MediaPause), self).activated.connect(self.on_playpause_clicked)
            QShortcut(QKeySequence(Qt.Key_MediaStop), self).activated.connect(self.on_stop_clicked)
            QShortcut(QKeySequence(Qt.Key_MediaNext), self).activated.connect(self.on_next_clicked)
            QShortcut(QKeySequence(Qt.Key_MediaPrevious), self).activated.connect(self.on_prev_clicked)
        except Exception as e:
            print(f"Some media key shortcuts unavailable: {e}")

    def _setup_vlc_events(self):
        """Setup VLC player event handlers."""
        event_manager = self.controller.player.event_manager()
        event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: self.on_playing())
        event_manager.event_attach(vlc.EventType.MediaPlayerPaused, lambda e: self.on_paused())
        event_manager.event_attach(vlc.EventType.MediaPlayerStopped, lambda e: self.on_stopped())

    def _create_button(self, icon_or_path, icon_size):
        """Helper to create a button with an icon."""
        button = QPushButton()
        if isinstance(icon_or_path, QIcon):
            button.setIcon(icon_or_path)
        else:
            button.setIcon(QIcon(icon_or_path))
        button.setIconSize(QSize(icon_size, icon_size))
        return button

    # Playback event handlers
    def on_playing(self):
        self.update_playback_ui()
        self.update_playpause_icon()

    def on_paused(self):
        self.update_playback_ui()
        self.update_playpause_icon()

    def on_stopped(self):
        self.update_playback_ui()
        self.update_playpause_icon()
        if self.equalizer:
            QTimer.singleShot(0, lambda: self.equalizer.stop(clear_display=True))

    # UI update methods
    def update_album_art(self, filepath):
        pixmap = extract_album_art(filepath)
        if pixmap:
            self.album_art.set_album_pixmap(pixmap)
        else:
            self.album_art.clear()
            self.album_art._original_pixmap = None

    def update_playpause_icon(self):
        if self.controller.player.is_playing():
            self.btn_playpause.setIcon(self.icons["ctrl_pause"])
        else:
            self.btn_playpause.setIcon(self.icons["ctrl_play"])

    def update_playback_ui(self):
        self.playlist.viewport().update()

    def update_slider_colors(self):
        if not self.playlist_model.highlight_color:
            return
        
        color_name = self.playlist_model.highlight_color.name()
        slider_style = f"""
            QSlider::groove:horizontal {{
                border: 1px solid #999;
                height: 6px;
                background: {color_name};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                background: {color_name};
                border: 1px solid #666;
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }}
        """
        self.progress_slider.setStyleSheet(slider_style)
        self.slider_vol.setStyleSheet(slider_style)
        self.equalizer.update_color(self.playlist_model.highlight_color)

    def update_tree_stylesheet(self, color):
        text_color = "white" if is_dark_color(color) else "black"
        self.tree.setStyleSheet(self.get_tree_style(color.name(), text_color, self.hover_color))

    def update_reset_action_state(self):
        self.reset_default_act.setEnabled(self.settings.contains("default_dir"))

    # Playback control handlers
    def on_playpause_clicked(self):
        if self.controller.gapless_manager.is_playing():
            self.controller.pause()
        else:
            if self.playlist_model.rowCount() > 0:
                if self.controller.current_index == -1:
                    self.controller.play_index(0)
                else:
                    self.controller.play()
        self.update_playback_ui()
        self.update_playpause_icon()

    def on_stop_clicked(self):
        self.controller.stop()
        self.update_playback_ui()

    def on_prev_clicked(self):
        self.controller.previous()
        self.update_playback_ui()

    def on_next_clicked(self):
        self.controller.next()
        self.update_playback_ui()
    
    def on_shuffle_clicked(self):
        """Shuffle the current playlist, keeping the currently playing track at the top."""
        import random
        
        row_count = self.playlist_model.rowCount()
        if row_count <= 1:
            return  # Nothing to shuffle
        
        # Get currently playing track index
        current_index = self.controller.current_index
        current_track = None
        
        if current_index >= 0 and current_index < row_count:
            current_track = self.playlist_model.get_filepath(current_index)
        
        # Get all tracks
        tracks = []
        for row in range(row_count):
            filepath = self.playlist_model.get_filepath(row)
            if filepath:
                tracks.append(filepath)
        
        # If there's a playing track, remove it from the list, shuffle the rest, then put it at the top
        if current_track and current_track in tracks:
            tracks.remove(current_track)
            random.shuffle(tracks)
            tracks.insert(0, current_track)
            new_current_index = 0
        else:
            # No playing track, just shuffle everything
            random.shuffle(tracks)
            new_current_index = -1
        
        # Reload playlist with shuffled tracks
        self.playlist_model.add_tracks(tracks, clear=True)
        
        # Update current index
        if new_current_index >= 0:
            self.controller.current_index = new_current_index
            self.playlist_model.set_current_index(new_current_index)

    def on_volume_changed(self, volume):
        self.controller.set_volume(volume)

    def on_seek(self):
        if self.controller.gapless_manager and self.controller.gapless_manager.is_playing():
            length = self.controller.gapless_manager.get_length()
            if length > 0:
                position = self.progress_slider.value() / 1000.0
                self.controller.gapless_manager.set_time(int(length * position))

    def update_progress(self):
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

    @staticmethod
    def format_time(milliseconds):
        seconds = milliseconds // 1000
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d}"

    # File browser handlers
    def on_tree_double_click(self, index):
        path = self.fs_model.filePath(index)
        
        if os.path.isfile(path):
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS:
                self.controller.stop()
                self.playlist_model.add_tracks([path], clear=True)
                QTimer.singleShot(200, lambda: self.controller.play_index(0))
                self.update_playback_ui()
        elif os.path.isdir(path):
            playlist_is_empty = self.playlist_model.rowCount() == 0
            is_expanded = self.tree.isExpanded(index)
            
            # If playlist is empty, always load the folder and play (don't expand)
            if playlist_is_empty:
                files = self._get_audio_files_from_directory(path)
                if files:
                    self.controller.stop()
                    self.playlist_model.add_tracks(files, clear=True)
                    QTimer.singleShot(200, lambda: self.controller.play_index(0))
                    self.update_playback_ui()
                # Prevent default expand behavior by collapsing if it just expanded
                if not is_expanded:
                    QTimer.singleShot(0, lambda: self.tree.collapse(index))
                return
            
            # If folder is not expanded, let it expand (default behavior)
            if not is_expanded:
                return  # Let default expand happen
            
            # Folder is already expanded and playlist has content, so load it into playlist
            files = self._get_audio_files_from_directory(path)
            if files:
                self.controller.stop()
                self.playlist_model.add_tracks(files, clear=True)
                QTimer.singleShot(200, lambda: self.controller.play_index(0))
                self.update_playback_ui()
                # Keep folder expanded - collapse it first, then re-expand to prevent default toggle
                QTimer.singleShot(0, lambda: self.tree.setExpanded(index, True))

    def on_tree_expanded(self, index):
        parent = index.parent()
        for row in range(self.fs_model.rowCount(parent)):
            sibling = self.fs_model.index(row, 0, parent)
            if sibling != index and self.tree.isExpanded(sibling):
                self.tree.collapse(sibling)

    # Playlist handlers
    def on_playlist_double_click(self, index):
        self.controller.play_index(index.row())
        self.update_playback_ui()

    # Menu action handlers
    def on_set_playlist_font(self):
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
        self.playlist.setFont(font)
        self.search_results_dialog.results_tree.setFont(font)  # Apply to search results too
        self.settings.setValue("playlistFontFamily", font.family())
        self.settings.setValue("playlistFontSize", font.pointSize())
        self.playlist.viewport().update()
    
    def on_set_browser_font(self):
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
        self.tree.setFont(font)
        self.settings.setValue("browserFontFamily", font.family())
        self.settings.setValue("browserFontSize", font.pointSize())
        self.tree.viewport().update()
    
    def on_adjust_peak_transparency(self):
        if self.peak_transparency_dialog is None or not self.peak_transparency_dialog.isVisible():
            current_alpha = self.equalizer.peak_alpha
            self.peak_transparency_dialog = PeakTransparencyDialog(current_alpha, self)
            self.peak_transparency_dialog.transparency_changed.connect(self._on_peak_transparency_changed)
        self.peak_transparency_dialog.show()
        self.peak_transparency_dialog.raise_()
        self.peak_transparency_dialog.activateWindow()
    
    def _on_peak_transparency_changed(self, alpha):
        self.equalizer.set_peak_alpha(alpha)
        self.settings.setValue("peakAlpha", alpha)
    
    def on_choose_peak_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.equalizer.set_peak_color(color)
            self.settings.setValue("peakColor", color.name())
            self.reset_peak_color_act.setEnabled(True)
            self.statusBar().showMessage(f"Peak indicator colour set to {color.name()}", 3000)
                
    def on_reset_peak_color(self):
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
        folder = QFileDialog.getExistingDirectory(self, "Choose music folder", QDir.homePath())
        if not folder:
            return
        
        files = self._get_audio_files_from_directory(folder)
        if files:
            self.playlist_model.add_tracks(files, clear=True)
            if self.playlist_model.rowCount() > 0:
                self.controller.play_index(0)
        self.update_playback_ui()

    def on_choose_default_folder(self):
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
        color = QColorDialog.getColor()
        if color.isValid():
            color.setAlpha(100)  # Set transparency for accent color
            self.playlist_model.highlight_color = color
            self.tree_delegate.set_highlight_color(color)
            self.update_tree_stylesheet(color)
            self.settings.setValue("highlightColor", color.name())
            self.update_playback_ui()
            self.update_slider_colors()
            self.equalizer.update_color(color)
    
    def on_choose_hover_color(self):
        """Choose custom hover color for both playlist and file browser."""
        current_color = self.hover_color if self.hover_color else QColor(220, 238, 255)
        color = QColorDialog.getColor(current_color, self, "Choose Hover Color")
        if color.isValid():
            color.setAlpha(100)  # Set transparency for hover color
            self.hover_color = color
            self.settings.setValue("hoverColor", color.name())
            # Update playlist delegate
            self.delegate.set_hover_color(color)
            # Update tree delegate
            self.tree_delegate.set_hover_color(color)
            # Update tree stylesheet
            if self.playlist_model.highlight_color:
                text_color = "white" if is_dark_color(self.playlist_model.highlight_color) else "black"
                self.tree.setStyleSheet(self.get_tree_style(
                    self.playlist_model.highlight_color.name(), 
                    text_color,
                    color
                ))
            self.playlist.viewport().update()
            self.tree.viewport().update()
    
    def on_search(self):
        """Perform search in the default folder."""
        query = self.search_box.text().strip()
        if not query:
            return
        
        default_dir = self.settings.value("default_dir", QDir.rootPath())
        if not os.path.exists(default_dir):
            QMessageBox.warning(self, "Search Error", "Default folder not found. Please set a valid default folder.")
            return
        
        # Prevent multiple simultaneous searches
        if hasattr(self, 'search_worker') and self.search_worker.isRunning():
            return
        
        # Reset the closed flag when starting a new search
        self.search_results_closed = False
        
        # Clear previous search results before starting new search
        self.search_results_dialog.model.set_results([], default_dir)
        
        # Show searching indicator
        self.search_box.setEnabled(False)
        self.search_box.setPlaceholderText("Searching...")
        
        # Start search in background thread
        self.search_worker = SearchWorker(default_dir, query, self)
        self.search_worker.progress.connect(self._on_search_progress)
        self.search_worker.finished.connect(self._on_search_finished)
        self.search_worker.start()
    
    def _on_search_progress(self, batch_results, base_directory):
        """Handle progressive search results."""
        # If user closed the dialog, don't reopen it
        if self.search_results_closed:
            return
        
        # Show dialog on first batch if not already shown
        if not self.search_results_dialog.isVisible():
            self.search_results_dialog.set_colors(
                self.playlist_model.highlight_color, 
                self.hover_color
            )
            self.search_results_dialog.show()
            self.search_results_dialog.raise_()
            self.search_results_dialog.activateWindow()
        
        # Add batch to existing results
        self.search_results_dialog.add_results(batch_results, base_directory)
    
    def _on_search_finished(self, results, base_directory):
        """Handle search completion."""
        # Restore search box
        self.search_box.setEnabled(True)
        self.search_box.setPlaceholderText("Search library...")
        
        # Reset the closed flag for next search
        self.search_results_closed = False

    # Helper methods
    def _get_audio_files_from_directory(self, directory):
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
    
    def _auto_populate_playlist_on_startup(self, directory):
        """Auto-populate playlist if default directory is a flat folder with audio files."""
        if not os.path.isdir(directory):
            return
        
        try:
            has_subdirs = False
            audio_files = []
            
            for name in sorted(os.listdir(directory)):
                path = os.path.join(directory, name)
                if os.path.isdir(path):
                    has_subdirs = True
                    break
                elif os.path.isfile(path):
                    if os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS:
                        audio_files.append(path)
            
            if not has_subdirs and audio_files:
                self.playlist_model.add_tracks(audio_files, clear=True)
                print(f"Auto-populated playlist with {len(audio_files)} tracks")
        except (PermissionError, OSError) as e:
            print(f"Could not auto-populate playlist: {e}")

    # Settings persistence
    def restore_settings(self):
        """Restore saved settings from previous session."""
        # Restore hover color first
        hover_color_name = self.settings.value("hoverColor")
        if hover_color_name:
            hover_color = QColor(hover_color_name)
            if hover_color.isValid():
                hover_color.setAlpha(100)  # Set transparency for hover color
                self.hover_color = hover_color
                self.delegate.set_hover_color(hover_color)
                self.tree_delegate.set_hover_color(hover_color)
        
        color_name = self.settings.value("highlightColor")
        if color_name:
            color = QColor(color_name)
            if color.isValid():
                color.setAlpha(100)  # Set transparency for accent color
                self.playlist_model.highlight_color = color
                self.tree_delegate.set_highlight_color(color)
                self.update_tree_stylesheet(color)
                self.update_slider_colors()
                self.equalizer.update_color(color)
        
        peak_color_name = self.settings.value("peakColor")
        if peak_color_name:
            peak_color = QColor(peak_color_name)
            if peak_color.isValid():
                self.equalizer.set_peak_color(peak_color)
                self.reset_peak_color_act.setEnabled(True)
        else:
            self.reset_peak_color_act.setEnabled(False)
        
        if self.settings.contains("peakAlpha"):
            peak_alpha = int(self.settings.value("peakAlpha"))
            self.equalizer.set_peak_alpha(peak_alpha)

        if self.settings.contains("playlistFontFamily"):
            font_family = self.settings.value("playlistFontFamily")
            font_size = int(self.settings.value("playlistFontSize", 10))
            playlist_font = QFont(font_family, font_size)
            self.playlist.setFont(playlist_font)
            self.search_results_dialog.results_tree.setFont(playlist_font)  # Apply to search results too
        
        if self.settings.contains("browserFontFamily"):
            font_family = self.settings.value("browserFontFamily")
            font_size = int(self.settings.value("browserFontSize", 10))
            self.tree.setFont(QFont(font_family, font_size))

        if self.settings.contains("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        if self.settings.contains("leftSplitterState"):
            self.left_splitter.restoreState(self.settings.value("leftSplitterState"))
        if self.settings.contains("windowState"):
            self.restoreState(self.settings.value("windowState"))
        if self.settings.contains("splitterState"):
            self.splitter.restoreState(self.settings.value("splitterState"))
        if self.settings.contains("playlistHeader"):
            self.playlist.horizontalHeader().restoreState(self.settings.value("playlistHeader"))
        
        if self.settings.contains("volume"):
            vol = int(self.settings.value("volume"))
            self.slider_vol.setValue(vol)
            self.on_volume_changed(vol)
        
        # Restore playlist contents and current track
        self._restore_playlist_state()
    
    def _save_playlist_state(self):
        """Save current playlist contents and playing track."""
        if self.playlist_model.rowCount() == 0:
            # No playlist to save
            self.settings.remove("playlistTracks")
            self.settings.remove("currentTrackIndex")
            return
        
        # Save all track file paths
        track_paths = []
        for row in range(self.playlist_model.rowCount()):
            filepath = self.playlist_model.get_filepath(row)
            if filepath:
                track_paths.append(filepath)
        
        self.settings.setValue("playlistTracks", json.dumps(track_paths))
        
        # Save current track index
        current_index = self.controller.current_index
        if current_index >= 0:
            self.settings.setValue("currentTrackIndex", current_index)
        else:
            self.settings.remove("currentTrackIndex")
    
    def _restore_playlist_state(self):
        """Restore playlist contents and highlight last track from previous session."""
        if not self.settings.contains("playlistTracks"):
            return
        
        try:
            track_paths_json = self.settings.value("playlistTracks")
            track_paths = json.loads(track_paths_json)
            
            # Filter out tracks that no longer exist
            valid_tracks = [path for path in track_paths if os.path.exists(path)]
            
            if not valid_tracks:
                return
            
            # Load tracks into playlist
            self.playlist_model.add_tracks(valid_tracks, clear=True)
            
            # Restore and select the last track index if available
            if self.settings.contains("currentTrackIndex"):
                saved_index = int(self.settings.value("currentTrackIndex"))
                
                # Ensure the index is still valid
                if 0 <= saved_index < self.playlist_model.rowCount():
                    # Set it as the current track in the model (shows with accent color)
                    self.playlist_model.set_current_index(saved_index)
                    self.controller.current_index = saved_index
                    
                    # Also select it in the playlist view
                    self.playlist.selectRow(saved_index)
                    self.playlist.scrollTo(self.playlist_model.index(saved_index, 0))
                    
                    # Update album art for the track
                    filepath = self.playlist_model.get_filepath(saved_index)
                    if filepath:
                        self.update_album_art(filepath)
                    
        except (json.JSONDecodeError, ValueError, Exception) as e:
            print(f"Error restoring playlist: {e}")

    def closeEvent(self, event):
        """Save settings on application close."""
        if self.global_media_handler:
            self.global_media_handler.cleanup()
            if sys.platform == 'win32':
                QApplication.instance().removeNativeEventFilter(self.global_media_handler)
        
        # Save playlist contents and current track
        self._save_playlist_state()
        
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("leftSplitterState", self.left_splitter.saveState())
        self.settings.setValue("windowState", self.saveState())
        self.settings.setValue("splitterState", self.splitter.saveState())
        self.settings.setValue("playlistHeader", self.playlist.horizontalHeader().saveState())
        self.settings.setValue("volume", self.slider_vol.value())
        super().closeEvent(event)

# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================

def main():
    """Main application entry point."""
    if sys.platform == 'win32':
        try:
            myappid = u"litheplayer.audio.app"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("Lithe Player")
    app.setWindowIcon(QIcon(get_asset_path("icon.ico")))

    splash_pix = QPixmap(get_asset_path("splash.png"))
    splash = QSplashScreen(splash_pix)
    splash.show()
    app.processEvents()

    window = MainWindow()

    def show_main_window():
        splash.finish(window)
        window.show()
    
    QTimer.singleShot(SPLASH_SCREEN_DURATION_MS, show_main_window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

