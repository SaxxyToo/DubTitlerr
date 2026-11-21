"""DubTitlerr pipeline package (F2 restructure).

Modules are CLI-first; import submodules directly (e.g. ``from dubtitlerr.core import common``).
This __init__ must stay import-free: modules on the shell-call path (review_requests,
review_settings_write, glossary_lock, watch_queue) are imported in minimal app dirs whose
python3 may lack pysubs2/jellyfish.

``__version__`` is the ONE version of this fork: pyproject.toml resolves it (dynamic attr), the
/api/v1 status route reports it, the container banner and ``python3 -m dubtitlerr --version``
print it, and the image labels are built from it. Bump it here and nowhere else -- a literal
copy anywhere else is drift, and tests/test_version.py fails on drift. The assignment must stay
a bare literal (no import, no computation) both for the import-free rule above and because
setuptools reads it statically.
"""

__version__ = "0.4.12"
