/**
 * CHECKS Terminal Web App — Phase 3
 * Real-time theme grid with top 4 stocks per theme
 */

const ALPHAFORGE_TRACKING_LABELS = new Set([
    'ACTION_ALERT',
    'PRIORITY_WATCH',
    'NEAR_BUY',
    'BUY_CANDIDATE',
]);

function getAlphaForgeLabels(stock) {
    if (!stock) return [];
    return [
        stock.alert_type,
        stock.watch_alert_type,
        stock.legacy_label,
        stock.final_label,
        stock.display_label,
        stock.display_watch_alert_type,
    ].filter(Boolean);
}

function isAlphaForgeTrackingCandidate(stock) {
    return getAlphaForgeLabels(stock).some(label => ALPHAFORGE_TRACKING_LABELS.has(label));
}

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
        sortMode: 'default',
        themeStableOrder: [],
        themeSortOrder: [],
        quotePolling: {},
        supplyDataReason: '',
        supplyPolling: {},
        surges: [],
        surgeSortMode: 'change_pct',
        sessionType: 'closed',  // 'pre' | 'regular' | 'after' | 'closed'
        market: 'KR',
        currentTab: 'dashboard',
        scanning: false,
        scanResults: { a: [], b: [], c: [] },
        alphaforgeCandidatesLoaded: 0,
        alphaforgeCandidatesGeneratedAt: '',
        alphaforgePicksData: [],
        themeLoadStatus: 'ok',
        themeLoadReason: '',
        chartHistory: { stocks: {}, indices: {} },
        copyStatus: 'idle',
        signalSaveStatus: 'idle',
        // US Portfolio Watch state (only fetched when market === 'US')
        usWatchlist: [],
        usWatchlistUpdatedAt: '',
        usWatchlistSession: '',
        // KR Sector Leaders state (only fetched when market === 'KR')
        krSectorLeaders: [],
        krSectorLeadersUpdatedAt: '',
        // Decision Engine state
        decisionCounts: { BUY_NOW: 0, STARTER_POSITION: 0, CONDITIONAL_BUY: 0, WATCH_ONLY: 0, AVOID: 0 },
        decisionSession: '',
        decisionMarketGate: {},
        decisionSetupTop3: [],
        decisionQualitySummary: {},
        dataConfidenceCounts: {},
        reasonCodeCounts: {},
        marketGateLevel: '',
        marketGateReason: '',
        marketGateBlocksBuyNow: false,
        journalStatus: {},
        sectorAuditWarnings: [],
        duplicatedSymbols: [],
        suspiciousSectorMembers: [],
        forwardTestSummary: null,

        // Methods
        async init() {
            console.log('🚀 CHECKS Terminal Phase 4-A (Pin + Sort)');
            this.updateTime();
            setInterval(() => this.updateTime(), 1000);

            // Load persisted state
            this.loadPinnedThemes();
            this.loadSortMode();
            this.loadSurgeSortMode();

            // ── Market persistence: URL query > localStorage > default KR ──
            const targetMarket = this._getInitialMarket();
            if (targetMarket !== this.market) {
                try {
                    const res = await fetch('/api/market', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ market: targetMarket })
                    });
                    if (res.ok) {
                        this.market = targetMarket;
                        localStorage.setItem('CHECKS_SELECTED_MARKET', targetMarket);
                    }
                } catch (e) {
                    console.warn('Market restore failed:', e);
                }
            }

            // Load initial theme data
            await this.loadThemes();
            await this.loadSurges();
            await this.loadIndices();
            if (this.market === 'US') await this.loadUsWatchlist();
            if (this.market === 'KR') await this.loadKrSectorLeaders();

            // Connect WebSocket
            this.connectWebSocket();

            this.updateSessionType();
            setInterval(() => this.updateSessionType(), 30000);

            // Refresh dashboard data at a steady cadence without starving REST polling.
            setInterval(() => {
                this.loadThemes();
                this.loadSurges();
            }, 2000);
            // Indices refresh every 3s (backend polls every 5s)
            setInterval(() => this.loadIndices(), 3000);
            // US Portfolio Watch refresh every 5s when market === 'US'
            setInterval(() => {
                if (this.market === 'US') this.loadUsWatchlist();
            }, 5000);
            // KR Sector Leaders refresh every 3s when market === 'KR'
            setInterval(() => {
                if (this.market === 'KR') this.loadKrSectorLeaders();
            }, 3000);

            // ── Focus / visibility refresh (background tab 복귀 시 즉시 갱신) ──
            this._setupFocusRefresh();
        },

        async loadUsWatchlist() {
            try {
                const res = await fetch('/api/us/watchlist');
                if (!res.ok) return;
                const data = await res.json();
                if (Array.isArray(data.rows)) {
                    this.usWatchlist = data.rows;
                    this.usWatchlistUpdatedAt = data.updated_at || '';
                    this.usWatchlistSession = data.session_status || '';
                    // Sync sessionType from API for US market (more reliable than client clock)
                    if (this.market === 'US' && this.usWatchlistSession) {
                        const _SM = {
                            REGULAR: 'regular', PRE_MARKET: 'pre',
                            AFTER_MARKET: 'after', MARKET_CLOSED: 'closed',
                        };
                        this.sessionType = _SM[this.usWatchlistSession] || 'closed';
                    }
                }
            } catch (e) {
                console.error('Failed to load US watchlist:', e);
            }
        },

        async loadKrSectorLeaders() {
            try {
                const res = await fetch('/api/kr/sector-leaders');
                if (!res.ok) return;
                const data = await res.json();
                if (Array.isArray(data.sectors)) {
                    this.krSectorLeaders = data.sectors;
                    this.krSectorLeadersUpdatedAt = data.updated_at || '';
                }
            } catch (e) {
                console.error('Failed to load KR sector leaders:', e);
            }
        },

        sectorStatusClass(level) {
            if (level === 'RISK')      return 'text-red-600';
            if (level === 'INFO')      return 'text-blue-600';
            if (level === 'DATA_WAIT') return 'text-gray-400';
            return 'text-gray-500';
        },

        sectorStatusIcon(level, type) {
            if (level === 'RISK')              return '⚠️';
            if (type  === 'STRONG_UP')         return '📈';
            if (level === 'DATA_WAIT')         return '⏳';
            return '·';
        },

        usEventColorClass(level) {
            if (level === 'RISK') return 'text-red-600';
            if (level === 'INFO') return 'text-blue-600';
            if (level === 'OFF') return 'text-gray-400';
            return 'text-gray-500';
        },

        // ── Null-safe display helpers ─────────────────────────────────────────
        // Prevents null/undefined/NaN showing as "0" or "0.00%".
        // Only actual finite numbers are rendered as numbers; everything else → '-'.

        _isVal(v) {
            return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
        },
        // ±XX.XX% format (for change_pct inline)
        fmtPct(v, digits = 2) {
            if (!this._isVal(v)) return '-';
            const n = Number(v);
            return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}%`;
        },
        // ▲/▼ XX.XX% format (KR index / stock change)
        fmtAbsPct(v, digits = 2) {
            if (!this._isVal(v)) return '-';
            const n = Number(v);
            return `${n >= 0 ? '▲' : '▼'} ${Math.abs(n).toFixed(digits)}%`;
        },
        // Integer strength (toFixed(0))
        fmtStr(v) {
            if (!this._isVal(v)) return '-';
            const n = Number(v);
            if (n <= 0) return '-';
            return n.toFixed(0);
        },
        // Float strength (toFixed(1)) for theme-level display
        fmtStr1(v) {
            if (!this._isVal(v)) return '-';
            return Number(v).toFixed(1);
        },

        getThemeMinMaxLog() {
            const strengths = Object.values(this.themes || {})
                .map(t => Number(t?.strength))
                .filter(s => this._isVal(s) && !isNaN(s) && s >= 0);
            if (strengths.length === 0) return { min: 0, max: 0 };
            const logs = strengths.map(s => Math.log1p(s));
            return {
                min: Math.min(...logs),
                max: Math.max(...logs)
            };
        },

        fmtThemeStrength(v) {
            if (!this._isVal(v)) return '-';
            const raw = Number(v);
            if (isNaN(raw) || raw < 0) return '-';

            const { min, max } = this.getThemeMinMaxLog();
            if (max === min) return '50';

            const logVal = Math.log1p(raw);
            let val = ((logVal - min) / (max - min)) * 99 + 1;
            if (val < 1) val = 1;
            if (val > 100) val = 100;
            return Math.round(val).toFixed(0);
        },

        fmtThemeDirection(changePct) {
            if (!this._isVal(changePct)) return '중립';
            const pct = Number(changePct);
            if (pct >= 1.0) return '강세';
            if (pct <= -1.0) return '약세';
            return '중립';
        },

        formatUsPrice(p) {
            if (p === null || p === undefined || p === '') return '-';
            const n = Number(p);
            if (!Number.isFinite(n) || n <= 0) return '-';
            return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        },

        formatUsChange(c) {
            if (c === null || c === undefined || c === '') return '-';
            const n = Number(c);
            if (!Number.isFinite(n)) return '-';
            return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
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
                    localStorage.setItem('CHECKS_SELECTED_MARKET', m);
                    // No pre-clear: Alpine x-if guards hide stale market data automatically.
                    // Clearing before fetch would show "0" placeholders during the reload.
                    // Immediate reload
                    this.updateSessionType();
                    const promises = [this.loadThemes(), this.loadSurges(), this.loadIndices()];
                    if (m === 'US') promises.push(this.loadUsWatchlist());
                    if (m === 'KR') promises.push(this.loadKrSectorLeaders());
                    await Promise.all(promises);
                }
            } catch (e) {
                console.error('Failed to switch market:', e);
            }
        },

        async loadIndices() {
            try {
                const res = await fetch('/api/indices');
                const data = await res.json();
                if (data.indices) {
                    this.indices = data.indices;
                    for (const [code, index] of Object.entries(this.indices || {})) {
                        this.recordChartPoint('indices', code, index?.price, index?.change_pct);
                    }
                }
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

        formatTradingValue(value) {
            const n = Number(value);
            if (!Number.isFinite(n) || n <= 0) return '-';
            if (n >= 1000000000000) return `${(n / 1000000000000).toFixed(1)}조`;
            if (n >= 100000000) return `${Math.round(n / 100000000)}억`;
            if (n >= 10000) return `${Math.round(n / 10000).toLocaleString('ko-KR')}만`;
            return n.toLocaleString('ko-KR');
        },

        formatStrength(value) {
            const n = Number(value);
            if (!Number.isFinite(n) || n <= 0) return '-';
            return n.toFixed(0);
        },

        supplyBadge(stock) {
            const status = stock?.supply_status || stock?.de_supply_status || 'DATA_NA';
            const recency = stock?.supply_recency || stock?.de_supply_recency || 'UNKNOWN';
            const source = stock?.supply_source || stock?.de_supply_source || '';

            if (status === 'OK') {
                if (recency === 'TODAY') return '당일수급';
                if (recency === 'PREV_DAY') return '전일수급';
                return '수급 확인';
            }
            if (status === 'ERROR') return '수급 오류';
            if (status === 'RATE_LIMIT') return '수급 제한';

            if (status === 'DATA_NA') {
                if (source === 'KIS') return '수급 미확인';
                return '수급 미조회';
            }
            return '수급 미확인';
        },

        formatClock(value) {
            if (!value) return '-';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return '-';
            return date.toLocaleTimeString('ko-KR', { hour12: false });
        },

        quoteLoadText() {
            const q = this.quotePolling || {};
            const total = Number(q.total) || 0;
            const success = Number(q.success) || 0;
            const missing = Number(q.missing) || 0;
            if (!total) return '시세 로드: 대기';
            return `시세 로드: ${success}/${total} · 누락 ${missing}`;
        },

        quoteEtaText() {
            const q = this.quotePolling || {};
            const seconds = Number(q.duration_sec) || Number(q.estimated_sec) || 0;
            const label = q.in_progress ? '전체 스캔 예상' : '전체 스캔';
            return seconds > 0 ? `${label}: 약 ${Math.round(seconds)}초` : `${label}: 약 30~90초`;
        },

        themeMetricText(themeData) {
            const priceCount = Number(themeData?.price_count) || 0;
            if (priceCount < 2) return '거래활성 계산중 · 방향 계산중';
            return `거래활성 ${this.fmtThemeStrength(themeData.strength)} · 방향 ${this.fmtThemeDirection(themeData.avg_change_pct)}`;
        },

        formatSupplyFlow(stock, key) {
            const status = stock?.supply_status;
            const deStatus = stock?.de_supply_status;
            if (!stock || (status !== 'OK' && deStatus !== 'OK')) return '';
            const value = stock[key];
            if (value === null || value === undefined || value === '') return '';
            const num = Number(value);
            if (!Number.isFinite(num)) return '';
            // 원(KRW) → 억 단위 표시
            const eok = num / 100_000_000;
            const sign = eok >= 0 ? '+' : '';
            if (Math.abs(eok) >= 1) {
                return `${sign}${eok.toFixed(0)}억`;
            } else if (Math.abs(num) > 0) {
                // 1억 미만이면 만 단위
                return `${sign}${(num / 10_000).toFixed(0)}만`;
            }
            return '0';
        },

        supplyStatusText(stock) {
            const status = stock?.supply_status || stock?.de_supply_status || 'DATA_NA';
            const recency = stock?.supply_recency || stock?.de_supply_recency || 'UNKNOWN';
            const source = stock?.supply_source || stock?.de_supply_source || '';

            if (status === 'OK') {
                if (recency === 'TODAY') return '당일수급';
                if (recency === 'PREV_DAY') return '전일수급';
                return '수급 확인';
            }
            if (status === 'RATE_LIMIT') return '수급 제한';
            if (status === 'ERROR') return '수급 오류';

            if (status === 'DATA_NA') {
                if (source === 'KIS') return '수급 미확인';
                return '수급 미조회';
            }
            return '수급 미확인';
        },

        supplyAgeText(stock) {
            const status = stock?.supply_status || stock?.de_supply_status;
            if (status !== 'OK') return '';
            const recency = stock?.supply_recency || stock?.de_supply_recency || 'UNKNOWN';
            const supplyDate = stock?.supply_date || stock?.de_supply_date || '';
            if (recency === 'PREV_DAY') {
                // 전일 수급이면 날짜 표시 (YYYYMMDD → MM/DD)
                if (supplyDate && supplyDate.length === 8) {
                    return `(${supplyDate.slice(4,6)}/${supplyDate.slice(6,8)} 전일)`;
                }
                return '(전일)';
            }
            // 당일 수급이면 경과 시간 표시
            const ts = stock?.supply_updated_at;
            if (!ts) return '';
            const date = new Date(ts);
            if (Number.isNaN(date.getTime())) return '';
            const minutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
            return minutes > 3 ? `(${minutes}분 전)` : '';
        },

        supplyPollingText() {
            const s = this.supplyPolling || {};
            const target = Number(s.target_total) || 0;
            const ok = Number(s.ok) || 0;
            const na = Number(s.data_na) || 0;
            const limited = Number(s.rate_limit) || 0;
            const last = s.last_updated_at ? this.formatClock(s.last_updated_at) : '-';
            // 당일/전일 분리 카운트: alphaForgePicks에서 계산
            const picks = this.alphaForgePicks ? this.alphaForgePicks() : [];
            let todayCnt = 0, prevDayCnt = 0;
            for (const p of picks) {
                const rec = p.supply_recency || p.de_supply_recency || 'UNKNOWN';
                const st = p.supply_status || 'DATA_NA';
                if (st === 'OK' && rec === 'TODAY') todayCnt++;
                else if (st === 'OK' && rec === 'PREV_DAY') prevDayCnt++;
            }
            const parts = [];
            if (todayCnt > 0) parts.push(`당일수급 ${todayCnt}`);
            if (prevDayCnt > 0) parts.push(`전일수급 ${prevDayCnt}`);
            if (na > 0) parts.push(`미확인 ${na}`);
            if (limited > 0) parts.push(`제한 ${limited}`);
            const detailStr = parts.length > 0 ? ` · ${parts.join(' · ')}` : '';
            return `수급 조회: ${target}종목${detailStr} · 마지막 ${last}`;
        },

        supplyFlowClass(stock, key) {
            const status = stock?.supply_status;
            const deStatus = stock?.de_supply_status;
            if (!stock || (status !== 'OK' && deStatus !== 'OK')) return 'text-gray-400';
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

        // -----------------------------------------------------------------
        //  Day-Chart Sparkline (Yahoo-Finance-style mini SVG)
        // -----------------------------------------------------------------
        sparklineSvg(chart, width, height) {
            const W  = Number(width)  || 120;
            const H  = Number(height) || 32;
            const status = chart && chart.status;

            // ── placeholder (DATA_NA / collecting) ─────────────────────────
            const midY = (H / 2 + 0.5).toFixed(1);
            const placeholder = (label, cls) => `\
<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" class="spark-svg">\
<title>${label}</title>\
<line x1="0" y1="${midY}" x2="${W}" y2="${midY}" stroke="#cbd5e1" stroke-width="1" stroke-dasharray="2 3"/>\
</svg>`;

            if (!chart || status === 'DATA_NA') return placeholder('DATA_NA', '#94a3b8');
            const pts = Array.isArray(chart.points) ? chart.points : [];
            if (status !== 'OK' || pts.length < 2) return placeholder('collecting', '#94a3b8');

            const ys = pts.map(p => Number(p.p)).filter(v => Number.isFinite(v) && v > 0);
            if (ys.length < 2) return placeholder('collecting', '#94a3b8');

            const baseline = Number(chart.baseline);
            const last     = ys[ys.length - 1];

            // ── Yahoo-style y-axis: scale ONLY to intraday points ──────────
            // baseline is NOT included in y-range calculation so tiny intraday
            // moves fill the full chart height — exactly what Yahoo Finance does.
            const rawMin = Math.min(...ys);
            const rawMax = Math.max(...ys);
            // Minimum visible range: 0.20 % of last price, so even a flat day
            // shows a slight undulation rather than a dead-straight line.
            const minRange = Math.abs(last) * 0.002;
            const range    = Math.max(rawMax - rawMin, minRange);
            const mid      = (rawMax + rawMin) / 2;
            const lo       = mid - range / 2;
            const hi       = mid + range / 2;

            // Usable pixel band with small top/bottom padding
            const topPad    = 3;
            const bottomPad = 3;
            const useH      = H - topPad - bottomPad;

            const yOf = (v) => {
                const frac = (hi - v) / (hi - lo);           // 0 at top, 1 at bottom
                return (topPad + Math.min(Math.max(frac, 0), 1) * useH).toFixed(1);
            };
            const xOf = (i) => ((i / (ys.length - 1)) * (W - 2) + 1).toFixed(1);

            // ── baseline dotted line ───────────────────────────────────────
            // Clamp to visible area when baseline is outside the intraday range.
            let baselineLineY;
            if (Number.isFinite(baseline)) {
                if (baseline >= lo && baseline <= hi) {
                    baselineLineY = yOf(baseline);
                } else if (last >= baseline) {
                    // price is above baseline → baseline below the chart area → clamp near bottom
                    baselineLineY = (H - bottomPad + 1).toFixed(1);
                } else {
                    // price is below baseline → baseline above the chart area → clamp near top
                    baselineLineY = (topPad - 1).toFixed(1);
                }
            }
            const baselineSvg = baselineLineY !== undefined
                ? `<line x1="0" y1="${baselineLineY}" x2="${W}" y2="${baselineLineY}" stroke="#cbd5e1" stroke-width="1" stroke-dasharray="2 3" opacity="0.75"/>`
                : '';

            // ── colour: green if last >= baseline (or first point) ─────────
            const up     = Number.isFinite(baseline) ? last >= baseline : last >= ys[0];
            const stroke = up ? '#16a34a' : '#dc2626';
            const fill   = up ? 'rgba(22,163,74,0.12)' : 'rgba(220,38,38,0.12)';

            // ── SVG path ───────────────────────────────────────────────────
            const linePath = ys.map((v, i) => `${i === 0 ? 'M' : 'L'}${xOf(i)},${yOf(v)}`).join(' ');
            const areaPath = `M${xOf(0)},${H} ` +
                             ys.map((v, i) => `L${xOf(i)},${yOf(v)}`).join(' ') +
                             ` L${xOf(ys.length - 1)},${H} Z`;

            return `\
<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" class="spark-svg">\
${baselineSvg}\
<path d="${areaPath}" fill="${fill}" stroke="none"/>\
<path d="${linePath}" fill="none" stroke="${stroke}" stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round"/>\
<circle cx="${xOf(ys.length - 1)}" cy="${yOf(last)}" r="2" fill="${stroke}"/>\
</svg>`;
        },

        sparklineMeta(chart) {
            if (!chart) return '';
            if (chart.status === 'DATA_NA') return 'DATA_NA';
            if (chart.status !== 'OK') return `collecting · ${chart.point_count || 0}`;
            const pct = Number(chart.change_from_baseline_pct);
            const pctStr = Number.isFinite(pct) ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : '-';
            return `${chart.point_count}pts · ${pctStr}`;
        },

        _priceBaseline(price, changePct) {
            const p = Number(price);
            const c = Number(changePct);
            if (!Number.isFinite(p) || p <= 0) return null;
            if (!Number.isFinite(c) || c <= -99.9) return p;
            return p / (1 + c / 100);
        },

        recordChartPoint(bucket, key, price, changePct) {
            const p = Number(price);
            if (!key || !Number.isFinite(p) || p <= 0) return;

            const store = this.chartHistory[bucket] || (this.chartHistory[bucket] = {});
            const now = Date.now();
            const baseline = this._priceBaseline(p, changePct);
            const chart = store[key] || {
                baseline: baseline || p,
                points: [],
            };
            if (baseline && (!Number.isFinite(Number(chart.baseline)) || chart.baseline <= 0)) {
                chart.baseline = baseline;
            }

            const last = chart.points[chart.points.length - 1];
            if (!last || Math.abs(Number(last.p) - p) > 0.0001 || now - Number(last.t || 0) > 30000) {
                chart.points.push({ t: now, p });
            }
            chart.points = chart.points.slice(-48);
            store[key] = chart;
        },

        chartFromHistory(bucket, key) {
            const chart = this.chartHistory?.[bucket]?.[key];
            if (!chart || !Array.isArray(chart.points) || chart.points.length < 2) {
                return null;
            }
            const first = Number(chart.points[0]?.p);
            const last = Number(chart.points[chart.points.length - 1]?.p);
            const baseline = Number(chart.baseline);
            const ref = Number.isFinite(baseline) && baseline > 0 ? baseline : first;
            return {
                status: 'OK',
                baseline: ref,
                points: chart.points,
                point_count: chart.points.length,
                change_from_baseline_pct: ref ? ((last - ref) / ref) * 100 : 0,
            };
        },

        chartFromQuote(price, changePct) {
            const p = Number(price);
            if (!Number.isFinite(p) || p <= 0) return null;
            const baseline = this._priceBaseline(p, changePct) || p;
            const now = Date.now();
            return {
                status: 'OK',
                baseline,
                points: [
                    { t: now - 60000, p: baseline },
                    { t: now, p },
                ],
                point_count: 2,
                change_from_baseline_pct: baseline ? ((p - baseline) / baseline) * 100 : 0,
            };
        },

        stockChart(stock) {
            if (!stock) return null;
            const chart = stock.chart || stock.price_chart || stock.sparkline;
            if (chart) return chart;
            return this.chartFromHistory('stocks', stock.code)
                || this.chartFromQuote(stock.price, stock.change_pct);
        },

        indexChart(index, code) {
            if (!index) return null;
            const chart = index.chart || index.price_chart || index.sparkline;
            if (chart) return chart;
            return this.chartFromHistory('indices', code)
                || this.chartFromQuote(index.price, index.change_pct);
        },

        isLiveStock(stock) {
            return this._isVal(stock?.price) && Number(stock.price) > 0;
        },

        compactLeaders(themeData) {
            return [...(themeData?.leaders || [])]
                .sort((a, b) => {
                    const liveA = this.isLiveStock(a) ? 1 : 0;
                    const liveB = this.isLiveStock(b) ? 1 : 0;
                    if (liveA !== liveB) return liveB - liveA;
                    return (Number(b.score) || 0) - (Number(a.score) || 0);
                })
                .slice(0, 4);
        },

        sortedThemeEntries() {
            const themes = this.themes || {};
            const existing = new Set(Object.keys(themes));
            const pinned = [...this.pinnedThemes].filter(key => existing.has(key));
            const baseOrder = this.sortMode === 'default' ? this.themeStableOrder : this.themeSortOrder;
            const ordered = [...pinned];

            for (const key of baseOrder) {
                if (existing.has(key) && !ordered.includes(key)) ordered.push(key);
            }
            for (const key of Object.keys(themes)) {
                if (!ordered.includes(key)) ordered.push(key);
            }
            return ordered.map(key => [key, themes[key]]).filter(([, value]) => !!value);
        },

        themeSortValue(theme, mode) {
            if (!theme) return 0;
            if (mode === 'change_pct') return Number(theme.avg_change_pct) || 0;
            if (mode === 'trading_value') {
                return (theme.leaders || []).reduce((sum, stock) => (
                    sum + (Number(stock.cumulative_trading_value) || 0)
                ), 0);
            }
            return Number(theme.strength) || 0;
        },

        rebuildThemeSortOrder(mode) {
            const stableIndex = new Map(this.themeStableOrder.map((key, index) => [key, index]));
            this.themeSortOrder = [...this.themeStableOrder]
                .filter(key => this.themes?.[key])
                .sort((a, b) => {
                    const diff = this.themeSortValue(this.themes[b], mode) - this.themeSortValue(this.themes[a], mode);
                    if (diff !== 0) return diff;
                    return (stableIndex.get(a) || 0) - (stableIndex.get(b) || 0);
                });
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

        intradayEvent(stock) {
            // Prefer server-computed event if present (live API).
            if (stock && stock.event_level && stock.event_type) {
                return {
                    event_level: stock.event_level,
                    event_type: stock.event_type,
                    event_reason: stock.event_reason || '',
                    event_should_alert: !!stock.event_should_alert,
                };
            }
            // Client-side fallback (mirrors Python event_layer.compute_event).
            const LEVEL_PRIORITY = { DATA_WAIT: 0, OFF: 1, L1: 2, L2: 3, L3: 4, RISK: 5 };
            const price = Number(stock?.price) || 0;
            const chg = Number(stock?.change_pct) || 0;
            const strength = Number(stock?.strength) || 0;
            const box = Number(stock?.box_upper_price) || 0;
            const horizon = stock?.horizon_label || '';
            const alertType = stock?.alert_type || '';

            if (this.sessionType !== 'regular') {
                return { event_level: 'OFF', event_type: 'MARKET_CLOSED',
                         event_reason: '장마감/비정규 세션', event_should_alert: false };
            }
            if (price <= 0) {
                return { event_level: 'DATA_WAIT', event_type: 'NO_PRICE',
                         event_reason: '현재가 대기', event_should_alert: false };
            }

            const cands = [];
            if (box > 0) {
                if (price < box) {
                    const gapPct = ((box - price) / box) * 100;
                    if (gapPct <= 1.0) {
                        cands.push({ event_level: 'L1', event_type: 'BOX_NEAR',
                            event_reason: `BOX ${this.formatBoxPrice(box)} 상단 근접 (이격 ${gapPct.toFixed(2)}%)`,
                            event_should_alert: false });
                    }
                } else {
                    if (strength < 100) {
                        cands.push({ event_level: 'RISK', event_type: 'FAKEOUT_RISK',
                            event_reason: `BOX ${this.formatBoxPrice(box)} 돌파했지만 체결강도 약함 (체결강도 ${strength.toFixed(0)})`,
                            event_should_alert: true });
                    } else if (strength < 120) {
                        cands.push({ event_level: 'L2', event_type: 'BREAKOUT_WATCH',
                            event_reason: `BOX ${this.formatBoxPrice(box)} 돌파 관찰 · 체결강도 ${strength.toFixed(0)}`,
                            event_should_alert: true });
                    } else {
                        cands.push({ event_level: 'L3', event_type: 'BREAKOUT_STRONG',
                            event_reason: `BOX ${this.formatBoxPrice(box)} 돌파 + 강한 체결강도 ${strength.toFixed(0)}`,
                            event_should_alert: true });
                    }
                }
            }
            if (chg >= 7.0 && strength < 110) {
                cands.push({ event_level: 'RISK', event_type: 'WEAK_CHASE_RISK',
                    event_reason: `급등(+${chg.toFixed(2)}%)했지만 체결강도 부족(${strength.toFixed(0)}) · 추격주의`,
                    event_should_alert: true });
            }
            if (chg <= -3.0) {
                cands.push({ event_level: 'RISK', event_type: 'DROP_WATCH',
                    event_reason: `후보 종목 장중 급락 관찰 (${chg.toFixed(2)}%)`,
                    event_should_alert: true });
            }

            let chosen;
            if (cands.length === 0) {
                chosen = { event_level: 'L1', event_type: 'OBSERVING',
                           event_reason: '특이 신호 없음', event_should_alert: false };
            } else {
                chosen = cands.reduce((a, b) =>
                    (LEVEL_PRIORITY[b.event_level] ?? -1) > (LEVEL_PRIORITY[a.event_level] ?? -1) ? b : a);
            }
            if ((horizon === 'CHASE_RISK' || alertType === 'RISK_WATCH')
                && !chosen.event_reason.includes('추격주의')) {
                chosen = { ...chosen, event_reason: chosen.event_reason + ' · 추격주의' };
            }
            return chosen;
        },

        eventColorClass(level) {
            if (level === 'RISK') return 'text-red-600';
            if (level === 'L3') return 'text-green-700';
            if (level === 'L2') return 'text-amber-700';
            if (level === 'L1') return 'text-blue-600';
            return 'text-gray-500';
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
            if (isAlphaForgeTrackingCandidate(stock)) {
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
            if (Array.isArray(this.alphaforgePicksData) && this.alphaforgePicksData.length > 0) {
                return this.alphaforgePicksData.map(stock => {
                    const live = this.stocks[stock.code] || {};
                    const merged = { ...stock, ...live };
                    return {
                        ...merged,
                        horizon_setup_label: this.alphaForgeHorizonLabel(merged),
                    };
                });
            }
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

        // Decision Engine helpers
        deDecisionBadgeClass(decision) {
            const m = {
                BUY_NOW: 'bg-green-600 text-white',
                STARTER_POSITION: 'bg-blue-500 text-white',
                CONDITIONAL_BUY: 'bg-amber-500 text-white',
                WATCH_ONLY: 'bg-gray-400 text-white',
                AVOID: 'bg-red-600 text-white',
                MARKET_CLOSED: 'bg-gray-500 text-white',
            };
            return m[decision] || 'bg-gray-200 text-gray-600';
        },

        deDecisionKo(decision) {
            const m = {
                BUY_NOW: '매수 가능',
                STARTER_POSITION: '소량 선취',
                CONDITIONAL_BUY: '조건부 매수',
                WATCH_ONLY: '관찰',
                AVOID: '매수 금지',
                MARKET_CLOSED: '장마감',
            };
            return m[decision] || decision || '-';
        },

        deActionBoardTotal() {
            const c = this.decisionCounts || {};
            return (c.BUY_NOW || 0) + (c.STARTER_POSITION || 0) + (c.CONDITIONAL_BUY || 0) + (c.WATCH_ONLY || 0) + (c.AVOID || 0);
        },

        deHasBuySignal() {
            const c = this.decisionCounts || {};
            return (c.BUY_NOW || 0) > 0 || (c.STARTER_POSITION || 0) > 0;
        },

        deDecisionText(stock) {
            return stock?.de_decision || stock?.decision_engine?.decision || '-';
        },

        deConfidenceText(stock) {
            const score = stock?.de_confidence ?? stock?.decision_engine?.confidence_score;
            if (score === null || score === undefined || score === '') return '-';
            return `${Number(score) || 0}점`;
        },

        deDataConfidenceText(stock) {
            return stock?.de_data_confidence || stock?.decision_engine?.data_confidence || '-';
        },

        deMaxPositionText(stock) {
            const pct = stock?.de_max_pct ?? stock?.decision_engine?.max_position_pct;
            if (pct === null || pct === undefined || pct === '') return '0%';
            return `${Number(pct) || 0}%`;
        },

        deSetupScoreText(stock) {
            const score = stock?.de_setup_score ?? stock?.decision_engine?.setup_score;
            if (score === null || score === undefined || score === '') return '-';
            return `${Number(score) || 0}점`;
        },

        deSetupLabelText(stock) {
            return stock?.de_setup_label || stock?.decision_engine?.setup_label || '-';
        },

        deNextSessionTrigger(stock) {
            return stock?.de_next_session_trigger || stock?.decision_engine?.next_session_trigger || '-';
        },

        deNextSessionPlan(stock) {
            return stock?.de_next_session_plan || stock?.decision_engine?.next_session_plan || '-';
        },

        deSetupReason(stock) {
            return stock?.de_setup_reason || stock?.decision_engine?.setup_reason || '-';
        },

        deSetupTop3Text() {
            const rows = Array.isArray(this.decisionSetupTop3) ? this.decisionSetupTop3 : [];
            if (rows.length === 0) return '내일 관찰 후보: -';
            return `내일 관찰 후보: ${rows.map(row => `${row.name || row.symbol} ${row.setup_score || 0}점`).join(' / ')}`;
        },

        deTopReasonCodesText(limit = 3) {
            const rows = Object.entries(this.reasonCodeCounts || {})
                .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
                .slice(0, limit);
            if (rows.length === 0) return '차단 원인 TOP3: -';
            return `차단 원인 TOP3: ${rows.map(([code, count]) => `${code} ${count}`).join(' / ')}`;
        },

        deDataConfidenceCountsText() {
            const c = this.dataConfidenceCounts || {};
            return `데이터 신뢰 HIGH ${c.HIGH || 0} / MID ${c.MID || 0} / LOW ${c.LOW || 0}`;
        },

        deSectorAuditText() {
            const warnings = Array.isArray(this.sectorAuditWarnings) ? this.sectorAuditWarnings.length : 0;
            const duplicated = Array.isArray(this.duplicatedSymbols) ? this.duplicatedSymbols.length : 0;
            const suspicious = Array.isArray(this.suspiciousSectorMembers) ? this.suspiciousSectorMembers.length : 0;
            return `섹터 경고 ${warnings} · 중복 ${duplicated} · 의심 ${suspicious}`;
        },

        liveMomentumPicks() {
            const alphaByCode = new Map(this.alphaForgePicks().map(s => [s.code, s]));
            const rows = Object.values(this.stocks || []).map(stock => {
                const alpha = alphaByCode.get(stock.code) || {};
                const merged = {
                    ...alpha,
                    ...stock,
                    name: alpha.name || this.findStockName(stock.code) || stock.name || stock.code,
                };
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

        findStockName(code) {
            if (!code) return '';
            for (const theme of Object.values(this.themes || {})) {
                for (const stock of theme.leaders || []) {
                    if (stock.code === code && stock.name && stock.name !== code) return stock.name;
                }
            }
            for (const stock of this.alphaforgePicksData || []) {
                if (stock.code === code && stock.name && stock.name !== code) return stock.name;
            }
            return '';
        },

        momentumDisplayName(stock) {
            const code = stock?.code || '';
            const name = stock?.name || '';
            if (name && name !== code) return `${name} (${code})`;
            return code;
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
            lines.push('[Action Board]');
            const dc = this.decisionCounts || {};
            lines.push(`BUY_NOW ${dc.BUY_NOW || 0} / STARTER ${dc.STARTER_POSITION || 0} / CONDITIONAL ${dc.CONDITIONAL_BUY || 0} / WATCH ${dc.WATCH_ONLY || 0} / AVOID ${dc.AVOID || 0}`);
            lines.push(`시장 게이트 ${this.marketGateLevel || this.decisionMarketGate?.market_gate_level || '-'} / ${this.marketGateReason || this.decisionMarketGate?.market_gate_reason || this.decisionMarketGate?.reason || '-'}`);
            lines.push(this.deTopReasonCodesText());
            lines.push(this.deDataConfidenceCountsText());
            lines.push(this.deSectorAuditText());
            lines.push(this.supplyPollingText());
            if (!this.deHasBuySignal()) {
                const reason = this.decisionSession === 'REGULAR'
                    ? (this.decisionMarketGate?.reason || '조건 미충족')
                    : 'MARKET_CLOSED';
                lines.push(`오늘 매수추천 없음: ${reason}`);
            }
            lines.push(this.deSetupTop3Text());
            lines.push('');
            lines.push('[Market Indices]');
            const indexEntries = Object.entries(this.indices || {});
            if (indexEntries.length === 0) {
                lines.push('지수 없음');
            }
            for (const [code, index] of indexEntries) {
                lines.push([
                    `${index.name || code} (${code})`,
                    `현재가 ${this._isVal(index.price) ? Number(index.price).toLocaleString('ko-KR') : '-'}`,
                    `등락률 ${this.fmtPct(index.change_pct)}`,
                    `외 ${this.formatIndexSupplyFlow(index, 'investor_foreigner')}`,
                    `기 ${this.formatIndexSupplyFlow(index, 'investor_institution')}`,
                    `개 ${this.formatIndexSupplyFlow(index, 'investor_individual')}`,
                ].join(' / '));
            }
            lines.push('');
            lines.push('[산업군/테마]');
            const themeEntries = this.sortedThemeEntries();
            if (themeEntries.length === 0) {
                lines.push(`산업군 데이터 없음${this.themeLoadReason ? `: ${this.themeLoadReason}` : ''}`);
            }
            for (const [themeName, theme] of themeEntries) {
                lines.push(`${theme.display_name || themeName} / 거래활성 ${this.fmtThemeStrength(theme.strength)} / 방향 ${this.fmtThemeDirection(theme.avg_change_pct)} / 등락 ${this.fmtPct(theme.avg_change_pct)}`);
                for (const stock of this.compactLeaders(theme)) {
                    lines.push([
                        `- ${stock.name || stock.code} (${stock.code})`,
                        `현재가 ${this._isVal(stock.price) ? Number(stock.price).toLocaleString('ko-KR') : '-'}`,
                        `등락률 ${this.fmtPct(stock.change_pct)}`,
                        `체결강도 ${this.fmtStr(stock.strength)}`,
                        `거래대금 ${stock.cumulative_trading_value ? `${(Number(stock.cumulative_trading_value) / 100000000).toFixed(0)}억` : '-'}`,
                    ].join(' / '));
                }
            }
            lines.push('');
            lines.push('[AlphaForge Picks]');

            const picks = this.alphaForgePicks();
            if (picks.length === 0) {
                lines.push('후보 없음');
            }
            for (const stock of picks) {
                const decision = this.intradayDecision(stock);
                const event = this.intradayEvent(stock);
                const deDecision = this.deDecisionKo(this.deDecisionText(stock));
                const deConfidence = stock.de_confidence !== undefined && stock.de_confidence !== null
                    ? `매수신뢰 ${stock.de_confidence}점`
                    : '매수신뢰 0점';
                const deDataConfidence = this.deDataConfidenceText(stock);
                const deReason = stock.de_no_buy_reason || stock.de_action_reason || '-';
                const deTrigger = stock.de_entry_trigger || '-';
                const deInvalidation = stock.de_invalidation || stock.de_invalidation_reason || '-';
                const deMaxPct = this.deMaxPositionText(stock);
                const setupScore = this.deSetupScoreText(stock);
                const setupLabel = this.deSetupLabelText(stock);
                const nextTrigger = this.deNextSessionTrigger(stock);
                const nextPlan = this.deNextSessionPlan(stock);
                const setupReason = this.deSetupReason(stock);
                const reasonCodes = Array.isArray(stock.de_reason_codes) ? stock.de_reason_codes.join(',') : '-';
                const qualityFlags = Array.isArray(stock.de_data_quality_flags) ? stock.de_data_quality_flags.join(',') : '-';
                const deConfirmations = Array.isArray(stock.de_required_confirmations)
                    ? stock.de_required_confirmations.join(', ')
                    : (stock.de_required_confirmations || '-');
                const price = stock.price ? Number(stock.price).toLocaleString('ko-KR') : '-';
                const changePct = `${(Number(stock.change_pct) || 0).toFixed(2)}%`;
                const strength = this.formatStrength(stock.strength);
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
                        `수급구분 ${stock.supply_recency || stock.de_supply_recency || 'UNKNOWN'}`,
                        `수급날짜 ${stock.supply_date || stock.de_supply_date || '-'}`,
                        `수급시각 ${stock.supply_updated_at || '-'}`,
                        `수급소스 ${stock.supply_source || '-'}`,
                        `외 ${this.formatSupplyFlow(stock, 'foreign_flow') || '-'}`,
                        `기 ${this.formatSupplyFlow(stock, 'institution_flow') || '-'}`,
                        `개 ${this.formatSupplyFlow(stock, 'individual_flow') || '-'}`,
                        `DECISION_ENGINE ${deDecision}`,
                        `confidence ${deConfidence}`,
                        `data_confidence ${deDataConfidence}`,
                        `data_quality_flags ${qualityFlags || '-'}`,
                        `quote_age_sec ${stock.de_quote_age_sec ?? '-'}`,
                        `setup_score ${setupScore}`,
                        `setup_label ${setupLabel}`,
                        `setup_reason ${setupReason}`,
                        `reason_codes ${reasonCodes || '-'}`,
                        `no_buy_reason ${deReason}`,
                        `entry_trigger ${deTrigger}`,
                        `next_session_trigger ${nextTrigger}`,
                        `next_session_plan ${nextPlan}`,
                        `invalidation ${deInvalidation}`,
                        `max_position_pct ${deMaxPct}`,
                        `required_confirmations ${deConfirmations || '-'}`,
                        `LEGACY_DECISION ${decision.decision}`,
                        `legacy 사유 ${this.formatDecisionItems(decision.reasons)}`,
                        `EVENT ${event.event_level} / ${event.event_type}`,
                        `EVENT 사유 ${event.event_reason || '-'}`,
                        `현재가 ${price}`,
                        `등락률 ${changePct}`,
                        `체결강도 ${strength}`,
                        `거래대금 ${tradingValue}`,
                    ].join(' / ')
                );
            }

            // ── Forward Test Evaluator v1 ──
            lines.push('');
            lines.push('[Forward Test]');
            const ft = this.forwardTestSummary;
            if (!ft || ft.status === 'DATA_INSUFFICIENT') {
                lines.push('데이터가 부족하여 성과 분석을 표시할 수 없습니다.');
            } else {
                for (const hz of ['1d', '3d', '5d', '10d']) {
                    const hData = ft.horizons && ft.horizons[hz];
                    if (!hData || hData.status === 'DATA_INSUFFICIENT') {
                        lines.push(`${hz}: 성과 평균 0.00% / 승률 0.0% / 차단 아직 데이터 부족 (차단 0 / Good 0 / Missed 0)`);
                    } else {
                        const bq = hData.blocked_quality || {};
                        const blocked = bq.blocked_count || 0;
                        const good = bq.good_block_count || 0;
                        const missed = bq.missed_opportunity_count || 0;
                        const grade = bq.quality_grade || '아직 데이터 부족';
                        const avg = hData.avg_return !== undefined ? hData.avg_return.toFixed(2) : '0.00';
                        const win = hData.win_rate !== undefined ? hData.win_rate.toFixed(1) : '0.0';
                        lines.push(`${hz}: 성과 평균 ${avg >= 0 ? '+' : ''}${avg}% / 승률 ${win}% / 차단 ${grade} (차단 ${blocked} / Good ${good} / Missed ${missed})`);
                    }
                }
            }

            return lines.join('\n');
        },

        async copyDashboardText() {
            try {
                const text = this.buildDashboardCopyText();
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(text);
                } else {
                    const textarea = document.createElement('textarea');
                    textarea.value = text;
                    textarea.setAttribute('readonly', '');
                    textarea.style.position = 'fixed';
                    textarea.style.left = '-9999px';
                    document.body.appendChild(textarea);
                    textarea.select();
                    document.execCommand('copy');
                    document.body.removeChild(textarea);
                }
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

        // ── Market persistence helpers ────────────────────────────────────────

        _getInitialMarket() {
            // Priority: URL query > localStorage > default 'KR'
            const params = new URLSearchParams(window.location.search);
            const qm = (params.get('market') || '').toUpperCase();
            if (qm === 'US' || qm === 'KR') {
                localStorage.setItem('CHECKS_SELECTED_MARKET', qm);
                return qm;
            }
            const stored = localStorage.getItem('CHECKS_SELECTED_MARKET');
            if (stored === 'US' || stored === 'KR') return stored;
            return 'KR';
        },

        _setupFocusRefresh() {
            let _lastRefresh = 0;
            const THROTTLE_MS = 2000;  // 2초 throttle — 짧은 연속 이벤트 방지
            const refresh = () => {
                const now = Date.now();
                if (now - _lastRefresh < THROTTLE_MS) return;
                _lastRefresh = now;
                console.log('🔄 Focus/visibility refresh', this.market);
                if (this.market === 'US') {
                    this.loadUsWatchlist();
                } else {
                    this.loadThemes();
                    this.loadSurges();
                    this.loadIndices();
                    this.loadKrSectorLeaders();
                }
            };
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible') refresh();
            });
            window.addEventListener('focus', refresh);
        },

        // ── Pinned themes ─────────────────────────────────────────────────────

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
                this.sortMode = localStorage.getItem('tima_sort_mode') || 'default';
            } catch (e) {
                this.sortMode = 'default';
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
            this.sortMode = this.sortMode === mode ? 'default' : mode;
            if (this.sortMode === 'default') {
                this.themeSortOrder = [];
            } else {
                this.rebuildThemeSortOrder(this.sortMode);
            }
            this.saveSortMode();
            console.log(`🔄 Sort mode: ${this.sortMode}`);
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

        // ── ET helper: returns {dow, hhmm} in America/New_York ──────────────────
        _etNow() {
            const now = new Date();
            const parts = new Intl.DateTimeFormat('en-US', {
                timeZone: 'America/New_York',
                weekday: 'short',
                hour: 'numeric',
                minute: 'numeric',
                hour12: false,
            }).formatToParts(now);
            const wd  = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']
                          .indexOf(parts.find(p => p.type === 'weekday').value);
            const h   = parseInt(parts.find(p => p.type === 'hour').value)   % 24;
            const min = parseInt(parts.find(p => p.type === 'minute').value);
            return { dow: wd, hhmm: h * 100 + min };
        },

        updateSessionType() {
            if (this.market === 'US') {
                // Use actual ET time — fixes KST Sat ≡ ET Fri boundary
                const { dow, hhmm } = this._etNow();
                if (dow === 0 || dow === 6) { this.sessionType = 'closed'; return; }
                if (hhmm >= 400 && hhmm < 930)   this.sessionType = 'pre';
                else if (hhmm >= 930 && hhmm < 1600)  this.sessionType = 'regular';
                else if (hhmm >= 1600 && hhmm < 2000) this.sessionType = 'after';
                else                                   this.sessionType = 'closed';
                return;
            }
            // KR: use local KST time
            const now = new Date();
            const dow = now.getDay();
            if (dow === 0 || dow === 6) { this.sessionType = 'closed'; return; }
            const hhmm = now.getHours() * 100 + now.getMinutes();
            if (hhmm >= 800 && hhmm < 850) this.sessionType = 'pre';
            else if (hhmm >= 900 && hhmm <= 1530) this.sessionType = 'regular';
            else if (hhmm > 1530 && hhmm < 2000) this.sessionType = 'after';
            else this.sessionType = 'closed';
        },

        async loadThemes() {
            try {
                const pinnedStr = [...this.pinnedThemes].join(',');
                const response = await fetch(`/api/themes?sort=default&pinned=${pinnedStr}`);
                const data = await response.json();

                this.wsConnected = data.ws_connected || false;
                this.mode = data.mode || 'paper';
                this.alphaforgeCandidatesLoaded = data.alphaforge_candidates_loaded || 0;
                this.alphaforgeCandidatesGeneratedAt = data.alphaforge_candidates_generated_at || '';
                this.alphaforgePicksData = Array.isArray(data.alphaforge_picks) ? data.alphaforge_picks : [];
                this.themeLoadStatus = data.theme_load_status || 'ok';
                this.themeLoadReason = data.theme_load_reason || '';
                this.quotePolling = data.quote_polling || {};
                this.supplyDataReason = data.supply_data_reason || '';
                this.supplyPolling = data.supply_polling || {};

                if (data.themes) {
                    this.themes = data.themes;
                    const incomingOrder = Object.keys(data.themes);
                    if (this.themeStableOrder.length === 0) {
                        this.themeStableOrder = incomingOrder;
                    } else {
                        for (const key of incomingOrder) {
                            if (!this.themeStableOrder.includes(key)) this.themeStableOrder.push(key);
                        }
                        this.themeStableOrder = this.themeStableOrder.filter(key => incomingOrder.includes(key));
                    }
                    if (this.sortMode !== 'default' && this.themeSortOrder.length === 0) {
                        this.rebuildThemeSortOrder(this.sortMode);
                    }
                    for (const theme of Object.values(this.themes || {})) {
                        for (const stock of theme.leaders || []) {
                            this.recordChartPoint('stocks', stock.code, stock.price, stock.change_pct);
                        }
                    }
                    for (const stock of this.alphaforgePicksData || []) {
                        this.recordChartPoint('stocks', stock.code, stock.price, stock.change_pct);
                    }
                }
                // Decision Engine state
                if (data.decision_counts) this.decisionCounts = data.decision_counts;
                if (data.decision_session) this.decisionSession = data.decision_session;
                if (data.decision_market_gate) this.decisionMarketGate = data.decision_market_gate;
                if (data.decision_setup_top3) this.decisionSetupTop3 = data.decision_setup_top3;
                if (data.decision_quality_summary) this.decisionQualitySummary = data.decision_quality_summary;
                if (data.data_confidence_counts) this.dataConfidenceCounts = data.data_confidence_counts;
                if (data.reason_code_counts) this.reasonCodeCounts = data.reason_code_counts;
                if (data.market_gate_level) this.marketGateLevel = data.market_gate_level;
                if (data.market_gate_reason) this.marketGateReason = data.market_gate_reason;
                if (data.market_gate_blocks_buy_now !== undefined) this.marketGateBlocksBuyNow = !!data.market_gate_blocks_buy_now;
                if (data.journal_status) this.journalStatus = data.journal_status;
                if (data.sector_audit_warnings) this.sectorAuditWarnings = data.sector_audit_warnings;
                if (data.duplicated_symbols) this.duplicatedSymbols = data.duplicated_symbols;
                if (data.suspicious_sector_members) this.suspiciousSectorMembers = data.suspicious_sector_members;
                if (data.forward_test_summary) this.forwardTestSummary = data.forward_test_summary;
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
                this.recordChartPoint('stocks', code, msg.price, msg.change_pct);

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
