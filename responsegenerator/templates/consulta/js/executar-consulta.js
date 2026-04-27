const selectedQuestions = new Set();

function toggleQuestion(element) {
    const id = element.dataset.id;
    if (selectedQuestions.has(id)) {
        selectedQuestions.delete(id);
        element.classList.remove("selected");
    } else {
        selectedQuestions.add(id);
        element.classList.add("selected");
    }
}

async function showAnswer(id) {
    const response = await fetch(`/respostas/${id}`)
    const data = await response.json()

    document.getElementById("modal-questao-conteudo").innerHTML = data.questao
    document.getElementById("modal-overlay").style.display = "flex"

    const grid = document.getElementById("answers-card")
    grid.innerHTML = ""

    for (const r of data.respostas) {
        grid.innerHTML += `
            <div class="answer-card">
                <div class="ai-name">${r.llm}</div>
                <div class="ai-response">${r.conteudo}</div>
            </div>
        `
    }
}

function closeAnswer() {
    document.getElementById("modal-overlay").style.display = "none"
}

async function executeQuestions() {
    if (selectedQuestions.size === 0) {
        alert("Selecione pelo menos uma pergunta para executar.");
        return;
    }

    const btnRun = document.querySelector('.btn-run');
    const originalText = btnRun.innerHTML;
    
    btnRun.innerHTML = '<span>⏳</span><span>Processando...</span>';
    btnRun.disabled = true;

    document.getElementById('progressBar').style.display = 'block';

    try { // Cria um array de requisições
        const requisicoes = Array.from(selectedQuestions).map(id => 
            fetch(`/gerar_resposta/${id}`)
        );

        await Promise.all(requisicoes); // => Espera TODAS as requisições terminarem ao mesmo tempo

        selectedQuestions.clear(); // => Limpa as seleções e recarrega a página apenas quando tudo estiver pronto
        document.getElementById('progressBar').style.display = 'none';
        location.reload();
        
    } catch (error) {
        console.error("Erro ao executar consultas:", error);
        alert("Houve um erro ao processar as perguntas.");
        btnRun.innerHTML = originalText;
        btnRun.disabled = false;
    }
}

function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            // Verifica se o cookie começa com o nome que queremos ("csrftoken")
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

async function limparRespostas() {
    if (!confirm("Apagar todas as respostas do banco?")) return;

    const res = await fetch("/limpar_respostas/", {
        method: "POST",
        headers: { "X-CSRFToken": getCookie("csrftoken") }
    });
    const data = await res.json();

    if (data.ok) location.reload();
}