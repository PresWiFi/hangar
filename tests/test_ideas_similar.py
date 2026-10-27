"""Duplicate detection. The threshold in hangar/ideas/similar.py is tuned against these lists."""
import pytest

from hangar.ideas import similar

SAME_IDEA = [
    ("Hangar Ideas plugin", "Ideas plugin for Hangar"),
    ("Garden watering planner", "Garden Watering Planner App"),
    ("Photo culling tool", "photo-culling tool"),
    ("Recipe organiser", "Recipe organizer"),
    ("Budget tracker", "Budget tracking"),
    ("Budget tracker", "Family budget tracker"),
    ("Water reminder", "Watering reminder"),
    ("Route planner for gas stops", "Gas stop route planner"),
    ("  The NFC  blocker!! ", "nfc blocker"),
    ("Demo idea", "demo ideas"),
    ("Habit tracker app", "Habit tracker apps"),
]

DIFFERENT_IDEAS = [
    ("Garden watering planner", "Garden lighting planner"),
    ("Budget", "Budget travel blog"),
    ("Home lab dashboard", "Home lab backup"),
    ("NFC app blocker", "NFC door lock"),
    ("Gas stop route planner", "Office declutter planner"),
    ("Photo culling", "AI photo culling workflow"),
    ("Recipe organizer", "Recipe sharing site"),
]


@pytest.mark.parametrize("a,b", SAME_IDEA)
def test_near_duplicates_match(a, b):
    assert similar.is_duplicate(a, b), similar.score(a, b)
    assert similar.is_duplicate(b, a)


@pytest.mark.parametrize("a,b", DIFFERENT_IDEAS)
def test_different_ideas_do_not_match(a, b):
    assert not similar.is_duplicate(a, b), similar.score(a, b)


def test_empty_titles_score_zero():
    assert similar.score("", "anything") == 0
    assert similar.score("!!!", "???") == 0


def test_only_filler_words_still_compare():
    assert similar.is_duplicate("The App", "the app")
