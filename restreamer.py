# tcp_streamer_app.py (Version 18 - Key Caching for Faster Sling TV Launch)
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
import atexit
import signal
import json
import re
import xml.etree.ElementTree as ET
import math
from urllib.parse import urljoin, quote_plus, unquote_plus
from flask import Flask, jsonify, render_template, Response, redirect, request, stream_with_context, abort
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
STREAMS_JSON_FILE = resource_path('streams.json')
IDLE_TIMEOUT_SECONDS = 300
KEY_CACHE_EXPIRY_SECONDS = 3600*24*30  # 1 hour
SLING_JWT = os.environ.get('SLING_JWT', "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2NvdW50U3RhdHVzIjoiYWN0aXZlIiwiZGV2IjoiNjlmYWFjOTctYmMwZi00Y2RhLTllNGYtOTZkOTk4YzQ4Nzg3IiwiaWF0IjoxNzU5MzUxOTg0LCJpc3MiOiJDTVciLCJwbGF0IjoiYnJvd3NlciIsInByb2QiOiJzbGluZyIsInByb2YiOiIyODgwYzg0NC1mMmQ1LTExZTktODMwZi0wZTIwYTUxZDhlN2MiLCJwcm9maWxlVHlwZSI6IkFkbWluIiwic3ViIjoiMjg4MGM4NDQtZjJkNS0xMWU5LTgzMGYtMGUyMGE1MWQ4ZTdjIn0.zGhss5iouL7-OV30Qf_cZlj-AoUGfqigmXBrAIp5qAk")


logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s')

# ==============================================================================
# --- 2. CORE LOGIC (SLING TV - WIDEVINE HLS) ---
# ==============================================================================
class CoreConfig:
    def __init__(self, wvd_path):
        self.WVD_PATH = wvd_path
        self.SLING_STREAM_AUTH_URL = 'https://p-streamauth.movetv.com/stream/auth'
        self.SLING_WIDEVINE_PROXY_URL = 'https://p-drmwv.movetv.com/widevine/proxy'
        self.SLING_CHANNELS_URL = 'https://p-cmwnext.movetv.com/pres/grid_guide_a_z'

class SlingClient:
    def __init__(self, user_jwt, config):
        self.config = config
        self._user_jwt = user_jwt
        self._subscriber_id = None
        try:
            decoded_jwt = jwt.decode(self._user_jwt, options={"verify_signature": False})
            self._subscriber_id = decoded_jwt.get('prof')
        except (jwt.exceptions.DecodeError, KeyError):
            logging.error("SLING_JWT is invalid or missing 'prof' claim. Sling TV features will not work.")

    def get_channel_list(self):
        if not self._subscriber_id: return []
        headers = { 'authorization': f'Bearer {self._user_jwt}', 'client-config': 'rn-client-config', 'client-version': '4.32.20', 'content-type': 'application/json; charset=UTF-8', 'device-model': 'Chrome', 'dma': '501', 'features': 'use_ui_4=true,inplace_reno_invalidation=true,gzip_response=true,enable_extended_expiry=true,enable_home_channels=true,enable_iap=true,enable_trash_franchise_iview=false,browse-by-service-ribbon=true,subpack-hub-view=true,entitled_streaming_hub=false,add-premium-channels=false,enable-basepack-ribbon=true', 'page_size': 'large', 'player-version': '7.6.2', 'response-config': 'ar_browser_1_1', 'sling-interaction-id': '596bd797-90f1-440f-bc02-e6f588dae8f6', 'timezone': 'America/Los_Angeles' }
        try:
            response = requests.get(self.config.SLING_CHANNELS_URL, headers=headers, timeout=15).json()
            channels = []
            if 'special_ribbons' in response and response['special_ribbons']:
                for tile in response['special_ribbons'][0].get('tiles', []):
                    channels.append({'title': tile.get('title'), 'id': tile.get('href', '').split('/')[-1]})
            return channels
        except (requests.RequestException, IndexError, KeyError) as e: 
            logging.error(f"Could not fetch/parse channel list: {e}"); return []

    def authenticate_stream(self,channel_id):
        if not self._subscriber_id: return None, None, None
        headers={'authorization':f'Bearer {self._user_jwt}','Content-Type':'application/json'}
        payload={'subscriber_id':self._subscriber_id,'drm':'widevine','qvt':f'https://cbd46b77.cdn.cms.movetv.com/playermetadata/sling/v1/api/channels/{channel_id}/current/schedule.qvt','os_version':'10','device_name':'browser','os_name':'Windows','brand':'sling','account_status':'active','advertiser_id':None,'support_mode':'false','ssai_vod':'true','ssai_dvr':'true',}
        try:
            response=requests.post(self.config.SLING_STREAM_AUTH_URL,headers=headers,json=payload,timeout=20); response.raise_for_status(); data=response.json()
            stream_manifest_url=data.get('m3u8_url','')
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
            logging.error(f"Error during Widevine license acquisition: {e}"); return {}

# ==============================================================================
# --- 3. DASH/HLS PROXY LOGIC ---
# ==============================================================================
dash_cache = {}
dash_cache_lock = threading.Lock()

# ... (rest of proxy logic functions: _parse_duration, _format_template, parse_mpd, dash_segment_fetcher, parse_m3u8, hls_segment_fetcher) ...
# NOTE: No changes are needed in these helper functions. They are included here for completeness.
def _parse_duration(duration_str: str) -> float:
    if not duration_str or not duration_str.startswith("PT"): return 0.0
    duration, pattern = 0.0, re.compile(r"(\d+(?:\.\d+)?)([HMS])")
    parts = pattern.findall(duration_str[2:])
    for value, unit in parts:
        v = float(value)
        if unit == 'H': duration += v * 3600
        elif unit == 'M': duration += v * 60
        elif unit == 'S': duration += v
    return duration

def _format_template(template_str, number=None, time_val=None):
    def replace_func(match):
        name, fmt = match.group(1), match.group(2)
        value = number if name == 'Number' else time_val
        if value is None: return ""
        if fmt: return f"{value:{fmt.lstrip('%')}}"
        return str(value)
    return re.sub(r'\$(Number|Time)(%[\da-zA-Z]+)?\$', replace_func, template_str)

def parse_mpd(mpd_url: str):
    with dash_cache_lock:
        if mpd_url in dash_cache and time.time() - dash_cache[mpd_url].get('timestamp', 0) < 5:
            return dash_cache[mpd_url]['tracks'], dash_cache[mpd_url]['type']
    try:
        response = requests.get(mpd_url, timeout=10); response.raise_for_status()
        base_url = response.url.rsplit('/', 1)[0] + '/'; root = ET.fromstring(response.text)
        ns = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}
        mpd_type = root.attrib.get('type', 'static'); tracks = {}
        period = root.find('mpd:Period', ns)
        if period is None: return None, None
        for adaptation_set in period.findall('mpd:AdaptationSet', ns):
            content_type = adaptation_set.attrib.get('contentType', 'unknown')
            segment_template = adaptation_set.find('mpd:SegmentTemplate', ns)
            for rep in adaptation_set.findall('mpd:Representation', ns):
                rep_id = rep.attrib.get('id');
                if not rep_id: continue
                current_template = rep.find('mpd:SegmentTemplate', ns) or segment_template
                if current_template is None: continue
                key = f"{content_type}_{rep_id}"; urls = []
                init_template = current_template.attrib.get('initialization')
                if init_template: urls.append(urljoin(base_url, _format_template(init_template).replace('$RepresentationID$', rep_id)))
                media_template = current_template.attrib.get('media')
                if not media_template: continue
                start_number = int(current_template.attrib.get('startNumber', 1))
                timeline = current_template.find('mpd:SegmentTimeline', ns)
                if timeline is not None:
                    current_time, current_number = 0, start_number
                    for s_tag in timeline.findall('mpd:S', ns):
                        d = int(s_tag.attrib['d']); t_str = s_tag.attrib.get('t')
                        current_time = int(t_str) if t_str is not None else current_time; r = int(s_tag.attrib.get('r', 0))
                        for _ in range(r + 1):
                            urls.append(urljoin(base_url, _format_template(media_template, number=current_number, time_val=current_time).replace('$RepresentationID$', rep_id)))
                            current_time += d; current_number += 1
                else:
                    media_duration = _parse_duration(root.attrib.get('mediaPresentationDuration'))
                    timescale = int(current_template.attrib.get('timescale', 1)); seg_duration_units = int(current_template.attrib.get('duration', 0))
                    if seg_duration_units > 0 and timescale > 0 and media_duration > 0:
                        segment_len_secs = seg_duration_units / timescale; segment_count = math.ceil(media_duration / segment_len_secs)
                        for i in range(segment_count):
                            urls.append(urljoin(base_url, _format_template(media_template, number=start_number + i).replace('$RepresentationID$', rep_id)))
                if urls: tracks[key] = {'key': key, 'id': rep_id, 'type': content_type, 'bandwidth': int(rep.attrib.get('bandwidth', 0)), 'urls': urls}
        with dash_cache_lock: dash_cache[mpd_url] = {'tracks': tracks, 'type': mpd_type, 'timestamp': time.time()}
        return tracks, mpd_type
    except (requests.RequestException, ET.ParseError) as e:
        logging.error(f"[DASH Parser] Error processing MPD {mpd_url}: {e}"); return None, None

def dash_segment_fetcher(q: queue.Queue, session: requests.Session, track: dict, mpd_type: str, mpd_url: str, stop_event: threading.Event):
    try:
        if mpd_type == 'static':
            for segment_url in track['urls']:
                if stop_event.is_set(): break
                try: res = session.get(segment_url, timeout=10); res.raise_for_status(); q.put(res.content)
                except requests.RequestException: pass
        else: # Live stream
            sent_segments = set()
            while not stop_event.is_set():
                live_tracks, _ = parse_mpd(mpd_url)
                if not live_tracks or track['key'] not in live_tracks: time.sleep(2); continue
                new_urls = [url for url in live_tracks[track['key']]['urls'] if url not in sent_segments]
                for segment_url in new_urls:
                    if stop_event.is_set(): break
                    try: res = session.get(segment_url, timeout=10); res.raise_for_status(); q.put(res.content); sent_segments.add(segment_url)
                    except requests.RequestException: pass
                time.sleep(2.0)
    finally: q.put(None); logging.info(f"[DASH Fetcher] Fetcher for track {track.get('id')} stopped.")

def parse_m3u8(m3u8_url: str):
    try:
        playlist_obj = m3u8.load(uri=m3u8_url, timeout=10)
        m3u8_type = 'VOD' if playlist_obj.is_endlist else 'LIVE'
        tracks = {}
        if playlist_obj.is_variant:
            for p in playlist_obj.playlists:
                if p.stream_info and p.stream_info.bandwidth:
                    key = f"video_{p.stream_info.bandwidth}"
                    tracks[key] = {'key': key, 'type': 'video', 'bandwidth': p.stream_info.bandwidth, 'url': p.absolute_uri}
            for m in playlist_obj.media:
                if m.type == 'AUDIO' and m.group_id:
                    key = f"audio_{m.group_id}_{m.language or m.name}"
                    tracks[key] = {'key': key, 'type': 'audio', 'bandwidth': 0, 'url': m.absolute_uri}
        else:
             tracks["video_default"] = {'key': "video_default", 'type': 'video', 'bandwidth': 0, 'url': m3u8_url}
        return tracks, m3u8_type
    except Exception as e:
        logging.error(f"[HLS Parser] Error parsing M3U8 from {m3u8_url}: {e}"); return None, None

def hls_segment_fetcher(q: queue.Queue, session: requests.Session, track: dict, m3u8_type: str, stop_event: threading.Event):
    media_playlist_url = track['url']
    try:
        if m3u8_type == 'VOD':
            media_playlist = m3u8.load(media_playlist_url, timeout=10)
            if media_playlist.segment_map and media_playlist.segment_map[0].uri:
                init_uri = media_playlist.segment_map[0].absolute_uri
                logging.info(f"[HLS Fetcher] Found VOD init segment (EXT-X-MAP): {init_uri}")
                try:
                    resp = session.get(init_uri, timeout=10); resp.raise_for_status()
                    q.put(resp.content)
                except requests.RequestException as e: logging.error(f"[HLS Fetcher] Failed to fetch init segment: {e}")
            for segment in media_playlist.segments:
                if stop_event.is_set(): break
                try: resp = session.get(segment.absolute_uri, timeout=10); resp.raise_for_status(); q.put(resp.content)
                except requests.RequestException: pass
        else: # LIVE
            sent_segments = set()
            while not stop_event.is_set():
                try:
                    media_playlist = m3u8.load(media_playlist_url, timeout=5)
                    if media_playlist.segment_map and media_playlist.segment_map[0].uri:
                        init_uri = media_playlist.segment_map[0].absolute_uri
                        if init_uri not in sent_segments:
                            logging.info(f"[HLS Fetcher] Found LIVE init segment (EXT-X-MAP): {init_uri}")
                            try:
                                resp = session.get(init_uri, timeout=5); resp.raise_for_status()
                                q.put(resp.content); sent_segments.add(init_uri)
                            except requests.RequestException as e: logging.error(f"[HLS Fetcher] Failed to fetch init segment: {e}")
                    for segment in media_playlist.segments:
                        if stop_event.is_set(): break
                        if segment.absolute_uri not in sent_segments:
                            resp = session.get(segment.absolute_uri, timeout=5); resp.raise_for_status()
                            q.put(resp.content); sent_segments.add(segment.absolute_uri)
                    sleep_duration = (media_playlist.target_duration / 2) if media_playlist.target_duration else 2
                    time.sleep(sleep_duration)
                except Exception: time.sleep(2)
    finally: q.put(None); logging.info(f"[HLS Fetcher] Fetcher for track '{track['key']}' finished.")

# ==============================================================================
# --- 4. FFmpeg PROCESS MANAGER (SIMPLIFIED SINGLE PROCESS) ---
# ==============================================================================
class FFmpegProcess:
    def __init__(self, ffmpeg_args, on_error_callback=None):
        self.ffmpeg_args = ffmpeg_args
        self.on_error_callback = on_error_callback
        self.process = None
        self._stop_event, self._stop_lock = threading.Event(), threading.Lock()

    def start(self):
        video_url = self.ffmpeg_args['video_url']
        audio_url = self.ffmpeg_args['audio_url']
        keys = self.ffmpeg_args.get('keys') or {}
        key = self.ffmpeg_args.get('key')
        
        cmd = [FFMPEG_BIN, '-y', '-hide_banner', '-loglevel', 'error']
        
        if keys: # Widevine (uses first available key)
            decryption_key = list(keys.values())[0]
            cmd.extend(['-decryption_key', decryption_key, '-i', video_url])
            cmd.extend(['-decryption_key', decryption_key, '-i', audio_url])
        elif key: # CENC
            cmd.extend(['-decryption_key', key, '-i', video_url])
            cmd.extend(['-decryption_key', key, '-i', audio_url])
        else:
             raise ValueError("FFmpeg requires keys for decryption")

        cmd.extend([
            '-map', '0:v', '-map', '1:a',
            '-c:v', 'copy', '-c:a', 'copy',
            '-fflags', '+genpts', '-copyts', '-bsf:a', 'aac_adtstoasc',
            '-f', 'mpegts',
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            '-' # Output to stdout
        ])
        
        logging.info(f"Starting single FFmpeg process for video: {video_url}")
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creation_flags)
        
        threading.Thread(target=self._capture_stderr, daemon=True).start()
        threading.Thread(target=self._monitor_process, daemon=True).start()
        logging.info("FFmpeg process started.")
        
    def stop(self):
        with self._stop_lock:
            if self._stop_event.is_set(): return
            logging.info("Stopping FFmpeg process..."); self._stop_event.set()
        
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate(); self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()
            except Exception: pass
        logging.info("FFmpeg process stopped.")

    def _capture_stderr(self,):
        for line in iter(self.process.stderr.readline, b''):
            if self._stop_event.is_set(): break
            decoded = line.decode('utf-8', errors='ignore').strip()
            if decoded: logging.debug(f"FFmpeg: {decoded}")
            
    def _monitor_process(self):
        self.process.wait()
        if not self._stop_event.is_set():
            logging.error("FFmpeg process terminated unexpectedly.")
            if self.on_error_callback: self.on_error_callback()

# ==============================================================================
# --- 5. UNIFIED STREAM MANAGER & STREAM CLASS ---
# ==============================================================================
class StreamManager:
    def __init__(self):
        self.streams, self.lock = {}, threading.Lock()
        self.channel_cache, self.cache_lock, self.cache_expiry = {}, threading.Lock(), 3600
        self.custom_streams_config, self.custom_streams_lock = {}, threading.Lock()
        self.key_cache, self.key_cache_lock = {}, threading.Lock() # NEW: Cache for stream keys
        self.load_custom_streams()
    def load_custom_streams(self):
        with self.custom_streams_lock:
            if os.path.exists(STREAMS_JSON_FILE):
                try:
                    with open(STREAMS_JSON_FILE, 'r') as f: self.custom_streams_config = json.load(f)
                    logging.info(f"Loaded {len(self.custom_streams_config)} custom streams.")
                except json.JSONDecodeError: self.custom_streams_config = {}
    def save_custom_streams(self):
        with self.custom_streams_lock:
            with open(STREAMS_JSON_FILE, 'w') as f: json.dump(self.custom_streams_config, f, indent=4)
    def add_custom_stream(self, stream_id, config):
        with self.custom_streams_lock:
            if stream_id in self.custom_streams_config: return False, f"Stream ID '{stream_id}' already exists."
            self.custom_streams_config[stream_id] = config
        self.save_custom_streams(); return True, f"Stream '{stream_id}' added."
    def update_custom_stream(self, stream_id, new_config):
        with self.custom_streams_lock:
            if stream_id not in self.custom_streams_config: return False, "Stream not found"
            self.custom_streams_config[stream_id].update(new_config)
        self.save_custom_streams()
        with self.lock:
            if stream_id in self.streams:
                logging.info(f"Restarting stream '{stream_id}' to apply updates.")
                self.restart_stream(stream_id)
        return True, "Stream updated successfully."
    def remove_custom_stream(self, stream_id):
        with self.custom_streams_lock:
            if stream_id in self.custom_streams_config:
                del self.custom_streams_config[stream_id]
                self.save_custom_streams(); self.stop_stream(stream_id); return True
        return False
    def get_stream_title(self, stream_id):
        with self.custom_streams_lock:
            if stream_id in self.custom_streams_config: return self.custom_streams_config[stream_id].get('title', stream_id)
        return self.get_sling_channel_title(stream_id)
    def _refresh_sling_channels(self):
        logging.info("Refreshing Sling TV channel cache...")
        client = SlingClient(SLING_JWT, CoreConfig(wvd_path=WVD_FILE))
        channels = client.get_channel_list()
        with self.cache_lock:
            self.channel_cache['channels'] = {c['id']: c['title'] for c in channels}
            self.channel_cache['expiry'] = time.time() + self.cache_expiry
        logging.info(f"Cache refreshed with {len(channels)} channels.")
    def get_sling_channel_title(self, channel_id):
        with self.cache_lock: is_stale = not self.channel_cache or time.time() >= self.channel_cache.get('expiry', 0)
        if is_stale: self._refresh_sling_channels()
        with self.cache_lock: return self.channel_cache.get('channels', {}).get(channel_id, channel_id)
    def get_all_sling_channels(self):
        with self.cache_lock: is_stale = not self.channel_cache or time.time() >= self.channel_cache.get('expiry', 0)
        if is_stale: self._refresh_sling_channels()
        with self.cache_lock: return self.channel_cache.get('channels', {})
    def get_or_start_stream(self, stream_id):
        with self.lock:
            if stream_id in self.streams: return self.streams[stream_id]
            with self.custom_streams_lock:
                config = self.custom_streams_config.get(stream_id, {'type': 'sling', 'id': stream_id, 'title': self.get_sling_channel_title(stream_id)})
            logging.info(f"Starting new TCP stream for '{config.get('title', stream_id)}'")
            stream = TCPStream(stream_id, config); stream.start()
            self.streams[stream_id] = stream; return stream
    def stop_stream(self, stream_id):
        with self.lock:
            if stream_id in self.streams: logging.info(f"Stopping TCP stream for {stream_id}"); self.streams.pop(stream_id).stop()
    def restart_stream(self, stream_id):
        with self.lock:
            if stream_id in self.streams:
                logging.info(f"Restarting stream for {stream_id}")
                self.streams.pop(stream_id).stop()
                time.sleep(2)
                with self.custom_streams_lock:
                    config = self.custom_streams_config.get(stream_id, {'type': 'sling', 'id': stream_id, 'title': self.get_sling_channel_title(stream_id)})
                new_stream = TCPStream(stream_id, config); new_stream.start(); self.streams[stream_id] = new_stream
                return True
        return False
    def get_status(self):
        server_ip = get_local_ip()
        with self.lock: return {sid: {'status': s.status, 'clients': s.client_count, 'port': s.port, 'title': s.config.get('title', sid), 'uptime': s.uptime, 'error': s.error_message, 'type': s.config.get('type'), 'server_ip': server_ip} for sid, s in self.streams.items()}
    def janitor_run(self):
        while True:
            time.sleep(30); idle_streams = []
            with self.lock:
                now = time.time()
                for sid, stream in self.streams.items():
                    if stream.client_count > 0: stream.last_activity = now
                    if stream.client_count == 0 and now - stream.last_activity > IDLE_TIMEOUT_SECONDS:
                        logging.info(f"Stream '{sid}' is idle. Shutting down."); idle_streams.append(sid)
            for sid in idle_streams: self.stop_stream(sid)
    def shutdown_all_streams(self):
        logging.info("Shutting down. Stopping all streams.")
        with self.lock: channel_ids = list(self.streams.keys())
        for channel_id in channel_ids: self.stop_stream(channel_id)
    def clear_key_cache(self): # NEW: Method to clear the key cache
        with self.key_cache_lock:
            count = len(self.key_cache)
            self.key_cache.clear()
            logging.info(f"Cleared {count} items from the key cache.")
            return count

class TCPStream:
    def __init__(self, stream_id, config):
        self.stream_id, self.config = stream_id, config
        self.port, self.status = self._get_free_port(), "INITIALIZING"
        self.error_message, self.client_count = None, 0
        self.last_activity, self.start_time = time.time(), None
        self._stop_event, self._client_queues = threading.Event(), set()
        self._ffmpeg = None
    @property
    def uptime(self): return time.time() - self.start_time if self.status == "STREAMING" and self.start_time else 0
    def start(self):
        threading.Thread(target=self._run_stream_pipeline, name=f"Streamer-{self.stream_id}", daemon=True).start()
        threading.Thread(target=self._run_tcp_server, name=f"TCPServer-{self.stream_id}", daemon=True).start()
    def stop(self):
        if self._stop_event.is_set(): return
        self._stop_event.set()
        if self._ffmpeg: self._ffmpeg.stop()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s: s.settimeout(1.0); s.connect(('127.0.0.1', self.port))
        except Exception: pass
    def _get_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s: s.bind(('0.0.0.0', 0)); return s.getsockname()[1]
    def _run_tcp_server(self):
        try:
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM); server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind(('0.0.0.0', self.port)); server_socket.listen(5)
            logging.info(f"TCP server for {self.stream_id} listening on port {self.port}")
            while not self._stop_event.is_set():
                client_socket, addr = server_socket.accept()
                if self._stop_event.is_set(): client_socket.close(); break
                logging.info(f"Accepted connection from {addr} for stream {self.stream_id}")
                threading.Thread(target=self._handle_client, args=(client_socket,), daemon=True).start()
        except Exception: pass
        finally: server_socket.close()
    def _handle_client(self, client_socket):
        self.client_count += 1; self.last_activity = time.time(); q = queue.Queue(maxsize=100); self._client_queues.add(q)
        try:
            client_socket.recv(4096); client_socket.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n")
            while not self._stop_event.is_set():
                chunk = q.get();
                if chunk is None: break
                client_socket.sendall(chunk)
        except (socket.error, BrokenPipeError): pass
        finally: self.client_count -= 1; self._client_queues.discard(q); client_socket.close()
    def _pump_ffmpeg_to_clients(self, error_event):
        while not self._stop_event.is_set() and not error_event.is_set():
            try:
                chunk = self._ffmpeg.process.stdout.read(8192)
                if not chunk: logging.info(f"FFmpeg muxer for {self.stream_id} stdout ended."); break
                for q in list(self._client_queues):
                    try: q.put(chunk, block=False)
                    except queue.Full: pass
            except Exception: break
    def _run_stream_pipeline(self):
        try:
            retry_count = 0; max_retries = 5; ffmpeg_error_event = threading.Event()
            def on_ffmpeg_error(): ffmpeg_error_event.set()
            while not self._stop_event.is_set() and retry_count < max_retries:
                used_cached_keys = False
                try:
                    ffmpeg_error_event.clear()
                    if self._ffmpeg: self._ffmpeg.stop()
                    ffmpeg_args = {}
                    stream_type = self.config.get('type')

                    if stream_type == 'sling':
                        self.status = "AUTHENTICATING"
                        keys = None
                        stream_url = None
                        
                        # --- START: Key Cache Check ---
                        with stream_manager.key_cache_lock:
                            if self.stream_id in stream_manager.key_cache:
                                cache_entry = stream_manager.key_cache[self.stream_id]
                                if time.time() < cache_entry['expiry']:
                                    logging.info(f"Using cached keys for stream {self.stream_id}")
                                    keys = cache_entry['keys']
                                    stream_url = cache_entry['stream_url']
                                    used_cached_keys = True
                                else:
                                    logging.info(f"Cached keys for {self.stream_id} have expired.")
                        # --- END: Key Cache Check ---

                        if not keys or not stream_url:
                            core_config = CoreConfig(WVD_FILE)
                            client = SlingClient(SLING_JWT, core_config)
                            widevine = WidevineManager(core_config)
                            
                            stream_url, key_url, temp_jwt = client.authenticate_stream(self.stream_id)
                            if not stream_url: raise RuntimeError("Sling auth failed")
                            pssh = client.get_pssh_from_hls(key_url)
                            if not pssh: raise RuntimeError("Failed to get PSSH")
                            keys = widevine.get_decryption_keys(pssh, temp_jwt)
                            if not keys: raise RuntimeError("Failed to get decryption keys")
                            
                            # --- START: Save to Key Cache ---
                            with stream_manager.key_cache_lock:
                                expiry_time = time.time() + KEY_CACHE_EXPIRY_SECONDS
                                stream_manager.key_cache[self.stream_id] = {'keys': keys, 'stream_url': stream_url, 'expiry': expiry_time}
                                logging.info(f"Cached new keys for stream {self.stream_id}")
                            # --- END: Save to Key Cache ---
                        
                        encoded_m3u8 = quote_plus(stream_url)
                        video_source_url = f"http://127.0.0.1:{HTTP_PORT}/proxy/hls/video/{encoded_m3u8}"
                        audio_source_url = f"http://127.0.0.1:{HTTP_PORT}/proxy/hls/audio/{encoded_m3u8}"
                        ffmpeg_args = {'video_url': video_source_url, 'audio_url': audio_source_url, 'keys': keys}
                    
                    elif stream_type == 'dash':
                        # ... (no changes to dash or hls types)
                        self.status = "PARSING_DASH"; mpd_url = self.config['mpd_url']; key = self.config.get('key')
                        if not key: raise RuntimeError("DASH stream requires a decryption key")
                        encoded_mpd = quote_plus(mpd_url)
                        video_source_url = f"http://127.0.0.1:{HTTP_PORT}/proxy/dash/video/{encoded_mpd}"
                        audio_source_url = f"http://127.0.0.1:{HTTP_PORT}/proxy/dash/audio/{encoded_mpd}"
                        ffmpeg_args = {'video_url': video_source_url, 'audio_url': audio_source_url, 'key': key}
                    elif stream_type == 'hls':
                        self.status = "PARSING_HLS"; m3u8_url = self.config['m3u8_url']; key = self.config.get('key')
                        if not key: raise RuntimeError("HLS stream requires a decryption key")
                        encoded_m3u8 = quote_plus(m3u8_url)
                        video_source_url = f"http://127.0.0.1:{HTTP_PORT}/proxy/hls/video/{encoded_m3u8}"
                        audio_source_url = f"http://127.0.0.1:{HTTP_PORT}/proxy/hls/audio/{encoded_m3u8}"
                        ffmpeg_args = {'video_url': video_source_url, 'audio_url': audio_source_url, 'key': key}
                    else: raise RuntimeError(f"Unknown stream type: {stream_type}")
                    
                    self.status = "STARTING_FFMPEG"
                    self._ffmpeg = FFmpegProcess(ffmpeg_args, on_error_callback=on_ffmpeg_error); self._ffmpeg.start()
                    self.status = "STREAMING"; self.start_time = time.time(); self.error_message = None; retry_count = 0
                    self._pump_ffmpeg_to_clients(ffmpeg_error_event)
                    if self._stop_event.is_set() or self.client_count == 0: break
                    logging.warning(f"FFmpeg pipeline for {self.stream_id} stopped. Retrying...")
                    retry_count += 1; time.sleep(5)
                except Exception as e:
                    self.status = "ERROR"; self.error_message = str(e); self.start_time = None
                    logging.error(f"CRITICAL STREAM FAILURE for {self.stream_id}: {e}", exc_info=True)
                    
                    # --- START: Stale Cache Invalidation ---
                    if used_cached_keys:
                        logging.warning(f"Failure occurred with cached keys for {self.stream_id}. Invalidating cache entry.")
                        with stream_manager.key_cache_lock:
                            stream_manager.key_cache.pop(self.stream_id, None)
                    # --- END: Stale Cache Invalidation ---

                    retry_count += 1
                    if self.client_count > 0 and not self._stop_event.is_set(): time.sleep(10)
                    else: break
        finally:
            self.start_time = None
            if self.status != "ERROR": self.status = "STOPPED"
            for q in list(self._client_queues): q.put(None)
            self.stop()

# ==============================================================================
# --- 6. APPLICATION STARTUP & FLASK ROUTES ---
# ==============================================================================
stream_manager = StreamManager()
app = Flask(__name__, template_folder=TEMPLATE_FOLDER)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(('10.255.255.255', 1)); IP = s.getsockname()[0]
    except Exception: IP = '127.0.0.1'
    finally: s.close()
    return IP

@app.route('/')
def index(): return render_template('index_tcp.html', server_ip=get_local_ip(), server_port=HTTP_PORT)
@app.route('/api/channels')
def api_get_channels():
    channels_dict = stream_manager.get_all_sling_channels()
    return jsonify(sorted([{'id': k, 'title': v} for k, v in channels_dict.items()], key=lambda x: x['title']))
@app.route('/api/streams', methods=['GET'])
def api_get_custom_streams():
    with stream_manager.custom_streams_lock: return jsonify(stream_manager.custom_streams_config)
@app.route('/api/streams', methods=['POST'])
def api_add_custom_stream():
    data = request.json
    sid = data.get('id') or re.sub(r'[^a-zA-Z0-9]', '', data.get('title', 'custom')).lower()
    if not sid or not data.get('key'): return jsonify({"success": False, "message": "ID and Key are required."}), 400
    if data.get('mpd_url'): config = {'type': 'dash', 'title': data.get('title', sid), 'mpd_url': data['mpd_url'], 'key': data['key']}
    elif data.get('m3u8_url'): config = {'type': 'hls', 'title': data.get('title', sid), 'm3u8_url': data['m3u8_url'], 'key': data['key']}
    else: return jsonify({"success": False, "message": "Either mpd_url or m3u8_url must be provided."}), 400
    success, message = stream_manager.add_custom_stream(sid, config)
    return jsonify({"success": success, "message": message}), 200 if success else 409
@app.route('/api/streams/<stream_id>', methods=['PUT'])
def api_update_custom_stream(stream_id):
    data = request.json
    if not data or not data.get('key'): return jsonify({"success": False, "message": "Key is required for update."}), 400
    if data.get('mpd_url'): config = {'type': 'dash', 'title': data.get('title', stream_id), 'mpd_url': data['mpd_url'], 'key': data['key']}
    elif data.get('m3u8_url'): config = {'type': 'hls', 'title': data.get('title', stream_id), 'm3u8_url': data['m3u8_url'], 'key': data['key']}
    else: return jsonify({"success": False, "message": "Either mpd_url or m3u8_url must be provided."}), 400
    success, message = stream_manager.update_custom_stream(stream_id, config)
    return jsonify({"success": success, "message": message}), 200 if success else 404
@app.route('/api/streams/<stream_id>', methods=['DELETE'])
def api_remove_custom_stream(stream_id):
    success = stream_manager.remove_custom_stream(stream_id)
    return jsonify({"success": success, "message": f"Stream '{stream_id}' removed." if success else "Stream not found."}), 200 if success else 404
@app.route('/api/status')
def api_get_status(): return jsonify(stream_manager.get_status())
@app.route('/api/stream/stop/<stream_id>', methods=['POST'])
def api_stop_stream(stream_id):
    stream_manager.stop_stream(stream_id)
    return jsonify({"success": True, "message": f"Stop command sent to stream {stream_id}."})
@app.route('/api/stream/start/<stream_id>', methods=['POST'])
def api_start_stream(stream_id):
    stream = stream_manager.get_or_start_stream(stream_id)
    return jsonify({"success": True, "message": f"Start command sent to stream {stream_id}.", "port": stream.port})
@app.route('/api/stream/restart/<stream_id>', methods=['POST'])
def api_restart_stream(stream_id):
    success = stream_manager.restart_stream(stream_id)
    return jsonify({"success": success, "message": f"Restart command sent to stream {stream_id}."})
@app.route('/api/cache/clear', methods=['POST']) # NEW: API endpoint to clear key cache
def api_clear_cache():
    count = stream_manager.clear_key_cache()
    return jsonify({"success": True, "message": f"Successfully cleared {count} items from the key cache."})
@app.route('/stream/<stream_id>')
def get_stream_playlist(stream_id):
    stream = stream_manager.get_or_start_stream(stream_id); start_time = time.time()
    while stream.status not in ["STREAMING", "ERROR"] and time.time() - start_time < 20: time.sleep(0.5)
    if stream.status == "ERROR":
        stream_manager.restart_stream(stream_id); stream = stream_manager.get_or_start_stream(stream_id)
    return redirect(f"http://{get_local_ip()}:{stream.port}")

# --- PROXY ROUTES ---
# ... (Proxy routes are unchanged) ...
def _create_stream_generator(fetcher_func, args):
    q = queue.Queue(maxsize=10); stop_event = threading.Event(); session = requests.Session()
    fetcher = threading.Thread(target=fetcher_func, args=(q, session, *args, stop_event), daemon=True)
    fetcher.start()
    try:
        while True:
            segment = q.get();
            if segment is None: break
            yield segment
    finally: stop_event.set(); session.close(); fetcher.join(timeout=5)

@app.route('/proxy/dash/<stream_type>/<path:mpd_url_path>')
def dash_stream(stream_type, mpd_url_path):
    if stream_type not in ['video', 'audio']: abort(404)
    mpd_url = unquote_plus(mpd_url_path)
    initial_tracks, mpd_type = parse_mpd(mpd_url)
    if not initial_tracks: abort(500, f"Error: Could not parse MPD from {mpd_url}")
    target_tracks = {k: v for k, v in initial_tracks.items() if stream_type in k.lower() and v['urls']}
    if not target_tracks: abort(404, f"No '{stream_type}' tracks found.")
    best_track = max(target_tracks.values(), key=lambda t: t['bandwidth'])
    generator = _create_stream_generator(dash_segment_fetcher, (best_track, mpd_type, mpd_url))
    return Response(stream_with_context(generator), mimetype='video/mp4')

@app.route('/proxy/hls/<stream_type>/<path:m3u8_url_path>')
def hls_stream(stream_type, m3u8_url_path):
    if stream_type not in ['video', 'audio']: abort(404)
    m3u8_url = unquote_plus(m3u8_url_path)
    initial_tracks, m3u8_type = parse_m3u8(m3u8_url)
    if not initial_tracks: abort(500, f"Error: Could not parse M3U8 from {m3u8_url}")
    target_tracks = {k: v for k, v in initial_tracks.items() if stream_type in v['type'].lower()}
    if not target_tracks: abort(404, f"No '{stream_type}' tracks found.")
    best_track = max(target_tracks.values(), key=lambda t: t.get('bandwidth', 0))
    generator = _create_stream_generator(hls_segment_fetcher, (best_track, m3u8_type))
    return Response(stream_with_context(generator), mimetype='video/mp2t')

def run_web_server():
    janitor_thread = threading.Thread(target=stream_manager.janitor_run, name="Janitor", daemon=True); janitor_thread.start()
    logging.info(f"Starting Sling+DASH+HLS TCP Streamer server at http://{get_local_ip()}:{HTTP_PORT}")
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    if not SLING_JWT or "YOUR_SLING_JWT" in SLING_JWT: logging.warning("SLING_JWT not set or is a placeholder! Sling TV functionality will fail.")
    if not os.path.exists(WVD_FILE): logging.warning(f"{WVD_FILE} not found! Sling TV functionality will fail.")
    if not os.path.exists(FFMPEG_BIN): logging.critical(f"FATAL: {FFMPEG_BIN} not found!"); sys.exit(1)
    
    def shutdown_handler(signum=None, frame=None):
        stream_manager.shutdown_all_streams(); time.sleep(1)
        if signum: sys.exit(0)
    atexit.register(shutdown_handler); signal.signal(signal.SIGINT, shutdown_handler); signal.signal(signal.SIGTERM, shutdown_handler)
    
    run_web_server()

