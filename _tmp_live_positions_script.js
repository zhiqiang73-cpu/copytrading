
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        dark: { bg: '#0f1117', card: '#1a1d29', border: '#2a2d3a', hover: '#242736' },
                        accent: '#3b82f6',
                        profit: '#22c55e',
                        loss: '#ef4444',
                    }
                }
            }
        }
    


        // ========== WebSocket 实时连接 ==========
        let socket = null;
        let wsReconnectAttempts = 0;
        const MAX_RECONNECT_ATTEMPTS = 5;
        
        function initWebSocket() {
            try {
                socket = io({
                    transports: ['websocket', 'polling'],
                    reconnection: true,
                    reconnectionDelay: 1000,
                    reconnectionDelayMax: 5000,
                    reconnectionAttempts: MAX_RECONNECT_ATTEMPTS
                });
                
                socket.on('connect', () => {
                    console.log('[WebSocket] 已连接');
                    wsReconnectAttempts = 0;
                    // 连接成功后立即更新状态
                    updateConnectionStatus('connected');
                });
                
                socket.on('disconnect', (reason) => {
                    console.log('[WebSocket] 断开:', reason);
                    updateConnectionStatus('disconnected');
                });
                
                socket.on('connect_error', (error) => {
                    wsReconnectAttempts++;
                    console.log(`[WebSocket] 连接错误 (${wsReconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}):`, error.message);
                    if (wsReconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
                        updateConnectionStatus('error');
                    }
                });
                
                // 监听初始状态
                socket.on('initial_state', (data) => {
                    console.log('[WebSocket] 收到初始状态:', data);
                    updateUIFromState(data);
                });
                
                // 监听状态更新
                socket.on('status_update', (data) => {
                    console.log('[WebSocket] 状态更新:', data);
                    updateUIFromState(data);
                });
                
                // 监听引擎启动事件
                socket.on('engine_started', (data) => {
                    console.log('[WebSocket] 引擎已启动');
                    updateUIFromState(data);
                    showNotification('引擎已启动', 'success');
                });
                
                // 监听引擎停止事件
                socket.on('engine_stopped', (data) => {
                    console.log('[WebSocket] 引擎已停止');
                    updateUIFromState(data);
                    showNotification('引擎已停止', 'info');
                });
                
                // 心跳
                socket.on('pong', (data) => {
                    // console.log('[WebSocket] pong');
                });
                
            } catch (error) {
                console.error('[WebSocket] 初始化失败:', error);
            }
        }
        
        function updateConnectionStatus(status) {
            const heartbeatIcon = document.getElementById('heartbeat-icon');
            const heartbeatText = document.getElementById('heartbeat-text');
            const heartbeatIndicator = document.getElementById('heartbeat-indicator');
            
            if (!heartbeatIcon || !heartbeatText) return;
            
            if (status === 'connected') {
                heartbeatIcon.classList.remove('text-red-400', 'text-yellow-400');
                heartbeatIcon.classList.add('text-green-400');
                heartbeatText.classList.remove('text-red-400', 'text-yellow-400');
                heartbeatText.classList.add('text-green-400');
                heartbeatText.textContent = '实时';
                if (heartbeatIndicator) {
                    heartbeatIndicator.classList.remove('border-red-500', 'border-yellow-500');
                    heartbeatIndicator.classList.add('border-green-500');
                }
            } else if (status === 'disconnected') {
                heartbeatIcon.classList.remove('text-green-400', 'text-red-400');
                heartbeatIcon.classList.add('text-yellow-400');
                heartbeatText.classList.remove('text-green-400', 'text-red-400');
                heartbeatText.classList.add('text-yellow-400');
                heartbeatText.textContent = '重连中';
                if (heartbeatIndicator) {
                    heartbeatIndicator.classList.remove('border-green-500', 'border-red-500');
                    heartbeatIndicator.classList.add('border-yellow-500');
                }
            } else if (status === 'error') {
                heartbeatIcon.classList.remove('text-green-400', 'text-yellow-400');
                heartbeatIcon.classList.add('text-red-400');
                heartbeatText.classList.remove('text-green-400', 'text-yellow-400');
                heartbeatText.classList.add('text-red-400');
                heartbeatText.textContent = '离线';
                if (heartbeatIndicator) {
                    heartbeatIndicator.classList.remove('border-green-500', 'border-yellow-500');
                    heartbeatIndicator.classList.add('border-red-500');
                }
            }
        }
        
        function updateUIFromState(data) {
            if (!data || data.error) return;
            
            const engineStatus = document.getElementById('engine-status');
            const dot = document.getElementById('status-dot');
            
            // 更新引擎状态
            if (data.engine) {
                const anyRunning = data.engine.any_running || data.engine.sim_running || data.engine.live_running;
                if (engineStatus) {
                    if (anyRunning) {
                        engineStatus.classList.remove('hidden');
                        engineStatus.classList.add('flex');
                    } else {
                        engineStatus.classList.add('hidden');
                        engineStatus.classList.remove('flex');
                    }
                }
            }
            
            // 更新API配置状态
            if (dot && typeof data.api_configured !== 'undefined') {
                if (data.api_configured) {
                    dot.innerHTML = '<span class="w-2 h-2 rounded-full bg-yellow-500"></span><span class="text-yellow-400">就绪</span>';
                } else {
                    dot.innerHTML = '<span class="w-2 h-2 rounded-full bg-gray-500"></span><span class="text-gray-400">未配置</span>';
                }
            }
            
            // 触发自定义事件，让其他页面可以监听
            window.dispatchEvent(new CustomEvent('ws_state_update', { detail: data }));
        }
        
        function showNotification(message, type = 'info') {
            // 如果页面有 showHomeNotice 函数就调用它
            if (typeof showHomeNotice === 'function') {
                showHomeNotice(message, type);
            }
        }
        
        // 页面加载时初始化WebSocket
        document.addEventListener('DOMContentLoaded', () => {
            initWebSocket();
        });
        
        // ========== 原有的状态检查（作为后备） ==========
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                const dot = document.getElementById('status-dot');
                const engineStatus = document.getElementById('engine-status');
                
                // 更新API配置状态
                if (data.collector_running) {
                    dot.innerHTML = '<span class="w-2 h-2 rounded-full bg-green-500 animate-pulse"></span><span class="text-green-400">采集中</span>';
                } else if (data.api_configured) {
                    dot.innerHTML = '<span class="w-2 h-2 rounded-full bg-yellow-500"></span><span class="text-yellow-400">就绪</span>';
                } else {
                    dot.innerHTML = '<span class="w-2 h-2 rounded-full bg-gray-500"></span><span class="text-gray-400">未配置</span>';
                }
                
                // 更新引擎状态
                const engineRunning = data.copy_engine_running || data.sim_copy_engine_running || data.live_copy_engine_running;
                if (engineRunning && engineStatus) {
                    engineStatus.classList.remove('hidden');
                    engineStatus.classList.add('flex');
                } else if (engineStatus) {
                    engineStatus.classList.add('hidden');
                    engineStatus.classList.remove('flex');
                }
            } catch(e) {
                console.error('Status fetch failed:', e);
            }
        }

        // 心跳机制：告诉后端网页还开着，防止后端自动关闭
        const APP_TOKEN = document.querySelector('meta[name="app-token"]')?.content || "";
        let heartbeatFailCount = 0;
        let lastHeartbeatTime = Date.now();

        async function sendHeartbeat() {
            const heartbeatIcon = document.getElementById('heartbeat-icon');
            const heartbeatText = document.getElementById('heartbeat-text');
            const heartbeatIndicator = document.getElementById('heartbeat-indicator');
            
            try {
                const response = await fetch('/api/heartbeat', { 
                    method: 'POST', 
                    headers: {'X-App-Token': APP_TOKEN},
                    signal: AbortSignal.timeout(3000)
                });
                
                if (response.ok) {
                    // 心跳成功
                    heartbeatFailCount = 0;
                    lastHeartbeatTime = Date.now();
                    heartbeatIcon.classList.remove('text-red-400', 'text-yellow-400');
                    heartbeatIcon.classList.add('text-green-400');
                    heartbeatText.classList.remove('text-red-400', 'text-yellow-400');
                    heartbeatText.classList.add('text-green-400');
                    heartbeatText.textContent = '在线';
                    heartbeatIndicator.classList.remove('border-red-500', 'border-yellow-500');
                    heartbeatIndicator.classList.add('border-dark-border');
                    
                    // 添加心跳动画
                    heartbeatIcon.classList.add('animate-pulse');
                    setTimeout(() => heartbeatIcon.classList.remove('animate-pulse'), 300);
                } else {
                    throw new Error('Heartbeat response not ok');
                }
            } catch (e) {
                heartbeatFailCount++;
                console.log(`Heartbeat failed (${heartbeatFailCount}):`, e.message);
                
                if (heartbeatFailCount >= 3) {
                    // 连续3次失败，标记为离线
                    heartbeatIcon.classList.remove('text-green-400', 'text-yellow-400');
                    heartbeatIcon.classList.add('text-red-400');
                    heartbeatText.classList.remove('text-green-400', 'text-yellow-400');
                    heartbeatText.classList.add('text-red-400');
                    heartbeatText.textContent = '离线';
                    heartbeatIndicator.classList.remove('border-dark-border', 'border-yellow-500');
                    heartbeatIndicator.classList.add('border-red-500');
                } else if (heartbeatFailCount >= 1) {
                    // 1-2次失败，标记为不稳定
                    heartbeatIcon.classList.remove('text-green-400', 'text-red-400');
                    heartbeatIcon.classList.add('text-yellow-400');
                    heartbeatText.classList.remove('text-green-400', 'text-red-400');
                    heartbeatText.classList.add('text-yellow-400');
                    heartbeatText.textContent = '不稳定';
                    heartbeatIndicator.classList.remove('border-dark-border', 'border-red-500');
                    heartbeatIndicator.classList.add('border-yellow-500');
                }
            }
        }

        fetchStatus();
        sendHeartbeat();
        setInterval(fetchStatus, 30000);  // WebSocket实时更新，降低轮询频率到30秒
        setInterval(sendHeartbeat, 3000);  // 每3秒发送一次心跳
        
        // 检测长时间无响应
        setInterval(() => {
            const timeSinceLastHeartbeat = Date.now() - lastHeartbeatTime;
            if (timeSinceLastHeartbeat > 15000) {  // 超过15秒无响应
                const heartbeatText = document.getElementById('heartbeat-text');
                const heartbeatIcon = document.getElementById('heartbeat-icon');
                heartbeatIcon.classList.remove('text-green-400', 'text-yellow-400');
                heartbeatIcon.classList.add('text-red-400');
                heartbeatText.classList.remove('text-green-400', 'text-yellow-400');
                heartbeatText.classList.add('text-red-400');
                heartbeatText.textContent = '超时';
            }
        }, 5000);

        async function apiPost(url, body) {
            const res = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-App-Token': APP_TOKEN},
                body: JSON.stringify(body || {})
            });
            return await res.json();
        }
        
        async function apiGet(url) {
            const res = await fetch(url, {method: 'GET'});
            return await res.json();
        }
    


    const state = { settings: null, traders: [] };
    const MASKED = '••••••••';
    const API_PREFIX = "/api/live";
    const PAGE_PROFILE = "live";

    function apiUrl(path) { return (API_PREFIX || '/api').replace(/\/$/, '') + (path.startsWith('/') ? path : '/' + path); }
    function setMsg(t, type) {
        const b = document.getElementById('page-msg');
        if(!t){ b.classList.add('hidden'); return; }
        const bgColor = type==='error' ? 'bg-red-900/50 text-red-200 border border-red-800' : 'bg-green-900/50 text-green-200 border border-green-800';
        b.className = `px-4 py-2 rounded text-xs flex items-center justify-between ${bgColor}`;
        b.innerHTML = `
            <span>${t}</span>
            <button onclick="document.getElementById('page-msg').classList.add('hidden')" class="ml-4 text-current opacity-70 hover:opacity-100 transition-opacity">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                </svg>
            </button>
        `;
        b.classList.remove('hidden');
        // 启动成功消息不自动消失, 错误消息15秒自动消失, 其他成功8秒
        if (type === 'success_important') {
            // 不自动消失，只能手动关闭
            return;
        }
        const autoCloseTime = type === 'error' ? 15000 : 8000;
        setTimeout(() => b.classList.add('hidden'), autoCloseTime);
    }

    function toggleSection(id) {
        const c = document.getElementById(id), k = id.split('-')[1], l = document.getElementById('label-'+k);
        const h = c.classList.toggle('hidden');
        if(l) l.textContent = h ? '展开' : '收起';
    }

    function formatMoney(v) {
        return (Number(v) || 0).toFixed(2) + ' U';
    }

    function formatRatio(v) {
        return ((Number(v) || 0) * 100).toFixed(1) + '%';
    }

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
    }

    function toNumberOrNull(value) {
        const num = Number(value);
        return Number.isFinite(num) ? num : null;
    }

    function formatMetricNumber(value) {
        const num = toNumberOrNull(value);
        if (num === null) return '--';
        const abs = Math.abs(num);
        const digits = abs >= 100 ? 2 : abs >= 1 ? 3 : 4;
        return num.toFixed(digits).replace(/\.?0+$/, '');
    }

    function formatMoneyOrDash(value) {
        const num = toNumberOrNull(value);
        return num === null ? '--' : `${num.toFixed(2)} U`;
    }

    function formatSignedMoney(value) {
        const num = toNumberOrNull(value);
        if (num === null) return '--';
        return `${num >= 0 ? '+' : ''}${num.toFixed(2)} U`;
    }

    function formatPercentOrDash(value) {
        const num = toNumberOrNull(value);
        if (num === null) return '--';
        return `${num >= 0 ? '+' : ''}${(num * 100).toFixed(2)}%`;
    }

    function getDirectionMeta(direction) {
        if (direction === 'long') {
            return {
                label: '\u591a',
                badgeClass: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
            };
        }
        if (direction === 'short') {
            return {
                label: '\u7a7a',
                badgeClass: 'border-rose-500/30 bg-rose-500/10 text-rose-300',
            };
        }
        return {
            label: '--',
            badgeClass: 'border-zinc-700 bg-zinc-800/60 text-zinc-400',
        };
    }

    function renderPositionMetric(label, value) {
        return `
            <div class="rounded-lg border border-zinc-800 bg-dark-bg/50 px-3 py-2">
                <div class="text-[10px] text-zinc-500">${escapeHtml(label)}</div>
                <div class="mt-1 text-[11px] font-semibold text-white break-all">${escapeHtml(value ?? '--')}</div>
            </div>
        `;
    }

    function renderPositionCard(position, platformLabel) {
        const direction = getDirectionMeta(position.direction);
        const pnl = toNumberOrNull(position.pnl);
        const pnlClass = pnl !== null ? (pnl >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-zinc-400';
        const roi = toNumberOrNull(position.return_rate);
        const roiClass = roi !== null ? (roi >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-zinc-500';
        const leverage = toNumberOrNull(position.leverage);
        const source = position.source && position.source !== '-' ? position.source : '\u672a\u5173\u8054\u6765\u6e90';

        return `
            <article class="rounded-xl border border-zinc-800 bg-dark-bg/40 p-4 shadow-[0_0_0_1px_rgba(255,255,255,0.02)]">
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                        <div class="flex items-center gap-2 flex-wrap">
                            <h5 class="text-sm font-semibold text-white">${escapeHtml(position.symbol || '--')}</h5>
                            <span class="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold ${direction.badgeClass}">${direction.label}</span>
                            <span class="inline-flex items-center rounded-full border border-zinc-700 bg-zinc-800/70 px-2 py-0.5 text-[10px] text-zinc-300">${leverage !== null ? `${formatMetricNumber(leverage)}x` : '--'}</span>
                        </div>
                        <div class="mt-1 text-[10px] text-zinc-500">${escapeHtml(platformLabel)} / ${escapeHtml(source)}</div>
                    </div>
                    <div class="text-right shrink-0">
                        <div class="text-base font-semibold ${pnlClass}">${formatSignedMoney(position.pnl)}</div>
                        <div class="mt-1 text-[10px] ${roiClass}">\u6536\u76ca\u7387 ${formatPercentOrDash(position.return_rate)}</div>
                    </div>
                </div>
                <div class="mt-4 grid grid-cols-2 lg:grid-cols-4 gap-2">
                    ${renderPositionMetric('\u4ea4\u6613\u5bf9', position.symbol || '--')}
                    ${renderPositionMetric('\u65b9\u5411', direction.label)}
                    ${renderPositionMetric('\u6570\u91cf', formatMetricNumber(position.qty))}
                    ${renderPositionMetric('\u6760\u6746', leverage !== null ? `${formatMetricNumber(leverage)}x` : '--')}
                    ${renderPositionMetric('\u5f00\u4ed3\u4ef7', formatMetricNumber(position.open_price))}
                    ${renderPositionMetric('\u4fdd\u8bc1\u91d1', formatMoneyOrDash(position.margin))}
                    ${renderPositionMetric('\u6536\u76ca\u7387', formatPercentOrDash(position.return_rate))}
                    ${renderPositionMetric('\u6765\u6e90', source)}
                </div>
            </article>
        `;
    }

    function renderPositionPanel(prefix, itemsInput, error, platformLabel) {
        const listEl = document.getElementById(`${prefix}-positions-grid`);
        const emptyEl = document.getElementById(`${prefix}-positions-empty`);
        const summaryEl = document.getElementById(`${prefix}-positions-summary`);
        const pnlEl = document.getElementById(`${prefix}-positions-pnl`);
        const items = Array.isArray(itemsInput) ? itemsInput : [];
        const totalMargin = items.reduce((sum, item) => sum + (toNumberOrNull(item.margin) || 0), 0);
        const totalPnl = items.reduce((sum, item) => sum + (toNumberOrNull(item.pnl) || 0), 0);

        if (!items.length) {
            listEl.innerHTML = '';
            emptyEl.classList.remove('hidden');
            emptyEl.className = `mt-4 rounded-xl border border-dashed px-4 py-6 text-center text-[11px] ${error ? 'border-rose-500/30 bg-rose-500/5 text-rose-300' : 'border-zinc-800 bg-dark-bg/40 text-zinc-500'}`;
            emptyEl.textContent = error || `\u6682\u65e0 ${platformLabel} \u6301\u4ed3`;
            summaryEl.textContent = error ? '\u8bfb\u53d6\u5931\u8d25' : `0 \u4e2a\u6301\u4ed3`;
            pnlEl.textContent = '--';
            pnlEl.className = 'text-sm font-semibold text-zinc-400';
            return;
        }

        emptyEl.classList.add('hidden');
        listEl.innerHTML = items.map((item) => renderPositionCard(item, platformLabel)).join('');
        summaryEl.textContent = `${items.length} \u4e2a\u6301\u4ed3 / \u4fdd\u8bc1\u91d1 ${formatMoney(totalMargin)}`;
        pnlEl.textContent = formatSignedMoney(totalPnl);
        pnlEl.className = `text-sm font-semibold ${totalPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
    }

    function renderAllocationPanel(prefix, summary, tradersMap) {
        const overviewEl = document.getElementById(`${prefix}-allocation-overview`);
        const tierEl = document.getElementById(`${prefix}-tier-list`);
        const traderEl = document.getElementById(`${prefix}-trader-list`);
        const metaEl = document.getElementById(`${prefix}-allocation-meta`);
        const platform = summary || {};
        const traders = Object.entries(tradersMap || {})
            .filter(([, trader]) => trader && trader.copy_enabled)
            .sort((a, b) => (a[1]?.tier || '').localeCompare(b[1]?.tier || '') || (a[1]?.nickname || '').localeCompare(b[1]?.nickname || ''));
        const overviewItems = [
            ['总资金池', formatMoney(platform.total_capital)],
            ['启用交易员', `${platform.enabled_count || 0} 人`],
            ['人均分配池', formatMoney(platform.pool_per_trader)],
            ['单人上限', formatMoney(platform.effective_margin_cap)],
        ];
        overviewEl.innerHTML = overviewItems.map(([label, value]) => `
            <div class="rounded-lg border border-zinc-800 bg-dark-card/50 px-3 py-2">
                <div class="text-[10px] text-zinc-500">${label}</div>
                <div class="mt-1 font-semibold text-white">${value}</div>
            </div>
        `).join('');
        metaEl.textContent = `可用余额上限 ${formatMoney(platform.available_margin_cap)}`;

        const tiers = platform.tiers || [];
        tierEl.innerHTML = tiers.length ? tiers.map((tier) => `
            <div class="rounded-lg border border-zinc-800 bg-dark-card/50 px-3 py-2">
                <div class="flex items-center justify-between gap-3">
                    <div>
                        <div class="font-semibold text-white">${escapeHtml(tier.tier_label || tier.tier || '未分类')}</div>
                        <div class="text-[10px] text-zinc-500">${tier.enabled_count || 0} 人</div>
                    </div>
                    <div class="text-right">
                        <div class="font-semibold text-white">${formatMoney(tier.effective_margin_cap)}</div>
                        <div class="text-[10px] text-zinc-500">池子 ${formatMoney(tier.allocation_pool)}</div>
                    </div>
                </div>
            </div>
        `).join('') : '<div class="text-[11px] text-zinc-500">暂无启用分类</div>';

        traderEl.innerHTML = traders.length ? traders.map(([pid, trader]) => {
            const ratioKey = `${prefix}_effective_follow_ratio`;
            const capKey = `${prefix}_effective_margin_cap`;
            const poolKey = `${prefix}_allocation_pool`;
            const sizingKey = `${prefix}_sizing_mode`;
            const sizingMode = trader[sizingKey] === 'tier_ratio' ? '分类比例' : '全局比例';
            return `
                <div class="rounded-lg border border-zinc-800 bg-dark-card/50 px-3 py-2">
                    <div class="flex items-center justify-between gap-3">
                        <div class="min-w-0">
                            <div class="font-semibold text-white truncate">${escapeHtml(trader.nickname || pid.slice(0, 8))}</div>
                            <div class="text-[10px] text-zinc-500">${escapeHtml(trader.tier_label || trader.tier || '未分类')} · ${sizingMode}</div>
                        </div>
                        <div class="text-right">
                            <div class="font-semibold text-white">${formatMoney(trader[capKey])}</div>
                            <div class="text-[10px] text-zinc-500">比例 ${formatRatio(trader[ratioKey])} · 池子 ${formatMoney(trader[poolKey])}</div>
                        </div>
                    </div>
                </div>
            `;
        }).join('') : '<div class="text-[11px] text-zinc-500">暂无启用交易员</div>';
    }

    function renderRiskGuardSummary(settings) {
        const badge = document.getElementById('risk-guard-badge');
        const overview = document.getElementById('risk-guard-overview');
        const detail = document.getElementById('risk-guard-detail');
        const enabled = !!settings?.take_profit_enabled;
        badge.textContent = enabled ? '保护已启用' : '保护未启用';
        badge.className = `text-[10px] ${enabled ? 'text-green-400' : 'text-zinc-500'}`;

        const overviewItems = [
            ['止损', formatRatio(settings?.stop_loss_pct)],
            ['TP1', `${formatRatio(settings?.tp1_roi_pct)} / 平 ${formatRatio(settings?.tp1_close_pct)}`],
            ['TP2', `${formatRatio(settings?.tp2_roi_pct)} / 平 ${formatRatio(settings?.tp2_close_pct)}`],
            ['TP3', `${formatRatio(settings?.tp3_roi_pct)} / 平 ${formatRatio(settings?.tp3_close_pct)}`],
        ];
        overview.innerHTML = overviewItems.map(([label, value]) => `
            <div class="rounded-lg border border-zinc-800 bg-dark-card/50 px-3 py-2">
                <div class="text-[10px] text-zinc-500">${label}</div>
                <div class="mt-1 font-semibold text-white">${value}</div>
            </div>
        `).join('');

        const detailItems = [
            ['保本上移', formatRatio(settings?.breakeven_buffer_pct)],
            ['回撤止盈', formatRatio(settings?.trail_callback_pct)],
            ['日亏损上限', formatRatio(settings?.daily_loss_limit_pct)],
            ['总回撤上限', formatRatio(settings?.total_drawdown_limit_pct)],
        ];
        detail.innerHTML = detailItems.map(([label, value]) => `
            <div class="rounded-lg border border-zinc-800 bg-dark-card/50 px-3 py-2 flex items-center justify-between gap-3">
                <span class="text-zinc-400">${label}</span>
                <span class="font-semibold text-white">${value}</span>
            </div>
        `).join('');
    }

    function renderAllocationSummary(settings) {
        const summary = settings?.allocation_summary || {};
        const traders = settings?.binance_traders || {};
        renderAllocationPanel('bitget', summary.bitget || {}, traders);
        renderAllocationPanel('binance', summary.binance || {}, traders);
    }


    function formatDateTime(value) {
        const num = toNumberOrNull(value);
        if (num === null || num <= 0) return '--';
        const d = new Date(num);
        const pad = (v) => String(v).padStart(2, '0');
        return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function formatSeconds(value) {
        const num = toNumberOrNull(value);
        if (num === null) return '--';
        if (num < 60) return `${num.toFixed(1)}s`;
        if (num < 3600) return `${(num / 60).toFixed(1)}m`;
        return `${(num / 3600).toFixed(1)}h`;
    }

    function renderDiagnostics(payload) {
        if (PAGE_PROFILE !== 'live') return;
        const summaryEl = document.getElementById('diagnostics-summary');
        const checksEl = document.getElementById('diagnostics-checks');
        const traderEl = document.getElementById('trader-diagnostics');
        const emptyEl = document.getElementById('diagnostics-empty');
        if (!summaryEl || !checksEl || !traderEl || !emptyEl) return;
        const checks = Array.isArray(payload?.checks) ? payload.checks : [];
        const traders = Array.isArray(payload?.trader_polling) ? payload.trader_polling : [];
        const warningCount = traders.filter((item) => item?.status && item.status !== 'pass').length;
        const summaryItems = [
            ['\u603b\u4f53\u72b6\u6001', payload?.overall || '--'],
            ['\u542f\u7528\u5bf9\u8c61', String(payload?.enabled_trader_count ?? '--')],
            ['\u8f6e\u8be2\u5f02\u5e38', String(warningCount)],
            ['\u751f\u6210\u65f6\u95f4', formatDateTime(payload?.generated_at)],
        ];
        summaryEl.innerHTML = summaryItems.map(([label, value]) => `
            <div class="rounded-lg border border-zinc-800 bg-dark-bg/40 px-3 py-2">
                <div class="text-[10px] text-zinc-500">${escapeHtml(label)}</div>
                <div class="mt-1 font-semibold text-white">${escapeHtml(value)}</div>
            </div>
        `).join('');

        checksEl.innerHTML = checks.map((item) => {
            const status = item?.status || 'warning';
            const statusClass = status === 'pass'
                ? 'border-emerald-500/20 bg-emerald-500/5 text-emerald-300'
                : status === 'blocker'
                    ? 'border-rose-500/20 bg-rose-500/5 text-rose-300'
                    : 'border-amber-500/20 bg-amber-500/5 text-amber-300';
            return `
                <div class="rounded-lg border px-3 py-2 ${statusClass}">
                    <div class="flex items-center justify-between gap-3">
                        <div class="font-semibold">${escapeHtml(item?.label || '--')}</div>
                        <div class="text-[10px] uppercase tracking-wide">${escapeHtml(status)}</div>
                    </div>
                    <div class="mt-1 text-[11px] opacity-90">${escapeHtml(item?.detail || '--')}</div>
                </div>
            `;
        }).join('');

        if (!traders.length) {
            traderEl.innerHTML = '';
            emptyEl.classList.remove('hidden');
            return;
        }

        emptyEl.classList.add('hidden');
        traderEl.innerHTML = traders.map((item) => {
            const status = item?.status || 'warning';
            const statusClass = status === 'pass'
                ? 'border-emerald-500/20 bg-emerald-500/5 text-emerald-300'
                : 'border-amber-500/20 bg-amber-500/5 text-amber-300';
            const lagValue = item?.catchup_lag_sec == null ? '--' : formatSeconds(item.catchup_lag_sec);
            const pollAge = item?.poll_age_sec == null ? '--' : formatSeconds(item.poll_age_sec);
            const remoteLine = item?.remote_symbol
                ? `${item.remote_symbol} / ${item.remote_action || '--'} / ${formatDateTime(item.remote_order_time)}`
                : '--';
            const dbLine = item?.db_symbol
                ? `${item.db_symbol} / ${item.db_action || '--'} / ${formatDateTime(item.db_order_time)}`
                : '--';
            const cursorLine = item?.cursor_order_time
                ? `${formatDateTime(item.cursor_order_time)} / ${item.cursor_order_id || '--'}`
                : '--';
            const warmupLine = item?.warmup_status
                ? `${item.warmup_status}${item.warmup_seed_count ? ` / ${item.warmup_seed_count} \u6761` : ''}`
                : '--';
            return `
                <article class="rounded-xl border border-zinc-800 bg-dark-bg/40 p-4">
                    <div class="flex items-start justify-between gap-3">
                        <div class="min-w-0">
                            <div class="text-sm font-semibold text-white truncate">${escapeHtml(item?.nickname || item?.trader_uid || '--')}</div>
                            <div class="mt-1 text-[10px] text-zinc-500 break-all">${escapeHtml(item?.trader_uid || '--')}</div>
                        </div>
                        <span class="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold ${statusClass}">${escapeHtml(status)}</span>
                    </div>
                    <div class="mt-4 grid grid-cols-2 gap-2 text-[11px]">
                        ${renderPositionMetric('\u6700\u540e\u8f6e\u8be2', formatDateTime(item?.last_poll_finished_at_ms))}
                        ${renderPositionMetric('\u8f6e\u8be2\u5e74\u9f84', pollAge)}
                        ${renderPositionMetric('\u65b0\u5355\u6570\u91cf', String(item?.last_new_order_count ?? 0))}
                        ${renderPositionMetric('Lag', lagValue)}
                        ${renderPositionMetric('\u70ed\u8eab\u72b6\u6001', warmupLine)}
                        ${renderPositionMetric('\u6e38\u6807', cursorLine)}
                        ${renderPositionMetric('\u6e90\u7aef\u6700\u8fd1\u5355', remoteLine)}
                        ${renderPositionMetric('\u672c\u5730\u6700\u8fd1\u5355', dbLine)}
                    </div>
                    ${item?.last_poll_error ? `<div class="mt-3 rounded-lg border border-rose-500/20 bg-rose-500/5 px-3 py-2 text-[11px] text-rose-300">${escapeHtml(item.last_poll_error)}</div>` : ''}
                </article>
            `;
        }).join('');
    }

    async function refreshDiagnostics() {
        if (PAGE_PROFILE !== 'live') return;
        try {
            const res = await apiGet(apiUrl('/diagnostics'));
            renderDiagnostics(res);
        } catch (e) {
            console.error('Failed to fetch diagnostics:', e);
        }
    }

    async function loadSettings() {
        const res = await apiGet(apiUrl('/copy/settings'));
        state.settings = res;
        document.getElementById('api_key').value = res.api_key || '';
        document.getElementById('api_secret').value = res.api_secret ? MASKED : '';
        document.getElementById('api_passphrase').value = res.api_passphrase ? MASKED : '';
        document.getElementById('binance_api_key').value = res.binance_api_key || '';
        document.getElementById('binance_api_secret').value = res.binance_api_secret ? MASKED : '';
        document.getElementById('total_capital').value = res.total_capital || '';
        document.getElementById('follow_ratio_pct').value = ((res.follow_ratio_pct || 0.003) * 100).toFixed(2);
        document.getElementById('binance_total_capital').value = res.binance_total_capital || '';
        document.getElementById('binance_follow_ratio_pct').value = ((res.binance_follow_ratio_pct || 0.003) * 100).toFixed(2);
        document.getElementById('entry_order_mode').value = res.entry_order_mode || 'maker_limit';
        document.getElementById('price_tolerance_pct').value = ((res.price_tolerance || 0.01) * 100).toFixed(1);
        document.getElementById('take_profit_enabled').checked = !!res.take_profit_enabled;
        document.getElementById('stop_loss_pct').value = ((res.stop_loss_pct || 0.06) * 100).toFixed(2);
        renderRiskGuardSummary(res);
        renderAllocationSummary(res);
        updateEngineBadge(res.engine_enabled);
        
        // 同时更新实时状态
        try {
            const status = await apiGet(apiUrl('/status'));
            const engineRunning = typeof status.current_copy_engine_running === 'boolean'
                ? status.current_copy_engine_running
                : (status.copy_engine_running || status.sim_copy_engine_running || status.live_copy_engine_running);
            updateEngineBadge(engineRunning);
        } catch (e) {
            console.error('Failed to fetch real-time status:', e);
        }
    }

    async function saveSettings() {
        const payload = {
            api_key: document.getElementById('api_key').value.trim(),
            api_secret: document.getElementById('api_secret').value === MASKED ? '' : document.getElementById('api_secret').value,
            api_passphrase: document.getElementById('api_passphrase').value === MASKED ? '' : document.getElementById('api_passphrase').value,
            binance_api_key: document.getElementById('binance_api_key').value.trim(),
            binance_api_secret: document.getElementById('binance_api_secret').value === MASKED ? '' : document.getElementById('binance_api_secret').value,
            total_capital: parseFloat(document.getElementById('total_capital').value) || 0,
            follow_ratio_pct: (parseFloat(document.getElementById('follow_ratio_pct').value) || 0) / 100,
            binance_total_capital: parseFloat(document.getElementById('binance_total_capital').value) || 0,
            binance_follow_ratio_pct: (parseFloat(document.getElementById('binance_follow_ratio_pct').value) || 0) / 100,
            entry_order_mode: document.getElementById('entry_order_mode').value,
            price_tolerance: (parseFloat(document.getElementById('price_tolerance_pct').value) || 1.0) / 100,
            take_profit_enabled: document.getElementById('take_profit_enabled').checked,
            stop_loss_pct: (parseFloat(document.getElementById('stop_loss_pct').value) || 0) / 100,
            binance_traders: state.settings.binance_traders,
            enabled_traders: state.settings.enabled_traders
        };
        const res = await apiPost(apiUrl('/copy/settings'), payload);
        if (res.error) setMsg(res.error, 'error'); else { setMsg('保存成功', 'success'); loadSettings(); }
    }

    async function saveBitgetSettings() { await saveSettings(); }
    async function saveBinanceSettings() { await saveSettings(); }

    async function testApi() {
        const s = document.getElementById('api-status'); s.textContent = '...';
        const res = await apiPost(apiUrl('/copy/test_api'), { api_key: document.getElementById('api_key').value.trim(), api_secret: document.getElementById('api_secret').value === MASKED ? '' : document.getElementById('api_secret').value, api_passphrase: document.getElementById('api_passphrase').value === MASKED ? '' : document.getElementById('api_passphrase').value });
        s.textContent = res.error ? '失败' : '通过'; s.className = `text-[10px] ${res.error?'text-red-400':'text-green-400'}`;
    }

    async function testBinanceApi() {
        const s = document.getElementById('binance-api-status'); s.textContent = '...';
        const res = await apiPost(apiUrl('/copy/test_api'), { source: 'binance', binance_api_key: document.getElementById('binance_api_key').value.trim(), binance_api_secret: document.getElementById('binance_api_secret').value === MASKED ? '' : document.getElementById('binance_api_secret').value });
        s.textContent = res.error ? '失败' : '通过'; s.className = `text-[10px] ${res.error?'text-red-400':'text-green-400'}`;
    }

    function updateEngineBadge(running) {
        const b = document.getElementById('badge-text'), badge = document.getElementById('engine-badge');
        b.textContent = running ? '运行中' : '已停止';
        badge.className = `px-3 py-1 rounded-lg border text-xs font-medium ${running?'bg-green-900/20 text-green-400 border-green-800':'bg-dark-card text-zinc-500 border-zinc-700'}`;
        
        // 同步更新顶部导航栏的引擎状态(如果存在)
        const topEngineStatus = window.parent?.document?.getElementById?.('engine-status') || document.getElementById('engine-status');
        if (topEngineStatus) {
            if (running) {
                topEngineStatus.classList.remove('hidden');
                topEngineStatus.classList.add('flex');
            } else {
                topEngineStatus.classList.add('hidden');
                topEngineStatus.classList.remove('flex');
            }
        }
    }

    async function toggleEngine(start) {
        const btnStart = document.getElementById('btn-start');
        const btnStop = document.getElementById('btn-stop');
        
        // 禁用按钮,防止重复点击
        if (btnStart) btnStart.disabled = true;
        if (btnStop) btnStop.disabled = true;
        
        try {
            const res = await apiPost(start ? apiUrl('/copy/start') : apiUrl('/copy/stop'), {});
            if (res.error) {
                setMsg(res.error, 'error');
            } else {
                // 显示成功消息 - 启动消息保持30秒
                const message = res.msg || (start ? '引擎启动成功' : '引擎已停止');
                setMsg(message, start ? 'success_important' : 'success');
                
                // 立即更新状态
                updateEngineBadge(start);
                
                // 2秒后刷新持仓和订单数据
                if (start) {
                    setTimeout(() => {
                        refreshPositions();
                        refreshOrders();
                    }, 2000);
                }
            }
        } catch (e) {
            setMsg('操作失败: ' + e.message, 'error');
        } finally {
            // 重新启用按钮
            if (btnStart) btnStart.disabled = false;
            if (btnStop) btnStop.disabled = false;
        }
    }

    async function refreshPositions() {
        const res = await apiGet(apiUrl('/copy/positions'));
        renderPositionPanel('bitget', res.bitget_items, res.bitget_error, 'Bitget');
        renderPositionPanel('binance', res.binance_items, res.binance_error, 'Binance');
        if(res.account_overview){
            document.getElementById('wallet-balance-value').textContent = (res.account_overview.wallet_balance || 0).toFixed(2) + ' U';
            const pnl = res.account_overview.day_pnl || 0, pnlEl = document.getElementById('wallet-day-pnl-value');
            pnlEl.textContent = (pnl>=0?'+':'')+pnl.toFixed(2)+' U'; pnlEl.className = `text-xs font-bold ${pnl>=0?'text-green-400':'text-red-400'}`;
        }
        loadBinanceBalance();
    }

    async function loadBinanceBalance() {
        try {
            const res = await apiGet(apiUrl('/binance/balance'));
            if(res.error) return;
            document.getElementById('bn-wallet-balance').textContent = (res.wallet_balance || 0).toFixed(2) + ' U';
            const pnl = res.day_pnl || 0, pnlEl = document.getElementById('bn-wallet-pnl');
            pnlEl.textContent = (pnl>=0?'+':'')+pnl.toFixed(2)+' U'; pnlEl.className = `text-xs font-bold ${pnl>=0?'text-green-400':'text-red-400'}`;
        } catch(e) {}
    }

    async function refreshOrders() {
        const res = await apiGet(apiUrl('/copy/orders?page=1&page_size=20'));
        const body = document.getElementById('orders-body'), empty = document.getElementById('orders-empty'), items = res.items || [];
        body.innerHTML = '';
        if(!items.length) empty.classList.remove('hidden'); else {
            empty.classList.add('hidden');
            items.forEach(o => {
                const tr = document.createElement('tr'), d = new Date(o.timestamp);
                tr.innerHTML = `<td class="px-4 py-2 text-zinc-500">${d.getMonth()+1}-${d.getDate()} ${d.getHours()}:${d.getMinutes()}</td><td class="px-2 py-2 text-blue-400">${o.trader_name||'-'}</td><td class="px-2 py-2 font-bold">${o.symbol}</td><td class="text-center px-2 py-2">${o.action==='open'?'<span class="text-green-400">开</span>':'<span class="text-red-400">平</span>'}</td><td class="text-right px-2 py-2">${o.exec_price||'-'}</td><td class="text-center px-2 py-2">${o.status}</td>`;
                body.appendChild(tr);
            });
        }
    }

    loadSettings().then(() => { refreshPositions(); refreshOrders(); if (PAGE_PROFILE === 'live') refreshDiagnostics(); });
    setInterval(refreshPositions, 10000);
    if (PAGE_PROFILE === 'live') setInterval(refreshDiagnostics, 10000);
    
    // 定期检查引擎状态(每5秒)
    setInterval(async () => {
        try {
            const status = await apiGet(apiUrl('/status'));
            const engineRunning = typeof status.current_copy_engine_running === 'boolean'
                ? status.current_copy_engine_running
                : (status.copy_engine_running || status.sim_copy_engine_running || status.live_copy_engine_running);
            updateEngineBadge(engineRunning);
        } catch (e) {
            console.error('Failed to check engine status:', e);
        }
    }, 5000);
