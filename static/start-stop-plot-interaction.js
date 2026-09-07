/* Shared interaction geometry; it never changes or recomputes scientific data. */
globalThis.StartStopPlotInteraction = class {
  constructor({frame, selection, menu, geometry, domain, reset, inspect, modeChanged}) {
    Object.assign(this, {frame, selection, menu, geometry, domain, reset, inspect, modeChanged});
    this.mode = "inspect";
    this.drag = null;
    frame.addEventListener("pointerdown", event => this.begin(event));
    frame.addEventListener("pointermove", event => this.move(event));
    frame.addEventListener("pointerup", event => this.finish(event));
    frame.addEventListener("pointercancel", () => this.cancel());
    frame.addEventListener("dblclick", event => { event.preventDefault(); this.reset(); });
    frame.addEventListener("wheel", event => {
      if (document.activeElement !== frame || !this.geometry()) return;
      event.preventDefault();
      this.zoom(Math.exp(Math.max(-500, Math.min(500, event.deltaY)) * 0.0015), this.position(event));
    }, {passive:false});
    frame.addEventListener("keydown", event => {
      if (["0", "Home"].includes(event.key)) this.reset();
      else if (["+", "="].includes(event.key)) this.zoom(0.75);
      else if (event.key === "-") this.zoom(1.4);
      else if (event.key.toLowerCase() === "z") this.setMode("zoom");
      else if (event.key.toLowerCase() === "p") this.setMode("pan");
      else if (event.key === "Escape") { this.cancel(); this.menu.hidden = true; }
      else return;
      event.preventDefault();
    });
    frame.addEventListener("contextmenu", event => {
      event.preventDefault();
      const box = frame.getBoundingClientRect();
      menu.style.left = `${Math.max(0, Math.min(event.clientX - box.left, box.width - 165))}px`;
      menu.style.top = `${Math.max(0, Math.min(event.clientY - box.top, box.height - 52))}px`;
      menu.hidden = false;
      menu.querySelector("button")?.focus({preventScroll:true});
    });
    menu.querySelector("button").addEventListener("click", () => {
      menu.hidden = true;
      this.reset();
      frame.focus({preventScroll:true});
    });
    document.addEventListener("pointerdown", event => { if (!menu.contains(event.target)) menu.hidden = true; });
  }

  setMode(mode) {
    if (!["inspect", "zoom", "pan"].includes(mode)) return;
    this.cancel();
    this.mode = mode;
    this.frame.dataset.interactionMode = mode;
    this.modeChanged?.(mode);
  }

  position(event, geometry = this.geometry()) {
    if (!geometry) return null;
    const rect = this.frame.getBoundingClientRect();
    const x = (event.clientX - rect.left) * geometry.width / rect.width;
    const y = (event.clientY - rect.top) * geometry.height / rect.height;
    const plot = geometry.plot;
    return {x, y, xRatio:Math.max(0, Math.min(1, (x - plot.left) / plot.width)),
      yRatio:Math.max(0, Math.min(1, (y - plot.top) / plot.height)),
      inside:x >= plot.left && x <= plot.left + plot.width && y >= plot.top && y <= plot.top + plot.height};
  }

  zoom(factor, position = null) {
    const geometry = this.geometry();
    if (!geometry) return;
    const d = geometry.domain;
    const xRatio = position?.xRatio ?? 0.5;
    const yRatio = position?.yRatio ?? 0.5;
    const xAnchor = d.xMin + xRatio * (d.xMax - d.xMin);
    const yAnchor = d.yMax - yRatio * (d.yMax - d.yMin);
    this.domain({xMin:xAnchor - (xAnchor-d.xMin)*factor, xMax:xAnchor+(d.xMax-xAnchor)*factor,
      yMin:yAnchor-(yAnchor-d.yMin)*factor, yMax:yAnchor+(d.yMax-yAnchor)*factor});
  }

  begin(event) {
    if (event.button !== 0 || this.menu.contains(event.target)) return;
    const geometry = this.geometry();
    const start = this.position(event, geometry);
    if (!start?.inside) return;
    this.frame.focus({preventScroll:true});
    this.menu.hidden = true;
    this.drag = {pointerId:event.pointerId, start, geometry};
    this.frame.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  move(event) {
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    const {start, geometry} = this.drag;
    const point = this.position(event, geometry);
    if (this.mode === "pan") {
      const d = geometry.domain;
      const dx = (point.x - start.x) / geometry.plot.width * (d.xMax-d.xMin);
      const dy = (point.y - start.y) / geometry.plot.height * (d.yMax-d.yMin);
      this.domain({xMin:d.xMin-dx,xMax:d.xMax-dx,yMin:d.yMin+dy,yMax:d.yMax+dy});
    } else if (this.mode === "zoom") {
      const rect = this.frame.getBoundingClientRect();
      const plot = geometry.plot;
      const x = plot.left + point.xRatio * plot.width;
      const y = plot.top + point.yRatio * plot.height;
      this.selection.hidden = false;
      Object.assign(this.selection.style, {
        left:`${Math.min(start.x,x)*rect.width/geometry.width}px`,
        top:`${Math.min(start.y,y)*rect.height/geometry.height}px`,
        width:`${Math.abs(x-start.x)*rect.width/geometry.width}px`,
        height:`${Math.abs(y-start.y)*rect.height/geometry.height}px`,
      });
    }
  }

  finish(event) {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const point = this.position(event, drag.geometry);
    const moved = Math.hypot(point.x-drag.start.x, point.y-drag.start.y);
    if (this.mode === "zoom" && moved > 8 && Math.abs(point.x-drag.start.x)>4 && Math.abs(point.y-drag.start.y)>4) {
      const d = drag.geometry.domain;
      const x1 = d.xMin+drag.start.xRatio*(d.xMax-d.xMin);
      const x2 = d.xMin+point.xRatio*(d.xMax-d.xMin);
      const y1 = d.yMax-drag.start.yRatio*(d.yMax-d.yMin);
      const y2 = d.yMax-point.yRatio*(d.yMax-d.yMin);
      this.domain({xMin:Math.min(x1,x2),xMax:Math.max(x1,x2),yMin:Math.min(y1,y2),yMax:Math.max(y1,y2)});
    } else if (this.mode === "inspect" && moved < 8) this.inspect?.(point);
    this.release();
  }

  release() {
    if (this.drag && this.frame.hasPointerCapture(this.drag.pointerId)) this.frame.releasePointerCapture(this.drag.pointerId);
    this.drag = null;
    this.selection.hidden = true;
  }

  cancel() {
    if (this.drag && this.mode === "pan") this.domain(this.drag.geometry.domain);
    this.release();
  }
};
