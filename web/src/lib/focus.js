// Focus return for surfaces that unmount when they close (the dialogs and the account menu).
//
// A dialog opened with showModal() takes focus; when the component unmounts, focus falls to <body>,
// so a keyboard user restarts from the top of the page instead of where they were. The opener
// captures the active element just before opening and gives it back on close.
//
// One surface at a time: every caller captures immediately before it opens.

/** @type {HTMLElement | null} */
let returnTo = null;

/** Remember the element focus should go back to. Call before opening the surface. */
export function rememberFocus() {
  returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
}

/** Put focus back on the remembered element if it is still in the page. Safe to call twice. */
export function restoreFocus() {
  const el = returnTo;
  returnTo = null;
  if (el?.isConnected) el.focus();
}
