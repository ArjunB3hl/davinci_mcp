"""Source-safe visual QC, followed by explicit host vision evidence and review."""
from __future__ import annotations

from pathlib import Path
import re
import uuid

from src.utils.generated_assets import require, run_media
from src.utils.generated_media import generation, verified_asset

CATEGORIES = ('character_change', 'unwanted_text', 'blank_frame', 'abrupt_transition', 'other')


def prepare(state, p, root):
    record_ids = p['record_ids']
    require(isinstance(record_ids, list) and 0 < len(record_ids) <= 50, 'invalid_params', 'Review 1..50 ordered visual records at a time.')
    key = uuid.uuid4().hex
    folder = Path(root) / 'reviews' / key
    folder.mkdir(parents=True)
    assets, evidence, flags = {}, [], []
    for record_id in record_ids:
        g = generation(state, record_id)
        asset = verified_asset(g)
        require(asset['kind'] in ('video', 'image'), 'wrong_media_type', 'Visual review requires images/video.')
        assets[record_id] = asset['sha256']
        duration = asset['duration_seconds']
        positions = [0.0] if asset['kind'] == 'image' else [0.0, duration / 2, max(0, duration - 1 / max(asset['fps'], 1))]
        for index, seconds in enumerate(positions):
            frame = folder / f'{record_id}-{index}.png'
            run_media(['ffmpeg', '-v', 'error', '-ss', str(seconds), '-protocol_whitelist', 'file,pipe', '-i', asset['local_path'],
                       '-frames:v', '1', '-vf', 'scale=960:-2', '-y', str(frame)])
            require(frame.is_file() and frame.stat().st_size > 0, 'frame_failed', 'Could not extract review frame.')
            evidence.append({'id': f'{record_id}:{index}', 'record_id': record_id, 'scene_id': g['scene_id'],
                             'seconds': seconds, 'path': str(frame), 'reference_images': [a['local_path'] for a in g.get('reference_assets', [])] or g['reference_images']})
        if asset['kind'] == 'video':
            result = run_media(['ffmpeg', '-hide_banner', '-nostats', '-protocol_whitelist', 'file,pipe', '-i', asset['local_path'],
                                '-vf', "blackdetect=d=0.05:pix_th=0.10,select='gt(scene,0.45)',showinfo",
                                '-an', '-f', 'null', '-'])
            log = result.stderr.decode(errors='replace')
            for start, end in re.findall(r'black_start:([\d.]+) black_end:([\d.]+)', log):
                flags.append({'record_id': record_id, 'category': 'blank_frame', 'start_seconds': float(start),
                              'end_seconds': float(end), 'confidence': .6,
                              'reason': 'Near-black interval; may be intentional. Review manually.'})
            for seconds in re.findall(r'pts_time:([\d.]+)', log):
                flags.append({'record_id': record_id, 'category': 'abrupt_transition', 'start_seconds': float(seconds),
                              'confidence': .5, 'reason': 'Large within-shot frame change; may be intentional motion/cut.'})
    result = {'id': key, 'status': 'pending_host_vision', 'assets': assets, 'ordered_record_ids': record_ids,
              'frames': evidence, 'automated_flags': flags,
              'vision_instructions': 'Open every frame and available reference image. Compare character appearance and unwanted text; compare each shot end to next shot start. Return findings with category, frame_id, confidence (0..1), and reason. Do not infer identity. Intentional cuts/darkness are not automatically defects.',
              'categories': list(CATEGORIES),
              'limitations': 'Sparse visual samples can miss defects. Automated checks cover black intervals and large changes within clips. Character consistency, text and between-shot transitions require host review; no perfect detection is claimed.'}
    state['reviews'][key] = result
    return {'review': result}


def commit(state, p):
    report = state['reviews'][p['review_id']]
    require(report['status'] == 'pending_host_vision', 'already_reviewed', 'This review was already committed; prepare a new review for changes.')
    for key, expected in report['assets'].items():
        require(verified_asset(generation(state, key))['sha256'] == expected, 'stale_review', 'Media changed since review preparation.')
    known = {f['id'] for f in report['frames']}
    reviewed = p['reviewed_frame_ids']
    require(isinstance(reviewed, list) and set(reviewed) == known, 'incomplete_review', 'Inspect and acknowledge every returned frame before completing vision review.')
    findings = p['findings']
    require(isinstance(findings, list), 'invalid_findings', 'findings must be a list, empty if no issues were observed.')
    clean = []
    for finding in findings:
        require(finding.get('category') in CATEGORIES and finding.get('frame_id') in known,
                'invalid_findings', 'Finding must name a valid category and evidence frame_id.')
        confidence = finding.get('confidence')
        require(type(confidence) in (int, float) and 0 <= confidence <= 1,
                'invalid_findings', 'Confidence must be 0..1.')
        require(isinstance(finding.get('reason'), str) and finding['reason'].strip(), 'invalid_findings', 'Finding needs a concrete observation.')
        clean.append({k: finding[k] for k in ('category', 'frame_id', 'confidence', 'reason')})
    report.update(status='ready_for_user_review', findings=clean,
                  reviewed_frame_ids=sorted(known), reviewer=p.get('reviewer', 'host vision'))
    return {'review': report}
