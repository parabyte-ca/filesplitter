import updates as u


def test_parse_version_bare():
    assert u.parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_v_prefixed():
    assert u.parse_version("v1.2.3") == (1, 2, 3)


def test_parse_version_rejects_prerelease_suffix():
    assert u.parse_version("1.2.3-beta") is None


def test_parse_version_rejects_two_segments():
    assert u.parse_version("1.2") is None


def test_parse_version_rejects_non_numeric():
    assert u.parse_version("a.b.c") is None


def test_parse_version_rejects_empty():
    assert u.parse_version("") is None


def test_parse_version_rejects_none():
    assert u.parse_version(None) is None


def test_parse_version_tolerates_whitespace():
    assert u.parse_version("  1.2.3  ") == (1, 2, 3)


def test_compare_versions_equal():
    assert u.compare_versions((1, 2, 3), (1, 2, 3)) == 0


def test_compare_versions_less():
    assert u.compare_versions((1, 2, 3), (1, 3, 0)) == -1


def test_compare_versions_greater():
    assert u.compare_versions((2, 0, 0), (1, 9, 9)) == 1


def test_compare_versions_multi_digit_segment_not_lexicographic():
    # (1,10,0) must be > (1,2,0) as tuples, not as strings ("1.10.0" < "1.2.0" lexically)
    assert u.compare_versions((1, 10, 0), (1, 2, 0)) == 1


def test_is_update_available_no_cached_release():
    assert u.is_update_available("1.0.0", {"latest": None}) is False


def test_is_update_available_equal():
    cache = {"latest": {"version": "1.0.0"}}
    assert u.is_update_available("1.0.0", cache) is False


def test_is_update_available_strictly_newer():
    cache = {"latest": {"version": "1.1.0"}}
    assert u.is_update_available("1.0.0", cache) is True


def test_is_update_available_older_or_reverted_release():
    cache = {"latest": {"version": "0.9.0"}}
    assert u.is_update_available("1.0.0", cache) is False


def test_owner_repo_from_source_url_plain():
    assert u._owner_repo_from_source_url("https://github.com/parabyte-ca/filesplitter") == (
        "parabyte-ca", "filesplitter",
    )


def test_owner_repo_from_source_url_trailing_slash():
    assert u._owner_repo_from_source_url("https://github.com/parabyte-ca/filesplitter/") == (
        "parabyte-ca", "filesplitter",
    )


def test_owner_repo_from_source_url_dot_git():
    assert u._owner_repo_from_source_url("https://github.com/parabyte-ca/filesplitter.git") == (
        "parabyte-ca", "filesplitter",
    )


def test_owner_repo_from_source_url_non_github():
    assert u._owner_repo_from_source_url("https://gitlab.com/owner/repo") is None
