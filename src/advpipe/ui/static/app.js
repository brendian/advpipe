// advpipe UI: the only hand-written script. Loaded from /static so the Content-Security-Policy
// can forbid inline scripts.
(function () {
  "use strict";

  // Copy buttons: <button data-copy="ID"> copies the text of the element with that id.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-copy]");
    if (!button) return;
    var source = document.getElementById(button.getAttribute("data-copy"));
    if (!source) return;
    var text = source.textContent;
    var done = function (label) {
      button.textContent = label;
      window.setTimeout(function () { button.textContent = "Copy"; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { done("Copied"); },
        function () { selectText(source); done("Press Ctrl+C"); }
      );
    } else {
      selectText(source);
      done("Press Ctrl+C");
    }
  });

  function selectText(element) {
    var range = document.createRange();
    range.selectNodeContents(element);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  // Live refreshes replace the header and timeline. Keep every <details> panel the reader
  // opened or closed the way they left it (panels have stable ids), instead of the defaults.
  // Restored on afterSwap, which fires for each swapped element (out-of-band ones too) before
  // the page is painted, so panels don't flicker.
  var panels = null;
  document.addEventListener("htmx:beforeSwap", function () {
    panels = {};
    document.querySelectorAll("details[id]").forEach(function (d) { panels[d.id] = d.open; });
  });
  document.addEventListener("htmx:afterSwap", function (event) {
    if (!panels) return;
    var root = event.target;
    var found = root.matches("details[id]") ? [root] : [];
    root.querySelectorAll("details[id]").forEach(function (d) { found.push(d); });
    found.forEach(function (d) {
      if (Object.prototype.hasOwnProperty.call(panels, d.id)) d.open = panels[d.id];
    });
  });
  document.addEventListener("htmx:afterSettle", function () { panels = null; });
})();
