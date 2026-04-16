// Redirect to login if not authenticated
if (!sessionStorage.getItem('idToken')) {
    window.location.href = 'index.html';
}

const API_URL = 'https://h4jxtox6vte7khp5ruplxsjiki0jktxc.lambda-url.us-east-1.on.aws/';

// Persistent session ID for conversation context
let chatSessionId = crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);

// Pending metrics query — stores params when disambiguation is needed
let pendingMetricsQuery = null;

const chatMessages = document.getElementById('chat-messages');
const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');

// Sidebar suggestion click
function useSuggestion(btn) {
    const text = btn.textContent.trim();
    chatInput.value = text;
    chatInput.focus();
}
const loadingIndicator = document.getElementById('loading-indicator');
const sendBtn = document.getElementById('send-btn');

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatBotMessage(text) {
    // Detect code blocks (```csv, ```json, or ```)
    const codeBlockRegex = /```\s*(\w*)\n?([\s\S]*?)```/g;
    let result = '';
    let lastIndex = 0;
    let match;
    let downloadData = null;
    let downloadExt = null;

    while ((match = codeBlockRegex.exec(text)) !== null) {
        // Text before code block
        if (match.index > lastIndex) {
            result += escapeHtml(text.substring(lastIndex, match.index));
        }
        const lang = match[1].toLowerCase() || 'text';
        const code = match[2].trim();

        // If it looks like CSV or JSON data, offer download
        if (lang === 'csv' || lang === 'json' || code.includes('channel_id,') || code.startsWith('[{')) {
            downloadData = code;
            downloadExt = lang === 'json' || code.startsWith('[{') ? 'json' : 'csv';
            result += '<pre style="background:var(--bg-tertiary);padding:12px;border-radius:8px;overflow-x:auto;font-size:0.8rem;max-height:300px;overflow-y:auto;border:1px solid var(--border);">' + escapeHtml(code.substring(0, 2000)) + (code.length > 2000 ? '\n... (truncado)' : '') + '</pre>';
        } else {
            result += '<pre style="background:var(--bg-tertiary);padding:12px;border-radius:8px;overflow-x:auto;font-size:0.85rem;">' + escapeHtml(code) + '</pre>';
        }
        lastIndex = codeBlockRegex.lastIndex;
    }

    // Remaining text
    if (lastIndex < text.length) {
        result += escapeHtml(text.substring(lastIndex));
    }

    // Add download button if we found data
    if (downloadData) {
        const id = 'dl-' + Date.now();
        result += '<button id="' + id + '" style="margin-top:8px;padding:8px 16px;background:var(--user-bubble);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:0.85rem;">📥 Baixar ' + downloadExt.toUpperCase() + '</button>';
        setTimeout(() => {
            const btn = document.getElementById(id);
            if (btn) {
                btn.onclick = () => {
                    const blob = new Blob([downloadData], {type: downloadExt === 'json' ? 'application/json' : 'text/csv'});
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'export.' + downloadExt;
                    a.click();
                    URL.revokeObjectURL(url);
                };
            }
        }, 100);
    }

    return result || escapeHtml(text);
}

// Also handle URLs in non-code text
function linkify(text) {
    const urlRegex = /(https?:\/\/[^\s<>"']+)/g;
    const parts = [];
    let lastIndex = 0;
    let match;
    while ((match = urlRegex.exec(text)) !== null) {
        if (match.index > lastIndex) parts.push(escapeHtml(text.substring(lastIndex, match.index)));
        const url = match[1];
        // Pre-signed S3 URLs → download button
        if (url.includes('X-Amz-') || url.includes('.s3.amazonaws.com')) {
            const id = 'dl-link-' + Date.now() + Math.random().toString(36).slice(2,6);
            parts.push('<a id="' + id + '" href="' + escapeHtml(url) + '" target="_blank" rel="noopener" style="display:inline-block;margin:6px 0;padding:8px 16px;background:var(--user-bubble);color:#fff;border-radius:6px;text-decoration:none;font-size:0.85rem;">📥 Baixar JSON</a>');
        } else {
            parts.push('<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener" style="color:var(--accent);">🔗 Link</a>');
        }
        lastIndex = urlRegex.lastIndex;
    }
    if (lastIndex < text.length) parts.push(escapeHtml(text.substring(lastIndex)));
    return parts.length > 0 ? parts.join('') : escapeHtml(text);
}

function addMessage(text, type) {
    const msg = document.createElement('div');
    msg.classList.add('message', 'message-' + type);

    if (type === 'error') {
        msg.setAttribute('role', 'alert');
    }

    if (type === 'bot') {
        // Check for code blocks first, then URLs
        if (text.includes('```')) {
            msg.innerHTML = formatBotMessage(text);
        } else {
            msg.innerHTML = linkify(text);
        }
    } else {
        msg.innerHTML = escapeHtml(text);
    }
    chatMessages.appendChild(msg);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function showLoading() {
    loadingIndicator.classList.add('visible');
    loadingIndicator.setAttribute('aria-hidden', 'false');
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function hideLoading() {
    loadingIndicator.classList.remove('visible');
    loadingIndicator.setAttribute('aria-hidden', 'true');
}

function setInputEnabled(enabled) {
    chatInput.disabled = !enabled;
    sendBtn.disabled = !enabled;
}

function isExportRequest(text) {
    const lower = text.toLowerCase();
    return lower.includes('exportar') || lower.includes('export') ||
           (lower.includes('gerar') && (lower.includes('csv') || lower.includes('json'))) ||
           lower.includes('todos os canais') || lower.includes('quais canais') ||
           lower.includes('quantos canais') || lower.includes('liste os canais') ||
           lower.includes('lista de canais') || lower.includes('me retorne') ||
           lower.includes('quais são') || lower.includes('quais estão') ||
           lower.includes('quais tem') || lower.includes('quais têm') ||
           (lower.includes('canais') && lower.includes('audio')) ||
           (lower.includes('canais') && lower.includes('áudio')) ||
           (lower.includes('canais') && lower.includes('codec')) ||
           (lower.includes('canais') && lower.includes('resolução')) ||
           (lower.includes('canais') && lower.includes('resolucao')) ||
           (lower.includes('canais') && lower.includes('bitrate')) ||
           (lower.includes('canais') && lower.includes('framerate')) ||
           (lower.includes('canais') && lower.includes('legenda')) ||
           (lower.includes('canais') && lower.includes('failover')) ||
           (lower.includes('canais') && lower.includes('low latency')) ||
           (lower.includes('canais') && lower.includes('medialive')) ||
           (lower.includes('canais') && lower.includes('mediapackage')) ||
           (lower.includes('canais') && lower.includes('mediatailor')) ||
           (lower.includes('canais') && lower.includes('cloudfront'));
}

function extractFilters(text) {
    const lower = text.toLowerCase();
    const filtros = {};

    // Service filter
    if (lower.includes('medialive')) filtros.servico = 'MediaLive';
    else if (lower.includes('mediapackage')) filtros.servico = 'MediaPackage';
    else if (lower.includes('mediatailor')) filtros.servico = 'MediaTailor';
    else if (lower.includes('cloudfront')) filtros.servico = 'CloudFront';

    // Estado filter
    if (lower.includes('running')) filtros.estado = 'RUNNING';
    else if (lower.includes('idle')) filtros.estado = 'IDLE';
    else if (lower.includes('creating')) filtros.estado = 'CREATING';
    else if (lower.includes('deleting')) filtros.estado = 'DELETING';

    // Codec filter
    if (lower.includes('h.264') || lower.includes('h264')) filtros.codec_video = 'H.264';
    else if (lower.includes('h.265') || lower.includes('h265')) filtros.codec_video = 'H.265';

    // Segment length
    const segMatch = lower.match(/segmento?\s+(?:de\s+)?(\d+)/);
    if (segMatch) filtros.segment_length = parseInt(segMatch[1]);

    // Failover
    if (lower.includes('failover')) filtros.failover_enabled = true;

    // Low latency
    if (lower.includes('low latency') || lower.includes('baixa lat')) filtros.low_latency = true;

    // DRM
    if (lower.includes('fairplay')) filtros.endpoint_hls_drm = 'FAIRPLAY';
    if (lower.includes('widevine')) filtros.endpoint_dash_drm = 'WIDEVINE';

    // Framerate
    const frMatch = lower.match(/framerate\s+(\d+[\.,]?\d*)/);
    if (frMatch) filtros.framerate = parseFloat(frMatch[1].replace(',', '.'));

    // Channel name substring (e.g., "canais Globo", "canais ESPN")
    const nameMatch = lower.match(/cana(?:is|l)\s+(?:do\s+|da\s+)?(\w+)/);
    if (nameMatch) {
        const candidate = nameMatch[1].toUpperCase();
        // Exclude service names and generic words
        const exclude = ['MEDIALIVE', 'MEDIAPACKAGE', 'MEDIATAILOR', 'CLOUDFRONT',
                         'TODOS', 'ESTADO', 'RUNNING', 'IDLE', 'COM', 'EM', 'QUE',
                         'CREATING', 'DELETING', 'MEDIA', 'LIVE', 'ESTÃO', 'ESTAO',
                         'EST', 'TEM', 'TÊM', 'SAO', 'SÃO', 'USAM', 'EXISTEM',
                         'CODEC', 'LOW', 'LATENCY', 'AUDIO', 'AUDIOS', 'ÁUDIO',
                         'LEGENDA', 'LEGENDAS', 'OUTPUT', 'BITRATE', 'FRAMERATE',
                         'SEGMENTO', 'RESOLUÇÃO', 'RESOLUCAO'];
        if (!exclude.includes(candidate) && candidate.length > 2) {
            filtros.nome_canal_contains = candidate;
        }
    }

    // Audio count filter (e.g., "canais com 3 audios")
    const audioMatch = lower.match(/(\d+)\s+(?:áudio|audio)/);
    if (audioMatch) filtros.audio_count = parseInt(audioMatch[1]);

    // Caption/subtitle filter
    if (lower.includes('legenda') || lower.includes('subtitle') || lower.includes('dvb_sub')) {
        filtros.caption_count = 1;  // at least 1 caption
    }

    return filtros;
}

function isMassHealthCheck(text) {
    const lower = text.toLowerCase();
    return (lower.includes('saúde') || lower.includes('saude') || lower.includes('health check') || lower.includes('healthcheck') || lower.includes('dashboard de saúde') || lower.includes('dashboard de saude')) &&
           (lower.includes('todos') || lower.includes('todas') || lower.includes('geral') || lower.includes('dashboard') || lower.includes('resumo'));
}

function extractHealthcheckParams(text) {
    const lower = text.toLowerCase();
    const params = {};
    if (lower.includes('medialive')) params.servico = 'MediaLive';
    else if (lower.includes('mediapackage')) params.servico = 'MediaPackage';
    else if (lower.includes('mediatailor')) params.servico = 'MediaTailor';
    else if (lower.includes('cloudfront')) params.servico = 'CloudFront';
    return params;
}

function isAdRequest(text) {
    // Returns true if the text is about ads/SpringServe — must NOT go to direct metrics
    const lower = text.toLowerCase();
    return lower.includes('springserve') ||
           lower.includes('spring serve') ||
           lower.includes('supply tag') ||
           lower.includes('demand tag') ||
           lower.includes('ad server') ||
           lower.includes(' ads ') ||
           lower.endsWith(' ads') ||
           lower.startsWith('ads ') ||
           lower === 'ads' ||
           /\bads\b/.test(lower) ||
           lower.includes('anúncio') ||
           lower.includes('anuncio') ||
           lower.includes('impressões') ||
           lower.includes('impressoes') ||
           lower.includes('receita de') ||
           lower.includes('delivery modifier') ||
           lower.includes('fill rate') && (lower.includes('springserve') || lower.includes('ad'));
}

function isMetricsRequest(text) {
    const lower = text.toLowerCase();
    // Mass health check goes to Bedrock agent, not direct metrics
    if (isMassHealthCheck(text)) return false;
    // Ad/SpringServe queries go to Bedrock agent (KB_ADS), not direct metrics
    if (isAdRequest(text)) return false;
    // Broad detection — any mention of metrics, status, health, monitoring
    return lower.includes('métricas') || lower.includes('metricas') ||
           lower.includes('métrica') || lower.includes('metrica') ||
           (lower.includes('status') && lower.includes('canal')) ||
           (lower.includes('saúde') || lower.includes('saude')) ||
           (lower.includes('como está') || lower.includes('como esta')) ||
           lower.includes('monitoramento') ||
           lower.includes('alertas do canal') ||
           lower.includes('erros do canal') ||
           lower.includes('fill rate') ||
           lower.includes('taxa de erro') ||
           lower.includes('framerate do canal') ||
           lower.includes('bitrate do canal') ||
           lower.includes('network do canal');
}

function extractMetricsParams(text) {
    const lower = text.toLowerCase();
    const params = { servico: 'MediaLive' };

    // Detect service
    if (lower.includes('mediapackage')) params.servico = 'MediaPackage';
    else if (lower.includes('mediatailor')) params.servico = 'MediaTailor';
    else if (lower.includes('cloudfront')) params.servico = 'CloudFront';

    // Extract resource name — multiple patterns
    const patterns = [
        /(?:canal|distribuição|distribuicao|configuração|config)\s+(\S+)/i,
        /(?:métricas|metricas|métrica|metrica|status|saúde|saude)\s+(?:do|da|de|das|dos)\s+(?:canal\s+)?(\S+)/i,
        /(?:como\s+está|como\s+esta)\s+(?:o\s+)?(?:canal\s+)?(\S+)/i,
    ];
    for (const pat of patterns) {
        const m = text.match(pat);
        if (m && m[1]) {
            // Clean trailing punctuation
            params.resource_id = m[1].replace(/[?!.,;]+$/, '');
            break;
        }
    }

    // Extract period
    const periodMatch = lower.match(/(?:últimos?|ultima|último|ultimas|ultimos)\s+(\d+)\s+(?:horas?|h)/);
    if (periodMatch) params.periodo_minutos = parseInt(periodMatch[1]) * 60;
    else {
        const minMatch = lower.match(/(?:últimos?|ultima|último)\s+(\d+)\s+(?:minutos?|min)/);
        if (minMatch) params.periodo_minutos = parseInt(minMatch[1]);
        else if (lower.includes('última hora') || lower.includes('ultima hora')) params.periodo_minutos = 60;
        else if (lower.includes('24 horas') || lower.includes('24h')) params.periodo_minutos = 1440;
        else params.periodo_minutos = 60;
    }

    return params;
}

async function sendMessage(text) {
    addMessage(text, 'user');
    chatInput.value = '';
    setInputEnabled(false);
    showLoading();

    try {
        // Direct health check — bypass agent for structured dashboard
        if (isMassHealthCheck(text)) {
            const hcParams = extractHealthcheckParams(text);
            console.log('[HEALTHCHECK] Detected request:', hcParams);
            const response = await fetch(API_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ healthcheck: true, ...hcParams }),
            });
            hideLoading();
            if (response.ok) {
                const data = await response.json();
                console.log('[HEALTHCHECK] Response keys:', Object.keys(data));
                if (data.dashboard) {
                    renderHealthDashboard(data.dashboard);
                } else if (data.erro) {
                    addMessage(data.erro, 'error');
                } else {
                    addMessage('Sem dados de health check.', 'bot');
                }
            } else {
                const err = await response.json().catch(() => ({}));
                addMessage(err.erro || 'Erro ao executar health check.', 'error');
            }
            setInputEnabled(true);
            chatInput.focus();
            return;
        }

        // Direct metrics fast path DISABLED — metrics now go through Bedrock agent
        // The agent calls /consultarMetricas and returns [METRICS_DATA:{...}] marker
        // which is detected and rendered by the normal chat flow below.
        // Keeping pendingMetricsQuery reset for safety:
        if (pendingMetricsQuery) pendingMetricsQuery = null;

        // Normal chat — via Bedrock agent
        const response = await fetch(API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pergunta: text, session_id: chatSessionId }),
        });

        hideLoading();

        if (response.ok) {
            const data = await response.json();
            const resposta = data.resposta || 'Resposta vazia do servidor.';

            // Check if response contains metrics with time series data
            const metricsData = data.metrics_chart_data || extractMetricsFromResponse(resposta);
            if (metricsData && metricsData.metricas) {
                addMessage(resposta, 'bot');
                renderMetricsCharts(metricsData);
                setInputEnabled(true);
                chatInput.focus();
                return;
            }

            // Check if agent response contains config download marker
            const configDlMatch = resposta.match(/\[DOWNLOAD_CONFIG:([^\]:]+):([^\]:]+):([^\]]+)\]/);

            // Check if agent response contains export download marker
            let exportDlMatch = resposta.match(/\[DOWNLOAD_EXPORT:([^\]:]+):([^\]]+)\]/);
            if (!exportDlMatch) {
                const filenameMatch = resposta.match(/export-\w+-\d{4}-\d{2}-\d{2}T[\d-]+Z-[a-f0-9]+\.(csv|json)/);
                if (filenameMatch) {
                    exportDlMatch = [null, filenameMatch[0], filenameMatch[1]];
                }
            }

            // Check for analytics chart marker
            const chartMatch = resposta.match(/\[CHART_DATA:(\{[\s\S]*?\})\]/);

            // Check for metrics data marker (from consultarMetricas via agent)
            // Use a function to extract balanced JSON instead of regex
            const metricsMatch = (() => {
                const tag = '[METRICS_DATA:';
                const idx = resposta.indexOf(tag);
                if (idx === -1) return null;
                const jsonStart = idx + tag.length;
                let depth = 0, i = jsonStart;
                while (i < resposta.length) {
                    if (resposta[i] === '{') depth++;
                    else if (resposta[i] === '}') { depth--; if (depth === 0) break; }
                    i++;
                }
                if (depth !== 0) return null;
                try {
                    const jsonStr = resposta.slice(jsonStart, i + 1);
                    return [resposta.slice(idx, i + 2), jsonStr];
                } catch { return null; }
            })();

            // Check for revenue data marker (from SpringServe report via agent)
            const revenueMatch = (() => {
                const tag = '[REVENUE_DATA:';
                const idx = resposta.indexOf(tag);
                if (idx === -1) return null;
                const jsonStart = idx + tag.length;
                let depth = 0, i = jsonStart;
                while (i < resposta.length) {
                    if (resposta[i] === '{') depth++;
                    else if (resposta[i] === '}') { depth--; if (depth === 0) break; }
                    i++;
                }
                if (depth !== 0) return null;
                try {
                    const jsonStr = resposta.slice(jsonStart, i + 1);
                    return [resposta.slice(idx, i + 2), jsonStr];
                } catch { return null; }
            })();

            // Check for analytics CSV download marker
            const csvMatch = resposta.match(/\[DOWNLOAD_ANALYTICS_CSV:([^:]+):([A-Za-z0-9+/=]+)\]/);

            // Clean markers from text before displaying
            let cleanResposta = resposta;
            if (configDlMatch) cleanResposta = cleanResposta.replace(/\[DOWNLOAD_CONFIG:[^\]]+\]/, '').trim();
            if (exportDlMatch) cleanResposta = cleanResposta.replace(/\[DOWNLOAD_EXPORT:[^\]]+\]/, '').trim();
            if (chartMatch) cleanResposta = cleanResposta.replace(/\[CHART_DATA:\{[\s\S]*?\}\]/, '').trim();
            if (metricsMatch) cleanResposta = cleanResposta.replace(metricsMatch[0], '').trim();
            if (revenueMatch) cleanResposta = cleanResposta.replace(revenueMatch[0], '').trim();
            if (csvMatch) cleanResposta = cleanResposta.replace(/\[DOWNLOAD_ANALYTICS_CSV:[^\]]+\]/, '').trim();

            // Display the clean text
            if (cleanResposta) addMessage(cleanResposta, 'bot');

            // Render analytics chart if present
            if (chartMatch) {
                try {
                    const chartData = JSON.parse(chartMatch[1]);
                    renderAnalyticsChart(chartData);
                } catch (e) {
                    console.warn('Failed to parse chart data:', e);
                }
            } else {
                // Try to extract ranking from text response (Bedrock rewrites the response)
                const textChart = extractRankingFromText(cleanResposta);
                if (textChart) renderAnalyticsChart(textChart);
            }

            // Render metrics charts if METRICS_DATA marker present (from agent via consultarMetricas)
            if (metricsMatch) {
                try {
                    const metricsData = JSON.parse(metricsMatch[1]);
                    renderMetricsCharts(metricsData);
                } catch (e) {
                    console.warn('Failed to parse metrics data:', e.message);
                }
            }

            // Render revenue dashboard if REVENUE_DATA marker present
            if (revenueMatch) {
                try {
                    const revenueData = JSON.parse(revenueMatch[1]);
                    renderRevenueDashboard(revenueData);
                } catch (e) {
                    console.warn('Failed to parse revenue data:', e.message);
                }
            }
            // Render CSV download button if present
            if (csvMatch) {
                const csvFilename = csvMatch[1];
                const csvB64 = csvMatch[2];
                const dlDiv = document.createElement('div');
                dlDiv.classList.add('message', 'message-bot');
                dlDiv.style.cssText = 'padding:8px 16px;';
                const btnId = 'dl-analytics-' + Date.now();
                dlDiv.innerHTML = `<button id="${btnId}" style="padding:8px 16px;background:var(--user-bubble);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:0.85rem;">📥 Baixar CSV completo</button>`;
                chatMessages.appendChild(dlDiv);
                chatMessages.scrollTop = chatMessages.scrollHeight;
                document.getElementById(btnId).addEventListener('click', () => {
                    try {
                        const csvContent = atob(csvB64);
                        const blob = new Blob(['\uFEFF' + csvContent], { type: 'text/csv;charset=utf-8;' });
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = csvFilename;
                        a.click();
                        URL.revokeObjectURL(url);
                    } catch (e) {
                        console.warn('CSV download failed:', e);
                    }
                });
            }

            // Handle config download
            if (configDlMatch) {
                const resourceId = configDlMatch[1];
                const servico = configDlMatch[2];
                const tipoRecurso = configDlMatch[3];

                try {
                    const cfgResp = await fetch(API_URL, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            download_config: true,
                            servico: servico,
                            tipo_recurso: tipoRecurso,
                            resource_id: resourceId,
                        }),
                    });
                    if (cfgResp.ok) {
                        const cfgData = await cfgResp.json();
                        const content = cfgData.dados_exportados;
                        if (content) {
                            const dlMsg = document.createElement('div');
                            dlMsg.classList.add('message', 'message-bot');
                            const btnId = 'dl-cfg-' + Date.now();
                            dlMsg.innerHTML = '<button id="' + btnId + '" style="padding:10px 20px;background:var(--user-bubble);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:0.9rem;">📥 Baixar JSON Completo</button>';
                            chatMessages.appendChild(dlMsg);
                            chatMessages.scrollTop = chatMessages.scrollHeight;
                            document.getElementById(btnId).onclick = () => {
                                const blob = new Blob([content], {type: 'application/json'});
                                const url = URL.createObjectURL(blob);
                                const a = document.createElement('a');
                                a.href = url;
                                a.download = 'config-' + resourceId + '.json';
                                a.click();
                                URL.revokeObjectURL(url);
                            };
                        }
                    }
                } catch (e) {
                    console.error('Config download error:', e);
                }
            }

            // Handle export download
            if (exportDlMatch) {
                const exportFilename = exportDlMatch[1];
                const exportExt = exportDlMatch[2];

                try {
                    const exportResp = await fetch(API_URL, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            download_export: true,
                            filename: exportFilename,
                        }),
                    });
                    if (exportResp.ok) {
                        const exportData = await exportResp.json();
                        const exportContent = exportData.dados_exportados;
                        if (exportContent) {
                            // Detect fill rate CSV → render fill rate dashboard
                            const isFillRate = isFillRateCsv(exportContent);
                            // Detect revenue CSV → render revenue dashboard
                            const isRevenue = !isFillRate && isRevenueCsv(exportContent);
                            if (isFillRate) {
                                renderFillRateDashboard(exportContent, exportFilename);
                            } else if (isRevenue) {
                                renderRevenueDashboardFromCsv(exportContent, exportFilename);
                            } else {
                                const dlMsg = document.createElement('div');
                                dlMsg.classList.add('message', 'message-bot');
                                const btnId = 'dl-exp-' + Date.now();
                                dlMsg.innerHTML = '<button id="' + btnId + '" style="padding:10px 20px;background:var(--user-bubble);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:0.9rem;">📥 Baixar ' + exportExt.toUpperCase() + '</button>';
                                chatMessages.appendChild(dlMsg);
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                document.getElementById(btnId).onclick = () => {
                                    const blob = new Blob([exportContent], {type: exportExt === 'json' ? 'application/json' : 'text/csv'});
                                    const url = URL.createObjectURL(blob);
                                    const a = document.createElement('a');
                                    a.href = url;
                                    a.download = exportFilename;
                                    a.click();
                                    URL.revokeObjectURL(url);
                                };
                            }
                        }
                    }
                } catch (e) {
                    console.error('Export download error:', e);
                }
            }
        } else if (response.status === 400) {
            addMessage('Pergunta inválida. Verifique o conteúdo e tente novamente.', 'error');
        } else if (response.status === 504) {
            addMessage('Servidor demorou para responder. Tente novamente em instantes.', 'error');
        } else if (response.status === 500) {
            addMessage('Erro interno do servidor. Tente novamente mais tarde.', 'error');
        } else {
            addMessage('Erro inesperado (HTTP ' + response.status + '). Tente novamente.', 'error');
        }
    } catch (err) {
        hideLoading();
        addMessage('Sem conexão com o servidor. Verifique sua rede e tente novamente.', 'error');
    } finally {
        setInputEnabled(true);
        chatInput.focus();
    }
}

chatForm.addEventListener('submit', function (e) {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (text) {
        sendMessage(text);
    }
});

// Logout
document.getElementById('logout-btn').addEventListener('click', () => {
    sessionStorage.clear();
    window.location.href = 'index.html';
});

// --- Analytics Bar Chart Rendering ---

function extractRankingFromText(text) {
    if (!text) return null;
    const lines = text.split('\n');
    const labels = [], values = [], formatted = [];
    let unit = '';

    for (const line of lines) {
        let label = null, rawNum = null, unitStr = '';

        // Format 1: "1. **Gran Hermano 24h** (live_1289): **1.343.864 impressões**"
        // Format 2: "1. **live_1289** - 1.323.518 impressões"
        // Format 3: "1. **name** - 0.64 GB"
        // Format 4: "1. **name** - **0.64 GB**"

        // Try format with parentheses: "1. **Name** (id): **value unit**"
        let m = line.match(/^\s*\d+\.\s+\*{0,2}([^*()\n]+?)\*{0,2}\s*\([^)]+\)\s*[:\-–]\s*\*{0,2}([\d.,]+)\s*([^\s*\n]*)\*{0,2}/i);
        if (m) {
            label = m[1].trim();
            rawNum = m[2];
            unitStr = (m[3] || '').trim().toLowerCase();
        }

        // Try standard format: "1. **name** - value unit"
        if (!label) {
            m = line.match(/^\s*\d+\.\s+\*{0,2}([^*\n]+?)\*{0,2}\s*[-–]\s*\*{0,2}([\d.,]+)\s*([^\s*\n]*)\*{0,2}\s*$/i);
            if (m) {
                label = m[1].trim();
                rawNum = m[2];
                unitStr = (m[3] || '').trim().toLowerCase();
            }
        }

        if (!label || !rawNum) continue;

        // Parse number: handle thousand separator (e.g. "1.343.864") vs decimal (e.g. "0.64")
        let val;
        if (/^\d{1,3}(\.\d{3})+$/.test(rawNum)) {
            // Thousand separator pattern: 1.343.864 → 1343864
            val = parseFloat(rawNum.replace(/\./g, ''));
        } else {
            val = parseFloat(rawNum.replace(',', '.'));
        }
        if (isNaN(val)) continue;

        labels.push(label);
        values.push(val);
        if (unitStr && !unit) unit = unitStr;
        formatted.push(rawNum + (unitStr ? ' ' + unitStr : ''));
    }

    if (labels.length < 2) return null;

    let unitKey = '';
    if (['gb', 'mb', 'kb', 'bytes'].includes(unit)) unitKey = 'bytes';
    else if (unit === '%') unitKey = '%';
    else if (unit === 'ms' || unit === 's') unitKey = 'ms';

    const titleLine = lines.find(l =>
        /ranking|canal|top\s*\d|maior|menor|egress|fill|bitrate|impressões|impressao|anúncio|fill.rate/i.test(l)
        && !/^\s*\d+\./.test(l)
    );

    return {
        labels,
        values,
        formatted,
        label: titleLine ? titleLine.replace(/[*#_]/g, ' ').replace(/\s+/g, ' ').trim().substring(0, 70) : 'Ranking',
        unit: unitKey,
        title: '',
    };
}

function renderAnalyticsChart(chartData) {
    if (!chartData || !chartData.labels || !chartData.values) return;

    const container = document.createElement('div');
    container.classList.add('message', 'message-bot');
    container.style.cssText = 'padding:12px 16px;';

    const title = document.createElement('div');
    title.style.cssText = 'font-size:0.85rem;color:var(--text-secondary);margin-bottom:8px;font-weight:600;';
    title.textContent = chartData.label || 'Ranking';
    container.appendChild(title);

    const wrapper = document.createElement('div');
    // Taller for vertical bars with many labels
    const h = Math.max(220, chartData.labels.length * 28);
    wrapper.style.cssText = `background:var(--bg-tertiary);border-radius:8px;padding:12px;border:1px solid var(--border);height:${h}px;`;

    const canvas = document.createElement('canvas');
    canvas.id = 'analytics-chart-' + Date.now();
    wrapper.appendChild(canvas);
    container.appendChild(wrapper);

    chatMessages.appendChild(container);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    setTimeout(() => {
        if (typeof Chart === 'undefined') return;
        const unit = chartData.unit || '';
        const formatted = chartData.formatted || [];

        // Shorten labels: keep last segment after underscore if too long
        const shortLabels = chartData.labels.map(l => {
            if (l.length <= 18) return l;
            const parts = l.split('_');
            return parts.length > 2 ? parts.slice(-2).join('_') : l.substring(0, 18);
        });

        const baseColor = 'hsla(210, 70%, 55%, 0.85)';
        const colors = chartData.values.map((_, i) => `hsla(${(200 + i * 18) % 360}, 65%, 55%, 0.85)`);

        const formatVal = (v) => {
            if (unit === 'bytes') return v.toFixed(2) + ' GB';
            if (unit === '%') return v.toFixed(1) + '%';
            if (unit === 'ms') return v.toFixed(0) + ' ms';
            return v.toLocaleString();
        };

        new Chart(canvas, {
            type: 'bar',
            data: {
                labels: shortLabels,
                datasets: [{
                    label: chartData.label || 'Valor',
                    data: chartData.values,
                    backgroundColor: colors,
                    borderColor: colors.map(c => c.replace('0.85', '1')),
                    borderWidth: 1,
                    borderRadius: 3,
                }]
            },
            options: {
                indexAxis: 'y',  // horizontal bars — easier to read channel names
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: (items) => chartData.labels[items[0].dataIndex] || '',
                            label: (ctx) => {
                                const raw = ctx.parsed.x;
                                // Use pre-formatted value if available
                                const pre = formatted[ctx.dataIndex];
                                return ' ' + (pre || formatVal(raw));
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: 'var(--text-secondary)',
                            font: { size: 11 },
                            callback: v => formatVal(v),
                        },
                        grid: { color: 'rgba(128,128,128,0.15)' }
                    },
                    y: {
                        ticks: {
                            color: 'var(--text-secondary)',
                            font: { size: 11 },
                            maxRotation: 0,
                        },
                        grid: { display: false }
                    }
                }
            }
        });
    }, 50);
}

// --- Health Dashboard Rendering ---

function renderHealthDashboard(dashboard) {
    // dashboard is the structured JSON from _execute_healthcheck()
    const score = dashboard.score_saude ?? 100;
    const totais = dashboard.totais || {};
    const verde = totais.verde || 0;
    const amarelo = totais.amarelo || 0;
    const vermelho = totais.vermelho || 0;
    const total = dashboard.total_recursos || (verde + amarelo + vermelho);
    const redResources = dashboard.recursos_vermelho || [];
    const yellowResources = dashboard.recursos_amarelo || [];
    const erros = dashboard.erros || [];
    const parcial = dashboard.parcial || false;
    const servicos = (dashboard.servicos_consultados || []).join(', ');

    const container = document.createElement('div');
    container.classList.add('message', 'message-bot');
    container.style.cssText = 'padding:0;border:none;background:transparent;max-width:90%;';

    const scoreColor = score >= 90 ? '#3fb950' : score >= 70 ? '#d29922' : '#f85149';
    const scoreEmoji = score >= 90 ? '✅' : score >= 70 ? '⚠️' : '🚨';

    let html = `
    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;overflow:hidden;">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#161b22,#1c2333);padding:20px 24px;border-bottom:1px solid var(--border);">
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
                <div>
                    <div style="font-size:1.1rem;font-weight:700;color:var(--text-primary);">🏥 Dashboard de Saúde</div>
                    <div style="font-size:0.75rem;color:var(--text-secondary);margin-top:4px;">${total} recursos · ${servicos || 'Todos os serviços'}${parcial ? ' · ⚠️ Resultado parcial' : ''}</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:2.2rem;font-weight:800;color:${scoreColor};line-height:1;">${scoreEmoji} ${score}%</div>
                    <div style="font-size:0.7rem;color:var(--text-secondary);margin-top:2px;">Score Geral</div>
                </div>
            </div>
        </div>

        <!-- Semaphore Cards -->
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border);">
            <div style="background:var(--bg-secondary);padding:16px;text-align:center;">
                <div style="font-size:1.8rem;">🟢</div>
                <div style="font-size:1.5rem;font-weight:700;color:#3fb950;">${verde}</div>
                <div style="font-size:0.7rem;color:var(--text-secondary);">Saudáveis</div>
            </div>
            <div style="background:var(--bg-secondary);padding:16px;text-align:center;">
                <div style="font-size:1.8rem;">🟡</div>
                <div style="font-size:1.5rem;font-weight:700;color:#d29922;">${amarelo}</div>
                <div style="font-size:0.7rem;color:var(--text-secondary);">Atenção</div>
            </div>
            <div style="background:var(--bg-secondary);padding:16px;text-align:center;">
                <div style="font-size:1.8rem;">🔴</div>
                <div style="font-size:1.5rem;font-weight:700;color:#f85149;">${vermelho}</div>
                <div style="font-size:0.7rem;color:var(--text-secondary);">Críticos</div>
            </div>
        </div>

        <!-- Progress Bar -->
        <div style="padding:12px 24px;background:var(--bg-secondary);">
            <div style="height:10px;border-radius:5px;background:var(--bg-tertiary);overflow:hidden;display:flex;">
                <div style="width:${total ? (verde/total*100) : 0}%;background:#3fb950;transition:width 0.5s;"></div>
                <div style="width:${total ? (amarelo/total*100) : 0}%;background:#d29922;transition:width 0.5s;"></div>
                <div style="width:${total ? (vermelho/total*100) : 0}%;background:#f85149;transition:width 0.5s;"></div>
            </div>
        </div>`;

    // Doughnut chart
    const chartId = 'health-chart-' + Date.now();
    html += `
        <div style="padding:16px 24px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:center;">
            <canvas id="${chartId}" style="max-width:200px;max-height:200px;"></canvas>
        </div>`;

    // Red resources table
    if (redResources.length > 0) {
        html += `
        <div style="border-top:1px solid var(--border);padding:16px 24px;">
            <div style="font-size:0.85rem;font-weight:600;color:#f85149;margin-bottom:10px;">🔴 Recursos Críticos (${redResources.length})</div>
            <div style="max-height:300px;overflow-y:auto;">
                <table style="width:100%;border-collapse:collapse;font-size:0.8rem;">
                    <thead>
                        <tr style="border-bottom:1px solid var(--border);">
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Recurso</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Serviço</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Severidade</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Alertas</th>
                        </tr>
                    </thead>
                    <tbody>`;
        redResources.forEach(r => {
            const sevColor = r.severidade === 'CRITICAL' ? '#f85149' : '#d29922';
            const alertSummary = (r.alertas || []).map(a => a.metrica + ': ' + a.valor).join(', ') || '-';
            html += `
                        <tr style="border-bottom:1px solid var(--border);">
                            <td style="padding:8px;color:var(--text-primary);font-family:monospace;font-size:0.75rem;">${escapeHtml(r.nome)}</td>
                            <td style="padding:8px;color:var(--text-secondary);">${escapeHtml(r.servico)}</td>
                            <td style="padding:8px;color:${sevColor};font-weight:600;">${escapeHtml(r.severidade)}</td>
                            <td style="padding:8px;color:var(--text-secondary);font-size:0.75rem;">${escapeHtml(alertSummary)}</td>
                        </tr>`;
        });
        html += `
                    </tbody>
                </table>
            </div>
        </div>`;
    }

    // Yellow resources (collapsed)
    if (yellowResources.length > 0) {
        html += `
        <div style="border-top:1px solid var(--border);padding:16px 24px;">
            <div style="font-size:0.85rem;font-weight:600;color:#d29922;margin-bottom:10px;">🟡 Recursos com Atenção (${yellowResources.length})</div>
            <div style="max-height:200px;overflow-y:auto;">
                <table style="width:100%;border-collapse:collapse;font-size:0.8rem;">
                    <thead>
                        <tr style="border-bottom:1px solid var(--border);">
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Recurso</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Serviço</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Alertas</th>
                        </tr>
                    </thead>
                    <tbody>`;
        yellowResources.forEach(r => {
            const alertSummary = (r.alertas || []).map(a => a.metrica + ': ' + a.valor).join(', ') || '-';
            html += `
                        <tr style="border-bottom:1px solid var(--border);">
                            <td style="padding:8px;color:var(--text-primary);font-family:monospace;font-size:0.75rem;">${escapeHtml(r.nome)}</td>
                            <td style="padding:8px;color:var(--text-secondary);">${escapeHtml(r.servico)}</td>
                            <td style="padding:8px;color:var(--text-secondary);font-size:0.75rem;">${escapeHtml(alertSummary)}</td>
                        </tr>`;
        });
        html += `
                    </tbody>
                </table>
            </div>
        </div>`;
    }

    // Errors
    if (erros.length > 0) {
        html += `
        <div style="border-top:1px solid var(--border);padding:12px 24px;background:rgba(248,81,73,0.05);">
            <div style="font-size:0.75rem;color:#f85149;">⚠️ Erros durante consulta:</div>`;
        erros.forEach(e => {
            html += `<div style="font-size:0.7rem;color:var(--text-secondary);margin-top:4px;">• ${escapeHtml(e.servico || '')}: ${escapeHtml(e.mensagem || '')}</div>`;
        });
        html += `</div>`;
    }

    // CSV Download button
    const csvBtnId = 'dl-health-' + Date.now();
    const allProblems = [...redResources, ...yellowResources];
    html += `
        <div style="padding:12px 24px 16px;border-top:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;">
            <button id="${csvBtnId}" style="padding:8px 16px;background:var(--user-bubble);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:0.8rem;">📥 Baixar CSV (${allProblems.length} problemas)</button>
        </div>
    </div>`;

    container.innerHTML = html;
    chatMessages.appendChild(container);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Render doughnut chart
    setTimeout(() => {
        const canvas = document.getElementById(chartId);
        if (canvas && typeof Chart !== 'undefined') {
            new Chart(canvas, {
                type: 'doughnut',
                data: {
                    labels: ['Saudáveis', 'Atenção', 'Críticos'],
                    datasets: [{
                        data: [verde, amarelo, vermelho],
                        backgroundColor: ['#3fb950', '#d29922', '#f85149'],
                        borderColor: '#161b22',
                        borderWidth: 2,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    cutout: '60%',
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { color: '#8b949e', font: { size: 11 }, padding: 12 },
                        },
                    },
                },
            });
        }
    }, 50);

    // Wire up CSV download
    setTimeout(() => {
        const btn = document.getElementById(csvBtnId);
        if (btn) {
            btn.onclick = () => {
                let csv = 'Recurso,Servico,Cor,Severidade,Alertas\n';
                allProblems.forEach(r => {
                    const cor = r.severidade === 'CRITICAL' || r.severidade === 'ERROR' ? 'vermelho' : 'amarelo';
                    const alertas = (r.alertas || []).map(a => a.metrica + '=' + a.valor + ' (' + a.severidade + ')').join('; ');
                    csv += '"' + (r.nome || '').replace(/"/g, '""') + '","'
                         + (r.servico || '').replace(/"/g, '""') + '","'
                         + cor + '","'
                         + (r.severidade || '').replace(/"/g, '""') + '","'
                         + alertas.replace(/"/g, '""') + '"\n';
                });
                const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'health-check-problemas.csv';
                a.click();
                URL.revokeObjectURL(url);
            };
        }
    }, 100);
}

// --- Metrics Chart Rendering ---

function extractMetricsFromResponse(text) {
    // Try to detect if the response contains structured metrics data
    // Look for patterns like "serie_temporal" in JSON blocks
    try {
        const jsonMatch = text.match(/```json\s*([\s\S]*?)```/);
        if (jsonMatch) {
            const parsed = JSON.parse(jsonMatch[1]);
            if (parsed.metricas || parsed.resumo?.metricas) {
                return parsed.resumo || parsed;
            }
        }
    } catch (e) { /* not JSON */ }
    return null;
}

function renderMetricsCharts(metricsData) {
    const metricas = metricsData.metricas;
    if (!metricas) return;

    const chartsToRender = Object.entries(metricas).filter(
        ([, data]) => data.serie_temporal && data.serie_temporal.length > 0
    );
    if (chartsToRender.length === 0) return;

    // Detect fill rate metrics and their severity
    const fillRateEntry = chartsToRender.find(([name]) => name === 'Avail.FillRate');
    const adsEntry      = chartsToRender.find(([name]) => name === 'AdDecisionServer.Ads');
    const isFillRateCritical = fillRateEntry && (fillRateEntry[1].media ?? fillRateEntry[1].atual ?? 0) < 50;
    const isFillRateWarning  = fillRateEntry && !isFillRateCritical && (fillRateEntry[1].media ?? fillRateEntry[1].atual ?? 100) < 80;

    const container = document.createElement('div');
    container.classList.add('message', 'message-bot');
    container.style.cssText = 'padding:0;border:none;background:transparent;max-width:95%;';

    // ── Header card ──────────────────────────────────────────────────────────
    const severidade = metricsData.severidade_geral || 'INFO';
    const headerColor = severidade === 'ERROR' || severidade === 'CRITICAL' ? '#f85149'
                      : severidade === 'WARNING' ? '#d29922' : '#3fb950';
    const headerEmoji = severidade === 'ERROR' || severidade === 'CRITICAL' ? '🚨'
                      : severidade === 'WARNING' ? '⚠️' : '✅';
    const alertas = metricsData.alertas || [];

    let headerHtml = `
    <div style="background:var(--bg-secondary);border:1px solid ${headerColor}44;border-radius:12px;overflow:hidden;margin-bottom:4px;">
        <div style="background:linear-gradient(135deg,#161b22,#1c2333);padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
            <div style="font-size:1rem;font-weight:700;color:var(--text-primary);">📊 Métricas em Tempo Real</div>
            <div style="font-size:1rem;font-weight:800;color:${headerColor};">${headerEmoji} ${severidade}</div>
        </div>`;

    // Alert pills
    if (alertas.length > 0) {
        headerHtml += `<div style="padding:10px 18px;display:flex;flex-wrap:wrap;gap:8px;border-bottom:1px solid var(--border);">`;
        alertas.forEach(a => {
            const c = a.severidade === 'ERROR' || a.severidade === 'CRITICAL' ? '#f85149' : '#d29922';
            headerHtml += `<span style="background:${c}22;border:1px solid ${c}55;border-radius:20px;padding:4px 12px;font-size:0.75rem;color:${c};font-weight:600;">${escapeHtml(a.metrica)}: ${a.valor}${a.metrica.includes('FillRate') ? '%' : ''}</span>`;
        });
        headerHtml += `</div>`;
    }

    // Fill rate critical banner
    if (isFillRateCritical) {
        const frVal = (fillRateEntry[1].media ?? fillRateEntry[1].atual ?? 0).toFixed(1);
        const adsVal = adsEntry ? Math.round(adsEntry[1].media ?? 0) : null;
        headerHtml += `
        <div style="padding:14px 18px;background:#f8514910;border-bottom:1px solid #f8514933;">
            <div style="font-size:0.85rem;font-weight:700;color:#f85149;margin-bottom:6px;">🚨 Fill Rate Crítico — ${frVal}%</div>
            <div style="font-size:0.78rem;color:var(--text-secondary);line-height:1.6;">
                O MediaTailor está recebendo decisões do ad server${adsVal !== null ? ` (média ${adsVal} ads/período)` : ''}, mas os avails <strong style="color:#f85149">não estão sendo preenchidos</strong>.<br>
                Verifique: URL do ad server · configuração de avails · SSAI manifest · permissões de rede.
            </div>
        </div>`;
    } else if (isFillRateWarning) {
        const frVal = (fillRateEntry[1].media ?? fillRateEntry[1].atual ?? 0).toFixed(1);
        headerHtml += `
        <div style="padding:12px 18px;background:#d2992210;border-bottom:1px solid #d2992233;">
            <div style="font-size:0.82rem;color:#d29922;">⚠️ Fill Rate abaixo de 80% — média ${frVal}%</div>
        </div>`;
    }

    headerHtml += `</div>`;
    container.innerHTML = headerHtml;
    chatMessages.appendChild(container);

    // ── Combined Fill Rate + Ads chart (dual axis) ───────────────────────────
    if (fillRateEntry && adsEntry) {
        const [, frData] = fillRateEntry;
        const [, adsData] = adsEntry;

        // Align series by timestamp
        const frSeries  = frData.serie_temporal;
        const adsSeries = adsData.serie_temporal;
        const labels = frSeries.map(p => {
            const d = new Date(p.timestamp);
            return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        });
        const frValues  = frSeries.map(p => p.value);
        const adsValues = adsSeries.map(p => p.value);

        const wrapper = document.createElement('div');
        wrapper.classList.add('message', 'message-bot');
        wrapper.style.cssText = 'padding:0;border:none;background:transparent;max-width:95%;';

        const chartId = 'chart-dual-' + Date.now();
        wrapper.innerHTML = `
        <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;overflow:hidden;">
            <div style="padding:12px 18px;border-bottom:1px solid var(--border);font-size:0.8rem;font-weight:600;color:var(--text-secondary);">
                FILL RATE vs ADS RECEBIDOS — últimas 24h
            </div>
            <div style="padding:12px;background:var(--bg-tertiary);height:260px;">
                <canvas id="${chartId}"></canvas>
            </div>
            <div style="padding:8px 18px;display:flex;gap:16px;font-size:0.72rem;color:var(--text-secondary);">
                <span><span style="display:inline-block;width:12px;height:3px;background:#f85149;border-radius:2px;vertical-align:middle;margin-right:4px;"></span>Fill Rate % (eixo esq.)</span>
                <span><span style="display:inline-block;width:12px;height:3px;background:#4f8cff;border-radius:2px;vertical-align:middle;margin-right:4px;"></span>Ads recebidos (eixo dir.)</span>
                <span><span style="display:inline-block;width:12px;height:2px;background:#d29922;border-radius:2px;vertical-align:middle;margin-right:4px;border-top:2px dashed #d29922;"></span>Limite 80%</span>
            </div>
        </div>`;
        chatMessages.appendChild(wrapper);

        setTimeout(() => {
            const canvas = document.getElementById(chartId);
            if (!canvas || typeof Chart === 'undefined') return;
            new Chart(canvas, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Fill Rate (%)',
                            data: frValues,
                            borderColor: '#f85149',
                            backgroundColor: '#f8514920',
                            fill: true,
                            tension: 0.3,
                            pointRadius: 2,
                            borderWidth: 2,
                            yAxisID: 'yFill',
                        },
                        {
                            label: 'Ads recebidos',
                            data: adsValues,
                            borderColor: '#4f8cff',
                            backgroundColor: 'transparent',
                            fill: false,
                            tension: 0.3,
                            pointRadius: 2,
                            borderWidth: 1.5,
                            borderDash: [],
                            yAxisID: 'yAds',
                        },
                        {
                            // Reference line at 80%
                            label: 'Limite 80%',
                            data: labels.map(() => 80),
                            borderColor: '#d29922',
                            borderDash: [6, 4],
                            borderWidth: 1.5,
                            pointRadius: 0,
                            fill: false,
                            yAxisID: 'yFill',
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: ctx => {
                                    if (ctx.dataset.label === 'Limite 80%') return null;
                                    const v = ctx.parsed.y;
                                    if (ctx.dataset.label === 'Fill Rate (%)') return ` Fill Rate: ${v.toFixed(1)}%`;
                                    return ` Ads: ${Math.round(v)}`;
                                },
                            },
                        },
                    },
                    scales: {
                        x: {
                            ticks: { color: '#999', font: { size: 10 }, maxTicksLimit: 12 },
                            grid: { color: '#2a2a2a' },
                        },
                        yFill: {
                            type: 'linear',
                            position: 'left',
                            min: 0,
                            max: 100,
                            ticks: { color: '#f85149', font: { size: 10 }, callback: v => v + '%' },
                            grid: { color: '#2a2a2a' },
                            title: { display: true, text: 'Fill Rate %', color: '#f85149', font: { size: 10 } },
                        },
                        yAds: {
                            type: 'linear',
                            position: 'right',
                            ticks: { color: '#4f8cff', font: { size: 10 } },
                            grid: { display: false },
                            title: { display: true, text: 'Ads', color: '#4f8cff', font: { size: 10 } },
                        },
                    },
                },
            });
        }, 50);

        chatMessages.scrollTop = chatMessages.scrollHeight;
        // Skip individual charts for these two — already combined
        const remaining = chartsToRender.filter(([n]) => n !== 'Avail.FillRate' && n !== 'AdDecisionServer.Ads');
        if (remaining.length === 0) return;
        renderIndividualMetricCharts(remaining);
        return;
    }

    // ── Individual charts for all other metrics ───────────────────────────────
    renderIndividualMetricCharts(chartsToRender);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function renderRevenueDashboard(data) {
    if (!data || !data.labels || !data.values || data.labels.length === 0) return;

    const total = data.total ?? data.values.reduce((a, b) => a + b, 0);
    const rpmMedio = data.rpm_medio ?? 0;
    const cpmMedio = data.cpm_medio ?? 0;
    const impressionsTotal = data.impressions_total ?? 0;

    const fmtUSD = v => '$' + (v ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const fmtNum = v => (v ?? 0).toLocaleString('pt-BR');

    // Shorten label: last segment after space or underscore
    const shortLabel = l => {
        if (!l || l.length <= 22) return l;
        const parts = l.split(/[\s_]+/);
        return parts.length > 2 ? parts.slice(-2).join(' ') : l.substring(0, 22);
    };

    const container = document.createElement('div');
    container.classList.add('message', 'message-bot');
    container.style.cssText = 'padding:0;border:none;background:transparent;max-width:95%;';

    // Color each bar by revenue rank
    const maxVal = Math.max(...data.values);
    const barColors = data.values.map(v => {
        const ratio = maxVal > 0 ? v / maxVal : 0;
        if (ratio >= 0.7) return '#3fb950';
        if (ratio >= 0.3) return '#4f8cff';
        return '#748ffc';
    });

    const chartId = 'revenue-chart-' + Date.now();
    const h = Math.max(260, data.labels.length * 30);

    container.innerHTML = `
    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;overflow:hidden;">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#0d1f0d,#1a2e1a);padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
            <div>
                <div style="font-size:1rem;font-weight:700;color:#3fb950;">💰 Revenue SpringServe</div>
                <div style="font-size:0.72rem;color:var(--text-secondary);margin-top:3px;">${data.labels.length} supply tags</div>
            </div>
            <div style="font-size:1.8rem;font-weight:800;color:#3fb950;">${fmtUSD(total)}</div>
        </div>

        <!-- KPI cards -->
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border);">
            <div style="background:var(--bg-secondary);padding:14px;text-align:center;">
                <div style="font-size:1.1rem;font-weight:700;color:#3fb950;">${fmtUSD(total)}</div>
                <div style="font-size:0.68rem;color:var(--text-secondary);margin-top:3px;">Revenue Total</div>
            </div>
            <div style="background:var(--bg-secondary);padding:14px;text-align:center;">
                <div style="font-size:1.1rem;font-weight:700;color:#4f8cff;">${fmtUSD(rpmMedio)}</div>
                <div style="font-size:0.68rem;color:var(--text-secondary);margin-top:3px;">RPM Médio</div>
            </div>
            <div style="background:var(--bg-secondary);padding:14px;text-align:center;">
                <div style="font-size:1.1rem;font-weight:700;color:#ffd43b;">${fmtNum(impressionsTotal)}</div>
                <div style="font-size:0.68rem;color:var(--text-secondary);margin-top:3px;">Impressões</div>
            </div>
        </div>

        <!-- Bar chart -->
        <div style="padding:16px 20px;border-top:1px solid var(--border);">
            <div style="font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:10px;">REVENUE POR SUPPLY TAG (USD)</div>
            <div style="background:var(--bg-tertiary);border-radius:8px;padding:12px;border:1px solid var(--border);height:${h}px;">
                <canvas id="${chartId}"></canvas>
            </div>
        </div>
    </div>`;

    chatMessages.appendChild(container);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    setTimeout(() => {
        const canvas = document.getElementById(chartId);
        if (!canvas || typeof Chart === 'undefined') return;
        new Chart(canvas, {
            type: 'bar',
            data: {
                labels: data.labels.map(shortLabel),
                datasets: [{
                    label: 'Revenue (USD)',
                    data: data.values,
                    backgroundColor: barColors,
                    borderColor: barColors.map(c => c),
                    borderWidth: 1,
                    borderRadius: 4,
                }],
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: items => data.labels[items[0].dataIndex] || '',
                            label: ctx => ' ' + fmtUSD(ctx.parsed.x),
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: { color: '#999', font: { size: 10 }, callback: v => '$' + v.toFixed(2) },
                        grid: { color: '#2a2a2a' },
                    },
                    y: {
                        ticks: { color: '#ccc', font: { size: 10 }, maxRotation: 0 },
                        grid: { display: false },
                    },
                },
            },
        });
    }, 50);
}

function renderIndividualMetricCharts(chartsToRender) {
    const colors = ['#4f8cff', '#ff6b6b', '#51cf66', '#ffd43b', '#cc5de8', '#20c997', '#ff922b', '#748ffc'];

    chartsToRender.forEach(([metricName, data], idx) => {
        const wrapper = document.createElement('div');
        wrapper.classList.add('message', 'message-bot');
        wrapper.style.cssText = 'padding:0;border:none;background:transparent;max-width:95%;';

        const chartId = 'chart-' + Date.now() + '-' + idx;
        const color = colors[idx % colors.length];
        const isFill = metricName.includes('FillRate');
        const unit = isFill ? '%' : (data.unidade || '');

        wrapper.innerHTML = `
        <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;overflow:hidden;">
            <div style="padding:10px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
                <span style="font-size:0.8rem;font-weight:600;color:var(--text-secondary);">${escapeHtml(metricName)}</span>
                <span style="font-size:0.75rem;color:var(--text-secondary);">
                    avg: <strong style="color:var(--text-primary);">${(data.media ?? 0).toFixed(1)}${unit}</strong>
                    · max: <strong style="color:var(--text-primary);">${(data.max ?? 0).toFixed(1)}${unit}</strong>
                </span>
            </div>
            <div style="padding:12px;background:var(--bg-tertiary);height:180px;">
                <canvas id="${chartId}"></canvas>
            </div>
        </div>`;
        chatMessages.appendChild(wrapper);

        const series = data.serie_temporal;
        const labels = series.map(p => {
            const d = new Date(p.timestamp);
            return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        });
        const values = series.map(p => p.value);

        setTimeout(() => {
            const canvas = document.getElementById(chartId);
            if (!canvas || typeof Chart === 'undefined') return;
            const datasets = [{
                label: metricName,
                data: values,
                borderColor: color,
                backgroundColor: color + '20',
                fill: true,
                tension: 0.3,
                pointRadius: 2,
                borderWidth: 2,
            }];
            if (isFill) {
                datasets.push({
                    label: 'Limite 80%',
                    data: labels.map(() => 80),
                    borderColor: '#d29922',
                    borderDash: [6, 4],
                    borderWidth: 1.5,
                    pointRadius: 0,
                    fill: false,
                });
            }
            new Chart(canvas, {
                type: 'line',
                data: { labels, datasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: { label: ctx => ` ${ctx.parsed.y.toFixed(1)}${unit}` },
                        },
                    },
                    scales: {
                        x: { ticks: { color: '#999', font: { size: 10 }, maxTicksLimit: 12 }, grid: { color: '#2a2a2a' } },
                        y: {
                            min: isFill ? 0 : undefined,
                            max: isFill ? 100 : undefined,
                            ticks: { color: '#999', font: { size: 10 }, callback: v => v + (unit ? unit : '') },
                            grid: { color: '#2a2a2a' },
                        },
                    },
                },
            });
        }, 50 + idx * 20);
    });
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// --- Fill Rate Dashboard ---

function fixEncoding(str) {
    // Fix UTF-8 mojibake (Latin-1 misread): decode via TextDecoder trick
    try {
        const bytes = new Uint8Array(str.split('').map(c => c.charCodeAt(0)));
        return new TextDecoder('utf-8').decode(bytes);
    } catch (e) {
        return str;
    }
}

function parseCsv(text) {
    // Fix encoding first
    const fixed = fixEncoding(text);
    const lines = fixed.split('\n').filter(l => l.trim());
    if (lines.length < 2) return [];

    // Detect separator (tab or comma)
    const sep = lines[0].includes('\t') ? '\t' : ',';
    const headers = lines[0].split(sep).map(h => h.trim().replace(/^"|"$/g, ''));

    return lines.slice(1).map(line => {
        const vals = line.split(sep).map(v => v.trim().replace(/^"|"$/g, ''));
        const obj = {};
        headers.forEach((h, i) => { obj[h] = vals[i] || ''; });
        return obj;
    }).filter(r => Object.values(r).some(v => v));
}

function isFillRateCsv(content) {
    const lower = content.toLowerCase();
    return (lower.includes('fill_rate') || lower.includes('fill rate')) &&
           (lower.includes('canal') || lower.includes('channel')) &&
           lower.includes('timestamp');
}

function isRevenueCsv(content) {
    const lower = content.toLowerCase();
    return (lower.includes('revenue') || lower.includes('total_revenue')) &&
           (lower.includes('supply_tag') || lower.includes('supply tag') || lower.includes('impressions') || lower.includes('rpm'));
}

function renderRevenueDashboardFromCsv(csvContent, filename) {
    const rows = parseCsv(csvContent);
    if (!rows.length) return;

    // Aggregate by supply_tag_name (sum revenue, impressions; avg rpm, cpm)
    const byTag = {};
    rows.forEach(r => {
        const name = r.supply_tag_name || r.nome || r.channel_id || '';
        if (!name) return;
        const rev = parseFloat(r.revenue ?? r.total_revenue ?? 0) || 0;
        const imps = parseFloat(r.impressions ?? r.total_impressions ?? 0) || 0;
        const rpm = parseFloat(r.rpm ?? 0) || 0;
        const cpm = parseFloat(r.cpm ?? 0) || 0;
        if (!byTag[name]) byTag[name] = { revenue: 0, impressions: 0, rpm_sum: 0, cpm_sum: 0, count: 0 };
        byTag[name].revenue += rev;
        byTag[name].impressions += imps;
        byTag[name].rpm_sum += rpm;
        byTag[name].cpm_sum += cpm;
        byTag[name].count++;
    });

    // Sort by revenue desc, take top 20
    const sorted = Object.entries(byTag)
        .map(([name, d]) => ({
            name,
            revenue: d.revenue,
            impressions: d.impressions,
            rpm: d.count > 0 ? d.rpm_sum / d.count : 0,
            cpm: d.count > 0 ? d.cpm_sum / d.count : 0,
        }))
        .filter(d => d.revenue > 0)
        .sort((a, b) => b.revenue - a.revenue);

    if (!sorted.length) {
        // No revenue data — just show download button
        const dlMsg = document.createElement('div');
        dlMsg.classList.add('message', 'message-bot');
        const btnId = 'dl-rev-' + Date.now();
        dlMsg.innerHTML = `<button id="${btnId}" style="padding:10px 20px;background:var(--user-bubble);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:0.9rem;">📥 Baixar CSV (sem dados de revenue)</button>`;
        chatMessages.appendChild(dlMsg);
        document.getElementById(btnId).onclick = () => {
            const blob = new Blob([csvContent], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
            URL.revokeObjectURL(url);
        };
        return;
    }

    const top = sorted.slice(0, 20);
    const totalRevenue = sorted.reduce((s, d) => s + d.revenue, 0);
    const totalImpressions = sorted.reduce((s, d) => s + d.impressions, 0);
    const rpmMedio = sorted.filter(d => d.rpm > 0).reduce((s, d, _, a) => s + d.rpm / a.length, 0);
    const cpmMedio = sorted.filter(d => d.cpm > 0).reduce((s, d, _, a) => s + d.cpm / a.length, 0);

    const revenueData = {
        labels: top.map(d => d.name),
        values: top.map(d => d.revenue),
        total: totalRevenue,
        rpm_medio: rpmMedio,
        cpm_medio: cpmMedio,
        impressions_total: totalImpressions,
    };

    // Render the visual dashboard
    renderRevenueDashboard(revenueData);

    // Also render a summary table for top 10
    const fmtUSD = v => '$' + (v ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const fmtNum = v => (v ?? 0).toLocaleString('pt-BR');

    const tableContainer = document.createElement('div');
    tableContainer.classList.add('message', 'message-bot');
    tableContainer.style.cssText = 'padding:0;border:none;background:transparent;max-width:95%;';

    const dlBtnId = 'dl-rev-csv-' + Date.now();
    const tableRows = top.slice(0, 10).map((d, i) => `
        <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:7px 10px;color:var(--text-secondary);font-size:0.75rem;">${i + 1}</td>
            <td style="padding:7px 10px;font-size:0.78rem;color:var(--text-primary);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</td>
            <td style="padding:7px 10px;font-size:0.82rem;font-weight:700;color:#3fb950;">${fmtUSD(d.revenue)}</td>
            <td style="padding:7px 10px;font-size:0.78rem;color:#4f8cff;">${fmtUSD(d.rpm)}</td>
            <td style="padding:7px 10px;font-size:0.78rem;color:var(--text-secondary);">${fmtNum(Math.round(d.impressions))}</td>
        </tr>`).join('');

    tableContainer.innerHTML = `
    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;overflow:hidden;">
        <div style="padding:12px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
            <span style="font-size:0.8rem;font-weight:600;color:var(--text-secondary);">TOP 10 POR REVENUE · ${sorted.length} supply tags no total</span>
            <button id="${dlBtnId}" style="padding:6px 14px;background:var(--user-bubble);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:0.75rem;">📥 CSV completo</button>
        </div>
        <div style="overflow-x:auto;">
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="border-bottom:1px solid var(--border);background:var(--bg-tertiary);">
                        <th style="padding:8px 10px;text-align:left;font-size:0.72rem;color:var(--text-secondary);font-weight:600;">#</th>
                        <th style="padding:8px 10px;text-align:left;font-size:0.72rem;color:var(--text-secondary);font-weight:600;">Supply Tag</th>
                        <th style="padding:8px 10px;text-align:left;font-size:0.72rem;color:var(--text-secondary);font-weight:600;">Revenue</th>
                        <th style="padding:8px 10px;text-align:left;font-size:0.72rem;color:var(--text-secondary);font-weight:600;">RPM</th>
                        <th style="padding:8px 10px;text-align:left;font-size:0.72rem;color:var(--text-secondary);font-weight:600;">Impressões</th>
                    </tr>
                </thead>
                <tbody>${tableRows}</tbody>
            </table>
        </div>
    </div>`;

    chatMessages.appendChild(tableContainer);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Wire download button
    setTimeout(() => {
        const btn = document.getElementById(dlBtnId);
        if (btn) btn.onclick = () => {
            const blob = new Blob(['\uFEFF' + csvContent], { type: 'text/csv;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
            URL.revokeObjectURL(url);
        };
    }, 100);
}

function extractFillRateFromDesc(desc) {
    // "Configuração live_1071 com fill rate de 0.485% (abaixo de 50%)"
    const m = desc.match(/fill rate de ([\d.]+)/i);
    return m ? parseFloat(m[1]) : null;
}

function renderFillRateDashboard(csvContent, filename) {
    const rows = parseCsv(csvContent);
    if (!rows.length) return;

    // Aggregate by canal: collect time series + stats
    const byCanal = {};
    rows.forEach(r => {
        const canal = r.canal || r.channel || '';
        const ts = r.timestamp || '';
        // fill_rate may be in a dedicated column or embedded in descricao
        let fr = parseFloat(r.fill_rate);
        if (isNaN(fr)) fr = extractFillRateFromDesc(r.descricao || r.descricao || '');
        if (!canal || fr === null || isNaN(fr)) return;

        if (!byCanal[canal]) byCanal[canal] = { series: [], min: Infinity, max: -Infinity, sum: 0, count: 0 };
        byCanal[canal].series.push({ ts, fr });
        byCanal[canal].min = Math.min(byCanal[canal].min, fr);
        byCanal[canal].max = Math.max(byCanal[canal].max, fr);
        byCanal[canal].sum += fr;
        byCanal[canal].count++;
    });

    const canais = Object.keys(byCanal).sort();
    if (!canais.length) return;

    // Sort series by timestamp per canal
    canais.forEach(c => {
        byCanal[c].series.sort((a, b) => a.ts.localeCompare(b.ts));
        byCanal[c].avg = byCanal[c].sum / byCanal[c].count;
    });

    // Sort canais by avg fill rate ascending (worst first)
    canais.sort((a, b) => byCanal[a].avg - byCanal[b].avg);

    const container = document.createElement('div');
    container.classList.add('message', 'message-bot');
    container.style.cssText = 'padding:0;border:none;background:transparent;max-width:95%;';

    const totalEvents = rows.length;
    const worstCanal = canais[0];
    const worstAvg = byCanal[worstCanal].avg;

    // Summary cards HTML
    const summaryCards = canais.slice(0, 6).map(c => {
        const d = byCanal[c];
        const avg = d.avg;
        const color = avg < 20 ? '#f85149' : avg < 40 ? '#d29922' : '#e3b341';
        const emoji = avg < 20 ? '🔴' : avg < 40 ? '🟠' : '🟡';
        return `
        <div style="background:var(--bg-tertiary);border:1px solid var(--border);border-radius:8px;padding:12px;min-width:140px;flex:1;">
            <div style="font-size:0.7rem;color:var(--text-secondary);margin-bottom:4px;font-family:monospace;">${escapeHtml(c)}</div>
            <div style="font-size:1.6rem;font-weight:800;color:${color};">${emoji} ${avg.toFixed(1)}%</div>
            <div style="font-size:0.65rem;color:var(--text-secondary);margin-top:4px;">
                min: ${d.min.toFixed(1)}% · max: ${d.max.toFixed(1)}%<br>
                ${d.count} eventos
            </div>
        </div>`;
    }).join('');

    // Table rows for all canais
    const tableRows = canais.map(c => {
        const d = byCanal[c];
        const avg = d.avg;
        const color = avg < 20 ? '#f85149' : avg < 40 ? '#d29922' : '#e3b341';
        const bar = Math.round(avg);
        return `
        <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:8px;font-family:monospace;font-size:0.78rem;color:var(--text-primary);">${escapeHtml(c)}</td>
            <td style="padding:8px;color:${color};font-weight:700;font-size:0.85rem;">${avg.toFixed(2)}%</td>
            <td style="padding:8px;color:var(--text-secondary);font-size:0.78rem;">${d.min.toFixed(2)}%</td>
            <td style="padding:8px;color:var(--text-secondary);font-size:0.78rem;">${d.max.toFixed(2)}%</td>
            <td style="padding:8px;">
                <div style="background:var(--bg-tertiary);border-radius:4px;height:8px;width:100%;min-width:80px;">
                    <div style="background:${color};height:8px;border-radius:4px;width:${bar}%;"></div>
                </div>
            </td>
            <td style="padding:8px;color:var(--text-secondary);font-size:0.75rem;">${d.count}</td>
        </tr>`;
    }).join('');

    const chartId = 'fillrate-chart-' + Date.now();
    const dlBtnId = 'dl-fillrate-' + Date.now();

    container.innerHTML = `
    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;overflow:hidden;">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#1a0a0a,#2a1010);padding:20px 24px;border-bottom:1px solid var(--border);">
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
                <div>
                    <div style="font-size:1.1rem;font-weight:700;color:#f85149;">📉 Dashboard Fill Rate — MediaTailor</div>
                    <div style="font-size:0.75rem;color:var(--text-secondary);margin-top:4px;">
                        ${canais.length} canais · ${totalEvents.toLocaleString()} eventos · Todos abaixo de 50%
                    </div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:1.8rem;font-weight:800;color:#f85149;">🚨 CRÍTICO</div>
                    <div style="font-size:0.7rem;color:var(--text-secondary);">Pior: ${escapeHtml(worstCanal)} (${worstAvg.toFixed(1)}%)</div>
                </div>
            </div>
        </div>

        <!-- Summary cards -->
        <div style="padding:16px 24px;border-bottom:1px solid var(--border);">
            <div style="font-size:0.75rem;font-weight:600;color:var(--text-secondary);margin-bottom:10px;">PIORES CANAIS (fill rate médio)</div>
            <div style="display:flex;flex-wrap:wrap;gap:10px;">${summaryCards}</div>
        </div>

        <!-- Line chart -->
        <div style="padding:16px 24px;border-bottom:1px solid var(--border);">
            <div style="font-size:0.75rem;font-weight:600;color:var(--text-secondary);margin-bottom:10px;">EVOLUÇÃO TEMPORAL (fill rate % por canal)</div>
            <div style="background:var(--bg-tertiary);border-radius:8px;padding:12px;border:1px solid var(--border);height:260px;">
                <canvas id="${chartId}"></canvas>
            </div>
        </div>

        <!-- Table -->
        <div style="padding:16px 24px;border-bottom:1px solid var(--border);">
            <div style="font-size:0.75rem;font-weight:600;color:var(--text-secondary);margin-bottom:10px;">RESUMO POR CANAL</div>
            <div style="overflow-x:auto;max-height:280px;overflow-y:auto;">
                <table style="width:100%;border-collapse:collapse;font-size:0.8rem;">
                    <thead>
                        <tr style="border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg-secondary);">
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Canal</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Média</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Mín</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Máx</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;min-width:100px;">Fill Rate</th>
                            <th style="text-align:left;padding:8px;color:var(--text-secondary);font-weight:600;">Eventos</th>
                        </tr>
                    </thead>
                    <tbody>${tableRows}</tbody>
                </table>
            </div>
        </div>

        <!-- Download -->
        <div style="padding:12px 24px 16px;display:flex;gap:8px;">
            <button id="${dlBtnId}" style="padding:8px 16px;background:var(--user-bubble);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:0.8rem;">📥 Baixar CSV original</button>
        </div>
    </div>`;

    chatMessages.appendChild(container);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Render line chart — one dataset per canal, sampled to max 60 points
    setTimeout(() => {
        const canvas = document.getElementById(chartId);
        if (!canvas || typeof Chart === 'undefined') return;

        const palette = [
            '#f85149','#d29922','#e3b341','#ff922b','#cc5de8',
            '#4f8cff','#20c997','#51cf66','#748ffc','#ff6b6b',
        ];

        // Sample series to max 60 points per canal for readability
        const sampleSeries = (series, maxPts) => {
            if (series.length <= maxPts) return series;
            const step = Math.ceil(series.length / maxPts);
            return series.filter((_, i) => i % step === 0);
        };

        // Use the canal with most points as x-axis labels
        const refCanal = canais.reduce((a, b) => byCanal[a].series.length > byCanal[b].series.length ? a : b);
        const refSeries = sampleSeries(byCanal[refCanal].series, 60);
        const labels = refSeries.map(p => {
            const d = new Date(p.ts);
            return isNaN(d) ? p.ts.substring(11, 16) : d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        });

        const datasets = canais.slice(0, 8).map((c, i) => {
            const sampled = sampleSeries(byCanal[c].series, 60);
            return {
                label: c,
                data: sampled.map(p => parseFloat(p.fr.toFixed(2))),
                borderColor: palette[i % palette.length],
                backgroundColor: 'transparent',
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.3,
            };
        });

        new Chart(canvas, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        labels: { color: '#8b949e', font: { size: 10 }, boxWidth: 12, padding: 8 },
                    },
                    tooltip: {
                        callbacks: {
                            label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}%`,
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 10, maxRotation: 0 },
                        grid: { color: 'rgba(128,128,128,0.1)' },
                    },
                    y: {
                        min: 0,
                        max: 55,
                        ticks: { color: '#8b949e', font: { size: 10 }, callback: v => v + '%' },
                        grid: { color: 'rgba(128,128,128,0.1)' },
                    },
                },
            },
        });
    }, 50);

    // Wire download button
    setTimeout(() => {
        const btn = document.getElementById(dlBtnId);
        if (btn) {
            btn.onclick = () => {
                const blob = new Blob(['\uFEFF' + csvContent], { type: 'text/csv;charset=utf-8;' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename || 'fill-rate.csv';
                a.click();
                URL.revokeObjectURL(url);
            };
        }
    }, 100);
}
