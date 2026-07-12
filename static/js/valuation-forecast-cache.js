/**
 * Session-scoped cache + background prefetch for Stock Forecasts page.
 * Uses a single combined API to avoid parallel heavy requests (OOM on small instances).
 */
(function () {
  const PAGE_STORAGE_KEY = "farm-dashboard:stock-forecasts-page";
  const FY_KEY = "farm-dashboard:valuation-forecast-fy";
  const DEFAULT_FARMS = ["CM", "GAD"];
  const DEFAULT_STOCK_GROUP = "cows";

  let pageInflight = null;

  function farms() {
    if (Array.isArray(window.__HERD_FARMS__) && window.__HERD_FARMS__.length) {
      return window.__HERD_FARMS__;
    }
    return DEFAULT_FARMS;
  }

  function pageCacheKey(fiscalYear, stockGroup, farmList) {
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

  function buildPageUrl(fiscalYear, stockGroup, farmList) {
    const params = new URLSearchParams();
    const list = farmList && farmList.length ? farmList : farms();
    list.forEach(farm => params.append("farm", farm));
    params.set("stock_group", stockGroup || DEFAULT_STOCK_GROUP);
    if (fiscalYear) {
      params.set("fiscal_year", String(fiscalYear));
    }
    return `/api/benchmarking/stock-forecasts-page?${params}`;
  }

  function setPageCache(fiscalYear, stockGroup, farmList, pageData) {
    if (!pageData) return;
    const stock = pageData.stock_forecasts;
    const year = fiscalYear != null ? fiscalYear : stock?.selected_fiscal_year;
    if (year == null) return;
    rememberFiscalYear(year);
    const key = pageCacheKey(year, stockGroup || stock?.stock_group, farmList);
    const store = readJson(PAGE_STORAGE_KEY) || {};
    store[key] = pageData;
    writeJson(PAGE_STORAGE_KEY, store);
  }

  function getPageCache(fiscalYear, stockGroup, farmList) {
    const key = pageCacheKey(fiscalYear, stockGroup, farmList);
    const store = readJson(PAGE_STORAGE_KEY) || {};
    return store[key] || null;
  }

  function clearPageCache() {
    try {
      sessionStorage.removeItem(PAGE_STORAGE_KEY);
    } catch {
      /* ignore */
    }
    pageInflight = null;
  }

  const StockForecastsPageCache = {
    get(fiscalYear, stockGroup, farmList) {
      return getPageCache(fiscalYear, stockGroup, farmList);
    },

    set(fiscalYear, stockGroup, farmList, pageData) {
      setPageCache(fiscalYear, stockGroup, farmList, pageData);
    },

    clear() {
      clearPageCache();
    },

    getInflight() {
      return pageInflight;
    },

    prefetch(fiscalYear, stockGroup, farmList) {
      const fy =
        fiscalYear != null ? fiscalYear : sessionStorage.getItem(FY_KEY) || undefined;
      const group = stockGroup || DEFAULT_STOCK_GROUP;
      const list = farmList && farmList.length ? farmList : farms();
      const cached = getPageCache(fy, group, list);
      if (
        cached
        && fy != null
        && String(cached.stock_forecasts?.selected_fiscal_year) === String(fy)
        && cached.stock_forecasts?.stock_group === group
      ) {
        return Promise.resolve(cached);
      }
      if (pageInflight) {
        return pageInflight;
      }

      pageInflight = fetch(buildPageUrl(fy, group, list))
        .then(response => {
          if (!response.ok) {
            throw new Error("prefetch failed");
          }
          return response.json();
        })
        .then(pageData => {
          setPageCache(fy, group, list, pageData);
          return pageData;
        })
        .catch(() => null)
        .finally(() => {
          pageInflight = null;
        });

      return pageInflight;
    },
  };

  const ValuationForecastCache = {
    get(fiscalYear) {
      const page = getPageCache(
        fiscalYear,
        DEFAULT_STOCK_GROUP,
        farms()
      );
      return page?.valuation_forecasts || null;
    },

    set(fiscalYear, data) {
      if (!data) return;
      const page = getPageCache(fiscalYear, DEFAULT_STOCK_GROUP, farms()) || {};
      page.valuation_forecasts = data;
      if (!page.stock_forecasts) {
        page.stock_forecasts = { selected_fiscal_year: fiscalYear };
      }
      setPageCache(fiscalYear, DEFAULT_STOCK_GROUP, farms(), page);
    },

    getInflight() {
      const pending = pageInflight;
      if (!pending) return null;
      return pending.then(page => page?.valuation_forecasts || null);
    },

    prefetch(fiscalYear) {
      return StockForecastsPageCache.prefetch(
        fiscalYear,
        DEFAULT_STOCK_GROUP,
        farms()
      ).then(page => page?.valuation_forecasts || null);
    },
  };

  const StockForecastCache = {
    get(fiscalYear, stockGroup, farmList) {
      return getPageCache(fiscalYear, stockGroup, farmList)?.stock_forecasts || null;
    },

    set(fiscalYear, stockGroup, farmList, data) {
      if (!data) return;
      const page = getPageCache(fiscalYear, stockGroup, farmList) || {};
      page.stock_forecasts = data;
      setPageCache(fiscalYear, stockGroup, farmList, page);
    },

    getInflight() {
      const pending = pageInflight;
      if (!pending) return null;
      return pending.then(page => page?.stock_forecasts || null);
    },

    prefetch(fiscalYear, stockGroup, farmList) {
      return StockForecastsPageCache.prefetch(
        fiscalYear,
        stockGroup,
        farmList
      ).then(page => page?.stock_forecasts || null);
    },
  };

  function prefetchPage(fiscalYear) {
    const fy =
      fiscalYear != null ? fiscalYear : sessionStorage.getItem(FY_KEY) || undefined;
    return StockForecastsPageCache.prefetch(fy, DEFAULT_STOCK_GROUP, farms());
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
  }

  window.StockForecastsPageCache = StockForecastsPageCache;
  window.ValuationForecastCache = ValuationForecastCache;
  window.StockForecastCache = StockForecastCache;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
