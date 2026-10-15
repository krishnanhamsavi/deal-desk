from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.mark.parametrize("page", ["Overview", "Desk queue", "New request", "Leakage analytics", "Pricing policy"])
def test_page_renders(page):
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
