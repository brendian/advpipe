"""Cost tracking with a hard abort when the per-task budget is crossed."""

from __future__ import annotations

from collections import defaultdict


class BudgetExceeded(Exception):
    def __init__(self, spent: float, limit: float) -> None:
        super().__init__(f"budget exceeded: ${spent:.2f} spent, limit ${limit:.2f}")
        self.spent = spent
        self.limit = limit


class Budget:
    def __init__(self, limit_usd: float, spent_usd: float = 0.0) -> None:
        self.limit = limit_usd
        self.spent = spent_usd
        self.by_stage: defaultdict[str, float] = defaultdict(float)

    def add(self, cost_usd: float, stage: str = "") -> None:
        """Record a cost. Raises BudgetExceeded as soon as the limit is crossed."""
        self.spent += cost_usd
        self.by_stage[stage] += cost_usd
        if self.spent > self.limit:
            raise BudgetExceeded(self.spent, self.limit)

    @property
    def remaining(self) -> float:
        return max(self.limit - self.spent, 0.0)
