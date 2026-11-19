<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { ApiError } from '../lib/api.js';
  import {
    buildOp,
    describeError,
    evidenceRows,
    filterReview,
    filterTerms,
    isDirty,
    reviewAction,
    reviewNote,
    sortTerms,
    validateTerm,
  } from '../lib/glossary.js';
  import { isAdmin } from '../lib/roles.js';
  import { Search } from '@lucide/svelte';

  type Key = 'names' | 'phrases' | 'hard_fixes' | 'review';
  type ReviewItem = {
    term: string;
    reason?: string;
    canonical?: string;
    effect?: string;
    variant_count?: number;
    canonical_count?: number;
    bound?: number;
    score?: number;
    context?: string[];
  };

  // The same authenticated fetch wrapper App.svelte uses: GET when body is absent, POST otherwise.
  // `dirty` is bound by App.svelte so leaving this view with unsaved text can ask first.
  let {
    api,
    dirty = $bindable(false),
    role = 'user',
  }: { api: (path: string, body?: object) => Promise<any>; dirty?: boolean; role?: string } = $props();

  const TABS = ['names', 'phrases', 'hard_fixes', 'review'] as const;
  const TAB_LABELS: Record<Key, string> = {
    names: 'Names',
    phrases: 'Phrases',
    hard_fixes: 'Hard fixes',
    review: 'Review queue',
  };

  let shows = $state<string[]>([]);
  let show = $state('');
  let version = $state('');
  let names = $state<string[]>([]);
  let phrases = $state<string[]>([]);
  let hardFixes = $state<Record<string, string>>({});
  let reviewItems = $state<ReviewItem[]>([]);
  let flaggedTotal = $state(0);
  let wiki = $state('');
  let wikiDraft = $state('');
  let wikiResult = $state<{ ok: boolean; text: string } | null>(null);
  let wikiBusy = $state(false);
  let tab = $state<Key>('names');
  let query = $state('');
  let status = $state('');
  let errorMsg = $state('');
  let note = $state('');
  let loading = $state(false);
  let saving = $state(false);
  let newTerm = $state('');
  let newValue = $state('');
  let editTerm = $state<string | null>(null);
  let editDraft = $state('');
  let editValue = $state('');
  let searchEl = $state<HTMLInputElement>();
  let newTermEl = $state<HTMLInputElement>();
  $effect(() => {
    dirty =
      isDirty({ newTerm, newValue, editTerm, editDraft, editValue, saved: hardFixes }) ||
      (isAdmin(role) && wikiDraft.trim() !== wiki);
  });

  const pairRows = $derived(
    tab === 'hard_fixes'
      ? (sortTerms(filterTerms(Object.entries(hardFixes), query)) as [string, string][])
      : [],
  );
  const textRows = $derived(
    tab === 'hard_fixes'
      ? []
      : (sortTerms(filterTerms(tab === 'names' ? names : phrases, query)) as string[]),
  );
  const reviewRows = $derived(filterReview(reviewItems, query) as ReviewItem[]);
  const limit = $derived(tab === 'names' ? 80 : 120);

  // `quiet` re-reads the lists without swapping the form out for "Loading…" (a save is in progress and the operator is
  // still looking at it); `clearForm: false` keeps the typed text, which a refused save must not throw away.
  async function loadDetail({ quiet = false, clearForm = true } = {}) {
    if (!show) return;
    if (!quiet) {
      loading = true;
      errorMsg = '';
    }
    try {
      const data = (await api('/api/glossary?show=' + encodeURIComponent(show))) as {
        version: string;
        names: string[];
        phrases: string[];
        hard_fixes: Record<string, string>;
        wiki?: string;
        flagged?: ReviewItem[];
        flagged_total?: number;
      };
      version = data.version;
      names = data.names;
      phrases = data.phrases;
      hardFixes = data.hard_fixes;
      reviewItems = data.flagged ?? [];
      flaggedTotal = data.flagged_total ?? reviewItems.length;
      wiki = data.wiki ?? '';
      if (clearForm) {
        wikiDraft = wiki;
        wikiResult = null;
        editTerm = null;
        newTerm = '';
        newValue = '';
      }
    } catch (exc) {
      errorMsg = exc instanceof Error ? exc.message : 'Could not load this glossary.';
    } finally {
      if (!quiet) loading = false;
    }
  }

  async function write(
    op: 'add' | 'update' | 'delete',
    term: string,
    value: string | null,
  ): Promise<boolean> {
    if (saving) return false; // a double click or a held Enter must not send the same edit twice
    if (tab === 'review') return false; // the queue decides through decide(), not the term editor
    errorMsg = '';
    status = '';
    note = '';
    const problem = validateTerm(tab, term, value);
    if (problem) {
      errorMsg = problem;
      return false;
    }
    saving = true;
    try {
      const res = (await api('/api/glossary', buildOp(show, op, tab, term, value, version))) as {
        version: string;
        reapply_hint?: boolean;
      };
      await loadDetail({ quiet: true });
      if (res.reapply_hint) note = 'Saved. Applies to episodes processed from now on.';
      else status = 'Saved.';
      return true;
    } catch (exc) {
      if (exc instanceof ApiError) {
        // 409: someone else saved first. Reload their version, then say so as an error, not a success.
        if (exc.status === 409) await loadDetail({ quiet: true, clearForm: false });
        errorMsg = describeError(exc.status, exc.payload as { error?: string });
      } else {
        errorMsg = exc instanceof Error ? exc.message : 'Request failed. Please try again.';
      }
      return false;
    } finally {
      saving = false;
      // The button that was just pressed is disabled while saving, which drops focus to <body>: put it back.
      await tick();
      (op === 'add' ? newTermEl : searchEl)?.focus();
    }
  }

  // One accept/reject on a flagged entry. The server applies it through the CLI's record_decision; the
  // canonical we saw is sent along so a proposal that changed meanwhile is refused (409) rather than
  // approved sight unseen.
  async function decide(item: ReviewItem, accept = reviewAction(item).accept) {
    if (saving) return;
    errorMsg = '';
    status = '';
    note = '';
    saving = true;
    try {
      await api('/api/glossary', {
        show,
        op: 'review',
        term: item.term,
        accept,
        canonical: item.canonical ?? '',
        expected_version: version,
      });
      await loadDetail({ quiet: true });
      note = accept
        ? `Accepted “${item.term}”. Applies to episodes processed from now on.`
        : `“${item.term}” will not be proposed again.`;
    } catch (exc) {
      if (exc instanceof ApiError) {
        // 409: the queue moved under us (someone saved first, or the proposal changed). Reload, then say so.
        if (exc.status === 409) await loadDetail({ quiet: true });
        errorMsg = describeError(exc.status, exc.payload as { error?: string });
      } else {
        errorMsg = exc instanceof Error ? exc.message : 'Request failed. Please try again.';
      }
    } finally {
      saving = false;
      await tick();
      searchEl?.focus(); // the card that held the button is gone; keep focus inside the editor
    }
  }

  async function testWiki() {
    if (wikiBusy) return;
    wikiBusy = true;
    wikiResult = null;
    try {
      const res = (await api('/api/glossary/wiki-test', { url: wikiDraft.trim() })) as {
        ok: boolean;
        sitename?: string;
        api?: string;
        error?: string;
      };
      wikiResult = res.ok
        ? { ok: true, text: `Reachable: ${res.sitename} (${res.api})` }
        : { ok: false, text: `Not reachable: ${res.error} (${res.api})` };
    } catch (exc) {
      const msg =
        exc instanceof ApiError
          ? describeError(exc.status, exc.payload as { error?: string })
          : exc instanceof Error
            ? exc.message
            : 'Request failed. Please try again.';
      wikiResult = { ok: false, text: msg };
    } finally {
      wikiBusy = false;
    }
  }

  async function saveWiki() {
    if (saving || wikiBusy) return;
    errorMsg = '';
    status = '';
    note = '';
    saving = true;
    try {
      const res = (await api('/api/glossary', {
        show,
        expected_version: version,
        wiki: wikiDraft.trim(),
      })) as { reapply_hint?: boolean };
      await loadDetail({ quiet: true, clearForm: false }); // a half-typed term must survive a wiki save
      if (res.reapply_hint) note = 'Saved. Applies to episodes processed from now on.';
      else status = 'Saved.';
    } catch (exc) {
      if (exc instanceof ApiError) {
        if (exc.status === 409) await loadDetail({ quiet: true, clearForm: false });
        errorMsg = describeError(exc.status, exc.payload as { error?: string });
      } else {
        errorMsg = exc instanceof Error ? exc.message : 'Request failed. Please try again.';
      }
    } finally {
      saving = false;
    }
  }

  async function add() {
    await write('add', newTerm, tab === 'hard_fixes' ? newValue : null);
  }

  function startEdit(term: string) {
    editTerm = term;
    editDraft = term;
    editValue = tab === 'hard_fixes' ? (hardFixes[term] ?? '') : '';
    errorMsg = '';
  }

  async function saveEdit() {
    if (editTerm === null) return;
    if (tab === 'hard_fixes') await write('update', editTerm, editValue);
    else await write('update', editTerm, editDraft);
  }

  async function remove(term: string) {
    if (!window.confirm(`Delete "${term}" from ${show}? This cannot be undone.`)) return;
    await write('delete', term, null); // write() puts focus back on the search field: the deleted row is gone
  }

  onMount(() => {
    void (async () => {
      loading = true;
      try {
        const data = (await api('/api/glossary')) as { shows: string[] };
        shows = [...data.shows].sort((a, b) => a.localeCompare(b));
        if (shows.length > 0) {
          show = shows[0];
          await loadDetail();
        }
      } catch (exc) {
        errorMsg = exc instanceof Error ? exc.message : 'Could not load glossaries.';
      } finally {
        loading = false;
      }
    })();
  });
</script>

<section aria-label="Glossary editor">
  <header class="page-header">
    <h1 tabindex="-1">Glossary</h1>
  </header>
  <p class="page-sub">
    {#if show}Editing {show}{#if version}, version <span title={version}>{version.slice(0, 12)}</span
        >{/if}{:else}Names, phrases and hard fixes for each show.{/if}
  </p>

  {#if errorMsg}<p class="notice failure" role="alert">{errorMsg}</p>{/if}
  {#if status}<p class="notice success" role="status">{status}</p>{/if}
  {#if note}<p class="notice success" role="status">{note}</p>{/if}

  {#if !shows.length && !loading}
    <p class="page-sub">No deployed glossaries.</p>
  {:else}
    <div class="glossary-controls">
      <label for="glossary-show">Show</label>
      <select
        id="glossary-show"
        bind:value={show}
        onchange={() => void loadDetail()}
        disabled={loading || saving}
      >
        {#each shows as name (name)}<option value={name}>{name}</option>{/each}
      </select>
      <div class="glossary-tabs">
        {#each TABS as key (key)}<button
            type="button"
            class="secondary"
            aria-pressed={tab === key}
            disabled={loading || saving}
            onclick={() => {
              tab = key;
              editTerm = null;
            }}
            >{TAB_LABELS[key]}{#if key === 'review' && flaggedTotal > 0}<span
                class="count-badge"
                aria-hidden="true">{flaggedTotal}</span
              >{/if}</button
          >{/each}
      </div>
      <label class="search-field glossary-search"
        ><Search size={16} aria-hidden="true" /><input
          bind:this={searchEl}
          bind:value={query}
          placeholder="Filter terms…"
          aria-label="Search terms"
        /></label
      >
    </div>

    {#if loading}
      <p class="page-sub">Loading glossary…</p>
    {:else if show}
      <div class="glossary-wiki">
        {#if isAdmin(role)}
          <form
            onsubmit={(e) => {
              e.preventDefault();
              void saveWiki();
            }}
          >
            <label for="glossary-wiki">Wiki</label>
            <input
              id="glossary-wiki"
              bind:value={wikiDraft}
              maxlength={300}
              placeholder="https://yugioh.fandom.com"
              autocomplete="off"
              spellcheck="false"
              aria-describedby="glossary-wiki-help"
            />
            <button
              class="secondary"
              type="button"
              disabled={wikiBusy || saving || !wikiDraft.trim()}
              onclick={() => void testWiki()}>Test wiki</button
            >
            <button class="primary" type="submit" disabled={saving || wikiBusy || wikiDraft.trim() === wiki}
              >Save wiki</button
            >
          </form>
        {:else}
          <p><span class="wiki-label">Wiki</span> {wiki || 'auto-detect'}</p>
        {/if}
        <p id="glossary-wiki-help" class="page-sub">
          Fandom or MediaWiki site URL, e.g. https://yugioh.fandom.com. Empty = auto-detect.
        </p>
        {#if wikiResult}<p class="notice {wikiResult.ok ? 'success' : 'failure'}" role="status">
            {wikiResult.text}
          </p>{/if}
      </div>
      {#if tab !== 'review'}
        <form
          class="glossary-add"
          onsubmit={(e) => {
            e.preventDefault();
            void add();
          }}
        >
          <label for="glossary-new-term">New term</label>
          <input
            id="glossary-new-term"
            bind:this={newTermEl}
            bind:value={newTerm}
            maxlength={limit}
            placeholder={tab === 'hard_fixes' ? 'Misheard text' : 'Term to add'}
            autocomplete="off"
            spellcheck="false"
          />
          {#if tab === 'hard_fixes'}
            <label for="glossary-new-value">Replacement</label>
            <input
              id="glossary-new-value"
              bind:value={newValue}
              maxlength={limit}
              placeholder="Correct text"
              autocomplete="off"
              spellcheck="false"
            />
          {/if}
          <button class="primary" type="submit" disabled={saving || !newTerm.trim()}>Add term</button>
        </form>
      {/if}

      {#if tab === 'review'}
        <section class="review-queue" aria-label="Flagged review queue">
          <p class="page-sub">
            {#if flaggedTotal === 0}
              No flagged entries to review.
            {:else}
              {reviewRows.length} of {flaggedTotal} flagged {flaggedTotal === 1 ? 'entry' : 'entries'} shown. Accepting
              writes the canonical, rejecting records the spelling as known.
            {/if}
          </p>
          <ul class="review-list">
            {#each reviewRows as item (item.term)}
              <li class="review-card">
                <div class="review-head">
                  <span class="review-term">{item.term}</span>
                  {#if item.canonical}
                    <span class="review-arrow" aria-hidden="true">→</span>
                    <span class="review-canonical">{item.canonical}</span>
                  {:else}
                    <span class="review-canonical muted">(no canonical)</span>
                  {/if}
                  {#if item.reason}<span class="review-reason">{item.reason}</span>{/if}
                </div>
                <p class="review-evidence">
                  {#each evidenceRows(item) as row, i (i)}<span class="review-datum"
                      ><span class="review-datum-label">{row.label}</span> {row.value}</span
                    >{/each}
                </p>
                {#if reviewNote(item)}<p class="page-sub review-note">{reviewNote(item)}</p>{/if}
                <div class="review-actions">
                  <button class="primary" type="button" disabled={saving} onclick={() => void decide(item)}>
                    {reviewAction(item).label}
                  </button>
                  {#if item.canonical}
                    <button
                      class="secondary"
                      type="button"
                      disabled={saving}
                      onclick={() => void decide(item, false)}>Reject</button
                    >
                  {/if}
                </div>
              </li>
            {:else}
              <li class="empty-row">
                {flaggedTotal === 0 ? 'Nothing is waiting in the review queue.' : 'No flagged entries match.'}
              </li>
            {/each}
          </ul>
        </section>
      {:else if tab === 'hard_fixes'}
        <table class="data-table glossary-table">
          <thead
            ><tr><th scope="col">Misheard</th><th scope="col">Replacement</th><th scope="col">Actions</th></tr
            ></thead
          >
          <tbody>
            {#each pairRows as [term, value], i (term)}
              <tr>
                {#if editTerm === term}
                  <td>{term}</td>
                  <td>
                    <label for="glossary-edit-{i}">Replacement</label>
                    <input
                      id="glossary-edit-{i}"
                      bind:value={editValue}
                      maxlength={limit}
                      autocomplete="off"
                      spellcheck="false"
                    />
                  </td>
                  <td>
                    <button class="primary" type="button" disabled={saving} onclick={() => void saveEdit()}
                      >Save</button
                    >
                    <button
                      class="secondary"
                      type="button"
                      disabled={saving}
                      onclick={() => (editTerm = null)}>Cancel</button
                    >
                  </td>
                {:else}
                  <td>{term}</td>
                  <td>{value}</td>
                  <td>
                    <button class="secondary" type="button" disabled={saving} onclick={() => startEdit(term)}
                      >Edit</button
                    >
                    <button
                      class="secondary danger"
                      type="button"
                      disabled={saving}
                      onclick={() => void remove(term)}>Delete</button
                    >
                  </td>
                {/if}
              </tr>
            {:else}
              <tr><td class="empty-row" colspan="3">No terms match.</td></tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <table class="data-table glossary-table">
          <thead><tr><th scope="col">Term</th><th scope="col">Actions</th></tr></thead>
          <tbody>
            {#each textRows as term, i (i + ':' + term)}
              <tr>
                {#if editTerm === term}
                  <td>
                    <label for="glossary-edit-{i}">Term</label>
                    <input
                      id="glossary-edit-{i}"
                      bind:value={editDraft}
                      maxlength={limit}
                      autocomplete="off"
                      spellcheck="false"
                    />
                  </td>
                  <td>
                    <button class="primary" type="button" disabled={saving} onclick={() => void saveEdit()}
                      >Save</button
                    >
                    <button
                      class="secondary"
                      type="button"
                      disabled={saving}
                      onclick={() => (editTerm = null)}>Cancel</button
                    >
                  </td>
                {:else}
                  <td>{term}</td>
                  <td>
                    <button class="secondary" type="button" disabled={saving} onclick={() => startEdit(term)}
                      >Edit</button
                    >
                    <button
                      class="secondary danger"
                      type="button"
                      disabled={saving}
                      onclick={() => void remove(term)}>Delete</button
                    >
                  </td>
                {/if}
              </tr>
            {:else}
              <tr><td class="empty-row" colspan="2">No terms match.</td></tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {/if}
  {/if}
</section>

<style>
  .glossary-controls {
    display: grid;
    grid-template-columns: max-content minmax(0, 16rem) 1fr;
    align-items: center;
    gap: 10px 12px;
    margin-bottom: 16px;
  }
  .glossary-controls > label {
    color: var(--muted);
  }
  .glossary-tabs,
  .glossary-search {
    grid-column: 1 / -1;
  }
  .glossary-search {
    max-width: 24rem;
  }
  /* Tabs stay toggle buttons (aria-pressed); the pressed one reads as the selected tab. */
  .glossary-tabs {
    display: flex;
    gap: 0;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border);
  }
  .glossary-tabs button {
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    color: var(--muted);
    margin-bottom: -1px;
  }
  .glossary-tabs button:hover:not(:disabled) {
    background: var(--surface);
    color: var(--text);
  }
  .glossary-tabs button[aria-pressed='true'] {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
  .glossary-add {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }
  .glossary-wiki {
    margin-bottom: 16px;
  }
  .glossary-wiki form {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .glossary-wiki label,
  .wiki-label {
    color: var(--muted);
  }
  .glossary-wiki input {
    flex: 1 1 20rem;
    max-width: 32rem;
  }
  .glossary-add > label {
    color: var(--muted);
  }
  .glossary-table td:last-child {
    white-space: nowrap;
    width: 1%;
  }
  .glossary-table td button + button {
    margin-left: 6px;
  }
  .glossary-table td input {
    width: 100%;
  }
  /* A backlog number on the Review queue tab: visible at a glance, outside the button's accessible name. */
  .count-badge {
    display: inline-block;
    margin-left: 6px;
    padding: 0 6px;
    border-radius: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 0.75em;
    line-height: 1.5;
  }
  .review-list {
    display: grid;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .review-card {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    display: grid;
    gap: 6px;
  }
  .review-head {
    display: flex;
    gap: 8px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  .review-term {
    font-weight: 600;
  }
  .review-canonical.muted {
    color: var(--muted);
  }
  .review-reason {
    margin-left: auto;
    color: var(--muted);
    font-size: 0.85em;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0 6px;
  }
  .review-evidence {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 0.85em;
  }
  .review-datum-label {
    color: var(--muted);
  }
  .review-note {
    margin: 0;
  }
  .review-actions {
    display: flex;
    gap: 8px;
  }
  /* The inline edit label is read by screen readers; sighted users see the field in place of the term. */
  .glossary-table td label {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
