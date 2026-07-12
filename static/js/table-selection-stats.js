/**
 * Excel-like rectangular cell selection with Sum / Average / Count.
 *
 * Usage:
 *   enableTableSelectionStats(tableElement, { cellSelector: 'td.month-col' });
 */
(function (global) {
  const STYLE_ID = "table-selection-stats-style";
  const BOX_ID = "table-selection-stats-box";
  const controllers = new Set();

  function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      table[data-selection-stats-bound="1"] td.month-col {
        user-select: none;
        -webkit-user-select: none;
        -webkit-user-drag: none;
      }
      table[data-selection-stats-bound="1"] td.month-col input {
        -webkit-user-drag: none;
      }
      table[data-selection-stats-bound="1"].selection-dragging td.month-col input {
        user-select: none !important;
        -webkit-user-select: none !important;
        pointer-events: none;
      }
      td.cell-selected {
        outline: 2px solid #2563eb !important;
        outline-offset: -2px;
        background: rgba(37, 99, 235, 0.16) !important;
      }
      .table-selection-stats {
        position: fixed;
        right: 1rem;
        bottom: 1rem;
        z-index: 60;
        display: none;
        gap: 0.85rem;
        align-items: baseline;
        padding: 0.55rem 0.85rem;
        background: #0f172a;
        color: #f8fafc;
        border-radius: 8px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.35);
        font-size: 0.82rem;
        line-height: 1.3;
        pointer-events: none;
      }
      .table-selection-stats.is-visible { display: inline-flex; }
      .table-selection-stats .stat-label {
        color: #94a3b8;
        margin-right: 0.25rem;
      }
      .table-selection-stats .stat-value {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
      }
      .grid-table.selection-dragging,
      .grid-table.selection-dragging * {
        user-select: none !important;
        -webkit-user-select: none !important;
        cursor: cell;
      }
    `;
    document.head.appendChild(style);
  }

  function ensureBox() {
    let box = document.getElementById(BOX_ID);
    if (box) return box;
    box = document.createElement("div");
    box.id = BOX_ID;
    box.className = "table-selection-stats";
    box.setAttribute("aria-live", "polite");
    document.body.appendChild(box);
    return box;
  }

  function parseNumericCell(td) {
    const input = td.querySelector("input");
    const raw = input ? input.value : td.textContent;
    const text = String(raw ?? "")
      .replace(/[£,\s]/g, "")
      .replace(/[−–]/g, "-")
      .trim();
    if (!text || text === "—" || text === "-" || text === "–") return null;
    const num = Number(text);
    return Number.isFinite(num) ? num : null;
  }

  function cellLooksLikeCurrency(td) {
    const input = td.querySelector("input");
    if (input && input.dataset.currency === "1") return true;
    if (input && input.dataset.currency === "0") return false;
    const raw = input ? input.value : td.textContent;
    return String(raw ?? "").includes("£");
  }

  function formatNumber(value, { currency = false, digits = 2 } = {}) {
    const opts = {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    };
    const body = Number(value).toLocaleString("en-GB", opts);
    return currency ? `£${body}` : body;
  }

  function indexCells(table, cellSelector) {
    const rows = [];
    table.querySelectorAll("tbody tr").forEach((tr) => {
      const cells = Array.from(tr.querySelectorAll(cellSelector));
      if (cells.length) rows.push(cells);
    });
    return rows;
  }

  function findCellCoords(matrix, cell) {
    for (let r = 0; r < matrix.length; r += 1) {
      const c = matrix[r].indexOf(cell);
      if (c >= 0) return { r, c };
    }
    return null;
  }

  function clearAllSelectionsExcept(except) {
    controllers.forEach((ctrl) => {
      if (ctrl !== except) ctrl.clear();
    });
  }

  function enableTableSelectionStats(table, options = {}) {
    if (!table) return null;
    if (table.dataset.selectionStatsBound === "1") {
      return table._selectionStatsController || null;
    }

    ensureStyles();
    const box = ensureBox();
    const cellSelector = options.cellSelector || "td.month-col";
    const parseValue = options.parseValue || parseNumericCell;

    let anchor = null;
    let selected = [];
    let dragging = false;
    let dragMoved = false;
    let startX = 0;
    let startY = 0;

    function hideBoxIfIdle() {
      const anySelected = Array.from(controllers).some((c) => c.hasSelection());
      if (!anySelected) {
        box.classList.remove("is-visible");
        box.innerHTML = "";
      }
    }

    function paint(cells) {
      table.querySelectorAll(`${cellSelector}.cell-selected`).forEach((td) => {
        td.classList.remove("cell-selected");
      });
      cells.forEach((td) => td.classList.add("cell-selected"));
    }

    function updateStats(cells) {
      selected = cells;
      paint(cells);
      if (!cells.length) {
        hideBoxIfIdle();
        return;
      }

      const values = [];
      let currencyVotes = 0;
      cells.forEach((td) => {
        const value = parseValue(td);
        if (value == null) return;
        values.push(value);
        if (cellLooksLikeCurrency(td)) currencyVotes += 1;
      });

      if (!values.length) {
        box.innerHTML = `
          <span><span class="stat-label">Count</span><span class="stat-value">0</span></span>
          <span><span class="stat-label">Sum</span><span class="stat-value">—</span></span>
          <span><span class="stat-label">Average</span><span class="stat-value">—</span></span>
        `;
        box.classList.add("is-visible");
        return;
      }

      const count = values.length;
      const sum = values.reduce((acc, n) => acc + n, 0);
      const avg = sum / count;
      const asCurrency = currencyVotes >= Math.ceil(count / 2);
      box.innerHTML = `
        <span><span class="stat-label">Count</span><span class="stat-value">${count}</span></span>
        <span><span class="stat-label">Sum</span><span class="stat-value">${formatNumber(sum, { currency: asCurrency })}</span></span>
        <span><span class="stat-label">Average</span><span class="stat-value">${formatNumber(avg, { currency: asCurrency })}</span></span>
      `;
      box.classList.add("is-visible");
    }

    function selectRange(fromCell, toCell) {
      const matrix = indexCells(table, cellSelector);
      const a = findCellCoords(matrix, fromCell);
      const b = findCellCoords(matrix, toCell);
      if (!a || !b) {
        updateStats(fromCell ? [fromCell] : []);
        return;
      }
      const r0 = Math.min(a.r, b.r);
      const r1 = Math.max(a.r, b.r);
      const c0 = Math.min(a.c, b.c);
      const c1 = Math.max(a.c, b.c);
      const cells = [];
      for (let r = r0; r <= r1; r += 1) {
        for (let c = c0; c <= c1; c += 1) {
          const cell = matrix[r]?.[c];
          if (cell) cells.push(cell);
        }
      }
      updateStats(cells);
    }

    function clear() {
      anchor = null;
      dragging = false;
      dragMoved = false;
      table.classList.remove("selection-dragging");
      updateStats([]);
    }

    function cellFromEvent(event) {
      const target = event.target;
      if (!(target instanceof Element)) return null;
      const cell = target.closest(cellSelector);
      if (!cell || !table.contains(cell)) return null;
      return cell;
    }

    function cellFromPoint(clientX, clientY) {
      const el = document.elementFromPoint(clientX, clientY);
      if (!(el instanceof Element)) return null;
      const cell = el.closest(cellSelector);
      if (!cell || !table.contains(cell)) return null;
      return cell;
    }

    function onMouseDown(event) {
      if (event.button !== 0) return;
      const cell = cellFromEvent(event);
      if (!cell) return;

      // Stop native text / input content dragging.
      event.preventDefault();
      clearAllSelectionsExcept(controller);
      dragging = true;
      dragMoved = false;
      startX = event.clientX;
      startY = event.clientY;
      anchor = cell;
      table.classList.add("selection-dragging");
      selectRange(anchor, anchor);

      const active = document.activeElement;
      if (active && table.contains(active) && active.matches?.("input")) {
        active.blur();
      }
      const sel = global.getSelection?.();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
    }

    function onMouseMove(event) {
      if (!dragging || !anchor) return;
      event.preventDefault();
      const dx = Math.abs(event.clientX - startX);
      const dy = Math.abs(event.clientY - startY);
      if (dx > 3 || dy > 3) dragMoved = true;

      const cell = cellFromPoint(event.clientX, event.clientY);
      if (cell) selectRange(anchor, cell);

      const sel = global.getSelection?.();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
    }

    function onMouseUp() {
      if (!dragging) return;
      dragging = false;
      table.classList.remove("selection-dragging");

      // Single click on an editable amount → focus for typing.
      if (!dragMoved && anchor) {
        const input = anchor.querySelector("input.amount-input:not([disabled])");
        if (input) {
          input.focus();
          input.select();
        }
      }
    }

    function onDragStart(event) {
      if (cellFromEvent(event)) {
        event.preventDefault();
      }
    }

    function onSelectStart(event) {
      if (dragging || cellFromEvent(event)) {
        event.preventDefault();
      }
    }

    function onKeyDown(event) {
      if (event.key === "Escape" && selected.length) {
        clear();
      }
    }

    function onDocumentMouseDown(event) {
      if (!(event.target instanceof Element)) return;
      if (table.contains(event.target)) return;
      if (event.target.closest?.("#" + BOX_ID)) return;
      if (event.target.closest?.("table[data-selection-stats-bound='1']")) return;
      clear();
    }

    const controller = {
      clear,
      hasSelection: () => selected.length > 0,
      refresh: () => {
        if (!selected.length) return;
        const stillThere = selected.filter((td) => table.contains(td));
        updateStats(stillThere);
      },
    };

    table.dataset.selectionStatsBound = "1";
    table._selectionStatsController = controller;
    controllers.add(controller);

    table.addEventListener("mousedown", onMouseDown);
    table.addEventListener("dragstart", onDragStart);
    table.addEventListener("selectstart", onSelectStart);
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onDocumentMouseDown, true);

    const tbody = table.tBodies[0];
    if (tbody) {
      const observer = new MutationObserver(() => {
        if (selected.length) clear();
      });
      observer.observe(tbody, { childList: true });
      controller._observer = observer;
    }

    return controller;
  }

  global.enableTableSelectionStats = enableTableSelectionStats;
})(window);
