from app.redaction import Redactor, rehydrate


def test_redacts_pii_and_secrets_and_roundtrips():
    r = Redactor(use_presidio=False, extra_terms=["Project Nimbus"])
    text = (
        "Case study with Jane at jane.doe@acme.com, +1 (415) 555-0199, card 4111 1111 1111 1111. "
        "Key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 about Project Nimbus."
    )
    res = r.redact(text)
    for leaked in ["jane.doe@acme.com", "415", "4111", "sk-ant-", "Project Nimbus"]:
        assert leaked not in res.text
    assert res.counts["EMAIL"] == 1
    assert res.counts["CONFIDENTIAL_TERM"] == 1
    assert rehydrate(res.text, res.mapping) == text


def test_embargoed_lines_are_withheld():
    res = Redactor(use_presidio=False).redact(
        "Public line.\nEMBARGOED until 9 Nov: new pricing is $9.\nAnother public line."
    )
    assert "pricing" not in res.text
    assert "Public line." in res.text


def test_same_value_gets_same_placeholder():
    res = Redactor(use_presidio=False).redact("a@b.com wrote to a@b.com")
    assert res.text == "[REDACTED_EMAIL_1] wrote to [REDACTED_EMAIL_1]"
