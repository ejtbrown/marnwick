from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass


def build_random_order(items: Sequence[str], start_item: str, seed: int | None = None) -> list[str]:
    if start_item not in items:
        raise ValueError("start_item must be present in items")
    remaining = [item for item in items if item != start_item]
    # Deterministic UI shuffle, not security-sensitive randomness.
    rng = random.Random(seed)  # nosec B311
    rng.shuffle(remaining)
    return [start_item, *remaining]


@dataclass(slots=True)
class ImageNavigator:
    order: list[str]
    index: int = 0

    @classmethod
    def sequential(cls, items: Sequence[str], start_item: str) -> "ImageNavigator":
        order = list(items)
        if start_item not in order:
            raise ValueError("start_item must be present in items")
        return cls(order=order, index=order.index(start_item))

    @classmethod
    def random(cls, items: Sequence[str], start_item: str, seed: int | None = None) -> "ImageNavigator":
        return cls(order=build_random_order(items, start_item, seed), index=0)

    @property
    def current(self) -> str:
        return self.order[self.index]

    def next(self) -> str | None:
        if self.index + 1 >= len(self.order):
            return None
        self.index += 1
        return self.current

    def previous(self) -> str | None:
        if self.index <= 0:
            return None
        self.index -= 1
        return self.current
