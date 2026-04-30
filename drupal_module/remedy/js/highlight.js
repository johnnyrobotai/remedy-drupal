(function () {
  function removeExisting() {
    document.querySelectorAll('.remedy-highlight-overlay').forEach((node) => node.remove());
  }

  function highlight(selector) {
    removeExisting();
    if (!selector) {
      return false;
    }
    const target = document.querySelector(selector);
    if (!target) {
      return false;
    }

    const rect = target.getBoundingClientRect();
    const overlay = document.createElement('div');
    overlay.className = 'remedy-highlight-overlay';
    overlay.style.top = `${window.scrollY + rect.top - 4}px`;
    overlay.style.left = `${window.scrollX + rect.left - 4}px`;
    overlay.style.width = `${Math.max(rect.width + 8, 12)}px`;
    overlay.style.height = `${Math.max(rect.height + 8, 12)}px`;
    document.body.appendChild(overlay);

    target.scrollIntoView({behavior: 'smooth', block: 'center'});
    return true;
  }

  window.RemedyHighlight = {
    highlight,
    clear: removeExisting
  };
})();

