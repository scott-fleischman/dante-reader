#!/usr/bin/env node
//
// Generate a voice audio file + word-level timing data using the ElevenLabs API.
//
// Usage:
//   node generate_voice.js --voice-id <VOICE_ID> --speed <SPEED> --output <PREFIX>
//
// Example:
//   node generate_voice.js --voice-id pNInz6obpgDQGcFmaJgB --speed 0.7 --output canto1_male_slow
//
// Produces:
//   <PREFIX>.mp3         — the audio file
//   <PREFIX>_timing.json — word-level timing data matching the app's expected format
//
// Requires ELEVENLABS_API_KEY in .env
//
// To list available voices:
//   node generate_voice.js --list-voices

const fs = require('fs');
const path = require('path');

// Load .env
const envPath = path.join(__dirname, '.env');
const envContent = fs.readFileSync(envPath, 'utf8');
const apiKey = envContent.match(/ELEVENLABS_API_KEY=(.+)/)?.[1]?.trim();
if (!apiKey) {
  console.error('Missing ELEVENLABS_API_KEY in .env');
  process.exit(1);
}

// Parse args
const args = process.argv.slice(2);
function getArg(name) {
  const idx = args.indexOf(name);
  return idx >= 0 && idx + 1 < args.length ? args[idx + 1] : null;
}

if (args.includes('--list-voices')) {
  listVoices().then(() => process.exit(0));
} else {
  const voiceId = getArg('--voice-id');
  const speed = parseFloat(getArg('--speed') || '1.0');
  const output = getArg('--output');

  if (!voiceId || !output) {
    console.error('Usage: node generate_voice.js --voice-id <ID> --speed <SPEED> --output <PREFIX>');
    console.error('       node generate_voice.js --list-voices');
    process.exit(1);
  }

  generateVoice(voiceId, speed, output);
}

async function listVoices() {
  const res = await fetch('https://api.elevenlabs.io/v1/voices', {
    headers: { 'xi-api-key': apiKey }
  });
  const data = await res.json();
  if (!data.voices) {
    console.error('Unexpected API response:', JSON.stringify(data, null, 2));
    process.exit(1);
  }
  console.log('Available voices:\n');
  for (const v of data.voices) {
    const labels = v.labels || {};
    const desc = [labels.accent, labels.age, labels.gender, labels.use_case]
      .filter(Boolean).join(', ');
    console.log(`  ${v.voice_id}  ${v.name}  (${desc})`);
  }
}

async function generateVoice(voiceId, speed, outputPrefix) {
  // Extract Italian text from index.html, grouped by stanza
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const stanzaMatch = html.match(/const STANZAS = (\[[\s\S]*?\]);/);
  if (!stanzaMatch) {
    console.error('Could not extract STANZAS from index.html');
    process.exit(1);
  }
  const stanzas = eval(stanzaMatch[1]);

  // Build full Italian text with stanza markers
  // We join with double newlines between stanzas so ElevenLabs adds natural pauses
  const fullText = stanzas.map(s => s.italian.join('\n')).join('\n\n');

  console.log(`Generating audio with voice ${voiceId} at speed ${speed}...`);
  console.log(`Text length: ${fullText.length} characters, ${stanzas.length} stanzas`);

  // Call ElevenLabs with-timestamps endpoint
  const url = `https://api.elevenlabs.io/v1/text-to-speech/${voiceId}/with-timestamps`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'xi-api-key': apiKey,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      text: fullText,
      model_id: 'eleven_multilingual_v2',
      voice_settings: {
        stability: 0.5,
        similarity_boost: 0.75,
        speed: speed
      }
    })
  });

  if (!res.ok) {
    const err = await res.text();
    console.error(`ElevenLabs API error (${res.status}):`, err);
    process.exit(1);
  }

  const result = await res.json();

  // Save audio
  const audioBuffer = Buffer.from(result.audio_base64, 'base64');
  const mp3Path = path.join(__dirname, `${outputPrefix}.mp3`);
  fs.writeFileSync(mp3Path, audioBuffer);
  console.log(`Saved audio: ${mp3Path} (${(audioBuffer.length / 1024).toFixed(0)} KB)`);

  // Process character-level alignment into word-level timing
  const alignment = result.alignment;
  const chars = alignment.characters;
  const starts = alignment.character_start_times_seconds;
  const ends = alignment.character_end_times_seconds;

  // Build the same tokenization the app uses, matching characters to timing
  const timingData = buildTimingData(stanzas, fullText, chars, starts, ends);

  const timingPath = path.join(__dirname, `${outputPrefix}_timing.json`);
  fs.writeFileSync(timingPath, JSON.stringify(timingData, null, 2));
  console.log(`Saved timing: ${timingPath} (${timingData.words.length} words, ${Object.keys(timingData.stanzas).length} stanzas)`);
}

// Match character-level ElevenLabs alignment to the app's word tokenization
function buildTimingData(stanzas, fullText, chars, starts, ends) {
  const words = [];
  const stanzaTimings = {};

  // Build a map: for each character position in fullText, find its timing
  // ElevenLabs returns one timing entry per character in the input text
  // We walk through the text character by character, matching to our stanza/word structure

  let charIdx = 0; // index into the ElevenLabs chars/starts/ends arrays

  for (let si = 0; si < stanzas.length; si++) {
    const s = stanzas[si];
    let stanzaStartTime = null;
    let wordIdx = 0;

    for (let li = 0; li < s.italian.length; li++) {
      const line = s.italian[li];
      const tokens = tokenizeLine(line);

      for (const token of tokens) {
        if (token.isWord) {
          // Find this word's characters in the alignment
          const wordStart = findCharStart(charIdx, token.text, chars, starts, ends);
          const wordEnd = findCharEnd(charIdx, token.text, chars, starts, ends);

          // Advance charIdx past this word's characters
          charIdx = advancePast(charIdx, token.text, chars);

          if (stanzaStartTime === null && wordStart !== null) {
            stanzaStartTime = wordStart;
          }

          words.push({
            stanza: si,
            wordIdx: wordIdx,
            word: token.text,
            start: wordStart || 0,
            end: wordEnd || 0
          });
          wordIdx++;
        } else {
          // Punctuation/whitespace — advance charIdx past it
          charIdx = advancePast(charIdx, token.text, chars);
        }
      }

      // Advance past the newline between lines
      if (charIdx < chars.length && chars[charIdx] === '\n') {
        charIdx++;
      }
    }

    // Advance past inter-stanza newlines
    while (charIdx < chars.length && chars[charIdx] === '\n') {
      charIdx++;
    }

    stanzaTimings[si] = { startTime: stanzaStartTime || 0 };
  }

  return { stanzas: stanzaTimings, words };
}

function findCharStart(startIdx, text, chars, starts, ends) {
  // Find the start time of the first character of this text
  for (let i = startIdx; i < chars.length && i < startIdx + text.length + 5; i++) {
    if (chars[i] === text[0] && starts[i] > 0) {
      return starts[i];
    }
    if (starts[i] > 0) return starts[i]; // first non-zero time nearby
  }
  return null;
}

function findCharEnd(startIdx, text, chars, starts, ends) {
  // Find the end time of the last character of this text
  let lastEnd = null;
  for (let i = startIdx; i < chars.length && i < startIdx + text.length + 5; i++) {
    if (ends[i] > 0) lastEnd = ends[i];
    // Stop after we've gone through enough characters
    if (i >= startIdx + text.length) break;
  }
  return lastEnd;
}

function advancePast(startIdx, text, chars) {
  // Advance the character index past this text
  let idx = startIdx;
  let ti = 0;
  while (idx < chars.length && ti < text.length) {
    if (chars[idx] === text[ti]) {
      idx++;
      ti++;
    } else {
      // Character mismatch — try advancing (ElevenLabs may normalize chars)
      idx++;
    }
  }
  return idx;
}

// Same tokenizer as in index.html
function tokenizeLine(line) {
  const tokens = [];
  let i = 0;
  while (i < line.length) {
    if (/[\s,;:.!?\u00ab\u00bb\u2018\u2019\u201c\u201d]/.test(line[i]) && !/['\u2019]/.test(line[i])) {
      let punct = '';
      while (i < line.length && /[\s,;:.!?\u00ab\u00bb\u2018\u2019\u201c\u201d]/.test(line[i]) && !/['\u2019]/.test(line[i])) {
        punct += line[i]; i++;
      }
      tokens.push({ text: punct, isWord: false });
    } else {
      let word = '';
      while (i < line.length && !/[\s,;:.!?\u00ab\u00bb\u2018\u2019\u201c\u201d]/.test(line[i]) || (i < line.length && /['\u2019]/.test(line[i]))) {
        if (/[,;:.!?]/.test(line[i])) break;
        word += line[i]; i++;
      }
      if (word) tokens.push({ text: word, isWord: true });
    }
  }
  return tokens;
}
