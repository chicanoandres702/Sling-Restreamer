# tcp_streamer_app.py (Version 6 - With Stream Management)
#
# Dual-purpose Sling TV Streamer.
#
# USAGE:
#   - To run in Web Server mode (default):
#     python tcp_streamer_app.py
#
#   - To run in Command-Line Interface (CLI) mode:
#     python tcp_streamer_app.py --list-channels
#     python tcp_streamer_app.py --channel <CHANNEL_ID> | ffplay -
#     python tcp_streamer_app.py --channel <CHANNEL_ID> --output stream.ts

import os
import sys
import time
import queue
import base64
import threading
import subprocess
import logging
import socket
import jwt
import requests
import m3u8
import argparse
from flask import Flask, jsonify, render_template, Response
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# ==============================================================================
# --- 1. CONFIGURATION ---
# ==============================================================================
def resource_path(relative_path):
    try: base_path = sys._MEIPASS
    except Exception: base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

HTTP_HOST = '0.0.0.0'
HTTP_PORT = 5000
LOG_LEVEL = logging.INFO
WVD_FILE = resource_path('WVD.wvd')
FFMPEG_BIN = resource_path('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
TEMPLATE_FOLDER = resource_path('templates')
IDLE_TIMEOUT_SECONDS = 300
SLING_JWT = os.environ.get('SLING_JWT', "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2NvdW50U3RhdHVzIjoiYWN0aXZlIiwiZGV2IjoiNjlmYWFjOTctYmMwZi00Y2RhLTllNGYtOTZkOTk4YzQ4Nzg3IiwiaWF0IjoxNzU5MzUxOTg0LCJpc3MiOiJDTVciLCJwbGF0IjoiYnJvd3NlciIsInByb2QiOiJzbGluZyIsInByb2YiOiIyODgwYzg0NC1mMmQ1LTExZTktODMwZi0wZTIwYTUxZDhlN2MiLCJwcm9maWxlVHlwZSI6IkFkbWluIiwic3ViIjoiMjg4MGM4NDQtZjJkNS0xMWU5LTgzMGYtMGUyMGE1MWQ4ZTdjIn0.zGhss5iouL7-OV30Qf_cZlj-AoUGfqigmXBrAIp5qAk")

logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s')

# ==============================================================================
# --- 2. CORE LOGIC (SHARED BY WEB AND CLI) ---
# ==============================================================================
class CoreConfig:
    def __init__(self, wvd_path):
        self.WVD_PATH = wvd_path
        self.SLING_STREAM_AUTH_URL = 'https://p-streamauth.movetv.com/stream/auth'
        self.SLING_WIDEVINE_PROXY_URL = 'https://p-drmwv.movetv.com/widevine/proxy'
        self.SLING_CHANNELS_URL = 'https://p-cmwnext.movetv.com/pres/grid_guide_a_z'

class SlingClient:
    def __init__(self, user_jwt, config):
        self.config=config; self._user_jwt=user_jwt; self._subscriber_id=jwt.decode(self._user_jwt,options={"verify_signature":False})['prof']
    def get_channel_list(self):
        headers = { 'authorization': f'Bearer {self._user_jwt}', 'client-config': 'rn-client-config', 'client-version': '4.32.20', 'content-type': 'application/json; charset=UTF-8', 'device-model': 'Chrome', 'dma': '501', 'features': 'use_ui_4=true,inplace_reno_invalidation=true,gzip_response=true,enable_extended_expiry=true,enable_home_channels=true,enable_iap=true,enable_trash_franchise_iview=false,browse-by-service-ribbon=true,subpack-hub-view=true,entitled_streaming_hub=false,add-premium-channels=false,enable_home_sports_scores=false,enable-basepack-ribbon=true', 'page_size': 'large', 'player-version': '7.6.2', 'response-config': 'ar_browser_1_1', 'sling-interaction-id': '596bd797-90f1-440f-bc02-e6f588dae8f6', 'timezone': 'America/Los_Angeles' }
        try:
            response = requests.get(self.config.SLING_CHANNELS_URL, headers=headers, timeout=15).json()
            channels = []
            if 'special_ribbons' in response and response['special_ribbons']:
                tiles = response['special_ribbons'][0].get('tiles', [])
                for tile in tiles: channels.append({'title': tile.get('title'), 'id': tile.get('href', '').split('/')[-1]})
            return channels
        except (requests.RequestException, IndexError, KeyError) as e: logging.error(f"Could not fetch/parse channel list: {e}"); return []
    def authenticate_stream(self,channel_id):
        headers={'authorization':f'Bearer {self._user_jwt}','Content-Type':'application/json'}
        payload={'subscriber_id':self._subscriber_id,'drm':'widevine','qvt':f'https://cbd46b77.cdn.cms.movetv.com/playermetadata/sling/v1/api/channels/{channel_id}/current/schedule.qvt','os_version':'10','device_name':'browser','os_name':'Windows','brand':'sling','account_status':'active','advertiser_id':None,'support_mode':'false','ssai_vod':'true','ssai_dvr':'true',}
        try:
            response=requests.post(self.config.SLING_STREAM_AUTH_URL,headers=headers,json=payload,timeout=20); response.raise_for_status(); data=response.json()
            stream_manifest_url=data.get('ssai_manifest','')
            key_manifest_url=data.get('m3u8_url')
            temp_jwt=data.get('jwt')
            if not stream_manifest_url or not temp_jwt or not key_manifest_url: raise ValueError("Stream auth response missing data.")
            return stream_manifest_url, key_manifest_url, temp_jwt
        except(requests.RequestException,ValueError) as e: logging.error(f"Error authenticating stream: {e}"); return None, None, None
    def get_pssh_from_hls(self, hls_url):
        try:
            response = requests.get(hls_url, timeout=10); response.raise_for_status()
            m3u8_obj = m3u8.loads(content=response.text, uri=response.url)
            for key in m3u8_obj.session_keys:
                if key and key.uri: return key.uri.split(',')[-1]
            raise ValueError("PSSH data not found in HLS manifest session keys.")
        except Exception as e: logging.error(f"Error getting PSSH from HLS: {e}"); return None
    def derive_audio_url(self, video_url):
        try:
            master_playlist_content = requests.get(video_url, timeout=10).text
            master_playlist = m3u8.loads(master_playlist_content, uri=video_url)
            if master_playlist.is_variant:
                best_video_playlist = sorted(master_playlist.playlists, key=lambda p: p.stream_info.bandwidth, reverse=True)[0]
                audio_group_id = best_video_playlist.stream_info.audio
                if audio_group_id:
                    audio_media = [m for m in master_playlist.media if m.type == 'AUDIO' and m.group_id == audio_group_id]
                    for media in audio_media:
                        if media.absolute_uri:
                            logging.info(f"Found audio stream from manifest: {media.absolute_uri}")
                            return media.absolute_uri
        except Exception as e:
            logging.warning(f"Could not parse master manifest to find audio stream: {e}. Falling back.")
        audio_url = video_url.replace('/video/', '/audio/').replace('/vid06.m3u8', '/stereo/192.m3u8')
        logging.info(f"Derived audio URL (fallback): {audio_url}")
        return audio_url

class WidevineManager:
    def __init__(self, config):
        self.config = config
        if not os.path.exists(config.WVD_PATH): raise FileNotFoundError(f"WVD file not found at '{config.WVD_PATH}'")
        self._device = Device.load(config.WVD_PATH)
    def get_decryption_keys(self, pssh_b64, temp_jwt):
        try:
            license_channel_id = jwt.decode(temp_jwt, options={"verify_signature": False})['channel_guid']
            pssh = PSSH(base64.b64decode(pssh_b64))
            cdm = Cdm.from_device(self._device)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh)
            headers = {'Authorization': f'Bearer {temp_jwt}', 'Env': 'production', 'Channel-Id': license_channel_id, 'Content-Type': 'application/json'}
            payload = {"message": list(challenge)}
            response = requests.post(self.config.SLING_WIDEVINE_PROXY_URL, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            cdm.parse_license(session_id, response.content)
            keys = {}
            for key in cdm.get_keys(session_id):
                if key.type == 'CONTENT':
                    if isinstance(key.kid, bytes): kid_hex = key.kid.hex()
                    else: kid_hex = str(key.kid)
                    if isinstance(key.key, bytes): key_hex = key.key.hex()
                    else: key_hex = str(key.key)
                    kid_hex = kid_hex.replace('-', ''); key_hex = key_hex.replace('-', '')
                    keys[kid_hex] = key_hex
            cdm.close(session_id)
            if not keys: raise ValueError("No content keys were returned.")
            return keys
        except (requests.RequestException, ValueError, FileNotFoundError) as e:
            logging.error(f"Error during Widevine license acquisition: {e}")
            return {}

class HLSDownloader(threading.Thread):
    def __init__(self, hls_url: str, segment_queue: queue.Queue, stream_type: str = "video"):
        super().__init__(name=f"HLSDownloader-{stream_type}")
        self.daemon = True; self._hls_url = hls_url; self._segment_queue = segment_queue
        self._stream_type = stream_type; self._session = requests.Session()
        self._stop_event = threading.Event(); self._downloaded_segments = set()
    def stop(self): self._stop_event.set()
    def run(self):
        logging.info(f"HLSDownloader for {self._stream_type} started")
        while not self._stop_event.is_set():
            try:
                response = self._session.get(self._hls_url, timeout=10)
                response.raise_for_status()
                playlist = m3u8.loads(content=response.text, uri=response.url)
                if playlist.is_variant:
                    best_stream = sorted(playlist.playlists, key=lambda p: p.stream_info.bandwidth, reverse=True)[0]
                    self._hls_url = best_stream.absolute_uri
                    logging.info(f"{self._stream_type}: Switched to best stream: {self._hls_url}")
                    continue
                if playlist.segment_map and playlist.segment_map[0].uri:
                    init_uri = playlist.segment_map[0].absolute_uri
                    if init_uri not in self._downloaded_segments:
                        init_data = self._session.get(init_uri, timeout=10).content
                        self._segment_queue.put(init_data)
                        self._downloaded_segments.add(init_uri)
                for segment in playlist.segments:
                    if self._stop_event.is_set(): break
                    if segment.uri and segment.absolute_uri not in self._downloaded_segments:
                        media_data = self._session.get(segment.absolute_uri, timeout=10).content
                        self._segment_queue.put(media_data)
                        self._downloaded_segments.add(segment.absolute_uri)
                if playlist.is_endlist: break
                time.sleep(playlist.target_duration / 2 if playlist.target_duration else 2)
            except Exception as e:
                logging.warning(f"HLSDownloader ({self._stream_type}) error: {e}. Retrying.")
                time.sleep(2)
        self._segment_queue.put(None)
        logging.info(f"HLSDownloader ({self._stream_type}) stopped.")

# ==============================================================================
# --- 3. NEW ROBUST FFmpeg PROCESS MANAGER ---
# ==============================================================================
class FFmpegProcess:
    def __init__(self, keys_dict):
        self.keys_dict = keys_dict; self.video_process = None; self.audio_process = None
        self.muxer_process = None; self.process = None; self.video_pipe = None
        self.audio_pipe = None; self._stop_event = threading.Event()
    def _get_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
    def _setup_tcp_server(self, name):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0)); server.listen(1); port = server.getsockname()[1]
        logging.info(f"{name} TCP server for FFmpeg IPC listening on port {port}")
        return server, port
    def start(self):
        if os.name != 'nt':
            raise NotImplementedError("This FFmpeg architecture is optimized for Windows TCP IPC; named pipes would be needed for Linux/macOS.")
        key = list(self.keys_dict.values())[0]; decryption_args = ['-decryption_key', key]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        video_server, video_port = self._setup_tcp_server("Video decrypter"); audio_server, audio_port = self._setup_tcp_server("Audio decrypter")
        video_mux_server, video_mux_port = self._setup_tcp_server("Video muxer input"); audio_mux_server, audio_mux_port = self._setup_tcp_server("Audio muxer input")
        # Build FFmpeg commands
        video_cmd = [ FFMPEG_BIN, '-y', *decryption_args, '-i', f'tcp://127.0.0.1:{video_port}', '-c:v', 'copy','-copyts', '-f', 'mpegts', f'tcp://127.0.0.1:{video_mux_port}?listen=1' ]
        audio_cmd = [ FFMPEG_BIN, '-y', *decryption_args, '-i', f'tcp://127.0.0.1:{audio_port}', '-c:a', 'copy','-copyts', '-f', 'mpegts', f'tcp://127.0.0.1:{audio_mux_port}?listen=1' ]
        muxer_cmd = [
                FFMPEG_BIN, '-y',
                '-i', f'tcp://127.0.0.1:{video_mux_port}?timeout=10000000',
                '-i', f'tcp://127.0.0.1:{audio_mux_port}?timeout=10000000',
                '-map', '0:v',
                '-map', '1:a',
                '-fflags', '+genpts',
                # Preserve original timestamps and sync audio start to video
                '-copyts',
                '-async', '1',
                '-bsf:a', 'aac_adtstoasc',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '192k',
                '-f', 'mpegts',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-'
            ]
        self.video_process = subprocess.Popen(video_cmd, stderr=subprocess.PIPE, creationflags=creation_flags)
        self.audio_process = subprocess.Popen(audio_cmd, stderr=subprocess.PIPE, creationflags=creation_flags)
        time.sleep(1)
        self.muxer_process = subprocess.Popen(muxer_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creation_flags)
        self.process = self.muxer_process
        video_server.settimeout(30); audio_server.settimeout(30)
        video_conn, _ = video_server.accept(); self.video_pipe = video_conn.makefile('wb')
        audio_conn, _ = audio_server.accept(); self.audio_pipe = audio_conn.makefile('wb')
        video_server.close(); audio_server.close(); video_mux_server.close(); audio_mux_server.close()
        threading.Thread(target=self._capture_stderr, args=(self.video_process, "Video"), daemon=True).start()
        threading.Thread(target=self._capture_stderr, args=(self.audio_process, "Audio"), daemon=True).start()
        threading.Thread(target=self._capture_stderr, args=(self.muxer_process, "Muxer"), daemon=True).start()
        logging.info("FFmpeg IPC pipeline connected and running.")
    def stop(self):
        logging.info("Stopping FFmpeg processes..."); self._stop_event.set()
        for pipe in [self.video_pipe, self.audio_pipe]:
            if pipe:
                try: pipe.close()
                except Exception: pass
        for p in [self.muxer_process, self.video_process, self.audio_process]:
            if p and p.poll() is None:
                try: p.terminate(); p.wait(timeout=5)
                except subprocess.TimeoutExpired: p.kill()
        logging.info("FFmpeg processes stopped.")
    def _capture_stderr(self, process, name):
        for line in iter(process.stderr.readline, b''):
            if self._stop_event.is_set(): break
            decoded = line.decode('utf-8', errors='ignore').strip()
            if decoded: logging.debug(f"FFmpeg ({name}): {decoded}")

# ==============================================================================
# --- 4. WEB SERVER MODE LOGIC ---
# ==============================================================================
class StreamManager:
    def __init__(self):
        self.streams = {}; self.lock = threading.Lock()
        self.channel_cache = {}; self.cache_lock = threading.Lock(); self.cache_expiry = 3600
    def get_channel_title(self, channel_id):
        with self.cache_lock:
            if self.channel_cache and time.time() < self.channel_cache.get('expiry', 0):
                return self.channel_cache.get('channels', {}).get(channel_id, channel_id)
        logging.info("Channel cache expired, refreshing...")
        config = CoreConfig(wvd_path=WVD_FILE); client = SlingClient(SLING_JWT, config)
        channels = client.get_channel_list()
        with self.cache_lock:
            self.channel_cache['channels'] = {c['id']: c['title'] for c in channels}
            self.channel_cache['expiry'] = time.time() + self.cache_expiry
            return self.channel_cache['channels'].get(channel_id, channel_id)
    def get_or_start_stream(self, channel_id):
        with self.lock:
            if channel_id in self.streams:
                logging.info(f"TCP stream for {channel_id} is already active.")
                return self.streams[channel_id]
            logging.info(f"Starting new TCP stream for channel: {channel_id}")
            title = self.get_channel_title(channel_id)
            stream = TCPStream(channel_id, title)
            stream.start(); self.streams[channel_id] = stream
            return stream
    def stop_stream(self, channel_id):
        with self.lock:
            if channel_id in self.streams:
                logging.info(f"Stopping TCP stream for {channel_id}")
                stream = self.streams.pop(channel_id)
                stream.stop()
    def restart_stream(self, channel_id):
        with self.lock:
            if channel_id in self.streams:
                logging.info(f"Restarting stream for {channel_id}")
                old_stream = self.streams.pop(channel_id)
                title = old_stream.title; old_stream.stop(); time.sleep(1)
                new_stream = TCPStream(channel_id, title); new_stream.start()
                self.streams[channel_id] = new_stream
                return True
        return False
    def get_status(self):
        with self.lock:
            return {
                channel_id: {
                    'status': stream.status, 'clients': stream.client_count,
                    'port': stream.port, 'title': stream.title, 'uptime': stream.uptime,
                    'error': stream.error_message
                } for channel_id, stream in self.streams.items()
            }
    def janitor_run(self):
        while True:
            time.sleep(60); idle_channels = []
            with self.lock:
                now = time.time()
                for channel_id, stream in self.streams.items():
                    if stream.client_count > 0: stream.last_activity = now
                    if stream.client_count == 0 and now - stream.last_activity > IDLE_TIMEOUT_SECONDS:
                        logging.info(f"Stream for {channel_id} has been idle for too long. Shutting down.")
                        idle_channels.append(channel_id)
            for channel_id in idle_channels: self.stop_stream(channel_id)

class TCPStream:
    def __init__(self, channel_id, title):
        self.channel_id = channel_id; self.title = title
        self.port = self._get_free_port(); self.status = "INITIALIZING"
        self.error_message = None; self.client_count = 0
        self.last_activity = time.time(); self.start_time = None
        self._stop_event = threading.Event(); self._client_queues = set()
        self._ffmpeg = None; self._downloader_video = None; self._downloader_audio = None
    @property
    def uptime(self):
        if self.status == "STREAMING" and self.start_time:
            return time.time() - self.start_time
        return 0
    def start(self):
        threading.Thread(target=self._run_stream_pipeline, name=f"Streamer-{self.channel_id}", daemon=True).start()
        threading.Thread(target=self._run_tcp_server, name=f"TCPServer-{self.channel_id}", daemon=True).start()
    def stop(self):
        if self._stop_event.is_set(): return
        self._stop_event.set()
        if self._downloader_video: self._downloader_video.stop()
        if self._downloader_audio: self._downloader_audio.stop()
        if self._ffmpeg: self._ffmpeg.stop()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0); s.connect(('127.0.0.1', self.port))
        except Exception: pass
    def _get_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('0.0.0.0', 0)); return s.getsockname()[1]
    def _run_tcp_server(self):
        try:
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind(('0.0.0.0', self.port)); server_socket.listen(5)
            logging.info(f"TCP server for {self.channel_id} listening on port {self.port}")
            while not self._stop_event.is_set():
                client_socket, addr = server_socket.accept()
                if self._stop_event.is_set(): client_socket.close(); break
                logging.info(f"Accepted connection from {addr} for channel {self.channel_id}")
                threading.Thread(target=self._handle_client, args=(client_socket,), daemon=True).start()
        except Exception as e:
            if not self._stop_event.is_set(): logging.error(f"TCP server for {self.channel_id} failed: {e}")
        finally:
            server_socket.close(); logging.info(f"TCP server for {self.channel_id} shut down.")
    def _handle_client(self, client_socket):
        self.client_count += 1; self.last_activity = time.time()
        q = queue.Queue(maxsize=100); self._client_queues.add(q)
        try:
            client_socket.recv(4096)
            response = b"HTTP/1.0 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n"
            client_socket.sendall(response)
            while not self._stop_event.is_set():
                chunk = q.get()
                if chunk is None: break
                client_socket.sendall(chunk)
        except (socket.error, BrokenPipeError) as e: logging.info(f"Client disconnected: {e}")
        finally:
            self.client_count -= 1; self._client_queues.discard(q); client_socket.close()
    def _run_stream_pipeline(self):
        try:
            self.status = "AUTHENTICATING"; config = CoreConfig(WVD_FILE)
            client = SlingClient(SLING_JWT, config); widevine = WidevineManager(config)
            stream_url, key_url, temp_jwt = client.authenticate_stream(self.channel_id)
            if not stream_url: raise RuntimeError("Authentication failed")
            audio_url = client.derive_audio_url(stream_url); pssh = client.get_pssh_from_hls(key_url)
            if not pssh: raise RuntimeError("Failed to get PSSH")
            keys = widevine.get_decryption_keys(pssh, temp_jwt)
            if not keys: raise RuntimeError("Failed to get decryption keys")
            self.status = "STARTING_FFMPEG"; self._ffmpeg = FFmpegProcess(keys); self._ffmpeg.start()
            video_q = queue.Queue(); audio_q = queue.Queue()
            self._downloader_video = HLSDownloader(stream_url, video_q, "video")
            self._downloader_audio = HLSDownloader(audio_url, audio_q, "audio")
            self._downloader_video.start(); self._downloader_audio.start()
            threading.Thread(target=self._feed_ffmpeg, args=(video_q, self._ffmpeg.video_pipe), daemon=True).start()
            threading.Thread(target=self._feed_ffmpeg, args=(audio_q, self._ffmpeg.audio_pipe), daemon=True).start()
            self.status = "STREAMING"; self.start_time = time.time()
            self._pump_ffmpeg_to_clients()
        except Exception as e:
            self.status = "ERROR"; self.error_message = str(e); self.start_time = None
            logging.error(f"CRITICAL STREAM PIPELINE FAILURE for {self.channel_id}: {e}", exc_info=True)
        finally:
            self.start_time = None
            for q in self._client_queues: q.put(None)
            self.stop()
    def _feed_ffmpeg(self, segment_queue, ffmpeg_pipe):
        try:
            while not self._stop_event.is_set():
                segment = segment_queue.get(timeout=45)
                if segment is None: break
                if ffmpeg_pipe: ffmpeg_pipe.write(segment); ffmpeg_pipe.flush()
        except queue.Empty:
            if not self._stop_event.is_set(): logging.warning(f"Segment queue timeout for {self.channel_id}")
        except (IOError, BrokenPipeError):
            if not self._stop_event.is_set(): logging.warning(f"FFmpeg pipe broke for {self.channel_id}")
        finally: logging.info("FFmpeg feeder stopped.")
    def _pump_ffmpeg_to_clients(self):
        while not self._stop_event.is_set():
            chunk = self._ffmpeg.process.stdout.read(8192)
            if not chunk:
                logging.info(f"FFmpeg muxer process for {self.channel_id} ended.")
                break
            for q in self._client_queues: q.put(chunk)

# ==============================================================================
# --- 5. COMMAND-LINE INTERFACE (CLI) MODE LOGIC ---
# ==============================================================================
def list_channels_cli():
    print("Fetching channel list...")
    config = CoreConfig(wvd_path=WVD_FILE); client = SlingClient(SLING_JWT, config)
    channels = client.get_channel_list()
    if not channels:
        print("\nNo channels found or failed to fetch. Check JWT and logs."); return
    print("\n--- Available Channels ---")
    for channel in sorted(channels, key=lambda c: c.get('title', '')):
        print(f"  ID: {channel.get('id', 'N/A'):<40} | Title: {channel.get('title', 'No Title')}")
    print("--------------------------\n")
def stream_channel_cli(channel_id, output_file):
    ffmpeg_proc = None; downloader_video = None; downloader_audio = None; output_handle = None
    try:
        if output_file:
            logging.info(f"Streaming to file: {output_file}"); output_handle = open(output_file, 'wb')
        else:
            logging.info("Streaming to stdout. Pipe to a player like ffplay or vlc."); output_handle = sys.stdout.buffer
        logging.info(f"Authenticating stream for channel {channel_id}"); config = CoreConfig(wvd_path=WVD_FILE)
        client = SlingClient(SLING_JWT, config); widevine = WidevineManager(config)
        stream_url, key_url, temp_jwt = client.authenticate_stream(channel_id)
        if not stream_url: raise RuntimeError("Authentication failed")
        audio_url = client.derive_audio_url(stream_url); pssh = client.get_pssh_from_hls(key_url)
        if not pssh: raise RuntimeError("Failed to get PSSH")
        keys = widevine.get_decryption_keys(pssh, temp_jwt)
        if not keys: raise RuntimeError("Failed to get decryption keys")
        logging.info("Starting FFmpeg and downloaders..."); ffmpeg_proc = FFmpegProcess(keys); ffmpeg_proc.start()
        video_q = queue.Queue(); audio_q = queue.Queue()
        downloader_video = HLSDownloader(stream_url, video_q, "video"); downloader_audio = HLSDownloader(audio_url, audio_q, "audio")
        downloader_video.start(); downloader_audio.start()
        def _feed(q, pipe):
            try:
                while True:
                    segment = q.get()
                    if segment is None: break
                    if pipe: pipe.write(segment); pipe.flush()
            except (IOError, BrokenPipeError): logging.warning("CLI feeder pipe broke.")
        threading.Thread(target=_feed, args=(video_q, ffmpeg_proc.video_pipe), daemon=True).start()
        threading.Thread(target=_feed, args=(audio_q, ffmpeg_proc.audio_pipe), daemon=True).start()
        logging.info("Streaming started. Press Ctrl+C to stop.")
        while True:
            chunk = ffmpeg_proc.process.stdout.read(8192)
            if not chunk: logging.info("End of stream."); break
            output_handle.write(chunk); output_handle.flush()
    except KeyboardInterrupt: logging.info("\nInterrupted by user. Shutting down.")
    except (BrokenPipeError): logging.warning("Output pipe broken. Player likely closed.")
    except Exception as e: logging.critical(f"A critical error occurred in CLI mode: {e}", exc_info=True)
    finally:
        logging.info("Cleaning up CLI resources...")
        if downloader_video: downloader_video.stop()
        if downloader_audio: downloader_audio.stop()
        if ffmpeg_proc: ffmpeg_proc.stop()
        if output_file and output_handle: output_handle.close()
        logging.info("CLI finished.")

# ==============================================================================
# --- 6. APPLICATION STARTUP ---
# ==============================================================================
def run_web_server():
    stream_manager = StreamManager()
    app = Flask(__name__, template_folder=TEMPLATE_FOLDER)
    def get_local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try: s.connect(('10.255.255.255', 1)); IP = s.getsockname()[0]
        except Exception: IP = '127.0.0.1'
        finally: s.close()
        return IP
    @app.route('/')
    def index():
        return render_template('index_tcp.html', server_ip=get_local_ip(), server_port=HTTP_PORT)
    @app.route('/api/channels')
    def api_get_channels():
        stream_manager.get_channel_title("refresh")
        with stream_manager.cache_lock:
            channels_dict = stream_manager.channel_cache.get('channels', {})
        channels_list = sorted([{'id': k, 'title': v} for k, v in channels_dict.items()], key=lambda x: x['title'])
        return jsonify(channels_list)
    @app.route('/api/status')
    def api_get_status():
        return jsonify(stream_manager.get_status())
    @app.route('/api/stream/stop/<channel_id>', methods=['POST'])
    def api_stop_stream(channel_id):
        stream_manager.stop_stream(channel_id)
        return jsonify({"success": True, "message": f"Stop command sent to stream {channel_id}."})
    @app.route('/api/stream/restart/<channel_id>', methods=['POST'])
    def api_restart_stream(channel_id):
        success = stream_manager.restart_stream(channel_id)
        if success:
            return jsonify({"success": True, "message": f"Restart command sent to stream {channel_id}."})
        else:
            return jsonify({"success": False, "message": f"Stream {channel_id} not found."}), 404
    @app.route('/stream/<channel_id>')
    def get_stream_playlist(channel_id):
        stream = stream_manager.get_or_start_stream(channel_id)
        timeout = 20; start_time = time.time()
        while stream.status not in ["STREAMING", "ERROR"]:
            if time.time() - start_time > timeout:
                stream.stop()
                return Response(f"Stream for {channel_id} failed to initialize in time.", status=500)
            time.sleep(0.5)
        if stream.status == "ERROR":
            return Response(f"Failed to start stream for {channel_id}: {stream.error_message}", status=500)
        playlist_content = f"#EXTM3U\n#EXTINF:-1,{stream.title}\nhttp://{get_local_ip()}:{stream.port}"
        return Response(playlist_content, mimetype='audio/x-mpegurl')

    janitor_thread = threading.Thread(target=stream_manager.janitor_run, name="Janitor", daemon=True)
    janitor_thread.start()
    logging.info(f"Starting Sling TCP Streamer server at http://{HTTP_HOST}:{HTTP_PORT}")
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Sling TV Web/CLI Streamer. Runs as a web server by default.", formatter_class=argparse.RawTextHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-l', '--list-channels', action='store_true', help='List all available channels and exit.')
    group.add_argument('-c', '--channel', metavar='ID', type=str, help='The ID of the channel to stream to stdout.')
    parser.add_argument('-o', '--output', metavar='FILE', type=str, help='(Optional, with --channel) Save stream to file.')
    args = parser.parse_args()
    if not SLING_JWT or len(SLING_JWT) < 50: logging.critical("FATAL: SLING_JWT not set or invalid!"); sys.exit(1)
    if not os.path.exists(WVD_FILE): logging.critical(f"FATAL: {WVD_FILE} not found!"); sys.exit(1)
    if not os.path.exists(FFMPEG_BIN): logging.critical(f"FATAL: {FFMPEG_BIN} not found!"); sys.exit(1)
    if args.list_channels:
        logging.info("--- Starting in CLI mode: List Channels ---"); list_channels_cli()
    elif args.channel:
        logging.info(f"--- Starting in CLI mode: Stream Channel {args.channel} ---"); stream_channel_cli(args.channel, args.output)
    else:
        logging.info("--- No CLI arguments detected. Starting Web Server mode. ---"); run_web_server()