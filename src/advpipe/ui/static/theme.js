// advpipe UI: applies the theme chosen with the theme button (app.js) before the page is drawn,
// so it doesn't flash the other theme. Loaded in <head> without defer, for that reason.
(function () {
  "use strict";
  try {
    var theme = window.localStorage.getItem("advpipe-theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    }
  } catch (e) {
    // Storage blocked: follow the system setting.
  }
})();
