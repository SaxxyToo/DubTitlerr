<script lang="ts">
  import { onDestroy } from 'svelte';
  import { rememberFocus, restoreFocus } from './focus.js';

  let { onstay, ondiscard }: { onstay: () => void; ondiscard: () => void } = $props();
  let dialog = $state<HTMLDialogElement>();
  $effect(() => {
    if (dialog && !dialog.open) {
      rememberFocus();
      dialog.showModal();
    }
  });
  onDestroy(restoreFocus);
</script>

<dialog bind:this={dialog} class="dialog-shell" aria-labelledby="unsaved-title" onclose={onstay}>
  <div class="panel-title"><h2 id="unsaved-title">Unsaved changes</h2></div>
  <p>This page has edits that are not saved yet. Leaving now drops them.</p>
  <div class="dialog-actions">
    <button class="secondary" type="button" onclick={onstay}>Stay and review</button>
    <button class="primary" type="button" onclick={ondiscard}>Discard</button>
  </div>
</dialog>
