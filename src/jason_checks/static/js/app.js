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
        market: 'KR',
        currentTab: 'dashboard',
        scanning: false,
        scanResults: { a: [], b: [], c: [] },
        alphaforgeCandidatesLoaded: 0,
        alphaforgeCandidatesGeneratedAt: '',
        copyStatus: 'idle',
        signalSaveStatus: 'idle',

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

        async setMarket(m) {
            if (this.market === m) return;
            console.log(`🌐 Switching market to: ${m}`);
            try {
                const res = await fetch('/api/market', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ market: m })
                });
                if (res.ok) {
                    this.market = m;
                    // Clear data to prevent flicker
                    this.themes = {};
                    this.stocks = {};
                    this.surges = [];
                    // Immediate reload
                    this.updateSessionType();
                    await Promise.all([
                        this.loadThemes(),
                        this.loadSurges(),
                        this.loadIndices()
                    ]);
                }
            } catch (e) {
                console.error('Failed to switch market:', e);
            }
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
            } else if (abs >= 10000) {
                return `${sign}${Math.round(abs / 10000).toLocaleString()}만`;
            } else {
                return `${sign}${abs.toLocaleString()}`;
            }
        },

        formatSupplyFlow(stock, key) {
            if (!stock || stock.supply_status !== 'OK') return 'DATA_NA';
            const value = stock[key];
            if (value === null || value === undefined || value === '') return 'DATA_NA';
            return this.formatTrend(value);
        },

        supplyFlowClass(stock, key) {
            if (!stock || stock.supply_status !== 'OK') return 'text-gray-400';
            const value = Number(stock[key]);
            if (!Number.isFinite(value)) return 'text-gray-400';
            if (value > 0) return 'up';
            if (value < 0) return 'down';
            return 'text-gray-500';
        },

        formatIndexSupplyFlow(index, key) {
            if (!index) return 'DATA_NA';
            const value = index[key];
            if (value === null || value === undefined || value === '' || Number(value) === 0) {
                return 'DATA_NA';
            }
            return this.formatTrend(value);
        },

        indexSupplyClass(index, key) {
            const value = Number(index?.[key]);
            if (!Number.isFinite(value) || value === 0) return 'text-gray-400';
            return value > 0 ? 'up' : 'down';
        },

        formatBoxPrice(value) {
            if (value === null || value === undefined || value === '') return '-';
            const n = Number(value);
            if (!Number.isFinite(n) || n <= 0) return '-';
            return n.toLocaleString('ko-KR');
        },

        formatDecisionItems(items) {
            if (!items || items.length === 0) return '-';
            if (Array.isArray(items)) return items.slice(0, 3).join(' · ');
            return String(items);
        },

        horizonLabelKo(label) {
            const labels = {
                OVERLAP: '단기+중기',
                SHORT_SWING: '단기스윙',
                SHORT_WATCH: '단기관찰',
                CHASE_RISK: '강하지만 추격주의',
                POSITION_SWING: '중기스윙',
                WATCH_ONLY: '관찰',
                RISK_ONLY: '위험/제외',
            };
            return labels[label] || label || '-';
        },

        intradayDecision(stock) {
            const reasons = [];
            const failed_conditions = [];
            const box = Number(stock.box_upper_price);
            const price = Number(stock.price) || 0;
            const strength = Number(stock.strength) || 0;
            const tradingValue = Number(stock.cumulative_trading_value) || 0;
            const tradingValueOk = tradingValue >= 1000000000;

            if (this.sessionType === 'closed') {
                return {
                    decision: 'MARKET_CLOSED',
                    reasons: ['장중판정 비활성'],
                    failed_conditions: [],
                };
            }

            if (!price) {
                return {
                    decision: 'DATA_WAIT',
                    reasons: ['현재가 대기'],
                    failed_conditions: ['현재가 없음'],
                };
            }

            if (!Number.isFinite(box) || box <= 0) {
                return {
                    decision: 'DATA_WAIT',
                    reasons: ['BOX 기준가 대기'],
                    failed_conditions: ['box_upper_price 없음'],
                };
            }

            if (stock.vcp_status === 'RALLY_EXHAUSTION') {
                reasons.push('추격주의');
            }
            if (stock.alert_type === 'ACTION_ALERT') {
                reasons.push('우선관찰');
            }

            if (price <= box) {
                reasons.unshift(`현재가가 BOX ${this.formatBoxPrice(box)} 아래`);
                return {
                    decision: 'IN_BOX_WAIT',
                    reasons,
                    failed_conditions: ['BOX 돌파 전'],
                };
            }

            reasons.unshift(`BOX ${this.formatBoxPrice(box)} 돌파`);
            if (strength >= 140 && tradingValueOk) {
                reasons.push(`체결강도 ${strength.toFixed(0)}`);
                reasons.push('거래대금 양호');
                return { decision: 'ENTRY_OK', reasons, failed_conditions };
            }
            if (strength >= 120) {
                reasons.push(`체결강도 ${strength.toFixed(0)}`);
                if (strength < 140) failed_conditions.push('체결강도 140 미만');
                if (!tradingValueOk) failed_conditions.push('거래대금 부족');
                return { decision: 'BREAKOUT_WATCH', reasons, failed_conditions };
            }

            reasons.push(`체결강도 ${strength.toFixed(0)}`);
            failed_conditions.push('체결강도 120 미만');
            return { decision: 'FAKEOUT_RISK', reasons, failed_conditions };
        },

        alphaForgePicks() {
            const picks = [];
            for (const [themeName, theme] of Object.entries(this.themes || {})) {
                const isAlphaForge = themeName === 'AlphaForge' || theme?.display_name === 'AlphaForge';
                if (!isAlphaForge) continue;
                for (const stock of theme.leaders || []) {
                    const live = this.stocks[stock.code] || {};
                    const merged = { ...stock, ...live };
                    picks.push({
                        ...merged,
                        horizon_setup_label: this.alphaForgeHorizonLabel(merged),
                    });
                }
            }
            return picks;
        },

        alphaForgeHorizonLabel(stock) {
            const decision = this.intradayDecision(stock).decision;
            if (decision === 'IN_BOX_WAIT' || decision === 'MARKET_CLOSED' || decision === 'DATA_WAIT') {
                return 'POSITION_SETUP';
            }
            if (decision === 'ENTRY_OK') return 'SWING_READY';
            if (decision === 'FAKEOUT_RISK' || stock.vcp_status === 'RALLY_EXHAUSTION') return 'CHASE_RISK';
            return 'SWING_WATCH';
        },

        liveMomentumPicks() {
            const alphaByCode = new Map(this.alphaForgePicks().map(s => [s.code, s]));
            const rows = Object.values(this.stocks || []).map(stock => {
                const alpha = alphaByCode.get(stock.code) || {};
                const merged = { ...alpha, ...stock, name: alpha.name || stock.name || stock.code };
                return {
                    ...merged,
                    horizon_label: this.liveMomentumLabel(merged),
                };
            }).filter(stock => stock.horizon_label);

            rows.sort((a, b) => {
                const av = Number(a.cumulative_trading_value) || 0;
                const bv = Number(b.cumulative_trading_value) || 0;
                return bv - av;
            });
            return rows.slice(0, 8);
        },

        liveMomentumLabel(stock) {
            const changePct = Number(stock.change_pct) || 0;
            const strength = Number(stock.strength) || 0;
            const tradingValue = Number(stock.cumulative_trading_value) || 0;
            const hasTradingValue = tradingValue > 0;

            if (stock.vcp_status === 'RALLY_EXHAUSTION' && changePct >= 3) return 'CHASE_RISK';
            if (changePct >= 3 && hasTradingValue && strength < 100) return 'CHASE_RISK';
            if (changePct >= 3 && hasTradingValue && strength >= 120) return 'SWING_READY';
            if (changePct >= 3 && hasTradingValue && strength >= 100) return 'SWING_WATCH';
            return '';
        },

        overlapPicks() {
            const alphaCodes = new Set(this.alphaForgePicks().map(stock => stock.code));
            return this.liveMomentumPicks()
                .filter(stock => alphaCodes.has(stock.code) && stock.horizon_label === 'SWING_READY')
                .map(stock => ({ ...stock, horizon_label: 'OVERLAP_LEADER' }));
        },

        copyButtonText() {
            if (this.copyStatus === 'success') return '복사완료';
            if (this.copyStatus === 'error') return '복사실패';
            return '전체복사';
        },

        buildDashboardCopyText() {
            const lines = [];
            lines.push('CHECKS Terminal Dashboard');
            lines.push(`현재 시간: ${this.currentTime}`);
            lines.push(`SESSION: ${this.sessionType.toUpperCase()}`);
            lines.push(`MARKET: ${this.market} / ${this.marketOpen() ? 'MARKET OPEN' : 'MARKET CLOSED'}`);
            lines.push(`AlphaForge 후보 로드: ${this.alphaforgeCandidatesLoaded}개 / ${this.alphaforgeCandidatesGeneratedAt || '-'}`);
            lines.push('');
            lines.push('[Market Indices]');
            const indexEntries = Object.entries(this.indices || {});
            if (indexEntries.length === 0) {
                lines.push('지수 없음');
            }
            for (const [code, index] of indexEntries) {
                lines.push([
                    `${index.name || code} (${code})`,
                    `외 ${this.formatIndexSupplyFlow(index, 'investor_foreigner')}`,
                    `기 ${this.formatIndexSupplyFlow(index, 'investor_institution')}`,
                    `개 ${this.formatIndexSupplyFlow(index, 'investor_individual')}`,
                ].join(' / '));
            }
            lines.push('');
            lines.push('[AlphaForge Picks]');

            const picks = this.alphaForgePicks();
            if (picks.length === 0) {
                lines.push('후보 없음');
            }
            for (const stock of picks) {
                const decision = this.intradayDecision(stock);
                const price = stock.price ? Number(stock.price).toLocaleString('ko-KR') : '-';
                const changePct = `${(Number(stock.change_pct) || 0).toFixed(2)}%`;
                const strength = `${(Number(stock.strength) || 0).toFixed(0)}`;
                const tradingValue = stock.cumulative_trading_value
                    ? `${(Number(stock.cumulative_trading_value) / 100000000).toFixed(0)}억`
                    : '-';
                lines.push(
                    [
                        `${stock.name || stock.code} (${stock.code})`,
                        `Tier ${stock.tier || '-'}`,
                        `alert ${stock.alert_type || '-'}`,
                        `RS ${stock.rs ?? '-'}`,
                        `VCP ${stock.vcp_status || '-'}`,
                        `BOX ${this.formatBoxPrice(stock.box_upper_price)}`,
                        `단기 점수 ${stock.short_swing_score ?? '-'}`,
                        `중기 점수 ${stock.position_swing_score ?? '-'}`,
                        `Horizon ${this.horizonLabelKo(stock.horizon_label)}`,
                        `단기 사유 ${this.formatDecisionItems(stock.short_reasons)}`,
                        `중기 사유 ${this.formatDecisionItems(stock.position_reasons)}`,
                        `수급상태 ${stock.supply_status || 'DATA_NA'}`,
                        `수급시각 ${stock.supply_updated_at || '-'}`,
                        `외 ${this.formatSupplyFlow(stock, 'foreign_flow')}`,
                        `기 ${this.formatSupplyFlow(stock, 'institution_flow')}`,
                        `개 ${this.formatSupplyFlow(stock, 'individual_flow')}`,
                        `DECISION ${decision.decision}`,
                        `사유 ${this.formatDecisionItems(decision.reasons)}`,
                        `현재가 ${price}`,
                        `등락률 ${changePct}`,
                        `체결강도 ${strength}`,
                        `거래대금 ${tradingValue}`,
                    ].join(' / ')
                );
            }

            return lines.join('\n');
        },

        async copyDashboardText() {
            try {
                await navigator.clipboard.writeText(this.buildDashboardCopyText());
                this.copyStatus = 'success';
            } catch (error) {
                console.error('Failed to copy dashboard:', error);
                this.copyStatus = 'error';
            }
            setTimeout(() => {
                this.copyStatus = 'idle';
            }, 1500);
        },

        signalSaveButtonText() {
            if (this.signalSaveStatus === 'success') return '저장완료';
            if (this.signalSaveStatus === 'error') return '저장실패';
            return '신호저장';
        },

        async saveSignalJournal() {
            try {
                const response = await fetch('/api/signal-journal', { method: 'POST' });
                const data = await response.json();
                this.signalSaveStatus = data.ok ? 'success' : 'error';
            } catch (error) {
                console.error('Failed to save signal journal:', error);
                this.signalSaveStatus = 'error';
            }
            setTimeout(() => {
                this.signalSaveStatus = 'idle';
            }, 1500);
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
            
            const hours = now.getHours();
            const hhmm = hours * 100 + now.getMinutes();

            if (this.market === 'KR') {
                if (hhmm >= 800 && hhmm < 850) this.sessionType = 'pre';
                else if (hhmm >= 900 && hhmm <= 1530) this.sessionType = 'regular';
                else if (hhmm > 1530 && hhmm < 2000) this.sessionType = 'after';
                else this.sessionType = 'closed';
            } else {
                // US Market (KST approximation)
                if (hhmm >= 1700 && hhmm < 2230) this.sessionType = 'pre';
                else if (hhmm >= 2230 || hhmm < 500) this.sessionType = 'regular';
                else if (hhmm >= 500 && hhmm < 900) this.sessionType = 'after';
                else this.sessionType = 'closed';
            }
        },

        async loadThemes() {
            try {
                const pinnedStr = [...this.pinnedThemes].join(',');
                const response = await fetch(`/api/themes?sort=${this.sortMode}&pinned=${pinnedStr}`);
                const data = await response.json();

                this.wsConnected = data.ws_connected || false;
                this.mode = data.mode || 'paper';
                this.alphaforgeCandidatesLoaded = data.alphaforge_candidates_loaded || 0;
                this.alphaforgeCandidatesGeneratedAt = data.alphaforge_candidates_generated_at || '';

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
                    cumulative_trading_value: msg.cumulative_trading_value,
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

        async runScan() {
            if (this.scanning) return;
            console.log('🔍 Starting value investment scan...');
            this.scanning = true;
            try {
                const response = await fetch('/api/scan');
                const data = await response.json();
                if (data.results) {
                    this.scanResults = data.results;
                    console.log('✅ Scan complete:', this.scanResults);
                }
            } catch (error) {
                console.error('Failed to run scan:', error);
            } finally {
                this.scanning = false;
            }
        },

        marketOpen() {
            const now = new Date();
            const dow = now.getDay();
            const hours = now.getHours();
            const mins = hours * 60 + now.getMinutes();

            // Weekend (Sat=6, Sun=0)
            if (dow === 0 || dow === 6) return false;

            if (this.market === 'KR') {
                // 09:00 ~ 15:30
                return mins >= 9 * 60 && mins <= (15 * 60 + 30);
            } else {
                // US Market (KST approximation: 22:30 ~ 05:00)
                // Also account for 18:00 (Pre) ~ 09:00 (After)
                return mins >= 22 * 60 + 30 || mins <= 5 * 60;
            }
        },
    };
}
