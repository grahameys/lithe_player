================================================================================
                           LITHE PLAYER v1.0
                A Modern Audio Player with FFT Equalizer
================================================================================

Author: grahameys
Date: 2025-11-07
License: MIT

================================================================================
                              OVERVIEW
================================================================================

Lithe Player is a modern, lightweight audio player built with Python and 
PySide6, featuring:

  * Gapless playback with dual-player architecture
  * Real-time FFT equalizer visualization (70 bands)
  * Multi-format support (MP3, FLAC, WAV, M4A, AAC, OGG)
  * Album art display with metadata extraction
  * Customizable colors and fonts
  * Global media key support (Windows)
  * Intuitive file browser and playlist management

================================================================================
                            REQUIREMENTS
================================================================================

Software Requirements:
  - Python 3.8 or higher
  - VLC Media Player (3.0.0 or higher)

Python Dependencies (see requirements.txt):
  - PySide6 >= 6.4.0
  - python-vlc >= 3.0.0
  - soundfile >= 0.12.0
  - numpy >= 1.24.0
  - mutagen >= 1.47.0

================================================================================
                         QUICK START GUIDE
================================================================================

1. INSTALLATION

   a) Install Python dependencies:
      
      pip install -r requirements.txt

   b) Install VLC Media Player:
      
      Windows: Download from https://www.videolan.org/vlc/
      macOS:   brew install vlc
      Linux:   sudo apt-get install vlc libvlc-dev

2. RUNNING THE APPLICATION

   python lithe_player.py

3. BASIC USAGE

   - Use the left panel to browse your music folders
   - Double-click a folder to load all audio files
   - Double-click a track in the playlist to play it
   - Use the control buttons or keyboard shortcuts to control playback

================================================================================
                         KEYBOARD SHORTCUTS
================================================================================

  Space                    Play/Pause
  Left Arrow               Previous Track
  Right Arrow              Next Track
  Media Play/Pause         Toggle Playback
  Media Stop               Stop Playback
  Media Next               Next Track
  Media Previous           Previous Track

================================================================================
                      PORTABLE DEPLOYMENT (WINDOWS)
================================================================================

For creating a standalone version without requiring system VLC installation:

STEP 1: Create a "plugins" folder in the same directory as lithe_player.py

STEP 2: Copy the following files from your VLC installation 
        (typically C:\Program Files\VideoLAN\VLC\) to the plugins folder:

  CORE LIBRARIES (from VLC root directory):
    - libvlc.dll
    - libvlccore.dll

  CODEC PLUGINS (from VLC\plugins\codec\):
    - libavcodec_plugin.dll
    - libfaad_plugin.dll
    - libflac_plugin.dll
    - libmpg123_plugin.dll
    - libopus_plugin.dll
    - libtaglib_plugin.dll

  DEMUXER PLUGINS (from VLC\plugins\demux\):
    - libflacsys_plugin.dll
    - libmp4_plugin.dll
    - libogg_plugin.dll
    - librawaud_plugin.dll
    - libwav_plugin.dll

  ACCESS PLUGINS (from VLC\plugins\access\):
    - libfilesystem_plugin.dll

  AUDIO OUTPUT PLUGINS (from VLC\plugins\audio_output\):
    - libdirectsound_plugin.dll
    - libwaveout_plugin.dll

  MISCELLANEOUS (from VLC\plugins\logger\):
    - liblogger_plugin.dll

  TOTAL: 19 files (~24 MB)

STEP 3: Run the application - it will automatically use the local plugins

AUTOMATED SCRIPT: Use the included PowerShell script "copy-vlc-plugins.ps1"
                  to automate the file copying process.

================================================================================
                           PROJECT STRUCTURE
================================================================================

lithe-player/
│
├── lithe_player.py          # Main application file
├── requirements.txt          # Python dependencies
├── README.md                 # Full documentation (Markdown)
├── readme.txt                # This file (Plain text)
│
├── assets/                   # UI resources
│   ├── icon.ico             # Application icon
│   ├── logo.png             # Logo watermark
│   ├── splash.png           # Splash screen
│   └── ... (various icons)
│
└── plugins/                  # Optional: VLC plugins for portable deployment
    ├── libvlc.dll
    ├── libvlccore.dll
    └── ... (17 plugin DLLs)

================================================================================
                             FEATURES
================================================================================

AUDIO PLAYBACK:
  - Gapless playback using dual VLC players
  - Seamless track transitions (500ms pre-trigger)
  - Multi-format support (MP3, FLAC, WAV, M4A, AAC, OGG)
  - Smart preloading of next track

VISUALIZATION:
  - 70-band FFT equalizer (60Hz to 17kHz)
  - Physics-based animation with gravity effects
  - Peak hold indicators with customizable colors
  - Adjustable peak transparency (0-100%)

CUSTOMIZATION:
  - Custom accent colors for highlights and equalizer
  - Custom peak indicator colors
  - Adjustable fonts for playlist and file browser
  - Persistent window layout and settings

CONTROLS:
  - Intuitive file browser with album art preview
  - Drag-and-drop playlist management
  - Progress seeking and volume control
  - Global media key support (Windows)

================================================================================
                          CUSTOMIZATION
================================================================================

ACCESS CUSTOMIZATION VIA THE MENU BAR:

File Menu:
  - Open folder              Load a directory of music files
  - Choose default folder    Set startup location for file browser
  - Reset default folder     Return to system root view

View Menu:
  - Set accent colour        Change highlight color throughout the app
  - Set peak indicator colour        Custom color for equalizer peaks
  - Adjust peak indicator transparency    Control peak visibility
  - Set playlist font        Customize playlist typography
  - Set directory browser font    Adjust file browser text appearance

================================================================================
                         CONFIGURATION FILES
================================================================================

Settings are automatically saved to:

  Windows:  %APPDATA%\LithePlayer\AudioPlayer.conf
  macOS:    ~/Library/Preferences/com.LithePlayer.AudioPlayer.plist
  Linux:    ~/.config/LithePlayer/AudioPlayer.conf

Stored settings include:
  - Window geometry and splitter positions
  - Accent colors and theme customization
  - Peak indicator color and transparency
  - Font selections
  - Volume level
  - Default music folder

================================================================================
                          TROUBLESHOOTING
================================================================================

PROBLEM: VLC libraries not found
SOLUTION: 
  - Ensure VLC Media Player is installed (version 3.0.0+)
  - On Windows, verify installation in Program Files
  - Try portable deployment with plugins folder

PROBLEM: Some audio files won't play
SOLUTION:
  - Check file extension is supported (MP3, FLAC, WAV, M4A, AAC, OGG)
  - Verify codec plugins are present in portable deployment
  - Test file in VLC Player directly

PROBLEM: Equalizer not showing visualization
SOLUTION:
  - Ensure audio is playing (not paused)
  - Verify file contains valid audio data
  - Check that numpy and soundfile are installed correctly
  - Try increasing volume

PROBLEM: Global media keys not working
SOLUTION:
  - Feature currently Windows-only
  - Ensure app has focus on other platforms
  - Check if other apps have captured media keys (Spotify, iTunes)

PROBLEM: Portable deployment not working
SOLUTION:
  - Verify all 19 DLL files are present in plugins folder
  - Check file permissions (files must be readable)
  - Ensure all files are from same VLC version
  - Try deleting plugins.dat and restart app
  - Windows may block DLLs - right-click → Properties → Unblock

================================================================================
                        SUPPORTED AUDIO FORMATS
================================================================================

  .mp3     MP3 (MPEG Audio Layer 3)
  .flac    FLAC (Free Lossless Audio Codec)
  .wav     WAV (Waveform Audio File)
  .m4a     M4A (MPEG-4 Audio)
  .aac     AAC (Advanced Audio Coding)
  .ogg     OGG Vorbis

All formats support:
  - Metadata extraction (title, artist, album, year)
  - Album art display
  - Gapless playback

================================================================================
                         PERFORMANCE NOTES
================================================================================

  CPU Usage:          ~1-3% during gapless playback
  Memory Usage:       ~50-100MB (depending on playlist size)
  Equalizer Update:   30ms interval (~33 FPS)
  FFT Analysis:       2048 samples at 44.1kHz
  Startup Time:       <3 seconds (includes splash screen)
  Plugin Loading:     +0.5s for portable deployment

================================================================================
                         TECHNICAL DETAILS
================================================================================

GAPLESS PLAYBACK SYSTEM:
  - Uses dual VLC MediaPlayer instances
  - Active player plays current track
  - Standby player preloads next track in background
  - Automatic transition at 500ms before track end
  - Supports all audio formats seamlessly

FFT EQUALIZER:
  - 70 frequency bands (60Hz to 17kHz)
  - 15 segments per bar with smooth interpolation
  - Physics-based animation with gravity (0.4 acceleration)
  - Peak hold indicators (12 frames @ 30fps)
  - Exponential moving average for normalization
  - High-frequency emphasis for visual balance
  - Bass boost for lower frequencies

================================================================================
                            LICENSE
================================================================================

MIT License

Copyright (c) 2025 grahameys

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

================================================================================
                          ACKNOWLEDGMENTS
================================================================================

This project uses the following open-source libraries:

  - VLC Media Player    Robust audio playback engine
  - Qt/PySide6          Cross-platform GUI framework
  - Mutagen             Metadata extraction
  - NumPy               FFT calculations and audio analysis
  - SoundFile           Audio file decoding

================================================================================
                            CONTACT
================================================================================

Author:   grahameys
GitHub:   https://github.com/grahameys
Project:  https://github.com/grahameys/lithe-player

For bug reports, feature requests, or contributions, please visit the
GitHub repository and open an issue or pull request.

================================================================================
                             ROADMAP
================================================================================

Planned features for future releases:

  [ ] Cross-platform global media key support (macOS, Linux)
  [ ] Playlist save/load functionality (M3U, PLS formats)
  [ ] Shuffle and repeat modes
  [ ] Search and filter in playlist
  [ ] Mini player mode
  [ ] Audio effects and equalizer presets
  [ ] Dark mode theme
  [ ] Lyrics display (synced LRC support)
  [ ] Last.fm scrobbling
  [ ] Plugin system for extensions

================================================================================

Made with ❤️ and Python

If you find this project useful, please consider starring it on GitHub!

================================================================================
                          END OF DOCUMENT
================================================================================