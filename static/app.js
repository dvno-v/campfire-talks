const $ = (s) => document.querySelector(s);
const state = { user: null, communities: [], community: null, channel: null, members: [], online: new Set(), messages: new Map(), eventSource: null, streamOpened: false, unread: new Map(), defaultMode: 'all', unreadBoundary: false };
let registering = false;

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Something went wrong');
  return data;
}

function initials(name) { return name.slice(0, 2).toUpperCase(); }
const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
// Values are interpolated into attributes as well as text, so quotes must escape too.
function escapeHTML(value) { return String(value ?? '').replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]); }
function showAuth() { $('#auth').classList.remove('hidden'); $('#app').classList.add('hidden'); }

async function enterApp() {
  const data = await api('/api/bootstrap');
  state.user = data.user; state.communities = data.communities;
  state.defaultMode = data.notifications?.default_mode || 'all';
  state.unread.clear();
  data.communities.forEach(community => community.channels.forEach(channel => rememberChannelState(channel.id, channel)));
  renderNotificationBell();
  $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden');
  $('#current-user').textContent = state.user.username;
  $('#avatar').textContent = initials(state.user.username);
  state.eventSource?.close(); state.eventSource = new EventSource('/api/events');
  // The member list reports who is connected, so re-read it once this stream is
  // registered — on first open and again after any automatic reconnect.
  state.eventSource.onopen = () => {
    if (state.community) loadMembers(state.community.id);
    // A reconnect means the stream was down for a while; anything sent during
    // the gap was never delivered, so re-read the channel rather than trust it.
    // Unread totals are re-read for the same reason: they would otherwise stay
    // wrong without saying so.
    if (state.streamOpened) { resyncChannel(); refreshUnread(); }
    state.streamOpened = true;
  };
  state.eventSource.onmessage = ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === 'stream.reset') { refreshUnread(); return resyncChannel(); }
    if (event.type.startsWith('presence.')) return applyPresence(event);
    if (event.type === 'member.joined') return applyMemberJoined(event);
    if (event.type === 'channel.created') return applyChannelCreated(event);
    if (event.type === 'message.created') countUnread(event);
    if (event.type === 'message.deleted') discountUnread(event);
    if (event.channel_id !== state.channel?.id) return;
    if (event.type === 'message.deleted') return removeMessage(event.id);
    if (event.type === 'message.updated') return replaceMessage(event);
    if (document.querySelector(`[data-message-id="${Number(event.id)}"]`)) return;
    markUnreadBoundary(event); appendMessage(event); readActiveChannel();
  };
  selectCommunity(state.communities[0]);
}

function renderCommunities() {
  $('#community-icons').innerHTML = state.communities.map(c => `<button class="community-icon ${c.id === state.community?.id ? 'active' : ''} ${communityHasUnread(c) ? 'has-unread' : ''}" data-id="${c.id}" title="${escapeHTML(c.name)}">${escapeHTML(initials(c.name))}</button>`).join('');
  document.querySelectorAll('.community-icon').forEach(button => button.onclick = () => selectCommunity(state.communities.find(c => c.id === +button.dataset.id)));
}

function selectCommunity(community) {
  if ($('#invite-dialog').open) $('#invite-dialog').close();
  if ($('#notify-dialog').open) $('#notify-dialog').close();
  state.community = community; $('#community-name').textContent = community?.name || 'Campfire'; renderCommunities();
  state.members = []; renderMembers();
  renderChannels();
  selectChannel(community?.channels[0]);
  if (community) loadMembers(community.id);
}

function renderChannels() {
  const community = state.community;
  $('#channels').innerHTML = (community?.channels || []).map(c => {
    const unread = channelState(c.id).unread, muted = notifyMode(c.id) === 'none';
    // A muted channel still shows that it moved, but never with a count that
    // asks to be cleared.
    const badge = unread && !muted ? `<span class="channel-badge">${unread > 99 ? '99+' : unread}</span>` : '';
    return `<button class="channel ${c.id === state.channel?.id ? 'active' : ''} ${unread ? 'unread' : ''} ${muted ? 'muted' : ''}" data-id="${c.id}">${escapeHTML(c.name)}${badge}</button>`;
  }).join('');
  document.querySelectorAll('.channel').forEach(button => button.onclick = () => selectChannel(community.channels.find(c => c.id === +button.dataset.id)));
}

function channelState(channelId) { return state.unread.get(Number(channelId)) || { unread: 0, last_read_message_id: 0, notify: null }; }
// A per-channel choice wins; otherwise the account default decides.
function notifyMode(channelId) { return channelState(channelId).notify || state.defaultMode; }
function communityHasUnread(community) { return community.channels.some(channel => channelState(channel.id).unread > 0 && notifyMode(channel.id) !== 'none'); }
function channelNamed(channelId) { return state.communities.flatMap(community => community.channels).find(channel => channel.id === Number(channelId)); }

function rememberChannelState(channelId, patch) {
  const merged = { ...channelState(channelId), ...patch };
  state.unread.set(Number(channelId), { unread: Math.max(0, Number(merged.unread) || 0), last_read_message_id: Number(merged.last_read_message_id) || 0, notify: merged.notify ?? null });
}

function applyChannelState(payload) { rememberChannelState(payload.channel_id, payload); renderChannels(); renderCommunities(); renderNotificationBell(); }

// A message counts as unread until somebody has actually been in a position to
// see it: never your own, and never one arriving in the channel already on
// screen while this tab is in front of a person.
function countUnread(message) {
  if (Number(message.author_id) === state.user?.id) return;
  if (message.channel_id !== state.channel?.id || document.visibilityState !== 'visible') {
    rememberChannelState(message.channel_id, { unread: channelState(message.channel_id).unread + 1 });
    renderChannels(); renderCommunities();
  }
  maybeNotify(message);
}

// Deleting an unread message removes it from the count. The event does not say
// who wrote it, so a count can drift low until the next reconnect re-reads it.
function discountUnread(event) {
  const entry = channelState(event.channel_id);
  if (!entry.unread || Number(event.id) <= entry.last_read_message_id) return;
  rememberChannelState(event.channel_id, { unread: entry.unread - 1 });
  renderChannels(); renderCommunities();
}

async function refreshUnread() {
  try {
    const data = await api('/api/unread');
    state.defaultMode = data.default_mode || 'all';
    state.unread.clear();
    data.channels.forEach(entry => rememberChannelState(entry.channel_id, entry));
    renderChannels(); renderCommunities(); renderNotificationBell();
  } catch { /* the badges keep their last known values until the next attempt */ }
}

function readActiveChannel() {
  if (!state.channel || document.visibilityState !== 'visible') return;
  markRead(state.channel.id, Math.max(0, ...state.messages.keys()));
}

// The marker is moved locally first so the badge clears the moment you look at
// the channel; the server reconciles the exact figure a round trip later.
async function markRead(channelId, messageId) {
  const entry = channelState(channelId);
  if (messageId <= entry.last_read_message_id) {
    if (entry.unread) { rememberChannelState(channelId, { unread: 0 }); renderChannels(); renderCommunities(); }
    return;
  }
  rememberChannelState(channelId, { unread: 0, last_read_message_id: messageId });
  renderChannels(); renderCommunities();
  try { applyChannelState(await api(`/api/channels/${Number(channelId)}/read`, { method: 'POST', body: JSON.stringify({ message_id: messageId }) })); }
  catch { /* retried the next time this channel is opened or the tab is focused */ }
}

function appendUnreadDivider() {
  state.unreadBoundary = true;
  const divider = document.createElement('div');
  divider.className = 'unread-divider'; divider.textContent = 'NEW MESSAGES';
  $('#messages').append(divider);
}

// A message arriving while the tab is in the background still earns the
// divider, so coming back shows where reading stopped.
function markUnreadBoundary(message) {
  if (state.unreadBoundary || document.visibilityState === 'visible') return;
  if (Number(message.author_id) === state.user?.id) return;
  appendUnreadDivider();
}

// Deliberately contentless: this can be shown on a lock screen or a shared
// desktop, so it reports that something happened and where, never what was said.
function maybeNotify(message) {
  if (notifyMode(message.channel_id) !== 'all') return;
  if (document.visibilityState === 'visible' && message.channel_id === state.channel?.id) return;
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const channel = channelNamed(message.channel_id);
  const notification = new Notification(channel ? `New message in #${channel.name}` : 'New message', {
    body: `${message.username} wrote something`, tag: `campfire-${Number(message.channel_id)}` });
  notification.onclick = () => { window.focus(); if (channel) openChannel(channel.id); notification.close(); };
}

function openChannel(channelId) {
  const community = state.communities.find(c => c.channels.some(channel => channel.id === Number(channelId)));
  if (!community) return;
  if (community.id !== state.community?.id) selectCommunity(community);
  selectChannel(community.channels.find(channel => channel.id === Number(channelId)));
}

async function loadMembers(communityId) {
  try { const data=await api(`/api/communities/${communityId}/members`); if(state.community?.id!==communityId)return; state.members=data.members; state.online=new Set(data.members.filter(member=>member.online).map(member=>member.id)); renderMembers(); refreshMessages(); }
  catch(error) { if(state.community?.id===communityId) $('#member-list').innerHTML=`<p class="member-empty">${escapeHTML(error.message)}</p>`; }
}

function memberRow(member) {
  return `<article class="member-row ${state.online.has(member.id) ? 'online' : 'offline'}"><div class="member-avatar">${escapeHTML(initials(member.username))}<span class="presence-dot" title="${state.online.has(member.id) ? 'Online' : 'Offline'}"></span></div><div class="member-identity"><strong>${escapeHTML(member.username)}</strong>${member.role==='owner'?'<span class="member-role">OWNER</span>':''}</div></article>`;
}

function renderMembers() {
  $('#member-count').textContent = state.members.length;
  $('#invite-friend').classList.toggle('hidden', !isOwner(state.user?.id));
  if (!state.members.length) { $('#member-list').innerHTML = '<p class="member-empty">Loading members…</p>'; return; }
  const groups = [['ONLINE', state.members.filter(m => state.online.has(m.id))], ['OFFLINE', state.members.filter(m => !state.online.has(m.id))]];
  $('#member-list').innerHTML = groups.filter(([, members]) => members.length)
    .map(([label, members]) => `<div class="member-group">${label} — ${members.length}</div>${members.map(memberRow).join('')}`).join('');
}

function applyPresence(event) {
  const userId = Number(event.user_id);
  if (event.type === 'presence.online') state.online.add(userId); else state.online.delete(userId);
  if (state.members.some(member => member.id === userId)) renderMembers();
}

// Owner first, then by name, matching the order the server returns.
function sortMembers(members) {
  return members.sort((a, b) => (a.role === 'owner' ? 0 : 1) - (b.role === 'owner' ? 0 : 1)
    || a.username.localeCompare(b.username, undefined, { sensitivity: 'base' }));
}

function applyMemberJoined(event) {
  if (event.community_id !== state.community?.id) return;
  if (state.members.some(member => member.id === event.member.id)) return;
  state.members = sortMembers([...state.members, event.member]);
  renderMembers();
}

// Re-reads the channel outright: a gap can hide edits and deletions too, not
// only new messages, so appending what is missing would not be enough.
function resyncChannel() { if (state.channel) selectChannel(state.channel); }

function applyChannelCreated(event) {
  const community = state.communities.find(c => c.id === event.community_id);
  if (!community || community.channels.some(channel => channel.id === event.id)) return;
  community.channels.push({ id: event.id, name: event.name });
  if (community.id === state.community?.id) renderChannels();
}

function isOwner(userId) { return state.members.some(member => member.id===Number(userId) && member.role==='owner'); }
// Owner badges and delete rights both depend on the member list, which arrives after the first messages.
function refreshMessages() { document.querySelectorAll('.message-row').forEach(row => { const message=state.messages.get(Number(row.dataset.messageId)); if(message) renderInto(row, message); }); }

async function selectChannel(channel) {
  state.channel = channel; state.unreadBoundary = false;
  if (!channel) return;
  $('#channel-name').textContent = channel.name; $('#message').placeholder = `Message #${channel.name}`;
  renderChannels();
  // Read the marker before anything clears it: it decides where the divider goes.
  const marker = channelState(channel.id).last_read_message_id;
  // Clear before awaiting so the previous channel's messages never sit under the new channel's name.
  state.messages.clear();
  $('#messages').innerHTML = '<div class="empty"><div>#</div><h2>Welcome to #' + escapeHTML(channel.name) + '</h2><p>This is the beginning of the channel.</p></div>';
  const data = await api(`/api/channels/${channel.id}/messages`);
  if (state.channel?.id !== channel.id) return;  // a later selection already owns the view
  data.messages.forEach(message => {
    if (!state.unreadBoundary && message.id > marker && message.author_id !== state.user?.id) appendUnreadDivider();
    appendMessage(message);
  });
  scrollMessages(); readActiveChannel();
}

function messageActions(message) {
  const authored = message.author_id === state.user?.id;
  if (!authored && !isOwner(state.user?.id)) return '';
  const edit = authored && !message.attachment ? `<button type="button" data-edit-message title="Edit message">Edit</button>` : '';
  return `<div class="message-actions">${edit}<button type="button" data-delete-message title="Delete message">Delete</button></div>`;
}

function messageMarkup(message) {
  const date = new Date(message.created_at);
  const attachment = message.attachment ? `<a class="message-attachment" href="/api/attachments/${Number(message.attachment.id)}" target="_blank" rel="noopener"><img src="/api/attachments/${Number(message.attachment.id)}" alt="${escapeHTML(message.attachment.name)}" loading="lazy"><span>${escapeHTML(message.attachment.name)} · ${formatBytes(message.attachment.byte_size)}</span></a>` : '';
  const edited = message.edited_at ? `<span class="message-edited" title="Edited ${escapeHTML(new Date(message.edited_at).toLocaleString())}">(edited)</span>` : '';
  return `<div class="message-avatar">${escapeHTML(initials(message.username))}</div><div><div class="message-meta"><strong>${escapeHTML(message.username)}</strong>${isOwner(message.author_id)?'<span class="owner-indicator">OWNER</span>':''}<time>${date.toLocaleString([], {dateStyle:'medium',timeStyle:'short'})}</time>${edited}</div><div class="message-body">${escapeHTML(message.body)}</div>${attachment}</div>${messageActions(message)}`;
}

function renderInto(row, message) {
  state.messages.set(Number(message.id), message);
  row.innerHTML = messageMarkup(message);
  row.querySelector('[data-edit-message]')?.addEventListener('click', () => startEdit(message));
  row.querySelector('[data-delete-message]')?.addEventListener('click', () => deleteMessage(message));
}

function appendMessage(message) {
  const row = document.createElement('article'); row.className = 'message-row'; row.dataset.messageId = message.id; row.dataset.authorId = message.author_id;
  renderInto(row, message);
  $('#messages').append(row); scrollMessages();
}

function replaceMessage(message) { const row=document.querySelector(`[data-message-id="${Number(message.id)}"]`); if(row) renderInto(row, message); }
function removeMessage(messageId) { document.querySelector(`[data-message-id="${Number(messageId)}"]`)?.remove(); state.messages.delete(Number(messageId)); }

function startEdit(message) {
  const row = document.querySelector(`[data-message-id="${Number(message.id)}"]`); if (!row) return;
  const container = row.querySelector('.message-body');
  container.innerHTML = `<form class="message-edit"><textarea rows="2" maxlength="4000"></textarea><div class="message-edit-actions"><button type="submit" class="primary">Save</button><button type="button" data-cancel-edit>Cancel</button></div></form>`;
  const field = container.querySelector('textarea'); field.value = message.body; field.focus(); field.setSelectionRange(field.value.length, field.value.length);
  const cancel = () => replaceMessage(message);
  container.querySelector('[data-cancel-edit]').onclick = cancel;
  field.onkeydown = event => { if (event.key === 'Escape') cancel(); if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); container.querySelector('form').requestSubmit(); } };
  container.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const body = field.value.trim(); if (!body) return;
    if (body === message.body) return cancel();
    try { replaceMessage(await api(`/api/messages/${Number(message.id)}`, { method:'PATCH', body:JSON.stringify({ body }) })); }
    catch (error) { alert(error.message); cancel(); }
  };
}

async function deleteMessage(message) {
  if (!confirm(message.attachment ? 'Delete this image? The file is removed from the server.' : 'Delete this message?')) return;
  try { await api(`/api/messages/${Number(message.id)}`, { method:'DELETE' }); removeMessage(message.id); }
  catch (error) { alert(error.message); }
}
function scrollMessages() { const list = $('#messages'); list.scrollTop = list.scrollHeight; }
function formatBytes(value) { const bytes=Number(value)||0; return bytes < 1024*1024 ? `${Math.ceil(bytes/1024)} KB` : `${(bytes/1024/1024).toFixed(1)} MB`; }

function setAuthMode(register) { registering = register; $('#auth-submit').textContent = registering ? 'Create account' : 'Sign in'; $('#auth-toggle').textContent = registering ? 'Already have an account? Sign in' : 'New here? Create an account'; $('#password').autocomplete = registering ? 'new-password' : 'current-password'; $('#invite-label').classList.toggle('hidden', !registering); }
$('#auth-toggle').onclick = () => setAuthMode(!registering);
$('#auth-form').onsubmit = async (event) => { event.preventDefault(); $('#auth-error').textContent = ''; try { await api(registering ? '/api/register' : '/api/login', { method:'POST', body:JSON.stringify({username:$('#username').value,password:$('#password').value,invite:$('#invite').value}) }); await enterApp(); } catch (error) { $('#auth-error').textContent = error.message; } };
$('#message-form').onsubmit = sendMessage;
$('#message').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendMessage(event); } };
async function sendMessage(event) { event.preventDefault(); const input=$('#message'), body=input.value.trim(); if(!body || !state.channel)return; input.value=''; try { const message=await api(`/api/channels/${state.channel.id}/messages`,{method:'POST',body:JSON.stringify({body})}); if(!document.querySelector(`[data-message-id="${message.id}"]`))appendMessage(message); readActiveChannel(); } catch(error){ input.value=body; alert(error.message); } }
$('#upload-button').onclick = () => state.channel && $('#file-input').click();
$('#toggle-members').onclick = () => { const compact=window.matchMedia('(max-width: 1100px)').matches; if(compact) $('#members-panel').classList.toggle('open'); else $('#app').classList.toggle('members-hidden'); const visible=compact?$('#members-panel').classList.contains('open'):!$('#app').classList.contains('members-hidden'); $('#toggle-members').setAttribute('aria-expanded',String(visible)); };
$('#close-members').onclick = () => { $('#members-panel').classList.remove('open'); $('#toggle-members').setAttribute('aria-expanded','false'); };
$('#file-input').onchange = async event => { const file=event.target.files[0]; event.target.value=''; if(!file || !state.channel)return; if(file.size > 8*1024*1024){alert('Images must be 8 MB or smaller.');return;} const button=$('#upload-button'); button.disabled=true; button.textContent='…'; try { const message=await api(`/api/channels/${state.channel.id}/uploads`,{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Campfire-Filename':encodeURIComponent(file.name)},body:file}); if(!document.querySelector(`[data-message-id="${message.id}"]`))appendMessage(message); } catch(error){alert(error.message);} finally {button.disabled=false;button.textContent='+';} };
$('#logout').onclick = async () => { state.eventSource?.close(); await api('/api/logout',{method:'POST'}); showAuth(); };
$('#add-community').onclick = async () => { const name=prompt('Community name'); if(!name)return; try { const community=await api('/api/communities',{method:'POST',body:JSON.stringify({name})}); state.communities.push(community); selectCommunity(community); } catch(error){alert(error.message);} };
$('#join-community').onclick = async () => { const invite=prompt('Paste the invite code'); if(!invite)return; try { await api('/api/invites/join',{method:'POST',body:JSON.stringify({invite})}); await enterApp(); } catch(error){alert(error.message);} };
$('#invite-friend').onclick = async () => { if(!state.community)return; $('#invite-dialog').showModal(); await loadInvites(); };
$('#close-invites').onclick = () => $('#invite-dialog').close();
$('#create-invite').onclick = async () => { if(!state.community)return; const button=$('#create-invite'); button.disabled=true; try { const data=await api('/api/invites',{method:'POST',body:JSON.stringify({community_id:state.community.id,max_uses:10,lifetime_hours:24})}); if(navigator.clipboard && window.isSecureContext){await navigator.clipboard.writeText(data.token); alert('Invite code copied. It expires in 24 hours.');}else{prompt('Copy this invite code. It expires in 24 hours.',data.token);} await loadInvites(); } catch(error){alert(error.message);} finally {button.disabled=false;} };
async function loadInvites() { const communityId=state.community?.id; if(!communityId)return; $('#invite-list').innerHTML='<p class="member-empty">Loading invites…</p>'; try { const data=await api(`/api/communities/${communityId}/invites`); if(state.community?.id!==communityId)return; $('#invite-list').innerHTML=data.invites.length?data.invites.map(invite=>`<article class="invite-row"><div><strong>Invite #${Number(invite.id)}</strong><span>Created by ${escapeHTML(invite.creator_username)} · ${Number(invite.uses)}/${Number(invite.max_uses)} uses</span><span>Expires ${new Date(Number(invite.expires_at)*1000).toLocaleString()}</span></div><button type="button" data-revoke-invite="${Number(invite.id)}">Revoke</button></article>`).join(''):'<p class="member-empty">No active invites.</p>'; document.querySelectorAll('[data-revoke-invite]').forEach(button=>button.onclick=()=>revokeInvite(Number(button.dataset.revokeInvite))); } catch(error){ $('#invite-list').innerHTML=`<p class="member-empty">${escapeHTML(error.message)}</p>`; } }
async function revokeInvite(inviteId) { if(!confirm(`Revoke invite #${inviteId}? Every copy will stop working immediately.`))return; try { await api(`/api/invites/${inviteId}`,{method:'DELETE'}); await loadInvites(); } catch(error){alert(error.message);} }
$('#add-channel').onclick = async () => { if(!state.community)return; const name=prompt('Channel name'); if(!name)return; try { const channel=await api('/api/channels',{method:'POST',body:JSON.stringify({name,community_id:state.community.id})}); state.community.channels.push(channel); renderChannels(); selectChannel(channel); } catch(error){alert(error.message);} };

$('#notify-settings').onclick = () => { $('#notify-dialog').showModal(); renderNotificationSettings(); };
$('#close-notify').onclick = () => $('#notify-dialog').close();
$('#notify-default').onchange = event => setDefaultNotifications(event.target.value);
$('#enable-notifications').onclick = async () => { await requestNotificationPermission(); renderNotificationSettings(); };
// Coming back to the tab is the moment the messages on screen have been seen.
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') readActiveChannel(); });

function renderNotificationSettings() {
  $('#notify-default').value = state.defaultMode;
  renderNotificationPermission(); renderNotificationBell();
  const channels = state.community?.channels || [];
  $('#notify-channels').innerHTML = channels.length ? channels.map(channel => `<article class="invite-row"><div><strong>#${escapeHTML(channel.name)}</strong><span>${channelState(channel.id).unread} unread</span></div><select data-channel-notify="${Number(channel.id)}" aria-label="Notifications for ${escapeHTML(channel.name)}"><option value="default">Use the default</option><option value="all">Notify me</option><option value="none">Mute</option></select></article>`).join('') : '<p class="member-empty">No channels yet.</p>';
  document.querySelectorAll('[data-channel-notify]').forEach(select => {
    select.value = channelState(select.dataset.channelNotify).notify || 'default';
    select.onchange = () => setChannelNotifications(Number(select.dataset.channelNotify), select.value);
  });
}

async function setDefaultNotifications(mode) {
  try { const data = await api('/api/preferences/notifications', { method: 'PATCH', body: JSON.stringify({ default_mode: mode }) }); state.defaultMode = data.default_mode; renderChannels(); renderCommunities(); renderNotificationBell(); }
  catch (error) { alert(error.message); }
  if (mode === 'all') await requestNotificationPermission();
  renderNotificationSettings();
}

async function setChannelNotifications(channelId, mode) {
  try { applyChannelState(await api(`/api/channels/${channelId}/notifications`, { method: 'PATCH', body: JSON.stringify({ mode }) })); }
  catch (error) { alert(error.message); }
  if (mode === 'all') await requestNotificationPermission();
  renderNotificationSettings();
}

// Asked for only when somebody turns notifications on, never on load: a prompt
// nobody invited is how people learn to refuse without reading.
async function requestNotificationPermission() {
  if (!('Notification' in window) || Notification.permission !== 'default') return;
  try { await Notification.requestPermission(); } catch { /* a refusal is an answer */ }
}

function renderNotificationPermission() {
  const permission = 'Notification' in window ? Notification.permission : 'unsupported';
  $('#notify-permission').textContent = {
    granted: 'This browser will show desktop notifications.',
    denied: 'This browser is blocking desktop notifications; undo that in its site settings. Unread markers still work.',
    default: 'This browser has not been asked yet, so nothing can pop up until you allow it.',
    unsupported: 'This browser cannot show desktop notifications. Unread markers still work.',
  }[permission];
  // Only an unasked browser can still be asked; a denial has to be undone in
  // the browser's own settings, and asking again would do nothing.
  $('#enable-notifications').classList.toggle('hidden', permission !== 'default');
}

// Notifications are on by default, so a browser that has never been asked would
// otherwise stay silent with nothing on screen explaining why.
function renderNotificationBell() {
  const wanted = state.defaultMode === 'all' || [...state.unread.values()].some(entry => entry.notify === 'all');
  const asleep = wanted && 'Notification' in window && Notification.permission !== 'granted';
  $('#notify-settings').classList.toggle('needs-permission', asleep);
  $('#notify-settings').title = asleep
    ? 'Notification settings — this browser is not allowed to show them yet'
    : 'Notification settings';
}

api('/api/me').then(data => data.user ? enterApp() : showAuth()).catch(showAuth);
