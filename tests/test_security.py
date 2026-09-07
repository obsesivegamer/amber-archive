import pytest

from app.security import (
    normalize_url,
    validate_public_http_request_url,
    validate_public_http_url,
)


def test_normalize_strips_tracking_and_trailing_slash():
    assert (
        normalize_url("https://News.Example.com/story/?utm_source=tw&id=1")
        == "https://news.example.com/story?id=1"
    )


def test_validate_adds_https():
    url = validate_public_http_url("example.com")
    assert url.startswith("https://example.com")


def test_rejects_empty():
    with pytest.raises(ValueError, match="Paste"):
        validate_public_http_url("  ")


def test_rejects_ftp():
    with pytest.raises(ValueError, match="http"):
        validate_public_http_url("ftp://example.com/file")


def test_rejects_credentials():
    with pytest.raises(ValueError, match="credentials"):
        validate_public_http_url("https://user:pass@example.com/")


def test_rejects_localhost():
    with pytest.raises(ValueError, match="Local"):
        validate_public_http_url("http://localhost:8080/secret")


def test_rejects_loopback_ip():
    with pytest.raises(ValueError, match="Private"):
        validate_public_http_url("http://127.0.0.1/secret")


def test_rejects_rfc1918():
    with pytest.raises(ValueError, match="Private"):
        validate_public_http_url("http://192.168.1.50/admin")


def test_rejects_link_local_metadata():
    with pytest.raises(ValueError, match="Private"):
        validate_public_http_url("http://169.254.169.254/latest/meta-data/")


def test_browser_request_validation_does_not_apply_intake_length_limit():
    url = "http://93.184.216.34/image.png?x=" + "x" * 2048
    with pytest.raises(ValueError, match="too long"):
        validate_public_http_url(url)
    assert validate_public_http_request_url(url) == url
