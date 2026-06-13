from __future__ import annotations

from marnwick.navigation import ImageNavigator, build_random_order


def test_random_order_starts_with_selected_image_and_is_deterministic() -> None:
    items = ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]

    assert build_random_order(items, "c.jpg", seed=7) == build_random_order(items, "c.jpg", seed=7)
    assert build_random_order(items, "c.jpg", seed=7)[0] == "c.jpg"


def test_random_navigator_previous_returns_actual_prior_image() -> None:
    navigator = ImageNavigator.random(["a", "b", "c", "d"], "b", seed=11)
    first = navigator.current
    second = navigator.next()

    assert first == "b"
    assert navigator.previous() == first
    assert navigator.next() == second


def test_sequential_navigator_uses_display_order() -> None:
    navigator = ImageNavigator.sequential(["a", "b", "c"], "b")

    assert navigator.current == "b"
    assert navigator.next() == "c"
    assert navigator.next() is None
    assert navigator.current == "c"
    assert navigator.previous() == "b"
    assert navigator.previous() == "a"
    assert navigator.previous() is None
    assert navigator.current == "a"
