window.PrivateCompanionAppearance = (() => {
  function normalizeFontFamily(value) {
    return String(value || "original").trim().toLowerCase() === "cheng" ? "cheng" : "original";
  }

  function applyFontFamily(value) {
    const font = normalizeFontFamily(value);
    document.documentElement.dataset.pageFont = font;
    try { localStorage.setItem("pc_font", font); } catch (e) {}
    document.querySelectorAll("[data-page-font-select]").forEach((select) => {
      if (select instanceof HTMLSelectElement) select.value = font;
    });
    return font;
  }

  return {
    normalizeFontFamily,
    applyFontFamily,
  };
})();
