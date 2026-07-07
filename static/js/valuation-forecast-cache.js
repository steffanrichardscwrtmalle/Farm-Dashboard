/**
 * Session-scoped cache + background prefetch for Stock Forecasts page APIs.
 * Lets the page render immediately when navigating from other benchmarking pages.
 */
(function () {
  const VALUATION_STORAGE_KEY = "farm-dashboard:valuation-forecast";
  const STOCK_STORAGE_KEY = "farm-dashboard:stock-forecast";
  const FY_KEY = "farm-dashboard:valuation-forecast-fy";
  const DEFAULT_FARMS = ["CM", "GAD"];
  const DEFAULT_STOCK_GROUP = "cows";

  let valuationInflight = null;
  let stockInflight = null;

  function farms() {
    if (Array.isArray(window.__HERD_FARMS__) && window.__HERD_FARMS__.length) {
      return window.__HERD_FARMS__;
    }
    return DEFAULT_FARMS;
  }

  function stockCacheKey(fiscalYear, stockGroup, farmList) {
    const fy = fiscalYear != null ? String(fiscalYear) : "";
    const group = stockGroup || DEFAULT_STOCK_GROUP;
    const list = farmList && farmList.length ? farmList : farms();
    return `${fy}|${group}|${[...list].sort().join(",")}`;
  }

  function readJson(key) {
    try {
      const raw = sessionStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function writeJson(key, value) {
    try {
      sessionStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage full or disabled */
    }
  }

  function rememberFiscalYear(fiscalYear) {
    if (fiscalYear == null) return;
    try {
      sessionStorage.setItem(FY_KEY, String(fiscalYear));
    } catch {
      /* ignore */
    }
  }

  function buildValuationUrl(fiscalYear) {
    const params = new URLSearchParams();
    farms().forEach(farm => params.append("farm", farm));
    if (fiscalYear) {
      params.set("fiscal_year", String(fiscalYear));
    }
    return `/api/benchmarking/stock-valuation-forecasts?${params}`;
  }

  function buildStockUrl(fiscalYear, stockGroup, farmList) {
    const params = new URLSearchParams();
    const list = farmList && farmList.length ? farmList : farms();
    list.forEach(farm => params.append("farm", farm));
    params.set("stock_group", stockGroup || DEFAULT_STOCK_GROUP);
    if (fiscalYear) {
      params.set("fiscal_year", String(fiscalYear));
    }
    return `/api/benchmarking/stock-forecasts?${params}`;
  }

  const ValuationForecastCache = {
    get(fiscalYear) {
      const entry = readJson(VALUATION_STORAGE_KEY);
      if (!entry || String(entry.fiscalYear) !== String(fiscalYear)) {
        return null;
      }
      return entry.data;
    },

    set(fiscalYear, data) {
      if (!data) return;
      const year = fiscalYear != null ? fiscalYear : data.selected_fiscal_year;
      if (year == null) return;
      rememberFiscalYear(year);
      writeJson(VALUATION_STORAGE_KEY, { fiscalYear: String(year), data });
    },

    getInflight() {
      return valuationInflight;
    },

    prefetch(fiscalYear) {
      const fy =
        fiscalYear != null ? fiscalYear : sessionStorage.getItem(FY_KEY) || undefined;
      const cached = ValuationForecastCache.get(fy || readJson(VALUATION_STORAGE_KEY)?.fiscalYear);
      if (cached && fy != null && String(cached.selected_fiscal_year) === String(fy)) {
        return Promise.resolve(cached);
      }
      if (valuationInflight) {
        return valuationInflight;
      }

      valuationInflight = fetch(buildValuationUrl(fy))
        .then(response => {
          if (!response.ok) {
            throw new Error("prefetch failed");
          }
          return response.json();
        })
        .then(data => {
          ValuationForecastCache.set(data.selected_fiscal_year, data);
          return data;
        })
        .catch(() => null)
        .finally(() => {
          valuationInflight = null;
        });

      return valuationInflight;
    },
  };

  const StockForecastCache = {
    get(fiscalYear, stockGroup, farmList) {
      const key = stockCacheKey(fiscalYear, stockGroup, farmList);
      const store = readJson(STOCK_STORAGE_KEY) || {};
      return store[key] || null;
    },

    set(fiscalYear, stockGroup, farmList, data) {
      if (!data) return;
      const year = fiscalYear != null ? fiscalYear : data.selected_fiscal_year;
      if (year == null) return;
      rememberFiscalYear(year);
      const key = stockCacheKey(year, stockGroup || data.stock_group, farmList);
      const store = readJson(STOCK_STORAGE_KEY) || {};
      store[key] = data;
      writeJson(STOCK_STORAGE_KEY, store);
    },

    getInflight() {
      return stockInflight;
    },

    prefetch(fiscalYear, stockGroup, farmList) {
      const fy =
        fiscalYear != null ? fiscalYear : sessionStorage.getItem(FY_KEY) || undefined;
      const group = stockGroup || DEFAULT_STOCK_GROUP;
      const list = farmList && farmList.length ? farmList : farms();
      const cached = StockForecastCache.get(fy, group, list);
      if (
        cached
        && fy != null
        && String(cached.selected_fiscal_year) === String(fy)
        && cached.stock_group === group
      ) {
        return Promise.resolve(cached);
      }
      if (stockInflight) {
        return stockInflight;
      }

      stockInflight = fetch(buildStockUrl(fy, group, list))
        .then(response => {
          if (!response.ok) {
            throw new Error("prefetch failed");
          }
          return response.json();
        })
        .then(data => {
          StockForecastCache.set(data.selected_fiscal_year, group, list, data);
          return data;
        })
        .catch(() => null)
        .finally(() => {
          stockInflight = null;
        });

      return stockInflight;
    },
  };

  function prefetchPage(fiscalYear) {
    const fy =
      fiscalYear != null ? fiscalYear : sessionStorage.getItem(FY_KEY) || undefined;
    return Promise.all([
      ValuationForecastCache.prefetch(fy),
      StockForecastCache.prefetch(fy, DEFAULT_STOCK_GROUP, farms()),
    ]);
  }

  function schedulePrefetch(delayMs) {
    const delay = delayMs == null ? 800 : delayMs;
    const run = () => prefetchPage().catch(() => {});

    if (typeof requestIdleCallback === "function") {
      requestIdleCallback(run, { timeout: delay + 3000 });
    } else {
      window.setTimeout(run, delay);
    }
  }

  function bindLinkPrefetch() {
    const link = document.querySelector('a[href="/benchmarking/stock-forecasts"]');
    if (!link || link.dataset.stockPagePrefetchBound) {
      return;
    }
    link.dataset.stockPagePrefetchBound = "1";
    link.addEventListener("mouseenter", () => prefetchPage().catch(() => {}));
    link.addEventListener("focus", () => prefetchPage().catch(() => {}));
  }

  function init() {
    bindLinkPrefetch();
    if (window.location.pathname.startsWith("/benchmarking/stock-forecasts")) {
      return;
    }
    schedulePrefetch();
  }

  window.ValuationForecastCache = ValuationForecastCache;
  window.StockForecastCache = StockForecastCache;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
