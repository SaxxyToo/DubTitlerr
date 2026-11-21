<script lang="ts">
  import {
    CLEAR,
    blankNotification,
    describeNotificationResult,
    notificationTestable,
    secretFieldFor,
    toggleTrigger,
    withNotificationKind,
  } from '../../lib/settingsModel.js';

  type Provider = { name: string; kind: string; [field: string]: any };
  let {
    rules,
    saved,
    working = $bindable([]),
    errors = {},
    secretsWritable,
    canEdit = true,
    api,
  }: {
    /** `notifications` from GET /api/settings/schema: the kinds, their fields and the trigger names. */
    rules: any;
    /** The providers the server holds, to tell a saved one (testable) from a new or edited one. */
    saved: Provider[];
    working?: Provider[];
    /** Field messages from a refused save: "notifications.<name or index>.<field>". */
    errors?: Record<string, string>;
    secretsWritable: boolean;
    /** False for a reviewer: the page shows the providers but changes nothing. */
    canEdit?: boolean;
    api: (path: string, options?: { method?: string; body?: object }) => Promise<any>;
  } = $props();

  const TRIGGER_LABELS: Record<string, string> = {
    'run.requested': 'Run requested',
    'sweep.completed': 'Sweep completed',
    'merge.completed': 'Merge completed',
    'pass.failed': 'Pass failed',
    'review.pending': 'Review pending',
    'episode.delivered': 'Episode delivered',
    'episode.refused': 'Episode refused',
  };
  const KIND_LABELS: Record<string, string> = {
    discord: 'Discord',
    ntfy: 'ntfy',
    webhook: 'Webhook',
  };
  let results = $state<Record<string, { ok: boolean; text: string }>>({});
  let testing = $state<Record<string, boolean>>({});

  const label = (p: Provider, i: number) => p.name || String(i);
  const fieldError = (p: Provider, i: number, field: string) =>
    errors[`notifications.${label(p, i)}.${field}`] ?? '';
  const savedCopy = (p: Provider) => saved.find((s) => s.name === p.name);
  const secretField = (p: Provider) => secretFieldFor(p.kind);

  function add() {
    working = [...working, blankNotification(rules)];
  }
  function remove(i: number) {
    working = working.filter((_, n) => n !== i);
  }
  function update(i: number, patch: Partial<Provider>) {
    working = working.map((p, n) => (n === i ? { ...p, ...patch } : p));
  }
  function setKind(i: number, kind: string) {
    working = working.map((p, n) => (n === i ? withNotificationKind(p, kind) : p));
  }
  function toggle(i: number, trigger: string) {
    update(i, { triggers: toggleTrigger(working[i].triggers ?? [], trigger) });
  }

  async function run(slot: string, name: string) {
    if (testing[slot]) return;
    testing[slot] = true;
    try {
      const res = await api('/api/settings/test', { method: 'POST', body: { notification: name } });
      results[slot] = { ok: res?.ok === true, text: describeNotificationResult(res) };
    } catch (exc) {
      results[slot] = { ok: false, text: exc instanceof Error ? exc.message : 'The test could not run.' };
    } finally {
      testing[slot] = false;
    }
  }
</script>

<section class="notifications" aria-label="Notifications">
  <h2>Notifications</h2>
  <p class="page-sub">
    A notification provider receives an event when a run, sweep, merge pass or review finishes. A provider
    with no triggers stays quiet until you pick one.
  </p>

  {#each working as p, i (i)}
    {@const field = secretField(p)}
    <fieldset class="provider" data-notification={i} disabled={!canEdit}>
      <legend>Notification {p.name || '(new)'}</legend>
      <div class="field">
        <label for={`notif-${i}-name`}>Name</label>
        <input
          id={`notif-${i}-name`}
          type="text"
          value={p.name}
          readonly={Boolean(savedCopy(p))}
          autocomplete="off"
          spellcheck="false"
          oninput={(e) => update(i, { name: e.currentTarget.value })}
        />
        {#if fieldError(p, i, 'name')}<p class="field-error" role="alert">{fieldError(p, i, 'name')}</p>{/if}
      </div>
      <div class="field">
        <label for={`notif-${i}-kind`}>Kind</label>
        <select id={`notif-${i}-kind`} value={p.kind} onchange={(e) => setKind(i, e.currentTarget.value)}>
          {#each rules?.kinds ?? [] as kind (kind)}
            <option value={kind}>{KIND_LABELS[kind] ?? kind}</option>
          {/each}
        </select>
        {#if fieldError(p, i, 'kind')}<p class="field-error" role="alert">{fieldError(p, i, 'kind')}</p>{/if}
      </div>
      <div class="field inline">
        <label for={`notif-${i}-enabled`}>
          <input
            id={`notif-${i}-enabled`}
            type="checkbox"
            checked={p.enabled === true}
            onchange={(e) => update(i, { enabled: e.currentTarget.checked })}
          />
          Enabled
        </label>
      </div>

      {#if p.kind === 'ntfy'}
        <div class="field">
          <label for={`notif-${i}-server`}>Server URL</label>
          <input
            id={`notif-${i}-server`}
            type="text"
            value={p.server ?? ''}
            autocomplete="off"
            spellcheck="false"
            placeholder="https://ntfy.sh"
            oninput={(e) => update(i, { server: e.currentTarget.value })}
          />
          {#if fieldError(p, i, 'server')}<p class="field-error" role="alert">
              {fieldError(p, i, 'server')}
            </p>{/if}
        </div>
        <div class="field">
          <label for={`notif-${i}-topic`}>Topic</label>
          <input
            id={`notif-${i}-topic`}
            type="text"
            value={p.topic ?? ''}
            autocomplete="off"
            spellcheck="false"
            placeholder="dubtitlerr"
            oninput={(e) => update(i, { topic: e.currentTarget.value })}
          />
          {#if fieldError(p, i, 'topic')}<p class="field-error" role="alert">
              {fieldError(p, i, 'topic')}
            </p>{/if}
        </div>
      {/if}

      {#if p.kind === 'discord'}
        <div class="field">
          <label for={`notif-${i}-link-url`}>Review UI URL (optional)</label>
          <input
            id={`notif-${i}-link-url`}
            type="text"
            value={p.link_url ?? ''}
            autocomplete="off"
            spellcheck="false"
            placeholder="https://dub.example/review"
            oninput={(e) => update(i, { link_url: e.currentTarget.value })}
          />
          {#if fieldError(p, i, 'link_url')}<p class="field-error" role="alert">
              {fieldError(p, i, 'link_url')}
            </p>{/if}
        </div>
      {/if}

      <div class="field">
        {#if secretsWritable && canEdit}
          <label for={`notif-${i}-secret`}>
            {p.kind === 'ntfy' ? 'Access token' : p.kind === 'discord' ? 'Webhook URL' : 'Webhook URL'}
          </label>
          <input
            id={`notif-${i}-secret`}
            type="password"
            autocomplete="new-password"
            spellcheck="false"
            value={typeof p.secret === 'string' ? p.secret : ''}
            placeholder={p[field]?.set ? 'Set. Type to replace it.' : 'Not set'}
            oninput={(e) =>
              update(i, { secret: e.currentTarget.value === '' ? undefined : e.currentTarget.value })}
          />
          {#if p[field]?.set && p.secret !== CLEAR}
            <button class="secondary" type="button" onclick={() => update(i, { secret: CLEAR })}>
              Clear {p.kind === 'ntfy' ? 'token' : 'URL'}
            </button>
          {/if}
          {#if p.secret === CLEAR}<span class="hint">The secret is cleared when you save.</span>{/if}
        {:else}
          <p class="field-label">
            {p.kind === 'ntfy' ? 'Access token' : 'Webhook URL'}: {p[field]?.set ? 'Set' : 'Not set'}
          </p>
          {#if !secretsWritable}<p class="hint">available after login is enabled</p>{/if}
        {/if}
        {#if fieldError(p, i, field)}<p class="field-error" role="alert">{fieldError(p, i, field)}</p>{/if}
      </div>

      <div class="field">
        <p class="field-label">Send on</p>
        <div class="triggers">
          {#each rules?.triggers ?? [] as trigger (trigger)}
            <label for={`notif-${i}-${trigger}`}>
              <input
                id={`notif-${i}-${trigger}`}
                type="checkbox"
                checked={(p.triggers ?? []).includes(trigger)}
                onchange={() => toggle(i, trigger)}
              />
              {TRIGGER_LABELS[trigger] ?? trigger}
            </label>
          {/each}
        </div>
        {#if fieldError(p, i, 'triggers')}<p class="field-error" role="alert">
            {fieldError(p, i, 'triggers')}
          </p>{/if}
      </div>

      <div class="actions">
        {#if canEdit}
          <button
            class="secondary"
            type="button"
            disabled={!notificationTestable(p, saved) || testing[`notif:${p.name}`]}
            title={notificationTestable(p, saved) ? '' : 'Save this notification first'}
            onclick={() => void run(`notif:${p.name}`, p.name)}>Test notification {p.name}</button
          >
          <button
            class="secondary"
            type="button"
            aria-label={`Remove notification ${p.name || 'new'}`}
            onclick={() => remove(i)}>Remove</button
          >
        {/if}
      </div>
      {#if results[`notif:${p.name}`]}
        <p
          id={`notif-${i}-result`}
          class={['notice', results[`notif:${p.name}`].ok ? 'success' : 'failure']}
          role={results[`notif:${p.name}`].ok ? 'status' : 'alert'}
        >
          {results[`notif:${p.name}`].text}
        </p>
      {/if}
    </fieldset>
  {/each}
  {#if canEdit}<button class="secondary" type="button" onclick={add}>Add notification</button>{/if}
</section>

<style>
  .notifications {
    margin-top: 8px;
    max-width: 52rem;
  }
  .provider {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 14px;
    margin: 0 0 10px;
    display: grid;
    gap: 8px;
  }
  .provider legend {
    padding: 0 6px;
    font-weight: 600;
  }
  .field {
    display: grid;
    gap: 4px;
  }
  .field.inline {
    grid-template-columns: repeat(auto-fit, minmax(10rem, max-content));
    align-items: center;
    gap: 8px;
  }
  .field input[type='text'],
  .field input[type='password'],
  .field select {
    max-width: 36rem;
    min-width: 0;
  }
  .field-label {
    margin: 0;
    font-weight: 600;
  }
  .triggers {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(12rem, max-content));
    gap: 4px 12px;
  }
  .triggers label {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .hint {
    margin: 0;
    color: var(--muted);
  }
  .field-error {
    margin: 0;
    color: var(--bad);
  }
  .actions {
    display: flex;
    gap: 8px;
  }
</style>
