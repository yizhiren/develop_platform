from typing import Final


AGENT_KEYS: Final[tuple[str, ...]] = ("agent1", "agent2", "agent3", "agent4")

ROLE_TO_AGENT_KEY: Final[dict[str, str]] = {
    "clarify": "agent1",
    "architect": "agent2",
    "review": "agent2",
    "revise": "agent2",
    "develop": "agent3",
    "accept": "agent4",
    "final_accept": "agent4",
    "regression": "agent4",
}


def agent_key_for_role(role: str) -> str:
    try:
        return ROLE_TO_AGENT_KEY[role]
    except KeyError as exc:
        raise ValueError(f"unsupported agent role: {role}") from exc
