"""Verified, content-addressed generated media. Never submits a paid generation."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
from urllib.parse import quote, urljoin, urlsplit


class WorkflowError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise WorkflowError(code, message)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run_media(args, timeout=120):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError('media_tool_unavailable', f'{args[0]}: {type(exc).__name__}') from exc
    require(result.returncode == 0, 'invalid_media', 'Media decoder failed; no asset was accepted.')
    return result


def probe(path):
    """Inspect and decode the entire file; reject HTML, partial downloads and empty media."""
    path = Path(path).resolve()
    require(path.is_file() and path.stat().st_size > 0, 'missing_media', 'Media file is missing or empty.')
    data = json.loads(run_media(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_format', '-show_streams',
                                 '-of', 'json', str(path)]).stdout)
    streams = data.get('streams', [])
    require(any(s.get('codec_type') in ('audio', 'video') for s in streams),
            'invalid_media', 'File contains no audio or video stream.')
    run_media(['ffmpeg', '-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v?',
               '-map', '0:a?', '-f', 'null', '-'], timeout=300)
    video = next((s for s in streams if s.get('codec_type') == 'video'), {})
    fmt = data.get('format', {})
    still = fmt.get('format_name') in ('image2', 'png_pipe', 'jpeg_pipe', 'webp_pipe', 'bmp_pipe', 'tiff_pipe')
    from fractions import Fraction
    try:
        fps = float(Fraction(video.get('avg_frame_rate', '0/1')))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = float(fmt.get('duration') or max((float(s.get('duration') or 0) for s in streams), default=0))
    require(still or duration > 0, 'unknown_duration', 'Timed media has no usable duration.')
    return {'sha256': digest(path), 'bytes': path.stat().st_size, 'duration_seconds': duration,
            'kind': 'image' if still else ('video' if video else 'audio'), 'fps': fps,
            'width': video.get('width'), 'height': video.get('height'),
            'has_audio': any(s.get('codec_type') == 'audio' for s in streams),
            'format': fmt.get('format_name'), 'codec': video.get('codec_name')}


def _extension(info):
    if info['kind'] == 'image':
        return {'png': '.png', 'mjpeg': '.jpg', 'webp': '.webp', 'bmp': '.bmp', 'tiff': '.tif'}.get(info['codec'], '.img')
    names = info['format'].split(',')
    for name, ext in [('wav', '.wav'), ('mp3', '.mp3'), ('flac', '.flac'), ('ogg', '.ogg'),
                      ('mov', '.mov'), ('matroska', '.mkv'), ('avi', '.avi'), ('aac', '.aac')]:
        if name in names:
            return ext
    raise WorkflowError('unsupported_container', f'Unsupported media container: {info["format"]}')


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to the validated public address, retaining hostname TLS verification."""
    def __init__(self, host, address):
        super().__init__(host, timeout=30, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(url, target, max_bytes=1024 * 1024 * 1024, headers=None):
    """Bounded HTTPS fetch. No credentials on redirect; no private-network URL access."""
    original = urlsplit(url).hostname
    for _ in range(6):
        parts = urlsplit(url)
        require(parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password
                and parts.port in (None, 443), 'unsafe_url', 'Only public HTTPS URLs on port 443 are accepted.')
        addresses = {v[4][0] for v in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)}
        require(addresses and all(ipaddress.ip_address(a).is_global for a in addresses),
                'unsafe_url', 'Download address must be public.')
        conn = _PinnedHTTPS(parts.hostname, sorted(addresses)[0])
        try:
            conn.request('GET', (parts.path or '/') + ('?' + parts.query if parts.query else ''),
                         headers=(headers or {}) if parts.hostname == original else {})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader('Location', ''))
                headers = {}  # never forward API credentials on any redirect
                continue
            require(response.status == 200, 'download_failed', f'Download returned HTTP {response.status}; retry download, not generation.')
            length = response.getheader('Content-Length')
            require(length is None or int(length) <= max_bytes, 'file_too_large', 'Download exceeds byte limit.')
            total = 0
            with open(target, 'wb') as output:
                while True:
                    chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    require(total <= max_bytes, 'file_too_large', 'Download exceeds byte limit.')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            require(total and (length is None or total == int(length)), 'incomplete_download', 'Download was truncated.')
            return
        finally:
            conn.close()
    raise WorkflowError('download_failed', 'Too many redirects.')


def ingest(root, *, local_path=None, url=None, expected_sha256=None, max_bytes=1024 * 1024 * 1024):
    require(bool(local_path) != bool(url), 'invalid_params', 'Supply exactly one of local_path or url.')
    require(isinstance(max_bytes, int) and 0 < max_bytes <= 4 * 1024**3, 'invalid_params', 'max_bytes must be 1..4 GiB.')
    folder = Path(root) / 'assets'
    folder.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.download-', dir=folder)
    os.close(fd)
    try:
        if local_path:
            source = Path(local_path).expanduser().resolve()
            require(source.is_file() and source.stat().st_size <= max_bytes, 'missing_media', 'Local media missing or exceeds byte limit.')
            shutil.copyfile(source, temp)
        else:
            fetch(url, temp, max_bytes)
        info = probe(temp)
        require(not expected_sha256 or expected_sha256.lower() == info['sha256'], 'checksum_mismatch', 'SHA-256 did not match; file rejected.')
        destination = folder / (info['sha256'] + _extension(info))
        if destination.exists():
            require(digest(destination) == info['sha256'], 'corrupt_asset', 'Existing managed asset has changed.')
        else:
            os.replace(temp, destination)
        return dict(info, local_path=str(destination))
    finally:
        Path(temp).unlink(missing_ok=True)


def elevenlabs_result(generation_id, kind):
    require(kind in ('image', 'video'), 'invalid_params', 'API lookup supports image or video; audio accepts a completed download/URL.')
    key = os.environ.get('ELEVENLABS_API_KEY')
    require(key, 'missing_credentials', 'Set ELEVENLABS_API_KEY in the MCP environment, or supply a completed file/URL.')
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / 'result.json'
        fetch(f'https://api.elevenlabs.io/v1/flows/{kind}/{quote(generation_id, safe="")}', path,
              max_bytes=2 * 1024**2, headers={'xi-api-key': key})
        data = json.loads(path.read_text())
    require(data.get('status') == 'completed', 'generation_not_completed',
            f'Existing generation status: {data.get("status", "unknown")}. Poll this ID; do not generate again.')
    require(data.get('content_url'), 'missing_result_url', 'Completed generation has no content_url.')
    return data
