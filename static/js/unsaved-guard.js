/**
 * Warn before leaving a page or in-app link when a table still has unsaved edits.
 *
 * Usage:
 *   const guard = bindUnsavedGuard({
 *     isDirty: () => boolean,
 *     flush: async () => boolean,  // try autosave; true if it is safe to leave
 *     message: 'optional confirm text',
 *   });
 *   if (!(await guard.confirmLeave())) return;
 */
(function (global) {
  const DEFAULT_MESSAGE = 'This table still has unsaved changes. Continue anyway?';

  function bindUnsavedGuard(options) {
    const isDirty = options.isDirty || (() => false);
    const flush = options.flush || (async () => true);
    const message = options.message || DEFAULT_MESSAGE;
    let allowingLeave = false;

    async function confirmLeave() {
      if (allowingLeave) return true;
      let flushed = true;
      try {
        flushed = await flush();
      } catch (_) {
        flushed = false;
      }
      if (flushed && !isDirty()) return true;
      return window.confirm(message);
    }

    window.addEventListener('beforeunload', (e) => {
      if (allowingLeave) return;
      if (!isDirty()) return;
      e.preventDefault();
      e.returnValue = '';
    });

    document.addEventListener('click', async (e) => {
      if (allowingLeave) return;
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const link = e.target.closest?.('a[href]');
      if (!link || link.hasAttribute('download')) return;
      if (link.target && link.target !== '_self') return;
      const href = link.getAttribute('href');
      if (!href || href.startsWith('#') || href.toLowerCase().startsWith('javascript:')) return;
      let url;
      try {
        url = new URL(link.href, window.location.href);
      } catch (_) {
        return;
      }
      if (url.origin !== window.location.origin) return;
      if (
        url.pathname === window.location.pathname
        && url.search === window.location.search
      ) return;
      if (!isDirty()) return;

      e.preventDefault();
      e.stopPropagation();
      const ok = await confirmLeave();
      if (!ok) return;
      allowingLeave = true;
      window.location.href = link.href;
    }, true);

    return { confirmLeave };
  }

  global.bindUnsavedGuard = bindUnsavedGuard;
})(window);
