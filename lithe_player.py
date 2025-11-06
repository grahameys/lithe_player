import sys
import os
import json
import numpy as np
import threading
import time
import ctypes

from PySide6.QtCore import (
    Qt, QDir, QAbstractTableModel, QModelIndex, QSettings, QSize, QTimer, 
    QRect, QEvent, QAbstractNativeEventFilter
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

import vlc
import soundfile as sf
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

# ============================================================================
# CONFIGURATION
# ============================================================================

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg'}
CONFIG_FILE = "config.json"
ALBUM_ART_SIZE = 200
DEFAULT_ANALYSIS_RATE = 44100
ANALYSIS_CHUNK_SAMPLES = 2048

# ============================================================================
# VLC ENVIRONMENT SETUP
# ============================================================================

def setup_vlc_environment():
    """Configure VLC to use local plugin libraries from the 'plugins' subfolder."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    plugins_dir = os.path.join(script_dir, "plugins")
    
    if not os.path.exists(plugins_dir):
        print(f"Warning: plugins directory not found at {plugins_dir}")
        print("VLC will attempt to use system-installed libraries.")
        return None
    
    os.environ['VLC_PLUGIN_PATH'] = plugins_dir
    print(f"VLC plugin path set to: {plugins_dir}")
    
    # Windows-specific PATH setup
    if sys.platform == 'win32':
        os.environ['PATH'] = plugins_dir + os.pathsep + os.environ.get('PATH', '')
        
        parent_dir = os.path.dirname(plugins_dir)
        if os.path.exists(os.path.join(parent_dir, 'libvlc.dll')):
            os.environ['PATH'] = parent_dir + os.pathsep + os.environ['PATH']
            print(f"Added to PATH: {parent_dir}")
    
    return plugins_dir

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def load_config():
    """Load configuration from JSON file."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_config(cfg):
    """Save configuration to JSON file."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def is_dark_color(color: QColor) -> bool:
    """Determine if a color is dark based on perceived brightness."""
    brightness = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return brightness < 128

def extract_album_art(filepath):
    """Extract album art from audio file."""
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
    """FFT-driven equalizer fed by a background SoundFile decoder thread."""

    def __init__(self, bar_count=40, segments=15, parent=None):
        super().__init__(parent)
        self.bar_count = bar_count
        self.segments = segments
        self.levels = [0] * bar_count
        self.color = QColor("#00cc66")
        self.buffer_size = ANALYSIS_CHUNK_SAMPLES
        self.sample_buffer = np.zeros(self.buffer_size, dtype=np.float32)
        self._band_ema_max = [1e-6] * bar_count
        self._decoder_thread = None
        self._decoder_running = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_from_fft)

    def start(self, filepath):
        """Start the equalizer decoder and animation."""
        self.stop()
        self._decoder_running = True
        self._decoder_thread = threading.Thread(
            target=self._decode_loop, args=(filepath,), daemon=True
        )
        self._decoder_thread.start()
        self.timer.start(30)

    def stop(self, clear_display=True):
        """Stop the equalizer decoder and animation."""
        self._decoder_running = False
        if self._decoder_thread and self._decoder_thread.is_alive():
            self._decoder_thread.join(timeout=0.5)
        self._decoder_thread = None
        self.timer.stop()
        if clear_display:
            self.levels = [0] * self.bar_count
            self.update()

    def _decode_loop(self, filepath):
        """Background thread for audio decoding."""
        try:
            with sf.SoundFile(filepath) as f:
                while self._decoder_running:
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
                    time.sleep(len(samples) / DEFAULT_ANALYSIS_RATE)
        except Exception as e:
            print(f"Decoder thread error: {e}")

    def update_from_fft(self):
        """Update equalizer bars from FFT analysis."""
        # Perform FFT with Hanning window
        fft = np.fft.rfft(self.sample_buffer * np.hanning(len(self.sample_buffer)))
        magnitude = np.abs(fft)

        # Filter frequency range
        freqs_hz = np.fft.rfftfreq(len(self.sample_buffer), 1.0 / DEFAULT_ANALYSIS_RATE)
        mask = (freqs_hz >= 60) & (freqs_hz <= 17000)
        magnitude = magnitude[mask]

        # Calculate bar values
        bars_raw = self._calculate_bar_values(magnitude)
        
        # Normalize and scale bars
        bars_norm = self._normalize_bars(bars_raw)
        
        self.levels = [max(0, min(self.segments, v)) for v in bars_norm]
        self.update()

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
        decay = 0.97
        eps = 1e-6
        bars_norm = []
        
        for i, val in enumerate(bars_raw):
            # Update EMA max
            ema_candidate = self._band_ema_max[i] * decay
            self._band_ema_max[i] = max(val, ema_candidate)
            
            # Normalize
            norm = val / (self._band_ema_max[i] + eps)
            
            # Apply frequency-dependent boost
            hf_tilt = 1.0 + 0.3 * (i / max(1, self.bar_count - 1))
            norm *= hf_tilt
            if i < 2:
                norm *= 1.2
            
            # Scale to segments
            scaled = norm * (self.segments * 0.9)
            bars_norm.append(int(scaled))
        
        return bars_norm

    def update_color(self, color: QColor):
        """Update equalizer color."""
        if color:
            self.color = color
            self.update()

    def paintEvent(self, event):
        """Paint the equalizer bars."""
        painter = QPainter(self)
        bar_width = self.width() / self.bar_count
        segment_height = self.height() / self.segments
        
        for i, level in enumerate(self.levels):
            for seg in range(level):
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
        """Get the appropriate playback icon based on state."""
        if not self.controller:
            return None
        
        is_dark = self.highlight_color and is_dark_color(self.highlight_color)
        is_playing = self.controller.player.is_playing()
        
        if is_playing:
            return self.icons.get("row_play_white" if is_dark else "row_play")
        else:
            return self.icons.get("row_pause_white" if is_dark else "row_pause")

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
    """Controller for VLC audio playback."""

    def __init__(self, view=None, eq_widget=None):
        plugins_dir = setup_vlc_environment()
        
        # Create VLC instance
        if plugins_dir:
            try:
                self.instance = vlc.Instance(f'--plugin-path={plugins_dir}')
                print("VLC instance created with local plugins")
            except Exception as e:
                print(f"Failed to create VLC instance with local plugins: {e}")
                print("Falling back to system VLC installation")
                self.instance = vlc.Instance()
        else:
            self.instance = vlc.Instance()
            print("VLC instance created using system installation")
        
        self.player = self.instance.media_player_new()
        self.current_index = -1
        self.model = None
        self.view = view
        self.eq_widget = eq_widget

    def set_model(self, model):
        """Set the playlist model."""
        self.model = model

    def set_view(self, view):
        """Set the playlist view."""
        self.view = view

    def set_equalizer(self, eq_widget):
        """Set the equalizer widget."""
        self.eq_widget = eq_widget

    def play_index(self, index):
        """Play the track at the given index."""
        if not self.model:
            return
        
        path = self.model.path_at(index)
        if not path:
            return
        
        media = self.instance.media_new(path)
        self.player.set_media(media)
        self.player.play()
        self.current_index = index
        self.model.set_current_index(index)
        
        if self.view:
            self.view.clearSelection()
            self.view.selectRow(index)
            self.view.viewport().update()
        
        # Update album art
        main_window = self.view.window()
        if hasattr(main_window, "update_album_art"):
            main_window.update_album_art(path)
        
        # Update window title
        if main_window:
            track = self.model._tracks[index]
            artist = track.get("artist", "Unknown Artist")
            title = track.get("title", "Unknown Track")
            main_window.setWindowTitle(f"{artist} - {title}")
        
        # Start equalizer
        if self.eq_widget:
            self.eq_widget.start(path)

    def pause(self):
        """Pause playback and freeze the equalizer."""
        self.player.pause()
        if self.eq_widget:
            # Stop the timer but don't clear the display
            self.eq_widget.timer.stop()

    def play(self):
        """Resume playback and restart the equalizer."""
        self.player.play()
        if self.eq_widget and self.model and self.current_index >= 0:
            path = self.model.path_at(self.current_index)
            if path:
                # If decoder is already running, just restart the timer
                if self.eq_widget._decoder_running:
                    self.eq_widget.timer.start(30)
                else:
                    # Otherwise start fresh
                    self.eq_widget.start(path)

    def stop(self):
        """Stop playback and clear the equalizer."""
        self.player.stop()
        if self.eq_widget:
            self.eq_widget.stop(clear_display=True)

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
        self.player.audio_set_volume(volume)

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

        # Currently playing row
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

        # Hovered row
        if index.row() == self.hover_row:
            painter.save()
            painter.fillRect(opt.rect, QColor(220, 238, 255, 100))
            painter.restore()

        # Selected but not playing
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

    def __init__(self, logo_path="assets/logo.png", parent=None):
        super().__init__(parent)
        self.logo = QPixmap(logo_path)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        """Handle mouse move events for hover tracking."""
        index = self.indexAt(event.pos())
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
            index = self.indexAt(event.pos())
            if index.isValid():
                # If the clicked row is already selected, deselect it
                if self.selectionModel().isSelected(index):
                    self.clearSelection()
                    return
        
        # Otherwise, handle normally
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

# ============================================================================
# GLOBAL MEDIA KEY HANDLER
# ============================================================================

# Windows-specific imports
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
            # Get the window handle
            self.hwnd = int(self.main_window.winId())
            
            # Define hotkey IDs
            self.HOTKEY_PLAY_PAUSE = 1
            self.HOTKEY_STOP = 2
            self.HOTKEY_NEXT = 3
            self.HOTKEY_PREV = 4
            
            # VK codes for media keys
            VK_MEDIA_PLAY_PAUSE = 0xB3
            VK_MEDIA_STOP = 0xB2
            VK_MEDIA_NEXT_TRACK = 0xB0
            VK_MEDIA_PREV_TRACK = 0xB1
            
            user32 = ctypes.windll.user32
            
            # Register hotkeys
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
        try:
            # macOS requires special permissions and framework integration
            # This is a placeholder for potential future implementation
            print("macOS media key support requires additional configuration")
            print("Using application-level shortcuts instead")
        except Exception as e:
            print(f"Failed to setup macOS media keys: {e}")
    
    def _setup_linux_handler(self):
        """Setup Linux media key handling using DBus (MPRIS)."""
        try:
            # Try to setup MPRIS D-Bus interface
            print("Linux media key support via MPRIS not fully implemented")
            print("Using application-level shortcuts instead")
            
        except Exception as e:
            print(f"Failed to setup Linux media keys: {e}")
    
    def nativeEventFilter(self, eventType, message):
        """Handle native events for Windows."""
        if sys.platform == 'win32':
            try:
                # Windows message constant
                WM_HOTKEY = 0x0312
                
                # On Windows, check for WM_HOTKEY messages
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
# MAIN WINDOW
# ============================================================================

class MainWindow(QMainWindow):
    """Main application window."""

    # Stylesheet constants
    TREE_STYLE_TEMPLATE = """
        QTreeView {{
            background-color: #fafafa;
            alternate-background-color: #f0f0f0;
            border: none;
        }}
        QTreeView::item {{
            padding: 1px 4px;
            min-height: 18px;
            border: none;
            outline: none;
        }}
        QTreeView::item:hover {{
            background: #dceeff;
            color: black;
            border: none;
            outline: none;
            border-radius: 0px;
        }}
        QTreeView::item:selected {{
            background: {color};
            color: {text_color};
            border: 1px solid transparent;
            outline: transparent;
            border-radius: 0px;
        }}
        QTreeView::item:selected:hover,
        QTreeView::item:selected:active,
        QTreeView::item:selected:!active,
        QTreeView::item:selected:pressed {{
            background: {color};
            color: {text_color};
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
        }}
        QTreeView::branch:has-siblings:!adjoins-item,
        QTreeView::branch:has-siblings:adjoins-item,
        QTreeView::branch:!has-children:!has-siblings:adjoins-item {{
            border-image: none;
            image: none;
        }}
        QTreeView::branch:has-children:!has-siblings:closed,
        QTreeView::branch:closed:has-children:has-siblings {{
            border-image: none;
            image: url(assets/branch-closed.png);
        }}
        QTreeView::branch:open:has-children:!has-siblings,
        QTreeView::branch:open:has-children:has-siblings {{
            border-image: none;
            image: url(assets/branch-open.png);
        }}
    """

    BUTTON_STYLE = """
        QPushButton {
            background-color: #f0f0f0;
            border: none;
            border-radius: 6px;
            padding: 6px;
        }
        QPushButton:hover { background-color: #e0e0e0; }
        QPushButton:pressed { background-color: #d0d0d0; }
    """

    SLIDER_STYLE = """
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

    PLAYLIST_STYLE = """
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
        QTableView::item:hover {
            background: rgba(220, 238, 255, 50);
            color: black;
        }
        QTableView::item:selected {
            background: transparent;
            color: inherit;
            border: none;
            outline: none;
        }
        QTableView::item:selected:hover {
            background: rgba(220, 238, 255, 50);
            color: black;
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
        self.setWindowIcon(QIcon("assets/icon.ico"))

        # Load configuration
        self.config = load_config()
        self.settings = QSettings("LithePlayer", "AudioPlayer")

        # Load icons
        self.icons = {
            "row_play": QIcon("assets/plplay.svg"),
            "row_play_white": QIcon("assets/plplaywhite.svg"),
            "row_pause": QIcon("assets/plpause.svg"),
            "row_pause_white": QIcon("assets/plpausewhite.svg"),
            "ctrl_play": QIcon("assets/play.svg"),
            "ctrl_pause": QIcon("assets/pause.svg"),
        }

        # Setup UI
        self._setup_ui()
        self._setup_connections()
        self._setup_vlc_events()
        self._setup_keyboard_shortcuts()

        # Setup global media key handler
        self.global_media_handler = None
        self._setup_global_media_keys()

        # Restore previous session
        self.restore_settings()

    def _setup_global_media_keys(self):
        """Setup global media key handling."""
        try:
            self.global_media_handler = GlobalMediaKeyHandler(self)
            
            # Install the native event filter for Windows
            if sys.platform == 'win32':
                QApplication.instance().installNativeEventFilter(self.global_media_handler)
                print("Global media key support enabled")
            else:
                print("Global media keys available for application focus only")
                
        except Exception as e:
            print(f"Could not setup global media keys: {e}")
            print("Falling back to application-level shortcuts")

    # ========================================================================
    # UI SETUP
    # ========================================================================

    def _setup_ui(self):
        """Initialize all UI components."""
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Main horizontal splitter
        self.splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(self.splitter)

        # Setup panels
        self._setup_left_panel()
        self._setup_right_panel()
        self._setup_bottom_controls(main_layout)
        self._setup_menu_bar()

        # Set initial file browser path
        default_path = self.config.get("default_dir", QDir.rootPath())
        self.fs_model.setRootPath(default_path)
        self.tree.setRootIndex(self.fs_model.index(default_path))
        self.update_reset_action_state()

    def _setup_left_panel(self):
        """Setup file browser and album art display."""
        # File system model
        self.fs_model = QFileSystemModel()
        self.fs_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)

        # Tree view
        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        self.tree.setSortingEnabled(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.header().hide()

        # Hide extra columns
        for col in range(1, self.fs_model.columnCount()):
            self.tree.hideColumn(col)

        # Custom delegate
        self.tree_delegate = DirectoryBrowserDelegate(self.tree, self.tree)
        self.tree.setItemDelegate(self.tree_delegate)
        self.tree.setStyleSheet(self.TREE_STYLE_TEMPLATE.format(
            color="#3399ff", text_color="white"
        ))
        self.tree.expanded.connect(self._on_tree_expanded)

        # Album art
        self.album_art = AlbumArtLabel()
        self.album_art.setStyleSheet("""
            QLabel {
                background: #fafafa;
                border: 1px solid #ccc;
            }
        """)

        # Vertical splitter
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

        # Playlist model and view
        self.playlist_model = PlaylistModel(controller=None, icons=self.icons)
        self.playlist = PlaylistView("assets/logo.png")
        self.playlist.setModel(self.playlist_model)
        self.playlist.setSelectionBehavior(QTableView.SelectRows)
        self.playlist.setSelectionMode(QTableView.SingleSelection)
        self.playlist.setAlternatingRowColors(True)
        self.playlist.setIconSize(QSize(16, 16))
        self.playlist.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.playlist.setStyleSheet(self.PLAYLIST_STYLE)

        # Header configuration
        header = self.playlist.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(False)
        header.setSectionsClickable(False)
        for col in range(len(PlaylistModel.HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)

        # Delegate
        self.delegate = PlayingRowDelegate(self.playlist_model, self.playlist)
        self.playlist.setItemDelegate(self.delegate)

        playlist_layout.addWidget(self.playlist)
        self.splitter.addWidget(playlist_container)

        # Audio controller
        self.controller = AudioPlayerController(self.playlist)
        self.controller.set_model(self.playlist_model)
        self.playlist_model.controller = self.controller

    def _setup_bottom_controls(self, parent_layout):
        """Setup playback controls, progress bar, volume, and equalizer."""
        bottom_layout = QVBoxLayout()
        parent_layout.addLayout(bottom_layout)

        # Playback controls
        controls = QHBoxLayout()
        controls.addStretch(1)
        
        self.btn_prev = self._create_button("assets/prev.svg", 24)
        self.btn_playpause = self._create_button("assets/play.svg", 24)
        self.btn_stop = self._create_button("assets/stop.svg", 24)
        self.btn_next = self._create_button("assets/next.svg", 24)
        
        for btn in [self.btn_prev, self.btn_playpause, self.btn_stop, self.btn_next]:
            btn.setStyleSheet(self.BUTTON_STYLE)
            controls.addWidget(btn)
        
        controls.addStretch(1)
        bottom_layout.addLayout(controls)

        # Progress and volume row
        progress_row = QHBoxLayout()
        
        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.setStyleSheet(self.SLIDER_STYLE)
        
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
        self.slider_vol.setStyleSheet(self.SLIDER_STYLE)
        
        progress_row.addWidget(self.lbl_vol)
        progress_row.addWidget(self.slider_vol)
        bottom_layout.addLayout(progress_row)

        # Equalizer
        self.equalizer = EqualizerWidget(bar_count=70, segments=15)
        self.equalizer.setFixedHeight(120)
        bottom_layout.addWidget(self.equalizer)

        self.controller.set_equalizer(self.equalizer)

        # Progress update timer
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.update_progress)
        self.timer.start()

    def _setup_menu_bar(self):
        """Setup application menu bar."""
        # File menu
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

        # View menu
        view_menu = self.menuBar().addMenu("&View")
        act_color = QAction("Set accent colour", self)
        act_color.triggered.connect(self.on_choose_highlight_color)
        view_menu.addAction(act_color)

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
        
        # Set initial volume
        self.on_volume_changed(self.slider_vol.value())

    def _setup_media_keys(self):
        """Setup media key shortcuts."""
        # Media Play/Pause
        try:
            play_pause_shortcut = QShortcut(QKeySequence(Qt.Key_MediaPlay), self)
            play_pause_shortcut.activated.connect(self.on_playpause_clicked)
            
            toggle_shortcut = QShortcut(QKeySequence(Qt.Key_MediaTogglePlayPause), self)
            toggle_shortcut.activated.connect(self.on_playpause_clicked)
            
            pause_shortcut = QShortcut(QKeySequence(Qt.Key_MediaPause), self)
            pause_shortcut.activated.connect(self.on_playpause_clicked)
        except Exception as e:
            print(f"Media play/pause key setup failed: {e}")
        
        # Media Stop
        try:
            stop_shortcut = QShortcut(QKeySequence(Qt.Key_MediaStop), self)
            stop_shortcut.activated.connect(self.on_stop_clicked)
        except Exception as e:
            print(f"Media stop key setup failed: {e}")
        
        # Media Next
        try:
            next_shortcut = QShortcut(QKeySequence(Qt.Key_MediaNext), self)
            next_shortcut.activated.connect(self.on_next_clicked)
        except Exception as e:
            print(f"Media next key setup failed: {e}")
        
        # Media Previous
        try:
            prev_shortcut = QShortcut(QKeySequence(Qt.Key_MediaPrevious), self)
            prev_shortcut.activated.connect(self.on_prev_clicked)
        except Exception as e:
            print(f"Media previous key setup failed: {e}")
        
        print("Media key shortcuts configured")

    def _setup_keyboard_shortcuts(self):
        """Setup additional keyboard shortcuts (application-level)."""
        # Space bar for play/pause
        space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        space_shortcut.activated.connect(self.on_playpause_clicked)
        
        # Arrow keys for previous/next
        left_shortcut = QShortcut(QKeySequence(Qt.Key_Left), self)
        left_shortcut.activated.connect(self.on_prev_clicked)
        
        right_shortcut = QShortcut(QKeySequence(Qt.Key_Right), self)
        right_shortcut.activated.connect(self.on_next_clicked)
        
        # Standard media keys (when app has focus)
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
        event_manager.event_attach(vlc.EventType.MediaPlayerEndReached, 
                                   lambda e: self.on_stopped())

    def _create_button(self, icon_path, icon_size):
        """Helper to create a button with an icon."""
        button = QPushButton()
        button.setIcon(QIcon(icon_path))
        button.setIconSize(QSize(icon_size, icon_size))
        return button

    # ========================================================================
    # PLAYBACK EVENT HANDLERS
    # ========================================================================

    def on_playing(self):
        """Handle playing event."""
        self.update_playback_ui()
        self.update_playpause_icon()

    def on_paused(self):
        """Handle paused event."""
        self.update_playback_ui()
        self.update_playpause_icon()
        # Don't stop the equalizer on pause - it will freeze automatically

    def on_stopped(self):
        """Handle stopped event."""
        self.update_playback_ui()
        self.update_playpause_icon()
        if self.equalizer:
            self.equalizer.stop(clear_display=True)

    # ========================================================================
    # UI UPDATE METHODS
    # ========================================================================

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
            self.TREE_STYLE_TEMPLATE.format(
                color=color.name(), 
                text_color=text_color
            )
        )

    def update_reset_action_state(self):
        """Enable/disable reset default folder action."""
        self.reset_default_act.setEnabled("default_dir" in self.config)

    # ========================================================================
    # PLAYBACK CONTROL HANDLERS
    # ========================================================================

    def on_playpause_clicked(self):
        """Handle play/pause button click."""
        if self.controller.player.is_playing():
            self.controller.pause()
        else:
            if self.playlist_model.rowCount() > 0 and self.controller.current_index == -1:
                self.controller.play_index(0)
            else:
                self.controller.play()
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
        if not self.controller.player:
            return
        
        length = self.controller.player.get_length()
        current = self.controller.player.get_time()
        
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
        """Format time from milliseconds to MM:SS."""
        seconds = milliseconds // 1000
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d}"

    # ========================================================================
    # FILE BROWSER HANDLERS
    # ========================================================================

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

    # ========================================================================
    # PLAYLIST HANDLERS
    # ========================================================================

    def on_playlist_double_click(self, index):
        """Handle double-click on playlist."""
        self.controller.play_index(index.row())
        self.update_playback_ui()

    # ========================================================================
    # MENU ACTION HANDLERS
    # ========================================================================

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
            self.config["default_dir"] = folder
            save_config(self.config)
            self.fs_model.setRootPath(folder)
            self.tree.setRootIndex(self.fs_model.index(folder))
            self.statusBar().showMessage(f"Default folder set to {folder}", 3000)
            self.update_reset_action_state()

    def on_reset_default_folder(self):
        """Handle 'Reset default folder' menu action."""
        if "default_dir" not in self.config:
            self.statusBar().showMessage("No default folder set", 3000)
            return
        
        reply = QMessageBox.question(
            self, "Reset Default Folder",
            "Are you sure you want to reset the default folder?\n\n"
            "This will revert the browser to showing all drives.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            del self.config["default_dir"]
            save_config(self.config)
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

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

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

    # ========================================================================
    # SETTINGS PERSISTENCE
    # ========================================================================

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
    app.setWindowIcon(QIcon("assets/icon.ico"))

    # Create splash screen
    splash_pix = QPixmap("assets/splash.png")
    splash = QSplashScreen(splash_pix)
    splash.show()
    app.processEvents()

    # Create main window
    window = MainWindow()

    # Show main window after splash delay
    QTimer.singleShot(3000, lambda: (splash.finish(window), window.show()))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()