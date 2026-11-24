<script lang="ts">
  // P33 audit-first: the workspace opens on what the app DECIDED, not on a pending gate.
  // Most rows are automatic (P32 dismiss/apply, P33 expiry); each one is inspectable and
  // reversible in one tap, and the reversal is recorded server-side. The pending queues are
  // the exception list, reached from the header, and they are bounded and expire on their own.
  import { onMount } from 'svelte';
  import { CheckCircle2, Clock, History, RefreshCw, RotateCcw, ShieldCheck } from '@lucide/svelte';
  import {
    auditSummary,
    classRows,
    decisionLabel,
    decisionReason,
    decisionTitle,
    decisionTone,
    isUndoable,
    queueHeadline,
  } from '../lib/audit.js';
  import { formatCount, relativeTime } from '../lib/format.js';
  import Empty from '../lib/Empty.svelte';
  import Loading from '../lib/Loading.svelte';

  let {
    api,
    onopeninbox,
    pending = 0,
  }: {
    api: (path: string, body?: object, headers?: Record<string, string>, method?: string) => Promise<any>;
    onopeninbox: () => void;
    pending?: number;
  } = $props();

  type AuditItem = {
    id: string;
    kind: string;
    decision: string;
    at: number;
    show: string;
    term?: string;
    reason?: string;
    guard?: string | null;
    original_text?: string;
    detail: Record<string, unknown>;
    undoable: boolean;
  };

  let decisions = $state<AuditItem[]>([]);
  let counts = $state<Record<string, number>>({});
  let queues = $state<any>(null);
  let loading = $state(true);
  let busy = $state('');
  let error = $state('');
  let message = $state('');

  async function load() {
    error = '';
    try {
      const feed = await api('/api/audit?limit=50');
      decisions = feed.decisions ?? [];
      counts = feed.counts ?? {};
      queues = await api('/api/queues');
    } catch (exc) {
      error = exc instanceof Error ? exc.message : 'Could not load the audit feed.';
    } finally {
      loading = false;
    }
  }

  async function undo(item: AuditItem) {
    if (busy) return;
    busy = item.id;
    message = '';
    error = '';
    try {
      await api('/api/audit/undo', { id: item.id }, undefined, 'POST');
      message = `Restored: ${decisionTitle(item)}. The reversal is recorded in the audit.`;
      await load();
    } catch (exc) {
      error = exc instanceof Error ? exc.message : 'Undo failed.';
    } finally {
      busy = '';
    }
  }

  onMount(load);

  const headline = $derived(queueHeadline(queues));
  const classes = $derived(classRows(queues));
  const summary = $derived(auditSummary(counts));
</script>

<header class="page-header">
  <h1 tabindex="-1">Recent decisions</h1>
  <div class="page-actions">
    <button class="secondary" onclick={load} disabled={loading}
      ><RefreshCw size={16} class={loading ? 'spin' : ''} />Refresh</button
    >
    <button class="secondary" onclick={onopeninbox}
      >Review inbox{pending ? ` (${formatCount(pending)})` : ''}</button
    >
  </div>
</header>
<p class="page-sub">
  {summary}. The app made these; each one is inspectable and reversible, and every undo restores the previous
  state and records the reversal.
</p>

{#if message}<div class="notice success" role="status">
    <CheckCircle2 size={18} /><span>{message}</span>
  </div>{/if}
{#if error}<div class="notice failure" role="alert">
    <ShieldCheck size={18} /><span>{error}</span>
  </div>{/if}

<section class="queue-health" aria-label="Queue health">
  <div class="panel-title">
    <h2>Queues</h2>
    <span class="count">{formatCount(headline.queued)} open</span>
  </div>
  <p class="page-sub">{headline.note}</p>
  {#if classes.length}
    <ul class="class-list">
      {#each classes as row (row.cls)}
        <li><span class="class-name">{row.cls}</span><span class="count">{formatCount(row.count)}</span></li>
      {/each}
    </ul>
  {:else}
    <p class="empty-small">Nothing is queued. That is the normal state now.</p>
  {/if}
</section>

<section class="audit-feed" aria-label="Recent auto decisions">
  {#if loading}
    <Loading label="Reading the audit…" />
  {:else if !decisions.length}
    <Empty
      icon={History}
      title="No automatic decisions yet"
      detail="When the pipeline dismisses noise or applies a corroborated fix, it lands here first."
    />
  {:else}
    <ul class="audit-list">
      {#each decisions as item (item.id)}
        <li class="audit-row">
          <div class="audit-main">
            <div class="audit-title">
              <span class="badge" data-tone={decisionTone(item.decision)}>{decisionLabel(item.decision)}</span
              >
              <strong>{decisionTitle(item)}</strong>
              {#if item.show}<small class="audit-show">{item.show}</small>{/if}
            </div>
            <small class="audit-why">{decisionReason(item)}</small>
          </div>
          <div class="audit-side">
            <small class="audit-time"><Clock size={13} />{relativeTime(item.at)}</small>
            {#if isUndoable(item)}
              <button class="secondary" onclick={() => undo(item)} disabled={busy === item.id}>
                <RotateCcw size={15} />Undo
              </button>
            {/if}
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .page-actions {
    display: flex;
    gap: 0.5em;
    flex-wrap: wrap;
  }
  .queue-health,
  .audit-feed {
    margin-top: 1.25em;
  }
  .class-list {
    list-style: none;
    margin: 0.5em 0 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5em 1.25em;
  }
  .class-list li {
    display: flex;
    align-items: center;
    gap: 0.45em;
  }
  .class-name {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 0.9em;
  }
  .audit-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5em;
  }
  .audit-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1em;
    padding: 0.65em 0.8em;
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .audit-title {
    display: flex;
    align-items: baseline;
    gap: 0.5em;
    flex-wrap: wrap;
  }
  .audit-show {
    opacity: 0.7;
  }
  .audit-why {
    display: block;
    margin-top: 0.2em;
    opacity: 0.75;
    word-break: break-word;
  }
  .audit-side {
    display: flex;
    align-items: center;
    gap: 0.6em;
    flex-shrink: 0;
  }
  .audit-time {
    display: inline-flex;
    align-items: center;
    gap: 0.25em;
    opacity: 0.7;
    white-space: nowrap;
  }
  @media (max-width: 720px) {
    .audit-row {
      flex-direction: column;
      align-items: flex-start;
    }
  }
</style>
