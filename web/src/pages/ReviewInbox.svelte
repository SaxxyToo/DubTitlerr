<script lang="ts">
  // The review inbox — first viewport, The Audio Deck composition (comp-3).
  // Boxes and colours bind the measured scaffold (layout.css) and the comp's
  // sampled pixels: exact values for a 1024x576 frame.
  // Words are the approved comp's, verbatim where the comp's words are real words.
  //
  // FROZEN CAPTURE. This page is a screenshot artifact (hero.html), not product copy: it is not
  // routed to, it does not import app.css, and its palette/vocabulary are measured from the comp.
  // The live source of truth is App.svelte + app.css; when the design moves, re-capture this file
  // rather than editing it to match. The waveform generator is shared either way (lib/waveform.js).
  import { waveTrace } from '../lib/waveform.js';

  // The comp shows one garbled extra label before Dashboard; it is rendered here
  // as its most plausible word. Integration swaps in the real section list.
  const tabs = ['Playlists', 'Dashboard', 'Glossary', 'Runs', 'Settings'];
  const scaleLabels = ['1.0', '0.8', '0.6', '0.4', '0.2', '0.0', '0.2', '0.4', '0.6', '0.8', '1.0'];
  const tickLabels = ['00:00', '00:02', '00:04', '00:06', '00:08', '00:10', '00:12', '00:14', '00:16'];

  // The hero's own pixel-measured band, drawn by the shared generator.
  const wave = waveTrace(924.7, 290.3, 330);
</script>

<div class="frame">
  <header class="topbar">
    <a class="r-brand" href="#/inbox" data-region="brand">DubTitlerr</a>

    <nav class="tabs" aria-label="Sections">
      <button class="r-tab-inbox tab" data-region="tab-inbox" aria-current="page">
        Review inbox<span class="count">3</span>
      </button>
      <button class="r-tab-shared tab" data-region="tab-shared">Shared lines</button>
      <span class="r-tab-cluster tab-cluster" data-region="tab-cluster"
        >{#each tabs as t (t)}<button class="tab">{t}</button>{/each}</span
      >
    </nav>

    <span class="r-healthy healthy" data-region="healthy">Healthy</span>
    <span class="r-top-icons top-icons" data-region="top-icons" aria-hidden="true">
      <span class="glyph">?</span>
      <svg class="bell" viewBox="0 0 24 24" width="18" height="18" fill="#99979a" aria-hidden="true"
        ><path
          d="M12 22a2.5 2.5 0 0 0 2.45-2h-4.9A2.5 2.5 0 0 0 12 22Zm7-5h-1v-6a6 6 0 0 0-4.5-5.8V4a1.5 1.5 0 0 0-3 0v1.2A6 6 0 0 0 6 11v6H5a1 1 0 0 0 0 2h14a1 1 0 0 0 0-2Z"
        /></svg
      >
      <span class="acct"><i>✕</i></span>
    </span>
    <span class="r-scroll-thumb scroll-thumb" data-region="scroll-thumb"></span>
  </header>

  <main class="deck">
    <section class="r-band-ground band" data-region="band-ground" aria-label="Dub audio for the active line">
      <div class="scale" aria-hidden="true">
        {#each scaleLabels as s, i (i)}<span>{s}</span>{/each}
      </div>
      <svg
        class="r-waveform-trace wave"
        data-region="waveform-trace"
        viewBox="0 0 924.7 290.3"
        preserveAspectRatio="none"
        aria-hidden="true"><path d={wave} /></svg
      >
      <div class="r-playhead-line playhead" data-region="playhead-line" aria-hidden="true">
        <i class="line"></i><i class="handle"></i>
      </div>
      <div class="r-timechip timechip" data-region="timechip">0:08</div>
    </section>

    <div class="r-timeline-strip timeline" data-region="timeline-strip" aria-hidden="true">
      <div class="ticks"></div>
      <div class="tick-labels">
        {#each tickLabels as t (t)}<span>{t}</span>{/each}
      </div>
    </div>
    <button class="r-listen-hint listen-hint" data-region="listen-hint">Listen to dub (Space)</button>
    <button class="r-listen-pill listen-pill" data-region="listen-pill">
      <span class="plabel">Listen to dub (Space)</span> <span class="seg"><span class="chev">∨</span></span>
    </button>

    <section class="r-overlay-card card" data-region="overlay-card" aria-label="Line under review">
      <p class="r-orig-text orig" data-region="orig-text">
        The <s>blue</s> compass is ours.
      </p>
      <i class="rule"></i>
      <p class="r-repair-label rep-label" data-region="repair-label">Repair line</p>
      <p class="r-repair-text repair" data-region="repair-text">The compass is ours.</p>
    </section>

    <div
      class="r-transport-deck transport"
      data-region="transport-deck"
      role="group"
      aria-label="Review decisions"
    >
      <button class="r-btn-prev btn" data-region="btn-prev">Previous <kbd>(K)</kbd></button>
      <button class="r-btn-keep-repair btn primary" data-region="btn-keep-repair">
        Keep repair <kbd>(A)</kbd>
      </button>
      <button class="r-btn-keep-original btn" data-region="btn-keep-original"
        >Keep original <kbd>(R)</kbd></button
      >
      <button class="r-btn-write btn" data-region="btn-write">Write correction <kbd>(C)</kbd></button>
      <button class="r-btn-next btn" data-region="btn-next">Next line <kbd>(J)</kbd></button>
    </div>
  </main>
</div>

<style>
  /* Scaffold numbers (layout.css), bound in exact frame px (1024x576). */
  .frame {
    position: relative;
    width: 1024px;
    height: 576px;
    overflow: hidden;
    background: #191819;
    color: #f1f2f2;
    font-family: Archivo, system-ui, sans-serif;
  }

  .frame :where(a, button) {
    font: inherit;
    color: inherit;
    background: none;
    border: 0;
    padding: 0;
    cursor: pointer;
  }
  .frame :where(a, button):focus-visible {
    outline: 2px solid #e79b32;
    outline-offset: 2px;
  }
  kbd {
    font:
      500 12.5px Archivo,
      sans-serif;
    color: rgba(255, 255, 255, 0.9);
    margin-left: 4px;
  }
  .primary kbd {
    color: rgba(255, 255, 255, 0.9);
  }

  /* Top bar — sampled two-tone band */
  .topbar {
    position: absolute;
    left: 0;
    right: 0;
    top: 0;
    height: 88px;
    background: linear-gradient(180deg, #363537 0, #363537 44px, #272628 44px, #272628 88px);
  }
  .r-brand {
    position: absolute;
    left: 19px;
    top: 5.5px;
    display: flex;
    align-items: center;
    height: 28px;
    font-size: 27px;
    font-weight: 700;
    letter-spacing: -0.01em;
    color: #ffffff;
    text-decoration: none;
    white-space: nowrap;
  }
  .tab {
    appearance: none;
    color: #e8e8e6;
    font-size: 17px;
    font-weight: 330;
    letter-spacing: -0.01em;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding-bottom: 2px;
    border-bottom: 2px solid transparent;
    white-space: nowrap;
  }
  .tab:hover {
    color: #ffffff;
  }
  .r-tab-inbox {
    position: absolute;
    left: 16.4px;
    top: 44.9px;
    width: 133.1px;
    height: 35.7px;
    justify-content: flex-start;
    align-items: flex-end;
    padding-left: 7px;
    color: #ffffff;
  }
  .r-tab-shared {
    position: absolute;
    left: 131.1px;
    top: 44.9px;
    width: 94.2px;
    height: 35.7px;
    align-items: flex-end;
    padding-left: 13px;
    color: #f2f2f0;
  }
  .r-tab-shared::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    bottom: -6px;
    height: 2.5px;
    background: #6f4614;
    border-radius: 2px;
  }
  .r-tab-cluster {
    position: absolute;
    left: 239.6px;
    top: 42px;
    width: 360.4px;
    height: 46.1px;
    display: flex;
    align-items: center;
    gap: 7px;
    padding-left: 9px;
    padding-top: 6px;
    justify-content: flex-start;
  }
  .r-tab-cluster .tab {
    font-size: 17px;
    color: #e8e8e6;
  }
  .count {
    min-width: 14px;
    height: 14px;
    padding: 0 4px;
    margin-left: 6px;
    border-radius: 3px;
    background: #e68a19;
    color: #ffffff;
    font:
      600 10px/14px Archivo,
      sans-serif;
    text-align: center;
  }
  .r-healthy {
    position: absolute;
    left: 955px;
    top: 58.2px;
    height: 17.3px;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    font-size: 16px;
    font-weight: 400;
    color: #d8dad6;
    white-space: nowrap;
  }
  .r-top-icons {
    position: absolute;
    left: 895px;
    top: 9.8px;
    height: 24.8px;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding-left: 6px;
    gap: 28px;
  }
  .glyph {
    font-size: 22px;
    font-weight: 500;
    color: #99979a;
    line-height: 1;
  }
  .acct {
    width: 19px;
    height: 19px;
    border-radius: 50%;
    border: 1px solid #83838a;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .acct i {
    font-style: normal;
    font-size: 10px;
    color: #99979a;
    line-height: 1;
  }
  .r-scroll-thumb {
    position: absolute;
    left: 1003px;
    top: 60px;
    width: 16px;
    height: 82px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.1);
  }
  .r-scroll-thumb::after {
    content: '';
    position: absolute;
    left: 4px;
    top: 5px;
    width: 9px;
    height: 46px;
    border-radius: 4px;
    background: #b0b0b4;
  }

  /* The audio deck */
  .deck {
    position: absolute;
    inset: 0;
  }
  .r-band-ground {
    position: absolute;
    left: 18.4px;
    top: 125px;
    width: 973.8px;
    height: 320.3px;
    background:
      repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.022) 0 1px, transparent 1px 32px),
      repeating-linear-gradient(90deg, rgba(255, 255, 255, 0.016) 0 1px, transparent 1px 64px), #171617;
    border-radius: 6px;
  }
  .scale {
    position: absolute;
    left: 8px;
    top: 26px;
    bottom: 18px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    font:
      400 9px 'JetBrains Mono',
      monospace;
    color: #4f4f52;
  }
  .r-waveform-trace {
    position: absolute;
    left: 36.9px;
    top: 9.8px;
    width: 924.7px;
    height: 290.3px;
  }
  .wave path {
    stroke: #a5f6f5;
    stroke-width: 1.15;
    fill: none;
    opacity: 0.95;
  }
  .r-playhead-line {
    position: absolute;
    left: 485.4px;
    top: 9.8px;
    width: 10.2px;
    height: 315.6px;
  }
  .playhead .line {
    position: absolute;
    left: 50%;
    top: 0;
    bottom: 6px;
    width: 2px;
    transform: translateX(-50%);
    background: #e8a33d;
  }
  .playhead .handle {
    position: absolute;
    left: 50%;
    bottom: -5px;
    width: 11px;
    height: 11px;
    margin-left: -5.5px;
    border-radius: 50%;
    background: #f7f7f4;
    border: 2.5px solid #e8a33d;
    box-sizing: border-box;
  }
  .r-timechip {
    position: absolute;
    left: 24.6px;
    top: 271.3px;
    width: 53.2px;
    height: 34.6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font:
      700 20px 'JetBrains Mono',
      monospace;
    color: #ffffff;
    background: rgba(8, 8, 8, 0.72);
    border-radius: 6px;
  }
  .r-timeline-strip {
    position: absolute;
    left: 50.2px;
    top: 440.1px;
    width: 929.8px;
    height: 27.6px;
  }
  .ticks {
    position: absolute;
    inset: 0 0 auto 0;
    height: 5px;
    background: repeating-linear-gradient(90deg, rgba(255, 255, 255, 0.2) 0 1px, transparent 1px 28.6px);
  }
  .tick-labels {
    position: absolute;
    top: 9px;
    left: 0;
    right: 0;
    display: flex;
    justify-content: space-between;
    font:
      400 10px 'JetBrains Mono',
      monospace;
    color: #8a8a8c;
  }
  .r-listen-hint {
    position: absolute;
    left: 850px;
    top: 95px;
    width: 134px;
    height: 26px;
    font-size: 13px;
    color: #b4b6b5;
    text-align: right;
    display: flex;
    align-items: center;
    justify-content: flex-end;
  }
  .r-listen-pill {
    position: absolute;
    left: 799px;
    top: 401px;
    width: 155px;
    height: 27px;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding-left: 8px;
    font-size: 12px;
    font-weight: 500;
    color: #ffffff;
    background: rgba(16, 16, 16, 0.88);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 5px;
    overflow: hidden;
    white-space: nowrap;
  }
  .plabel {
    max-width: 112px;
    overflow: hidden;
    white-space: nowrap;
  }
  .seg {
    height: 100%;
    width: 24px;
    margin-left: auto;
    background: #333437;
    border-left: 1px solid rgba(255, 255, 255, 0.15);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .chev {
    font-size: 12px;
    color: #d5d5d3;
  }

  /* Review card */
  .r-overlay-card {
    position: absolute;
    left: 315.4px;
    top: 148px;
    width: 392.2px;
    height: 146.9px;
    background: rgba(46, 52, 51, 0.9);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    backdrop-filter: blur(2px);
  }
  .r-orig-text {
    position: absolute;
    left: 13.3px;
    top: 19px;
    width: 356.4px;
    height: 28.8px;
    margin: 0;
    font-size: 32px;
    line-height: 28.8px;
    color: #949493;
    display: block;
    white-space: nowrap;
    overflow: visible;
  }
  .orig s {
    text-decoration-color: #b9543f;
    text-decoration-thickness: 2px;
  }
  .rule {
    position: absolute;
    left: 13.3px;
    top: 51px;
    width: 356.4px;
    height: 1px;
    background: rgba(255, 255, 255, 0.12);
  }
  .r-repair-label {
    position: absolute;
    left: 18px;
    top: 66px;
    width: 110px;
    height: 24px;
    margin: 0;
    font-size: 16px;
    line-height: 1;
    color: #8a8c89;
    display: flex;
    align-items: center;
    white-space: nowrap;
  }
  .r-repair-text {
    position: absolute;
    left: 41.6px;
    top: 84px;
    height: 46px;
    margin: 0;
    font-family: Gurajada, Archivo, sans-serif;
    font-weight: 700;
    font-size: 37px;
    line-height: 1;
    color: #ffffff;
    display: flex;
    align-items: center;
    white-space: nowrap;
  }

  /* Transport deck */
  .transport {
    position: absolute;
    left: 204.8px;
    top: 488.4px;
    width: 612.4px;
    height: 47.2px;
  }
  .btn {
    position: absolute;
    top: 5.6px;
    height: 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: #525457;
    border: 1px solid #5a5c60;
    border-radius: 3px;
    font-size: 12.5px;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: #ffffff;
    white-space: nowrap;
  }
  .btn:hover {
    background: #5a5c60;
  }
  .primary {
    background: #e99b2f;
    border-color: #e99b2f;
    color: #ffffff;
  }
  .primary:hover {
    background: #f0a53c;
  }
  .r-btn-prev {
    left: 16.2px;
    width: 98px;
  }
  .r-btn-keep-repair {
    left: 119.2px;
    width: 118px;
  }
  .r-btn-keep-original {
    left: 242.2px;
    width: 120px;
  }
  .r-btn-write {
    left: 367.2px;
    width: 128px;
  }
  .r-btn-next {
    left: 500.2px;
    width: 98px;
  }
</style>
