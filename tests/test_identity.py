import pytest

from app.identity import PhoneNormalisationError, normalise_e164


@pytest.mark.parametrize("raw,expected", [
    ("+919876543210", "+919876543210"),
    ("919876543210", "+919876543210"),
    ("09876543210", "+919876543210"),
    ("9876543210", "+919876543210"),
    ("00919876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("098765-43210", "+919876543210"),
])
def test_all_shapes_collapse_to_one(raw, expected):
    assert normalise_e164(raw) == expected


def test_one_person_is_not_split_into_two():
    shapes = ["+919876543210", "09876543210", "9876543210", "91 98765 43210"]
    assert len({normalise_e164(s) for s in shapes}) == 1


def test_empty_is_rejected():
    with pytest.raises(PhoneNormalisationError):
        normalise_e164("   ")
