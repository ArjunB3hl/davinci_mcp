"""Resolve adapter: explicit project binding, deterministic assembly, reversible takes."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re
import struct
import uuid
import zlib

from src.utils.generated_assets import require, probe, run_media, digest
from src.utils.generated_media import generation, verified_asset
from src.utils.resolve_probe import has_method


def capabilities(resolve):
    if resolve is None:
        return {'connected': False, 'remediation': 'On Free 20.3.2 start Workspace > Scripts > resolve_bridge.',
                'ffmpeg_required': True}
    product = resolve.GetProductName()
    version = resolve.GetVersionString()
    proj = resolve.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool() if proj else None
    timeline = proj.GetCurrentTimeline() if proj else None
    item = None
    if timeline:
        clips = timeline.GetItemListInTrack('video', 1) or []
        item = clips[0] if clips else None
    import shutil
    return {'connected': True, 'product': product, 'version': version,
            'edition': 'Studio' if 'studio' in str(product).lower() else 'Free (inferred from product name)',
            'project_name': proj.GetName() if proj else None,
            'project_id': str(proj.GetUniqueId()) if proj else None,
            'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
            'read_only_probe': True,
            'exposed_methods': {name: has_method(obj, name) if obj else None for obj, name in [
                (mp, 'ImportMedia'), (mp, 'CreateEmptyTimeline'), (mp, 'AppendToTimeline'),
                (item, 'AddTake'), (item, 'SelectTakeByIndex'), (item, 'GetTakeByIndex')]},
            'verification': 'Method exposure is not proof of working behavior. Run the synthetic live harness for that.',
            'excluded_from_workflow': {'Studio_AI': 'No Magic Mask, Smart Reframe, or native AI transcription calls.',
                'Resolve_21_features': 'No 21.x-only generation, IntelliSearch, native speed/fades or IsStudio calls.',
                'visual_identity': 'Character continuity and unwanted text require host image review; detection is imperfect.'}}


def bin_path(scene_id):
    slug = re.sub(r'[^a-zA-Z0-9_-]+', '_', scene_id)[:50]
    return 'Generated/' + slug + '-' + hashlib.sha256(scene_id.encode()).hexdigest()[:8]


def walk(folder):
    yield folder
    for child in folder.GetSubFolderList() or []:
        yield from walk(child)


def matches_asset(clip, asset):
    value = clip.GetClipProperty('File Path') if clip else None
    if not value:
        return False
    path = Path(value).resolve()
    try:
        return path.is_file() and path.stat().st_size == asset['bytes'] and digest(path) == asset['sha256']
    except OSError:
        return False


def import_asset(proj, asset, scene_id):
    mp = proj.GetMediaPool()
    path = str(Path(asset['local_path']).resolve())
    root = mp.GetRootFolder()
    found = None
    for folder in walk(root):
        for clip in folder.GetClipList() or []:
            if matches_asset(clip, asset):
                found = clip
                break
        if found:
            break
    target = root
    for name in bin_path(scene_id).split('/'):
        child = next((f for f in target.GetSubFolderList() or [] if f.GetName() == name), None)
        target = child or mp.AddSubFolder(target, name)
        require(target is not None, 'bin_failed', 'Could not create generated media bin.')
    if found:
        # The same bytes may legitimately serve multiple scenes; retain one Media Pool item.
        return found, True
    previous = mp.GetCurrentFolder()
    try:
        require(mp.SetCurrentFolder(target), 'bin_failed', 'Could not select target bin.')
        clips = mp.ImportMedia([path]) or []
        require(len(clips) == 1, 'import_failed', 'Resolve did not import exactly one asset. Retry import; generation remains complete.')
        clip = clips[0]
        require(str(Path(clip.GetClipProperty('File Path')).resolve()) == path,
                'import_readback_failed', 'Resolve imported an unexpected file.')
        return clip, False
    finally:
        if previous:
            mp.SetCurrentFolder(previous)


def frames(seconds, fps):
    return int(Fraction(str(seconds)) * fps + Fraction(1, 2))


def asset_slot(g, count, fps, kind):
    asset = verified_asset(g)
    require(asset['kind'] in (('video',) if kind == 'video' else ('audio', 'video')),
            'wrong_media_type', f'Expected {kind} media. Still images can be imported/reviewed, but planned-duration storyboard shots require video; Resolve 20.3.2 ignores still append extents.')
    if asset['kind'] == 'video' and kind == 'video':
        require(abs(asset['fps'] - float(fps)) < .01, 'fps_mismatch', 'Video and storyboard frame rates differ; conform explicitly before assembly.')
    if asset['kind'] != 'image':
        available = math.floor(asset['duration_seconds'] * float(fps) + .01)
        require(available >= count, 'duration_mismatch', f'Asset has {available} frames; slot needs {count}. No implicit looping or retiming.')
    return asset


def plan_storyboard(state, p):
    fps = Fraction(str(p.get('fps', 24)))
    require(1 <= fps <= 120, 'invalid_fps', 'fps must be 1..120, optionally a fraction such as 24000/1001.')
    rows = p['scenes']
    require(isinstance(rows, list) and 0 < len(rows) <= 500, 'invalid_scenes', 'Supply 1..500 ordered scene entries.')
    slots = []
    cursor = Fraction(0)
    for row in rows:
        scene_id = row['scene_id']
        require(scene_id in state['scenes'], 'unknown_scene', scene_id)
        scene = state['scenes'][scene_id]
        start = frames(cursor, fps)
        cursor += Fraction(str(scene['duration_seconds']))
        count = frames(cursor, fps) - start
        require(count > 0, 'invalid_duration', 'Scene is shorter than one frame.')
        entry = {'scene_id': scene_id, 'start': start, 'frames': count, 'prompt': scene['prompt'],
                 'record_id': row.get('record_id'), 'voiceover_record_id': row.get('voiceover_record_id')}
        if entry['record_id']:
            g = generation(state, entry['record_id'])
            require(g['scene_id'] == scene_id, 'wrong_scene', 'Visual record belongs to another scene.')
            entry['sha256'] = asset_slot(g, count, fps, 'video')['sha256']
        if entry['voiceover_record_id']:
            g = generation(state, entry['voiceover_record_id'])
            asset = verified_asset(g)
            audio_frames = min(count, math.floor(asset['duration_seconds'] * float(fps) + .01))
            require(audio_frames > 0, 'invalid_duration', 'Voiceover is empty.')
            entry['voiceover_frames'] = audio_frames
            entry['voiceover_sha256'] = asset_slot(g, audio_frames, fps, 'audio')['sha256']
            entry['voiceover_trimmed'] = asset['duration_seconds'] > count / float(fps) + .01
        slots.append(entry)
    total = frames(cursor, fps)
    music_id = p.get('music_record_id')
    music = None
    if music_id:
        g = generation(state, music_id)
        asset = verified_asset(g)
        count = min(total, math.floor(asset['duration_seconds'] * float(fps) + .01))
        require(count > 0, 'invalid_duration', 'Music is empty.')
        music = {'record_id': music_id, 'frames': count, 'sha256': asset_slot(g, count, fps, 'audio')['sha256'],
                 'trimmed': asset['duration_seconds'] > total / float(fps) + .01}
    key = uuid.uuid4().hex
    plan = {'id': key, 'type': 'storyboard', 'fps': str(fps), 'slots': slots, 'total_frames': total,
            'music': music, 'timeline_name': str(p.get('name', 'Generated storyboard'))[:100] + ' [' + key[:12] + ']',
            'project_id': state['project_id'], 'status': 'planned', 'timeline_id': None,
            'notes': ['Missing visual records become colored placeholder cards with scene markers.',
                      'Video audio is excluded; voiceover uses A1 and music uses A2. Audio longer than its slot is trimmed as shown in this plan.']}
    state['plans'][key] = plan
    return {'plan': plan}


def placeholder(root, scene_id):
    """Small source-independent PNG; scene/prompt label is a timeline marker."""
    seed = hashlib.sha256(scene_id.encode()).digest()
    path = Path(root) / 'placeholders' / (seed.hex() + '.png')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        width, height = 320, 180
        color = bytes(40 + v % 100 for v in seed[:3])
        raw = b''.join(b'\0' + color * width for _ in range(height))
        def chunk(tag, data):
            return struct.pack('!I', len(data)) + tag + data + struct.pack('!I', zlib.crc32(tag + data) & 0xffffffff)
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
                         + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    return dict(probe(path), local_path=str(path))


def placeholder_video(root, scene_id, count, fps):
    """Synthetic duration-accurate card; no existing media is transformed."""
    key = hashlib.sha256(f'{scene_id}:{count}:{fps}'.encode()).hexdigest()
    path = Path(root) / 'placeholders' / (key + '.mp4')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        color = hashlib.sha256(scene_id.encode()).hexdigest()[:6]
        temporary = path.with_suffix('.partial.mp4')
        try:
            run_media(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                       f'color=c=0x{color}:s=1280x720:r={fps}', '-frames:v', str(count),
                       '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(temporary)])
            probe(temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return dict(probe(path), local_path=str(path))


def timelines(proj):
    return [proj.GetTimelineByIndex(i) for i in range(1, proj.GetTimelineCount() + 1)]


def snapshot(timeline):
    rows = []
    for kind in ('video', 'audio', 'subtitle'):
        for track in range(1, (timeline.GetTrackCount(kind) or 0) + 1):
            for item in timeline.GetItemListInTrack(kind, track) or []:
                rows.append({'id': str(item.GetUniqueId()), 'kind': kind, 'track': track,
                             'start': item.GetStart(), 'duration': item.GetDuration()})
    return rows


def _timeline(proj, key):
    value = next((t for t in timelines(proj) if str(t.GetUniqueId()) == key), None)
    require(value is not None, 'missing_timeline', 'Planned timeline no longer exists.')
    return value


def _item(timeline, key):
    for track in range(1, (timeline.GetTrackCount('video') or 0) + 1):
        for item in timeline.GetItemListInTrack('video', track) or []:
            if str(item.GetUniqueId()) == key:
                return item
    require(False, 'missing_item', 'Target video item no longer exists.')


def build_storyboard(proj, state, p, root):
    plan = state['plans'][p['plan_id']]
    require(plan['type'] == 'storyboard', 'wrong_plan', 'Expected storyboard plan.')
    fps = Fraction(plan['fps'])
    # Validate every file and frozen fingerprint before touching Resolve.
    entries = []
    for slot in plan['slots']:
        if slot['record_id']:
            g = generation(state, slot['record_id'])
            asset = asset_slot(g, slot['frames'], fps, 'video')
            require(asset['sha256'] == slot['sha256'], 'stale_plan', 'Visual asset changed; re-plan.')
        else:
            asset = placeholder_video(root, slot['scene_id'], slot['frames'], fps)
        entries.append(dict(slot, asset=asset, kind='video', track=1))
        if slot['voiceover_record_id']:
            g = generation(state, slot['voiceover_record_id'])
            asset = asset_slot(g, slot['voiceover_frames'], fps, 'audio')
            require(asset['sha256'] == slot['voiceover_sha256'], 'stale_plan', 'Voiceover changed; re-plan.')
            entries.append(dict(slot, asset=asset, kind='audio', track=1, frames=slot['voiceover_frames']))
    if plan['music']:
        music = plan['music']
        g = generation(state, music['record_id'])
        asset = asset_slot(g, music['frames'], fps, 'audio')
        require(asset['sha256'] == music['sha256'], 'stale_plan', 'Music changed; re-plan.')
        entries.append({'scene_id': g['scene_id'], 'asset': asset, 'kind': 'audio', 'track': 2,
                        'frames': music['frames'], 'start': 0})
    mp = proj.GetMediaPool()
    previous = proj.GetCurrentTimeline()
    timeline = next((t for t in timelines(proj) if t.GetName() == plan['timeline_name']), None)
    if plan['timeline_id']:
        timeline = _timeline(proj, plan['timeline_id'])
    try:
        if timeline is None:
            timeline = mp.CreateEmptyTimeline(plan['timeline_name'])
            require(timeline is not None, 'timeline_failed', 'Could not create storyboard timeline.')
        require(proj.SetCurrentTimeline(timeline), 'timeline_failed', 'Could not select storyboard timeline.')
        existing = snapshot(timeline)
        if not existing:
            require(timeline.SetSetting('useCustomSettings', '1'), 'settings_failed', 'Could not enable custom timeline settings.')
            require(timeline.SetSetting('timelineFrameRate', format(float(fps), '.3f').rstrip('0').rstrip('.')), 'unsupported_fps', 'Resolve refused requested timeline FPS.')
        require(abs(float(timeline.GetSetting('timelineFrameRate')) - float(fps)) < .01,
                'fps_mismatch', 'Timeline frame rate differs from the plan.')
        origin = int(timeline.GetStartFrame())
        desired = {(e['kind'], e['track'], origin + e['start']): e for e in entries}
        require(len(desired) == len(entries), 'invalid_plan', 'Plan has overlapping starts.')
        for row in existing:
            wanted = desired.get((row['kind'], row['track'], row['start']))
            require(wanted and row['duration'] == wanted['frames'], 'timeline_changed', 'Storyboard was edited externally; create a new plan.')
        require(len({(r['kind'], r['track'], r['start']) for r in existing}) == len(existing),
                'timeline_changed', 'Timeline has duplicate slots.')
        result_items = []
        for entry in entries:
            kind, track = entry['kind'], entry['track']
            while (timeline.GetTrackCount(kind) or 0) < track:
                require(timeline.AddTrack(kind), 'track_failed', 'Could not create required track.')
            start = origin + entry['start']
            present = [i for i in timeline.GetItemListInTrack(kind, track) or [] if i.GetStart() == start]
            if present:
                item = present[0]
                clip = item.GetMediaPoolItem()
                require(matches_asset(clip, entry['asset']),
                        'timeline_changed', 'Existing slot points to different media; re-plan.')
            else:
                clip, _ = import_asset(proj, entry['asset'], entry['scene_id'])
                items = mp.AppendToTimeline([{'mediaPoolItem': clip, 'startFrame': 0, 'endFrame': entry['frames'],
                                             'recordFrame': start, 'mediaType': 1 if kind == 'video' else 2,
                                             'trackIndex': track}]) or []
                require(len(items) == 1, 'append_failed', 'Resolve did not append one item; retry this same plan.')
                item = items[0]
            require(item.GetStart() == start and item.GetDuration() == entry['frames'], 'append_readback_failed',
                    f'Resolve {kind} slot {entry["scene_id"]}: requested start={start}, duration={entry["frames"]}; observed start={item.GetStart()}, duration={item.GetDuration()}. Inspect partial storyboard before retrying.')
            result_items.append({'scene_id': entry['scene_id'], 'item_id': str(item.GetUniqueId()),
                                 'kind': kind, 'track': track, 'start': start, 'frames': entry['frames']})
        markers = timeline.GetMarkers() or {}
        for slot in plan['slots']:
            tag = 'generated-media:' + plan['id'] + ':' + str(slot['start'])
            if not any(m.get('customData') == tag for m in markers.values()):
                require(timeline.AddMarker(slot['start'], 'Blue' if slot['record_id'] else 'Yellow',
                                           slot['scene_id'], slot['prompt'][:2000], slot['frames'], tag),
                        'marker_failed', 'Could not label storyboard scene; retry same plan.')
        plan.update(status='built', timeline_id=str(timeline.GetUniqueId()), items=result_items)
        return {'plan': plan, 'timeline_name': timeline.GetName(), 'timeline_id': plan['timeline_id']}
    finally:
        if previous:
            proj.SetCurrentTimeline(previous)


def plan_replace(proj, state, p):
    timeline = _timeline(proj, p['timeline_id'])
    item = _item(timeline, p['item_id'])
    g = generation(state, p['record_id'])
    asset = verified_asset(g)
    require(asset['kind'] == 'video', 'unsupported_take', 'Replacement currently requires a video asset; still-image take extents are not assumed.')
    fps = Fraction(str(timeline.GetSetting('timelineFrameRate')))
    require(abs(asset['fps'] - float(fps)) < .01, 'fps_mismatch', 'Replacement video must match timeline frame rate.')
    for method in ('AddTake', 'SelectTakeByIndex', 'GetTakeByIndex', 'GetTakesCount', 'GetSelectedTakeIndex'):
        require(has_method(item, method), 'unavailable_on_this_build', f'{method} is unavailable; use a separately assembled timeline variant.')
    # Source offsets/retimes cannot be reconstructed reliably on 20.x; new take uses an explicit source start.
    count = int(item.GetDuration())
    source_start = p.get('source_start_frame', 0)
    require(type(source_start) is int and source_start >= 0, 'invalid_params', 'source_start_frame must be a non-negative integer.')
    available = math.floor(asset['duration_seconds'] * float(fps) + .01) - source_start
    mismatch = available - count
    key = uuid.uuid4().hex
    plan = {'id': key, 'type': 'replace', 'timeline_id': p['timeline_id'], 'item_id': p['item_id'],
            'record_id': p['record_id'], 'sha256': asset['sha256'], 'source_start_frame': source_start,
            'frames': count, 'available_frames': available, 'duration_mismatch_frames': mismatch,
            'can_execute': mismatch >= 0, 'snapshot': snapshot(timeline),
            'previous_clip_id': str(item.GetMediaPoolItem().GetUniqueId()),
            'previous_take_index': item.GetSelectedTakeIndex(), 'status': 'planned',
            'note': 'Only this video take changes; linked audio stays unchanged. Extra source frames are unused. Short replacements are refused. Previous take is retained.'}
    state['plans'][key] = plan
    return {'plan': plan}


def replace_shot(proj, state, p):
    plan = state['plans'][p['plan_id']]
    require(plan['type'] == 'replace' and plan['can_execute'], 'duration_mismatch', 'Replacement is too short, or this is not a replacement plan.')
    timeline = _timeline(proj, plan['timeline_id'])
    require(snapshot(timeline) == plan['snapshot'], 'stale_plan', 'Timeline structure changed; re-plan against the intended item.')
    item = _item(timeline, plan['item_id'])
    g = generation(state, plan['record_id'])
    asset = verified_asset(g)
    require(asset['sha256'] == plan['sha256'], 'stale_plan', 'Replacement asset changed.')
    current = item.GetMediaPoolItem()
    require(current is not None, 'unsupported_item', 'Target has no Media Pool source.')
    if plan['status'] == 'replaced':
        require(matches_asset(current, asset), 'stale_plan', 'Target changed after replacement.')
        return {'plan': plan, 'reused': True}
    require(str(current.GetUniqueId()) == plan['previous_clip_id'] or
            matches_asset(current, asset),
            'stale_plan', 'Target take changed; re-plan.')
    clip, _ = import_asset(proj, asset, g['scene_id'])
    start = plan['source_start_frame']
    end = start + plan['frames'] - 1
    selected = None
    original = None
    for i in range(1, (item.GetTakesCount() or 0) + 1):
        take = item.GetTakeByIndex(i) or {}
        media = take.get('mediaPoolItem')
        if media and str(media.GetUniqueId()) == plan['previous_clip_id']:
            original = i
        if media and str(media.GetUniqueId()) == str(clip.GetUniqueId()) and take.get('startFrame') == start and take.get('endFrame') == end:
            selected = i
    if selected is None:
        require(item.AddTake(clip, start, end), 'take_failed', 'Resolve refused AddTake; existing item remains in place.')
        selected = item.GetTakesCount()
    try:
        require(item.SelectTakeByIndex(selected), 'take_failed', 'Resolve refused take selection.')
        require(snapshot(timeline) == plan['snapshot'], 'take_readback_failed', 'Timeline positions/durations changed unexpectedly.')
        require(str(item.GetMediaPoolItem().GetUniqueId()) == str(clip.GetUniqueId()), 'take_readback_failed', 'Selected take did not read back as the replacement.')
    except Exception:
        rollback = plan['previous_take_index'] or original
        if not rollback:
            for i in range(1, item.GetTakesCount() + 1):
                take = item.GetTakeByIndex(i) or {}
                if take.get('mediaPoolItem') and str(take['mediaPoolItem'].GetUniqueId()) == plan['previous_clip_id']:
                    rollback = i
                    break
        if rollback:
            item.SelectTakeByIndex(rollback)
        raise
    plan.update(status='replaced', selected_take_index=selected, replacement_clip_id=str(clip.GetUniqueId()))
    return {'plan': plan, 'reused': False}
