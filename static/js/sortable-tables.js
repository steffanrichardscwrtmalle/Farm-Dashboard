/**
 * Click a column header to sort that table.
 * Tables that already sort themselves (th[data-sort]) are left alone.
 * Sort is reapplied when a script replaces the rows, so dynamic tables keep order.
 */
(function () {
  const STYLE_ID = "sortable-tables-style";
  const MONTHS = {
    jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
    jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
  };

  let raf = 0;
  const pending = new Set();

  function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      table thead th.col-sortable,
      table > tbody > tr:first-child > th.col-sortable,
      table > tr:first-child > th.col-sortable {
        cursor: pointer;
        user-select: none;
      }
      table thead th.col-sortable:hover,
      table > tbody > tr:first-child > th.col-sortable:hover,
      table > tr:first-child > th.col-sortable:hover {
        box-shadow: inset 0 -2px 0 currentColor;
      }
      table thead th.col-sortable.sorted-asc::after,
      table thead th.col-sortable.sorted-desc::after,
      table > tbody > tr:first-child > th.col-sortable.sorted-asc::after,
      table > tbody > tr:first-child > th.col-sortable.sorted-desc::after,
      table > tr:first-child > th.col-sortable.sorted-asc::after,
      table > tr:first-child > th.col-sortable.sorted-desc::after {
        font-size: 0.65em;
      }
      table thead th.col-sortable.sorted-asc::after,
      table > tbody > tr:first-child > th.col-sortable.sorted-asc::after,
      table > tr:first-child > th.col-sortable.sorted-asc::after { content: " \\25B2"; }
      table thead th.col-sortable.sorted-desc::after,
      table > tbody > tr:first-child > th.col-sortable.sorted-desc::after,
      table > tr:first-child > th.col-sortable.sorted-desc::after { content: " \\25BC"; }
    `;
    document.head.appendChild(style);
  }

  function ownsSort(table) {
    const rows = table.tHead ? table.tHead.rows : [];
    for (const row of rows) {
      for (const cell of row.cells) {
        if (cell.dataset && cell.dataset.sort) return true;
      }
    }
    return false;
  }

  function sortableTable(table) {
    return table && table.dataset.sortable !== "off" && !ownsSort(table);
  }

  function isHeaderRow(row) {
    if (!row) return false;
    if (row.parentElement && row.parentElement.tagName === "THEAD") return true;
    return row.cells.length > 0 && [...row.cells].every((cell) => cell.tagName === "TH");
  }

  function headerRows(table) {
    if (table.tHead && table.tHead.rows.length) return [...table.tHead.rows];
    const first = table.rows[0];
    return first && isHeaderRow(first) ? [first] : [];
  }

  function isControlHeader(th) {
    const text = (th.textContent || "").replace(/\s+/g, "");
    if (text) return false;
    return !!th.querySelector("input, button, select, textarea");
  }

  function columnIndex(cell) {
    let index = 0;
    for (const sibling of cell.parentElement.cells) {
      if (sibling === cell) return index;
      index += sibling.colSpan || 1;
    }
    return index;
  }

  function cellAt(row, colIndex) {
    let index = 0;
    for (const cell of row.cells) {
      const span = cell.colSpan || 1;
      if (colIndex < index + span) return cell;
      index += span;
    }
    return null;
  }

  function cellText(cell) {
    if (!cell) return "";
    if (cell.dataset && cell.dataset.sortValue != null && cell.dataset.sortValue !== "") {
      return cell.dataset.sortValue;
    }
    const clone = cell.cloneNode(true);
    clone.querySelectorAll("input, select, textarea, button").forEach((el) => el.remove());
    const text = (clone.textContent || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
    if (text) return text;
    const box = cell.querySelector("input[type='checkbox'], input[type='radio']");
    if (box) return box.checked ? "1" : "0";
    const field = cell.querySelector("select, input, textarea");
    if (!field) return "";
    if (field.tagName === "SELECT") {
      const selected = field.selectedOptions && field.selectedOptions[0];
      return selected ? selected.textContent.trim() : field.value;
    }
    return String(field.value || "");
  }

  function parseCell(text) {
    const raw = String(text || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
    if (!raw || raw === "—" || raw === "–" || raw === "-" || /^n\/?a$/i.test(raw)) {
      return { type: "empty" };
    }

    let numeric = raw.replace(/[£$€]/g, "").trim();
    let sign = 1;
    if (/^\(.*\)$/.test(numeric)) {
      sign = -1;
      numeric = numeric.slice(1, -1).trim();
    }
    numeric = numeric.replace(/%$/, "").trim();
    if (/^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$/.test(numeric) || /^[+-]?\d+(\.\d+)?$/.test(numeric)) {
      return { type: "number", value: sign * parseFloat(numeric.replace(/,/g, "")) };
    }

    const dmy = raw.match(/^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2}|\d{4})$/);
    if (dmy) {
      let year = Number(dmy[3]);
      if (year < 100) year += 2000;
      return { type: "date", value: Date.UTC(year, Number(dmy[2]) - 1, Number(dmy[1])) };
    }
    const iso = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (iso) {
      return { type: "date", value: Date.UTC(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3])) };
    }
    const dMon = raw.match(/^(\d{1,2})[\/\-.\s]([A-Za-z]{3,9})[\/\-.\s](\d{2}|\d{4})$/);
    if (dMon && MONTHS[dMon[2].slice(0, 3).toLowerCase()] != null) {
      let year = Number(dMon[3]);
      if (year < 100) year += 2000;
      return { type: "date", value: Date.UTC(year, MONTHS[dMon[2].slice(0, 3).toLowerCase()], Number(dMon[1])) };
    }
    const monY = raw.match(/^([A-Za-z]{3,9})[\/\-.\s](\d{2}|\d{4})$/);
    if (monY && MONTHS[monY[1].slice(0, 3).toLowerCase()] != null) {
      let year = Number(monY[2]);
      if (year < 100) year += 2000;
      return { type: "date", value: Date.UTC(year, MONTHS[monY[1].slice(0, 3).toLowerCase()], 1) };
    }

    return { type: "text", value: raw.toLowerCase() };
  }

  function compareParsed(a, b) {
    if (a.type === "empty" && b.type === "empty") return 0;
    if (a.type === "empty") return 1;
    if (b.type === "empty") return -1;
    if (a.type !== b.type) {
      const order = { number: 0, date: 1, text: 2 };
      return order[a.type] - order[b.type];
    }
    if (a.type === "text") return a.value.localeCompare(b.value, undefined, { numeric: true, sensitivity: "base" });
    if (a.value < b.value) return -1;
    if (a.value > b.value) return 1;
    return 0;
  }

  function pinPosition(row) {
    const pin = row.dataset.sortPin;
    if (pin === "top" || pin === "bottom") return pin;
    if (row.classList.contains("total-row") || row.classList.contains("totals-row")) return "bottom";
    return "";
  }

  function sortRows(rows, colIndex, dir) {
    const top = [];
    const mid = [];
    const bottom = [];
    rows.forEach((row) => {
      const pin = pinPosition(row);
      if (pin === "top") top.push(row);
      else if (pin === "bottom") bottom.push(row);
      else mid.push(row);
    });
    mid.sort((a, b) => {
      const av = parseCell(cellText(cellAt(a, colIndex)));
      const bv = parseCell(cellText(cellAt(b, colIndex)));
      const cmp = compareParsed(av, bv);
      if (av.type === "empty" || bv.type === "empty") return cmp;
      if (av.type !== bv.type) return cmp;
      return cmp * dir;
    });
    return top.concat(mid, bottom);
  }

  function sortTable(table) {
    const colIndex = Number(table.dataset.sortCol);
    const dir = Number(table.dataset.sortDir) || 1;
    if (!Number.isFinite(colIndex)) return;
    const bodies = table.tBodies.length ? [...table.tBodies] : [];
    bodies.forEach((tbody) => {
      const headers = [];
      const rest = [];
      [...tbody.rows].forEach((row) => (isHeaderRow(row) ? headers : rest).push(row));
      const ordered = headers.concat(sortRows(rest, colIndex, dir));
      const current = headers.concat(rest);
      if (ordered.every((row, index) => row === current[index])) return;
      const frag = document.createDocumentFragment();
      ordered.forEach((row) => frag.appendChild(row));
      tbody.appendChild(frag);
    });
  }

  function paintHeaders(table) {
    const colIndex = table.dataset.sortCol == null ? null : Number(table.dataset.sortCol);
    const dir = Number(table.dataset.sortDir) || 1;
    const activeRow = Number(table.dataset.sortRow || 0);
    headerRows(table).forEach((row, rowIndex) => {
      [...row.cells].forEach((th) => {
        if (th.colSpan > 1 || isControlHeader(th)) {
          th.classList.remove("col-sortable", "sorted-asc", "sorted-desc");
          th.removeAttribute("aria-sort");
          return;
        }
        th.classList.add("col-sortable");
        const active = colIndex != null && columnIndex(th) === colIndex && rowIndex === activeRow;
        th.classList.toggle("sorted-asc", active && dir === 1);
        th.classList.toggle("sorted-desc", active && dir === -1);
        if (active) th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
        else th.setAttribute("aria-sort", "none");
      });
    });
  }

  function applySort(table) {
    if (!table.isConnected || !sortableTable(table) || table.dataset.sortCol == null) {
      if (table.isConnected && sortableTable(table)) paintHeaders(table);
      return;
    }
    sortTable(table);
    paintHeaders(table);
  }

  function decorate(table) {
    if (!sortableTable(table)) return;
    paintHeaders(table);
  }

  function schedule(table, resort) {
    if (!table) return;
    table._sortableResort = table._sortableResort || resort;
    if (resort) table._sortableResort = true;
    pending.add(table);
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const tables = [...pending];
      pending.clear();
      tables.forEach((table) => {
        if (!table.isConnected) return;
        if (table._sortableResort && table.dataset.sortCol != null) applySort(table);
        else decorate(table);
        table._sortableResort = false;
      });
    });
  }

  document.addEventListener("click", (event) => {
    if (event.button !== 0 || event.defaultPrevented) return;
    const th = event.target.closest && event.target.closest("th");
    if (!th) return;
    if (event.target.closest("a, button, input, select, textarea, label")) return;
    const table = th.closest("table");
    if (!sortableTable(table)) return;
    const rows = headerRows(table);
    const rowIndex = rows.indexOf(th.parentElement);
    if (rowIndex < 0 || th.colSpan > 1 || isControlHeader(th)) return;
    const colIndex = columnIndex(th);
    const same = Number(table.dataset.sortCol) === colIndex && Number(table.dataset.sortRow || 0) === rowIndex;
    const dir = same ? -(Number(table.dataset.sortDir) || 1) : 1;
    table.dataset.sortCol = String(colIndex);
    table.dataset.sortDir = String(dir);
    table.dataset.sortRow = String(rowIndex);
    applySort(table);
  });

  const observer = new MutationObserver((records) => {
    records.forEach((record) => {
      if (record.type !== "childList") return;
      const target = record.target.nodeType === 1 ? record.target : record.target.parentElement;
      const table = target && target.closest ? target.closest("table") : null;
      if (table) {
        schedule(table, table.dataset.sortCol != null);
        return;
      }
      record.addedNodes.forEach((node) => {
        if (!node || node.nodeType !== 1) return;
        if (node.matches && node.matches("table")) schedule(node, false);
        if (node.querySelectorAll) node.querySelectorAll("table").forEach((found) => schedule(found, false));
      });
    });
  });

  function start() {
    ensureStyles();
    document.querySelectorAll("table").forEach((table) => decorate(table));
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
