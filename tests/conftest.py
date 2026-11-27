import pytest

from guest_support.store import BookingStore


@pytest.fixture
def store(tmp_path):
    return BookingStore(db_path=tmp_path / "booking.sqlite3")
