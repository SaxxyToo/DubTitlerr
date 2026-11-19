"""DubTitlerr pipeline package (F2 restructure).

Modules are CLI-first; import submodules directly (e.g. ``from dubtitlerr.core import common``).
This __init__ must stay import-free: modules on the shell-call path (review_requests,
review_settings_write, glossary_lock, watch_queue) are imported in minimal app dirs whose
python3 may lack pysubs2/jellyfish.
"""
