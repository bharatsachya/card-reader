"""
postprocess.py -- the deterministic half that runs after the model.

The design claim being tested: the model reliably FINDS a phone number and
unreliably FORMATS it, so formatting is done in code where it is reproducible
and testable. These tests are what make that claim checkable.
"""

import pytest

from app.postprocess import normalise_email, normalise_phone, postprocess_fields

# --- phones ---------------------------------------------------------------

def test_domestic_indian_number_gains_the_default_region_code():
    # DEFAULT_PHONE_REGION=IN, and most domestic cards print no country code.
    assert normalise_phone("98200 12345") == "+919820012345"


@pytest.mark.parametrize("printed", [
    "+91 98200 12345",
    "+91-98200-12345",
    "+91 (98200) 12345",
    "  +919820012345  ",
])
def test_punctuation_and_spacing_do_not_change_the_result(printed):
    assert normalise_phone(printed) == "+919820012345"


@pytest.mark.parametrize("printed,country_code", [
    ("+1 415 555 2671",  "+1"),    # US
    ("+44 20 7946 0958", "+44"),   # UK
    ("+49 30 901820",    "+49"),   # DE
    ("+81 3-5555-0107",  "+81"),   # JP
])
def test_international_numbers_keep_their_own_country_code(printed, country_code):
    """
    A number that prints its own +country must NOT be reinterpreted as Indian.

    This is the case that breaks if DEFAULT_PHONE_REGION is applied blindly:
    a German number parsed as Indian either fails validation and gets dropped,
    or worse, validates into a completely different real number.
    """
    result = normalise_phone(printed)
    assert result.startswith(country_code), f"{printed} -> {result}"
    assert not result.startswith("+91"), f"{printed} was reinterpreted as Indian"
    assert result.replace("+", "").isdigit()


def test_unparseable_text_is_kept_rather_than_discarded():
    """
    Deliberate: losing data the model read correctly is worse than leaving it
    unformatted. A human can still act on "ext. 402".
    """
    assert normalise_phone("ext. 402") == "ext. 402"


def test_a_postcode_mistaken_for_a_phone_is_not_invented_into_one():
    # is_valid_number() checks against the real numbering plan, which is what
    # stops five digits becoming a plausible-looking E164 number.
    result = normalise_phone("400001")
    assert not result.startswith("+91400001")


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_blank_phone_stays_blank(empty):
    assert normalise_phone(empty) in (None, "")


# --- emails ---------------------------------------------------------------

@pytest.mark.parametrize("printed,expected", [
    ("ASHA@NIMBUS.IN",            "asha@nimbus.in"),
    ("Asha.Rao@Nimbus.In",        "asha.rao@nimbus.in"),
    ("mailto:asha@nimbus.in",     "asha@nimbus.in"),
    ("  asha@nimbus.in  ",        "asha@nimbus.in"),
    ("asha@nimbus.in.",           "asha@nimbus.in"),
    ("asha (at) nimbus.in",       "asha@nimbus.in"),
])
def test_email_normalisation(printed, expected):
    assert normalise_email(printed) == expected


def test_obviously_broken_email_is_not_silently_invented():
    # Better a flagged oddity a human can see than a confidently wrong address.
    assert normalise_email("not an email") != "not@an.email"


# --- the whole dict -------------------------------------------------------

def test_postprocess_fields_preserves_every_key():
    fields = {
        "first_name": "  Asha ", "last_name": "Rao", "title": None,
        "company": "Nimbus", "location": "Mumbai",
        "phone": "98200 12345", "email": "ASHA@NIMBUS.IN",
    }
    out = postprocess_fields(fields)
    assert set(out) == set(fields)
    assert out["first_name"] == "Asha"          # whitespace trimmed
    assert out["phone"] == "+919820012345"
    assert out["email"] == "asha@nimbus.in"
