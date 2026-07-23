function togglePasswordVisibility(inputId, button) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const isVisible = input.type === "text";
    input.type = isVisible ? "password" : "text";
    button.classList.toggle("is-visible", !isVisible);
    button.setAttribute("aria-pressed", String(!isVisible));
    const label = isVisible ? button.dataset.showLabel : button.dataset.hideLabel;
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
}
