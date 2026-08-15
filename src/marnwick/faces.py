from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from threading import Event
import time
from typing import Any
import unicodedata

import numpy as np

from .face_engine import (
    FaceAnalysis,
    FaceEngine,
    FaceInferenceCancelled,
    FaceInferenceError,
)
from .face_models import FACE_DETECTOR_VERSION, FACE_EMBEDDING_VERSION
from .face_schema import FACE_THUMBNAIL_DIR_NAME
from .safe_image import open_catalog_image


FACE_STATUS_ACTIVE = "active"
FACE_STATUS_IGNORED = "ignored"
FACE_STATUS_NOT_FACE = "not_face"
FACE_STATUSES = {FACE_STATUS_ACTIVE, FACE_STATUS_IGNORED, FACE_STATUS_NOT_FACE}
FACE_GROUP_SIMILARITY = 0.55
FACE_PERSON_SUGGESTION_SIMILARITY = 0.48
FACE_PERSON_SUGGESTION_MARGIN = 0.055
FACE_REANALYSIS_IDENTITY_SIMILARITY = 0.60
FACE_LSH_TABLES = 8
FACE_LSH_BITS = 8
FACE_MAX_PERSON_ANCHORS = 16
# Review is a rolling quality-ordered window. Decisions drain it, allowing the
# next faces into view without making one interactive refresh catalog-sized.
FACE_REVIEW_FACE_LIMIT = 25_000
FACE_SEPARATION_PAIR_LIMIT = 100_000
FACE_REVIEW_GROUP_LIMIT = 500
FACE_REPRESENTATIVE_LIMIT = 16
FACE_DEFER_DAYS = 7


@dataclass(frozen=True, slots=True)
class PersonRecord:
    id: int
    name: str
    face_count: int


@dataclass(frozen=True, slots=True)
class FaceTile:
    id: int
    image_id: int
    rel_path: str
    filename: str
    quality: float
    detection_score: float
    person_id: int | None
    person_name: str | None
    status: str
    thumbnail_rel_path: str


@dataclass(frozen=True, slots=True)
class FaceReviewGroup:
    key: str
    kind: str
    face_ids: tuple[int, ...]
    representative_ids: tuple[int, ...]
    title: str
    count: int
    proposed_person_id: int | None = None
    proposed_person_name: str | None = None
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class FaceIndexSummary:
    images_processed: int
    faces_found: int
    provider: str


class FaceStore:
    """Catalog-bound storage and conservative human-in-the-loop grouping."""

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        self.connection: sqlite3.Connection = catalog._conn
        self.thumbnail_dir: Path = catalog.state_dir / FACE_THUMBNAIL_DIR_NAME

    def pending_image_count(self) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM images AS image
            LEFT JOIN face_image_state AS state ON state.image_id = image.id
            WHERE image.media_kind = 'image'
              AND image.image_hash IS NOT NULL
              AND (
                    state.image_id IS NULL
                 OR state.image_hash != image.image_hash
                 OR state.detector_version != ?
                 OR state.embedding_version != ?
              )
            """,
            (FACE_DETECTOR_VERSION, FACE_EMBEDDING_VERSION),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def pending_images(self, *, limit: int = 256) -> list[tuple[int, str, str]]:
        rows = self.connection.execute(
            """
            SELECT image.id, image.rel_path, image.image_hash
            FROM images AS image
            LEFT JOIN face_image_state AS state ON state.image_id = image.id
            WHERE image.media_kind = 'image'
              AND image.image_hash IS NOT NULL
              AND (
                    state.image_id IS NULL
                 OR state.image_hash != image.image_hash
                 OR state.detector_version != ?
                 OR state.embedding_version != ?
              )
            ORDER BY image.indexed_at_ns DESC, image.id
            LIMIT ?
            """,
            (FACE_DETECTOR_VERSION, FACE_EMBEDDING_VERSION, max(1, min(4096, int(limit)))),
        )
        return [
            (int(row["id"]), str(row["rel_path"]), str(row["image_hash"]))
            for row in rows
        ]

    def index_pending(
        self,
        engine: FaceEngine,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        cancel_event: Event | None = None,
    ) -> FaceIndexSummary:
        total = self.pending_image_count()
        processed = 0
        faces_found = 0
        while processed < total:
            if cancel_event is not None and cancel_event.is_set():
                raise FaceInferenceCancelled("Face processing was canceled")
            batch = self.pending_images(limit=128)
            if not batch:
                break
            for image_id, rel_path, image_hash in batch:
                if cancel_event is not None and cancel_event.is_set():
                    raise FaceInferenceCancelled("Face processing was canceled")
                path = self.catalog.abs_path(rel_path)
                try:
                    analysis = self._analyze_verified_image(
                        path,
                        image_hash,
                        engine,
                        cancel_event,
                    )
                    if self.store_analysis(image_id, image_hash, analysis):
                        faces_found += len(analysis.faces)
                except FaceInferenceCancelled:
                    raise
                except FaceInferenceError:
                    # A model, provider, transport, or response-contract
                    # failure affects the pass rather than one source image.
                    # Abort instead of issuing the same failing request for
                    # every pending photograph in a large catalog.
                    raise
                except Exception as error:
                    self.remember_error(image_id, image_hash, engine.provider, error)
                processed += 1
                if progress is not None:
                    progress(processed, total, rel_path)
        self.prune_thumbnails(cancel_event=cancel_event)
        return FaceIndexSummary(processed, faces_found, engine.provider)

    @staticmethod
    def _analyze_verified_image(
        path: Path,
        expected_hash: str,
        engine: FaceEngine,
        cancel_event: Event | None,
    ) -> FaceAnalysis:
        """Analyze and hash one stable open file description.

        Face rows must describe the same bytes as the catalog image hash. A
        pathname can be replaced while inference runs, so keep one descriptor
        pinned, verify its raw bytes, and compare the final named object before
        allowing publication.
        """

        named_before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        elif stat.S_ISLNK(named_before.st_mode):
            raise OSError(f"refusing symbolic-link image: {path}")
        fd = os.open(path, flags)
        try:
            opened_before = os.fstat(fd)
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or _file_stat_token(named_before) != _file_stat_token(opened_before)
            ):
                raise OSError(f"image changed before face analysis: {path}")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                with open_catalog_image(handle) as source:
                    analysis = engine.analyze(source, cancel_event=cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    raise FaceInferenceCancelled("Face processing was canceled")
                handle.seek(0)
                digest = hashlib.sha256()
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise FaceInferenceCancelled("Face processing was canceled")
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            opened_after = os.fstat(fd)
        finally:
            os.close(fd)
        named_after = path.lstat()
        if not (
            _file_stat_token(named_before)
            == _file_stat_token(opened_before)
            == _file_stat_token(opened_after)
            == _file_stat_token(named_after)
        ):
            raise OSError(f"image changed during face analysis: {path}")
        if digest.hexdigest() != expected_hash:
            raise OSError(f"image changed since catalog indexing: {path}")
        return analysis

    def store_analysis(self, image_id: int, image_hash: str, analysis: FaceAnalysis) -> bool:
        if (
            analysis.detector_version != FACE_DETECTOR_VERSION
            or analysis.embedding_version != FACE_EMBEDDING_VERSION
            or analysis.width <= 0
            or analysis.height <= 0
            or not analysis.provider
            or len(analysis.provider) > 500
            or len(analysis.faces) > 200
        ):
            raise ValueError("Face analysis returned an unexpected contract")
        current = self.connection.execute(
            "SELECT image_hash FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
        if current is None or str(current["image_hash"]) != image_hash:
            return False
        self._ensure_thumbnail_directory()
        prepared: list[tuple[Any, str, bytes]] = []
        for face in analysis.faces:
            embedding_blob = _validated_embedding(face.embedding)
            bbox = np.asarray(face.bbox, dtype=np.float64)
            landmarks = np.asarray(face.landmarks, dtype=np.float64)
            if (
                bbox.shape != (4,)
                or not np.all(np.isfinite(bbox))
                or bbox[0] < 0.0
                or bbox[1] < 0.0
                or bbox[2] <= 0.0
                or bbox[3] <= 0.0
                or bbox[0] + bbox[2] > 1.000001
                or bbox[1] + bbox[3] > 1.000001
                or landmarks.shape != (10,)
                or not np.all(np.isfinite(landmarks))
                or not np.isfinite(face.detection_score)
                or not 0.0 <= face.detection_score <= 1.0
                or not np.isfinite(face.quality)
                or not 0.0 <= face.quality <= 1.0
                or not face.thumbnail_jpeg
                or len(face.thumbnail_jpeg) > 2 * 1024 * 1024
            ):
                raise ValueError("Face analysis returned invalid data")
            key = hashlib.sha256(face.thumbnail_jpeg).hexdigest()
            rel_path = f"{key[:2]}/{key}.jpg"
            self._write_thumbnail(rel_path, face.thumbnail_jpeg)
            prepared.append((face, rel_path, embedding_blob))
        old_rows = list(
            self.connection.execute(
                """
                SELECT id, x, y, width, height, person_id, confirmed, status,
                       deferred_until_ns, embedding, embedding_version
                FROM faces WHERE image_id = ?
                """,
                (image_id,),
            )
        )
        previous_state = self.connection.execute(
            "SELECT image_hash FROM face_image_state WHERE image_id = ?",
            (image_id,),
        ).fetchone()
        matches = _match_old_faces(
            old_rows,
            [item[0].bbox for item in prepared],
            [item[2] for item in prepared],
            require_similarity=(
                previous_state is None or str(previous_state["image_hash"]) != image_hash
            ),
        )
        now = time.time_ns()
        with self.catalog._database_savepoint("store_face_analysis"):
            # Free the positive UNIQUE(image_id, ordinal) namespace before
            # matching rows are reordered by a fresh detector result.
            self.connection.execute(
                "UPDATE faces SET ordinal = -ordinal - 1 WHERE image_id = ?",
                (image_id,),
            )
            retained_ids: set[int] = set()
            for ordinal, (face, thumb_rel_path, embedding_blob) in enumerate(prepared):
                old = old_rows[matches[ordinal]] if ordinal in matches else None
                landmarks_blob = np.asarray(face.landmarks, dtype="<f4").tobytes()
                if old is None:
                    cursor = self.connection.execute(
                        """
                        INSERT INTO faces(
                            image_id, ordinal, x, y, width, height, landmarks,
                            detection_score, quality, embedding, embedding_version,
                            thumbnail_rel_path, created_at_ns, updated_at_ns
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            image_id,
                            ordinal,
                            *face.bbox,
                            landmarks_blob,
                            face.detection_score,
                            face.quality,
                            embedding_blob,
                            analysis.embedding_version,
                            thumb_rel_path,
                            now,
                            now,
                        ),
                    )
                    retained_ids.add(int(cursor.lastrowid))
                else:
                    face_id = int(old["id"])
                    retained_ids.add(face_id)
                    self.connection.execute(
                        """
                        UPDATE faces SET ordinal = ?, x = ?, y = ?, width = ?, height = ?,
                            landmarks = ?, detection_score = ?, quality = ?, embedding = ?,
                            embedding_version = ?, thumbnail_rel_path = ?, updated_at_ns = ?
                        WHERE id = ?
                        """,
                        (
                            ordinal,
                            *face.bbox,
                            landmarks_blob,
                            face.detection_score,
                            face.quality,
                            embedding_blob,
                            analysis.embedding_version,
                            thumb_rel_path,
                            now,
                            face_id,
                        ),
                    )
            stale_ids = [int(row["id"]) for row in old_rows if int(row["id"]) not in retained_ids]
            if stale_ids:
                self.connection.executemany("DELETE FROM faces WHERE id = ?", ((value,) for value in stale_ids))
            self.connection.execute(
                """
                INSERT INTO face_image_state(
                    image_id, image_hash, detector_version, embedding_version,
                    provider, scanned_at_ns, error
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(image_id) DO UPDATE SET
                    image_hash = excluded.image_hash,
                    detector_version = excluded.detector_version,
                    embedding_version = excluded.embedding_version,
                    provider = excluded.provider,
                    scanned_at_ns = excluded.scanned_at_ns,
                    error = NULL
                """,
                (
                    image_id,
                    image_hash,
                    analysis.detector_version,
                    analysis.embedding_version,
                    analysis.provider,
                    now,
                ),
            )
        return True

    def remember_error(
        self,
        image_id: int,
        image_hash: str,
        provider: str,
        error: BaseException,
    ) -> None:
        current = self.connection.execute(
            "SELECT image_hash FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
        if current is None or str(current["image_hash"]) != image_hash:
            return
        message = " ".join(str(error).splitlines())[:2000]
        self.connection.execute(
            """
            INSERT INTO face_image_state(
                image_id, image_hash, detector_version, embedding_version,
                provider, scanned_at_ns, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_id) DO UPDATE SET
                image_hash = excluded.image_hash,
                detector_version = excluded.detector_version,
                embedding_version = excluded.embedding_version,
                provider = excluded.provider,
                scanned_at_ns = excluded.scanned_at_ns,
                error = excluded.error
            """,
            (
                image_id,
                image_hash,
                FACE_DETECTOR_VERSION,
                FACE_EMBEDDING_VERSION,
                provider,
                time.time_ns(),
                message,
            ),
        )

    def people(self) -> list[PersonRecord]:
        rows = self.connection.execute(
            """
            SELECT person.id, person.name, COUNT(face.id) AS face_count
            FROM people AS person
            LEFT JOIN faces AS face
              ON face.person_id = person.id AND face.status = 'active'
            GROUP BY person.id
            ORDER BY person.name COLLATE NOCASE
            """
        )
        return [PersonRecord(int(row["id"]), str(row["name"]), int(row["face_count"])) for row in rows]

    def review_groups(self, *, include_deferred: bool = False) -> list[FaceReviewGroup]:
        now = time.time_ns()
        rows = list(
            self.connection.execute(
                """
                SELECT id, quality, embedding
                FROM faces
                WHERE status = 'active' AND person_id IS NULL
                  AND (? OR deferred_until_ns <= ?)
                  AND embedding_version = ?
                ORDER BY quality DESC, id
                LIMIT ?
                """,
                (
                    1 if include_deferred else 0,
                    now,
                    FACE_EMBEDDING_VERSION,
                    FACE_REVIEW_FACE_LIMIT,
                ),
            )
        )
        if not rows:
            return []
        face_ids = np.asarray([int(row["id"]) for row in rows], dtype=np.int64)
        qualities = np.asarray([float(row["quality"]) for row in rows], dtype=np.float32)
        embeddings = _embedding_matrix(rows)
        codes = _lsh_codes(embeddings)
        rejected_by_face: dict[int, set[int]] = defaultdict(set)
        for row in self.connection.execute("SELECT face_id, person_id FROM face_person_rejections"):
            rejected_by_face[int(row["face_id"])].add(int(row["person_id"]))
        proposed, proposal_scores = self._person_proposals(
            face_ids,
            embeddings,
            codes,
            rejected_by_face,
        )
        groups: list[FaceReviewGroup] = []
        proposed_members: dict[int, list[int]] = defaultdict(list)
        proposed_values: dict[int, list[float]] = defaultdict(list)
        remaining_indexes: list[int] = []
        for index, person_id in enumerate(proposed):
            if person_id is None:
                remaining_indexes.append(index)
            else:
                proposed_members[person_id].append(index)
                proposed_values[person_id].append(proposal_scores[index])
        person_names = {person.id: person.name for person in self.people()}
        for person_id, indexes in proposed_members.items():
            ordered = sorted(indexes, key=lambda value: (-qualities[value], int(face_ids[value])))
            ids = tuple(int(face_ids[index]) for index in ordered)
            confidence = float(min(proposed_values[person_id]))
            name = person_names.get(person_id, f"Person {person_id}")
            groups.append(
                FaceReviewGroup(
                    key=f"person:{person_id}",
                    kind="proposal",
                    face_ids=ids,
                    representative_ids=_diverse_representatives(ids, embeddings, face_ids),
                    title=f"Likely {name}",
                    count=len(ids),
                    proposed_person_id=person_id,
                    proposed_person_name=name,
                    confidence=confidence,
                )
            )
        if remaining_indexes:
            subset = np.asarray(remaining_indexes, dtype=np.int64)
            for indexes in _conservative_clusters(
                embeddings[subset],
                codes[subset],
                face_ids[subset],
                self._pair_rejections(),
            ):
                original = [remaining_indexes[index] for index in indexes]
                ordered = sorted(original, key=lambda value: (-qualities[value], int(face_ids[value])))
                ids = tuple(int(face_ids[index]) for index in ordered)
                if len(ids) == 1:
                    groups.append(
                        FaceReviewGroup(
                            key=f"loose:{ids[0]}",
                            kind="loose",
                            face_ids=ids,
                            representative_ids=ids,
                            title="Loose face",
                            count=1,
                        )
                    )
                else:
                    groups.append(
                        FaceReviewGroup(
                            key=f"cluster:{min(ids)}",
                            kind="unnamed",
                            face_ids=ids,
                            representative_ids=_diverse_representatives(ids, embeddings, face_ids),
                            title="Unnamed group",
                            count=len(ids),
                        )
                    )
        kind_rank = {"proposal": 0, "unnamed": 1, "loose": 2}
        groups.sort(
            key=lambda group: (
                -group.count,
                kind_rank[group.kind],
                -group.confidence,
                group.title.casefold(),
                group.key,
            )
        )
        return groups[:FACE_REVIEW_GROUP_LIMIT]

    def groups_for_view(self, view: str) -> list[FaceReviewGroup]:
        if view in {"review", "suggestions", "unnamed", "loose"}:
            groups = self.review_groups()
            if view == "suggestions":
                return [group for group in groups if group.kind == "proposal"]
            if view == "unnamed":
                return [group for group in groups if group.kind == "unnamed"]
            if view == "loose":
                return [group for group in groups if group.kind == "loose"]
            return groups
        if view == "people":
            groups: list[FaceReviewGroup] = []
            for person in self.people():
                ids = tuple(
                    int(row["id"])
                    for row in self.connection.execute(
                        """
                        SELECT id FROM faces
                        WHERE person_id = ? AND status = 'active'
                        ORDER BY quality DESC, id
                        """,
                        (person.id,),
                    )
                )
                if ids:
                    groups.append(
                        FaceReviewGroup(
                            key=f"named:{person.id}",
                            kind="named",
                            face_ids=ids,
                            representative_ids=ids[:FACE_REPRESENTATIVE_LIMIT],
                            title=person.name,
                            count=len(ids),
                            proposed_person_id=person.id,
                            proposed_person_name=person.name,
                            confidence=1.0,
                        )
                    )
            return groups
        if view in {"ignored", "not_faces"}:
            status = FACE_STATUS_IGNORED if view == "ignored" else FACE_STATUS_NOT_FACE
            ids = tuple(
                int(row["id"])
                for row in self.connection.execute(
                    "SELECT id FROM faces WHERE status = ? ORDER BY updated_at_ns DESC, id",
                    (status,),
                )
            )
            if not ids:
                return []
            return [
                FaceReviewGroup(
                    key=view,
                    kind=view,
                    face_ids=ids,
                    representative_ids=ids[:FACE_REPRESENTATIVE_LIMIT],
                    title="Ignored faces" if view == "ignored" else "Rejected detections",
                    count=len(ids),
                )
            ]
        raise ValueError(f"unsupported face review view: {view}")

    def tiles(self, face_ids: Sequence[int]) -> list[FaceTile]:
        if not face_ids:
            return []
        rows_by_id: dict[int, sqlite3.Row] = {}
        for batch_start in range(0, len(face_ids), 500):
            batch = tuple(int(value) for value in face_ids[batch_start : batch_start + 500])
            placeholders = ",".join("?" for _ in batch)
            for row in self.connection.execute(
                f"""
                SELECT face.id, face.image_id, image.rel_path, image.filename,
                       face.quality, face.detection_score, face.person_id,
                       person.name AS person_name, face.status, face.thumbnail_rel_path
                FROM faces AS face
                JOIN images AS image ON image.id = face.image_id
                LEFT JOIN people AS person ON person.id = face.person_id
                WHERE face.id IN ({placeholders})
                """,
                batch,
            ):
                rows_by_id[int(row["id"])] = row
        return [
            FaceTile(
                id=int(row["id"]),
                image_id=int(row["image_id"]),
                rel_path=str(row["rel_path"]),
                filename=str(row["filename"]),
                quality=float(row["quality"]),
                detection_score=float(row["detection_score"]),
                person_id=int(row["person_id"]) if row["person_id"] is not None else None,
                person_name=str(row["person_name"]) if row["person_name"] is not None else None,
                status=str(row["status"]),
                thumbnail_rel_path=str(row["thumbnail_rel_path"]),
            )
            for face_id in face_ids
            if (row := rows_by_id.get(int(face_id))) is not None
        ]

    def thumbnail_bytes(self, tile: FaceTile) -> bytes:
        try:
            rel_path = _validated_thumbnail_rel_path(tile.thumbnail_rel_path)
        except ValueError:
            return b""
        path = self.thumbnail_dir / rel_path
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return b""
        try:
            value = os.fstat(fd)
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_nlink > 1
                or value.st_size <= 0
                or value.st_size > 2 * 1024 * 1024
            ):
                return b""
            chunks: list[bytes] = []
            remaining = int(value.st_size)
            while remaining:
                chunk = os.read(fd, min(128 * 1024, remaining))
                if not chunk:
                    return b""
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                return b""
            return b"".join(chunks)
        finally:
            os.close(fd)

    def name_faces(
        self,
        face_ids: Sequence[int],
        name: str,
        *,
        person_id: int | None = None,
    ) -> int:
        cleaned = " ".join(name.split())
        if not cleaned or len(cleaned) > 200:
            raise ValueError("Enter a person name from 1 through 200 characters")
        ids = _unique_ids(face_ids)
        if not ids:
            raise ValueError("Select at least one face")
        before = self._face_snapshots(ids)
        created_person_id: int | None = None
        with self.catalog._database_savepoint("name_faces"):
            if person_id is None:
                normalized = _normalize_person_name(cleaned)
                row = self.connection.execute(
                    "SELECT id FROM people WHERE normalized = ?",
                    (normalized,),
                ).fetchone()
                if row is None:
                    cursor = self.connection.execute(
                        "INSERT INTO people(name, normalized, created_at_ns) VALUES (?, ?, ?)",
                        (cleaned, normalized, time.time_ns()),
                    )
                    person_id = int(cursor.lastrowid)
                    created_person_id = person_id
            else:
                row = self.connection.execute(
                    "SELECT id FROM people WHERE id = ?",
                    (int(person_id),),
                ).fetchone()
                if row is None:
                    raise ValueError("The selected person no longer exists")
                person_id = int(row["id"])
            removed_rejections = [
                int(row["face_id"])
                for row in self.connection.execute(
                    f"SELECT face_id FROM face_person_rejections "
                    f"WHERE person_id = ? AND face_id IN ({','.join('?' for _ in ids)})",
                    (person_id, *ids),
                )
            ]
            placeholders = ",".join("?" for _ in ids)
            self.connection.execute(
                f"""
                UPDATE faces
                SET person_id = ?, confirmed = 1, status = 'active',
                    deferred_until_ns = 0, updated_at_ns = ?
                WHERE id IN ({placeholders})
                """,
                (person_id, time.time_ns(), *ids),
            )
            self.connection.executemany(
                "DELETE FROM face_person_rejections WHERE face_id = ? AND person_id = ?",
                ((face_id, person_id) for face_id in ids),
            )
            self._record_operation(
                "name",
                {
                    "faces": before,
                    "created_person_id": created_person_id,
                    "removed_rejection_person_id": person_id,
                    "removed_rejections": removed_rejections,
                },
            )
        return int(person_id)

    def reject_person(self, face_ids: Sequence[int], person_id: int) -> None:
        ids = _unique_ids(face_ids)
        if not ids:
            return
        before = self._face_snapshots(ids)
        existing = {
            int(row["face_id"])
            for row in self.connection.execute(
                f"SELECT face_id FROM face_person_rejections WHERE person_id = ? AND face_id IN ({','.join('?' for _ in ids)})",
                (person_id, *ids),
            )
        }
        with self.catalog._database_savepoint("reject_face_person"):
            now = time.time_ns()
            self.connection.executemany(
                "INSERT OR IGNORE INTO face_person_rejections(face_id, person_id, created_at_ns) VALUES (?, ?, ?)",
                ((face_id, person_id, now) for face_id in ids),
            )
            self.connection.execute(
                f"UPDATE faces SET person_id = NULL, confirmed = 0, updated_at_ns = ? WHERE id IN ({','.join('?' for _ in ids)})",
                (now, *ids),
            )
            self._record_operation(
                "different",
                {
                    "faces": before,
                    "person_id": person_id,
                    "new_rejections": [face_id for face_id in ids if face_id not in existing],
                },
            )

    def separate_faces(
        self,
        first_face_ids: Sequence[int],
        second_face_ids: Sequence[int],
    ) -> None:
        first_ids = _unique_ids(first_face_ids)
        second_ids = _unique_ids(second_face_ids)
        pairs = tuple(
            sorted(
                {
                    (min(first, second), max(first, second))
                    for first in first_ids
                    for second in second_ids
                    if first != second
                }
            )
        )
        if not pairs:
            return
        if len(pairs) > FACE_SEPARATION_PAIR_LIMIT:
            raise ValueError(
                "That split would create too many durable negative pairs; select fewer faces"
            )
        existing: set[tuple[int, int]] = set()
        for offset in range(0, len(pairs), 400):
            batch = pairs[offset : offset + 400]
            placeholders = ",".join("(?, ?)" for _ in batch)
            flat_pairs = tuple(value for pair in batch for value in pair)
            existing.update(
                (int(row["first_face_id"]), int(row["second_face_id"]))
                for row in self.connection.execute(
                    f"SELECT first_face_id, second_face_id FROM face_pair_rejections "
                    f"WHERE (first_face_id, second_face_id) IN ({placeholders})",
                    flat_pairs,
                )
            )
        new_pairs = tuple(pair for pair in pairs if pair not in existing)
        if not new_pairs:
            return
        with self.catalog._database_savepoint("separate_faces"):
            now = time.time_ns()
            self.connection.executemany(
                "INSERT INTO face_pair_rejections"
                "(first_face_id, second_face_id, created_at_ns) VALUES (?, ?, ?)",
                ((first, second, now) for first, second in new_pairs),
            )
            self._record_operation(
                "separate",
                {"new_pairs": [list(pair) for pair in new_pairs]},
            )

    def defer_faces(self, face_ids: Sequence[int]) -> None:
        ids = _unique_ids(face_ids)
        if not ids:
            return
        before = self._face_snapshots(ids)
        until = time.time_ns() + FACE_DEFER_DAYS * 24 * 60 * 60 * 1_000_000_000
        with self.catalog._database_savepoint("defer_faces"):
            self.connection.execute(
                f"UPDATE faces SET deferred_until_ns = ?, updated_at_ns = ? WHERE id IN ({','.join('?' for _ in ids)})",
                (until, time.time_ns(), *ids),
            )
            self._record_operation("defer", {"faces": before})

    def set_face_status(self, face_ids: Sequence[int], status: str) -> None:
        if status not in FACE_STATUSES:
            raise ValueError(f"unsupported face status: {status}")
        ids = _unique_ids(face_ids)
        if not ids:
            return
        before = self._face_snapshots(ids)
        with self.catalog._database_savepoint("set_face_status"):
            self.connection.execute(
                f"""
                UPDATE faces SET status = ?,
                    person_id = CASE WHEN ? = 'not_face' THEN NULL ELSE person_id END,
                    confirmed = CASE WHEN ? = 'not_face' THEN 0 ELSE confirmed END,
                    deferred_until_ns = 0, updated_at_ns = ?
                WHERE id IN ({','.join('?' for _ in ids)})
                """,
                (status, status, status, time.time_ns(), *ids),
            )
            self._record_operation("status", {"faces": before})

    def undo_last_operation(self) -> str | None:
        row = self.connection.execute(
            """
            SELECT id, kind, payload_json FROM face_operations
            WHERE undone_at_ns IS NULL ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        with self.catalog._database_savepoint("undo_face_operation"):
            for snapshot in payload.get("faces", []):
                self.connection.execute(
                    """
                    UPDATE faces SET person_id = ?, confirmed = ?, status = ?,
                        deferred_until_ns = ?, updated_at_ns = ? WHERE id = ?
                    """,
                    (
                        snapshot[1],
                        snapshot[2],
                        snapshot[3],
                        snapshot[4],
                        time.time_ns(),
                        snapshot[0],
                    ),
                )
            person_id = payload.get("person_id")
            for face_id in payload.get("new_rejections", []):
                self.connection.execute(
                    "DELETE FROM face_person_rejections WHERE face_id = ? AND person_id = ?",
                    (face_id, person_id),
                )
            for first_face_id, second_face_id in payload.get("new_pairs", []):
                self.connection.execute(
                    "DELETE FROM face_pair_rejections "
                    "WHERE first_face_id = ? AND second_face_id = ?",
                    (first_face_id, second_face_id),
                )
            removed_person_id = payload.get("removed_rejection_person_id")
            if removed_person_id is not None:
                now = time.time_ns()
                self.connection.executemany(
                    "INSERT OR IGNORE INTO face_person_rejections"
                    "(face_id, person_id, created_at_ns) VALUES (?, ?, ?)",
                    (
                        (int(face_id), int(removed_person_id), now)
                        for face_id in payload.get("removed_rejections", [])
                    ),
                )
            created_person_id = payload.get("created_person_id")
            if created_person_id is not None:
                self.connection.execute(
                    "DELETE FROM people WHERE id = ? AND NOT EXISTS (SELECT 1 FROM faces WHERE person_id = ?)",
                    (created_person_id, created_person_id),
                )
            self.connection.execute(
                "UPDATE face_operations SET undone_at_ns = ? WHERE id = ?",
                (time.time_ns(), int(row["id"])),
            )
        return str(row["kind"])

    def purge(self) -> None:
        with self.catalog._database_savepoint("purge_face_data"):
            self.connection.execute("DELETE FROM face_operations")
            self.connection.execute("DELETE FROM face_pair_rejections")
            self.connection.execute("DELETE FROM face_person_rejections")
            self.connection.execute("DELETE FROM faces")
            self.connection.execute("DELETE FROM people")
            self.connection.execute("DELETE FROM face_image_state")
        try:
            thumbnail_root = self.thumbnail_dir.lstat()
        except OSError:
            return
        if stat.S_ISLNK(thumbnail_root.st_mode) or not stat.S_ISDIR(thumbnail_root.st_mode):
            return
        for prefix in self.thumbnail_dir.iterdir():
            try:
                prefix_value = prefix.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(prefix_value.st_mode) and not stat.S_ISLNK(prefix_value.st_mode):
                for path in prefix.iterdir():
                    try:
                        value = path.lstat()
                        if stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
                            path.unlink()
                    except OSError:
                        continue
                try:
                    prefix.rmdir()
                except OSError:
                    pass

    def prune_thumbnails(self, *, cancel_event: Event | None = None) -> int:
        """Remove unreferenced, structurally valid face-crop cache files."""

        if not self.thumbnail_dir.is_dir() or self.thumbnail_dir.is_symlink():
            return 0
        referenced = {
            str(row["thumbnail_rel_path"])
            for row in self.connection.execute("SELECT thumbnail_rel_path FROM faces")
        }
        removed = 0
        for prefix in self.thumbnail_dir.iterdir():
            if cancel_event is not None and cancel_event.is_set():
                raise FaceInferenceCancelled("Face processing was canceled")
            try:
                prefix_stat = prefix.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(prefix_stat.st_mode) or not stat.S_ISDIR(prefix_stat.st_mode):
                continue
            for path in prefix.iterdir():
                rel_path = f"{prefix.name}/{path.name}"
                if rel_path in referenced:
                    continue
                try:
                    _validated_thumbnail_rel_path(rel_path)
                    value = path.lstat()
                    if (
                        stat.S_ISREG(value.st_mode)
                        and not stat.S_ISLNK(value.st_mode)
                        and value.st_nlink == 1
                    ):
                        path.unlink()
                        removed += 1
                except (OSError, ValueError):
                    continue
            try:
                prefix.rmdir()
            except OSError:
                pass
        return removed

    def stats(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS faces,
                   SUM(CASE WHEN person_id IS NOT NULL AND status = 'active' THEN 1 ELSE 0 END) AS named,
                   SUM(CASE WHEN status = 'ignored' THEN 1 ELSE 0 END) AS ignored,
                   SUM(CASE WHEN status = 'not_face' THEN 1 ELSE 0 END) AS not_faces
            FROM faces
            """
        ).fetchone()
        people = self.connection.execute("SELECT COUNT(*) AS count FROM people").fetchone()
        return {
            "faces": int(row["faces"] or 0),
            "named": int(row["named"] or 0),
            "ignored": int(row["ignored"] or 0),
            "not_faces": int(row["not_faces"] or 0),
            "people": int(people["count"] or 0),
            "pending_images": self.pending_image_count(),
        }

    def _person_proposals(
        self,
        face_ids: np.ndarray,
        embeddings: np.ndarray,
        codes: np.ndarray,
        rejected_by_face: dict[int, set[int]],
    ) -> tuple[list[int | None], list[float]]:
        anchors = list(
            self.connection.execute(
                """
                SELECT id, person_id, embedding, quality
                FROM (
                    SELECT id, person_id, embedding, quality,
                           ROW_NUMBER() OVER (
                               PARTITION BY person_id ORDER BY quality DESC, id
                           ) AS anchor_rank
                    FROM faces
                    WHERE status = 'active' AND person_id IS NOT NULL AND confirmed = 1
                      AND embedding_version = ?
                )
                WHERE anchor_rank <= ?
                ORDER BY person_id, quality DESC, id
                """,
                (FACE_EMBEDDING_VERSION, FACE_MAX_PERSON_ANCHORS),
            )
        )
        if not anchors:
            return [None] * len(face_ids), [0.0] * len(face_ids)
        anchor_embeddings = _embedding_matrix(anchors)
        anchor_people = np.asarray([int(row["person_id"]) for row in anchors], dtype=np.int64)
        anchor_codes = _lsh_codes(anchor_embeddings)
        buckets: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(FACE_LSH_TABLES)]
        for index in range(len(anchors)):
            for table in range(FACE_LSH_TABLES):
                buckets[table][int(anchor_codes[index, table])].append(index)
        proposed: list[int | None] = []
        scores: list[float] = []
        for index, face_id_value in enumerate(face_ids):
            candidates: set[int] = set()
            for table in range(FACE_LSH_TABLES):
                candidates.update(buckets[table].get(int(codes[index, table]), ()))
            if not candidates:
                proposed.append(None)
                scores.append(0.0)
                continue
            by_person: dict[int, float] = {}
            for anchor_index in candidates:
                person_id = int(anchor_people[anchor_index])
                if person_id in rejected_by_face.get(int(face_id_value), set()):
                    continue
                score = float(np.dot(embeddings[index], anchor_embeddings[anchor_index]))
                by_person[person_id] = max(score, by_person.get(person_id, -1.0))
            ordered = sorted(by_person.items(), key=lambda item: (-item[1], item[0]))
            if not ordered:
                proposed.append(None)
                scores.append(0.0)
                continue
            best_person, best_score = ordered[0]
            second_score = ordered[1][1] if len(ordered) > 1 else -1.0
            if (
                best_score >= FACE_PERSON_SUGGESTION_SIMILARITY
                and best_score - second_score >= FACE_PERSON_SUGGESTION_MARGIN
            ):
                proposed.append(best_person)
                scores.append(best_score)
            else:
                proposed.append(None)
                scores.append(best_score)
        return proposed, scores

    def _pair_rejections(self) -> set[tuple[int, int]]:
        return {
            (int(row["first_face_id"]), int(row["second_face_id"]))
            for row in self.connection.execute(
                "SELECT first_face_id, second_face_id FROM face_pair_rejections"
            )
        }

    def _face_snapshots(self, ids: Sequence[int]) -> list[list[Any]]:
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"""
            SELECT id, person_id, confirmed, status, deferred_until_ns
            FROM faces WHERE id IN ({placeholders})
            """,
            tuple(ids),
        )
        return [
            [
                int(row["id"]),
                int(row["person_id"]) if row["person_id"] is not None else None,
                int(row["confirmed"]),
                str(row["status"]),
                int(row["deferred_until_ns"]),
            ]
            for row in rows
        ]

    def _record_operation(self, kind: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO face_operations(kind, payload_json, created_at_ns) VALUES (?, ?, ?)",
            (kind, json.dumps(payload, separators=(",", ":"), sort_keys=True), time.time_ns()),
        )

    def _ensure_thumbnail_directory(self) -> None:
        try:
            value = self.thumbnail_dir.lstat()
        except FileNotFoundError:
            try:
                self.thumbnail_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
            value = self.thumbnail_dir.lstat()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise ValueError("catalog face-thumbnail state entry must be a directory")

    def _write_thumbnail(self, rel_path: str, data: bytes) -> None:
        target = self.thumbnail_dir / _validated_thumbnail_rel_path(rel_path)
        try:
            target.parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        parent_stat = target.parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise ValueError("catalog face-thumbnail prefix entry must be a directory")
        if _safe_existing_thumbnail(target):
            return
        fd, temp_name = tempfile.mkstemp(prefix=".face-", suffix=".tmp", dir=target.parent)
        temp_path = Path(temp_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                fd = -1
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if _safe_existing_thumbnail(target):
                return
            os.replace(temp_path, target)
        finally:
            if fd >= 0:
                os.close(fd)
            temp_path.unlink(missing_ok=True)


def _embedding_matrix(rows: Sequence[sqlite3.Row]) -> np.ndarray:
    result = np.empty((len(rows), 128), dtype=np.float32)
    for index, row in enumerate(rows):
        vector = np.frombuffer(bytes(row["embedding"]), dtype="<f4")
        if vector.size != 128:
            raise ValueError("Stored face embedding has an unexpected dimension")
        result[index] = vector
    return result


def _validated_embedding(data: bytes) -> bytes:
    vector = np.frombuffer(data, dtype="<f4")
    if vector.size != 128 or not np.all(np.isfinite(vector)):
        raise ValueError("Face analysis returned an invalid embedding")
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("Face analysis returned an empty embedding")
    return (vector / norm).astype("<f4", copy=False).tobytes()


def _validated_thumbnail_rel_path(value: str) -> Path:
    candidate = Path(value)
    if len(candidate.parts) != 2:
        raise ValueError("Invalid face-thumbnail path")
    prefix, filename = candidate.parts
    digest, suffix = os.path.splitext(filename)
    if (
        suffix != ".jpg"
        or len(prefix) != 2
        or len(digest) != 64
        or prefix != digest[:2]
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("Invalid face-thumbnail path")
    return candidate


def _safe_existing_thumbnail(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or value.st_nlink > 1
        or value.st_size <= 0
        or value.st_size > 2 * 1024 * 1024
    ):
        raise ValueError("catalog face-thumbnail entry must be a private regular file")
    return True


def _file_stat_token(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _lsh_projection() -> np.ndarray:
    generator = np.random.default_rng(0x4D41524E5749434B)
    return generator.standard_normal((128, FACE_LSH_TABLES * FACE_LSH_BITS), dtype=np.float32)


_FACE_LSH_PROJECTION = _lsh_projection()


def _lsh_codes(embeddings: np.ndarray) -> np.ndarray:
    codes = np.empty((len(embeddings), FACE_LSH_TABLES), dtype=np.uint16)
    for start in range(0, len(embeddings), 4096):
        values = embeddings[start : start + 4096] @ _FACE_LSH_PROJECTION
        bits = values >= 0
        for table in range(FACE_LSH_TABLES):
            table_bits = bits[:, table * FACE_LSH_BITS : (table + 1) * FACE_LSH_BITS]
            packed = np.zeros(len(table_bits), dtype=np.uint16)
            for bit in range(FACE_LSH_BITS):
                packed |= table_bits[:, bit].astype(np.uint16) << bit
            codes[start : start + len(table_bits), table] = packed
    return codes


def _conservative_clusters(
    embeddings: np.ndarray,
    codes: np.ndarray,
    face_ids: np.ndarray,
    rejected_pairs: set[tuple[int, int]],
) -> list[list[int]]:
    parent = list(range(len(embeddings)))
    size = [1] * len(embeddings)
    rejected_neighbors: dict[int, set[int]] = defaultdict(set)
    for first_face_id, second_face_id in rejected_pairs:
        rejected_neighbors[first_face_id].add(second_face_id)
        rejected_neighbors[second_face_id].add(first_face_id)
    component_restricted: dict[int, set[int]] = {
        index: {int(face_id)}
        for index, face_id in enumerate(face_ids)
        if int(face_id) in rejected_neighbors
    }
    component_forbidden: dict[int, set[int]] = {
        index: set(rejected_neighbors[int(face_id)])
        for index, face_id in enumerate(face_ids)
        if int(face_id) in rejected_neighbors
    }

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root == second_root:
            return
        empty: frozenset[int] = frozenset()
        if (
            component_forbidden.get(first_root, empty)
            & component_restricted.get(second_root, empty)
            or component_forbidden.get(second_root, empty)
            & component_restricted.get(first_root, empty)
        ):
            return
        if size[first_root] < size[second_root]:
            first_root, second_root = second_root, first_root
        parent[second_root] = first_root
        size[first_root] += size[second_root]
        restricted = component_restricted.pop(second_root, set())
        if restricted:
            component_restricted.setdefault(first_root, set()).update(restricted)
        forbidden = component_forbidden.pop(second_root, set())
        if forbidden:
            component_forbidden.setdefault(first_root, set()).update(forbidden)

    def connect(first_members: Sequence[int], second_members: Sequence[int] | None = None) -> None:
        first_values = np.asarray(first_members, dtype=np.int64)
        if first_values.size == 0:
            return
        if second_members is None:
            if first_values.size < 2:
                return
            similarities = embeddings[first_values] @ embeddings[first_values].T
            rows, columns = np.nonzero(
                np.triu(similarities >= FACE_GROUP_SIMILARITY, k=1)
            )
            second_values = first_values
        else:
            second_values = np.asarray(second_members, dtype=np.int64)
            if second_values.size == 0:
                return
            similarities = embeddings[first_values] @ embeddings[second_values].T
            rows, columns = np.nonzero(similarities >= FACE_GROUP_SIMILARITY)
        for row, column in zip(rows, columns, strict=True):
            first = int(first_values[row])
            second = int(second_values[column])
            if first == second or find(first) == find(second):
                continue
            face_pair = (
                min(int(face_ids[first]), int(face_ids[second])),
                max(int(face_ids[first]), int(face_ids[second])),
            )
            if face_pair not in rejected_pairs:
                union(first, second)

    for table in range(FACE_LSH_TABLES):
        buckets: dict[int, list[int]] = defaultdict(list)
        for index, code in enumerate(codes[:, table]):
            buckets[int(code)].append(index)
        for members in buckets.values():
            if len(members) <= 512:
                connect(members)
                continue
            # Bound each matrix for a pathological common hash bucket, but
            # compare every member and bridge blocks through diverse anchors.
            anchor_offsets = np.linspace(
                0,
                len(members) - 1,
                num=min(32, len(members)),
                dtype=np.int64,
            )
            anchors = [members[int(offset)] for offset in anchor_offsets]
            for offset in range(0, len(members), 512):
                block = members[offset : offset + 512]
                connect(block)
                connect(block, anchors)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(embeddings)):
        groups[find(index)].append(index)
    return list(groups.values())


def _diverse_representatives(
    ids: Sequence[int],
    all_embeddings: np.ndarray,
    all_face_ids: np.ndarray,
) -> tuple[int, ...]:
    if len(ids) <= FACE_REPRESENTATIVE_LIMIT:
        return tuple(ids)
    index_by_id = {int(face_id): index for index, face_id in enumerate(all_face_ids)}
    indexes = [index_by_id[value] for value in ids if value in index_by_id]
    if not indexes:
        return tuple(ids[:FACE_REPRESENTATIVE_LIMIT])
    selected = [indexes[0]]
    minimum_distance = np.full(len(indexes), np.inf, dtype=np.float32)
    while len(selected) < min(FACE_REPRESENTATIVE_LIMIT, len(indexes)):
        latest = all_embeddings[selected[-1]]
        similarities = all_embeddings[indexes] @ latest
        minimum_distance = np.minimum(minimum_distance, 1.0 - similarities)
        candidate_position = int(np.argmax(minimum_distance))
        candidate = indexes[candidate_position]
        if candidate in selected:
            break
        selected.append(candidate)
    return tuple(int(all_face_ids[index]) for index in selected)


def _match_old_faces(
    old_rows: Sequence[sqlite3.Row],
    new_boxes: Sequence[tuple[float, float, float, float]],
    new_embeddings: Sequence[bytes],
    *,
    require_similarity: bool,
) -> dict[int, int]:
    candidates: list[tuple[float, int, int]] = []
    for new_index, new_box in enumerate(new_boxes):
        for old_index, old in enumerate(old_rows):
            old_box = (float(old["x"]), float(old["y"]), float(old["width"]), float(old["height"]))
            overlap = _box_iou(new_box, old_box)
            if overlap < 0.5:
                continue
            if require_similarity:
                if str(old["embedding_version"]) != FACE_EMBEDDING_VERSION:
                    continue
                old_embedding = np.frombuffer(bytes(old["embedding"]), dtype="<f4")
                new_embedding = np.frombuffer(new_embeddings[new_index], dtype="<f4")
                if (
                    old_embedding.size != 128
                    or float(np.dot(old_embedding, new_embedding))
                    < FACE_REANALYSIS_IDENTITY_SIMILARITY
                ):
                    continue
            candidates.append((overlap, new_index, old_index))
    candidates.sort(reverse=True)
    matches: dict[int, int] = {}
    used_old: set[int] = set()
    for _overlap, new_index, old_index in candidates:
        if new_index not in matches and old_index not in used_old:
            matches[new_index] = old_index
            used_old.add(old_index)
    return matches


def _box_iou(first: Sequence[float], second: Sequence[float]) -> float:
    first_x2, first_y2 = first[0] + first[2], first[1] + first[3]
    second_x2, second_y2 = second[0] + second[2], second[1] + second[3]
    width = max(0.0, min(first_x2, second_x2) - max(first[0], second[0]))
    height = max(0.0, min(first_y2, second_y2) - max(first[1], second[1]))
    intersection = width * height
    union = first[2] * first[3] + second[2] * second[3] - intersection
    return intersection / union if union > 0 else 0.0


def _normalize_person_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _unique_ids(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(value) for value in values if int(value) > 0))
