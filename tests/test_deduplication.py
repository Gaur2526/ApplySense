from app.utils.urls import normalize_url


def test_normalize_url_drops_tracking_and_fragment():
    assert normalize_url("HTTPS://Jobs.Example:443/role/?utm_source=x&gh_src=y&id=4#apply") == "https://jobs.example/role?id=4"


def test_normalize_url_normalizes_host_and_trailing_slash():
    assert normalize_url("https://EXAMPLE.com/") == "https://example.com/"
