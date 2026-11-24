<script lang="ts">
  import { describeProcessing } from '../../lib/processing.js';

  let {
    processing = null,
    canEdit = false,
    busy = false,
    onaction,
  }: {
    processing?: {
      state?: string;
      by?: string | null;
      at?: string | null;
      default?: boolean;
      error?: string;
    } | null;
    canEdit?: boolean;
    busy?: boolean;
    onaction: (action: 'start' | 'pause' | 'stop') => void;
  } = $props();

  const view = $derived(describeProcessing(processing));
</script>

<section class="settings-section" aria-label="Processing">
  <header class="processing-head">
    <h2>Processing</h2>
    <span class="count state-{view.state}" role="status" aria-live="polite">{view.label}</span>
  </header>
  <p class="page-sub">{view.detail}</p>
  {#if canEdit}
    <div class="processing-actions">
      <button
        class="primary"
        type="button"
        disabled={busy || view.state === 'running'}
        onclick={() => onaction('start')}
      >
        Start
      </button>
      <button
        class="secondary"
        type="button"
        disabled={busy || view.state === 'paused'}
        onclick={() => onaction('pause')}
      >
        Pause
      </button>
      <button
        class="secondary danger"
        type="button"
        disabled={busy || view.state === 'stopped'}
        onclick={() => onaction('stop')}
      >
        Stop
      </button>
    </div>
    <p class="page-sub">
      Pause never kills the unit in flight: it finishes, then both loops hold. Stop drains the unit and ends
      the container; with a restart policy the UI comes back idle and Start resumes processing.
    </p>
  {:else}
    <p class="page-sub">Only an administrator can change the processing state.</p>
  {/if}
</section>

<style>
  .processing-head {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .state-running {
    color: var(--ok);
    border-color: currentColor;
  }
  .state-paused {
    color: var(--warn);
    border-color: currentColor;
  }
  .state-stopped {
    color: var(--bad);
    border-color: currentColor;
  }
  .processing-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
</style>
