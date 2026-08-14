import {
  ExternalE2EEKeyProvider,
  Room,
  RoomEvent,
  ScreenSharePresets,
  Track,
  TrackEvent,
  isE2EESupported,
  supportsAudioOutputSelection,
} from 'livekit-client';

const $ = (selector) => document.querySelector(selector);
const textEncoder = new TextEncoder();
const KEY_BYTES = 32;
// A place is only held for about three of these, because that hold is also how
// long a crashed browser keeps a room's key away from everybody else.
const HEARTBEAT_MS = 15_000;
// The server holds a place nobody has heartbeated for a shorter grace still, so
// the first beat lands early: it confirms the place well inside that grace and
// still leaves a whole interval of margin if it has to be retried.
const FIRST_HEARTBEAT_MS = 5_000;
const qualities = {
  economy: {
    label: '720p · 5 fps · 0.8 Mbps', resolution: { width: 1280, height: 720, frameRate: 5 },
    encoding: { maxBitrate: 800_000, maxFramerate: 5 }, layers: [ScreenSharePresets.h360fps3],
  },
  balanced: {
    label: '720p · 15 fps · 1.5 Mbps', resolution: { width: 1280, height: 720, frameRate: 15 },
    encoding: { maxBitrate: 1_500_000, maxFramerate: 15 }, layers: [ScreenSharePresets.h360fps3],
  },
  sharp: {
    label: '1080p · 30 fps · 4 Mbps', resolution: { width: 1920, height: 1080, frameRate: 30 },
    encoding: { maxBitrate: 4_000_000, maxFramerate: 30 },
    layers: [ScreenSharePresets.h360fps3, ScreenSharePresets.h720fps5],
  },
};

const state = {
  enabled: false, maxParticipants: 0, room: null, channel: null, lease: null,
  key: null, fingerprint: null, sharing: [], stoppingShare: false,
  connected: false,
  heartbeat: null, firstHeartbeat: null, heartbeatFailures: 0,
  muted: false, deafened: false,
  pendingInvite: null, dialogChannel: null,
  encryptionStatus: new Map(), encryptionTimers: new Map(),
};

function bytesBase64url(bytes) {
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function base64urlBytes(value) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) throw new Error('Invalid call key');
  const normalized = value.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(normalized + '='.repeat((4 - normalized.length % 4) % 4));
  const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
  if (bytes.length !== KEY_BYTES) throw new Error('Invalid call key');
  return bytes;
}

function rememberKey(channelId, key) {
  try { sessionStorage.setItem(`campfire.voice.key.${Number(channelId)}`, bytesBase64url(key)); }
  catch { /* Private browsing may deny storage; the live in-memory copy still works. */ }
}

function recalledKey(channelId) {
  try {
    const stored = sessionStorage.getItem(`campfire.voice.key.${Number(channelId)}`);
    return stored ? base64urlBytes(stored) : null;
  } catch { return null; }
}

async function fingerprint(key) {
  const digest = await crypto.subtle.digest('SHA-256', key);
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

function importFragment() {
  const match = location.hash.match(/^#voice=(\d+)\.([A-Za-z0-9_-]{43})$/);
  if (!match) return;
  try {
    const key = base64urlBytes(match[2]);
    rememberKey(Number(match[1]), key);
    state.pendingInvite = Number(match[1]);
    // Fragments are not sent in HTTP requests, but removing it also keeps the
    // key out of screenshots, copied address bars, and browser history.
    history.replaceState(null, '', location.pathname + location.search);
  } catch { /* An invalid fragment is ordinary navigation, not a call invite. */ }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.error || `Media request failed (${response.status})`);
  return body;
}

function chromiumShareAudio() {
  const brands = navigator.userAgentData?.brands?.map(item => item.brand).join(' ') || '';
  return /Chromium|Google Chrome|Microsoft Edge/.test(brands)
    || /(?:Chrome|Chromium|Edg|OPR)\//.test(navigator.userAgent);
}

function browserNote() {
  if (!navigator.mediaDevices?.getDisplayMedia) return 'This browser cannot capture a screen.';
  if (chromiumShareAudio()) {
    return 'Shared audio is browser-controlled. Choose a browser tab and enable its audio; Chromium on Windows may also offer system audio.';
  }
  return 'This browser can share video, but does not expose shared tab or application audio to Campfire.';
}

function loopbackMediaUrl(value) {
  try {
    const host = new URL(value).hostname.toLowerCase();
    return host === 'localhost' || host === '127.0.0.1' || host === '[::1]';
  } catch { return false; }
}

function setCallStatus(message, tone = '') {
  const output = $('#voice-status');
  if (!output) return;
  output.textContent = message;
  output.className = `voice-status ${tone}`.trim();
}

function callUrl() {
  return `${location.origin}${location.pathname}${location.search}#voice=${state.channel.id}.${bytesBase64url(state.key)}`;
}

async function copyCallLink() {
  if (!state.channel || !state.key) return;
  const link = callUrl();
  try {
    await navigator.clipboard.writeText(link);
    setCallStatus('Encrypted call link copied. Send it through a trusted private channel.', 'good');
  } catch { prompt('Copy this encrypted call link:', link); }
}

function metadataFingerprint(participant) {
  try { return JSON.parse(participant.metadata || '{}').keyFingerprint || ''; }
  catch { return ''; }
}

function micPublication(participant) {
  return participant.getTrackPublication(Track.Source.Microphone);
}

function participantMarkup(participant, local = false) {
  const mismatch = metadataFingerprint(participant) !== state.fingerprint;
  const mic = micPublication(participant);
  const muted = !mic || mic.isMuted;
  const quality = participant.connectionQuality || 'unknown';
  return `<div class="voice-person ${participant.isSpeaking ? 'speaking' : ''} ${mismatch ? 'key-mismatch' : ''}">
    <span class="voice-person-dot" title="Connection: ${quality}"></span>
    <strong>${escapeText(participant.name || participant.identity)}${local ? ' (you)' : ''}</strong>
    <span title="${mismatch ? 'Encryption key mismatch' : muted ? 'Microphone muted' : 'Microphone on'}">${mismatch ? '⚠' : muted ? '⌁' : '●'}</span>
  </div>`;
}

function escapeText(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function renderParticipants() {
  if (!state.room) { $('#voice-participants').innerHTML = ''; return; }
  const remote = [...state.room.remoteParticipants.values()];
  $('#voice-participants').innerHTML = participantMarkup(state.room.localParticipant, true)
    + remote.map(participant => participantMarkup(participant)).join('');
  $('#voice-count').textContent = `${remote.length + 1}/${state.maxParticipants}`;
}

function renderControls() {
  $('#voice-mute').textContent = state.muted ? 'Unmute' : 'Mute';
  $('#voice-mute').classList.toggle('active', state.muted);
  $('#voice-deafen').textContent = state.deafened ? 'Undeafen' : 'Deafen';
  $('#voice-deafen').classList.toggle('active', state.deafened);
  const sharing = state.sharing.length > 0;
  $('#voice-share').textContent = sharing ? 'Stop sharing' : 'Share screen';
  $('#voice-share').classList.toggle('danger-stop', sharing);
  $('#voice-stop-floating').classList.toggle('hidden', !sharing);
  document.querySelectorAll('.voice-remote-audio').forEach(element => { element.muted = state.deafened; });
}

async function refreshDevices() {
  if (!state.room) return;
  try {
    const inputs = await Room.getLocalDevices('audioinput', false);
    $('#voice-input').innerHTML = inputs.map((device, index) =>
      `<option value="${escapeText(device.deviceId)}">${escapeText(device.label || `Microphone ${index + 1}`)}</option>`).join('');
    const activeInput = state.room.getActiveDevice('audioinput');
    if (activeInput) $('#voice-input').value = activeInput;

    const outputs = (await navigator.mediaDevices.enumerateDevices())
      .filter(device => device.kind === 'audiooutput');
    const supported = supportsAudioOutputSelection();
    $('#voice-output').disabled = !supported;
    $('#voice-output').innerHTML = supported
      ? outputs.map((device, index) => `<option value="${escapeText(device.deviceId)}">${escapeText(device.label || `Speaker ${index + 1}`)}</option>`).join('')
      : '<option>Browser default (selection unsupported)</option>';
    const activeOutput = state.room.getActiveDevice('audiooutput');
    if (supported && activeOutput) $('#voice-output').value = activeOutput;
  } catch (error) { setCallStatus(`Device list unavailable: ${error.message}`, 'warn'); }
}

function removeTrackElements(track, publication) {
  track?.detach().forEach(element => element.remove());
  if (publication?.trackSid) document.querySelector(`[data-voice-track="${CSS.escape(publication.trackSid)}"]`)?.remove();
  updateStage();
}

function updateStage() {
  const stage = $('#voice-stage');
  const hasVideo = Boolean(stage.querySelector('video'));
  stage.classList.toggle('hidden', !hasVideo);
  $('.chat')?.classList.toggle('voice-watching', hasVideo);
}

function attachRemoteTrack(track, publication, participant) {
  if (!publication.isEncrypted) {
    track.detach().forEach(element => element.remove());
    void leave('An unencrypted media track appeared; the call was closed.');
    return;
  }
  if (track.kind === Track.Kind.Audio) {
    const element = track.attach();
    element.autoplay = true;
    element.className = 'voice-remote-audio';
    element.muted = state.deafened;
    element.dataset.voiceSid = publication.trackSid;
    $('#voice-audio').append(element);
  } else if (publication.source === Track.Source.ScreenShare) {
    const frame = document.createElement('figure');
    frame.className = 'voice-screen'; frame.dataset.voiceTrack = publication.trackSid;
    const element = track.attach();
    element.autoplay = true; element.playsInline = true;
    const caption = document.createElement('figcaption');
    caption.textContent = `${participant.name || participant.identity} is sharing`;
    frame.append(element, caption); $('#voice-stage').append(frame); updateStage();
  }
}

function clearEncryptionParticipant(participant) {
  const identity = participant?.identity;
  if (!identity) return;
  clearTimeout(state.encryptionTimers.get(identity));
  state.encryptionTimers.delete(identity);
  state.encryptionStatus.delete(identity);
}

function encryptionStatusChanged(room, encrypted, participant) {
  const identity = participant?.identity;
  if (!identity) return;
  state.encryptionStatus.set(identity, encrypted);
  clearTimeout(state.encryptionTimers.get(identity));
  state.encryptionTimers.delete(identity);
  if (encrypted) return;
  // The worker reports `false` once while a participant cryptor is being
  // initialized. Track metadata and decryption failures are enforced
  // immediately; this timer catches only a sustained later downgrade.
  const timer = setTimeout(() => {
    state.encryptionTimers.delete(identity);
    if (state.room === room && state.encryptionStatus.get(identity) === false
        && participant.trackPublications.size > 0) {
      void leave('A participant disabled media encryption; the call was closed.');
    }
  }, 3_000);
  state.encryptionTimers.set(identity, timer);
}

function configureRoomEvents(room) {
  const rerender = () => renderParticipants();
  room.on(RoomEvent.ParticipantConnected, rerender)
    .on(RoomEvent.ParticipantDisconnected, participant => {
      clearEncryptionParticipant(participant); rerender();
    })
    .on(RoomEvent.ActiveSpeakersChanged, rerender)
    .on(RoomEvent.ConnectionQualityChanged, rerender)
    .on(RoomEvent.TrackMuted, rerender)
    .on(RoomEvent.TrackUnmuted, rerender)
    .on(RoomEvent.ParticipantMetadataChanged, rerender)
    .on(RoomEvent.TrackPublished, publication => {
      if (!publication.isEncrypted) void leave('An unencrypted media track appeared; the call was closed.');
    })
    .on(RoomEvent.TrackSubscribed, attachRemoteTrack)
    .on(RoomEvent.TrackUnsubscribed, removeTrackElements)
    .on(RoomEvent.Reconnecting, () => setCallStatus('Reconnecting…', 'warn'))
    .on(RoomEvent.Reconnected, () => setCallStatus('Connected · media E2EE on', 'good'))
    .on(RoomEvent.AudioPlaybackStatusChanged, playing => {
      if (!playing) setCallStatus('Browser blocked audio playback. Click Undeafen or rejoin.', 'warn');
    })
    .on(RoomEvent.ParticipantEncryptionStatusChanged,
      (encrypted, participant) => encryptionStatusChanged(room, encrypted, participant))
    .on(RoomEvent.EncryptionError, () => void leave('Media encryption failed; the call was closed.'))
    .on(RoomEvent.Disconnected, () => {
      if (state.room === room && state.connected) {
        // Use the normal path so locally captured display audio/video is
        // stopped even when the network, rather than the button, ended first.
        void leave('The media connection ended.');
      }
    });
}

async function join(channel, key) {
  if (!state.enabled) throw new Error('Voice is not configured on this instance.');
  if (!isE2EESupported()) throw new Error('This browser cannot provide encoded-frame media E2EE, so Campfire will not start an unencrypted call.');
  if (!navigator.mediaDevices?.getUserMedia) throw new Error('This browser cannot capture a microphone.');
  if (state.room) await leave();
  let room = null;
  try {
    state.channel = channel; state.key = key; state.fingerprint = await fingerprint(key);
    setCallStatus('Authorizing encrypted call…');
    const grant = await api(`/api/channels/${Number(channel.id)}/voice/token`, {
      method: 'POST', body: JSON.stringify({ key_fingerprint: state.fingerprint }),
    });
    // Do not overwrite a working saved call key until the occupied-room
    // fingerprint and participant reservation have accepted this one.
    rememberKey(channel.id, key);
    state.lease = grant.lease; state.maxParticipants = Number(grant.max_participants);
    // The reservation starts expiring now, not once the call is up, so the
    // heartbeat starts with the lease. A slow negotiation must not let the
    // place lapse underneath the very connection it is holding open.
    startHeartbeat();
    const keyProvider = new ExternalE2EEKeyProvider();
    const keyBuffer = key.buffer.slice(key.byteOffset, key.byteOffset + key.byteLength);
    await keyProvider.setKey(keyBuffer);
    room = new Room({
      adaptiveStream: true,
      dynacast: true,
      encryption: {
        keyProvider,
        worker: new Worker('/livekit-e2ee-worker.js', { type: 'module', name: 'campfire-media-e2ee' }),
      },
      audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      publishDefaults: { simulcast: true, stopMicTrackOnMute: false },
    });
    state.room = room; configureRoomEvents(room);
    setCallStatus('Connecting…');
    const forceTurn = new URLSearchParams(location.search).get('forceTurn') === '1';
    if (forceTurn && loopbackMediaUrl(grant.url)) {
      throw new Error('TURN is not part of the loopback test. Remove ?forceTurn=1 from the URL.');
    }
    // LiveKit requires E2EE to be enabled before connect so the participant
    // joins with an enabled cryptor rather than briefly advertising `false`.
    await room.setE2EEEnabled(true);
    await room.connect(grant.url, grant.token, {
      autoSubscribe: true,
      ...(forceTurn ? { rtcConfig: { iceTransportPolicy: 'relay' } } : {}),
    });
    // A disconnect emitted while connect() is still pending is part of that
    // failed attempt. Let connect() surface its specific signaling/ICE error in
    // the dialog instead of racing it with the generic established-call alert.
    state.connected = true;
    const microphone = await room.localParticipant.setMicrophoneEnabled(true, {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    });
    if (!microphone?.isEncrypted) throw new Error('The microphone was not published with media E2EE.');
    state.muted = false; state.deafened = false;
    try { await room.startAudio(); } catch { /* The status event explains a blocked autoplay. */ }
    $('#voice-dialog').close();
    state.dialogChannel = null;
    $('#voice-dock').classList.remove('hidden');
    $('#voice-channel-name').textContent = channel.name;
    setCallStatus(`Connected · media E2EE on${forceTurn ? ' · TURN relay forced' : ''}`, 'good');
    renderParticipants(); renderControls(); await refreshDevices();
  } catch (error) {
    // A failed microphone publication or E2EE transition can happen after the
    // signaling socket connected. Explicitly tear it down before releasing the
    // application lease so no half-joined participant survives the UI error.
    state.connected = false; state.room = null;
    if (room) {
      try { await room.disconnect(); } catch { /* Preserve the original join error. */ }
    }
    await finishLeave();
    throw error;
  }
}

function startHeartbeat() {
  state.heartbeatFailures = 0;
  state.firstHeartbeat = setTimeout(heartbeat, FIRST_HEARTBEAT_MS);
  state.heartbeat = setInterval(heartbeat, HEARTBEAT_MS);
}

async function heartbeat() {
  if (!state.channel || !state.lease) return;
  try {
    await api(`/api/channels/${Number(state.channel.id)}/voice/heartbeat`, {
      method: 'POST', body: JSON.stringify({ lease: state.lease }),
    });
    state.heartbeatFailures = 0;
  } catch {
    state.heartbeatFailures += 1;
    if (state.heartbeatFailures >= 2) await leave('Voice access was revoked or its lease expired.');
  }
}

async function finishLeave(message = '') {
  clearInterval(state.heartbeat); state.heartbeat = null;
  clearTimeout(state.firstHeartbeat); state.firstHeartbeat = null;
  const channel = state.channel, lease = state.lease;
  state.connected = false;
  state.room = null; state.channel = null; state.lease = null;
  state.key = null; state.fingerprint = null;
  state.encryptionTimers.forEach(timer => clearTimeout(timer));
  state.encryptionTimers.clear(); state.encryptionStatus.clear();
  state.sharing = []; state.stoppingShare = false;
  $('#voice-dock').classList.add('hidden');
  // The dock is reused by the next call, so a leftover "Connected" line would
  // otherwise be the first thing the next join shows.
  setCallStatus('');
  $('#voice-audio').replaceChildren(); $('#voice-stage').replaceChildren(); updateStage();
  renderControls(); renderParticipants();
  if (channel && lease) {
    try {
      await api(`/api/channels/${Number(channel.id)}/voice/lease`, {
        method: 'DELETE', body: JSON.stringify({ lease }),
      });
    } catch { /* The lease expires quickly if the network is already gone. */ }
  }
  if (message) alert(message);
}

async function leave(message = '') {
  const room = state.room;
  if (room) {
    try { await stopScreen(); } catch { /* Disconnect still has to stop every local track. */ }
    // Clear ownership before disconnect emits synchronously, so the network
    // disconnect handler does not race this deliberate cleanup.
    state.connected = false; state.room = null;
    try { await room.disconnect(); } catch { /* Lease cleanup remains mandatory. */ }
  }
  await finishLeave(message);
}

async function toggleMute() {
  if (!state.room) return;
  const enable = state.muted;
  await state.room.localParticipant.setMicrophoneEnabled(enable, {
    echoCancellation: true, noiseSuppression: true, autoGainControl: true,
  });
  state.muted = !enable; renderControls(); renderParticipants();
}

async function toggleDeafen() {
  if (!state.room) return;
  state.deafened = !state.deafened;
  if (state.deafened && !state.muted) await toggleMute();
  if (!state.deafened) try { await state.room.startAudio(); } catch { /* status event handles it */ }
  renderControls();
}

async function startScreen() {
  if (!state.room || state.sharing.length) return;
  if (!navigator.mediaDevices?.getDisplayMedia) throw new Error('Screen capture is not supported here.');
  const quality = qualities[$('#voice-quality').value] || qualities.balanced;
  const wantsAudio = $('#voice-share-audio').checked && chromiumShareAudio();
  const tracks = await state.room.localParticipant.createScreenTracks({
    video: true,
    resolution: quality.resolution,
    contentHint: 'detail',
    systemAudio: wantsAudio ? 'include' : 'exclude',
    audio: wantsAudio ? {
      autoGainControl: false,
      echoCancellation: false,
      noiseSuppression: false,
      channelCount: 2,
      restrictOwnAudio: true,
    } : false,
  });
  if (!tracks.some(track => track.kind === Track.Kind.Video)) {
    tracks.forEach(track => track.stop());
    throw new Error('The browser did not supply a display-video track.');
  }
  state.sharing = tracks;
  // The sidebar becomes a drawer on small screens. Keep a stop action outside
  // it from the moment capture begins, including while publication negotiates.
  renderControls();
  tracks.forEach(track => track.once(TrackEvent.Ended, () => void stopScreen()));
  try {
    for (const track of tracks) {
      if (track.kind === Track.Kind.Video) {
        const publication = await state.room.localParticipant.publishTrack(track, {
          simulcast: true,
          videoEncoding: quality.encoding,
          screenShareSimulcastLayers: quality.layers,
          degradationPreference: 'maintain-resolution',
        });
        if (!publication.isEncrypted) throw new Error('The screen was not published with media E2EE.');
        const frame = document.createElement('figure');
        frame.className = 'voice-screen local'; frame.dataset.voiceTrack = 'local';
        const element = track.attach(); element.autoplay = true; element.muted = true; element.playsInline = true;
        const caption = document.createElement('figcaption'); caption.textContent = 'You are sharing';
        frame.append(element, caption); $('#voice-stage').append(frame); updateStage();
      } else {
        const publication = await state.room.localParticipant.publishTrack(
          track, { dtx: false, red: true, forceStereo: true });
        if (!publication.isEncrypted) throw new Error('Shared audio was not published with media E2EE.');
      }
    }
    const capturedAudio = tracks.some(track => track.source === Track.Source.ScreenShareAudio);
    setCallStatus(wantsAudio && !capturedAudio
      ? 'Screen shared without audio—the selected source/browser supplied no audio track.'
      : `Screen sharing · ${quality.label}${capturedAudio ? ' · separate app-audio track' : ''}`, wantsAudio && !capturedAudio ? 'warn' : 'good');
  } catch (error) {
    await stopScreen(); throw error;
  }
  renderControls();
}

async function stopScreen() {
  if (!state.sharing.length || state.stoppingShare) return;
  state.stoppingShare = true;
  const tracks = state.sharing.splice(0);
  try {
    await Promise.all(tracks.map(track => state.room?.localParticipant.unpublishTrack(track, true)));
  } finally {
    tracks.forEach(track => { track.detach().forEach(element => element.remove()); track.stop(); });
    document.querySelector('[data-voice-track="local"]')?.remove();
    state.stoppingShare = false; updateStage(); renderControls();
    if (state.room) setCallStatus('Connected · media E2EE on', 'good');
  }
}

function open(channel) {
  if (!channel || channel.kind !== 'voice') return;
  if (state.room && state.channel?.id === channel.id) return;
  state.dialogChannel = channel;
  const key = recalledKey(channel.id);
  $('#voice-dialog-name').textContent = channel.name;
  $('#voice-browser-note').textContent = browserNote();
  $('#voice-join').classList.toggle('hidden', !key);
  $('#voice-join').textContent = key ? 'Join with saved call key' : '';
  $('#voice-new-call').textContent = key ? 'Start with a new key' : 'Start encrypted call';
  // Without the key, starting is the only control on offer and it is refused
  // outright whenever a call is already running. Say so before it is pressed:
  // the key travels by link, and nothing else in Campfire hands it over.
  $('#voice-dialog-status').textContent = state.enabled
    ? (key
      ? 'This tab has an encrypted call key for the channel.'
      : 'No call key is saved in this tab. Starting creates one, which works only while nobody is in this call. To join a call already running, open its link from somebody inside it — Campfire cannot give you their key.')
    : 'Voice is not configured on this instance.';
  $('#voice-join').disabled = !state.enabled;
  $('#voice-new-call').disabled = !state.enabled;
  $('#voice-dialog').showModal();
}

async function joinFromDialog(newKey) {
  const button = newKey ? $('#voice-new-call') : $('#voice-join');
  button.disabled = true; $('#voice-dialog-status').textContent = 'Opening encrypted call…';
  try {
    const channel = state.dialogChannel;
    if (!channel) throw new Error('Choose a voice channel again.');
    const key = newKey ? crypto.getRandomValues(new Uint8Array(KEY_BYTES)) : recalledKey(channel.id);
    if (!key) throw new Error('This tab no longer has the call key. Start a new call or open its current link.');
    await join(channel, key);
  } catch (error) { $('#voice-dialog-status').textContent = error.message; }
  finally { button.disabled = !state.enabled; }
}

function configure(media) {
  state.enabled = Boolean(media?.enabled);
  state.maxParticipants = Number(media?.max_participants) || 0;
}

function consumeInvite() {
  const channelId = state.pendingInvite;
  state.pendingInvite = null;
  return channelId;
}

function bind() {
  importFragment();
  const chromium = chromiumShareAudio();
  $('#voice-share-audio').disabled = !chromium;
  $('#voice-share-audio-note').textContent = browserNote();
  $('#voice-close-dialog').onclick = () => $('#voice-dialog').close();
  $('#voice-join').onclick = () => joinFromDialog(false);
  $('#voice-new-call').onclick = () => joinFromDialog(true);
  $('#voice-mute').onclick = () => toggleMute().catch(error => setCallStatus(error.message, 'warn'));
  $('#voice-deafen').onclick = () => toggleDeafen().catch(error => setCallStatus(error.message, 'warn'));
  $('#voice-share').onclick = () => (state.sharing.length ? stopScreen() : startScreen())
    .catch(error => setCallStatus(error.message, 'warn'));
  $('#voice-stop-floating').onclick = () => stopScreen()
    .catch(error => setCallStatus(error.message, 'warn'));
  $('#voice-copy-link').onclick = copyCallLink;
  $('#voice-leave').onclick = () => leave();
  $('#voice-input').onchange = event => state.room?.switchActiveDevice('audioinput', event.target.value)
    .catch(error => setCallStatus(error.message, 'warn'));
  $('#voice-output').onchange = event => state.room?.switchActiveDevice('audiooutput', event.target.value)
    .catch(error => setCallStatus(error.message, 'warn'));
  navigator.mediaDevices?.addEventListener?.('devicechange', () => void refreshDevices());
  window.addEventListener('pagehide', () => {
    if (state.channel && state.lease) fetch(`/api/channels/${Number(state.channel.id)}/voice/lease`, {
      method: 'DELETE', keepalive: true, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lease: state.lease }),
    });
  });
}

bind();
window.CampfireVoice = { configure, consumeInvite, open, leave };
