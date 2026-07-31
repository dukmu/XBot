from slugger import make_slug


def test_ascii_slug_default():
    assert make_slug("Hello, Billing API!") == "hello-billing-api"


def test_custom_separator_kept():
    assert make_slug("Hello Billing API", separator="_") == "hello_billing_api"


def test_unicode_can_be_preserved():
    assert make_slug("Café API", preserve_unicode=True) == "café-api"


def test_empty_title_and_invalid_separator():
    assert make_slug("") == ""
    try:
        make_slug("Bad Separator", separator="/")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid separator should raise ValueError")
