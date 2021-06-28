from contextlib import closing
from itertools import repeat
from sqlite3 import Connection, OperationalError
from sqlite3.dbapi2 import Cursor
from threading import Lock
from typing import AbstractSet, Iterator, Mapping, Sequence, Tuple, TypedDict

from std2.sqllite3 import with_transaction

from ....consts import BUFFERS_DB
from ....registry import pool
from ....shared.database import init_db
from ....shared.executor import Executor
from ....shared.parse import coalesce
from ....shared.settings import Options
from ....shared.timeit import timeit
from .sql import sql


class SqlMetrics(TypedDict):
    wordcount: int
    insert_order: int


def _ensure_file(cursor: Cursor, file: str, filetype: str) -> None:
    cursor.execute(
        sql("insert", "file"),
        {"filename": file, "filetype": filetype},
    )


def _init() -> Connection:
    conn = Connection(BUFFERS_DB, isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


class BDB:
    def __init__(self) -> None:
        self._lock = Lock()
        self._ex = Executor(pool)
        self._conn: Connection = self._ex.submit(_init)

    def _interrupt(self) -> None:
        with self._lock:
            self._conn.interrupt()

    def ft_update(self, file: str, filetype: str) -> None:
        def cont() -> None:
            with self._lock, closing(self._conn.cursor()) as cursor:
                with with_transaction(cursor):
                    _ensure_file(cursor, file=file, filetype=filetype)
                    cursor.execute(
                        sql("update", "files"), {"filename": file, "filetype": filetype}
                    )

        self._ex.submit(cont)

    def set_lines(
        self,
        filename: str,
        filetype: str,
        lo: int,
        hi: int,
        lines: Sequence[str],
        unifying_chars: AbstractSet[str],
    ) -> None:
        def m1() -> Iterator[Mapping]:
            for line_num, line in enumerate(lines, start=lo):
                for word in coalesce(line, unifying_chars=unifying_chars):
                    yield {
                        "word": word,
                        "filename": filename,
                        "line_num": line_num,
                    }

        def m2() -> Iterator[Mapping]:
            for line_num, line in enumerate(lines, start=lo):
                yield {
                    "line": line,
                    "filename": filename,
                    "line_num": line_num,
                }

        words = tuple(m1())
        shift = len(lines) - (hi - lo)

        @timeit("SQL -- SETLINES")
        def cont() -> None:
            with self._lock, closing(self._conn.cursor()) as cursor:
                with with_transaction(cursor):
                    _ensure_file(cursor, file=filename, filetype=filetype)
                    del_params = {"filename": filename, "lo": lo, "hi": hi}
                    cursor.execute(sql("delete", "words"), del_params)
                    cursor.execute(sql("delete", "lines"), del_params)
                    cursor.execute(
                        sql("update", "lines"),
                        {"filename": filename, "lo": lo, "shift": shift},
                    )
                    cursor.executemany(sql("insert", "word"), words)
                    cursor.executemany(sql("insert", "line"), m2())

        self._ex.submit(cont)

    def lines(self, filename: str, lo: int, hi: int) -> Tuple[int, Sequence[str]]:
        @timeit("SQL -- GETLINES")
        def cont() -> Tuple[int, Sequence[str]]:
            params = {"filename": filename, "lo": lo, "hi": hi}
            with self._lock, closing(self._conn.cursor()) as cursor:
                with with_transaction(cursor):
                    cursor.execute(sql("select", "line_count"), params)
                    count = cursor.fetchone()["line_count"]
                    cursor.execute(sql("select", "lines"), params)
                    lines = count, tuple(row["line"] for row in cursor.fetchall())
            return lines

        self._interrupt()
        return self._ex.submit(cont)

    def inserted(
        self,
        content: str,
    ) -> None:
        def cont() -> None:
            with self._lock, closing(self._conn.cursor()) as cursor:
                with with_transaction(cursor):
                    cursor.execute(sql("insert", "insertion"), {"content": content})

        self._ex.submit(cont)

    @timeit("BUF SUGGEST")
    def suggestions(self, opts: Options, filetype: str, word: str) -> Sequence[str]:
        def cont() -> Sequence[str]:
            try:
                with closing(self._conn.cursor()) as cursor:
                    cursor.execute(
                        sql("select", "words_by_prefix"),
                        {
                            "exact": opts.exact_matches,
                            "cut_off": opts.fuzzy_cutoff,
                            "filetype": filetype,
                            "word": word,
                        },
                    )
                    return tuple(row["word"] for row in cursor.fetchall())
            except OperationalError:
                return ()

        return self._ex.submit(cont)

    def metric(self, filetype: str, words: Sequence[str]) -> Sequence[SqlMetrics]:
        def m1() -> Iterator[Mapping]:
            for word in words:
                yield {"filetype": filetype, "word": word}

        def cont() -> Sequence[SqlMetrics]:
            try:
                with closing(self._conn.cursor()) as cursor:
                    with with_transaction(cursor):
                        cursor.execute(sql("delete", "tmp_for_metrics"), ())
                        cursor.executemany(sql("insert", "tmp_for_metrics"), m1())
                        cursor.execute(sql("select", "metrics"))
                        return cursor.fetchall()
            except OperationalError:
                return tuple(
                    repeat(SqlMetrics(wordcount=0, insert_order=0), times=len(words))
                )

        return self._ex.submit(cont)

