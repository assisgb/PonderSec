// Função para copiar o texto direto do elemento HTML
function copyResponse(elementId) {
    const textElement = document.getElementById(elementId);
    // Usamos innerText para pegar o texto limpo, sem as tags HTML (como <br>)
    const text = textElement.innerText; 
    
    navigator.clipboard.writeText(text).then(() => {
        alert('Resposta copiada para a área de transferência!');
    }).catch(err => {
        console.error('Erro ao copiar: ', err);
    });
}

// Função para expandir o modal dinamicamente
function expandResponse(llmName, elementId) {
    const textElement = document.getElementById(elementId);
    // Pegamos o innerHTML para manter as quebras de linha (<br>) no modal
    const htmlContent = textElement.innerHTML; 
    
    document.getElementById('modalTitle').textContent = `${llmName} - Resposta Completa`;
    document.getElementById('modalBody').innerHTML = htmlContent;
    document.getElementById('responseModal').classList.add('active');
}

function closeModal() {
    document.getElementById('responseModal').classList.remove('active');
}

// Fechar modal ao clicar fora dele
document.getElementById('responseModal').addEventListener('click', function(e) {
    if (e.target === this) {
        closeModal();
    }
});