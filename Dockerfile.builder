# dubtitle-builder — the WHOLE pipeline (GPU transcribe + LLM repair + signs/songs merge +
# Plex refresh) as one long-running, restart-safe service. Built FROM subgen for the exact
# CUDA / faster-whisper / ctranslate2 stack already proven on the 1060 (Pascal) + driver 550.
# DockHand can't build images -> build on the host and reference fasc/dubtitle-builder:latest.
FROM node:24-alpine AS review-ui
WORKDIR /ui
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run check && npm run build

FROM mccloud/subgen:2026.06.2

# subgen ships python3 + ffmpeg but no pip; bootstrap pip to add pysubs2 (for the merge step)
# and jellyfish (Metaphone, glossary.py tier-4 phonetic match -- V2 A4/A5).
# wamerican = /usr/share/dict/american-english, the English-word gate for glossary.py (C1).
# mkvtoolnix = mkvmerge for the D1 mux stage (embed .ass + fonts as a default Dubtitles track).
# webrtcvad (Timing Compare U3/T8): tools/vad.py's --vad webrtcvad backend. tools/ itself
# isn't COPY'd into this image (offline analytics, run standalone -- matches
# tools/bakeoff.py, also not baked in); installed here anyway so the dep is available if
# the tool is ever run inside this image/venv. This image's Debian python3 has a prebuilt
# wheel for it (unlike the repo's py3.14 dev venv -- see tools/vad.py's module docstring);
# if that ever regresses, drop this line and use `--vad ffmpeg-silencedetect` instead
# (dep-free, ffmpeg is already in this image).
# Versions deliberately unpinned: Debian rotates its package pool, so a pinned
# apt version stops resolving within weeks and breaks every rebuild. pip is left
# to pyproject.toml, which is where this project declares its ranges.
# hadolint ignore=DL3008,DL3013
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip wamerican mkvtoolnix git tzdata \
    && python3 -m pip install --no-cache-dir pysubs2 jellyfish \
    && (python3 -m pip install --no-cache-dir webrtcvad || echo "webrtcvad install failed -- analytics-only, use --vad ffmpeg-silencedetect; NOT fatal for generation") \
    && rm -rf /var/lib/apt/lists/*

# Bake one Whisper model into the image so the container is fully self-contained — no
# dependency on an external models bind-mount. Fetched once at build time (CPU, just to
# download the files). MODEL_DIR points generate.py at it.
#
# WHISPER_MODEL is an ARG *and* an ENV on purpose: the build bakes the model this names,
# and the same value becomes the container's runtime default, so the baked model and the
# model generate.py asks for cannot drift apart. A mismatch is not an error — faster-whisper
# would silently re-download the missing model into /models on every container start.
#
#   large-v3-turbo (default, ~1.5GB) -- fits every card this runs on at the full
#                                       beam_size=7, including the 3500g node's 1050ti 4GB
#                                       where large-v3 OOMs at beam 7 and only fits forced
#                                       down to greedy -- measurably worse there than turbo
#                                       at the full beam (flagged=76/over_cps=111 vs
#                                       flagged=35/over_cps=98, peak 1405 MiB).
#   large-v3       (~3GB)            -- the 1060 6GB box, where it fits at beam 7. Opt back
#                                       in with:
#                                         docker build -f Dockerfile.builder \
#                                           --build-arg WHISPER_MODEL=large-v3 \
#                                           -t dubtitle-builder:latest .
#
# Turbo became the default 2026-08-27: it is what the production image has been built with
# since the 1050ti swap, so the default now matches the artifact that actually ships. Its
# known quality regression is on *translation*, and REQUIRE_ENG=1 means this pipeline only
# ever transcribes English audio to English text -- it never translates.
ARG WHISPER_MODEL=large-v3-turbo
ENV WHISPER_MODEL=${WHISPER_MODEL}
ENV MODEL_DIR=/models
ENV PYTHONPATH=/app
RUN python3 -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['WHISPER_MODEL'], device='cpu', compute_type='int8', download_root='/models')"

# [S-7] the review page. EXPOSE documents it; publishing still needs -p on docker run,
# which the README's example now carries.
EXPOSE 8842
# OCI labels: what a `docker inspect` answers without starting the container. The version is NOT
# hardcoded here -- a literal copy of dubtitlerr/__init__.py's __version__ in an image would
# survive a bump and label the wrong release. Pass it at build time, from the one source:
#   docker build -f Dockerfile.builder \
#     --build-arg DUBTITLERR_VERSION=$(python3 -m dubtitlerr --version | cut -d' ' -f2) \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     -t dubtitlerr:sidecar-fork-v$(python3 -m dubtitlerr --version | cut -d' ' -f2) .
ARG DUBTITLERR_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="dubtitlerr" \
      org.opencontainers.image.version="${DUBTITLERR_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/SaxxyToo/DubTitlerr"
HEALTHCHECK --interval=5m --timeout=10s --start-period=10m --retries=3 CMD python3 -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('REVIEW_PORT', '8842'), timeout=5).status==200 else 1)"

WORKDIR /app
# The pipeline is the importable dubtitlerr package (core/, pipeline/, glossary/, review/,
# settings/); the shells stay at /app and drive stages with `python3 -m dubtitlerr.<pkg>.<mod>`.
# ENV PYTHONPATH=/app above makes that resolve everywhere, including `docker exec`.
# NOTE (V2-U3 B7/B9): common.py was missing from this COPY list since V1 introduced it --
# every `from common import ...` (generate.py, mine_glossary.py, mux.py, repair.py,
# dub_signs_merge.py) would ImportError at container start. Added here alongside the new
# data/ (EXTRA_DIRS data file) and shell/ (extras_grep_pattern lib) directories that
# merge_pass.sh now sources from $APP/shell/lib.sh + $APP/data/extras.txt.
# recreate_srt.py added: it rebuilds <stem>.eng.dubtitles.srt from the conf.json when the
# srt has already been consumed (mux removes sidecars on success). That is the ONLY way to
# re-run repair on an already-muxed episode without re-transcribing it -- repair.py returns
# "skip" when the srt is absent -- so a model/prompt change cannot be rolled out to the
# existing library without this in the image.
# publish_gate.py added (1.1): merge_pass.sh runs it by default (PUBLISH_GATE defaults to 1)
# before mux, so a missing file made the pass print "MERGE PASS COMPLETE" having delivered
# nothing -- the in-loop `exit 1` only ended that piped subshell. It sits in ENTRYPOINTS in
# tests/test_dockerfile_copy.py now, so its own imports are checked against this list too.
COPY dubtitlerr/ /app/dubtitlerr/
COPY gen_loop.sh container_run.sh merge_pass.sh /app/
COPY data/ /app/data/
COPY shell/ /app/shell/
COPY --from=review-ui /ui/dist/ /app/web/dist/
# tools/ ships for the same reason recreate_srt.py does: recover_dub_srt.py rebuilds the
# srt from the already-muxed Dubtitles track, which is the only way to regenerate an
# episode whose conf.json is gone without sending it back through Whisper.
COPY tools/ /app/tools/
RUN chmod +x /app/*.sh

# Bypass subgen's init (we only want its runtime); run our two-loop supervisor as root so
# generate.py can chown sidecars to MEDIA_UID:MEDIA_GID.
# NO `USER` ON PURPOSE. This image runs as root, and that is a requirement, not an oversight:
#   * generate.py chowns every sidecar it writes to MEDIA_UID:MEDIA_GID (default 1000:100)
#     inside a bind-mounted media library. chown needs CAP_CHOWN, which a non-root USER does
#     not have; a fixed image user would either fail to set the ownership Plex and the host
#     need, or require the library to be world-writable.
#   * the media and config bind mounts carry host ownership the image cannot know at build
#     time. The operator pins the result with MEDIA_UID/GID, not with a passwd entry.
#   * the review server writes decisions, accounts and its token under the config volume as
#     the same user that owns the sidecars.
# Dropping to USER would trade a working chown for a per-deployment rebuild. A deployment
# whose mounts are already owned by one fixed uid can run the container as that uid itself
# (`docker run --user`); the pipeline honours MEDIA_UID/GID for everything it creates.
ENTRYPOINT ["sh", "/app/container_run.sh"]
