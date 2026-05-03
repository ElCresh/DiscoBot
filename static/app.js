// =====================================================================
// DiscoBot — Alpine root + API actions
// =====================================================================

function discoBot() {
    return {
        // ---- player state (mirrors WS) ----
        player: {
            isPlaying: false,
            track: null,
            position: 0,
            duration: 0,
            volume: 80,
            shuffle: false,
            repeat: 'off',
            normalize: true,
        },
        queue: [],

        // ---- ws ----
        ws: { connected: false, _socket: null, _reconnect: null },

        // ---- spotify ----
        spotify: {
            authed: false,
            ready: false,
            warming: false,
            failed: false,
            zeroconf: null,
            startingZeroconf: false,
            _fastPoll: null,
            _slowPoll: null,
        },

        // ---- ui ----
        activeTab: 'player',
        settingsOpen: false,
        settingsActiveSection: 'security',  // sezione attiva nel dialog
        theme: document.documentElement.getAttribute('data-theme') || 'dark',
        viewport: window.innerWidth,
        toasts: {
            items: [],
            _seq: 0,
            push(type, msg) {
                const id = ++this._seq;
                this.items.push({ id, type, msg });
                setTimeout(() => this.dismiss(id), 4000);
            },
            dismiss(id) {
                const i = this.items.findIndex(t => t.id === id);
                if (i >= 0) this.items.splice(i, 1);
            },
        },

        // ---- add card ----
        addMode: 'search',  // 'search' | 'browse' | 'url'
        addUrlInput: '',
        trackType: 'local', // legacy: utilizzato in alcune asserzioni Spotify
        playlistName: '',

        // ---- volume / mute ----
        muted: false,
        _volumeBeforeMute: 80,

        // ---- search ----
        search: {
            unifiedQuery: '', unifiedLoading: false, unifiedSearched: false,
            unifiedTabs: [], unifiedActiveTab: '',
            unifiedPageSize: 10, unifiedLoadingMore: false,
            // Per-source state: items[], hasMore, fetched count
            // Held inside unifiedTabs[*] (count, exhausted)
            ytQuery: '', ytLoading: false, ytSearched: false, ytResults: [],
            spQuery: '', spLoading: false, spSearched: false, spResults: [],
            scQuery: '', scLoading: false, scSearched: false, scResults: [],
        },

        // ---- media ----
        media: {
            path: '', parent: null,
            dirs: [], files: [], items: [],
        },

        // ---- history ----
        history: {
            items: [], offset: 0, limit: 15, total: 0,
            currentPage: 1, totalPages: 1,
        },

        // ---- playlists ----
        playlists: [],

        // ---- runtime config + pending (interfaccia pubblica) ----
        runtimeConfig: {
            public_enabled: false,
            public_require_approval: true,
            public_sources: { local: false, youtube: true, spotify: true, soundcloud: true },
            manager_auth_enabled: true,
        },
        pending: [],

        // ---- sessioni attive (manager) ----
        sessions: [],

        // ---- tunnel pubblico (cloudflared) ----
        tunnel: {
            running: false, url: null, error: null,
            started_at: null, binary_present: false,
            starting: false,
            _pollTimer: null,
        },

        // ---- drag-drop state ----
        _draggedId: null,

        // ===== Computed-ish getters =====
        get isDesktop() { return this.viewport >= 1024; },
        get isTablet()  { return this.viewport >= 640 && this.viewport < 1024; },
        get isMobile()  { return this.viewport < 640; },

        get spotifyStatusLabel() {
            if (!this.spotify.authed) return 'Non autenticato';
            if (this.spotify.warming) return 'In avvio…';
            if (this.spotify.failed)  return 'Errore connessione';
            if (this.spotify.ready)   return 'Pronto';
            return 'In attesa';
        },

        get mediaCrumbs() {
            if (!this.media.path) return [];
            const parts = this.media.path.split('/').filter(Boolean);
            const acc = [];
            let p = '';
            for (const part of parts) {
                p = p ? p + '/' + part : part;
                acc.push({ name: part, path: p });
            }
            return acc;
        },

        get currentSearchItems() {
            const tab = this.search.unifiedTabs.find(t => t.key === this.search.unifiedActiveTab);
            return tab ? tab.items : [];
        },

        // "Load more" visible if the active tab still has more results to fetch.
        // Calculated per-tab based on the last page returning a full set.
        get unifiedHasMore() {
            const tab = this.search.unifiedTabs.find(t => t.key === this.search.unifiedActiveTab);
            return tab ? tab.hasMore : false;
        },

        // ETA in queue: tempo rimanente della corrente + somma durate delle precedenti
        etaForIndex(i) {
            let secs = Math.max(0, (this.player.duration || 0) - (this.player.position || 0));
            for (let j = 0; j < i; j++) {
                secs += this.queue[j]?.duration || 0;
            }
            const m = Math.floor(secs / 60);
            const s = Math.floor(secs % 60);
            return m + ':' + String(s).padStart(2, '0');
        },

        // ===== Init =====
        init() {
            this.connectWebSocket();
            this.refreshMedia();
            this.refreshPlaylists();
            this.refreshSpotifyAuth();
            this.refreshHistory();
            this.refreshRuntimeConfig().then(() => this.refreshPending());
            // Polling pending ogni 4s (solo se interfaccia pubblica attiva)
            setInterval(() => this.refreshPending(), 4000);
            // Tunnel: refresh iniziale + polling soft ogni 30s (per catturare crash)
            this.refreshTunnel();
            setInterval(() => { if (!this.tunnel._pollTimer) this.refreshTunnel(); }, 30000);

            // Position polling fallback (WS doesn't push every second)
            setInterval(async () => {
                try {
                    const state = await this.api('/player/state');
                    this.player.position = state.position;
                    this.player.duration = state.duration;
                    this.player.isPlaying = state.is_playing;
                } catch (e) {}
            }, 1000);

            // Slow poll Spotify auth status (background sanity)
            this.spotify._slowPoll = setInterval(() => this.refreshSpotifyAuth(), 30000);

            // Viewport tracking
            window.addEventListener('resize', () => { this.viewport = window.innerWidth; });

            // Theme listener
            try {
                window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
                    if (!localStorage.getItem('theme')) {
                        this.setTheme(e.matches ? 'dark' : 'light');
                    }
                });
            } catch (e) {}
        },

        // ===== WebSocket =====
        connectWebSocket() {
            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            this.ws._socket = new WebSocket(`${protocol}//${location.host}/ws`);
            this.ws._socket.onopen = () => {
                this.ws.connected = true;
                if (this.ws._reconnect) { clearInterval(this.ws._reconnect); this.ws._reconnect = null; }
            };
            this.ws._socket.onmessage = (event) => {
                try { this.applyState(JSON.parse(event.data)); } catch (e) {}
            };
            this.ws._socket.onclose = () => {
                this.ws.connected = false;
                if (!this.ws._reconnect) {
                    this.ws._reconnect = setInterval(() => {
                        if (!this.ws._socket || this.ws._socket.readyState === WebSocket.CLOSED) {
                            this.connectWebSocket();
                        }
                    }, 3000);
                }
            };
            this.ws._socket.onerror = () => this.ws._socket?.close();
        },

        applyState(state) {
            this.player.isPlaying = state.is_playing;
            this.player.track = state.current_track || null;
            this.player.position = state.position;
            this.player.duration = state.duration;
            this.player.volume = state.volume;
            this.player.shuffle = state.shuffle;
            this.player.repeat = state.repeat;
            this.player.normalize = state.normalize !== false;
            this.queue = state.queue || [];
            // History changes piggybacked on state pushes
            this.refreshHistory();
        },

        // ===== API helper =====
        async api(url, method = 'GET', body = null) {
            const opts = { method };
            if (body) {
                opts.headers = { 'Content-Type': 'application/json' };
                opts.body = JSON.stringify(body);
            }
            const res = await fetch(url, opts);
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
            return res.json();
        },

        post(url) {
            this.api(url, 'POST').catch((e) => this.toasts.push('error', `Errore: ${e.message}`));
        },

        // ===== Player controls =====
        togglePlayPause() {
            this.post(this.player.isPlaying ? '/player/pause' : '/player/play');
        },
        toggleShuffle() {
            this.post('/player/shuffle?enabled=' + (!this.player.shuffle));
        },
        cycleRepeat() {
            const modes = ['off', 'one', 'all'];
            const next = modes[(modes.indexOf(this.player.repeat) + 1) % modes.length];
            this.post('/player/repeat?mode=' + next);
        },
        toggleNormalize() {
            this.post('/player/normalize?enabled=' + (!this.player.normalize));
        },
        setVolume(v) {
            this.player.volume = parseInt(v);
            if (this.muted && parseInt(v) > 0) this.muted = false;
            this.api('/player/volume?volume=' + v, 'POST').catch(() => {});
        },
        toggleMute() {
            if (this.muted) {
                const restore = this._volumeBeforeMute || 80;
                this.muted = false;
                this.setVolume(restore);
            } else {
                this._volumeBeforeMute = this.player.volume;
                this.muted = true;
                this.setVolume(0);
            }
        },
        seekFromEvent(e) {
            // Used as click fallback if no drag occurred
            if (this._didScrub) { this._didScrub = false; return; }
            const bar = e.currentTarget;
            const rect = bar.getBoundingClientRect();
            const pct = (e.clientX - rect.left) / rect.width;
            this._seekTo(pct);
        },
        startScrub(e) {
            if (this.player.duration <= 0) return;
            const bar = e.currentTarget;
            const rect = bar.getBoundingClientRect();
            const update = (clientX) => {
                let pct = (clientX - rect.left) / rect.width;
                pct = Math.min(1, Math.max(0, pct));
                this.player.position = pct * this.player.duration;
            };
            update(e.clientX);
            const onMove = (ev) => update(ev.clientX);
            const onUp = (ev) => {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                this._didScrub = true;
                let pct = (ev.clientX - rect.left) / rect.width;
                pct = Math.min(1, Math.max(0, pct));
                this._seekTo(pct);
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
            e.preventDefault();
        },
        _seekTo(pct) {
            if (this.player.duration > 0) {
                this.api('/player/seek?position=' + (pct * this.player.duration), 'POST').catch(() => {});
            }
        },

        // ===== Add track =====
        setType(type) { this.trackType = type; if (type === 'local') this.refreshMedia(); },

        async addUrlTrack(url, type) {
            try {
                await this.api('/queue/add', 'POST', { path: url, type });
                this.toasts.push('success', 'Aggiunto in coda');
            } catch (e) {
                this.toasts.push('error', `Errore: ${e.message}`);
            }
        },

        async addDirectUrl() {
            const url = this.addUrlInput.trim();
            if (!url) return;
            const type = /soundcloud\.com/i.test(url) ? 'soundcloud' : 'youtube';
            await this.addUrlTrack(url, type);
            this.addUrlInput = '';
        },

        async addSpotifyTrack(spotifyId, title) {
            try {
                await this.api('/queue/add', 'POST', { path: spotifyId, type: 'spotify' });
                this.toasts.push('success', `Aggiunto: ${title}`);
            } catch (e) {
                this.toasts.push('error', `Errore: ${e.message}`);
            }
        },

        async addMediaToQueue(filepath) {
            try {
                await this.api('/queue/add-media?path=' + encodeURIComponent(filepath), 'POST');
                this.toasts.push('success', 'Aggiunto in coda');
            } catch (e) {
                this.toasts.push('error', `Errore: ${e.message}`);
            }
        },

        addUnifiedItem(item, source) {
            if (source === 'local')      this.addMediaToQueue(item.filename);
            else if (source === 'spotify') this.addSpotifyTrack(item.spotify_id, item.title);
            else                          this.addUrlTrack(item.url, source);
        },

        // ===== Search =====
        async unifiedSearch() {
            const q = this.search.unifiedQuery.trim();
            if (!q) return;
            this.search.unifiedLoading = true;
            this.search.unifiedSearched = true;
            this.search.unifiedTabs = [];
            const limit = this.search.unifiedPageSize;
            try {
                const data = await this.api(`/search?q=${encodeURIComponent(q)}&limit=${limit}&offset=0`);
                this.search.unifiedTabs = this._buildTabsFromPage(data, limit, /* keyOffset */ 0);
                this.search.unifiedActiveTab = this.search.unifiedTabs[0]?.key || '';
            } catch (e) {
                this.toasts.push('error', 'Errore nella ricerca');
            } finally {
                this.search.unifiedLoading = false;
            }
        },

        // Builds (or extends) tab data structures from a page of results.
        // If `onlySource` is set, only that source is updated; the other tabs
        // are passed through unchanged (preserves existing items + hasMore).
        _buildTabsFromPage(data, limit, keyOffset, existingTabs = null, onlySource = null) {
            const sourceConfig = [
                { key: 'local',     label: 'File',       prefix: 'l', extract: d => d.local || [] },
                { key: 'youtube',   label: 'YouTube',    prefix: 'y', extract: d => d.youtube || [] },
                { key: 'spotify',   label: 'Spotify',    prefix: 's', extract: d => d.spotify || [] },
                { key: 'soundcloud',label: 'SoundCloud', prefix: 'c', extract: d => d.soundcloud || [] },
            ];
            const out = [];
            for (const s of sourceConfig) {
                const existing = existingTabs?.find(t => t.key === s.key);
                if (onlySource && s.key !== onlySource) {
                    if (existing) out.push(existing);
                    continue;
                }
                const newItems = s.extract(data).map((r, i) => ({
                    ...r,
                    _key: s.prefix + (keyOffset + i),
                    title: r.title || r.name,
                }));
                const allItems = existing ? [...existing.items, ...newItems] : newItems;
                if (allItems.length === 0 && !existing) continue;
                out.push({
                    key: s.key,
                    label: s.label,
                    prefix: s.prefix,
                    items: allItems,
                    // hasMore: questa pagina ha riempito il limite → probabilmente
                    // ce n'è ancora upstream. Cap interno backend a 200.
                    hasMore: newItems.length >= limit && allItems.length < 200,
                });
            }
            return out;
        },

        async loadMoreUnified() {
            const tab = this.search.unifiedTabs.find(t => t.key === this.search.unifiedActiveTab);
            if (!tab || !tab.hasMore || this.search.unifiedLoadingMore) return;

            this.search.unifiedLoadingMore = true;
            const q = this.search.unifiedQuery.trim();
            const limit = this.search.unifiedPageSize;
            const offset = tab.items.length;
            const sourceKey = tab.key;
            try {
                const data = await this.api(
                    `/search?q=${encodeURIComponent(q)}&limit=${limit}&offset=${offset}&sources=${sourceKey}`
                );
                this.search.unifiedTabs = this._buildTabsFromPage(
                    data, limit, offset, this.search.unifiedTabs, sourceKey
                );
            } catch (e) {
                this.toasts.push('error', 'Errore nel caricamento');
            } finally {
                this.search.unifiedLoadingMore = false;
            }
        },

        async searchYoutube() {
            const q = this.search.ytQuery.trim();
            if (!q) return;
            this.search.ytLoading = true;
            this.search.ytSearched = true;
            try {
                const data = await this.api('/youtube/search?q=' + encodeURIComponent(q));
                this.search.ytResults = data.results || [];
            } catch (e) {
                this.search.ytResults = [];
                this.toasts.push('error', 'Errore nella ricerca YouTube');
            } finally {
                this.search.ytLoading = false;
            }
        },

        async searchSpotify() {
            const q = this.search.spQuery.trim();
            if (!q) return;
            this.search.spLoading = true;
            this.search.spSearched = true;
            try {
                const data = await this.api('/spotify/search?q=' + encodeURIComponent(q));
                this.search.spResults = data.results || [];
            } catch (e) {
                this.search.spResults = [];
                this.toasts.push('error', 'Errore nella ricerca Spotify');
            } finally {
                this.search.spLoading = false;
            }
        },

        async searchSoundCloud() {
            const q = this.search.scQuery.trim();
            if (!q) return;
            this.search.scLoading = true;
            this.search.scSearched = true;
            try {
                const data = await this.api('/soundcloud/search?q=' + encodeURIComponent(q));
                this.search.scResults = data.results || [];
            } catch (e) {
                this.search.scResults = [];
                this.toasts.push('error', 'Errore nella ricerca SoundCloud');
            } finally {
                this.search.scLoading = false;
            }
        },

        // ===== Media browser =====
        async refreshMedia() {
            try {
                const data = await this.api('/media/list?path=' + encodeURIComponent(this.media.path));
                this.media.path = data.path;
                this.media.parent = data.parent;
                this.media.dirs = (data.dirs || []).map(d => ({
                    name: d,
                    path: data.path ? data.path + '/' + d : d,
                }));
                this.media.files = (data.files || []).map(f => ({
                    name: f,
                    path: data.path ? data.path + '/' + f : f,
                }));
                this.media.items = [...this.media.dirs, ...this.media.files];
            } catch (e) {
                console.error('Media refresh failed', e);
            }
        },

        navigateMedia(path) {
            this.media.path = path || '';
            this.refreshMedia();
        },

        async uploadFile() {
            const input = document.getElementById('fileUpload');
            if (!input || !input.files.length) return;
            const formData = new FormData();
            formData.append('file', input.files[0]);
            try {
                await fetch('/media/upload?path=' + encodeURIComponent(this.media.path), {
                    method: 'POST',
                    body: formData,
                });
                this.toasts.push('success', 'File caricato');
                input.value = '';
                this.refreshMedia();
            } catch (e) {
                this.toasts.push('error', 'Upload fallito');
            }
        },

        // ===== Queue actions =====
        async removeTrack(id) {
            try { await this.api('/queue/' + id, 'DELETE'); }
            catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async moveTrack(id, position) {
            try { await this.api('/queue/' + id + '/move?position=' + position, 'POST'); }
            catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },

        // Drag & drop
        onDragStart(e, id) {
            this._draggedId = String(id);
            e.currentTarget.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', String(id));
        },
        onDragEnd(e) {
            e.currentTarget.classList.remove('dragging');
            document.querySelectorAll('#queueList .drag-over').forEach(el => el.classList.remove('drag-over'));
            this._draggedId = null;
        },
        onDragOver(e) {
            e.dataTransfer.dropEffect = 'move';
            const li = e.currentTarget;
            if (li.dataset.trackId !== this._draggedId) li.classList.add('drag-over');
        },
        onDrop(e, targetIndex) {
            const li = e.currentTarget;
            li.classList.remove('drag-over');
            const srcId = e.dataTransfer.getData('text/plain') || this._draggedId;
            if (srcId && li.dataset.trackId !== srcId) {
                this.moveTrack(srcId, targetIndex);
            }
        },

        // ===== History =====
        async refreshHistory() {
            try {
                const data = await this.api(`/history?offset=${this.history.offset}&limit=${this.history.limit}`);
                if (this.history.offset >= data.total && data.total > 0) {
                    this.history.offset = Math.max(0, Math.floor((data.total - 1) / this.history.limit) * this.history.limit);
                    return this.refreshHistory();
                }
                this.history.items = data.items || [];
                this.history.total = data.total || 0;
                this.history.totalPages = Math.max(1, Math.ceil(data.total / data.limit));
                this.history.currentPage = Math.floor(data.offset / data.limit) + 1;
            } catch (e) {
                console.error('History refresh failed', e);
            }
        },
        historyPrev() { this.history.offset = Math.max(0, this.history.offset - this.history.limit); this.refreshHistory(); },
        historyNext() { this.history.offset += this.history.limit; this.refreshHistory(); },
        async requeueFromHistory(idx) {
            try {
                await this.api('/history/' + idx + '/requeue', 'POST');
                this.toasts.push('success', 'Aggiunto in coda');
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async removeHistoryEntry(idx) {
            try {
                await this.api('/history/' + idx, 'DELETE');
                this.refreshHistory();
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async clearHistory() {
            if (!confirm('Cancellare tutta la cronologia?')) return;
            try {
                await this.api('/history', 'DELETE');
                this.refreshHistory();
                this.toasts.push('success', 'Cronologia svuotata');
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },

        // ===== Playlists =====
        async refreshPlaylists() {
            try {
                const data = await this.api('/playlists');
                this.playlists = data.playlists || [];
            } catch (e) { console.error('Playlists refresh failed', e); }
        },

        // ===== Runtime config + Pending requests (manager) =====
        async refreshRuntimeConfig() {
            try {
                this.runtimeConfig = await this.api('/admin/config');
            } catch (e) { console.error('Runtime config refresh failed', e); }
        },
        async patchConfig(updates) {
            try {
                this.runtimeConfig = await this.api('/admin/config', 'PATCH', updates);
                this.toasts.push('success', 'Impostazioni aggiornate');
                if (this.runtimeConfig.public_enabled) this.refreshPending();
                else this.pending = [];
                // Se l'auth è stata disabilitata, anche il tunnel viene fermato lato backend
                if (!this.runtimeConfig.manager_auth_enabled) this.refreshTunnel();
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },

        confirmAuthToggle(event) {
            const checked = event.target.checked;
            if (!checked) {
                const ok = window.confirm(
                    'Disattivare l\'autenticazione Manager?\n\n' +
                    'Chiunque sulla LAN potrà accedere a /m senza password.\n' +
                    'L\'interfaccia pubblica e il tunnel verranno disabilitati per sicurezza.'
                );
                if (!ok) {
                    event.target.checked = true;
                    return;
                }
            }
            this.patchConfig({ manager_auth_enabled: checked });
        },

        async logout() {
            try {
                await this.api('/m/logout', 'POST');
                location.replace('/m/login');
            } catch (e) {
                this.toasts.push('error', `Errore logout: ${e.message}`);
            }
        },

        async refreshSessions() {
            if (!this.runtimeConfig.manager_auth_enabled) {
                this.sessions = [];
                return;
            }
            try {
                const data = await this.api('/m/sessions');
                this.sessions = data.sessions || [];
            } catch (e) {}
        },
        async revokeSession(sid) {
            const s = this.sessions.find(x => x.id === sid);
            const label = s ? s.ua_label : 'questa sessione';
            if (!confirm(`Revocare la sessione di ${label}?`)) return;
            try {
                await this.api(`/m/sessions/${sid}`, 'DELETE');
                this.toasts.push('warning', 'Sessione revocata');
                this.refreshSessions();
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async revokeAllOthers() {
            const n = this.sessions.filter(s => !s.current).length;
            if (n === 0) return;
            if (!confirm(`Esci da ${n} altri dispositivi?`)) return;
            try {
                const r = await this.api('/m/sessions?keep_current=true', 'DELETE');
                this.toasts.push('warning', `${r.count} sessioni revocate`);
                this.refreshSessions();
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async refreshPending() {
            if (!this.runtimeConfig.public_enabled || !this.runtimeConfig.public_require_approval) {
                this.pending = [];
                return;
            }
            try {
                const data = await this.api('/pending');
                this.pending = data.items || [];
            } catch (e) {}
        },
        async approvePending(pid) {
            try {
                await this.api(`/pending/${pid}/approve`, 'POST');
                this.toasts.push('success', 'Richiesta approvata');
                this.refreshPending();
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async rejectPending(pid) {
            try {
                await this.api(`/pending/${pid}`, 'DELETE');
                this.toasts.push('warning', 'Richiesta rifiutata');
                this.refreshPending();
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },

        // ===== Tunnel pubblico =====
        async refreshTunnel() {
            try {
                const data = await this.api('/admin/tunnel/status');
                Object.assign(this.tunnel, data);
                // Polling rapido finché stiamo aspettando l'URL
                if (this.tunnel.running && !this.tunnel.url && !this.tunnel._pollTimer) {
                    this.tunnel._pollTimer = setInterval(() => this.refreshTunnel(), 1000);
                } else if ((!this.tunnel.running || this.tunnel.url) && this.tunnel._pollTimer) {
                    clearInterval(this.tunnel._pollTimer);
                    this.tunnel._pollTimer = null;
                    if (this.tunnel.url && this.tunnel.starting) {
                        this.toasts.push('success', `Tunnel attivo: ${this.tunnel.url}`);
                    }
                    this.tunnel.starting = false;
                }
            } catch (e) {}
        },
        async tunnelStart() {
            this.tunnel.starting = true;
            this.tunnel.error = null;
            try {
                const data = await this.api('/admin/tunnel/start', 'POST');
                Object.assign(this.tunnel, data);
                if (this.tunnel.error) {
                    this.toasts.push('error', this.tunnel.error);
                    this.tunnel.starting = false;
                    return;
                }
                // Polling immediato per catturare l'URL appena disponibile
                if (!this.tunnel._pollTimer) {
                    this.tunnel._pollTimer = setInterval(() => this.refreshTunnel(), 1000);
                }
            } catch (e) {
                this.tunnel.starting = false;
                this.toasts.push('error', `Errore avvio tunnel: ${e.message}`);
            }
        },
        async tunnelStop() {
            try {
                const data = await this.api('/admin/tunnel/stop', 'POST');
                Object.assign(this.tunnel, data);
                this.toasts.push('warning', 'Tunnel fermato');
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
            if (this.tunnel._pollTimer) {
                clearInterval(this.tunnel._pollTimer);
                this.tunnel._pollTimer = null;
            }
        },
        async copyTunnelUrl() {
            if (!this.tunnel.url) return;
            let ok = false;
            // navigator.clipboard funziona solo in secure context (HTTPS o localhost).
            // Su LAN HTTP serve fallback a document.execCommand.
            if (navigator.clipboard && window.isSecureContext) {
                try {
                    await navigator.clipboard.writeText(this.tunnel.url);
                    ok = true;
                } catch (e) { /* fallthrough */ }
            }
            if (!ok) {
                try {
                    const ta = document.createElement('textarea');
                    ta.value = this.tunnel.url;
                    ta.setAttribute('readonly', '');
                    ta.style.position = 'fixed';
                    ta.style.left = '-9999px';
                    ta.style.top = '0';
                    document.body.appendChild(ta);
                    ta.focus();
                    ta.select();
                    ok = document.execCommand('copy');
                    document.body.removeChild(ta);
                } catch (e) { /* fallthrough */ }
            }
            if (ok) {
                this.toasts.push('success', 'URL copiato');
            } else {
                this.toasts.push('warning', 'Copia non disponibile — seleziona il testo e copialo a mano');
            }
        },
        async savePlaylist() {
            const name = this.playlistName.trim();
            if (!name) return;
            try {
                await this.api('/playlists/save?name=' + encodeURIComponent(name), 'POST');
                this.playlistName = '';
                this.refreshPlaylists();
                this.toasts.push('success', `Playlist "${name}" salvata`);
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async promptSavePlaylist() {
            const name = window.prompt('Nome della playlist:');
            if (!name || !name.trim()) return;
            this.playlistName = name.trim();
            await this.savePlaylist();
        },
        async loadPlaylist(name, replace) {
            try {
                await this.api('/playlists/' + encodeURIComponent(name) + '/load?replace=' + replace, 'POST');
                this.toasts.push('success', replace ? `Playlist "${name}" caricata` : `Playlist "${name}" accodata`);
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },
        async deletePlaylist(name) {
            if (!confirm(`Eliminare la playlist "${name}"?`)) return;
            try {
                await this.api('/playlists/' + encodeURIComponent(name), 'DELETE');
                this.refreshPlaylists();
                this.toasts.push('success', `Playlist "${name}" eliminata`);
            } catch (e) { this.toasts.push('error', `Errore: ${e.message}`); }
        },

        // ===== Spotify auth =====
        async refreshSpotifyAuth() {
            try {
                const data = await this.api('/spotify/auth-status');
                this.applySpotifyStatus(data);
            } catch (e) {}
        },

        applySpotifyStatus(data) {
            if (!data) return;
            const authed = !!data.authenticated;
            const status = data.session_status || 'idle';
            this.spotify.authed = authed;
            this.spotify.warming = authed && status === 'warming';
            this.spotify.ready = authed && status === 'ready';
            this.spotify.failed = authed && status === 'failed';
            this.spotify.zeroconf = data.zeroconf || null;

            if (this.trackType === 'spotify' && !this.spotify.ready) {
                this.trackType = 'local';
            }

            // Fast-poll while warming or zeroconf is running
            const needsFast = (authed && this.spotify.warming) ||
                              (data.zeroconf && data.zeroconf.running && !authed);
            if (needsFast && !this.spotify._fastPoll) {
                this.spotify._fastPoll = setInterval(() => this.refreshSpotifyAuth(), 1500);
            } else if (!needsFast && this.spotify._fastPoll) {
                clearInterval(this.spotify._fastPoll);
                this.spotify._fastPoll = null;
                if (this.spotify.ready && this._wasZeroconfRunning) {
                    this.toasts.push('success', 'Spotify Connect: login completato');
                }
            }
            this._wasZeroconfRunning = data.zeroconf && data.zeroconf.running;
        },

        async startSpotifyZeroconf() {
            this.spotify.startingZeroconf = true;
            try {
                await this.api('/spotify/zeroconf/start', 'POST');
                await this.refreshSpotifyAuth();
            } catch (e) {
                this.toasts.push('error', `Login fallito: ${e.message}`);
            } finally {
                this.spotify.startingZeroconf = false;
            }
        },

        async stopSpotifyZeroconf() {
            try { await this.api('/spotify/zeroconf/stop', 'POST'); } catch (e) {}
            await this.refreshSpotifyAuth();
        },

        // ===== Settings dialog =====
        openSettings() {
            const d = document.getElementById('settingsDialog');
            if (d && !d.open) {
                d.showModal();
                this.settingsOpen = true;
                this.refreshSpotifyAuth();
                this.refreshSessions();
            }
        },
        openSettingsAt(section) {
            this.settingsActiveSection = section;
            this.openSettings();
        },
        async closeSettings() {
            const d = document.getElementById('settingsDialog');
            if (d && d.open) d.close();
            this.settingsOpen = false;
            // Tear down stranded zeroconf bootstrap
            try {
                const data = await this.api('/spotify/auth-status');
                if (data && data.zeroconf && data.zeroconf.running && !data.authenticated) {
                    await this.api('/spotify/zeroconf/stop', 'POST');
                    this.refreshSpotifyAuth();
                }
            } catch (e) {}
        },

        // ===== Theme =====
        toggleTheme() { this.setTheme(this.theme === 'dark' ? 'light' : 'dark'); },
        setTheme(t) {
            this.theme = t;
            document.documentElement.setAttribute('data-theme', t);
            try { localStorage.setItem('theme', t); } catch (e) {}
        },

        // ===== Formatting helpers =====
        formatTime(seconds) {
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return m + ':' + s.toString().padStart(2, '0');
        },
        fmtDuration(ms) {
            if (!ms) return '';
            return Math.floor(ms / 60000) + ':' + String(Math.floor((ms % 60000) / 1000)).padStart(2, '0');
        },
        formatPlayedAt(iso) {
            try { return new Date(iso).toLocaleTimeString(); } catch (e) { return ''; }
        },
        formatRelativeTime(iso) {
            try {
                const then = new Date(iso).getTime();
                const diff = Math.max(0, (Date.now() - then) / 1000);
                if (diff < 60) return 'pochi secondi fa';
                if (diff < 3600) return Math.floor(diff / 60) + ' min fa';
                if (diff < 86400) return Math.floor(diff / 3600) + ' h fa';
                const days = Math.floor(diff / 86400);
                if (days < 7) return days + ' g fa';
                return new Date(iso).toLocaleDateString();
            } catch (e) { return ''; }
        },
        formatArtist(track) {
            const parts = [];
            if (track.artist) parts.push(track.artist);
            if (track.album) parts.push(track.album);
            return parts.join(' — ');
        },
        formatSearchSubtitle(item, source) {
            if (source === 'local')   return '';
            if (source === 'spotify') return [item.artist, item.album].filter(Boolean).join(' — ');
            return item.artist || '';
        },
        sourceLabel(type) {
            return type === 'youtube' ? 'YT'
                 : type === 'spotify' ? 'SP'
                 : type === 'soundcloud' ? 'SC'
                 : 'FILE';
        },
        sourceClass(type) {
            return type === 'youtube' ? 'yt'
                 : type === 'spotify' ? 'sp'
                 : type === 'soundcloud' ? 'sc'
                 : 'file';
        },
    };
}
