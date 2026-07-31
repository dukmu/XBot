from client import normalize_user


def test_normalize_v1_user():
    assert normalize_user({"id": "u1", "full_name": "Ada", "email": "ada@example.com", "plan": "pro"}) == {
        "id": "u1",
        "name": "Ada",
        "email": "ada@example.com",
        "plan": "pro",
    }


def test_normalize_v2_user_with_null_email():
    row = {"userId": "u2", "profile": {"displayName": "Noor", "email": None}, "subscription": {"plan": "free"}}
    assert normalize_user(row) == {"id": "u2", "name": "Noor", "email": None, "plan": "free"}
