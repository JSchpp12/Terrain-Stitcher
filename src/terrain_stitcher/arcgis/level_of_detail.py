from dataclasses import dataclass

@dataclass()
class LevelOfDetailInfo:
    level_id: int
    scale: float
    resolution: float