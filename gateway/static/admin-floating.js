// Native top-layer panels escape table overflow. A dialog-owned panel remains in
// the dialog's focus scope, then enters the top layer above that dialog.
let sequence = 0;
const opened = new Map();
export function closeFloatingPanels() {
  for (const controller of [...opened.values()]) controller.close();
}
export function floatingPanel(trigger, panel, { onOpen, onClose } = {}) {
  panel.id ||= `admin-floating-${++sequence}`;
  panel.setAttribute("popover", "manual");
  panel.classList.add("floating-panel");
  trigger.setAttribute("aria-controls", panel.id);
  trigger.setAttribute("aria-expanded", "false");
  let scope, abort, resize, mutation, frame;
  const isOpen = () => panel.isConnected && panel.matches(":popover-open");
  function close(restoreFocus = false) {
    if (!abort) return;
    abort.abort();
    abort = null;
    resize?.disconnect();
    mutation?.disconnect();
    cancelAnimationFrame(frame);
    if (isOpen()) panel.hidePopover();
    panel.remove();
    if (opened.get(scope) === controller) opened.delete(scope);
    trigger.setAttribute("aria-expanded", "false");
    onClose?.();
    if (restoreFocus && trigger.isConnected)
      trigger.focus({ preventScroll: true });
  }
  function position() {
    if (!isOpen()) return;
    const r = trigger.getBoundingClientRect(),
      vw = document.documentElement.clientWidth,
      vh = window.innerHeight;
    if (
      !trigger.isConnected ||
      (scope instanceof HTMLDialogElement && !scope.open) ||
      r.bottom < 0 ||
      r.top > vh ||
      r.right < 0 ||
      r.left > vw
    ) {
      close();
      return;
    }
    // Stop displaying a panel whose trigger has scrolled out of a clipping parent.
    for (
      let p = trigger.parentElement;
      p && p !== document.body;
      p = p.parentElement
    ) {
      const style = getComputedStyle(p),
        b = p.getBoundingClientRect();
      if (
        (/(auto|scroll|hidden)/.test(style.overflowY) &&
          (r.bottom <= b.top || r.top >= b.bottom)) ||
        (/(auto|scroll|hidden)/.test(style.overflowX) &&
          (r.right <= b.left || r.left >= b.right))
      ) {
        close();
        return;
      }
    }
    const gap = 6,
      edge = 8,
      below = vh - r.bottom - gap - edge,
      above = r.top - gap - edge;
    panel.style.maxHeight = `${Math.max(48, vh - edge * 2)}px`;
    const natural = panel.getBoundingClientRect().height,
      up = natural > below && above > below;
    panel.style.maxHeight = `${Math.max(48, up ? above : below)}px`;
    const box = panel.getBoundingClientRect();
    panel.style.left = `${Math.max(edge, Math.min(r.right - box.width, vw - box.width - edge))}px`;
    panel.style.top = `${Math.max(edge, Math.min(up ? r.top - gap - box.height : r.bottom + gap, vh - box.height - edge))}px`;
    panel.dataset.placement = up ? "top" : "bottom";
  }
  function schedule() {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(position);
  }
  function open() {
    if (isOpen() || !trigger.isConnected) return;
    scope = trigger.closest("dialog[open]") || document.body;
    opened.get(scope)?.close();
    scope.append(panel);
    abort = new AbortController();
    const options = { capture: true, signal: abort.signal };
    opened.set(scope, controller);
    panel.showPopover();
    trigger.setAttribute("aria-expanded", "true");
    resize = new ResizeObserver(schedule);
    resize.observe(trigger);
    resize.observe(panel);
    resize.observe(document.querySelector("main"));
    mutation = new MutationObserver(() => {
      if (
        !trigger.isConnected ||
        (scope instanceof HTMLDialogElement && !scope.open)
      )
        close();
      else schedule();
    });
    mutation.observe(document.body, { childList: true, subtree: true });
    document.addEventListener(
      "pointerdown",
      (e) => {
        if (!panel.contains(e.target) && !trigger.contains(e.target)) close();
      },
      options,
    );
    document.addEventListener(
      "focusin",
      (e) => {
        if (!panel.contains(e.target) && !trigger.contains(e.target)) close();
      },
      options,
    );
    document.addEventListener(
      "keydown",
      (e) => {
        if (e.key === "Escape" && !e.isComposing) {
          e.preventDefault();
          e.stopImmediatePropagation();
          close(true);
        }
      },
      options,
    );
    document.addEventListener("scroll", schedule, options);
    window.addEventListener("resize", schedule, { signal: abort.signal });
    scope.addEventListener("close", () => close(), { signal: abort.signal });
    position();
    if (isOpen()) onOpen?.();
  }
  const controller = { open, close, isOpen, position: schedule };
  return controller;
}
export function actionMenu(...items) {
  const wrap = document.createElement("span");
  wrap.className = "more-actions";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "secondary menu-trigger";
  trigger.textContent = "更多";
  trigger.setAttribute("aria-haspopup", "menu");
  const panel = document.createElement("div");
  panel.className = "action-menu";
  panel.setAttribute("role", "menu");
  panel.setAttribute("aria-label", "更多操作");
  for (const item of items) {
    item.setAttribute("role", "menuitem");
    item.tabIndex = -1;
    panel.append(item);
  }
  const controller = floatingPanel(trigger, panel);
  const enabled = () => items.filter((i) => !i.disabled && !i.hidden);
  function focusItem(index) {
    const list = enabled();
    list[(index + list.length) % list.length]?.focus();
  }
  trigger.addEventListener("click", () => {
    if (controller.isOpen()) controller.close(true);
    else {
      controller.open();
      focusItem(0);
    }
  });
  trigger.addEventListener("keydown", (e) => {
    if (["ArrowDown", "ArrowUp"].includes(e.key)) {
      e.preventDefault();
      controller.open();
      focusItem(e.key === "ArrowDown" ? 0 : enabled().length - 1);
    }
  });
  panel.addEventListener("keydown", (e) => {
    const list = enabled(),
      index = list.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      focusItem(
        e.key === "Home"
          ? 0
          : e.key === "End"
            ? list.length - 1
            : index + (e.key === "ArrowDown" ? 1 : -1),
      );
    } else if (e.key === "Tab") {
      // Return to the trigger before the browser advances its ordinary tab order.
      controller.close(true);
    } else if (
      e.key.length === 1 &&
      !e.ctrlKey &&
      !e.metaKey &&
      e.key !== " "
    ) {
      const match = [
        ...list.slice(index + 1),
        ...list.slice(0, index + 1),
      ].find((i) =>
        i.textContent.toLowerCase().startsWith(e.key.toLowerCase()),
      );
      match?.focus();
    }
  });
  // Close before the existing action runs, retaining its confirmation and error flow.
  panel.addEventListener(
    "click",
    (e) => {
      if (e.target.closest('[role="menuitem"]')) controller.close(true);
    },
    true,
  );
  wrap.append(trigger);
  return wrap;
}
