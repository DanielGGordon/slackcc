from slackcc import context_watch
from slackcc.context_watch import ContextWatch


def test_warns_once_when_the_threshold_is_crossed(tmp_path):
    watch = ContextWatch(tmp_path / "w.json", 200_000)
    assert watch.observe("t1", 150_000) is False and not watch.due("t1")
    assert watch.observe("t1", 205_000) is True and watch.due("t1")
    assert watch.observe("t1", 230_000) is False  # already warned


def test_none_usage_is_ignored(tmp_path):
    watch = ContextWatch(tmp_path / "w.json", 200_000)
    assert watch.observe("t1", None) is False and not watch.due("t1")


def test_dropping_under_the_threshold_clears_the_mark(tmp_path):
    watch = ContextWatch(tmp_path / "w.json", 200_000)
    watch.observe("t1", 210_000)
    assert watch.observe("t1", 40_000) is False
    assert not watch.due("t1")
    assert watch.observe("t1", 205_000) is True  # can warn again later


def test_clear_and_persistence_across_restart(tmp_path):
    path = tmp_path / "w.json"
    ContextWatch(path, 200_000).observe("t1", 210_000)
    again = ContextWatch(path, 200_000)
    assert again.due("t1")
    again.clear("t1")
    assert not ContextWatch(path, 200_000).due("t1")


def test_corrupt_state_file_starts_empty(tmp_path):
    path = tmp_path / "w.json"
    path.write_text("{nope")
    assert not ContextWatch(path).due("t1")


def test_warning_text_variants():
    tech = context_watch.append_warning("The answer.", 213_400)
    assert tech.startswith("The answer.\n\n") and "213k" in tech and "compact" in tech
    cust = context_watch.append_warning("Done.", 213_400, customer=True)
    assert "token" not in cust and "compact" not in cust and "fresh" in cust
    assert context_watch.append_warning("", 200_000).startswith(":warning:")
