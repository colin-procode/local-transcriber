const $ = (id) => document.getElementById(id);
const previewPlaceholder = $("previewPlaceholder");
const state = {
  recording: false,
  stopping: false,
  startedAt: null,
  sources: {},
  audioContext: null,
  displayStream: null,
  words: [],
  utterances: [],
  speakerNames: {},
  activeTranscriptId: null,
  activeSavedTranscript: null,
  pollTimer: null,
  statusRefreshPending: false,
  audioBuffers: {},
  runtimeReady: false,
  fileMediaElement: null,
  fileObjectUrl: null,
  fileProgressTimer: null,
  sessionTitle: "",
};

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function showError(message) {
  const box = $("errorBanner");
  box.textContent = message;
  box.classList.remove("hidden");
}

function clearError() {
  $("errorBanner").classList.add("hidden");
  $("errorBanner").textContent = "";
}

function updateRuntime(status) {
  state.runtimeReady = status.runtime === "ready";
  const pill = $("runtimePill");
  pill.classList.remove("ready", "error", "missing");
  const runtimeClass = status.runtime === "ready" ? "ready" : status.runtime === "error" || status.runtime === "missing" ? status.runtime : null;
  if (runtimeClass) pill.classList.add(runtimeClass);
  $("runtimeText").textContent = status.runtime === "ready"
    ? "Qwen3-ASR ready"
    : status.runtime === "loading"
      ? "Loading local models…"
      : status.runtime === "missing"
        ? "Runtime setup needed"
        : status.runtime === "error"
          ? "Model startup failed"
          : "Waiting for local models…";
  if (!state.recording && !state.stopping) {
    $("recordButton").disabled = status.runtime !== "ready";
    $("recordButtonText").textContent = status.runtime === "ready" ? "Start recording" : status.runtime === "missing" ? "Setup required" : "Loading models";
    $("recordHeading").textContent = status.runtime === "ready" ? "Capture a conversation" : status.runtime === "missing" ? "Set up the local runtime" : "Preparing local models";
    $("recordDescription").textContent = status.runtime === "ready"
      ? "Qwen3-ASR transcribes live; Nemotron adds speaker labels when you stop."
      : status.message || "The local ASR and diarization models are loading.";
  }
  updateFileButton();
}

function updateFileButton() {
  const input = $("mediaFileInput");
  const button = $("transcribeFileButton");
  if (!input || !button) return;
  button.textContent = "Transcribe file";
  button.title = input.files?.[0]?.name || "";
  button.disabled = !state.runtimeReady || state.recording || state.stopping || !input.files?.length;
}

async function refreshStatus() {
  if (state.statusRefreshPending) return;
  state.statusRefreshPending = true;
  try {
    const status = await request("/api/status");
    updateRuntime(status.model || {});
    $("folderPath").textContent = status.transcripts_dir;
  } catch (error) {
    updateRuntime({ runtime: "error", message: error.message });
  } finally {
    state.statusRefreshPending = false;
  }
}

function prettyDate(value) {
  if (!value) return "Unknown date";
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function filenameWithoutExtension(filename) {
  const baseName = String(filename || "").split(/[\\/]/).pop() || "";
  const extensionDot = baseName.lastIndexOf(".");
  return extensionDot > 0 ? baseName.slice(0, extensionDot) : baseName;
}

async function refreshTranscriptList() {
  try {
    const rows = await request("/api/transcripts");
    const list = $("transcriptList");
    list.replaceChildren();
    const filter = $("searchInput").value.trim().toLocaleLowerCase();
    const visible = rows.filter((row) => `${row.title} ${row.note || ""}`.toLocaleLowerCase().includes(filter));
    $("emptyLibrary").style.display = visible.length ? "none" : "block";
    $("emptyLibrary").textContent = rows.length ? "No matching transcripts." : "Your saved transcripts will appear here.";
    for (const row of visible) {
      const button = document.createElement("button");
      button.className = `transcript-item${state.activeTranscriptId === row.id ? " selected" : ""}`;
      const title = document.createElement("strong");
      title.textContent = row.title || "Untitled transcript";
      const date = document.createElement("span");
      date.textContent = prettyDate(row.start_time);
      button.append(title, date);
      if (row.note) {
        const note = document.createElement("span");
        note.className = "item-note";
        note.textContent = row.note;
        button.append(note);
      }
      button.addEventListener("click", () => openTranscript(row.id));
      list.append(button);
    }
  } catch (error) {
    showError(`Could not read transcript library: ${error.message}`);
  }
}

function displaySpeaker(speakerId, names = state.speakerNames) {
  if (!speakerId) return "Unknown speaker";
  return names[speakerId] || speakerId.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function renderUtterance(utterance, names = state.speakerNames) {
  const row = document.createElement("div");
  row.className = "utterance";
  const label = document.createElement("span");
  label.className = "speaker-label";
  label.textContent = displaySpeaker(utterance.speaker, names);
  const textWrap = document.createElement("div");
  const text = document.createElement("span");
  text.className = "utterance-text";
  text.textContent = utterance.text || "";
  const meta = document.createElement("span");
  meta.className = "utterance-meta";
  const start = Number.isFinite(Number(utterance.start)) ? `${Number(utterance.start).toFixed(1)}s` : "";
  const end = Number.isFinite(Number(utterance.end)) ? `${Number(utterance.end).toFixed(1)}s` : "";
  meta.textContent = `${utterance.source === "mic" ? "Microphone" : "Computer audio"}${start ? ` · ${start}${end ? `–${end}` : ""}` : ""}`;
  textWrap.append(text, meta);
  row.append(label, textWrap);
  return row;
}

function showLivePlaceholder(show) {
  previewPlaceholder.classList.toggle("hidden", !show);
}

function renderLiveTranscript() {
  const panel = $("liveTranscript");
  panel.replaceChildren();
  if (!state.utterances.length) {
    panel.append(previewPlaceholder);
    showLivePlaceholder(true);
    return;
  }
  showLivePlaceholder(false);
  const utterances = [...state.utterances].sort((a, b) => (a.start || 0) - (b.start || 0));
  utterances.forEach((utterance) => panel.append(renderUtterance(utterance)));
  panel.scrollTop = panel.scrollHeight;
}

function renderSavedTranscript(data) {
  const panel = $("liveTranscript");
  panel.replaceChildren();
  const utterances = data.utterances || [];
  const wordSpeakers = new Set((data.words || []).map((word) => word.speaker).filter(Boolean));
  const granularWords = (data.words || []).length > 1 && (wordSpeakers.size > 1 || !utterances.length);
  if (granularWords) {
    // Some realtime ASR events span an entire speech turn and retain only the
    // last speaker ID. Word-level diarization is more precise when available.
    const runs = [];
    for (const word of data.words) {
      const previous = runs[runs.length - 1];
      if (!previous || previous.speaker !== word.speaker || previous.source !== word.source) {
        runs.push({ speaker: word.speaker, source: word.source, start: word.start, end: word.end, text: String(word.word || "") });
      } else {
        previous.end = word.end;
        previous.text += `${previous.text ? " " : ""}${word.word || ""}`;
      }
    }
    runs.forEach((utterance) => panel.append(renderUtterance(utterance, data.speaker_names || {})));
  } else if (utterances.length) {
    [...utterances].sort((a, b) => (a.start || 0) - (b.start || 0)).forEach((utterance) => panel.append(renderUtterance(utterance, data.speaker_names || {})));
  } else if (data.words?.length) {
    const groups = new Map();
    for (const word of data.words) {
      const key = `${word.speaker}|${word.source}`;
      if (!groups.has(key)) groups.set(key, { speaker: word.speaker, source: word.source, start: word.start, end: word.end, text: "" });
      const group = groups.get(key);
      group.text += `${group.text ? " " : ""}${word.word}`;
      group.end = word.end;
    }
    [...groups.values()].forEach((utterance) => panel.append(renderUtterance(utterance, data.speaker_names || {})));
  } else {
    const placeholder = document.createElement("div");
    placeholder.className = "preview-placeholder";
    placeholder.innerHTML = '<div class="placeholder-icon">≋</div><h3>No words were recognized</h3><p>This transcript was saved, but the models returned no speech.</p>';
    panel.append(placeholder);
  }
  $("sessionTime").textContent = `${prettyDate(data.start_time)} — ${prettyDate(data.end_time)}`;
}

function detectedSpeakers(data) {
  const ids = new Set();
  for (const utterance of data.utterances || []) ids.add(utterance.speaker);
  for (const word of data.words || []) ids.add(word.speaker);
  for (const id of Object.keys(data.speaker_names || {})) ids.add(id);
  return [...ids].filter(Boolean).sort((a, b) => a === "YOU" ? -1 : b === "YOU" ? 1 : a.localeCompare(b, undefined, { numeric: true }));
}

function renderSpeakerFields(data) {
  const container = $("speakerNames");
  container.replaceChildren();
  for (const id of detectedSpeakers(data)) {
    const row = document.createElement("label");
    row.className = "speaker-name-row";
    const chip = document.createElement("span");
    chip.className = "speaker-chip";
    chip.textContent = id === "YOU" ? "MIC" : id.replace("MIC_SPEAKER_", "MIC SPK ").replace("COMPUTER_SPEAKER_", "COMPUTER SPK ").replace("SPEAKER_", "SPK ");
    const input = document.createElement("input");
    input.className = "speaker-name-input";
    input.dataset.speaker = id;
    input.value = data.speaker_names?.[id] || (id === "YOU" ? "You" : displaySpeaker(id, {}));
    input.setAttribute("aria-label", `Name for ${displaySpeaker(id, {})}`);
    row.append(chip, input);
    container.append(row);
  }
}

function syncTranscriptActions() {
  const hasSavedTranscript = Boolean(state.activeTranscriptId);
  $("transcriptActions").classList.toggle("hidden", !hasSavedTranscript);
  $("downloadTranscriptButton").href = hasSavedTranscript
    ? `/api/transcripts/${encodeURIComponent(state.activeTranscriptId)}/download`
    : "#";
}

async function openTranscript(id) {
  try {
    const data = await request(`/api/transcripts/${encodeURIComponent(id)}`);
    state.activeTranscriptId = id;
    syncTranscriptActions();
    state.activeSavedTranscript = data;
    state.speakerNames = data.speaker_names || {};
    $("detailEyebrow").textContent = "SAVED TRANSCRIPT";
    $("transcriptHeading").textContent = data.title || "Untitled transcript";
    $("titleInput").value = data.title || "";
    $("noteInput").value = data.note || "";
    $("detailsPanel").classList.remove("hidden");
    $("transcriptPanel").classList.remove("hidden");
    $("sessionTime").textContent = `${prettyDate(data.start_time)} — ${prettyDate(data.end_time)}`;
    renderSavedTranscript(data);
    renderSpeakerFields(data);
    await refreshTranscriptList();
  } catch (error) {
    showError(`Could not open transcript: ${error.message}`);
  }
}

function downsampleAndEncode(samples, inputRate, outputRate = 16000) {
  if (outputRate > inputRate) throw new Error("Browser audio rate is below 16 kHz");
  const ratio = inputRate / outputRate;
  const outputLength = Math.floor(samples.length / ratio);
  const pcm = new Int16Array(outputLength);
  for (let i = 0; i < outputLength; i++) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, samples.length - 1);
    const fraction = position - left;
    const sample = samples[left] * (1 - fraction) + samples[right] * fraction;
    const clamped = Math.max(-1, Math.min(1, sample));
    pcm[i] = clamped < 0 ? clamped * 32768 : clamped * 32767;
  }
  return pcm.buffer;
}

function wireAudioStream(stream, source) {
  const audioTracks = stream.getAudioTracks();
  if (!audioTracks.length) return false;
  const sourceNode = state.audioContext.createMediaStreamSource(new MediaStream(audioTracks));
  const processor = state.audioContext.createScriptProcessor(4096, 1, 1);
  const mute = state.audioContext.createGain();
  mute.gain.value = 0;
  state.audioBuffers[source] = [];
  processor.onaudioprocess = (event) => {
    if (!state.recording) return;
    const samples = event.inputBuffer.getChannelData(0);
    const pcm = downsampleAndEncode(samples, state.audioContext.sampleRate);
    if (pcm.byteLength) state.audioBuffers[source].push(pcm.slice(0));
  };
  sourceNode.connect(processor);
  processor.connect(mute);
  mute.connect(state.audioContext.destination);
  state.sources[source] = { stream, sourceNode, processor, mute };
  return true;
}

async function startRecording() {
  if (state.recording || state.stopping) return;
  clearError();
  let displayStream;
  let microphoneStream;
  let displayCaptureIssue = "";
  let captureStage = "computer audio sharing";
  try {
    state.fileMediaElement = null;
    state.sessionTitle = "";
    if (!navigator.mediaDevices?.getDisplayMedia) {
      displayCaptureIssue = "This browser doesn't support shared computer audio. Recording will use your microphone only. Try Chrome or Edge on localhost to capture a browser tab's audio.";
    } else {
      try {
        displayStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      } catch (error) {
        if (error.name !== "NotSupportedError" && error.name !== "TypeError" && error.message !== "Not supported") throw error;
        displayCaptureIssue = "This browser or system can't share computer audio. Recording will use your microphone only. Try Chrome or Edge on localhost, then share the tab with its audio enabled.";
      }
    }
    state.displayStream = displayStream || null;
    if (displayStream && !displayStream.getAudioTracks().length && !displayCaptureIssue) {
      displayCaptureIssue = "The browser shared no computer-audio track. In Chrome or Edge, choose the YouTube tab in the share dialog and turn on Share tab audio. Sharing an entire screen or window may provide video without sound.";
    }
    captureStage = "microphone access";
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("Microphone capture isn't available. Open this app in a supported browser using localhost.");
    }
    microphoneStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    captureStage = "audio setup";
    state.audioContext = new AudioContext();
    await state.audioContext.resume();
    state.startedAt = new Date();
    state.words = [];
    state.utterances = [];
    state.speakerNames = {};
    state.audioBuffers = {};
    $("detailEyebrow").textContent = "LIVE SESSION";
    $("transcriptHeading").textContent = "Transcript preview";
    $("sessionTime").textContent = `Started ${prettyDate(state.startedAt.toISOString())}`;
    $("detailsPanel").classList.add("hidden");
    $("transcriptPanel").classList.remove("hidden");
    $("partialLine").classList.add("hidden");
    renderLiveTranscript();

    const hasSystemAudio = displayStream ? wireAudioStream(displayStream, "system") : false;
    wireAudioStream(microphoneStream, "mic");
    state.recording = true;
    state.activeTranscriptId = null;
    syncTranscriptActions();
    $("recordButton").disabled = false;
    $("recordButton").classList.add("recording");
    $("recordButtonText").textContent = "Stop recording";
    $("recordButton").querySelector(".button-symbol").textContent = "■";
    $("recordStatus").textContent = "RECORDING LOCALLY";
    $("recordHeading").textContent = "Listening to your conversation";
    $("recordDescription").textContent = hasSystemAudio
      ? "Audio is being recorded locally. Transcription and speaker labeling start after you stop."
      : displayCaptureIssue
        ? "Your microphone is being recorded locally. Transcription starts after you stop."
        : "Your microphone is being recorded locally. Share a browser tab with its audio to capture computer sound too.";
    $("liveDot").classList.add("recording");
    $("captureHint").classList.toggle("hidden", hasSystemAudio);
    if (displayCaptureIssue) $("captureHint").textContent = displayCaptureIssue;
    displayStream?.getVideoTracks().forEach((track) => {
      track.onended = () => { if (state.recording) stopRecording(); };
    });
    for (const source of ["mic", "system"]) {
      state.sources[source]?.stream.getAudioTracks().forEach((track) => {
        track.onended = () => { if (state.recording && source === "mic") showError("Microphone capture stopped."); };
      });
    }
  } catch (error) {
    displayStream?.getTracks().forEach((track) => track.stop());
    microphoneStream?.getTracks().forEach((track) => track.stop());
    state.displayStream = null;
    if (state.audioContext) await state.audioContext.close().catch(() => {});
    state.audioContext = null;
    if (error.name === "NotAllowedError") {
      showError(`${captureStage} was denied. Allow the requested access and try again.`);
    } else if (error.name === "NotSupportedError" || error.message === "Not supported") {
      showError(`${captureStage} isn't supported by this browser or device. Try a current version of Chrome or Edge, opened at http://127.0.0.1:8000.`);
    } else {
      showError(error.message || "Could not start recording.");
    }
  }
}

async function startFileTranscription() {
  if (state.recording || state.stopping) return;
  clearError();
  const file = $("mediaFileInput").files?.[0];
  if (!file) return;
  if (file.size > 5_000_000_000) {
    showError("This file is larger than the local app's 5 GB upload limit. Choose a smaller file.");
    return;
  }
  state.recording = true;
  state.activeTranscriptId = null;
  syncTranscriptActions();
  $("fileProgress").classList.remove("hidden");
  updateFileProgress({ stage: "uploading", percent: 0, detail: "Uploading file to the local app…" });
  state.startedAt = new Date();
  const fileTitle = filenameWithoutExtension(file.name);
  state.sessionTitle = fileTitle;
  state.words = [];
  state.utterances = [];
  state.speakerNames = {};
  $("recordButton").disabled = true;
  $("transcribeFileButton").disabled = true;
  $("recordStatus").textContent = "PROCESSING FILE LOCALLY";
  $("recordHeading").textContent = "Transcribing your media file";
  $("recordDescription").textContent = "The file is being decoded locally and sent to Qwen as quickly as the model can process it. It does not play in the browser.";
  $("detailEyebrow").textContent = "MEDIA FILE";
  $("transcriptHeading").textContent = fileTitle;
  $("sessionTime").textContent = `Started ${prettyDate(state.startedAt.toISOString())}`;
  $("detailsPanel").classList.add("hidden");
  $("transcriptPanel").classList.remove("hidden");
  $("partialLine").classList.add("hidden");
  renderLiveTranscript();
  try {
    const progressPoll = async () => {
      try {
        const progress = await request("/api/file-progress");
        if (state.recording && progress.stage !== "idle") updateFileProgress(progress);
      } catch (_) { /* The upload request will surface any actual failure. */ }
    };
    state.fileProgressTimer = setInterval(progressPoll, 700);
    const result = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/transcribe-file?filename=" + encodeURIComponent(file.name));
      xhr.timeout = 2 * 60 * 60 * 1000;
      xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) updateFileProgress({ stage: "uploading", percent: Math.round(20 * event.loaded / event.total), detail: `Uploading file · ${Math.round(100 * event.loaded / event.total)}%` });
      };
      xhr.onerror = async () => {
        let appReachable = false;
        try { await request("/api/status"); appReachable = true; } catch (_) {}
        reject(new Error(appReachable
          ? "The app is running but closed the upload connection. Check transcripts/.app-errors.log for the server error, then retry."
          : "Can't reach the local transcription app. Make sure it is running at this address, then retry."));
      };
      xhr.ontimeout = () => reject(new Error("The local transcription request timed out."));
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* Use the HTTP status error below. */ }
        if (xhr.status < 200 || xhr.status >= 300) reject(new Error(body.error || `File transcription failed (${xhr.status})`));
        else resolve(body);
      };
      xhr.send(file);
    });
    state.utterances = result.utterances || [];
    state.speakerNames = result.speaker_names || {};
    renderLiveTranscript();
    if (result.warning) showError(`Transcript saved without speaker diarization: ${result.warning}`);

    const end = new Date();
    const dateTitle = state.startedAt.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    const saved = await request("/api/transcripts", {
      method: "POST",
      body: JSON.stringify({
        start_time: state.startedAt.toISOString(),
        end_time: end.toISOString(),
        title: `${fileTitle} · ${dateTitle}`,
        note: "",
        speaker_names: state.speakerNames,
        words: [],
        utterances: state.utterances,
      }),
    });
    state.activeTranscriptId = saved.id;
    syncTranscriptActions();
    state.activeSavedTranscript = saved.transcript;
    $("detailsPanel").classList.remove("hidden");
    $("titleInput").value = saved.transcript.title;
    $("noteInput").value = "";
    renderSpeakerFields(saved.transcript);
    await refreshTranscriptList();
    $("recordStatus").textContent = "TRANSCRIPT SAVED LOCALLY";
    updateFileProgress({ stage: "complete", percent: 100, detail: "Transcription complete · saved locally" });
    $("recordHeading").textContent = "Your transcript is saved";
    $("recordDescription").textContent = "Add a title, note, or speaker names below, or start another recording.";
    $("sessionTime").textContent = `${prettyDate(saved.transcript.start_time)} — ${prettyDate(saved.transcript.end_time)}`;
  } catch (error) {
    updateFileProgress({ stage: "error", percent: 0, detail: error.message || "Transcription failed" });
    showError(error.message || "Could not start media transcription.");
  } finally {
    if (state.fileProgressTimer) clearInterval(state.fileProgressTimer);
    state.fileProgressTimer = null;
    state.recording = false;
    $("recordButton").disabled = false;
    $("recordButtonText").textContent = "Start recording";
    $("recordButton").querySelector(".button-symbol").textContent = "●";
    updateFileButton();
  }
}

function updateFileProgress(progress) {
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  const labels = { uploading: "Uploading file", decoding: "Extracting audio", diarizing: "Detecting speakers", transcribing: "Transcribing audio", complete: "Complete", error: "Transcription failed" };
  const detail = progress.detail || "Working locally…";
  $("fileProgressText").textContent = progress.stage === "transcribing"
    ? (progress.detail ? `Transcribing audio · ${detail}` : "Transcribing audio · processing chunks…")
    : (progress.detail || labels[progress.stage] || "Processing file");
  $("fileProgressPercent").textContent = `${percent}%`;
  $("fileProgressBar").value = percent;
}

function pcmChunksToWav(chunks) {
  const length = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const buffer = new ArrayBuffer(44 + length);
  const view = new DataView(buffer);
  const write = (offset, value) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)); };
  write(0, "RIFF"); view.setUint32(4, 36 + length, true); write(8, "WAVE"); write(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); write(36, "data"); view.setUint32(40, length, true);
  let offset = 44;
  for (const chunk of chunks) {
    new Uint8Array(buffer, offset, chunk.byteLength).set(new Uint8Array(chunk));
    offset += chunk.byteLength;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

async function transcribeCapturedAudio() {
  const processedSources = new Set();
  const transcribed = [];
  const speakerNames = {};
  const warnings = [];
  for (const [source, chunks] of Object.entries(state.audioBuffers)) {
    if (!chunks.length) continue;
    const label = source === "mic" ? "microphone" : "computer audio";
    try {
      updateFileProgress({ stage: "uploading", percent: 0, detail: `Sending ${label} recording…` });
      const result = await request("/api/transcribe-file?filename=" + encodeURIComponent("live-" + source + ".wav") + "&source=" + encodeURIComponent(source), {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: pcmChunksToWav(chunks),
      });
      processedSources.add(source);
      transcribed.push(...(result.utterances || []));
      Object.assign(speakerNames, result.speaker_names || {});
      if (result.warning) warnings.push(`${label}: ${result.warning}`);
    } catch (error) {
      warnings.push(`${label}: ${error.message}`);
    }
  }
  if (processedSources.size) {
    state.utterances = state.utterances.filter((item) => !processedSources.has(item.source)).concat(transcribed);
    state.words = state.words.filter((item) => !processedSources.has(item.source));
  }
  state.speakerNames = {};
  state.utterances.forEach((item) => { state.speakerNames[item.speaker] ||= displaySpeaker(item.speaker, {}); });
  Object.assign(state.speakerNames, speakerNames);
  renderLiveTranscript();
  state.audioBuffers = {};
  if (warnings.length) showError("Transcript saved with some processing warnings: " + warnings.join("; "));
}

function stopAudioNodes() {
  for (const source of Object.values(state.sources)) {
    try { source.processor.disconnect(); } catch {}
    try { source.sourceNode.disconnect(); } catch {}
    try { source.mute.disconnect(); } catch {}
    source.stream.getTracks().forEach((track) => track.stop());
  }
  state.sources = {};
  state.displayStream?.getTracks().forEach((track) => track.stop());
  state.displayStream = null;
  if (state.fileMediaElement) {
    state.fileMediaElement.pause();
    state.fileMediaElement.removeAttribute("src");
    state.fileMediaElement.load();
    state.fileMediaElement.onended = null;
    state.fileMediaElement.classList.add("hidden");
  }
  if (state.fileObjectUrl) URL.revokeObjectURL(state.fileObjectUrl);
  state.fileMediaElement = null;
  state.fileObjectUrl = null;
  $("mediaPreview").classList.add("hidden");
}

async function stopRecording() {
  if (!state.recording || state.stopping) return;
  state.stopping = true;
  state.recording = false;
  const end = new Date();
  updateFileButton();
  $("recordButton").disabled = true;
  $("recordButtonText").textContent = "Finishing transcript…";
  $("recordStatus").textContent = "SAVING SESSION";
  $("recordHeading").textContent = "Finishing your transcript";
  $("recordDescription").textContent = "Transcribing the recorded audio and detecting speakers locally…";
  $("liveDot").classList.remove("recording");
  stopAudioNodes();

  if (state.audioContext) await state.audioContext.close().catch(() => {});
  state.audioContext = null;
  $("partialLine").classList.add("hidden");

  try {
    $("fileProgress").classList.remove("hidden");
    const progressPoll = async () => {
      try {
        const progress = await request("/api/file-progress");
        if (!["idle", "complete", "error"].includes(progress.stage)) updateFileProgress(progress);
      } catch (_) { /* Keep the stop flow moving if progress polling fails. */ }
    };
    state.fileProgressTimer = setInterval(progressPoll, 700);
    await transcribeCapturedAudio();
    updateFileProgress({ stage: "complete", percent: 100, detail: "Transcription complete · saved locally" });
  } catch (error) {
    state.audioBuffers = {};
    showError(`Could not transcribe the recorded audio: ${error.message}`);
  } finally {
    if (state.fileProgressTimer) clearInterval(state.fileProgressTimer);
    state.fileProgressTimer = null;
  }

  const dateTitle = state.startedAt.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const speakerNames = { ...state.speakerNames };
  try {
    const saved = await request("/api/transcripts", {
      method: "POST",
      body: JSON.stringify({
        start_time: state.startedAt.toISOString(),
        end_time: end.toISOString(),
        title: state.sessionTitle ? `${state.sessionTitle} · ${dateTitle}` : `Transcript · ${dateTitle}`,
        note: "",
        speaker_names: speakerNames,
        words: state.words,
        utterances: state.utterances,
      }),
    });
    state.activeTranscriptId = saved.id;
    syncTranscriptActions();
    state.stopping = false;
    $("recordButton").classList.remove("recording");
    $("recordButton").querySelector(".button-symbol").textContent = "●";
    $("recordButtonText").textContent = "Start recording";
    $("recordStatus").textContent = "TRANSCRIPT SAVED LOCALLY";
    $("recordHeading").textContent = "Your transcript is saved";
    $("recordDescription").textContent = "Add a title, note, or speaker names below, or start another recording.";
    $("recordButton").disabled = false;
    $("detailsPanel").classList.remove("hidden");
    $("titleInput").value = saved.transcript.title;
    $("noteInput").value = "";
    state.activeSavedTranscript = saved.transcript;
    renderSpeakerFields(saved.transcript);
    renderLiveTranscript();
    await refreshTranscriptList();
    $("captureHint").classList.remove("hidden");
    updateFileButton();
  } catch (error) {
    state.stopping = false;
    $("recordButton").classList.remove("recording");
    $("recordButton").querySelector(".button-symbol").textContent = "●";
    $("recordButtonText").textContent = "Start recording";
    $("recordButton").disabled = false;
    showError(`Recording stopped but the transcript could not be saved: ${error.message}`);
    updateFileButton();
  }
}

async function saveDetails() {
  if (!state.activeTranscriptId) return;
  const names = {};
  document.querySelectorAll(".speaker-name-input").forEach((input) => { names[input.dataset.speaker] = input.value.trim() || input.dataset.speaker; });
  try {
    const data = await request(`/api/transcripts/${encodeURIComponent(state.activeTranscriptId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: $("titleInput").value, note: $("noteInput").value, speaker_names: names }),
    });
    state.activeSavedTranscript = data;
    state.speakerNames = data.speaker_names || {};
    $("transcriptHeading").textContent = data.title;
    renderSavedTranscript(data);
    renderSpeakerFields(data);
    await refreshTranscriptList();
    $("saveDetailsButton").textContent = "Saved";
    setTimeout(() => { $("saveDetailsButton").textContent = "Save changes"; }, 1300);
  } catch (error) {
    showError(`Could not save changes: ${error.message}`);
  }
}

async function deleteTranscript() {
  const id = state.activeTranscriptId;
  if (!id) return;
  const title = $("transcriptHeading").textContent || "this transcript";
  if (!window.confirm(`Delete “${title}” permanently?`)) return;
  try {
    await request(`/api/transcripts/${encodeURIComponent(id)}`, { method: "DELETE" });
    state.activeTranscriptId = null;
    state.activeSavedTranscript = null;
    state.utterances = [];
    state.words = [];
    state.speakerNames = {};
    syncTranscriptActions();
    $("detailsPanel").classList.add("hidden");
    $("detailEyebrow").textContent = "LIVE SESSION";
    $("transcriptHeading").textContent = "Transcript preview";
    $("sessionTime").textContent = "";
    renderLiveTranscript();
    await refreshTranscriptList();
  } catch (error) {
    showError(`Could not delete transcript: ${error.message}`);
  }
}

$("recordButton").addEventListener("click", () => state.recording ? stopRecording() : startRecording());
$("mediaFileInput").addEventListener("change", () => {
  const file = $("mediaFileInput").files?.[0];
  $("filePickLabel").textContent = file?.name || "Choose audio or video";
  $("transcribeFileButton").textContent = "Transcribe file";
  $("transcribeFileButton").title = file ? file.name : "";
  updateFileButton();
});
$("transcribeFileButton").addEventListener("click", startFileTranscription);
$("saveDetailsButton").addEventListener("click", saveDetails);
$("deleteTranscriptButton").addEventListener("click", deleteTranscript);
$("refreshButton").addEventListener("click", () => { refreshTranscriptList(); refreshStatus(); });
$("searchInput").addEventListener("input", refreshTranscriptList);

refreshStatus();
refreshTranscriptList();
state.pollTimer = setInterval(refreshStatus, 1500);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshStatus();
});
window.addEventListener("focus", refreshStatus);
