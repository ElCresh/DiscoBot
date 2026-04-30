let trackType = 'local';
let isPlaying = false;
let currentShuffle = false;
let currentRepeat = 'off';
let currentNormalize = true;
let ws = null;
let wsReconnectTimer = null;

// --- WebSocket ---
function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws`);
    ws.onopen = () => {
        document.getElementById('wsStatus').classList.add('connected');
        if (wsReconnectTimer) { clearInterval(wsReconnectTimer); wsReconnectTimer = null; }
    };
    ws.onmessage = (event) => {
        const state = JSON.parse(event.data);
        applyState(state);
    };
    ws.onclose = () => {
        document.getElementById('wsStatus').classList.remove('connected');
        if (!wsReconnectTimer) {
            wsReconnectTimer = setInterval(() => {
                if (!ws || ws.readyState === WebSocket.CLOSED) connectWebSocket();
            }, 3000);
        }
    };
    ws.onerror = () => ws.close();
}

// Fallback polling for position updates (WS doesn't push these continuously)
setInterval(async () => {
    try {
        const state = await api('/player/state');
        // Only update time-sensitive fields, not the full UI
        document.getElementById('currentTime').textContent = formatTime(state.position);
        document.getElementById('totalTime').textContent = formatTime(state.duration);
        const pct = state.duration > 0 ? (state.position / state.duration * 100) : 0;
        document.getElementById('progressFill').style.width = pct + '%';
        isPlaying = state.is_playing;
        document.getElementById('playPauseBtn').innerHTML = isPlaying ? '&#9208;' : '&#9654;';
    } catch (e) {}
}, 1000);

function applyState(state) {
    isPlaying = state.is_playing;

    // Now playing
    const track = state.current_track;
    const title = track ? track.title : 'Nessuna traccia';
    document.getElementById('trackTitle').textContent = title;

    // Artist / album info
    const artistEl = document.getElementById('artistInfo');
    if (track && (track.artist || track.album)) {
        const parts = [];
        if (track.artist) parts.push(track.artist);
        if (track.album) parts.push(track.album);
        artistEl.textContent = parts.join(' — ');
    } else {
        artistEl.textContent = '';
    }

    document.getElementById('currentTime').textContent = formatTime(state.position);
    document.getElementById('totalTime').textContent = formatTime(state.duration);
    document.getElementById('playPauseBtn').innerHTML = isPlaying ? '&#9208;' : '&#9654;';

    // Progress
    const pct = state.duration > 0 ? (state.position / state.duration * 100) : 0;
    document.getElementById('progressFill').style.width = pct + '%';

    // Volume
    document.getElementById('volumeSlider').value = state.volume;
    document.getElementById('volumeLabel').textContent = state.volume;

    // Shuffle & Repeat
    currentShuffle = state.shuffle;
    currentRepeat = state.repeat;
    document.getElementById('shuffleBtn').classList.toggle('active', currentShuffle);
    const repeatBtn = document.getElementById('repeatBtn');
    repeatBtn.classList.toggle('active', currentRepeat !== 'off');
    const repeatSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>';
    if (currentRepeat === 'one') {
        repeatBtn.innerHTML = repeatSvg + '<small style="font-size:0.6em;margin-left:-2px;">1</small>';
    } else {
        repeatBtn.innerHTML = repeatSvg;
    }

    // Auto-livellamento (normalize)
    currentNormalize = state.normalize !== false;
    document.getElementById('normalizeBtn').classList.toggle('active', currentNormalize);

    // Queue
    renderQueue(state.queue);
    refreshHistory();
}

function setType(type) {
    trackType = type;
    document.getElementById('btnLocal').classList.toggle('active', type === 'local');
    document.getElementById('btnYoutube').classList.toggle('active', type === 'youtube');
    document.getElementById('btnSpotify').classList.toggle('active', type === 'spotify');
    document.getElementById('btnSoundcloud').classList.toggle('active', type === 'soundcloud');
    document.getElementById('youtubeInput').style.display = type === 'youtube' ? 'block' : 'none';
    document.getElementById('localInput').style.display = type === 'local' ? 'block' : 'none';
    document.getElementById('spotifyInput').style.display = type === 'spotify' ? 'block' : 'none';
    document.getElementById('soundcloudInput').style.display = type === 'soundcloud' ? 'block' : 'none';
}

async function api(url, method = 'GET', body = null) {
    const opts = { method };
    if (body) {
        opts.headers = { 'Content-Type': 'application/json' };
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    return res.json();
}

let currentMediaPath = '';

function escapeAttr(s) {
    return s.replace(/&/g, '&amp;').replace(/'/g, '&#39;').replace(/"/g, '&quot;');
}

function renderMediaBreadcrumb(path) {
    const crumbs = ['<span class="crumb" onclick="navigateMedia(\'\')">media</span>'];
    const parts = path.split('/').filter(Boolean);
    let acc = '';
    parts.forEach((p, i) => {
        acc = acc ? acc + '/' + p : p;
        crumbs.push('<span class="sep">/</span>');
        if (i === parts.length - 1) {
            crumbs.push(`<span style="color:#fff;">${p}</span>`);
        } else {
            crumbs.push(`<span class="crumb" onclick="navigateMedia('${escapeAttr(acc)}')">${p}</span>`);
        }
    });
    document.getElementById('mediaBreadcrumb').innerHTML = crumbs.join('');
}

async function refreshMedia() {
    try {
        const data = await api('/media/list?path=' + encodeURIComponent(currentMediaPath));
        renderMediaBreadcrumb(data.path);
        const list = document.getElementById('mediaList');
        const items = [];

        if (data.parent !== null) {
            items.push(
                `<li class="dir" onclick="navigateMedia('${escapeAttr(data.parent)}')">
                    <span>..</span>
                </li>`
            );
        }
        data.dirs.forEach(d => {
            const childPath = data.path ? data.path + '/' + d : d;
            items.push(
                `<li class="dir" onclick="navigateMedia('${escapeAttr(childPath)}')">
                    <span>${d}</span>
                </li>`
            );
        });
        data.files.forEach(f => {
            const fullPath = data.path ? data.path + '/' + f : f;
            items.push(
                `<li class="file">
                    <span>${f}</span>
                    <button onclick="addMediaToQueue('${escapeAttr(fullPath)}')">+</button>
                </li>`
            );
        });

        if (items.length === 0) {
            list.innerHTML = '<li class="empty-media">Cartella vuota</li>';
        } else {
            list.innerHTML = items.join('');
        }
    } catch (e) {
        console.error('Media refresh failed:', e);
    }
}

function navigateMedia(path) {
    currentMediaPath = path || '';
    refreshMedia();
}

async function addMediaToQueue(filepath) {
    await api('/queue/add-media?path=' + encodeURIComponent(filepath), 'POST');
}

async function uploadFile() {
    const input = document.getElementById('fileUpload');
    if (!input.files.length) return;
    const formData = new FormData();
    formData.append('file', input.files[0]);
    await fetch('/media/upload?path=' + encodeURIComponent(currentMediaPath), {
        method: 'POST',
        body: formData
    });
    input.value = '';
    refreshMedia();
}

async function addTrack() {
    const input = document.getElementById('trackInput');
    const url = input.value.trim();
    if (!url) return;
    await api('/queue/add', 'POST', { path: url, type: 'youtube' });
    input.value = '';
}

async function searchYoutube() {
    const input = document.getElementById('youtubeSearchInput');
    const query = input.value.trim();
    if (!query) return;
    const list = document.getElementById('youtubeSearchResults');
    list.innerHTML = '<li class="empty-media">Ricerca in corso...</li>';
    try {
        const data = await api('/youtube/search?q=' + encodeURIComponent(query));
        if (data.results.length === 0) {
            list.innerHTML = '<li class="empty-media">Nessun risultato</li>';
        } else {
            list.innerHTML = data.results.map(r =>
                `<li>
                    <span><strong>${r.title}</strong>${r.artist ? ' — ' + r.artist : ''}<br><small style="color:#888">${fmtDuration(r.duration_ms)}</small></span>
                    <button onclick="addYoutubeTrack('${r.url.replace(/'/g, "\\'")}')">+</button>
                </li>`
            ).join('');
        }
    } catch (e) {
        list.innerHTML = '<li class="empty-media">Errore nella ricerca</li>';
    }
}

async function addYoutubeTrack(url) {
    await api('/queue/add', 'POST', { path: url, type: 'youtube' });
}

async function searchSpotify() {
    const input = document.getElementById('spotifySearchInput');
    const query = input.value.trim();
    if (!query) return;
    const list = document.getElementById('spotifyResults');
    list.innerHTML = '<li class="empty-media">Ricerca in corso...</li>';
    try {
        const data = await api('/spotify/search?q=' + encodeURIComponent(query));
        if (data.results.length === 0) {
            list.innerHTML = '<li class="empty-media">Nessun risultato</li>';
        } else {
            list.innerHTML = data.results.map(r =>
                `<li>
                    <span><strong>${r.title}</strong> — ${r.artist}<br><small style="color:#888">${r.album} · ${Math.floor(r.duration_ms/60000)}:${String(Math.floor((r.duration_ms%60000)/1000)).padStart(2,'0')}</small></span>
                    <button onclick="addSpotifyTrack('${r.spotify_id}')">+</button>
                </li>`
            ).join('');
        }
    } catch (e) {
        list.innerHTML = '<li class="empty-media">Errore nella ricerca</li>';
    }
}

async function addSpotifyTrack(spotifyId) {
    await api('/queue/add', 'POST', { path: spotifyId, type: 'spotify' });
}

function fmtDuration(ms) {
    if (!ms) return '';
    return Math.floor(ms/60000) + ':' + String(Math.floor((ms%60000)/1000)).padStart(2,'0');
}

function switchSearchTab(tab) {
    document.querySelectorAll('.search-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    document.querySelectorAll('.search-tab-panel').forEach(p => p.classList.toggle('active', p.dataset.tab === tab));
}

async function unifiedSearch() {
    const input = document.getElementById('unifiedSearchInput');
    const query = input.value.trim();
    if (!query) return;
    const container = document.getElementById('unifiedResults');
    container.innerHTML = '<div class="search-loading">Ricerca in corso...</div>';
    try {
        const data = await api('/search?q=' + encodeURIComponent(query));

        const sources = [
            { key: 'local', label: 'File', items: data.local || [], render: f =>
                `<li><span>${f.title}</span><button onclick="addMediaToQueue('${f.filename.replace(/'/g, "\\'")}')">+</button></li>`
            },
            { key: 'youtube', label: 'YouTube', items: data.youtube || [], render: r =>
                `<li><span><strong>${r.title}</strong>${r.artist ? ' — ' + r.artist : ''}<br><small style="color:#888">${fmtDuration(r.duration_ms)}</small></span><button onclick="api('/queue/add','POST',{path:'${r.url.replace(/'/g, "\\'")}',type:'youtube'})">+</button></li>`
            },
            { key: 'spotify', label: 'Spotify', items: data.spotify || [], render: r =>
                `<li><span><strong>${r.title}</strong> — ${r.artist}<br><small style="color:#888">${r.album || ''} · ${fmtDuration(r.duration_ms)}</small></span><button onclick="addSpotifyTrack('${r.spotify_id}')">+</button></li>`
            },
            { key: 'soundcloud', label: 'SoundCloud', items: data.soundcloud || [], render: r =>
                `<li><span><strong>${r.title}</strong>${r.artist ? ' — ' + r.artist : ''}<br><small style="color:#888">${fmtDuration(r.duration_ms)}</small></span><button onclick="addSoundCloudTrack('${r.url.replace(/'/g, "\\'")}')">+</button></li>`
            },
        ];

        const withResults = sources.filter(s => s.items.length > 0);
        if (withResults.length === 0) {
            container.innerHTML = '<div class="search-empty">Nessun risultato trovato</div>';
            return;
        }

        let tabsHtml = '<div class="search-tabs">';
        tabsHtml += withResults.map((s, i) =>
            `<button data-tab="${s.key}" class="${i === 0 ? 'active' : ''}" onclick="switchSearchTab('${s.key}')">${s.label} (${s.items.length})</button>`
        ).join('');
        tabsHtml += '</div>';

        let panelsHtml = withResults.map((s, i) =>
            `<div class="search-tab-panel ${i === 0 ? 'active' : ''}" data-tab="${s.key}"><ul class="media-list">${s.items.map(s.render).join('')}</ul></div>`
        ).join('');

        container.innerHTML = tabsHtml + panelsHtml;
    } catch (e) {
        container.innerHTML = '<div class="search-empty">Errore nella ricerca</div>';
    }
}

async function searchSoundCloud() {
    const input = document.getElementById('soundcloudSearchInput');
    const query = input.value.trim();
    if (!query) return;
    const list = document.getElementById('soundcloudResults');
    list.innerHTML = '<li class="empty-media">Ricerca in corso...</li>';
    try {
        const data = await api('/soundcloud/search?q=' + encodeURIComponent(query));
        if (data.results.length === 0) {
            list.innerHTML = '<li class="empty-media">Nessun risultato</li>';
        } else {
            list.innerHTML = data.results.map(r =>
                `<li>
                    <span><strong>${r.title}</strong>${r.artist ? ' — ' + r.artist : ''}<br><small style="color:#888">${Math.floor(r.duration_ms/60000)}:${String(Math.floor((r.duration_ms%60000)/1000)).padStart(2,'0')}</small></span>
                    <button onclick="addSoundCloudTrack('${r.url.replace(/'/g, "\\'")}')">+</button>
                </li>`
            ).join('');
        }
    } catch (e) {
        list.innerHTML = '<li class="empty-media">Errore nella ricerca</li>';
    }
}

async function addSoundCloudTrack(url) {
    await api('/queue/add', 'POST', { path: url, type: 'soundcloud' });
}

async function removeTrack(id) {
    await api('/queue/' + id, 'DELETE');
}

async function moveTrack(id, position) {
    await api('/queue/' + id + '/move?position=' + position, 'POST');
}

let draggedTrackId = null;

function togglePlayPause() {
    api(isPlaying ? '/player/pause' : '/player/play', 'POST');
}

function toggleShuffle() {
    api('/player/shuffle?enabled=' + (!currentShuffle), 'POST');
}

function cycleRepeat() {
    const modes = ['off', 'one', 'all'];
    const next = modes[(modes.indexOf(currentRepeat) + 1) % modes.length];
    api('/player/repeat?mode=' + next, 'POST');
}

function toggleNormalize() {
    api('/player/normalize?enabled=' + (!currentNormalize), 'POST');
}

function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m + ':' + s.toString().padStart(2, '0');
}

function renderQueue(queue) {
    const list = document.getElementById('queueList');
    if (queue.length === 0) {
        list.innerHTML = '<li class="empty-queue">La scaletta è vuota</li>';
        return;
    }
    const len = queue.length;
    list.innerHTML = queue.map((t, i) => {
        const artistPart = t.artist ? `<span class="track-artist">${t.artist}</span>` : '';
        return `<li draggable="true" data-track-id="${t.id}" data-index="${i}">
            <span class="drag-handle">&#10303;</span>
            <span class="track-type">${t.type === 'youtube' ? 'YT' : t.type === 'spotify' ? 'SP' : t.type === 'soundcloud' ? 'SC' : 'FILE'}</span>
            <span class="track-name">${t.title}${artistPart ? '<br>' + artistPart : ''}</span>
            ${i > 0 ? `<button class="move-btn" onclick="moveTrack(${t.id}, ${i - 1})">&#8593;</button>` : ''}
            ${i < len - 1 ? `<button class="move-btn" onclick="moveTrack(${t.id}, ${i + 1})">&#8595;</button>` : ''}
            <button onclick="removeTrack(${t.id})">&#10005;</button>
        </li>`;
    }).join('');

    // Attach drag & drop events
    list.querySelectorAll('li[draggable]').forEach(li => {
        li.addEventListener('dragstart', (e) => {
            draggedTrackId = li.dataset.trackId;
            li.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', li.dataset.trackId);
        });
        li.addEventListener('dragend', () => {
            li.classList.remove('dragging');
            list.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
            draggedTrackId = null;
        });
        li.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            if (li.dataset.trackId !== draggedTrackId) {
                li.classList.add('drag-over');
            }
        });
        li.addEventListener('dragleave', () => {
            li.classList.remove('drag-over');
        });
        li.addEventListener('drop', (e) => {
            e.preventDefault();
            li.classList.remove('drag-over');
            const targetIndex = parseInt(li.dataset.index);
            const srcId = e.dataTransfer.getData('text/plain');
            if (srcId && li.dataset.trackId !== srcId) {
                moveTrack(srcId, targetIndex);
            }
        });
    });
}

let historyOffset = 0;
const historyLimit = 15;

async function refreshHistory() {
    try {
        const data = await api('/history?offset=' + historyOffset + '&limit=' + historyLimit);
        const list = document.getElementById('historyList');
        const pag = document.getElementById('historyPagination');
        const clearBtn = document.getElementById('clearHistoryBtn');

        // Clamp offset if entries were removed and we now overshoot
        if (historyOffset >= data.total && data.total > 0) {
            historyOffset = Math.max(0, Math.floor((data.total - 1) / historyLimit) * historyLimit);
            return refreshHistory();
        }

        if (data.total === 0) {
            list.innerHTML = '<li class="empty-history">Nessuna traccia nella cronologia</li>';
            pag.style.display = 'none';
            clearBtn.style.display = 'none';
            return;
        }

        list.innerHTML = data.items.map((e, i) => {
            const globalIdx = data.offset + i;
            return `<li>
                <span class="track-type">${e.track.type === 'youtube' ? 'YT' : e.track.type === 'spotify' ? 'SP' : e.track.type === 'soundcloud' ? 'SC' : 'FILE'}</span>
                <span class="track-name">${e.track.title}</span>
                <span class="track-time">${new Date(e.played_at).toLocaleTimeString()}</span>
                <button onclick="requeueFromHistory(${globalIdx})">+ Coda</button>
                <button onclick="removeHistoryEntry(${globalIdx})" style="background:none;color:#e94560;padding:4px;">&#10005;</button>
            </li>`;
        }).join('');

        clearBtn.style.display = 'block';

        const totalPages = Math.ceil(data.total / data.limit);
        const currentPage = Math.floor(data.offset / data.limit) + 1;
        if (totalPages > 1) {
            pag.style.display = 'flex';
            document.getElementById('histPageInfo').textContent =
                `${currentPage} / ${totalPages} (${data.total} tracce)`;
            document.getElementById('histPrev').disabled = data.offset === 0;
            document.getElementById('histNext').disabled = data.offset + data.limit >= data.total;
        } else {
            pag.style.display = 'none';
        }
    } catch (e) {
        console.error('History refresh failed:', e);
    }
}

function historyPrev() {
    historyOffset = Math.max(0, historyOffset - historyLimit);
    refreshHistory();
}

function historyNext() {
    historyOffset += historyLimit;
    refreshHistory();
}

async function requeueFromHistory(index) {
    await api('/history/' + index + '/requeue', 'POST');
}

async function removeHistoryEntry(index) {
    await api('/history/' + index, 'DELETE');
    refreshHistory();
}

async function clearHistory() {
    if (!confirm('Cancellare tutta la cronologia?')) return;
    await api('/history', 'DELETE');
    refreshHistory();
}

// --- Playlists ---
async function refreshPlaylists() {
    try {
        const data = await api('/playlists');
        const list = document.getElementById('playlistList');
        if (data.playlists.length === 0) {
            list.innerHTML = '<li class="empty-playlist">Nessuna playlist salvata</li>';
        } else {
            list.innerHTML = data.playlists.map(p =>
                `<li>
                    <div class="pl-info">
                        <div class="pl-name">${p.name}</div>
                        <div class="pl-count">${p.track_count} brani</div>
                    </div>
                    <div class="pl-actions">
                        <button onclick="loadPlaylist('${p.name.replace(/'/g, "\\'")}', false)" title="Accoda">+ Coda</button>
                        <button onclick="loadPlaylist('${p.name.replace(/'/g, "\\'")}', true)" title="Sostituisci">&#9654;</button>
                        <button class="del-btn" onclick="deletePlaylist('${p.name.replace(/'/g, "\\'")}')">&#10005;</button>
                    </div>
                </li>`
            ).join('');
        }
    } catch (e) {
        console.error('Playlist refresh failed:', e);
    }
}

async function savePlaylist() {
    const input = document.getElementById('playlistNameInput');
    const name = input.value.trim();
    if (!name) return;
    await api('/playlists/save?name=' + encodeURIComponent(name), 'POST');
    input.value = '';
    refreshPlaylists();
}

async function loadPlaylist(name, replace) {
    await api('/playlists/' + encodeURIComponent(name) + '/load?replace=' + replace, 'POST');
}

async function deletePlaylist(name) {
    if (!confirm(`Eliminare la playlist "${name}"?`)) return;
    await api('/playlists/' + encodeURIComponent(name), 'DELETE');
    refreshPlaylists();
}

// Seek on progress bar click
document.getElementById('progressBar').addEventListener('click', async (e) => {
    const bar = e.currentTarget;
    const pct = e.offsetX / bar.offsetWidth;
    const state = await api('/player/state');
    if (state.duration > 0) {
        api('/player/seek?position=' + (pct * state.duration), 'POST');
    }
});

// Allow Enter key to add track
document.getElementById('trackInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') addTrack();
});

// Enter key for YouTube search
document.getElementById('youtubeSearchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') searchYoutube();
});

// Enter key for Spotify search
document.getElementById('spotifySearchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') searchSpotify();
});

// Enter key for SoundCloud search
document.getElementById('soundcloudSearchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') searchSoundCloud();
});

// Enter key for unified search
document.getElementById('unifiedSearchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') unifiedSearch();
});

// Enter key for playlist save
document.getElementById('playlistNameInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') savePlaylist();
});

// --- Settings panel + Spotify auth status ---
let spotifyAuthFastPoll = null;

async function fetchSpotifyAuth() {
    try { return await api('/spotify/auth-status'); }
    catch (e) { return null; }
}

function applySpotifyStatus(data) {
    if (!data) return;
    const dot = document.getElementById('spotifyStatus');
    const sDot = document.getElementById('settingsSpotifyDot');
    const sLabel = document.getElementById('settingsSpotifyLabel');
    const loginArea = document.getElementById('spotifyLoginArea');
    const instructions = document.getElementById('spotifyInstructions');
    const success = document.getElementById('spotifySuccess');

    const authed = !!data.authenticated;
    const status = data.session_status || (authed ? 'idle' : 'idle');
    const warming = authed && status === 'warming';
    const ready = authed && status === 'ready';
    const failed = authed && status === 'failed';

    // Three-state header dot: red (no auth/failed) / yellow pulse (warming) / green (ready)
    dot.classList.toggle('authenticated', ready);
    dot.classList.toggle('warming', warming);
    dot.classList.toggle('unauthenticated', !authed || failed);
    sDot.classList.toggle('authenticated', ready);
    sDot.classList.toggle('warming', warming);
    sDot.classList.toggle('unauthenticated', !authed || failed);

    let labelText;
    if (!authed) labelText = 'Non autenticato';
    else if (warming) labelText = 'In avvio…';
    else if (failed) labelText = 'Errore connessione';
    else if (ready) labelText = 'Pronto';
    else labelText = 'In attesa';
    sLabel.textContent = labelText;
    dot.title = labelText;

    // Aggiungi Traccia → Spotify: mostra solo quando la sessione librespot è
    // operativa (ready), altrimenti il flusso "cerca → aggiungi → riproduce"
    // fallirebbe a metà o partirebbe con grossi delay.
    document.getElementById('btnSpotify').style.display = ready ? '' : 'none';
    if (!ready && trackType === 'spotify') setType('local');

    if (authed) {
        loginArea.style.display = 'none';
        instructions.style.display = 'none';
        // While the session is still warming, keep polling fast so the dot
        // and the "Aggiungi Spotify" button flip the moment it's ready.
        if (warming) {
            if (!spotifyAuthFastPoll) {
                spotifyAuthFastPoll = setInterval(refreshSpotifyAuth, 1500);
            }
        } else if (spotifyAuthFastPoll) {
            success.style.display = '';
            clearInterval(spotifyAuthFastPoll);
            spotifyAuthFastPoll = null;
        }
    } else {
        success.style.display = 'none';
        const zcRunning = data.zeroconf && data.zeroconf.running;
        if (zcRunning) {
            loginArea.style.display = 'none';
            instructions.style.display = '';
            document.getElementById('spotifyDeviceName').textContent =
                (data.zeroconf.device_name || 'DiscoBot');
        } else {
            loginArea.style.display = '';
            instructions.style.display = 'none';
            document.getElementById('spotifyLoginBtn').disabled = false;
        }
    }
}

async function refreshSpotifyAuth() {
    applySpotifyStatus(await fetchSpotifyAuth());
}

// --- Hash router: '' or '#/' → main, '#/settings' → settings page ---
async function applyRoute() {
    const settingsActive = (location.hash || '').startsWith('#/settings');
    document.getElementById('mainView').hidden = settingsActive;
    document.getElementById('settingsView').hidden = !settingsActive;
    if (settingsActive) {
        refreshSpotifyAuth();
        return;
    }
    // Leaving settings: stop the fast poll and tear down any stranded Zeroconf
    // bootstrap so DiscoBot doesn't linger as a Connect device on the LAN.
    if (spotifyAuthFastPoll) {
        clearInterval(spotifyAuthFastPoll);
        spotifyAuthFastPoll = null;
    }
    const data = await fetchSpotifyAuth();
    if (data && data.zeroconf && data.zeroconf.running && !data.authenticated) {
        try { await api('/spotify/zeroconf/stop', 'POST'); } catch (e) {}
    }
}

window.addEventListener('hashchange', applyRoute);

async function startSpotifyZeroconf() {
    const btn = document.getElementById('spotifyLoginBtn');
    btn.disabled = true;
    try {
        await api('/spotify/zeroconf/start', 'POST');
    } catch (e) {
        btn.disabled = false;
        return;
    }
    await refreshSpotifyAuth();
    if (!spotifyAuthFastPoll) {
        spotifyAuthFastPoll = setInterval(refreshSpotifyAuth, 2000);
    }
}

async function stopSpotifyZeroconf() {
    try { await api('/spotify/zeroconf/stop', 'POST'); } catch (e) {}
    if (spotifyAuthFastPoll) {
        clearInterval(spotifyAuthFastPoll);
        spotifyAuthFastPoll = null;
    }
    refreshSpotifyAuth();
}

// Background poll keeps the persistent dot honest at low cost
setInterval(refreshSpotifyAuth, 30000);
refreshSpotifyAuth();

// Init
connectWebSocket();
refreshMedia();
refreshPlaylists();
applyRoute();
