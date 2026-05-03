// =====================================================================
// DiscoBot — interfaccia pubblica
// =====================================================================

function discoBotPublic() {
    return {
        connected: false,
        config: { enabled: false, require_approval: true, sources: {} },
        player: { isPlaying: false, track: null, position: 0, duration: 0 },
        queue: [],
        myPending: [],

        // Toasts (stesso pattern del manager)
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

        theme: document.documentElement.getAttribute('data-theme') || 'dark',
        nameInput: '',
        firstVisit: false,

        // Search state
        search: {
            q: '', loading: false, loadingMore: false, searched: false,
            tabs: [], activeTab: '', pageSize: 10,
        },

        get currentSearchItems() {
            const t = this.search.tabs.find(t => t.key === this.search.activeTab);
            return t ? t.items : [];
        },

        get hasMore() {
            const t = this.search.tabs.find(t => t.key === this.search.activeTab);
            return t ? t.hasMore : false;
        },

        get displayName() {
            const stored = this._getCookie('discobot_pname');
            return stored && stored.trim() ? stored.trim() : 'Anonimo';
        },

        async init() {
            // Polling state ogni 2s, config ogni 10s (in caso il manager cambi modalità)
            await this._fetchConfig();
            this._fetchState();
            this._fetchMyPending();
            setInterval(() => this._fetchState(), 2000);
            setInterval(() => this._fetchConfig(), 10000);
            setInterval(() => this._fetchMyPending(), 5000);

            // Prima visita: chiedi il nome (o "Anonimo") se non ha mai scelto.
            // Mostriamo il dialog solo se l'interfaccia e' attiva — niente
            // senso chiederlo quando il DJ l'ha disabilitata.
            if (this.config.enabled && !this._getCookie('discobot_pname_chosen')) {
                this.firstVisit = true;
                this.openNameDialog();
            }
        },

        async _api(url, opts = {}) {
            const res = await fetch(url, { credentials: 'same-origin', ...opts });
            if (!res.ok) {
                let msg = `${res.status}`;
                try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
                const err = new Error(msg);
                err.status = res.status;
                throw err;
            }
            return res.json();
        },

        async _fetchConfig() {
            try {
                this.config = await this._api('/public/config');
            } catch (e) {}
        },

        async _fetchState() {
            try {
                const s = await this._api('/public/state');
                this.player.track = s.current_track;
                this.player.position = s.position;
                this.player.duration = s.duration;
                this.player.isPlaying = s.is_playing;
                this.queue = s.queue || [];
                this.connected = true;
            } catch (e) {
                this.connected = false;
            }
        },

        async _fetchMyPending() {
            if (!this.config.enabled || !this.config.require_approval) {
                this.myPending = [];
                return;
            }
            try {
                const data = await this._api('/public/my-pending');
                this.myPending = data.items || [];
            } catch (e) {}
        },

        async doSearch() {
            const q = this.search.q.trim();
            if (!q) return;
            this.search.loading = true;
            this.search.searched = true;
            this.search.tabs = [];
            try {
                const data = await this._api(`/public/search?q=${encodeURIComponent(q)}&limit=${this.search.pageSize}&offset=0`);
                this.search.tabs = this._buildTabs(data, this.search.pageSize, 0);
                this.search.activeTab = this.search.tabs[0]?.key || '';
            } catch (e) {
                this.toasts.push('error', 'Errore nella ricerca');
            } finally {
                this.search.loading = false;
            }
        },

        async loadMore() {
            const tab = this.search.tabs.find(t => t.key === this.search.activeTab);
            if (!tab || !tab.hasMore || this.search.loadingMore) return;
            this.search.loadingMore = true;
            const offset = tab.items.length;
            try {
                // /public/search filtra già per sources permesse: però per "load more"
                // su una singola sorgente passiamo direttamente quel filtro lato backend
                // così ridurre il costo (yt-dlp non viene rieseguito sulle altre).
                const data = await this._api(
                    `/public/search?q=${encodeURIComponent(this.search.q)}&limit=${this.search.pageSize}&offset=${offset}`
                );
                this.search.tabs = this._buildTabs(data, this.search.pageSize, offset, this.search.tabs, tab.key);
            } catch (e) {
                this.toasts.push('error', 'Errore nel caricamento');
            } finally {
                this.search.loadingMore = false;
            }
        },

        _buildTabs(data, limit, keyOffset, existing = null, onlySource = null) {
            const cfg = [
                { key: 'local',     label: 'File',       prefix: 'l', extract: d => d.local || [] },
                { key: 'youtube',   label: 'YouTube',    prefix: 'y', extract: d => d.youtube || [] },
                { key: 'spotify',   label: 'Spotify',    prefix: 's', extract: d => d.spotify || [] },
                { key: 'soundcloud',label: 'SoundCloud', prefix: 'c', extract: d => d.soundcloud || [] },
            ];
            const out = [];
            for (const s of cfg) {
                const ex = existing?.find(t => t.key === s.key);
                if (onlySource && s.key !== onlySource) {
                    if (ex) out.push(ex);
                    continue;
                }
                const newItems = s.extract(data).map((r, i) => ({
                    ...r, _key: s.prefix + (keyOffset + i), title: r.title || r.name,
                }));
                const all = ex ? [...ex.items, ...newItems] : newItems;
                if (all.length === 0 && !ex) continue;
                out.push({
                    key: s.key, label: s.label, items: all,
                    hasMore: newItems.length >= limit && all.length < 200,
                });
            }
            return out;
        },

        async requestItem(item, source) {
            const body = {
                requester_name: this.displayName === 'Anonimo' ? '' : this.displayName,
                preview_title: item.title,
            };
            if (source === 'local') {
                body.path = item.filename;
                body.type = 'local';
            } else if (source === 'spotify') {
                body.path = item.spotify_id;
                body.type = 'spotify';
            } else {
                body.path = item.url;
                body.type = source;
            }
            try {
                const res = await this._api('/public/queue/add', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (res.status === 'pending') {
                    this.toasts.push('success', `Richiesta inviata: il DJ deve ancora approvarla`);
                    this._fetchMyPending();
                } else if (res.status === 'queued') {
                    this.toasts.push('success', `Aggiunto in coda: ${item.title}`);
                }
            } catch (e) {
                if (e.status === 429) {
                    this.toasts.push('warning', e.message);
                } else if (e.status === 503) {
                    this.toasts.push('error', 'Interfaccia momentaneamente disabilitata');
                } else {
                    this.toasts.push('error', `Errore: ${e.message}`);
                }
            }
        },

        // ---- Name dialog ----
        openNameDialog() {
            this.nameInput = this._getCookie('discobot_pname') || '';
            const d = document.getElementById('nameDialog');
            if (d && !d.open) d.showModal();
        },
        closeNameDialog() {
            const d = document.getElementById('nameDialog');
            if (d && d.open) d.close();
            // Sia che l'utente confermi sia che chiuda con ESC/backdrop,
            // segniamo che ha visto il prompt — non lo riapriamo al refresh.
            this._setCookie('discobot_pname_chosen', '1', 365);
            this.firstVisit = false;
        },
        saveName() {
            const n = (this.nameInput || '').trim().slice(0, 30);
            if (n) {
                this._setCookie('discobot_pname', n, 365);
            } else {
                this._setCookie('discobot_pname', '', -1);
            }
            this.closeNameDialog();
        },
        clearName() {
            this._setCookie('discobot_pname', '', -1);
            this.nameInput = '';
            this.closeNameDialog();
        },

        // ---- Theme ----
        toggleTheme() {
            this.theme = this.theme === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', this.theme);
            try { localStorage.setItem('theme', this.theme); } catch (e) {}
        },

        // ---- Cookie helpers ----
        _getCookie(name) {
            const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
            return m ? decodeURIComponent(m[1]) : '';
        },
        _setCookie(name, value, days) {
            const d = new Date();
            d.setTime(d.getTime() + days * 86400000);
            document.cookie = `${name}=${encodeURIComponent(value)};expires=${d.toUTCString()};path=/;SameSite=Lax`;
        },

        // ---- Formatters ----
        formatTime(seconds) {
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return m + ':' + s.toString().padStart(2, '0');
        },
        fmtDuration(ms) {
            if (!ms) return '';
            return Math.floor(ms / 60000) + ':' + String(Math.floor((ms % 60000) / 1000)).padStart(2, '0');
        },
        formatRelativeTime(iso) {
            try {
                const then = new Date(iso).getTime();
                const diff = Math.max(0, (Date.now() - then) / 1000);
                if (diff < 60) return 'pochi secondi fa';
                if (diff < 3600) return Math.floor(diff / 60) + ' min fa';
                return Math.floor(diff / 3600) + ' h fa';
            } catch (e) { return ''; }
        },
        formatArtist(track) {
            const parts = [];
            if (track.artist) parts.push(track.artist);
            if (track.album) parts.push(track.album);
            return parts.join(' — ');
        },
        formatSearchSubtitle(item, source) {
            if (source === 'local') return '';
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
