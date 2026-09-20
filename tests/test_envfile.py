"""Tests for surgical .env edits.

These matter more than most: a careless rewrite of this file once destroyed a
working configuration by inserting empty keys above the real ones, and the
resulting failure pointed at authentication rather than at the file.
"""

from __future__ import annotations

import pytest

from monarch_euro.envfile import (
    EnvFileError,
    duplicate_keys,
    parse_cookie_header,
    read_key,
    update,
)

SAMPLE = """\
# Enable Banking
EB_APPLICATION_ID=abc-123
EB_PRIVATE_KEY_PATH=./secrets/k.pem

# Monarch — comments must survive
MONARCH_COOKIE_HEADER=old_session
MONARCH_CSRF_TOKEN=oldcsrf
MONARCH_COOKIE_NAME=session_id

LOOKBACK_DAYS=30
"""


def write(tmp_path, text=SAMPLE):
    p = tmp_path / ".env"
    p.write_text(text)
    return p


# -- surgical updates ------------------------------------------------------

def test_only_named_keys_change(tmp_path):
    p = write(tmp_path)
    update(p, {"MONARCH_CSRF_TOKEN": "newcsrf"})
    assert read_key(p, "MONARCH_CSRF_TOKEN") == "newcsrf"
    assert read_key(p, "EB_APPLICATION_ID") == "abc-123"
    assert read_key(p, "LOOKBACK_DAYS") == "30"


def test_comments_and_ordering_survive(tmp_path):
    p = write(tmp_path)
    update(p, {"MONARCH_CSRF_TOKEN": "newcsrf"})
    text = p.read_text()
    assert "# Enable Banking" in text
    assert "# Monarch — comments must survive" in text
    assert text.index("EB_APPLICATION_ID") < text.index("MONARCH_CSRF_TOKEN")


def test_absent_key_is_appended(tmp_path):
    p = write(tmp_path)
    update(p, {"BRAND_NEW": "value"})
    assert read_key(p, "BRAND_NEW") == "value"
    assert read_key(p, "EB_APPLICATION_ID") == "abc-123"


def test_no_duplicates_are_introduced(tmp_path):
    p = write(tmp_path)
    update(p, {"MONARCH_COOKIE_HEADER": "a", "MONARCH_CSRF_TOKEN": "b"})
    assert duplicate_keys(p) == set()


def test_a_backup_is_kept(tmp_path):
    p = write(tmp_path)
    backup = update(p, {"MONARCH_CSRF_TOKEN": "newcsrf"})
    assert backup is not None and backup.is_file()
    assert "oldcsrf" in backup.read_text()


def test_editing_an_already_duplicated_file_is_refused(tmp_path):
    """The exact corruption this module exists to prevent."""
    p = write(tmp_path, SAMPLE + "MONARCH_CSRF_TOKEN=second\n")
    with pytest.raises(EnvFileError, match="more than once"):
        update(p, {"MONARCH_CSRF_TOKEN": "newcsrf"})


def test_commented_out_key_is_not_treated_as_the_key(tmp_path):
    p = write(tmp_path, "# MONARCH_CSRF_TOKEN=commented\nMONARCH_CSRF_TOKEN=real\n")
    update(p, {"MONARCH_CSRF_TOKEN": "new"})
    text = p.read_text()
    assert "# MONARCH_CSRF_TOKEN=commented" in text
    assert "MONARCH_CSRF_TOKEN=new" in text


def test_values_containing_equals_survive(tmp_path):
    p = write(tmp_path)
    update(p, {"MONARCH_COOKIE_HEADER": "session_id=abc==; csrftoken=d=e"})
    assert read_key(p, "MONARCH_COOKIE_HEADER") == "session_id=abc==; csrftoken=d=e"


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(EnvFileError, match="does not exist"):
        update(tmp_path / "nope.env", {"A": "b"})


# -- cookie parsing --------------------------------------------------------

def test_parses_a_devtools_cookie_header():
    raw = ("ajs_anonymous_id=78d2; session_id=abc123; csrftoken=xyz789; "
           "__cf_bm=short; _dd_s=x")
    cookies = parse_cookie_header(raw)
    assert cookies["session_id"] == "abc123"
    assert cookies["csrftoken"] == "xyz789"


def test_leading_cookie_label_is_tolerated():
    assert parse_cookie_header("Cookie: session_id=abc; csrftoken=x")["session_id"] == "abc"


def test_wrapped_whitespace_and_newlines_are_tolerated():
    raw = "session_id=abc;\n   csrftoken=xyz\n"
    cookies = parse_cookie_header(raw)
    assert cookies == {"session_id": "abc", "csrftoken": "xyz"}


def test_cookie_values_containing_equals_are_kept_whole():
    assert parse_cookie_header("cf_clearance=a.b=c==; session_id=s")["cf_clearance"] == "a.b=c=="


def test_garbage_yields_nothing_rather_than_raising():
    assert parse_cookie_header("not a cookie header at all") == {}


# -- backups must not live in the repo -------------------------------------

def test_backups_are_written_outside_the_repo(tmp_path):
    """A backup written beside .env once reached a public repo, because
    .gitignore covered `.env` but not `.env.bak-<timestamp>`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = repo / ".env"
    env.write_text("A=1\n")
    backups = tmp_path / "elsewhere"

    backup = update(env, {"A": "2"}, backup_dir_override=backups)

    assert backup is not None
    assert backups in backup.parents
    assert repo not in backup.parents
    assert [p.name for p in repo.iterdir()] == [".env"]


def test_only_recent_backups_are_kept(tmp_path):
    """Old backups hold superseded credentials."""
    import time

    env = tmp_path / ".env"
    env.write_text("A=0\n")
    backups = tmp_path / "b"
    for i in range(8):
        update(env, {"A": str(i)}, backup_dir_override=backups)
        time.sleep(0.01)
    assert len(list(backups.glob("*.env.*"))) <= 5
