from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, RLock


@dataclass
class _PaperOperationState:
    active_indexes: int = 0
    active_parses: int = 0
    deletion_pending: bool = False
    users: int = 0


class PaperOperationCoordinator:
    """Coordinate indexing and deletion for papers within this process."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._collection_mutation_lock = RLock()
        self._states: dict[int, _PaperOperationState] = {}

    @contextmanager
    def collection_mutation(self) -> Iterator[None]:
        """Serialize SQLite chunk commits and Chroma mutations in this process."""
        with self._collection_mutation_lock:
            yield

    @contextmanager
    def parsing(self, paper_id: int) -> Iterator[None]:
        """Mark a paper as parsing without holding a lock during page extraction."""
        with self._condition:
            state = self._states.setdefault(paper_id, _PaperOperationState())
            state.users += 1
            try:
                while (
                    state.deletion_pending
                    or state.active_indexes
                ):
                    self._condition.wait()
                state.active_parses += 1
            except BaseException:
                state.users -= 1
                self._condition.notify_all()
                self._discard_if_idle(paper_id, state)
                raise

        try:
            yield
        finally:
            with self._condition:
                state.active_parses -= 1
                state.users -= 1
                self._condition.notify_all()
                self._discard_if_idle(paper_id, state)

    @contextmanager
    def indexing(self, paper_id: int) -> Iterator[None]:
        with self._condition:
            state = self._states.setdefault(paper_id, _PaperOperationState())
            state.users += 1
            try:
                while (
                    state.deletion_pending
                    or state.active_parses
                ):
                    self._condition.wait()
                state.active_indexes += 1
            except BaseException:
                state.users -= 1
                self._condition.notify_all()
                self._discard_if_idle(paper_id, state)
                raise

        try:
            yield
        finally:
            with self._condition:
                state.active_indexes -= 1
                state.users -= 1
                self._condition.notify_all()
                self._discard_if_idle(paper_id, state)

    @contextmanager
    def deleting(self, paper_id: int) -> Iterator[None]:
        with self._condition:
            state = self._states.setdefault(paper_id, _PaperOperationState())
            state.users += 1
            owns_deletion = False
            try:
                while state.deletion_pending:
                    self._condition.wait()
                state.deletion_pending = True
                owns_deletion = True
                while (
                    state.active_indexes
                    or state.active_parses
                ):
                    self._condition.wait()
            except BaseException:
                if owns_deletion:
                    state.deletion_pending = False
                state.users -= 1
                self._condition.notify_all()
                self._discard_if_idle(paper_id, state)
                raise

        try:
            yield
        finally:
            with self._condition:
                state.deletion_pending = False
                state.users -= 1
                self._condition.notify_all()
                self._discard_if_idle(paper_id, state)

    def _discard_if_idle(self, paper_id: int, state: _PaperOperationState) -> None:
        if (
            not state.users
            and not state.active_indexes
            and not state.active_parses
            and not state.deletion_pending
        ):
            self._states.pop(paper_id, None)


paper_operation_coordinator = PaperOperationCoordinator()
