from __future__ import annotations

import sqlite3


FACE_THUMBNAIL_DIR_NAME = "face-thumbnails"


def init_face_schema(connection: sqlite3.Connection) -> None:
    """Create the catalog-local face tables without importing inference code."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            normalized TEXT NOT NULL UNIQUE,
            created_at_ns INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS face_image_state (
            image_id INTEGER PRIMARY KEY,
            image_hash TEXT NOT NULL,
            detector_version TEXT NOT NULL,
            embedding_version TEXT NOT NULL,
            provider TEXT NOT NULL,
            scanned_at_ns INTEGER NOT NULL,
            error TEXT,
            FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            x REAL NOT NULL,
            y REAL NOT NULL,
            width REAL NOT NULL,
            height REAL NOT NULL,
            landmarks BLOB NOT NULL,
            detection_score REAL NOT NULL,
            quality REAL NOT NULL,
            embedding BLOB NOT NULL,
            embedding_version TEXT NOT NULL,
            thumbnail_rel_path TEXT NOT NULL,
            person_id INTEGER,
            confirmed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            deferred_until_ns INTEGER NOT NULL DEFAULT 0,
            created_at_ns INTEGER NOT NULL,
            updated_at_ns INTEGER NOT NULL,
            UNIQUE(image_id, ordinal),
            FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE,
            FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE SET NULL,
            CHECK(status IN ('active', 'ignored', 'not_face'))
        );
        CREATE INDEX IF NOT EXISTS idx_faces_person
            ON faces(person_id, status, quality DESC);
        CREATE INDEX IF NOT EXISTS idx_faces_review
            ON faces(status, person_id, deferred_until_ns);
        CREATE INDEX IF NOT EXISTS idx_faces_image
            ON faces(image_id, ordinal);

        CREATE TABLE IF NOT EXISTS face_person_rejections (
            face_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            created_at_ns INTEGER NOT NULL,
            PRIMARY KEY(face_id, person_id),
            FOREIGN KEY(face_id) REFERENCES faces(id) ON DELETE CASCADE,
            FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS face_pair_rejections (
            first_face_id INTEGER NOT NULL,
            second_face_id INTEGER NOT NULL,
            created_at_ns INTEGER NOT NULL,
            PRIMARY KEY(first_face_id, second_face_id),
            FOREIGN KEY(first_face_id) REFERENCES faces(id) ON DELETE CASCADE,
            FOREIGN KEY(second_face_id) REFERENCES faces(id) ON DELETE CASCADE,
            CHECK(first_face_id < second_face_id)
        );

        CREATE TABLE IF NOT EXISTS face_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at_ns INTEGER NOT NULL,
            undone_at_ns INTEGER
        );
        """
    )
