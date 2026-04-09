from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MountEntry:
    source: str
    target: str
    readonly: bool = False

    def to_volume_spec(self) -> str:
        mode = "ro" if self.readonly else "rw"
        return f"{self.source}:{self.target}:{mode}"


def merge_volume_specs(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    by_target: dict[str, str] = {}
    for group in groups:
        for entry in group:
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 3:
                if entry not in merged:
                    merged.append(entry)
                continue
            source = ":".join(parts[:-2])
            target = parts[-2]
            mode = parts[-1]
            existing = by_target.get(target)
            candidate = f"{source}:{target}:{mode}"
            if existing is None:
                by_target[target] = candidate
                merged.append(candidate)
                continue
            if existing == candidate:
                continue
            if existing.endswith(":rw") and candidate.endswith(":ro"):
                idx = merged.index(existing)
                merged[idx] = candidate
                by_target[target] = candidate
    return merged
