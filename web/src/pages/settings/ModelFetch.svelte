<script lang="ts">
  import { describeModelsResult } from '../../lib/settingsModel.js';

  let {
    api,
    target,
    label,
    hint = '',
    disabled = false,
    canEdit = true,
    onpick,
  }: {
    api: (path: string, options?: { method?: string; body?: object }) => Promise<any>;
    /** The POST /api/settings/models body: {connection: name} or {endpoint: 'transcription'}. */
    target: object;
    /** The Fetch button's text; the server is probed only when it is clicked. */
    label: string;
    hint?: string;
    /** True while the target cannot be probed yet (a new or edited connection has to be saved first). */
    disabled?: boolean;
    canEdit?: boolean;
    onpick?: (model: string) => void;
  } = $props();

  let models = $state<string[] | null>(null);
  let error = $state('');
  let busy = $state(false);

  async function fetchModels() {
    if (busy || disabled) return;
    busy = true;
    error = '';
    try {
      const res = await api('/api/settings/models', { method: 'POST', body: target });
      if (res?.ok === true) {
        models = Array.isArray(res.models) ? res.models : [];
      } else {
        models = null;
        error = describeModelsResult(res);
      }
    } catch (exc) {
      models = null;
      error = exc instanceof Error ? exc.message : 'The model list could not be fetched.';
    } finally {
      busy = false;
    }
  }

  function pick(event: Event) {
    const select = event.currentTarget as HTMLSelectElement;
    const value = select.value;
    select.value = '';
    if (value) onpick?.(value);
  }
</script>

{#if canEdit}
  <div class="model-fetch">
    <button class="secondary" type="button" disabled={disabled || busy} onclick={() => void fetchModels()}>
      {busy ? 'Fetching models…' : label}
    </button>
    {#if models && models.length}
      <select aria-label="Models the server offers" onchange={pick}>
        <option value="">Choose a model…</option>
        {#each models as id (id)}<option value={id}>{id}</option>{/each}
      </select>
    {:else if models}
      <span class="hint">The server lists no models; type the name.</span>
    {/if}
    {#if error}<p class="field-error" role="alert">{error}</p>{/if}
    {#if hint}<p class="hint">{hint}</p>{/if}
  </div>
{/if}

<style>
  .model-fetch {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .model-fetch select {
    max-width: 24rem;
    min-width: 0;
  }
  .hint {
    margin: 0;
    color: var(--muted);
  }
  .field-error {
    margin: 0;
    color: var(--bad);
  }
</style>
