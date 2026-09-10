"""Rules data access — loads shared/config/weapons.yaml + match.yaml via config.RulesConfig and
exposes typed lookups.

§16.3 rule 1: the resolver looks moves up here; it never branches on a literal weapon or action
name. Do not add a second way to reach this data (e.g. reading the YAML directly from
engine/resolver.py) — Rules is the only door.
"""

from __future__ import annotations

from dataclasses import dataclass

from cg_comms.config import RulesConfig, WeaponMove
from cg_comms.messages.enums import ActionClass, Weapon


@dataclass(frozen=True)
class Rules:
    config: RulesConfig

    @classmethod
    def load(cls) -> "Rules":
        return cls(config=RulesConfig.load())

    def move_for(self, weapon: Weapon, action: ActionClass) -> WeaponMove | None:
        moves = self.config.moves.get(weapon.value)
        if moves is None:
            return None
        return moves.get(action.value)

    def is_valid_move(self, weapon: Weapon, action: ActionClass) -> bool:
        return self.move_for(weapon, action) is not None

    @property
    def starting_hp(self) -> int:
        return self.config.starting_hp

    @property
    def parry_window_ms(self) -> int:
        return self.config.parry_window_ms

    @property
    def block_multiplier(self) -> float:
        return self.config.block_multiplier

    @property
    def stagger_ms(self) -> int:
        return self.config.stagger_ms

    @property
    def guard_arc_deg(self) -> float:
        return self.config.guard_arc_deg

    @property
    def min_confidence(self) -> float:
        return self.config.min_confidence

    @property
    def position_max_age_ms(self) -> int:
        return self.config.position_max_age_ms
