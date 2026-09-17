"""
parsing.py -- the defensive boundary around model output.

The contract under test is unusual and worth stating: parse_lead_json NEVER
raises. A language model is a probabilistic text generator, not an API, so
malformed output is an expected input here rather than an exceptional one.
Every case below is something a real VLM has actually returned.
"""

import pytest

from app.parsing import parse_lead_json
from app.schema import LEAD_FIELDS

CLEAN = '{"first_name":"Asha","last_name":"Rao","title":"Head of Sales",' \
        '"company":"Nimbus","location":"Mumbai","phone":"+91 98200 12345",' \
        '"email":"asha@nimbus.in"}'


def test_clean_json_parses():
    fields, error = parse_lead_json(CLEAN)
    assert error is None
    assert fields["first_name"] == "Asha"
    assert fields["email"] == "asha@nimbus.in"


def test_every_lead_field_is_present_even_when_the_model_omits_them():
    # Downstream code indexes these keys unconditionally; a missing key would
    # be a KeyError in the Excel writer, long after the cause.
    fields, error = parse_lead_json('{"first_name":"Solo"}')
    assert error is None
    assert set(fields) == set(LEAD_FIELDS)
    assert fields["email"] is None


@pytest.mark.parametrize("raw", [
    f"```json\n{CLEAN}\n```",                       # the commonest failure
    f"```\n{CLEAN}\n```",
    f"Here are the details you asked for:\n\n{CLEAN}\n\nHope that helps!",
    f"Sure!\n```json\n{CLEAN}\n```\nLet me know if you need anything else.",
])
def test_json_is_recovered_from_fences_and_prose(raw):
    fields, error = parse_lead_json(raw)
    assert error is None, f"failed to recover from: {raw[:40]!r}"
    assert fields["first_name"] == "Asha"


def test_nested_braces_do_not_confuse_the_extractor():
    # A regex cannot match balanced brackets; this is why parsing.py counts.
    raw = 'Result: {"first_name":"Asha","last_name":"Rao","title":null,' \
          '"company":"Nimbus {Labs}","location":null,"phone":null,"email":null} done'
    fields, error = parse_lead_json(raw)
    assert error is None
    assert fields["company"] == "Nimbus {Labs}"


def test_non_string_values_are_coerced_not_rejected():
    # Models routinely return a phone as an int and a location as a list.
    raw = '{"first_name":"Mei","last_name":null,"title":null,"company":null,' \
          '"location":["Mumbai","India"],"phone":9820012345,"email":null}'
    fields, error = parse_lead_json(raw)
    assert error is None
    assert isinstance(fields["phone"], str) and "9820012345" in fields["phone"]
    assert isinstance(fields["location"], str)


def test_python_literals_and_trailing_commas_are_repaired():
    raw = '{"first_name": "Asha", "last_name": None, "email": "a@b.co",}'
    fields, error = parse_lead_json(raw)
    assert error is None
    assert fields["first_name"] == "Asha"
    assert fields["last_name"] is None


def test_single_quoted_python_repr_is_NOT_recovered():
    """
    A documented limitation, pinned so that changing it has to be deliberate.

    A model that emits a Python dict repr -- {'first_name': 'Asha'} -- is not
    recovered, and that is a choice rather than an oversight. Converting single
    quotes to double quotes textually cannot be done safely without a real
    parser: the apostrophe in O'Brien, or in "l'Oreal", turns a naive
    replacement into corrupted output that still parses. Silently mangling a
    name is worse than a flagged row that says the reply was unreadable, so
    this case is left to fail loudly.
    """
    fields, error = parse_lead_json("{'first_name': 'Asha'}")
    assert fields is None
    assert error


@pytest.mark.parametrize("raw,label", [
    ("",                                    "empty reply"),
    ("   \n\t  ",                           "whitespace only"),
    ('{"first_name":"Asha","last_nam',      "truncated mid-object"),
    ("I'm sorry, I can't read this image.", "a refusal in prose"),
    ("null",                                "valid JSON that is not an object"),
    ("[1, 2, 3]",                           "valid JSON of the wrong shape"),
])
def test_unusable_output_returns_an_error_and_never_raises(raw, label):
    fields, error = parse_lead_json(raw)
    assert fields is None, f"{label} should not have parsed"
    assert error, f"{label} must explain itself"
    assert isinstance(error, str)


def test_parser_never_raises_on_hostile_input():
    # Belt and braces: the whole point of this module is that the caller can
    # rely on a return value rather than a try/except.
    for raw in ["{" * 500, "}" * 500, "\x00\x01\x02", '{"a":' + '"x"' * 5000]:
        fields, _error = parse_lead_json(raw)
        assert fields is None or isinstance(fields, dict)
