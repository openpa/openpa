from app.utils.formatting import dict_to_text


def test_flat_dict():
    data = {"name": "Alice", "age": 30}
    result = dict_to_text(data)
    assert result == "name: Alice\nage: 30"


def test_nested_dict():
    data = {"address": {"city": "Paris", "zip": "75001"}}
    result = dict_to_text(data)
    assert result == "address:\n  city: Paris\n  zip: 75001"


def test_list_of_scalars():
    data = {"tags": ["a", "b", "c"]}
    result = dict_to_text(data)
    assert result == "tags:\n  - a\n  - b\n  - c"


def test_list_of_dicts():
    data = {"items": [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]}
    result = dict_to_text(data)
    expected = "items:\n  - id: 1\n    name: x\n  - id: 2\n    name: y"
    assert result == expected


def test_empty_dict():
    assert dict_to_text({}) == ""


def test_mixed_nesting():
    data = {
        "user": "Alice",
        "contacts": [{"email": "a@b.com", "phone": "123"}],
        "settings": {"theme": "dark", "notifications": True},
    }
    result = dict_to_text(data)
    expected = (
        "user: Alice\n"
        "contacts:\n"
        "  - email: a@b.com\n"
        "    phone: 123\n"
        "settings:\n"
        "  theme: dark\n"
        "  notifications: True"
    )
    assert result == expected


def test_indent_parameter():
    data = {"key": "value"}
    result = dict_to_text(data, indent=2)
    assert result == "    key: value"


def test_json_string_value_parsed():
    """String values that are valid JSON dicts/lists should be parsed and formatted."""
    import json
    inner = {"query": "open claw", "total_matches": 1, "results": [{"name": "Doc.pdf", "size": 100}]}
    data = {"search_files": json.dumps(inner)}
    result = dict_to_text(data)
    assert '"query"' not in result  # raw JSON should not appear
    assert "query: open claw" in result
    assert "- name: Doc.pdf" in result


def test_plain_string_value_unchanged():
    """Regular string values should not be altered."""
    data = {"message": "hello world"}
    result = dict_to_text(data)
    assert result == "message: hello world"
