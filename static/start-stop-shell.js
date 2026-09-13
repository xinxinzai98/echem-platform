(() => {
  "use strict";

  const STORAGE_KEY = "start-stop.sidebar.collapsed.v1";
  const DESKTOP_QUERY = "(min-width: 1181px)";
  const root = document.documentElement;

  function readDesktopCollapsed() {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "true";
    } catch (_error) {
      return false;
    }
  }

  function writeDesktopCollapsed(collapsed) {
    try {
      window.localStorage.setItem(STORAGE_KEY, collapsed ? "true" : "false");
    } catch (_error) {
      // Storage can be disabled by the browser. The current page still works.
    }
  }

  root.dataset.startStopSidebar = readDesktopCollapsed() ? "collapsed" : "expanded";
  // This script runs in <head> so both desktop and compact layouts are final
  // before the sidebar can be painted. initializeSidebar() wires behavior later.
  root.dataset.startStopSidebarReady = "true";

  function initializeSidebar() {
    const sidebar = document.querySelector("#workbenchSidebar");
    const navigation = document.querySelector("#workbenchNavigation");
    const toggle = document.querySelector("#workbenchSidebarToggle");
    if (!sidebar || !navigation || !toggle) return;

    const desktop = window.matchMedia(DESKTOP_QUERY);
    let mobileOpen = false;

    function sync() {
      const desktopMode = desktop.matches;
      const collapsed = root.dataset.startStopSidebar === "collapsed";
      sidebar.dataset.mobileOpen = String(!desktopMode && mobileOpen);
      toggle.setAttribute("aria-expanded", String(desktopMode ? !collapsed : mobileOpen));
      toggle.setAttribute(
        "aria-label",
        desktopMode
          ? collapsed ? "展开侧边栏" : "收起侧边栏"
          : mobileOpen ? "收起导航菜单" : "展开导航菜单",
      );
      toggle.title = toggle.getAttribute("aria-label");
      const label = toggle.querySelector(".workbench-sidebar-toggle-label");
      if (label) {
        label.textContent = desktopMode
          ? collapsed ? "展开" : "收起"
          : mobileOpen ? "收起" : "菜单";
      }
      const icon = toggle.querySelector(".workbench-sidebar-toggle-icon");
      if (icon) icon.textContent = desktopMode ? collapsed ? "›" : "‹" : mobileOpen ? "×" : "☰";
    }

    toggle.addEventListener("click", () => {
      if (desktop.matches) {
        const collapsed = root.dataset.startStopSidebar !== "collapsed";
        root.dataset.startStopSidebar = collapsed ? "collapsed" : "expanded";
        writeDesktopCollapsed(collapsed);
      } else {
        mobileOpen = !mobileOpen;
      }
      sync();
    });

    document.addEventListener("keydown", (event) => {
      if (desktop.matches || event.key !== "Escape" || !mobileOpen) return;
      mobileOpen = false;
      sync();
      toggle.focus({ preventScroll: true });
    });

    const handleBreakpoint = () => {
      mobileOpen = false;
      sync();
    };
    if (typeof desktop.addEventListener === "function") desktop.addEventListener("change", handleBreakpoint);
    else desktop.addListener(handleBreakpoint);

    window.addEventListener("storage", (event) => {
      if (event.key !== STORAGE_KEY || !desktop.matches) return;
      root.dataset.startStopSidebar = event.newValue === "true" ? "collapsed" : "expanded";
      sync();
    });

    sync();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeSidebar, { once: true });
  } else {
    initializeSidebar();
  }
})();
