import os
import sys
import tempfile
from pathlib import Path

# Isolate every test run from the developer's real settings/signal database.
_TMP = tempfile.mkdtemp(prefix="signalforge-tests-")
os.environ.setdefault("TSA_DATA_DIR", _TMP)
os.environ.setdefault("TSA_HTTP_TIMEOUT", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.core.data.synthetic import generate_candles  # noqa: E402


@pytest.fixture(scope="session")
def candles():
    return generate_candles("BTCUSDT", "1h", 600, now=1_800_000_000)


@pytest.fixture()
def data_dir():
    return Path(os.environ["TSA_DATA_DIR"])
