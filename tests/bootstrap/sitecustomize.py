import os

if os.environ.get("REPOKEEP_LIVE_TESTS") != "1":
    from tests.offline import install

    install()
