// The seven views: one type and one label map, imported by both the sidebar and the breadcrumb.
//
// route.js owns the hash vocabulary (which hash names which view); this owns the words a person
// reads. tests/nav.test.mjs pins the two together so a view cannot be added to one and not the
// other. Plain JS on purpose: node --test cannot load TypeScript.

/** @typedef {'audit' | 'inbox' | 'shared' | 'dashboard' | 'glossary' | 'runs' | 'settings'} View */

/** @type {Record<View, string>} */
export const VIEW_LABELS = {
  audit: 'Recent decisions',
  inbox: 'Review inbox',
  shared: 'Shared lines',
  dashboard: 'Dashboard',
  glossary: 'Glossary',
  runs: 'Runs',
  settings: 'Settings',
};
