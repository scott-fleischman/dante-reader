#!/usr/bin/env python3
"""
Generate ElevenLabs TTS audio for Dante's Inferno Canto I.
Produces canto1.mp3 and canto1_timing.json with word-level timestamps.

Usage: python3 generate_audio.py
Requires: ELEVENLABS_API_KEY in .env file, ffmpeg/ffprobe installed
"""

import json
import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Load API key from .env
env_path = Path(__file__).parent / '.env'
api_key = None
for line in env_path.read_text().strip().split('\n'):
    if line.startswith('ELEVENLABS_API_KEY='):
        api_key = line.split('=', 1)[1].strip()
        break

if not api_key:
    print("Error: ELEVENLABS_API_KEY not found in .env")
    sys.exit(1)

# ElevenLabs config
VOICE_ID = "pFZP5JQG7iQjIQuC4Bku"  # Lily - multilingual
MODEL_ID = "eleven_multilingual_v2"
API_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/with-timestamps"
STANZA_GAP = 0.4  # seconds of silence between stanzas


def extract_stanzas():
    html_path = Path(__file__).parent / 'index.html'
    html = html_path.read_text(encoding='utf-8')
    match = re.search(r'const STANZAS = (\[.*?\]);', html, re.DOTALL)
    if not match:
        print("Error: Could not find STANZAS in index.html")
        sys.exit(1)
    return json.loads(match.group(1))


def generate_tts(text):
    """Call ElevenLabs TTS API with timestamps."""
    import urllib.request

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }

    body = json.dumps({
        "text": text,
        "model_id": MODEL_ID,
        "voice_settings": {
            "stability": 0.6,
            "similarity_boost": 0.75,
            "style": 0.4,
        },
    }).encode('utf-8')

    req = urllib.request.Request(API_URL, data=body, headers=headers, method='POST')

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"API error {e.code}: {error_body}")
        return None


def get_mp3_duration(filepath):
    """Get actual MP3 duration using ffprobe."""
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(filepath)],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def chars_to_words(alignment):
    """Convert character-level alignment to word-level timing."""
    chars = alignment['characters']
    starts = alignment['character_start_times_seconds']
    ends = alignment['character_end_times_seconds']

    words = []
    current_word = ''
    word_start = None

    for i, ch in enumerate(chars):
        if ch in (' ', '\n', '\t'):
            if current_word:
                words.append({
                    'word': current_word,
                    'start': word_start,
                    'end': ends[i - 1] if i > 0 else starts[i],
                })
                current_word = ''
                word_start = None
        else:
            if word_start is None:
                word_start = starts[i]
            current_word += ch

    if current_word and word_start is not None:
        words.append({
            'word': current_word,
            'start': word_start,
            'end': ends[-1] if ends else word_start,
        })

    return words


def tokenize_italian(lines):
    """Extract words from Italian lines, matching the JS tokenizer."""
    words = []
    for line in lines:
        i = 0
        while i < len(line):
            ch = line[i]
            if ch in ' ,;:.!?\u00ab\u00bb\u201c\u201d':
                i += 1
                continue
            word = ''
            while i < len(line):
                ch = line[i]
                if ch in ',;:.!?\u00ab\u00bb\u201c\u201d':
                    break
                if ch == ' ':
                    break
                word += ch
                i += 1
            if word:
                words.append(word)
    return words


def main():
    stanzas = extract_stanzas()
    print(f"Found {len(stanzas)} stanzas")

    output_dir = Path(__file__).parent
    tmp_dir = output_dir / '_tmp_audio'
    tmp_dir.mkdir(exist_ok=True)

    # Phase 1: Generate individual stanza audio files
    stanza_results = []
    for si, stanza in enumerate(stanzas):
        italian_lines = stanza['italian']
        full_text = '\n'.join(italian_lines)

        chunk_path = tmp_dir / f'stanza_{si:02d}.mp3'

        # Skip if already generated (for resuming)
        if chunk_path.exists() and chunk_path.stat().st_size > 0:
            # Load cached alignment if available
            align_path = tmp_dir / f'stanza_{si:02d}_align.json'
            if align_path.exists():
                alignment = json.loads(align_path.read_text())
                word_timings = chars_to_words(alignment)
                stanza_results.append({
                    'path': chunk_path,
                    'word_timings': word_timings,
                    'tokenized': tokenize_italian(italian_lines),
                })
                print(f"  Stanza {stanza['stanza']}: (cached)")
                continue

        print(f"  Stanza {stanza['stanza']}: {italian_lines[0][:40]}...")

        result = generate_tts(full_text)
        if not result:
            print(f"  ERROR: Failed to generate stanza {stanza['stanza']}")
            stanza_results.append(None)
            continue

        # Save audio chunk
        audio_bytes = base64.b64decode(result['audio_base64'])
        chunk_path.write_bytes(audio_bytes)

        # Save alignment for caching
        alignment = result.get('normalized_alignment') or result.get('alignment')
        align_path = tmp_dir / f'stanza_{si:02d}_align.json'
        align_path.write_text(json.dumps(alignment))

        word_timings = chars_to_words(alignment)
        tokenized = tokenize_italian(italian_lines)

        stanza_results.append({
            'path': chunk_path,
            'word_timings': word_timings,
            'tokenized': tokenized,
        })

        time.sleep(0.15)

    # Phase 2: Measure actual durations with ffprobe
    print("\nMeasuring audio durations...")
    actual_durations = []
    for si, sr in enumerate(stanza_results):
        if sr is None:
            actual_durations.append(0)
            continue
        dur = get_mp3_duration(sr['path'])
        actual_durations.append(dur)
        print(f"  Stanza {si + 1}: {dur:.2f}s")

    # Phase 3: Concatenate with ffmpeg (proper gapless join)
    print("\nConcatenating audio with ffmpeg...")
    concat_list_path = tmp_dir / 'concat.txt'
    silence_path = tmp_dir / 'silence.mp3'

    # Generate a short silence file for gaps between stanzas
    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi', '-i',
        f'anullsrc=r=44100:cl=mono:d={STANZA_GAP}',
        '-c:a', 'libmp3lame', '-q:a', '2',
        str(silence_path)
    ], capture_output=True)

    # Build concat list
    entries = []
    for si, sr in enumerate(stanza_results):
        if sr is None:
            continue
        entries.append(f"file '{sr['path'].name}'")
        if si < len(stanza_results) - 1:
            entries.append(f"file 'silence.mp3'")

    concat_list_path.write_text('\n'.join(entries))

    mp3_path = output_dir / 'canto1.mp3'
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', str(concat_list_path),
        '-c:a', 'libmp3lame', '-q:a', '2',
        str(mp3_path)
    ], capture_output=True)

    # Phase 4: Build timing data using actual durations
    print("Building timing data...")
    timing_data = {
        'stanzas': {},
        'words': [],
    }

    time_offset = 0.0
    for si, sr in enumerate(stanza_results):
        if sr is None:
            continue

        word_timings = sr['word_timings']
        tokenized = sr['tokenized']

        # Scale word timings to fit actual duration
        char_duration = word_timings[-1]['end'] if word_timings else 0
        actual_dur = actual_durations[si]
        scale = actual_dur / char_duration if char_duration > 0 else 1.0

        # Record stanza start time
        if word_timings:
            timing_data['stanzas'][si] = {
                'startTime': round(time_offset + word_timings[0]['start'] * scale, 4),
            }

        # Map word timings
        for wi, wt in enumerate(word_timings):
            word_idx = wi if wi < len(tokenized) else len(tokenized) - 1
            timing_data['words'].append({
                'stanza': si,
                'wordIdx': word_idx,
                'word': wt['word'],
                'start': round(time_offset + wt['start'] * scale, 4),
                'end': round(time_offset + wt['end'] * scale, 4),
            })

        time_offset += actual_dur + STANZA_GAP

    json_path = output_dir / 'canto1_timing.json'
    json_path.write_text(json.dumps(timing_data, indent=2, ensure_ascii=False))

    # Cleanup temp files
    print("Cleaning up temp files...")
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    total_words = len(timing_data['words'])
    print(f"\nDone! {total_words} words, ~{time_offset:.1f}s total duration")
    print(f"Files: {mp3_path.name}, {json_path.name}")
    final_dur = get_mp3_duration(mp3_path)
    print(f"Final MP3 duration: {final_dur:.1f}s")


if __name__ == '__main__':
    main()
