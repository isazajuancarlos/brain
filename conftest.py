"""Aísla los flags opt-in entre tests: importlib.reload + os.environ es leaky (un flag
seteado por un test se filtra al siguiente). Esta fixture los limpia antes y restaura después."""
import os
import pytest

_OPTIN = ["SHORT_FUNDING_CONTEXT", "ML_FUNDING_FEATURE"]


@pytest.fixture(autouse=True)
def _reset_optin_env():
    saved = {k: os.environ.get(k) for k in _OPTIN}
    for k in _OPTIN:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
