<script lang="ts">
  // One empty presentation: icon, headline and a sentence saying what would fill the space.
  import type { Component } from 'svelte';

  let {
    title,
    detail = '',
    icon,
  }: {
    title: string;
    /** The sentence under the headline; '' hides it. */
    detail?: string;
    /** A decorative icon component; the headline carries the meaning. */
    icon?: Component;
  } = $props();
</script>

<div class="empty-state">
  {#if icon}{@const Icon = icon}<Icon size={32} aria-hidden="true" />{/if}
  <h3>{title}</h3>
  {#if detail}<p>{detail}</p>{/if}
</div>
