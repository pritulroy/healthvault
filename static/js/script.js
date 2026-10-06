// HealthVault JavaScript

document.addEventListener("DOMContentLoaded", function () {

    // 1. Ask "Are you sure?" before submitting any form that has data-confirm
    var confirmForms = document.querySelectorAll("form[data-confirm]");
    confirmForms.forEach(function (form) {
        form.addEventListener("submit", function (event) {
            if (!window.confirm(form.dataset.confirm)) {
                event.preventDefault();   // cancel the submit
            }
        });
    });

    // 2. Copy-link button (only exists on the dashboard)
    var copyButton = document.getElementById("copy-link-btn");
    var linkInput = document.getElementById("emergency-link");

    if (!copyButton || !linkInput) {
        return;
    }

    copyButton.addEventListener("click", function () {
        // Modern way (works on https:// and on localhost)
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(linkInput.value).then(showCopied);
        } else {
            // Fallback for older browsers
            linkInput.select();
            document.execCommand("copy");
            showCopied();
        }
    });

    function showCopied() {
        var original = copyButton.textContent;
        copyButton.textContent = "Copied!";
        setTimeout(function () {
            copyButton.textContent = original;
        }, 1500);
    }
});