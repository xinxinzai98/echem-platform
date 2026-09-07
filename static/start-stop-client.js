/* Shared same-origin requests and visibility-aware, non-overlapping polling. */
globalThis.StartStopClient = (() => {
  "use strict";

  class RequestError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.name = "RequestError";
      this.status = status;
    }
  }

  async function request(url, options = {}, ErrorType = RequestError) {
    const { timeoutMs = 30_000, signal: externalSignal, readResponse, ...fetchOptions } = options;
    const controller = new AbortController();
    let timedOut = false;
    const abortFromCaller = () => controller.abort(externalSignal.reason);
    if (externalSignal?.aborted) abortFromCaller();
    else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    let rejectAbort;
    const onAbort = () => rejectAbort(controller.signal.reason || new DOMException("请求已取消", "AbortError"));
    const aborted = new Promise((_, reject) => { rejectAbort = reject; });
    controller.signal.addEventListener("abort", onAbort, { once: true });
    if (controller.signal.aborted) onAbort();
    try {
      if (controller.signal.aborted) return await aborted;
      const work = async () => {
        const response = await fetch(url, {
          cache: "no-store", credentials: "same-origin", ...fetchOptions, signal: controller.signal,
          headers: { Accept: "application/json", ...(fetchOptions.body ? { "Content-Type": "application/json" } : {}),
            ...(fetchOptions.headers || {}) },
        });
        if (response.ok && readResponse) return await readResponse(response);
        const type = response.headers.get("content-type") || "";
        const payload = type.includes("application/json") ? await response.json() : null;
        if (!response.ok) throw new ErrorType(payload?.error || `请求失败（${response.status}）`, response.status);
        if (payload === null) throw new ErrorType("服务器未返回有效数据，请刷新后重试。", response.status);
        return payload;
      };
      return await Promise.race([work(), aborted]);
    } catch (error) {
      if (timedOut) {
        const timeout = new ErrorType("请求超时，请重试；后台任务可能仍在运行，可刷新查看进度。", 0);
        timeout.name = "TimeoutError";
        throw timeout;
      }
      if (!controller.signal.aborted && error.name === "TypeError"
          && /Failed to fetch|NetworkError|Load failed/i.test(error.message)) {
        throw new ErrorType("无法连接服务器，请检查网络后重试；后台任务可能仍在运行。", 0);
      }
      if (error.name === "SyntaxError") throw new ErrorType("服务器数据格式异常，请刷新后重试。", 0);
      throw error;
    } finally {
      clearTimeout(timer);
      externalSignal?.removeEventListener("abort", abortFromCaller);
      controller.signal.removeEventListener("abort", onAbort);
    }
  }

  function download(url, options = {}) {
    return request(url, { timeoutMs: 300_000, ...options,
      readResponse: async response => ({ response, blob: await response.blob() }) });
  }

  function downloadFilename(response, fallback) {
    const match = (response.headers.get("content-disposition") || "").match(/filename\*=UTF-8''([^;]+)/i);
    if (!match) return fallback;
    try { return decodeURIComponent(match[1]); } catch (_error) { return fallback; }
  }

  const routes = Object.freeze({
    config: "/start-stop", workstations: "/start-stop/workstations", lanbts: "/start-stop/lanbts",
    analysis: "/start-stop/analysis", "cv-eis": "/start-stop/cv-eis", materials: "/start-stop/materials",
  });
  function applyRoutes(root = document) {
    for (const [name, href] of Object.entries(routes)) {
      root.querySelectorAll(`[data-${name}-route]`).forEach(link => { link.href = href; });
    }
  }

  function clearVisibleTimeout(ticket) {
    if (!ticket) return;
    ticket.cancelled = true;
    clearTimeout(ticket.id);
    document.removeEventListener("visibilitychange", ticket.onVisible);
  }

  function visibleTimeout(callback, delay) {
    const ticket = { id: null, cancelled: false, onVisible: null };
    ticket.onVisible = () => {
      if (ticket.cancelled || document.hidden) return;
      clearVisibleTimeout(ticket);
      callback();
    };
    ticket.id = setTimeout(() => {
      if (ticket.cancelled) return;
      if (document.hidden) document.addEventListener("visibilitychange", ticket.onVisible);
      else ticket.onVisible();
    }, delay);
    return ticket;
  }

  return Object.freeze({ request, download, downloadFilename, RequestError, applyRoutes, visibleTimeout, clearVisibleTimeout, routes });
})();
