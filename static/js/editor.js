export class CanvasEditor {
  constructor(canvasId, tools) {
    this.canvasId = canvasId;
    this.tools = tools;
    this.canvas = null;
    this.currentToolType = null;
    this.pendingId = 1;
    this.pendingRole = "Body";

    // 状态
    this.isDrawing = false;
    this.isPanning = false;
    this.isAltDown = false;

    this.tempRect = null;
    this.origX = 0;
    this.origY = 0;
    this.lastPosX = 0;
    this.lastPosY = 0;

    this.tooltipEl = null;
    this.logicalWidth = 0;
    this.logicalHeight = 0;

    this._resizeHandler = this.resizeCanvasToContainer.bind(this);
    this._keyDownHandler = this.handleKeyDown.bind(this);
    this._keyUpHandler = this.handleKeyUp.bind(this);
  }

  init() {
    this.canvas = new fabric.Canvas(this.canvasId, {
      preserveObjectStacking: true,
      uniformScaling: false,
      selection: false,
      fireRightClick: true,
      stopContextMenu: true,
      defaultCursor: "default",
      renderOnAddRemove: false,
      targetFindTolerance: 4, // 保持之前调优的参数
    });

    this.resizeCanvasToContainer();

    window.addEventListener("resize", this._resizeHandler);
    window.addEventListener("keydown", this._keyDownHandler);
    window.addEventListener("keyup", this._keyUpHandler);

    this.canvas.on("mouse:down", (o) => this.handleMouseDown(o));
    this.canvas.on("mouse:move", (o) => {
      this.handleMouseMove(o);
      this.updateTooltipPosition(o.e);
    });
    this.canvas.on("mouse:up", (o) => this.handleMouseUp(o));
    this.canvas.on("mouse:out", () => this.hideTooltip());
    this.canvas.on("mouse:over", () => this.showTooltip());
    this.canvas.on("mouse:wheel", (o) => this.handleWheel(o));

    this.canvas.on("object:modified", (o) => this.handleObjectModified(o));
    const syncLabel = (o) => this.updateLabelPosition(o.target);
    this.canvas.on("object:moving", syncLabel);
    this.canvas.on("object:scaling", syncLabel);
    this.canvas.on("object:resizing", syncLabel);

    this.canvas.on("selection:created", (e) => this.handleSelection(e));
    this.canvas.on("selection:updated", (e) => this.handleSelection(e));
    this.canvas.on("selection:cleared", () => this.handleSelectionCleared());

    this.initTooltip();
  }

  handleKeyDown(e) {
    if (e.key === "Alt") {
      this.isAltDown = true;
      if (this.canvas) {
        this.canvas.defaultCursor = "grab";
        this.canvas.requestRenderAll();
      }
    }
  }

  handleKeyUp(e) {
    if (e.key === "Alt") {
      this.isAltDown = false;
      this.isPanning = false;
      if (this.canvas) {
        this.canvas.defaultCursor = this.currentToolType
          ? "crosshair"
          : "default";
        this.canvas.requestRenderAll();
      }
    }
  }

  handleMouseDown(o) {
    const evt = o.e;

    // 1. 平移 (Alt + 左键)
    if (this.isAltDown && evt.button === 0) {
      this.isPanning = true;
      this.lastPosX = evt.clientX;
      this.lastPosY = evt.clientY;
      this.canvas.defaultCursor = "grabbing";
      return;
    }

    // 2. 右键删除
    if (evt.button === 2) {
      const target = this.canvas.findTarget(evt);
      if (
        this.currentToolType &&
        target &&
        target.customData &&
        target.customData.type === this.currentToolType
      ) {
        this.canvas.setActiveObject(target);
        this.deleteSelected();
      }
      return;
    }

    // 3. 选中判定
    const target = o.target || this.canvas.findTarget(evt);
    if (target) {
      if (this.canvas.getActiveObject() !== target) {
        this.canvas.setActiveObject(target);
      }
      return;
    }

    // 4. 画图逻辑
    if (this.currentToolType && !this.isAltDown && evt.button === 0) {
      this.isDrawing = true;
      const pointer = this.canvas.getPointer(evt);
      this.origX = pointer.x;
      this.origY = pointer.y;

      const tool = this.tools.find((t) => t.type === this.currentToolType);
      this.tempRect = new fabric.Rect({
        left: this.origX,
        top: this.origY,
        width: 0,
        height: 0,
        fill: "rgba(0,0,0,0.1)",
        stroke: tool.color,
        strokeWidth: 2,
        selectable: false,
        evented: false,
      });
      this.canvas.add(this.tempRect);
    }
  }

  handleMouseMove(o) {
    if (this.isPanning) {
      const e = o.e;
      const vpt = this.canvas.viewportTransform;
      vpt[4] += e.clientX - this.lastPosX;
      vpt[5] += e.clientY - this.lastPosY;
      this.canvas.requestRenderAll();
      this.lastPosX = e.clientX;
      this.lastPosY = e.clientY;
      return;
    }

    if (this.isDrawing && this.tempRect) {
      const pointer = this.canvas.getPointer(o.e);
      if (this.origX > pointer.x)
        this.tempRect.set({ left: Math.abs(pointer.x) });
      if (this.origY > pointer.y)
        this.tempRect.set({ top: Math.abs(pointer.y) });
      this.tempRect.set({ width: Math.abs(this.origX - pointer.x) });
      this.tempRect.set({ height: Math.abs(this.origY - pointer.y) });
      this.canvas.requestRenderAll();
    }
  }

  handleMouseUp(o) {
    if (this.isPanning) {
      this.isPanning = false;
      this.canvas.defaultCursor = this.isAltDown
        ? "grab"
        : this.currentToolType
        ? "crosshair"
        : "default";
      this.canvas.requestRenderAll();
      return;
    }

    if (this.isDrawing && this.tempRect) {
      this.isDrawing = false;
      this.canvas.remove(this.tempRect);
      if (this.tempRect.width < 5 || this.tempRect.height < 5) {
        this.canvas.requestRenderAll();
        return;
      }

      const newItem = {
        rect: [
          this.tempRect.left,
          this.tempRect.top,
          this.tempRect.left + this.tempRect.width,
          this.tempRect.top + this.tempRect.height,
        ],
        type: this.currentToolType,
        id: this.pendingId,
        role: this.pendingRole,
        uuid: crypto.randomUUID(),
      };

      this.addRect(newItem, true);

      if (this.onObjectAdded) this.onObjectAdded(newItem);
      this.tempRect = null;
      this.canvas.requestRenderAll();
    }
  }

  handleWheel(opt) {
    if (!opt.e.altKey) return;
    const delta = opt.e.deltaY;
    let zoom = this.canvas.getZoom();
    zoom *= 0.999 ** delta;
    if (zoom > 20) zoom = 20;
    if (zoom < 0.05) zoom = 0.05;
    this.canvas.zoomToPoint({ x: opt.e.offsetX, y: opt.e.offsetY }, zoom);
    opt.e.preventDefault();
    opt.e.stopPropagation();
    this.canvas.requestRenderAll();
    this.updateTooltipPosition(opt.e);
  }

  resizeCanvasToContainer() {
    if (!this.canvas) return;
    const canvasEl = document.getElementById(this.canvasId);
    if (!canvasEl) return;
    const container = canvasEl.closest(".canvas-shadow-box");
    if (container && container.clientWidth > 0) {
      this.canvas.setWidth(container.clientWidth);
      this.canvas.setHeight(container.clientHeight);
      this.canvas.requestRenderAll();
    }
  }

  setBackground(imageUrl, logicalWidth, logicalHeight) {
    this.logicalWidth = logicalWidth;
    this.logicalHeight = logicalHeight;
    return new Promise((resolve, reject) => {
      if (!this.canvas) return resolve();
      this.resizeCanvasToContainer();
      this.canvas.setViewportTransform([1, 0, 0, 1, 0, 0]);

      fabric.Image.fromURL(imageUrl, (img) => {
        if (!img) return reject("Image load error");
        img.scaleToWidth(logicalWidth);
        img.set({
          originX: "left",
          originY: "top",
          left: 0,
          top: 0,
          selectable: false,
          evented: false,
        });

        this.canvas.setBackgroundImage(img, () => {
          const containerW = this.canvas.getWidth();
          const containerH = this.canvas.getHeight();
          const validW = containerW > 0 ? containerW : 800;
          const validH = containerH > 0 ? containerH : 600;
          const scaleX = validW / logicalWidth;
          const scaleY = validH / logicalHeight;
          const startZoom = Math.min(scaleX, scaleY) * 0.95;
          const centerX = (validW - logicalWidth * startZoom) / 2;
          const centerY = (validH - logicalHeight * startZoom) / 2;

          this.canvas.setViewportTransform([
            startZoom,
            0,
            0,
            startZoom,
            centerX,
            centerY,
          ]);
          this.canvas.requestRenderAll();
          resolve();
        });
      });
    });
  }

  updateMode(toolType) {
    this.currentToolType = toolType;
    if (!this.isAltDown) {
      this.canvas.defaultCursor = toolType ? "crosshair" : "default";
    }
    if (!toolType) this.hideTooltip();
    else {
      this.renderTooltipContent();
      this.showTooltip();
    }
    if (!this.canvas) return;
    this.canvas.getObjects().forEach((obj) => {
      if (!obj.customData) return;
      if (!toolType) this.setObjectVisualState(obj, 1.0, false);
      else if (obj.customData.type !== toolType)
        this.setObjectVisualState(obj, 0.3, false);
      else this.setObjectVisualState(obj, 0.8, true);
    });
    this.updateFocus();
    // [新增] 必须手动触发重绘，否则从工具切换回总览模式时，视觉状态不会刷新
    this.canvas.requestRenderAll();
  }

  renderLayoutItems(items) {
    if (!this.canvas) return;
    this.canvas.remove(...this.canvas.getObjects());
    items.forEach((item) => this.addRect(item, false));
    this.updateMode(this.currentToolType);
    this.canvas.requestRenderAll();
  }

  addRect(item, isNew = true) {
    const [x0, y0, x1, y1] = item.rect;
    const tool = this.tools.find((t) => t.type === item.type);
    const color = tool ? tool.color : "black";
    const width = Math.abs(x1 - x0);
    const height = Math.abs(y1 - y0);
    const left = Math.min(x0, x1);
    const top = Math.min(y0, y1);
    const isCaption = item.role === "Caption";
    const dashArray = isCaption ? [5, 5] : null;

    const rect = new fabric.Rect({
      left: left,
      top: top,
      width: width,
      height: height,
      fill: "rgba(0,0,0,0)",
      stroke: color,
      strokeWidth: 2,
      strokeUniform: true,
      strokeDashArray: dashArray,

      lockUniScaling: false,
      lockScalingX: false,
      lockScalingY: false,
      hasControls: true,
      transparentCorners: false,
      cornerColor: "white",
      cornerStrokeColor: color,
      cornerStyle: "circle",
      cornerSize: 12,
      touchCornerSize: 24,
      padding: 0,

      lockRotation: true,
      hasRotatingPoint: false,
    });

    rect.setControlsVisibility({
      tl: true,
      tr: true,
      bl: true,
      br: true,
      mt: true,
      mb: true,
      ml: true,
      mr: true,
      mtr: false,
    });

    rect.customData = { ...item };

    const roleShort = item.role === "Body" ? "B" : "C";
    const labelText = ["Header", "Mask", "ContentArea"].includes(item.type)
      ? item.type
      : `${item.type} ${item.id} (${roleShort})`;

    const text = new fabric.Text(labelText, {
      left: left,
      top: top - 18,
      fontSize: 12,
      fill: "white",
      backgroundColor: color,
      selectable: false,
      evented: false,
      excludeFromExport: true,
      objectCaching: false,
    });

    rect.labelObj = text;
    text.rectObj = rect;
    this.canvas.add(rect);
    this.canvas.add(text);

    if (isNew) this.canvas.setActiveObject(rect);
  }

  updateLabelPosition(rect) {
    if (!rect || !rect.labelObj) return;
    rect.labelObj.set({ left: rect.left, top: rect.top - 18 });
  }

  handleObjectModified(e) {
    const obj = e.target;
    if (!obj || !obj.customData) return;
    const scaleX = obj.scaleX || 1;
    const scaleY = obj.scaleY || 1;
    const newWidth = obj.width * scaleX;
    const newHeight = obj.height * scaleY;
    obj.set({ width: newWidth, height: newHeight, scaleX: 1, scaleY: 1 });
    this.updateLabelPosition(obj);
    const modifiedItem = {
      rect: [obj.left, obj.top, obj.left + newWidth, obj.top + newHeight],
      ...obj.customData,
    };
    if (this.onObjectModified) this.onObjectModified(modifiedItem);
  }

  deleteSelected() {
    if (!this.canvas) return;
    const activeObj = this.canvas.getActiveObject();
    if (activeObj) {
      const data = activeObj.customData;
      if (activeObj.labelObj) this.canvas.remove(activeObj.labelObj);
      this.canvas.remove(activeObj);
      this.canvas.discardActiveObject();
      if (this.onObjectRemoved) this.onObjectRemoved(data);
      this.canvas.requestRenderAll();
    }
  }

  discardActiveObject() {
    if (this.canvas) {
      this.canvas.discardActiveObject();
      this.canvas.requestRenderAll();
      if (this.onSelectionCleared) this.onSelectionCleared();
    }
  }

  handleSelection(e) {
    this.updateFocus();
    const activeObj = e.selected[0];
    if (activeObj && activeObj.customData && this.onSelectionUpdated) {
      this.onSelectionUpdated(activeObj.customData);
    }
  }
  handleSelectionCleared() {
    this.updateFocus();
    if (this.onSelectionCleared) this.onSelectionCleared();
  }

  // === 查找逻辑：使用 Loose Equality (==) 兼容字符串/数字 ===
  selectObject(type, id) {
    console.warn("Deprecated: Use selectObjectByUuid instead.");
    if (!this.canvas) return;
    const targets = this.canvas
      .getObjects()
      .filter(
        (o) =>
          o.customData && o.customData.type === type && o.customData.id == id
      );
    if (targets.length > 0) {
      // 优先选中 Body，否则选中第一个
      const body = targets.find((t) => t.customData.role === "Body");
      this.canvas.setActiveObject(body || targets[0]);
      this.canvas.requestRenderAll();
    }
  }

  // [新增] 核心修复：通过 UUID 选中对象，解决 ID 修改时的定位丢失问题
  selectObjectByUuid(uuid) {
    if (!this.canvas) return;
    const target = this.canvas
      .getObjects()
      .find((o) => o.customData && o.customData.uuid === uuid);
    if (target) {
      this.canvas.setActiveObject(target);
      // 滚动到视图中心 (可选优化体验)
      this.canvas.absolutePan({
        x: target.left - this.canvas.width / 2,
        y: target.top - this.canvas.height / 2,
      });
      this.canvas.requestRenderAll();
    }
  }

  // === 核心修复：基于 UUID 更新，彻底解决 ID 冲突/更新错乱问题 ===
  updateObjectByUuid(uuid, newId) {
    if (!this.canvas) return;

    // 使用 uuid 精确查找对象
    const rect = this.canvas
      .getObjects()
      .find((o) => o.customData && o.customData.uuid === uuid);

    if (rect) {
      // 1. 同步内部数据
      rect.customData.id = newId;

      // 2. 更新标签
      const type = rect.customData.type;
      const roleShort = rect.customData.role === "Body" ? "B" : "C";
      const labelText = ["Header", "Mask", "ContentArea"].includes(type)
        ? type
        : `${type} ${newId} (${roleShort})`;

      if (rect.labelObj) {
        rect.labelObj.set("text", labelText);
      }
      this.canvas.requestRenderAll();
    }
  }

  setObjectVisualState(obj, opacity, selectable) {
    const tool = this.tools.find((t) => t.type === obj.customData.type);
    const color = tool ? tool.color : "black";
    obj.set({
      selectable: selectable,
      evented: selectable,
      opacity: opacity,
      stroke: selectable ? color : "#999",
    });
    if (obj.labelObj) obj.labelObj.set("opacity", opacity);
  }
  updateFocus() {
    if (!this.currentToolType) return;
    const activeObj = this.canvas.getActiveObject();
    this.canvas.getObjects().forEach((obj) => {
      if (!obj.customData) return;
      if (obj.customData.type !== this.currentToolType) return;
      if (activeObj && obj === activeObj) {
        obj.set("opacity", 1.0);
        if (obj.labelObj) obj.labelObj.set("opacity", 1.0);
      } else {
        obj.set("opacity", 0.8);
        if (obj.labelObj) obj.labelObj.set("opacity", 0.8);
      }
    });
    this.canvas.requestRenderAll();
  }

  initTooltip() {
    this.tooltipEl = document.createElement("div");
    this.tooltipEl.className = "cursor-tooltip";
    document.body.appendChild(this.tooltipEl);
  }
  setPendingState(id, role) {
    this.pendingId = id;
    this.pendingRole = role;
    this.renderTooltipContent();
  }
  renderTooltipContent() {
    if (!this.tooltipEl || !this.currentToolType) return;
    const toolName = this.currentToolType;
    if (
      ["Header", "ContentArea", "Mask", "Title", "Author"].includes(toolName)
    ) {
      this.tooltipEl.innerHTML = `<div>${toolName}</div>`;
    } else {
      const roleTxt = this.pendingRole === "Body" ? "内容区域" : "标题文字";
      this.tooltipEl.innerHTML = `<div style="font-weight:bold">${toolName} #${this.pendingId}</div><div class="cursor-sub">待标注: ${roleTxt}</div>`;
    }
  }
  updateTooltipPosition(e) {
    if (!this.tooltipEl || !e) return;
    this.tooltipEl.style.left = e.clientX + "px";
    this.tooltipEl.style.top = e.clientY + "px";
  }
  showTooltip() {
    if (this.tooltipEl && this.currentToolType)
      this.tooltipEl.style.display = "block";
  }
  hideTooltip() {
    if (this.tooltipEl) this.tooltipEl.style.display = "none";
  }

  dispose() {
    if (this.canvas) {
      this.canvas.dispose();
      this.canvas = null;
    }
    if (this.tooltipEl) this.tooltipEl.remove();
    window.removeEventListener("resize", this._resizeHandler);
    window.removeEventListener("keydown", this._keyDownHandler);
    window.removeEventListener("keyup", this._keyUpHandler);
  }
  clear() {
    if (this.canvas) this.canvas.clear();
  }
}
