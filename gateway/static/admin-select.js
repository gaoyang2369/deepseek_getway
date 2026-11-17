import { floatingPanel } from "./floating.js";

// Searchable single selection. The hidden input stays in its form while the
// floating list shares the same overflow/dialog handling as action menus.
export function searchableSelect(label, name, value, initialOptions) {
  const root = document.createElement("div");
  root.className = "search-select";
  const caption = document.createElement("span");
  caption.textContent = label;
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = name;
  input.value = value;
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "select-trigger secondary";
  trigger.setAttribute("role", "combobox");
  trigger.setAttribute("aria-label", label);
  trigger.setAttribute("aria-haspopup", "listbox");
  const panel = document.createElement("div");
  panel.className = "select-panel";
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = label === "成员" ? "搜索姓名或学号" : "搜索模型名称";
  search.setAttribute("aria-label", search.placeholder);
  const list = document.createElement("div");
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", label);
  panel.append(search, list);
  let options = initialOptions;
  function render() {
    list.replaceChildren();
    const query = search.value.trim().toLocaleLowerCase();
    for (const [id, text] of options) {
      if (id && !text.toLocaleLowerCase().includes(query)) continue;
      const option = document.createElement("button");
      option.type = "button";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(id === input.value));
      option.tabIndex = -1;
      option.textContent = text;
      option.onclick = () => {
        input.value = id;
        sync();
        controller.close(true);
        input.dispatchEvent(new Event("change", { bubbles: true }));
      };
      list.append(option);
    }
    if (list.childElementCount === 1 && query) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "没有匹配项，可修改搜索词或选择全部。";
      list.append(empty);
    }
    controller.position();
  }
  function sync() {
    trigger.textContent =
      options.find(([id]) => id === input.value)?.[1] || options[0][1];
  }
  const controller = floatingPanel(trigger, panel, {
    onOpen() {
      search.value = "";
      render();
      search.focus();
    },
  });
  list.id = panel.id + "-options";
  trigger.setAttribute("aria-controls", list.id);
  trigger.onclick = () =>
    controller.isOpen() ? controller.close(true) : controller.open();
  trigger.onkeydown = (event) => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      controller.open();
    }
  };
  search.oninput = render;
  panel.onkeydown = (event) => {
    if (event.isComposing) return;
    const items = [...list.querySelectorAll('[role="option"]')];
    const index = items.indexOf(document.activeElement);
    if (
      ["ArrowDown", "ArrowUp"].includes(event.key) ||
      (index >= 0 && ["Home", "End"].includes(event.key))
    ) {
      event.preventDefault();
      const next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? items.length - 1
            : index < 0
              ? event.key === "ArrowUp"
                ? items.length - 1
                : 0
              : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
                items.length;
      items[next]?.focus();
    } else if (event.key === "Enter" && event.target === search) {
      event.preventDefault();
      items.find((item) => item.textContent !== options[0][1])?.click();
    } else if (event.key === "Tab") controller.close(true);
  };
  root.append(caption, input, trigger);
  sync();
  return {
    element: root,
    setOptions(next) {
      options = next;
      const cleared = !options.some(([id]) => id === input.value);
      if (cleared) input.value = "";
      sync();
      if (controller.isOpen()) render();
      return cleared;
    },
  };
}
