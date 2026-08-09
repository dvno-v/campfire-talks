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
function showAuth() {
  state.eventSource?.close(); state.eventSource = null; state.user = null;
  setNavOpen(false); $('#members-panel').classList.remove('open');
  if ($('#account-dialog').open) $('#account-dialog').close();
  $('#password-form').reset(); $('#delete-account-form').reset();
  $('#auth').classList.remove('hidden'); $('#app').classList.add('hidden');
}

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
  // EventSource retries forever after a remote revocation. Check the ordinary
  // auth endpoint so a revoked browser returns to sign-in instead of appearing
  // silently frozen.
  state.eventSource.onerror = async () => {
    try { if (!(await api('/api/me')).user) showAuth(); } catch { /* a transient outage should keep retrying */ }
  };
  state.eventSource.onmessage = ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === 'stream.reset') { refreshUnread(); return resyncChannel(); }
    if (event.type.startsWith('presence.')) return applyPresence(event);
    if (event.type === 'member.joined') return applyMemberJoined(event);
    if (event.type === 'member.updated') return applyMemberUpdated(event);
    if (event.type === 'member.removed') return applyMemberRemoved(event);
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
  if ($('#ban-dialog').open) $('#ban-dialog').close();
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
  // Picking a channel is what the drawer was opened for, so it gets out of the
  // way. Picking a community does not: the channel list is the next choice.
  document.querySelectorAll('.channel').forEach(button => button.onclick = () => { selectChannel(community.channels.find(c => c.id === +button.dataset.id)); closeNavOnDrawer(); });
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
  closeNavOnDrawer();
}

async function loadMembers(communityId) {
  try { const data=await api(`/api/communities/${communityId}/members`); if(state.community?.id!==communityId)return; state.members=data.members; state.online=new Set(data.members.filter(member=>member.online).map(member=>member.id)); renderMembers(); refreshMessages(); }
  catch(error) { if(state.community?.id===communityId) $('#member-list').innerHTML=`<p class="member-empty">${escapeHTML(error.message)}</p>`; }
}

// ---------- Per-row actions menu ----------
// Messages and members both carry actions only some people may take. One
// trigger per row opens them on demand; a hidden menu per row would be a lot
// of DOM for something only ever open in one place at a time, so the popup is
// a single shared element that is filled in as it opens.
const MENU_ICON = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><circle cx="8" cy="3" r="1.6"/><circle cx="8" cy="8" r="1.6"/><circle cx="8" cy="13" r="1.6"/></svg>';
let openMenuTrigger = null;

function rowMenuTrigger(attribute, label) {
  return `<div class="row-menu"><button class="row-menu-trigger" type="button" ${attribute} aria-haspopup="true" aria-expanded="false" aria-label="${escapeHTML(label)}">${MENU_ICON}</button></div>`;
}

function closeRowMenu({ refocus = false } = {}) {
  const menu = $('#row-menu');
  menu.classList.remove('open');
  menu.innerHTML = '';
  if (openMenuTrigger) {
    openMenuTrigger.setAttribute('aria-expanded', 'false');
    if (refocus && openMenuTrigger.isConnected) openMenuTrigger.focus();
  }
  openMenuTrigger = null;
}

function openRowMenu(trigger, items) {
  const reopening = openMenuTrigger === trigger;
  closeRowMenu();
  if (reopening) return;  // a second press on the same row closes it again
  const menu = $('#row-menu');
  menu.innerHTML = items.map((item, index) =>
    `<button type="button" role="menuitem" data-index="${index}"${item.danger ? ' class="danger-item"' : ''}>${escapeHTML(item.label)}</button>`).join('');
  menu.querySelectorAll('button').forEach(button => button.onclick = () => {
    const { action } = items[Number(button.dataset.index)];
    closeRowMenu();
    action();
  });
  menu.classList.add('open');
  openMenuTrigger = trigger;
  trigger.setAttribute('aria-expanded', 'true');
  placeRowMenu(trigger, menu);
  // `preventScroll`: the popup is already placed against the viewport, and
  // letting focus scroll an ancestor to reveal it would fire the scroll
  // handler below and close what was just opened.
  menu.querySelector('button')?.focus({ preventScroll: true });
}

function placeRowMenu(trigger, menu) {
  const anchor = trigger.getBoundingClientRect(), size = menu.getBoundingClientRect(), margin = 8;
  // Below and right-aligned to the trigger by preference, flipped above when
  // that would run off the bottom, and never past either side edge.
  const left = Math.min(Math.max(margin, anchor.right - size.width), window.innerWidth - size.width - margin);
  const below = anchor.bottom + 6;
  const fits = below + size.height + margin <= window.innerHeight;
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(fits ? below : Math.max(margin, anchor.top - size.height - 6))}px`;
}

// A re-render replaces the row the open menu belongs to, which takes its
// trigger — and the thing the menu was about — out of the document.
function dropDetachedRowMenu() { if (openMenuTrigger && !openMenuTrigger.isConnected) closeRowMenu(); }

document.addEventListener('pointerdown', event => {
  if (!openMenuTrigger || $('#row-menu').contains(event.target) || openMenuTrigger.contains(event.target)) return;
  closeRowMenu();
});
// The popup is placed against the viewport, so anything that moves the row
// beneath it invalidates that placement. Scrolling takes the row away, so the
// menu goes with it; a resize only changes where it belongs — and on a phone
// every hide of the address bar is a resize, so closing on that would snatch
// the menu away mid-reach.
window.addEventListener('scroll', () => closeRowMenu(), true);
window.addEventListener('resize', () => { if (openMenuTrigger) placeRowMenu(openMenuTrigger, $('#row-menu')); });

function memberRoleControl(member) {
  if (member.role === 'owner') return '<span class="member-role">OWNER</span>';
  if (currentCommunityRole() !== 'owner') return member.role === 'member' ? '' : `<span class="member-role">${escapeHTML(member.role.toUpperCase())}</span>`;
  return `<select class="member-role-select" data-member-role="${Number(member.id)}" aria-label="Role for ${escapeHTML(member.username)}"><option value="administrator">Administrator</option><option value="moderator">Moderator</option><option value="member">Member</option></select>`;
}

function memberRow(member) {
  const actions = canModerateMember(member)
    ? rowMenuTrigger(`data-member-menu="${Number(member.id)}"`, `Actions for ${member.username}`) : '';
  return `<article class="member-row ${state.online.has(member.id) ? 'online' : 'offline'}"><div class="member-avatar">${escapeHTML(initials(member.username))}<span class="presence-dot" title="${state.online.has(member.id) ? 'Online' : 'Offline'}"></span></div><div class="member-identity"><strong>${escapeHTML(member.username)}</strong>${memberRoleControl(member)}</div>${actions}</article>`;
}

function renderMembers() {
  $('#member-count').textContent = state.members.length;
  const manages = ['owner', 'administrator'].includes(currentCommunityRole());
  $('#invite-friend').classList.toggle('hidden', !manages);
  $('#add-channel').classList.toggle('hidden', !manages);
  $('#manage-bans').classList.toggle('hidden', roleRank(currentCommunityRole()) < roleRank('moderator'));
  if (!state.members.length) { $('#member-list').innerHTML = '<p class="member-empty">Loading members…</p>'; return; }
  const groups = [['ONLINE', state.members.filter(m => state.online.has(m.id))], ['OFFLINE', state.members.filter(m => !state.online.has(m.id))]];
  $('#member-list').innerHTML = groups.filter(([, members]) => members.length)
    .map(([label, members]) => `<div class="member-group">${label} — ${members.length}</div>${members.map(memberRow).join('')}`).join('');
  document.querySelectorAll('[data-member-role]').forEach(select => {
    const member = state.members.find(entry => entry.id === Number(select.dataset.memberRole));
    select.value = member.role;
    select.onchange = () => { select.disabled = true; updateMemberRole(member, select.value); };
  });
  document.querySelectorAll('[data-member-menu]').forEach(button => {
    const member = state.members.find(entry => entry.id === Number(button.dataset.memberMenu));
    button.onclick = () => openRowMenu(button, [
      { label: 'Kick', action: () => moderateMember(member, false) },
      { label: 'Ban', danger: true, action: () => moderateMember(member, true) },
    ]);
  });
  dropDetachedRowMenu();
}

function applyPresence(event) {
  const userId = Number(event.user_id);
  if (event.type === 'presence.online') state.online.add(userId); else state.online.delete(userId);
  if (state.members.some(member => member.id === userId)) renderMembers();
}

// Privileged roles first, then by name, matching the order the server returns.
function sortMembers(members) {
  const rank = { owner: 0, administrator: 1, moderator: 2, member: 3 };
  return members.sort((a, b) => rank[a.role] - rank[b.role]
    || a.username.localeCompare(b.username, undefined, { sensitivity: 'base' }));
}

function applyMemberJoined(event) {
  if (event.community_id !== state.community?.id) return;
  if (state.members.some(member => member.id === event.member.id)) return;
  state.members = sortMembers([...state.members, event.member]);
  renderMembers();
}

function applyMemberUpdated(event) {
  const community = state.communities.find(entry => entry.id === Number(event.community_id));
  if (community && event.member.id === state.user?.id) community.role = event.member.role;
  if (event.community_id !== state.community?.id) return;
  const existing = state.members.find(member => member.id === event.member.id);
  if (!existing) return loadMembers(event.community_id);
  Object.assign(existing, event.member);
  state.members = sortMembers(state.members);
  renderMembers(); refreshMessages();
}

function applyMemberRemoved(event) {
  if (Number(event.user_id) === state.user?.id) return enterApp();
  if (event.community_id !== state.community?.id) return;
  state.members = state.members.filter(member => member.id !== Number(event.user_id));
  state.online.delete(Number(event.user_id));
  renderMembers();
  // A deleted account takes its messages with it, so the open channel is stale
  // in a way re-rendering the badges cannot fix.
  if (event.deleted_account) { resyncChannel(); refreshUnread(); } else refreshMessages();
  if (event.banned && $('#ban-dialog').open) loadBans();
}

async function updateMemberRole(member, role) {
  try {
    const updated = await api(`/api/communities/${state.community.id}/members/${Number(member.id)}`, {
      method: 'PATCH', body: JSON.stringify({ role }) });
    applyMemberUpdated({ type: 'member.updated', community_id: state.community.id, member: updated });
  } catch (error) { alert(error.message); renderMembers(); }
}

async function moderateMember(member, banned) {
  const consequence = banned ? ' They will not be able to rejoin until unbanned.' : '';
  if (!confirm(`${banned ? 'Ban' : 'Kick'} ${member.username}?${consequence}`)) return;
  try {
    const path = `/api/communities/${state.community.id}/members/${Number(member.id)}${banned ? '/ban' : ''}`;
    await api(path, { method: banned ? 'POST' : 'DELETE', ...(banned ? { body: '{}' } : {}) });
    applyMemberRemoved({ type: 'member.removed', community_id: state.community.id,
      user_id: member.id, banned });
  } catch (error) { alert(error.message); }
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

function currentCommunityRole() { return state.community?.role || state.members.find(member => member.id === state.user?.id)?.role || 'member'; }
function roleRank(role) { return { member: 0, moderator: 1, administrator: 2, owner: 3 }[role] ?? 0; }
function canModerateRole(role) { return roleRank(currentCommunityRole()) >= roleRank('moderator') && roleRank(currentCommunityRole()) > roleRank(role); }
function canModerateMember(member) { return member.id !== state.user?.id && canModerateRole(member.role); }
function mayModerate() { return ['owner', 'administrator', 'moderator'].includes(currentCommunityRole()); }
function roleBadge(userId) { const role=state.members.find(member=>member.id===Number(userId))?.role; return role && role!=='member' ? `<span class="owner-indicator">${escapeHTML(role.toUpperCase())}</span>` : ''; }
// Author role badges depend on the member list, which arrives after the first messages.
function refreshMessages() { document.querySelectorAll('.message-row').forEach(row => { const message=state.messages.get(Number(row.dataset.messageId)); if(message) renderInto(row, message); }); dropDetachedRowMenu(); }

async function selectChannel(channel) {
  state.channel = channel; state.unreadBoundary = false;
  if (!channel) {
    $('#channel-name').textContent = '';
    $('#message').placeholder = 'Choose or create a community'; $('#message').disabled = true;
    $('#upload-button').disabled = true;
    state.messages.clear(); $('#messages').innerHTML = '<div class="empty"><div>C</div><h2>No community selected</h2><p>Create one or join with an invite.</p></div>';
    return;
  }
  $('#message').disabled = false; $('#upload-button').disabled = false;
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

// Editing is the author's alone; deleting is the author's or a moderator's.
function messageMenuItems(message) {
  const authored = message.author_id === state.user?.id;
  if (!authored && !mayModerate()) return [];
  const items = [];
  // A shared image is replaced by deleting it, never rewritten in place.
  if (authored && !message.attachment) items.push({ label: 'Edit', action: () => startEdit(message) });
  items.push({ label: 'Delete', danger: true, action: () => deleteMessage(message) });
  return items;
}

function messageActions(message) {
  return messageMenuItems(message).length
    ? rowMenuTrigger('data-message-menu', 'Message actions') : '';
}

function messageMarkup(message) {
  const date = new Date(message.created_at);
  const attachment = message.attachment ? `<a class="message-attachment" href="/api/attachments/${Number(message.attachment.id)}" target="_blank" rel="noopener"><img src="/api/attachments/${Number(message.attachment.id)}" alt="${escapeHTML(message.attachment.name)}" loading="lazy"><span>${escapeHTML(message.attachment.name)} · ${formatBytes(message.attachment.byte_size)}</span></a>` : '';
  const edited = message.edited_at ? `<span class="message-edited" title="Edited ${escapeHTML(new Date(message.edited_at).toLocaleString())}">(edited)</span>` : '';
  return `<div class="message-avatar">${escapeHTML(initials(message.username))}</div><div><div class="message-meta"><strong>${escapeHTML(message.username)}</strong>${roleBadge(message.author_id)}<time>${date.toLocaleString([], {dateStyle:'medium',timeStyle:'short'})}</time>${edited}</div><div class="message-body">${escapeHTML(message.body)}</div>${attachment}</div>${messageActions(message)}`;
}

function renderInto(row, message) {
  state.messages.set(Number(message.id), message);
  row.innerHTML = messageMarkup(message);
  const trigger = row.querySelector('[data-message-menu]');
  // Only a row with something to offer reserves the gutter the trigger sits in.
  row.classList.toggle('has-actions', Boolean(trigger));
  trigger?.addEventListener('click', () => openRowMenu(trigger, messageMenuItems(message)));
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
$('#toggle-members').onclick = () => { const compact=window.matchMedia('(max-width: 1100px)').matches; if(compact){ setNavOpen(false); $('#members-panel').classList.toggle('open'); } else $('#app').classList.toggle('members-hidden'); const visible=compact?$('#members-panel').classList.contains('open'):!$('#app').classList.contains('members-hidden'); $('#toggle-members').setAttribute('aria-expanded',String(visible)); };
$('#close-members').onclick = () => { $('#members-panel').classList.remove('open'); $('#toggle-members').setAttribute('aria-expanded','false'); };

// Below 900px the community rail and channel list slide over the conversation.
// Everything they carry — communities, channels, invites, bans, notifications,
// account settings, sign-out — is only reachable through here on a phone.
function navIsDrawer() { return window.matchMedia('(max-width: 760px)').matches; }
function setNavOpen(open) {
  // The scrim's visibility is the class's job, not an attribute's: `hidden`
  // would cut the fade short, and CSS `display` overrides it anyway.
  $('#app').classList.toggle('nav-open', open);
  $('#toggle-nav').setAttribute('aria-expanded', String(open));
}
function closeNavOnDrawer() { if (navIsDrawer()) setNavOpen(false); }
$('#toggle-nav').onclick = () => { $('#members-panel').classList.remove('open'); setNavOpen(!$('#app').classList.contains('nav-open')); };
$('#nav-scrim').onclick = () => setNavOpen(false);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    // Innermost first: a menu opened over the drawer closes the menu, not the drawer.
    if (openMenuTrigger) return closeRowMenu({ refocus: true });
    setNavOpen(false); $('#members-panel').classList.remove('open');
    return;
  }
  if (!openMenuTrigger || (event.key !== 'ArrowDown' && event.key !== 'ArrowUp')) return;
  event.preventDefault();
  const items = [...$('#row-menu').querySelectorAll('button')];
  const next = items.indexOf(document.activeElement) + (event.key === 'ArrowDown' ? 1 : -1);
  items[(next + items.length) % items.length]?.focus();
});
// A drawer parked off-screen must not keep a resize's worth of stale state.
window.addEventListener('resize', () => { if (!navIsDrawer()) setNavOpen(false); });
$('#file-input').onchange = async event => { const file=event.target.files[0]; event.target.value=''; if(!file || !state.channel)return; if(file.size > 8*1024*1024){alert('Images must be 8 MB or smaller.');return;} const button=$('#upload-button'); button.disabled=true; button.textContent='…'; try { const message=await api(`/api/channels/${state.channel.id}/uploads`,{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Campfire-Filename':encodeURIComponent(file.name)},body:file}); if(!document.querySelector(`[data-message-id="${message.id}"]`))appendMessage(message); } catch(error){alert(error.message);} finally {button.disabled=false;button.textContent='+';} };
$('#logout').onclick = async () => { state.eventSource?.close(); await api('/api/logout',{method:'POST'}); showAuth(); };
$('#account-settings').onclick = async () => { closeNavOnDrawer(); $('#account-dialog').showModal(); $('#password-status').classList.remove('error'); $('#password-status').textContent=''; $('#delete-status').textContent=''; $('#delete-status').classList.remove('error'); $('#delete-account-form').reset(); await Promise.all([loadSessions(), loadDeletionPlan()]); };
$('#close-account').onclick = () => { $('#password-form').reset(); $('#delete-account-form').reset(); $('#account-dialog').close(); };
$('#password-form').onsubmit = async event => {
  event.preventDefault();
  const status=$('#password-status'), current=$('#current-password').value, next=$('#new-password').value;
  status.classList.remove('error'); status.textContent='';
  if(next!==$('#confirm-password').value){status.classList.add('error');status.textContent='The new passwords do not match.';return;}
  const button=event.currentTarget.querySelector('button[type="submit"]'); button.disabled=true;
  try {
    const data=await api('/api/account/password',{method:'PATCH',body:JSON.stringify({current_password:current,new_password:next})});
    event.currentTarget.reset(); status.textContent=`Password changed. ${Number(data.revoked_sessions)} other session${Number(data.revoked_sessions)===1?' was':'s were'} signed out.`; await loadSessions();
  } catch(error){status.classList.add('error');status.textContent=error.message;}
  finally{button.disabled=false;}
};
async function loadSessions() {
  const list=$('#session-list'); list.innerHTML='<p class="member-empty">Loading sessions…</p>';
  try {
    const data=await api('/api/sessions');
    list.innerHTML=data.sessions.length?data.sessions.map(session=>`<article class="invite-row"><div><strong>Session #${Number(session.id)}</strong><span${session.current?' class="session-current"':''}>${session.current?'This session':'Another signed-in session'}</span><span>Started ${new Date(session.created_at).toLocaleString()}</span><span>Expires ${new Date(Number(session.expires_at)*1000).toLocaleString()}</span></div>${session.current?'':`<button type="button" data-revoke-session="${Number(session.id)}">Sign out</button>`}</article>`).join(''):'<p class="member-empty">No active sessions.</p>';
    document.querySelectorAll('[data-revoke-session]').forEach(button=>button.onclick=()=>revokeSession(Number(button.dataset.revokeSession)));
  } catch(error){list.innerHTML=`<p class="member-empty">${escapeHTML(error.message)}</p>`;}
}
async function revokeSession(sessionId) {
  if(!confirm('Sign out that session? Its live connection will close within a couple of seconds.'))return;
  try{await api(`/api/sessions/${sessionId}`,{method:'DELETE'});await loadSessions();}catch(error){alert(error.message);}
}
// Ownership moving to someone else, or a community disappearing entirely, are
// consequences worth reading before the password box, not after the deletion.
async function loadDeletionPlan() {
  const summary=$('#deletion-plan'); summary.classList.remove('error'); summary.textContent='Checking what deletion would affect…';
  try {
    const plan=await api('/api/account/deletion');
    const lines=[`${Number(plan.messages)} message${Number(plan.messages)===1?'':'s'} and ${Number(plan.attachments)} image${Number(plan.attachments)===1?'':'s'} will be erased.`];
    plan.communities_transferred.forEach(community=>lines.push(`${community.name} passes to ${community.successor_username}.`));
    plan.communities_dissolved.forEach(community=>lines.push(`${community.name} has no other members and will be deleted with all of its channels and messages.`));
    summary.textContent=lines.join(' ');
  } catch(error){summary.classList.add('error');summary.textContent=error.message;}
}
$('#delete-account-form').onsubmit = async event => {
  event.preventDefault();
  const status=$('#delete-status'); status.classList.remove('error'); status.textContent='';
  if(!confirm('Delete your account permanently? Your messages and images go with it, and this cannot be undone.'))return;
  const button=event.currentTarget.querySelector('button[type="submit"]'); button.disabled=true;
  try {
    await api('/api/account',{method:'DELETE',body:JSON.stringify({current_password:$('#delete-password').value})});
    event.currentTarget.reset(); showAuth();
  } catch(error){status.classList.add('error');status.textContent=error.message;}
  finally{button.disabled=false;}
};
$('#add-community').onclick = async () => { const name=prompt('Community name'); if(!name)return; try { const community=await api('/api/communities',{method:'POST',body:JSON.stringify({name})}); state.communities.push(community); selectCommunity(community); } catch(error){alert(error.message);} };
$('#join-community').onclick = async () => { const invite=prompt('Paste the invite code'); if(!invite)return; try { await api('/api/invites/join',{method:'POST',body:JSON.stringify({invite})}); await enterApp(); } catch(error){alert(error.message);} };
$('#invite-friend').onclick = async () => { if(!state.community)return; closeNavOnDrawer(); $('#invite-dialog').showModal(); await loadInvites(); };
$('#close-invites').onclick = () => $('#invite-dialog').close();
$('#create-invite').onclick = async () => { if(!state.community)return; const button=$('#create-invite'); button.disabled=true; try { const data=await api('/api/invites',{method:'POST',body:JSON.stringify({community_id:state.community.id,max_uses:10,lifetime_hours:24})}); if(navigator.clipboard && window.isSecureContext){await navigator.clipboard.writeText(data.token); alert('Invite code copied. It expires in 24 hours.');}else{prompt('Copy this invite code. It expires in 24 hours.',data.token);} await loadInvites(); } catch(error){alert(error.message);} finally {button.disabled=false;} };
async function loadInvites() { const communityId=state.community?.id; if(!communityId)return; $('#invite-list').innerHTML='<p class="member-empty">Loading invites…</p>'; try { const data=await api(`/api/communities/${communityId}/invites`); if(state.community?.id!==communityId)return; $('#invite-list').innerHTML=data.invites.length?data.invites.map(invite=>`<article class="invite-row"><div><strong>Invite #${Number(invite.id)}</strong><span>Created by ${escapeHTML(invite.creator_username)} · ${Number(invite.uses)}/${Number(invite.max_uses)} uses</span><span>Expires ${new Date(Number(invite.expires_at)*1000).toLocaleString()}</span></div><button type="button" data-revoke-invite="${Number(invite.id)}">Revoke</button></article>`).join(''):'<p class="member-empty">No active invites.</p>'; document.querySelectorAll('[data-revoke-invite]').forEach(button=>button.onclick=()=>revokeInvite(Number(button.dataset.revokeInvite))); } catch(error){ $('#invite-list').innerHTML=`<p class="member-empty">${escapeHTML(error.message)}</p>`; } }
async function revokeInvite(inviteId) { if(!confirm(`Revoke invite #${inviteId}? Every copy will stop working immediately.`))return; try { await api(`/api/invites/${inviteId}`,{method:'DELETE'}); await loadInvites(); } catch(error){alert(error.message);} }
$('#manage-bans').onclick = async () => { if(!state.community)return; closeNavOnDrawer(); $('#ban-dialog').showModal(); await loadBans(); };
$('#close-bans').onclick = () => $('#ban-dialog').close();
async function loadBans() { const communityId=state.community?.id; if(!communityId)return; $('#ban-list').innerHTML='<p class="member-empty">Loading bans…</p>'; try { const data=await api(`/api/communities/${communityId}/bans`); if(state.community?.id!==communityId)return; $('#ban-list').innerHTML=data.bans.length?data.bans.map(ban=>`<article class="invite-row"><div><strong>${escapeHTML(ban.username)}</strong><span>Banned by ${escapeHTML(ban.banned_by_username||'a former moderator')} · ${new Date(ban.created_at).toLocaleString()}</span></div>${canModerateRole(ban.role_at_ban)?`<button type="button" data-unban-member="${Number(ban.user_id)}">Unban</button>`:''}</article>`).join(''):'<p class="member-empty">No banned accounts.</p>'; document.querySelectorAll('[data-unban-member]').forEach(button=>button.onclick=()=>unbanMember(Number(button.dataset.unbanMember))); } catch(error){ $('#ban-list').innerHTML=`<p class="member-empty">${escapeHTML(error.message)}</p>`; } }
async function unbanMember(userId) { if(!confirm('Allow this account to join again?'))return; try { await api(`/api/communities/${state.community.id}/bans/${userId}`,{method:'DELETE'}); await loadBans(); } catch(error){alert(error.message);} }
$('#add-channel').onclick = async () => { if(!state.community)return; const name=prompt('Channel name'); if(!name)return; try { const channel=await api('/api/channels',{method:'POST',body:JSON.stringify({name,community_id:state.community.id})}); state.community.channels.push(channel); renderChannels(); selectChannel(channel); } catch(error){alert(error.message);} };

$('#notify-settings').onclick = () => { closeNavOnDrawer(); $('#notify-dialog').showModal(); renderNotificationSettings(); };
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
