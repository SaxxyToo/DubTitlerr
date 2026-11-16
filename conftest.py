"""Put the repo root and tools/ on sys.path so tests can `import reflow`, `import
ordering`, `import sns_extract`, etc. regardless of pytest's import mode / invocation
directory."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tools"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_word_list_cache(monkeypatch):
    """The common-word guard is on by default, so many tests reach glossary.ordinary_words(), which caches the list and whether
    the configured file was readable. Start every test with that cache empty so one test's status never leaks into the next."""
    import glossary

    monkeypatch.setattr(glossary, "_ORDINARY", None)
    monkeypatch.setattr(glossary, "_ORDINARY_STATUS", None)


@pytest.fixture(autouse=True)
def _isolated_wiki_cache(monkeypatch, tmp_path):
    """glossary_verify keeps its adjudication journal and wiki title cache under CACHE_DIR; no test may touch a real one."""
    import glossary_verify

    monkeypatch.setattr(glossary_verify, "CACHE_DIR", str(tmp_path / "wiki_cache"))


@pytest.fixture(autouse=True)
def _accounts_are_installed_by_default(request, monkeypatch, tmp_path_factory):
    """Suites that predate accounts assert 401 for a missing token, which is the behaviour of an instance that has accounts.
    First run (409 setup required) is exercised only by files marked real_accounts, which get the real functions."""
    if request.node.get_closest_marker("real_accounts"):
        return
    import review_auth

    monkeypatch.setattr(review_auth, "setup_required", lambda: False)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path_factory.mktemp("auth_config")))  # even when the shell or CI already set one


@pytest.fixture(autouse=True)
def _deterministic_word_list(monkeypatch, tmp_path_factory):
    """The word guards must not consult the HOST's /usr/share/dict.

    CI's runner has no wamerican, so glossary.is_ordinary_word()/is_english() run on the
    bundled 228-word list. A dev box usually has a real list, and that silently changes
    behaviour: 'government' and 'burn' become ordinary words, which blocks expansions and
    skips LLM pairs. On this machine that alone turned 31 tests red. Pin an empty list so
    every host runs the bundled baseline CI runs. test_common_word_guard sets its own.
    """
    import glossary

    empty = tmp_path_factory.mktemp("wordlist") / "empty.txt"
    empty.write_text("")
    # Both, deliberately: the attribute covers modules already imported in this process,
    # and the environment covers the subprocess tests (a child imports glossary fresh and
    # reads WORDLIST_PATH from its environment).
    monkeypatch.setenv("WORDLIST_PATH", str(empty))
    monkeypatch.setattr(glossary, "WORDLIST_PATH", str(empty))
    monkeypatch.setattr(glossary, "_WORDS", None)  # is_english's own cache
