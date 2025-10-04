# Sling TV & Custom DRM Streamer
<img width="1440" height="900" alt="image" src="https://github.com/user-attachments/assets/47b3b569-2eed-43bf-8c17-8fb92bc6d436" />

This project is a powerful, Python-based application designed to restream video content from various sources, with a primary focus on Sling TV. It handles DRM-protected streams (Widevine for Sling, CENC for others) and converts them into a standard, unencrypted MPEG-TS format. This output is then made available over a simple TCP connection, compatible with a wide range of media players like VLC, or via an HLS playlist for web players.

The application is managed through a user-friendly web interface built with Flask, allowing you to start, stop, and manage your streams with ease.

## Key Features

- **Sling TV Integration**: Authenticates with your Sling TV account to access and decrypt your channel lineup using Widevine DRM.
- **Custom Stream Support**: Add and manage your own DRM-protected DASH (`.mpd`) or HLS (`.m3u8`) streams via a `streams.json` file or the web UI.
- **CENC Decryption**: Supports decryption for custom streams protected with standard CENC (Common Encryption).
- **Web-Based UIs**: Includes a comprehensive **Admin Panel** for stream management and a dedicated **Web Player** for viewing channels.
- **Efficient Restreaming**: Uses `ffmpeg` for robust and efficient decryption and remuxing of video and audio content into a single MPEG-TS stream.
- **Key Caching**: Caches decryption keys for Sling TV streams to significantly speed up channel switching and stream restarts.
- **Idle Stream Timeout**: Automatically shuts down streams that have no connected clients to conserve system resources.
- **Multiple Output Formats**:
  - **Direct TCP Stream**: Provides a raw MPEG-TS stream on a dedicated port for each channel, perfect for players like VLC.
  - **HLS Playlist**: Generates a `.m3u8` playlist for each stream, compatible with `hls.js` and other web-based HLS players.
- **Ngrok Integration**: Optionally exposes the web UI and streams to the internet with a single command-line flag.

## How It Works

1.  **Authentication**: For Sling TV, the app uses your provided JWT to authenticate and fetch a list of entitled channels.
2.  **Stream Authorization**: When a stream is requested, it performs the necessary authorization flow to get the manifest URL and license acquisition details.
3.  **DRM Handshake**: It uses `pywidevine` to perform the Widevine license challenge/response, obtaining the decryption keys.
4.  **Proxy & Decrypt**: The application acts as a local proxy, fetching the encrypted video/audio segments. These segments are then piped to an `ffmpeg` process.
5.  **FFmpeg Pipeline**: `ffmpeg` uses the decryption keys to decrypt the segments on-the-fly and remuxes them into a standard MPEG-TS format.
6.  **TCP Server**: The final, unencrypted stream is output from `ffmpeg` to a local TCP server, which listens on a unique port for each active stream.
7.  **Client Connection**: Media players can connect directly to the TCP port or use the generated `.m3u8` playlist to watch the stream.

---

## Prerequisites

- **Python 3.8+**
- **FFmpeg**: The `ffmpeg` executable must be available in the project's root directory or in your system's PATH.
- **Sling TV Account**: Required for Sling TV functionality.

## Installation & Setup

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/chicanoandres702/Sling-Restreamer
    cd Sling-Restreamer
    ```

2.  **Install Dependencies**:
    A `requirements.txt` file is provided. Install all necessary Python packages using pip:
    ```bash
    pip install -r requirements.txt
    ```

3.  **FFmpeg**:
    Download the FFmpeg binary for your operating system and place `ffmpeg.exe` (on Windows) or `ffmpeg` (on Linux/macOS) in the root directory of the project.

4.  **Sling TV Configuration**:
    - **Widevine CDM**: Obtain a `WVD.wvd` file from a Chrome-based browser installation. This file contains the necessary device information for Widevine. Place it in the root directory.
    - **Authentication JWT**: You need to provide your Sling TV authentication JWT. It is **highly recommended** to set this as an environment variable:
      ```bash
      # On Linux/macOS
      export SLING_JWT="ey..."

      # On Windows (Command Prompt)
      set SLING_JWT="ey..."
      ```
      Alternatively, you can hardcode it in `restreamer.py`, but this is not recommended for security reasons.

5.  **Custom Streams (Optional)**:
    Create a `streams.json` file in the root directory to add your own DASH or HLS streams. You can also manage these through the web UI.

    *Example `streams.json`*:
    ```json
    {
      "my_custom_dash_stream": {
        "type": "dash",
        "title": "My Custom DASH Stream",
        "mpd_url": "https://path/to/your/manifest.mpd",
        "key": "your_cenc_decryption_key_in_hex"
      },
      "my_custom_hls_stream": {
        "type": "hls",
        "title": "My Custom HLS Stream",
        "m3u8_url": "https://path/to/your/playlist.m3u8",
        "key": "your_cenc_decryption_key_in_hex"
      }
    }
    ```

## Usage

1.  **Run the Application**:
    ```bash
    python restreamer.py
    ```

2.  **Expose with Ngrok (Optional)**:
    To share your streams or access the UI from outside your local network, use the `--ngrok` flag.
    ```bash
    python restreamer.py --ngrok
    ```

3.  **Access the Web UI**:
    - **Admin Panel**: Open your browser to `http://127.0.0.1:5000`. This is the main dashboard for managing all streams.
    - **Web Player**: Navigate to `http://127.0.0.1:5000/player` to use the dedicated web player with a channel list.

4.  **Playing a Stream**:
    - **In VLC / External Players**: Use the M3U playlist generated from the Admin Panel, or use the direct stream URL for a single channel: `http://<server_ip>:5000/stream/<stream_id>`.
    - **In the Web Player**: Simply navigate to `http://<server_ip>:5000/player` and click on a channel.

## API Endpoints

The application provides a simple REST API for management:

- `GET /api/channels`: Get a list of available Sling TV channels.
- `GET /api/allstreams`: Get a combined list of all Sling and custom streams.
- `GET /api/streams`: Get a list of configured custom streams.
- `POST /api/streams`: Add a new custom stream.
- `PUT /api/streams/<stream_id>`: Update an existing custom stream.
- `DELETE /api/streams/<stream_id>`: Remove a custom stream.
- `GET /api/status`: Get the real-time status of all streams.
- `POST /api/stream/start/<stream_id>`: Start a specific stream.
- `POST /api/stream/stop/<stream_id>`: Stop a specific stream.
- `POST /api/stream/restart/<stream_id>`: Restart a specific stream.
- `POST /api/cache/clear`: Manually clear the cached decryption keys.
- `GET /api/ngrok_url`: Get the public Ngrok URL, if active.

## Legal Disclaimer

This tool is intended for personal, educational, and research purposes only. It allows you to access content to which you are legally entitled through your own subscriptions.

**Do not use this software to distribute, share, or pirate content.** The user is solely responsible for complying with all applicable laws and the terms of service of any content provider. The author assumes no liability for any misuse of this software.
