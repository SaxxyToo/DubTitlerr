<script lang="ts">
  // One loading presentation: the muted status line pages used to repeat by hand, or the tall
  // placeholder a panel needs so its layout does not jump when the data lands.
  import { LoaderCircle } from '@lucide/svelte';

  let {
    label,
    detail = '',
  }: {
    /** The line a reader sees, e.g. "Loading glossary…". */
    label: string;
    /** A second sentence; when set, the loading state fills a panel instead of showing one line. */
    detail?: string;
  } = $props();
</script>

{#if detail}
  <div class="empty-state" role="status">
    <LoaderCircle size={32} class="spin" aria-hidden="true" />
    <h3>{label}</h3>
    <p>{detail}</p>
  </div>
{:else}
  <!-- Inline: the same muted line the pages used to hand-roll. No live region here, so a page's own
       status announcement stays the only one a screen reader hears. -->
  <p class="page-sub loading-line">
    <LoaderCircle size={15} class="spin" aria-hidden="true" />{label}
  </p>
{/if}

<style>
  .loading-line {
    display: flex;
    align-items: center;
    gap: 8px;
  }
</style>
