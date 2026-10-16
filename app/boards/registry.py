from fastapi import HTTPException

from app.boards.base import JobBoard
from app.boards.dice.board import DiceBoard

# Register new boards here: BOARDS = {b.key: b for b in (DiceBoard(), IndeedBoard(), ...)}
BOARDS: dict[str, JobBoard] = {b.key: b for b in (DiceBoard(),)}


def get_board(key: str) -> JobBoard:
    board = BOARDS.get(key)
    if not board:
        raise HTTPException(404, f"Unknown job board '{key}'")
    return board


def list_boards() -> list[dict]:
    return [{"key": b.key, "name": b.name, "base_url": b.base_url} for b in BOARDS.values()]
