"""Tests for slackcc.bridgedoc: rendering the packaged protocol and installing
it into a project's CLAUDE.md idempotently."""

from __future__ import annotations

from slackcc import bridgedoc


# --------------------------------------------------------------------------- #
# the packaged document
# --------------------------------------------------------------------------- #


def test_doc_ships_with_the_package():
    assert bridgedoc.DOC_PATH.is_file(), (
        "slack-bridge.md must ship as package data, not live in someone's dotfiles"
    )


def test_render_bakes_in_real_cli_paths():
    rendered = bridgedoc.render()

    # No unsubstituted placeholders: an agent reads this verbatim.
    assert "{cli_dir}" not in rendered
    assert f"{bridgedoc.cli_dir()}/slack-upload" in rendered
    assert f"{bridgedoc.cli_dir()}/slack-send" in rendered


def test_render_covers_the_facts_a_turn_depends_on():
    rendered = bridgedoc.render()

    # The reply-duplication rule is the one that visibly breaks a channel.
    assert "slack-send" in rendered
    assert "double-posts" in rendered
    assert "EXTERNAL_UNTRUSTED_CONTENT" in rendered  # the ungated-message escape hatch


def test_cli_dir_is_absolute():
    assert bridgedoc.cli_dir().startswith("/")


# --------------------------------------------------------------------------- #
# install(): idempotent, marker-delimited
# --------------------------------------------------------------------------- #


def test_install_creates_claude_md_when_absent(tmp_path):
    assert bridgedoc.is_installed(tmp_path) is False

    assert bridgedoc.install(tmp_path) == "created"

    body = (tmp_path / "CLAUDE.md").read_text()
    assert body.startswith(bridgedoc.MARK_BEGIN)
    assert body.rstrip().endswith(bridgedoc.MARK_END)
    assert bridgedoc.is_installed(tmp_path) is True


def test_install_appends_to_an_existing_claude_md_without_clobbering_it(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nDon't touch this.\n")

    assert bridgedoc.install(tmp_path) == "updated"

    body = (tmp_path / "CLAUDE.md").read_text()
    assert body.startswith("# My project\n\nDon't touch this.\n")
    assert bridgedoc.MARK_BEGIN in body


def test_install_is_idempotent(tmp_path):
    bridgedoc.install(tmp_path)
    first = (tmp_path / "CLAUDE.md").read_text()

    assert bridgedoc.install(tmp_path) == "unchanged"

    assert (tmp_path / "CLAUDE.md").read_text() == first
    assert first.count(bridgedoc.MARK_BEGIN) == 1  # no second copy


def test_install_replaces_a_stale_section_in_place(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        f"# Head\n\n{bridgedoc.MARK_BEGIN}\nOLD PROTOCOL\n{bridgedoc.MARK_END}\n\n# Tail\n"
    )

    assert bridgedoc.install(tmp_path) == "updated"

    body = (tmp_path / "CLAUDE.md").read_text()
    assert "OLD PROTOCOL" not in body
    assert body.startswith("# Head\n")
    assert body.endswith("# Tail\n")  # content after the section survives
    assert body.count(bridgedoc.MARK_BEGIN) == 1


def test_install_recovers_from_a_truncated_section(tmp_path):
    # Someone deleted the end marker; appending would leave two copies.
    (tmp_path / "CLAUDE.md").write_text(
        f"# Head\n\n{bridgedoc.MARK_BEGIN}\nOLD PROTOCOL, no end marker\n"
    )

    assert bridgedoc.install(tmp_path) == "updated"

    body = (tmp_path / "CLAUDE.md").read_text()
    assert body.count(bridgedoc.MARK_BEGIN) == 1
    assert body.count(bridgedoc.MARK_END) == 1
    assert "OLD PROTOCOL" not in body


def test_is_installed_false_for_unrelated_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Just a normal project file\n")

    assert bridgedoc.is_installed(tmp_path) is False


def test_is_installed_false_for_missing_dir(tmp_path):
    assert bridgedoc.is_installed(tmp_path / "nope") is False
