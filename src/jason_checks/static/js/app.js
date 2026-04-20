/**
 * CHECKS Terminal Web App — Phase 3
 * Real-time theme grid with top 4 stocks per theme
 */

function timaApp() {
    return {
        // State
        themes: {},
        stocks: {},
        indices: {},  // { "0001": {name, price, change_pct, investor_*}, "1001": {...} }
        wsConnected: false,
        mode: 'paper',
        currentTime: new Date().toLocaleTimeString('ko-KR'),
        ws: null,
        pinnedThemes: new Set(),
        sortMode: 'strength',
        surges: [],
        surgeSortMode: 'change_pct',
        sessionType: 'closed',  // 'pre' | 'regular' | 'after' | 'closed'

        // Methods
        async init() {
            console.log('🚀 CHECKS Terminal Phase 4-A (Pin + Sort)');
            this.updateTime();
            setInterval(() => this.updateTime(), 1000);

            // Load persisted state
            this.loadPinnedThemes();
            this.loadSortMode();
            this.loadSurgeSortMode();

            // Load initial theme data
            await this.loadThemes();
            await this.loadSurges();
            await this.loadIndices();

            // Connect WebSocket
            this.connectWebSocket();

            this.updateSessionType();
            setInterval(() => this.updateSessionType(), 30000);

            // Refresh data every 500ms
            setInterval(() => {
                this.loadThemes();
                this.loadSurges();
            }, 500);
            // Indices refresh every 3s (backend polls every 5s)
            setInterval(() => this.loadIndices(), 3000);
        },

        async loadIndices() {
            try {
                const res = await fetch('/api/indices');
                const data = await res.json();
                if (data.indices) this.indices = data.indices;
            } catch (e) {
                console.error('Failed to load indices:', e);
            }
        },

        formatTrend(amount) {
            // Amount in 원 (can be negative). Output examples: "+10억", "-3억", "+5,200만", "0"
            const n = Number(amount) || 0;
            if (n === 0) return '0';
            const sign = n > 0 ? '+' : '-';
            const abs = Math.abs(n);
            if (abs >= 100000000) {
                return `${sign}${Math.round(abs / 100000000)}억`;
            }
            if (abs >= 10000) {
                return `${sign}${Math.round(abs / 10000)}만`;
            }
            return `${sign}${abs}`;
        },

        loadPinnedThemes() {
            try {
                const stored = localStorage.getItem('tima_pinned_themes');
                this.pinnedThemes = new Set(JSON.parse(stored || '[]'));
            } catch (e) {
                this.pinnedThemes = new Set();
            }
        },

        savePinnedThemes() {
            localStorage.setItem('tima_pinned_themes', JSON.stringify([...this.pinnedThemes]));
        },

        loadSortMode() {
            try {
                this.sortMode = localStorage.getItem('tima_sort_mode') || 'strength';
            } catch (e) {
                this.sortMode = 'strength';
            }
        },

        saveSortMode() {
            localStorage.setItem('tima_sort_mode', this.sortMode);
        },

        isPinned(themeName) {
            return this.pinnedThemes.has(themeName);
        },

        togglePin(themeName) {
            if (this.pinnedThemes.has(themeName)) {
                this.pinnedThemes.delete(themeName);
            } else {
                this.pinnedThemes.add(themeName);
            }
            this.savePinnedThemes();
            console.log(`📌 ${themeName}: ${this.isPinned(themeName) ? 'pinned' : 'unpinned'}`);
        },

        setSortMode(mode) {
            this.sortMode = mode;
            this.saveSortMode();
            this.loadThemes();
            console.log(`🔄 Sort mode: ${mode}`);
        },

        loadSurgeSortMode() {
            try {
                this.surgeSortMode = localStorage.getItem('tima_surge_sort') || 'change_pct';
            } catch (e) {
                this.surgeSortMode = 'change_pct';
            }
        },

        saveSurgeSortMode() {
            localStorage.setItem('tima_surge_sort', this.surgeSortMode);
        },

        setSurgeSortMode(mode) {
            this.surgeSortMode = mode;
            this.saveSurgeSortMode();
            this.loadSurges();
        },

        async loadSurges() {
            try {
                const response = await fetch(`/api/surges?sort=${this.surgeSortMode}&limit=10`);
                const data = await response.json();
                if (data.surges) {
                    // Normalize field names (REST API vs WS fallback may differ)
                    this.surges = data.surges.map(s => ({
                        ...s,
                        cumulative_trading_value: s.cumulative_trading_value || s.trading_value || 0,
                        strength: s.strength || 0,
                    }));
                }
            } catch (error) {
                console.error('Failed to load surges:', error);
            }
        },

        updateTime() {
            const now = new Date();
            const date = now.toLocaleDateString('ko-KR');
            const time = now.toLocaleTimeString('ko-KR');
            this.currentTime = `${date} ${time}`;
        },

        updateSessionType() {
            const now = new Date();
            const dow = now.getDay();
            if (dow === 0 || dow === 6) { this.sessionType = 'closed'; return; }
            const mins = now.getHours() * 100 + now.getMinutes();
            if (mins >= 800 && mins < 850) this.sessionType = 'pre';
            else if (mins >= 900 && mins <= 1530) this.sessionType = 'regular';
            else if (mins > 1530 && mins < 2000) this.sessionType = 'after';
            else this.sessionType = 'closed';
        },

        async loadThemes() {
            try {
                const pinnedStr = [...this.pinnedThemes].join(',');
                const response = await fetch(`/api/themes?sort=${this.sortMode}&pinned=${pinnedStr}`);
                const data = await response.json();

                this.wsConnected = data.ws_connected || false;
                this.mode = data.mode || 'paper';

                // Themes returned in order from backend (pinned first, then top 4)
                if (data.themes) {
                    this.themes = data.themes;
                }
            } catch (error) {
                console.error('Failed to load themes:', error);
            }
        },

        connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const url = `${protocol}//${window.location.host}/ws`;

            console.log(`📡 Connecting to ${url}`);
            this.ws = new WebSocket(url);

            this.ws.onopen = (event) => {
                console.log('✓ WebSocket connected');
                this.wsConnected = true;
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    this.handleMessage(msg);
                } catch (error) {
                    console.error('Failed to parse message:', error);
                }
            };

            this.ws.onerror = (error) => {
                console.error('✗ WebSocket error:', error);
                this.wsConnected = false;
            };

            this.ws.onclose = () => {
                console.log('⚠ WebSocket closed, reconnecting in 3s...');
                this.wsConnected = false;
                setTimeout(() => this.connectWebSocket(), 3000);
            };
        },

        handleMessage(msg) {
            if (msg.type === 'init') {
                console.log('Initialized:', msg);
                this.mode = msg.mode;
                this.wsConnected = msg.ws_connected;
                return;
            }

            if (msg.type === 'tick') {
                // Update stock state from WebSocket tick
                const code = msg.code;
                this.stocks[code] = {
                    code: code,
                    price: msg.price,
                    change_pct: msg.change_pct,
                    cumulative_volume: msg.cumulative_volume,
                    strength: msg.strength,
                    timestamp: this.formatTime(msg.timestamp),
                    surge_active: this.stocks[code]?.surge_active || false,
                };

                console.log(`💹 ${code}: ${msg.price} (${msg.change_pct >= 0 ? '+' : ''}${msg.change_pct.toFixed(2)}%)`);

                // Update themes with new tick data
                this.updateThemesWithTick(code);
                return;
            }

            if (msg.type === 'surge') {
                // Handle surge detection
                const code = msg.code;
                console.log(`⚡ SURGE: ${code} @ ${msg.price} | Vol: ${msg.volume}`);

                // Mark as surge active
                if (this.stocks[code]) {
                    this.stocks[code].surge_active = true;
                }

                // Clear surge flag after 30 seconds
                setTimeout(() => {
                    if (this.stocks[code]) {
                        this.stocks[code].surge_active = false;
                    }
                }, 30000);

                return;
            }
        },

        formatTime(hhmmss) {
            // Format: 092030 -> 09:20:30
            if (!hhmmss || hhmmss.length < 6) return '-';
            return `${hhmmss.substring(0, 2)}:${hhmmss.substring(2, 4)}:${hhmmss.substring(4, 6)}`;
        },

        updateThemesWithTick(code) {
            // Update the stock data in each theme
            for (const themeName in this.themes) {
                const theme = this.themes[themeName];
                if (theme.leaders) {
                    for (const leader of theme.leaders) {
                        if (leader.code === code && this.stocks[code]) {
                            // Update leader with latest tick, preserving surge_active
                            const surgeState = leader.surge_active;
                            Object.assign(leader, this.stocks[code]);
                            if (surgeState) leader.surge_active = surgeState;
                        }
                    }
                    // Recompute theme strength
                    theme.strength = this.computeThemeStrength(theme.leaders);
                }
            }
        },

        computeThemeStrength(leaders) {
            // Average leader score (change_pct + strength)
            if (!leaders || leaders.length === 0) return 0;

            let totalScore = 0;
            for (const leader of leaders) {
                const changeScore = Math.abs(leader.change_pct || 0) * 10;
                const strengthScore = (leader.strength || 0) * 50;
                totalScore += changeScore + strengthScore;
            }

            return totalScore / leaders.length;
        },

        marketOpen() {
            const now = new Date();
            const dow = now.getDay();
            const hours = now.getHours();
            const minutes = now.getMinutes();

            // Weekend (Sat=6, Sun=0)
            if (dow === 0 || dow === 6) return false;

            // 09:00 ~ 15:30
            const startTime = 9 * 60;
            const endTime = 15 * 60 + 30;
            const currentMinutes = hours * 60 + minutes;

            return currentMinutes >= startTime && currentMinutes <= endTime;
        },
    };
}
