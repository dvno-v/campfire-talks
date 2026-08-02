const $ = (s) => document.querySelector(s);
const state = { user: null, communities: [], community: null, channel: null, eventSource: null };
let registering = false;

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Something went wrong');
  return data;
}

function initials(name) { return name.slice(0, 2).toUpperCase(); }
function escapeHTML(value) { const node = document.createElement('span'); node.textContent = value; return node.innerHTML; }
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
    const message = JSON.parse(data);
    if (message.channel_id === state.channel?.id && !document.querySelector(`[data-message-id="${message.id}"]`)) appendMessage(message);
  };
}

function renderCommunities() {
  $('#community-icons').innerHTML = state.communities.map(c => `<button class="community-icon ${c.id === state.community?.id ? 'active' : ''}" data-id="${c.id}" title="${escapeHTML(c.name)}">${escapeHTML(initials(c.name))}</button>`).join('');
  document.querySelectorAll('.community-icon').forEach(button => button.onclick = () => selectCommunity(state.communities.find(c => c.id === +button.dataset.id)));
}

function selectCommunity(community) {
  state.community = community; $('#community-name').textContent = community?.name || 'Campfire'; renderCommunities();
  $('#channels').innerHTML = (community?.channels || []).map(c => `<button class="channel" data-id="${c.id}">${escapeHTML(c.name)}</button>`).join('');
  document.querySelectorAll('.channel').forEach(button => button.onclick = () => selectChannel(community.channels.find(c => c.id === +button.dataset.id)));
  selectChannel(community?.channels[0]);
}

async function selectChannel(channel) {
  state.channel = channel;
  if (!channel) return;
  $('#channel-name').textContent = channel.name; $('#message').placeholder = `Message #${channel.name}`;
  document.querySelectorAll('.channel').forEach(b => b.classList.toggle('active', +b.dataset.id === channel.id));
  const data = await api(`/api/channels/${channel.id}/messages`);
  $('#messages').innerHTML = '<div class="empty"><div>#</div><h2>Welcome to #' + escapeHTML(channel.name) + '</h2><p>This is the beginning of the channel.</p></div>';
  data.messages.forEach(appendMessage); scrollMessages();
}

function appendMessage(message) {
  const row = document.createElement('article'); row.className = 'message-row'; row.dataset.messageId = message.id;
  const date = new Date(message.created_at);
  const attachment = message.attachment ? `<a class="message-attachment" href="/api/attachments/${Number(message.attachment.id)}" target="_blank" rel="noopener"><img src="/api/attachments/${Number(message.attachment.id)}" alt="${escapeHTML(message.attachment.name)}" loading="lazy"><span>${escapeHTML(message.attachment.name)} · ${formatBytes(message.attachment.byte_size)}</span></a>` : '';
  row.innerHTML = `<div class="message-avatar">${escapeHTML(initials(message.username))}</div><div><div class="message-meta"><strong>${escapeHTML(message.username)}</strong><time>${date.toLocaleString([], {dateStyle:'medium',timeStyle:'short'})}</time></div><div class="message-body">${escapeHTML(message.body)}</div>${attachment}</div>`;
  $('#messages').append(row); scrollMessages();
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
$('#file-input').onchange = async event => { const file=event.target.files[0]; event.target.value=''; if(!file || !state.channel)return; if(file.size > 8*1024*1024){alert('Images must be 8 MB or smaller.');return;} const button=$('#upload-button'); button.disabled=true; button.textContent='…'; try { const message=await api(`/api/channels/${state.channel.id}/uploads`,{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Campfire-Filename':encodeURIComponent(file.name)},body:file}); if(!document.querySelector(`[data-message-id="${message.id}"]`))appendMessage(message); } catch(error){alert(error.message);} finally {button.disabled=false;button.textContent='+';} };
$('#logout').onclick = async () => { state.eventSource?.close(); await api('/api/logout',{method:'POST'}); showAuth(); };
$('#add-community').onclick = async () => { const name=prompt('Community name'); if(!name)return; try { const community=await api('/api/communities',{method:'POST',body:JSON.stringify({name})}); state.communities.push(community); selectCommunity(community); } catch(error){alert(error.message);} };
$('#join-community').onclick = async () => { const invite=prompt('Paste the invite code'); if(!invite)return; try { await api('/api/invites/join',{method:'POST',body:JSON.stringify({invite})}); await enterApp(); } catch(error){alert(error.message);} };
$('#invite-friend').onclick = async () => { if(!state.community)return; try { const data=await api('/api/invites',{method:'POST',body:JSON.stringify({community_id:state.community.id,max_uses:10,lifetime_hours:24})}); if(navigator.clipboard && window.isSecureContext){await navigator.clipboard.writeText(data.token); alert('Invite code copied. It expires in 24 hours.');}else{prompt('Copy this invite code. It expires in 24 hours.',data.token);} } catch(error){alert(error.message);} };
$('#add-channel').onclick = async () => { if(!state.community)return; const name=prompt('Channel name'); if(!name)return; try { const channel=await api('/api/channels',{method:'POST',body:JSON.stringify({name,community_id:state.community.id})}); state.community.channels.push(channel); selectCommunity(state.community); selectChannel(channel); } catch(error){alert(error.message);} };

api('/api/me').then(data => data.user ? enterApp() : showAuth()).catch(showAuth);
