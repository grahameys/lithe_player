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
    Qt, QDir, QAbstractTableModel, QModelIndex, QSize, QTimer, 
    QRect, QEvent, QAbstractNativeEventFilter, QObject, Signal, QThread,
    QByteArray, QUrl, QMimeData, QRectF
)
from PySide6.QtGui import (
    QAction, QFont, QColor, QIcon, QPalette, QPixmap, QPainter, 
    QKeySequence, QShortcut, QImage, QRegion, QPainterPath, QDrag
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QTreeView, QTableView,
    QVBoxLayout, QHBoxLayout, QPushButton, QFileSystemModel, QHeaderView,
    QLabel, QSlider, QFileDialog, QMessageBox, QColorDialog,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QSizePolicy,
    QAbstractItemView, QSplashScreen, QMenu, QFontComboBox
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
SPLASH_SCREEN_DURATION_MS = 3000

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
            self.signals.stop_equalizer.emit()
    
    def resume(self):
        if self.active_player and self.current_track_path:
            if not self.active_player.is_playing():
                self.active_player.play()
                self._start_monitoring()
                self.signals.start_equalizer.emit(self.current_track_path)
    
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
        if self._decoder_running:
            self._decoder_running = False
            self._stop_decoder.set()
            self._decoder_thread = None
        
        self._stop_decoder.clear()
        self._decoder_running = True
        self._decoder_thread = threading.Thread(
            target=self._decode_loop, args=(filepath,), daemon=True
        )
        self._decoder_thread.start()
        
        if not self.timer.isActive():
            self.timer.start(EQUALIZER_UPDATE_INTERVAL_MS)

    def stop(self, clear_display=True):
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

    def _decode_loop(self, filepath):
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
                    
                    sleep_time = len(samples) / DEFAULT_ANALYSIS_RATE
                    elapsed = 0
                    while elapsed < sleep_time and not self._stop_decoder.is_set():
                        time.sleep(0.01)
                        elapsed += 0.01
                        
        except Exception as e:
            print(f"Decoder thread error: {e}")

    def update_from_fft(self):
        if not self._decoder_running:
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
        self.gapless_manager.signals.track_changed.connect(self._on_gapless_track_change)
        self.gapless_manager.signals.start_equalizer.connect(self._start_equalizer)
        self.gapless_manager.signals.stop_equalizer.connect(self._stop_equalizer)
        
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
            self.eq_widget.timer.stop()

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
                        main_window.setWindowTitle(f"{artist} - {title}")
                    
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
            main_window.setWindowTitle(f"{artist} - {title}")

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

    def set_hover_row(self, row):
        if self.hover_row != row:
            self.hover_row = row
            if self.parent():
                self.parent().viewport().update()

    def paint(self, painter, option, index):
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

        super().paint(painter, opt, index)

class DirectoryBrowserDelegate(QStyledItemDelegate):
    """Custom delegate for directory browser highlighting."""

    def __init__(self, tree_view, parent=None):
        super().__init__(parent)
        self.tree_view = tree_view
        self.highlight_color = None
    
    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        
        model = index.model()
        is_directory = model.isDir(index)
        
        if not is_directory and self.tree_view:
            indentation = self.tree_view.indentation()
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
            if controller:
                controller.stop()
            self.playlist_model.add_tracks(paths, clear=True)
            if controller and len(paths) > 0:
                QTimer.singleShot(50, lambda: self._start_playback_after_overwrite(controller))
    
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
                    super().mousePressEvent(event)
                else:
                    self._drag_selecting = True
                    self._drag_start_row = index.row()
                    self.clearSelection()
                    self.selectRow(index.row())
            else:
                self.clearSelection()
                super().mousePressEvent(event)
        else:
            super().mousePressEvent(event)

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
    
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_selecting = False
            self._drag_start_row = -1
        super().mouseReleaseEvent(event)

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

# [Continued in next message - part 2...]
