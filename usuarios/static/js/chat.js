(function() {
    const config = window.PONDERSEC_CHAT || {};
    
    document.addEventListener("DOMContentLoaded", () => {
        const userMenuBtn = document.getElementById("userMenuBtn");
        const userDropdown = document.getElementById("userDropdown");
        if (userMenuBtn && userDropdown) {
            userMenuBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                userDropdown.classList.toggle("show");
                userMenuBtn.classList.toggle("active");
            });
            document.addEventListener("click", (e) => {
                if (!userDropdown.contains(e.target) && !userMenuBtn.contains(e.target)) {
                    userDropdown.classList.remove("show");
                    userMenuBtn.classList.remove("active");
                }
            });
        }
    });

    window.getCSRFToken = function() {
        const name = "csrftoken";
        const cookies = document.cookie ? document.cookie.split(";") : [];
        for (let cookie of cookies) {
            cookie = cookie.trim();
            if (cookie.startsWith(name + "=")) return decodeURIComponent(cookie.substring(name.length + 1));
        }
        return "";
    };

    window.escapeHtml = function(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    };

    window.renderCrossEvaluationTable = function(rows) {
        if (!Array.isArray(rows) || rows.length === 0) return "";
        const body = rows.map(row => {
            const nota = Number(row.nota);
            const max = Number(row.max || 5);
            const notaTxt = Number.isFinite(nota) ? `${nota}/${max}` : `-/${max}`;
            return `<tr>
                <td class="cross-eval-model">${escapeHtml(row.modelo_respondente || config.text.llm_respondente)}</td>
                <td class="cross-eval-judge">${escapeHtml(row.modelo_avaliador || config.text.llm_avaliadora)}</td>
                <td>${escapeHtml(row.metrica || config.text.metrica)}</td>
                <td class="cross-eval-score">${notaTxt}</td>
                <td class="cross-eval-note">${escapeHtml(row.justificativa || "-")}</td>
            </tr>`;
        }).join("");
        return `<section class="cross-eval-panel">
            <div class="cross-eval-title">
                <strong>${config.text.tabela_avaliacao}</strong>
                <span>${rows.length} ${config.text.notas}</span>
            </div>
            <div class="cross-eval-table-wrap">
                <table class="cross-eval-table">
                    <thead><tr>
                        <th>${config.text.col_llm_respondente}</th>
                        <th>${config.text.col_llm_avaliadora}</th>
                        <th>${config.text.col_metrica}</th>
                        <th>${config.text.col_nota}</th>
                        <th>${config.text.col_justificativa}</th>
                    </tr></thead>
                    <tbody>${body}</tbody>
                </table>
            </div>
        </section>`;
    };

    const textarea = document.getElementById('questionInput');
    const sendBtn = document.getElementById('sendBtn');
    if (textarea && sendBtn) {
        textarea.addEventListener('input', function() {
            this.style.height = '44px';
            this.style.height = Math.max(44, this.scrollHeight) + 'px';
            sendBtn.disabled = this.value.trim() === '';
        });
        textarea.addEventListener("keydown", function(event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                if (!sendBtn.disabled) window.sendPublicQuestion();
            }
        });
    }

    const chatHistory = document.getElementById("chatHistory");
    const heroSection = document.getElementById("heroSection");
    let isFirstMessage = true;

    window.scrollToBottom = function() {
        chatHistory.scrollTo({ top: chatHistory.scrollHeight, behavior: 'smooth' });
    };

    window.addUserMessage = function(text) {
        if (isFirstMessage && heroSection) { heroSection.style.display = "none"; isFirstMessage = false; }
        const div = document.createElement('div');
        div.className = 'message-wrapper user-group';
        div.innerHTML = `<div class="message-user">${escapeHtml(text).replace(/\n/g, '<br>')}</div>`;
        chatHistory.appendChild(div);
        scrollToBottom();
    };

    window.addLoadingBubble = function() {
        const id = 'load-' + Date.now();
        const div = document.createElement('div');
        div.className = 'message-wrapper ai-group';
        div.id = id;
        div.innerHTML = `
            <div class="ai-responses-grid single-response">
                <div class="message-ai" style="padding: 16px 24px;">
                    <div class="message-ai-header" style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">
                        <div class="ai-icon">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                        </div>
                        <span class="ai-name">PonderSEC</span>
                        <div class="typing-indicator" style="margin-left: auto;"><span></span><span></span><span></span></div>
                    </div>
                </div>
            </div>`;
        chatHistory.appendChild(div);
        scrollToBottom();
        return id;
    };

    window.removeBubble = function(id) {
        const el = document.getElementById(id);
        if (el) el.remove();
    };

    window.addAIBubblesGroup = function(respostas, tabelaAvaliacaoCruzada) {
        tabelaAvaliacaoCruzada = tabelaAvaliacaoCruzada || [];
        const div = document.createElement('div');
        div.className = 'message-wrapper ai-group';
        const gridClass = respostas.length === 1 ? "ai-responses-grid single-response" : "ai-responses-grid";
        let html = `<div class="${gridClass}">`;
        if (respostas.length === 0) {
            html += `<div class="message-ai">
                <div class="message-ai-header">
                    <div class="ai-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg></div>
                    <span class="ai-name">${config.text.sistema || 'Sistema'}</span>
                    <span class="ai-status error">${config.text.erro || 'Erro'}</span>
                </div>
                <div class="markdown-body">${config.text.nenhum_modelo || 'Nenhum modelo retornou resposta.'}</div>
            </div>`;
        } else {
            respostas.forEach(r => {
                const modelName = escapeHtml(r.modelo || r.model || r.nome || "LLM");
                const rawMarkdown = r.resposta || r.content || r.texto;
                const ok = r.ok !== false;
                const htmlContent = (typeof marked !== 'undefined') ? marked.parse(rawMarkdown || (config.text.sem_conteudo || 'Sem conteúdo.')) : (rawMarkdown || (config.text.sem_conteudo || 'Sem conteúdo.')).replace(/\n/g, '<br>');
                const statusHtml = ok ? (config.text.concluido || 'Concluído') : (config.text.erro || 'Erro');
                const statusClass = ok ? '' : 'error';
                html += `<div class="message-ai">
                    <div class="message-ai-header">
                        <div class="ai-icon">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2 2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"/><path d="M12 8v3"/><path d="M6.5 11a5.5 5.5 0 0 0 11 0"/><rect x="3" y="11" width="4" height="7" rx="1"/><rect x="17" y="11" width="4" height="7" rx="1"/><path d="M7 18h10"/><path d="M9 21h6"/></svg>
                        </div>
                        <span class="ai-name">${modelName}</span>
                        <span class="ai-status ${statusClass}">${statusHtml}</span>
                    </div>
                    <div class="markdown-body">${htmlContent}</div>
                </div>`;
            });
        }
        html += '</div>';
        html += renderCrossEvaluationTable(tabelaAvaliacaoCruzada);
        div.innerHTML = html;
        chatHistory.appendChild(div);
    };

    window.sendPublicQuestion = async function() {
        const pergunta = textarea.value.trim();
        if (!pergunta) return;
        textarea.value = '';
        textarea.style.height = '44px';
        sendBtn.disabled = true;
        addUserMessage(pergunta);
        const loadingId = addLoadingBubble();
        try {
            const response = await fetch(config.apiUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCSRFToken() },
                body: JSON.stringify({ pergunta })
            });
            const data = await response.json();
            removeBubble(loadingId);
            if (!response.ok || data.status === "erro") {
                throw new Error(data.mensagem || (config.text.erro_consulta || 'Erro ao consultar os modelos.'));
            }
            const respostas = data.respostas || data.responses || [];
            addAIBubblesGroup(respostas, data.tabela_avaliacao_cruzada || []);
            scrollToBottom();
        } catch (error) {
            removeBubble(loadingId);
            addAIBubblesGroup([{ modelo: "Sistema", resposta: `${config.text.erro || 'Erro:'} ${error.message}`, ok: false }]);
            scrollToBottom();
        }
    };
})();
