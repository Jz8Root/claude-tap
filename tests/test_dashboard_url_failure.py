"""Successful parsing stays compatible; failed parsing hides the current string."""

import json
from copy import deepcopy
from itertools import product
from urllib.parse import quote_plus

import aiohttp
import pytest

from claude_tap import dashboard
from claude_tap.live import LiveViewerServer
from claude_tap.trace_store import get_trace_store


def redact_records_for_display(records: list[dict]) -> list[dict]:
    """Exercise low-level recovery only within an explicit display scope."""
    saved = deepcopy(records)
    with dashboard.dashboard_redaction_scope():
        result = dashboard.redact_dashboard_records(records)
    assert records == saved
    return result


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://host/path?token=secret&label=public ", "https://host/path?token=REDACTED&label=public+"),
        ("https://host/path?token=secret\t", "https://host/path?token=REDACTED"),
        ("HTTPS://host/path?token=secret#", "https://host/path?token=REDACTED"),
        (" https://host/path?token=secret", "https://host/path?token=REDACTED"),
        ("/path?token=&flag", "/path?token=REDACTED&flag="),
        ("?token=secret", "?token=REDACTED"),
        ("relative/path?token=secret", "relative/path?token=REDACTED"),
        ("https://host/path?access_\ntoken=secret", "https://host/path?access_token=REDACTED"),
        ("token=secret&label=public", "token=REDACTED&label=public"),
        ("plain text without a URI", "plain text without a URI"),
        (
            "const result = ready ? token =~/expected/ : fallback;",
            "const result = ready ? token =~/expected/ : fallback;",
        ),
        (
            '{"url":"https://host/path?token=secret","label":"public"}',
            '{"url":"https://host/path?token=REDACTED","label":"public"}',
        ),
    ],
)
def test_normal_redaction_retains_original_outputs_without_recovery(monkeypatch, value: str, expected: str) -> None:
    original_urlsplit = dashboard.urlsplit
    parsed_values = []

    def observed_urlsplit(value, *args, **kwargs):
        parsed_values.append(value)
        return original_urlsplit(value, *args, **kwargs)

    monkeypatch.setattr(dashboard, "urlsplit", observed_urlsplit)
    record = {"content": value}
    assert dashboard.redact_dashboard_records([record]) == [{"content": expected}]
    assert all(candidate in value for candidate in parsed_values)
    assert record == {"content": value}


@pytest.mark.parametrize("authority", ["https://host", "https://host：443", "https://[broken"])
def test_only_successful_authorities_delegate_to_the_original_query_core(monkeypatch, authority: str) -> None:
    original_query_core = dashboard._redact_query_string
    queries = []

    def observed_query_core(query, *args, **kwargs):
        queries.append(query)
        return original_query_core(query, *args, **kwargs)

    monkeypatch.setattr(dashboard, "_redact_query_string", observed_query_core)
    query = "token=actual-secret&public=keep"
    value = authority + "/path?" + query
    expected = authority + "/path?token=REDACTED&public=keep" if authority == "https://host" else "REDACTED"
    assert redact_records_for_display([{"content": value}]) == [{"content": expected}]
    assert queries == ([query] if authority == "https://host" else [])


def test_original_comment_failure_hides_field_without_reparsing_source(monkeypatch) -> None:
    original_urlsplit = dashboard.urlsplit
    parsed_values = []

    def observed_urlsplit(value, *args, **kwargs):
        parsed_values.append(value)
        return original_urlsplit(value, *args, **kwargs)

    monkeypatch.setattr(dashboard, "urlsplit", observed_urlsplit)
    text = "// Deliverables： only\nconst result = ready ? token === expected : fallback;\n"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]
    assert parsed_values == [text]


@pytest.mark.parametrize("authority", ["https://host：443", "https://[broken", "//host：443"])
def test_public_malformed_whole_uri_hides_the_failed_leaf(authority: str) -> None:
    text = authority + "/path%2fpart?label=one%20two&flag#section"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


@pytest.mark.parametrize(
    "expression", ["token === expected", "token =~/expected/", "left : right", "token=actual-secret"]
)
@pytest.mark.parametrize("comment", ["// Deliverables： only", "//Deliverables： only", "//note："])
def test_failed_source_parse_does_not_run_assignment_grammar(expression: str, comment: str) -> None:
    text = f"{comment}\nconst result = ready ? {expression} : fallback;"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


@pytest.mark.parametrize("authority", ["https://host：443", "https://[broken", "//host：443"])
@pytest.mark.parametrize("credential", ["actual-secret", "==actual-secret", "~actual-secret"])
def test_failed_uri_authorities_hide_all_components(authority: str, credential: str) -> None:
    value = f"{authority}/path?token={credential}&label=public#keep"
    record = {"content": value}
    assert redact_records_for_display([record]) == [{"content": "REDACTED"}]
    assert record == {"content": value}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("payload=client_secret%3Dactual-secret", "payload=client_secret%3DREDACTED"),
        (
            "payload=%7B%22client_secret%22%3A%22actual-secret%22%2C%22public%22%3A%22keep%22%7D",
            "payload=%7B%22client_secret%22%3A%22REDACTED%22%2C%22public%22%3A%22keep%22%7D",
        ),
        ('payload={"client_secret":"actual-secret"}', "payload=%7B%22client_secret%22%3A%22REDACTED%22%7D"),
        ("access_\ntoken=actual-secret&label=public", "access_token=REDACTED&label=public"),
        ("%74oken=actual-secret&label=public", "token=REDACTED&label=public"),
    ],
)
def test_failed_uri_nested_query_inputs_hide_the_failed_leaf(query: str, expected: str) -> None:
    value = "https://host：443/path?" + query + "#keep"
    record = {"content": value}
    assert redact_records_for_display([record]) == [{"content": "REDACTED"}]
    normal = "https://host/path?" + query + "#keep"
    assert dashboard.redact_dashboard_records([{"content": normal}]) == [
        {"content": "https://host/path?" + expected + "#keep"}
    ]
    assert record["content"] == value


@pytest.mark.parametrize("quote", ["'", '"', "`"])
@pytest.mark.parametrize(
    "uri",
    [
        "https://host/path",
        "https://host：443/path",
        "//host：443/path",
        "/path",
        "relative/path",
        "",
        "mailto:public@example.com",
    ],
)
@pytest.mark.parametrize("value", ["==actual-secret", "~actual-secret"])
def test_source_with_quoted_uris_hides_the_failed_string(quote: str, uri: str, value: str) -> None:
    prefix = "// Deliverables： only\nconst uri = "
    suffix = ";\nconst untouched = ready ? token =~/expected/ : fallback;"
    text = prefix + quote + uri + "?token=" + value + "&label=public#keep" + quote + suffix
    result = redact_records_for_display([{"old_string": text}])[0]["old_string"]
    assert "actual-secret" not in result
    assert result == "REDACTED"


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ('"https://host：443/path?token=actual-secret;', "REDACTED"),
        ('"//host：443/path?token=actual-secret;', "REDACTED"),
        ('"/path?token=actual-secret;', "REDACTED"),
        ('"relative/path?token=actual-secret;', "REDACTED"),
        ('https://host：443/path?payload={"token":"actual-secret"};', "REDACTED"),
        ('"https://host：443/path?payload={"token":"actual-secret"}";', "REDACTED"),
        (r'"https://host：443/path?access_\u0074oken=actual-secret";', "REDACTED"),
        (r'"https:\/\/host：443/path?token=actual-secret";', "REDACTED"),
    ],
)
def test_failed_source_literal_inputs_hide_the_current_string(literal: str, expected: str) -> None:
    prefix = "// Deliverables： only\nconst uri = "
    text = prefix + literal
    record = {"old_string": text}
    assert redact_records_for_display([record]) == [{"old_string": expected}]
    assert record == {"old_string": text}


@pytest.mark.parametrize("control", ["\t", "\r", "\n", "\r\n"])
@pytest.mark.parametrize(
    "query", ["access_{control}token=actual-secret", "payload=client_se{control}cret=actual-secret"]
)
def test_failed_source_with_quoted_query_controls_hides_the_current_string(control: str, query: str) -> None:
    prefix = "// Deliverables： only\nconst uri = "
    suffix = ";\nconst keep = ready ? token =~/expected/ : fallback;"
    text = prefix + '"https://host：443/path?' + query.format(control=control) + '"' + suffix
    record = {"old_string": text}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": text}


def test_failed_source_with_nested_json_hides_the_current_string() -> None:
    prefix = "// Deliverables： only\nconst uri = 'https://host：443/path?"
    suffix = "';\nconst keep = 1;"
    text = prefix + 'payload={"token":"actual-secret","public":"ok"}' + suffix
    record = {"old_string": text}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": text}


@pytest.mark.parametrize(
    ("prefix", "expected_prefix"),
    [
        ("https\n://host：443/path", None),
        ("https\r\n://host：443/path", None),
        ("\x00//host：443/path", "//host：443/path"),
        ("//ho\nst：443/path", None),
        ("//ho\tst：443/path", None),
    ],
)
def test_failed_prefix_controls_hide_the_current_string(prefix: str, expected_prefix: str | None) -> None:
    text = prefix + "?token=actual-secret&label=public"
    record = {"old_string": text}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": text}


@pytest.mark.parametrize("prefix", ["//h：\n", "//ho\nst：443", "//ho\r\nst：443"])
@pytest.mark.parametrize("credential", ["s", "actual-secret", "==actual-secret"])
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_failed_multiline_network_prefix_hides_the_current_string(prefix: str, credential: str, quote: str) -> None:
    uri = prefix + "?token=" + credential + "&public=keep#fragment"
    before = "// Deliverables： only\nconst uri=" if quote else ""
    after = ";\nconst keep=1;" if quote else ""
    text = before + quote + uri + quote + after
    record = {"old_string": text}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": text}


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_failed_multiline_sensitive_query_hides_the_current_string(quote: str) -> None:
    prefix = "//ho\nst：443?"
    before = "// Deliverables： only\nconst uri=" if quote else ""
    after = ";\nconst keep=1;" if quote else ""
    text = before + quote + prefix + "public=(token==actual-secret)" + quote + after
    record = {"old_string": text}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": text}


@pytest.mark.parametrize(
    ("expression", "masked_query"),
    [("token===expected", "REDACTED"), ("token =~/expected/ : fallback;", "REDACTED")],
)
def test_uri_shaped_multiline_comment_uses_privacy_first_query_policy(expression: str, masked_query: str) -> None:
    prefix = "//note：\nready?"
    text = prefix + expression
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": masked_query}]


@pytest.mark.parametrize(
    "literal",
    [
        '"https://host：443/path?token=actual-secret;\nconst title="public";\nconst keep=1;',
        '\'https://host：443/path?payload={"token":"before\'actual-secret"}\';\nconst keep=1;',
    ],
)
def test_failed_source_with_conflicting_quotes_hides_the_current_string(literal: str) -> None:
    prefix = "// Deliverables： only\nconst uri = "
    record = {"old_string": prefix + literal}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": prefix + literal}


@pytest.mark.parametrize(
    "literal",
    [
        r'"https:\/\/host：443/path?token=actual-secret"',
        r'"https://host：443/path?tok\u0065n=actual-secret"',
        r'"https\u003a//host/path?token=actual-secret"',
        r'"https:\u002f\u002fhost/path?token=actual-secret"',
        r'"https:\x2f\x2fhost/path?token=actual-secret"',
    ],
)
def test_failed_source_with_escapes_hides_the_current_string(literal: str) -> None:
    prefix = "// Deliverables： only\nconst uri = "
    suffix = ';\nconst title="public";\nconst keep=1;'
    record = {"old_string": prefix + literal + suffix}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": prefix + literal + suffix}


def test_failed_source_with_nested_form_hides_the_current_string() -> None:
    prefix = "// Deliverables： only\nconst uri = "
    literal = "'https://host/path?payload=\"client_secret=actual-secret\"'"
    suffix = ";\nconst keep=1;"
    record = {"old_string": prefix + literal + suffix}
    assert redact_records_for_display([record]) == [{"old_string": "REDACTED"}]
    assert record == {"old_string": prefix + literal + suffix}


def test_failed_network_authority_with_trailing_space_hides_the_current_string() -> None:
    text = "//host：443 /path?token=actual-secret"
    record = {"old_string": text}
    expected = [{"old_string": "REDACTED"}]
    assert redact_records_for_display([record]) == expected
    assert redact_records_for_display(expected) == expected
    assert record == {"old_string": text}


@pytest.mark.parametrize("path", ["", "/path", "/path;param=value"])
@pytest.mark.parametrize("quote", ["", "'", '"', "`"])
def test_network_uri_variations_never_expose_query_credentials(path: str, quote: str) -> None:
    # Independent input dimensions exercise the URI grammar, not the recovery regex.
    for delimiter in "!$&'()*+,;=:":
        uri = f"//user{delimiter}name@host：443{path}?access_token=actual-secret&public=keep#fragment"
        if quote:
            content = uri.replace(quote, "\\" + quote)
            text = "// Deliverables： only\nconst u = " + quote + content + quote + ";\nconst keep = 1;"
        else:
            text = uri
        record = {"old_string": text}
        output = redact_records_for_display([record])[0]["old_string"]
        assert "actual-secret" not in output, (delimiter, path, quote, output)
        assert "REDACTED" in output
        assert output == "REDACTED"
        assert record == {"old_string": text}


def test_failed_uri_query_combinations_hide_the_current_string() -> None:
    authorities = [
        "//host：443",
        "//user=name@host：443",
        "//[broken",
        "https://host：443",
        "https://[broken",
        "//host／part",
        "//host＠name",
        "//host＃part",
    ]
    queries = [
        "access_token=actual-secret",
        "%61ccess_token=actual-secret",
        "access_\ntoken=actual-secret",
        "payload=" + quote_plus("client_secret=actual-secret"),
        "payload=" + quote_plus('{"token":"actual-secret"}'),
    ]
    for authority, path, query, quote in product(authorities, ["", "/path"], queries, ["", "'", '"', "`"]):
        uri = authority + path + "?" + query + "&public=keep"
        before = "// Deliverables： only\nconst u = " + quote if quote else ""
        after = quote + ";\nconst keep = 1;" if quote else ""
        text = before + uri + after
        output = redact_records_for_display([{"old_string": text}])[0]["old_string"]
        assert "actual-secret" not in output, (text, output)
        assert output == "REDACTED"
        assert "REDACTED" in output


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_failed_source_with_multiple_uri_literals_hides_the_current_string(quote: str) -> None:
    first = "//user=name@host：443?token=first-secret"
    second = "//host：443?token=second-secret"
    text = f"// Deliverables： only\nconst a = {quote}{first}{quote}; const b = {quote}{second}{quote};"
    output = redact_records_for_display([{"old_string": text}])[0]["old_string"]
    assert "first-secret" not in output
    assert "second-secret" not in output
    assert output == "REDACTED"


@pytest.mark.parametrize("comment", ["// Verify： ready", "//Verify： ready", "// Verify："])
@pytest.mark.parametrize("operator", ["===", "==", "=~", "!=", "<=", ">="])
def test_same_line_comment_query_hides_the_failed_string(comment: str, operator: str) -> None:
    text = f"{comment}? token {operator} expected\nconst keep = 1;"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_failed_source_with_unclosed_network_literal_hides_the_current_string(quote: str) -> None:
    prefix = "// Deliverables： only\nconst u = "
    text = prefix + quote + "//user=name@host：443?access_token=actual-secret;"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


def test_escaped_failed_network_authority_hides_the_current_string() -> None:
    text = r"//user\name@host：443?access_token=actual-secret"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


def test_failed_first_parse_never_enters_the_query_core(monkeypatch) -> None:
    # A failed first parse must short-circuit before the query layer.
    def failed_query(*args, **kwargs):
        pytest.fail("Failed URL parsing must not enter query redaction")

    monkeypatch.setattr(dashboard, "_redact_query_string", failed_query)
    for value in (
        "//user=name@host：443?access_token=actual-secret",
        '// Deliverables： only\nconst u = "//host：443?access_token=actual-secret";',
        "// Deliverables： only\nconst u = `//host：443?access_token=actual-secret`;",
    ):
        output = redact_records_for_display([{"old_string": value}])[0]["old_string"]
        assert output == "REDACTED"


@pytest.mark.parametrize(
    "template",
    [
        "`//${host}/path?token=${`actual-secret`}`",
        "`${base}?token=actual-secret`",
        r"`${base}?tok\u0065n=${`actual-secret`}`",
        "`https://${`host`}/path?token=actual-secret`",
        "`${getBase(`prod`)}/path?token=actual-secret`",
        "`${`https://host/path`}?token=actual-secret`",
        "`${ready ? `https://host#fragment` : base}/path?token=actual-secret`",
    ],
)
def test_dynamic_template_does_not_claim_a_static_literal_boundary(template: str) -> None:
    text = "// Deliverables： only\nconst u = " + template + ";"
    output = redact_records_for_display([{"old_string": text}])[0]["old_string"]
    assert output == "REDACTED"


@pytest.mark.parametrize("quote", ["'", '"', "`"])
@pytest.mark.parametrize(
    "uri", ["//host：443 ?token=actual-secret", "//ho st：443?token=actual-secret", " //host：443?token=actual-secret"]
)
def test_failed_literal_context_with_authority_spaces_hides_the_current_string(quote: str, uri: str) -> None:
    text = "// Deliverables： only\nconst u = " + quote + uri + quote + ";\nconst keep = 1;"
    output = redact_records_for_display([{"old_string": text}])[0]["old_string"]
    assert "actual-secret" not in output
    assert output == "REDACTED"


def test_failed_trailing_authority_space_hides_the_current_string() -> None:
    uri = "//host：443 ?token=actual-secret"
    expected = [{"old_string": "REDACTED"}]
    assert redact_records_for_display([{"old_string": uri}]) == expected
    assert redact_records_for_display(expected) == expected


@pytest.mark.parametrize("quote", ["'", '"', "`"])
@pytest.mark.parametrize(
    "uri",
    [
        " /path?token=actual-secret",
        r"https\u003a\u002f\u002fhost?access_token=actual-secret",
        r"https:\/\/host?token=actual-secret",
    ],
)
def test_failed_closed_and_unclosed_literals_hide_the_current_string(quote: str, uri: str) -> None:
    prefix = "// Deliverables： only\nconst u = "
    for closing in [quote, ""]:
        text = prefix + quote + uri + closing + ";"
        output = redact_records_for_display([{"old_string": text}])[0]["old_string"]
        assert "actual-secret" not in output
        assert output == "REDACTED"


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_failed_source_with_dangling_escape_hides_the_current_string(quote: str) -> None:
    prefix = "// Deliverables： only\nconst u = "
    text = prefix + quote + r"https\u003a\u002f\u002fhost?access_token=actual-secret" + "\\"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


@pytest.mark.parametrize("quote", ["'", '"', "`"])
@pytest.mark.parametrize("comment", ["// Don't： change", '// Say "hello：', "// Use `literal："])
def test_failed_comment_quotes_hide_the_current_string(quote: str, comment: str) -> None:
    text = comment + "\nconst u = " + quote + "/path?token=actual-secret" + quote + ";\nconst keep = 1;"
    output = redact_records_for_display([{"old_string": text}])[0]["old_string"]
    assert "actual-secret" not in output
    assert output == "REDACTED"


@pytest.mark.parametrize("template", ['`${ready ? "ready" : "waiting"}`', "`Hello ${name}`"])
def test_exceptional_templates_keep_the_original_question_mark_gate(template: str) -> None:
    text = "// Deliverables： only\nconst message = " + template + ";\nconst keep = 1;"
    expected = "REDACTED" if "?" in template else text
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": expected}]
    normal = text.removeprefix("// Deliverables： only\n")
    assert dashboard.redact_dashboard_records([{"old_string": normal}]) == [{"old_string": normal}]


@pytest.mark.parametrize(
    "template", ["`Hello ${name}`", '`${ready ? "ready" : "waiting"}`', '`Hello ${{name: "}"}.name}`']
)
def test_failed_interpolation_hides_the_current_string(template: str) -> None:
    text = (
        "// Deliverables： only\nconst label = " + template + ";\nconst result = ready ? token === expected : fallback;"
    )
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


@pytest.mark.parametrize("expression", ['"#".slice(1)', 'opts["#"]', "getBase(`host#fragment`)"])
def test_failed_dynamic_expression_fragment_hides_the_current_string(expression: str) -> None:
    prefix = "// Deliverables： only\nconst u = "
    text = prefix + "`https://host/path?public=${" + expression + "}&token=actual-secret`;\nconst keep = 1;"
    output = redact_records_for_display([{"old_string": text}])[0]["old_string"]
    assert "actual-secret" not in output
    assert output == "REDACTED"


def test_failed_source_with_regex_template_hides_the_current_string() -> None:
    text = "// Deliverables： only\nconst u = `${/}/.test(x) ? `host` : base}/path?token=actual-secret`;"
    assert redact_records_for_display([{"old_string": text}]) == [{"old_string": "REDACTED"}]


@pytest.mark.parametrize(
    ("text", "secret", "expected_text"),
    [
        (
            "//user=name@host：443?access_token=actual-secret",
            "actual-secret",
            "REDACTED",
        ),
        (
            "// Deliverables： only\nconst u = `//host：443/path?access_token=actual-secret`;",
            "actual-secret",
            "REDACTED",
        ),
        ("// Verify： ready? token === expected\nconst keep = 1;", None, "REDACTED"),
        ("// Deliverables： only\nconst result = ready ? token =~/expected/ : fallback;", None, "REDACTED"),
        (
            "https://host：443/path?token=actual-secret&label=public",
            "actual-secret",
            "REDACTED",
        ),
        (
            "https://host：443/path?payload=client_secret%3Dactual-secret",
            "actual-secret",
            "REDACTED",
        ),
        (
            '// Deliverables： only\nconst uri="https://host：443/path?token=actual-secret&label=public";',
            "actual-secret",
            "REDACTED",
        ),
        ("https\n://host：443/path?token=actual-secret", "actual-secret", "REDACTED"),
        ("\x00//host：443/path?token=actual-secret", "actual-secret", "REDACTED"),
        (
            '// Deliverables： only\nconst uri="https://host：443/path?access_\r\ntoken=actual-secret";\nconst keep=1;',
            "actual-secret",
            "REDACTED",
        ),
        (
            "// Deliverables： only\n"
            'const uri=\'https://host：443/path?payload={"token":"actual-secret","public":"ok"}\';\n'
            "const keep=1;",
            "actual-secret",
            "REDACTED",
        ),
        (
            "// Deliverables： only\n.foo { background: url(/next?access_token=actual-secret); }",
            "actual-secret",
            "REDACTED",
        ),
        ("// Deliverables： only\nconst result = ready ? token === expected : fallback;\n", None, "REDACTED"),
        ('https://bad：#fragment\nconst u="/next?token=actual-secret";', "actual-secret", "REDACTED"),
        ("https://host？token=actual-secret/path?label=keep", "actual-secret", "REDACTED"),
    ],
)
@pytest.mark.asyncio
async def test_original_failed_records_and_html_views_preserve_raw_capture(
    trace_db, text: str, secret: str | None, expected_text: str | None
) -> None:
    store = get_trace_store()
    session_id = store.create_session(client="claude", proxy_mode="reverse")
    record = {
        "timestamp": "2026-05-20T03:00:00+00:00",
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "body": {
                "system": text,
                "messages": [
                    {"role": "user", "content": "Apply the captured Edit."},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "name": "Edit", "input": {"old_string": text}}],
                    },
                ],
            },
        },
        "response": {"status": 200, "body": {"content": [{"type": "text", "text": "Edit applied."}]}},
    }
    store.append_record(session_id, record)
    conn = store._connect()
    raw_payload = conn.execute("SELECT payload_json FROM records WHERE session_id=?", (session_id,)).fetchone()[0]
    cached_summary = conn.execute("SELECT summary_json FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
    server = LiveViewerServer(host="127.0.0.1", port=0)
    port = await server.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base_url}/api/sessions/{session_id}/records") as response:
                assert response.status == 200
                payload = await response.json()
                actual_text = payload["records"][0]["request"]["body"]["messages"][1]["content"][0]["input"][
                    "old_string"
                ]
                assert actual_text == expected_text
                assert payload["records"][0]["request"]["body"]["system"] == expected_text
                assert payload["session"]["first_user"] == "REDACTED"
                assert payload["session"]["last_response"] == "REDACTED"
                assert payload["session"]["error"] == ""
                assert payload["records"][0]["request"]["body"]["messages"][0]["content"] == "Apply the captured Edit."
                assert payload["records"][0]["response"]["body"]["content"][0]["text"] == "Edit applied."
                if secret:
                    assert secret not in json.dumps(payload)
                else:
                    assert actual_text == expected_text
            async with client.get(f"{base_url}/api/sessions/{session_id}/html") as response:
                assert response.status == 200
                html = await response.text()
                if secret:
                    assert secret not in html
                metadata_json = html.partition("const EMBEDDED_TRACE_META = ")[2]
                metadata, _ = json.JSONDecoder().raw_decode(metadata_json)
                assert len(metadata) == 1
                assert metadata[0]["session_user_text"] == "REDACTED"
                assert metadata[0]["sys_hint"] == "REDACTED"
                assert metadata[0]["error_message"] == ""
    finally:
        await server.stop()
    assert store.load_records(session_id) == [record]
    assert (
        conn.execute("SELECT payload_json FROM records WHERE session_id=?", (session_id,)).fetchone()[0] == raw_payload
    )
    assert conn.execute("SELECT summary_json FROM sessions WHERE id=?", (session_id,)).fetchone()[0] == cached_summary
