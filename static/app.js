const $ = (s) => document.querySelector(s);
const state = { user: null, communities: [], community: null, channel: null, members: [], messages: new Map(), eventSource: null };
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
  $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden');
  $('#current-user').textContent = state.user.username;
  $('#avatar').textContent = initials(state.user.username);
  selectCommunity(state.communities[0]);
  state.eventSource?.close(); state.eventSource = new EventSource('/api/events');
  state.eventSource.onmessage = ({ data }) => {
    const event = JSON.parse(data);
    if (event.channel_id !== state.channel?.id) return;
    if (event.type === 'message.deleted') return removeMessage(event.id);
    if (event.type === 'message.updated') return replaceMessage(event);
    if (!document.querySelector(`[data-message-id="${Number(event.id)}"]`)) appendMessage(event);
  };
}

function renderCommunities() {
  $('#community-icons').innerHTML = state.communities.map(c => `<button class="community-icon ${c.id === state.community?.id ? 'active' : ''}" data-id="${c.id}" title="${escapeHTML(c.name)}">${escapeHTML(initials(c.name))}</button>`).join('');
  document.querySelectorAll('.community-icon').forEach(button => button.onclick = () => selectCommunity(state.communities.find(c => c.id === +button.dataset.id)));
}

function selectCommunity(community) {
  if ($('#invite-dialog').open) $('#invite-dialog').close();
  state.community = community; $('#community-name').textContent = community?.name || 'Campfire'; renderCommunities();
  state.members = []; renderMembers();
  renderChannels();
  selectChannel(community?.channels[0]);
  if (community) loadMembers(community.id);
}

function renderChannels() {
  const community = state.community;
  $('#channels').innerHTML = (community?.channels || []).map(c => `<button class="channel" data-id="${c.id}">${escapeHTML(c.name)}</button>`).join('');
  document.querySelectorAll('.channel').forEach(button => button.onclick = () => selectChannel(community.channels.find(c => c.id === +button.dataset.id)));
}

async function loadMembers(communityId) {
  try { const data=await api(`/api/communities/${communityId}/members`); if(state.community?.id!==communityId)return; state.members=data.members; renderMembers(); refreshMessages(); }
  catch(error) { if(state.community?.id===communityId) $('#member-list').innerHTML=`<p class="member-empty">${escapeHTML(error.message)}</p>`; }
}

function renderMembers() {
  $('#member-count').textContent = state.members.length;
  $('#invite-friend').classList.toggle('hidden', !isOwner(state.user?.id));
  $('#member-list').innerHTML = state.members.length ? state.members.map(member => `<article class="member-row"><div class="member-avatar">${escapeHTML(initials(member.username))}</div><div class="member-identity"><strong>${escapeHTML(member.username)}</strong>${member.role==='owner'?'<span class="member-role">OWNER</span>':''}</div></article>`).join('') : '<p class="member-empty">Loading members…</p>';
}

function isOwner(userId) { return state.members.some(member => member.id===Number(userId) && member.role==='owner'); }
// Owner badges and delete rights both depend on the member list, which arrives after the first messages.
function refreshMessages() { document.querySelectorAll('.message-row').forEach(row => { const message=state.messages.get(Number(row.dataset.messageId)); if(message) renderInto(row, message); }); }

async function selectChannel(channel) {
  state.channel = channel;
  if (!channel) return;
  $('#channel-name').textContent = channel.name; $('#message').placeholder = `Message #${channel.name}`;
  document.querySelectorAll('.channel').forEach(b => b.classList.toggle('active', +b.dataset.id === channel.id));
  // Clear before awaiting so the previous channel's messages never sit under the new channel's name.
  state.messages.clear();
  $('#messages').innerHTML = '<div class="empty"><div>#</div><h2>Welcome to #' + escapeHTML(channel.name) + '</h2><p>This is the beginning of the channel.</p></div>';
  const data = await api(`/api/channels/${channel.id}/messages`);
  if (state.channel?.id !== channel.id) return;  // a later selection already owns the view
  data.messages.forEach(appendMessage); scrollMessages();
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
async function sendMessage(event) { event.preventDefault(); const input=$('#message'), body=input.value.trim(); if(!body || !state.channel)return; input.value=''; try { const message=await api(`/api/channels/${state.channel.id}/messages`,{method:'POST',body:JSON.stringify({body})}); if(!document.querySelector(`[data-message-id="${message.id}"]`))appendMessage(message); } catch(error){ input.value=body; alert(error.message); } }
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

api('/api/me').then(data => data.user ? enterApp() : showAuth()).catch(showAuth);
