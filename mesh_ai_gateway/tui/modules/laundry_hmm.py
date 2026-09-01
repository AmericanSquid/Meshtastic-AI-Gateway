#!/usr/bin/env python3
"""
laundry_hmm.py

Laundry HMM experiment using the Mesh AI Gateway's Meshtastic connection.

The modified existing DetectionSensorModule counts rising edges on GPIO21,
applies a 50 ms edge filter, and emits one local-only JSON sample per second as
an existing DETECTION_SENSOR_APP packet. This script receives those packets
from the already-connected gateway daemon over its local Unix socket.

This script:
  1. Records one-second pulse counts into the existing SQLite database.
  2. Trains an unsupervised Poisson Hidden Markov Model from recorded cycles.
  3. Saves the trained model in that same database.
  4. Watches the washer live and prints the current hidden state.

Install dependencies:
    pip install hmmlearn numpy

Then:
    python laundry_hmm.py

The existing laundry_hmm.sqlite3 file remains compatible.
"""

from __future__ import annotations

import asyncio
import json
import os
import pickle
import sqlite3
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# EDIT THESE
# ---------------------------------------------------------------------------

GATEWAY_SOCKET_PATH = os.environ.get(
    "MESH_AI_GATEWAY_SOCKET",
    "/run/mesh-ai-gateway/control.sock",
)
CONNECT_TIMEOUT_SECONDS = 10
STREAM_STALE_SECONDS = 5.0
SIGNIFICANT_CHANNEL_INDEX = 3  # SQD-IoT
SOURCE_NAME = "Heltec Wireless Stick Lite V3 laundry counter on GPIO21"

# The firmware emits one observation per second.
SAMPLE_INTERVAL_SECONDS = 1.0

# Start with four hidden states. Change later and retrain to experiment.
HMM_STATES = 4

# Try several random initializations and keep the best likelihood.
HMM_RANDOM_RESTARTS = 8

# Number of recent samples used for live inference.
LIVE_WINDOW_SECONDS = 15 * 60

# Everything persistent lives in this one SQLite data/model file.
DB_PATH = Path(__file__).with_name("laundry_hmm.sqlite3")


# ---------------------------------------------------------------------------
# DEPENDENCIES
# ---------------------------------------------------------------------------

try:
    import numpy as np
    from hmmlearn import hmm
    #from mesh_ai_gateway.ipc.client import request
except ImportError as exc:
    missing = getattr(exc, "name", "a dependency")
    print(f"\nMissing Python package: {missing}")
    print("\nInstall everything this script needs with:")
    print("  pip install hmmlearn numpy\n")
    raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db() -> None:
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT NOT NULL,
                ended_at    TEXT,
                note        TEXT
            );

            CREATE TABLE IF NOT EXISTS samples (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL,
                timestamp   TEXT NOT NULL,
                pulse_count INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_samples_session
                ON samples(session_id, id);

            CREATE TABLE IF NOT EXISTS models (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                n_states    INTEGER NOT NULL,
                score       REAL NOT NULL,
                samples     INTEGER NOT NULL,
                sessions    INTEGER NOT NULL,
                payload     BLOB NOT NULL
            );
            """
        )


# ---------------------------------------------------------------------------
# GATEWAY IPC FEED
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LaundrySample:
    total: int
    delta: int
    uptime_ms: int
    filter_us: int
    running: bool


class LaundryCounterFeed:
    """Receives local-only Detection Sensor packets through gateway IPC."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        socket_path: str = GATEWAY_SOCKET_PATH,
    ) -> None:
        self.loop = loop
        self.socket_path = socket_path
        self.last_uptime_ms: Optional[int] = None

    def _sample_from_packet(self, packet: dict) -> Optional[LaundrySample]:
        try:
            message = json.loads(packet["decoded"]["text"])
            if message.get("v") != 1 or message.get("type") != "laundry":
                return None

            running = message["running"]
            if not isinstance(running, bool):
                return None

            sample = LaundrySample(
                total=int(message["total"]),
                delta=int(message["delta"]),
                uptime_ms=int(message["uptime_ms"]),
                filter_us=int(message["filter_us"]),
                running=running,
            )
            if sample.total < 0 or sample.delta < 0:
                return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return sample

    async def next_sample(self, timeout: float = STREAM_STALE_SECONDS) -> LaundrySample:
        deadline = self.loop.time() + timeout

        while True:
            remaining = deadline - self.loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    f"No local laundry sample arrived for {timeout:.0f} seconds."
                )

            try:
                response = await request(
                    self.socket_path,
                    {
                        "command": "next_detection_sensor",
                        "timeout": remaining,
                    },
                    timeout=remaining + 1.0,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"No local laundry sample arrived for {timeout:.0f} seconds."
                ) from exc

            if not response.get("ok"):
                error = response.get("error", "unknown gateway error")
                if error == "timeout":
                    raise RuntimeError(
                        f"No local laundry sample arrived for {timeout:.0f} seconds."
                    )
                raise RuntimeError(f"Gateway IPC error: {error}")

            sample = self._sample_from_packet(response.get("packet") or {})
            if sample is not None:
                break

        # A dropped API packet cannot safely be treated as a one-second HMM window.
        if self.last_uptime_ms is not None:
            elapsed_ms = (sample.uptime_ms - self.last_uptime_ms) & 0xFFFFFFFF
            if elapsed_ms > 1750:
                raise RuntimeError(
                    f"Missed a one-second laundry sample ({elapsed_ms} ms gap)."
                )

        self.last_uptime_ms = sample.uptime_ms
        return sample

    async def send_significant(self, text: str) -> None:
        response = await request(
            self.socket_path,
            {
                "command": "send_mesh_text",
                "text": text,
                "channel": SIGNIFICANT_CHANNEL_INDEX,
            },
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        if not response.get("ok"):
            raise RuntimeError(
                f"Gateway IPC error: {response.get('error', 'unknown gateway error')}"
            )

    async def close(self) -> None:
        return None


async def connect_sensor() -> tuple[LaundryCounterFeed, LaundrySample]:
    print(f"Connecting to Meshtastic through Mesh AI Gateway at {GATEWAY_SOCKET_PATH} ...")
    feed = LaundryCounterFeed(asyncio.get_running_loop())

    try:
        baseline = await feed.next_sample(timeout=CONNECT_TIMEOUT_SECONDS)
    except BaseException:
        await feed.close()
        raise

    print(
        "Meshtastic laundry feed connected: "
        f"total={baseline.total}, filter={baseline.filter_us / 1000:.0f} ms, "
        f"running={'ON' if baseline.running else 'OFF'}"
    )
    return feed, baseline


# ---------------------------------------------------------------------------
# RECORDING
# ---------------------------------------------------------------------------

async def record_cycle(note: str = "") -> None:
    feed: Optional[LaundryCounterFeed] = None
    session_id: Optional[int] = None
    db = db_connect()

    try:
        feed, baseline = await connect_sensor()

        started = datetime.now().isoformat(timespec="seconds")
        cur = db.execute(
            "INSERT INTO sessions(started_at, note) VALUES (?, ?)",
            (started, note or None),
        )
        session_id = int(cur.lastrowid)
        db.commit()

        print()
        print(f"Recording session #{session_id}.")
        print("Press Ctrl+C when this laundry cycle is finished.")
        print(f"Initial cumulative pulse total: {baseline.total}")
        print()

        samples_written = 0
        while True:
            sample = await feed.next_sample()
            pulse_count = sample.delta
            stamp = datetime.now().isoformat(timespec="seconds")

            db.execute(
                """
                INSERT INTO samples(session_id, timestamp, pulse_count)
                VALUES (?, ?, ?)
                """,
                (session_id, stamp, pulse_count),
            )
            samples_written += 1
            if samples_written % 10 == 0:
                db.commit()

            print(
                f"{stamp[-8:]}  "
                f"events/sec={pulse_count:3d}  "
                f"total={sample.total}  "
                f"running={'ON' if sample.running else 'OFF'}"
            )

    finally:
        ended = datetime.now().isoformat(timespec="seconds")
        if session_id is not None:
            try:
                db.execute(
                    "UPDATE sessions SET ended_at=? WHERE id=?",
                    (ended, session_id),
                )
                db.commit()
                count = db.execute(
                    "SELECT COUNT(*) FROM samples WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                print(f"\nSaved session #{session_id} with {count} samples.")
            except sqlite3.Error:
                pass

        db.close()
        if feed is not None:
            await feed.close()


# ---------------------------------------------------------------------------
# HMM TRAINING
# ---------------------------------------------------------------------------

def load_training_sequences():
    with db_connect() as db:
        sessions = db.execute(
            """
            SELECT s.id, s.started_at, s.note, COUNT(x.id)
            FROM sessions s
            JOIN samples x ON x.session_id = s.id
            GROUP BY s.id
            HAVING COUNT(x.id) >= 30
            ORDER BY s.id
            """
        ).fetchall()

        sequences = []
        usable_sessions = []

        for session_id, started_at, note, count in sessions:
            rows = db.execute(
                """
                SELECT pulse_count
                FROM samples
                WHERE session_id=?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()

            values = np.asarray(
                [max(0, int(row[0])) for row in rows],
                dtype=np.int64,
            ).reshape(-1, 1)

            if len(values) >= 30:
                sequences.append(values)
                usable_sessions.append(
                    (session_id, started_at, note, count)
                )

    return sequences, usable_sessions


def train_hmm() -> None:
    sequences, sessions = load_training_sequences()

    if not sequences:
        print("\nNo usable recordings yet.")
        print("Record at least one cycle first.")
        return

    X = np.concatenate(sequences, axis=0)
    lengths = [len(seq) for seq in sequences]

    if len(X) < max(120, HMM_STATES * 20):
        print(
            f"\nOnly {len(X)} seconds of data are available. "
            "You can experiment, but a full cycle will be much more useful."
        )

    print()
    print(
        f"Training {HMM_STATES}-state Poisson HMM on "
        f"{len(X)} seconds from {len(sequences)} session(s)..."
    )

    best_model = None
    best_score = -np.inf

    for seed in range(HMM_RANDOM_RESTARTS):
        model = hmm.PoissonHMM(
            n_components=HMM_STATES,
            n_iter=300,
            tol=1e-4,
            random_state=seed,
        )

        try:
            model.fit(X, lengths)
            score = float(model.score(X, lengths))
        except Exception as exc:
            print(f"  restart {seed + 1}: failed ({exc})")
            continue

        print(f"  restart {seed + 1}: log likelihood {score:.2f}")

        if score > best_score:
            best_model = model
            best_score = score

    if best_model is None:
        print("\nTraining failed on every restart.")
        return

    payload = pickle.dumps(
        {
            "model": best_model,
            "sample_interval": SAMPLE_INTERVAL_SECONDS,
            "source_name": SOURCE_NAME,
        },
        protocol=pickle.HIGHEST_PROTOCOL,
    )

    with db_connect() as db:
        db.execute(
            """
            INSERT INTO models(
                created_at, n_states, score, samples, sessions, payload
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                HMM_STATES,
                best_score,
                len(X),
                len(sequences),
                payload,
            ),
        )
        db.commit()

    print("\nModel saved.")
    describe_model(best_model)
    print_transition_matrix(best_model)
    print_session_summaries(best_model, sequences, sessions)


def state_activity_labels(model):
    rates = np.asarray(model.lambdas_).reshape(model.n_components, -1)[:, 0]
    order = list(np.argsort(rates))

    if model.n_components == 1:
        words = ["only state"]
    elif model.n_components == 2:
        words = ["quieter", "more active"]
    elif model.n_components == 3:
        words = ["quietest", "middle activity", "most active"]
    elif model.n_components == 4:
        words = ["quietest", "low activity", "medium activity", "most active"]
    else:
        words = [f"activity rank {i + 1}" for i in range(model.n_components)]

    labels = {}
    for rank, state in enumerate(order):
        labels[int(state)] = words[rank]
    return labels


def describe_model(model) -> None:
    rates = np.asarray(model.lambdas_).reshape(model.n_components, -1)[:, 0]
    labels = state_activity_labels(model)

    print("\nLearned hidden states:")
    for state in range(model.n_components):
        print(
            f"  State {state}: "
            f"expected {rates[state]:.3f} events/sec "
            f"({labels[state]})"
        )

    print(
        "\nThese are NOT automatically wash/rinse/spin labels. "
        "They are hidden states the model discovered from the pulse cadence."
    )


def print_transition_matrix(model) -> None:
    print("\nTransition probabilities:")
    header = "          " + " ".join(
        f"to {i:>5d}" for i in range(model.n_components)
    )
    print(header)

    for i, row in enumerate(model.transmat_):
        numbers = " ".join(f"{p:8.3f}" for p in row)
        print(f"from {i:>2d}  {numbers}")


def run_length_encode(states):
    if len(states) == 0:
        return []

    output = []
    start = 0
    current = int(states[0])

    for index in range(1, len(states)):
        state = int(states[index])
        if state != current:
            output.append((start, index - 1, current))
            start = index
            current = state

    output.append((start, len(states) - 1, current))
    return output


def print_session_summaries(model, sequences, sessions) -> None:
    print("\nState timeline by recorded session:")

    for seq, meta in zip(sequences, sessions):
        session_id, started_at, note, _count = meta
        states = model.predict(seq)
        segments = run_length_encode(states)

        # Avoid printing every two-second twitch. Show segments >= 5 seconds,
        # but still leave the full data in SQLite for future analysis.
        meaningful = [
            (start, end, state)
            for start, end, state in segments
            if (end - start + 1) >= 5
        ]

        suffix = f" ({note})" if note else ""
        print(f"\n  Session #{session_id} {started_at}{suffix}")

        if not meaningful:
            print("    no >=5 second state segments")
            continue

        for start, end, state in meaningful:
            duration = (end - start + 1) * SAMPLE_INTERVAL_SECONDS
            start_sec = start * SAMPLE_INTERVAL_SECONDS
            print(
                f"    +{start_sec:6.0f}s  "
                f"State {state} for {duration:5.0f}s"
            )


# ---------------------------------------------------------------------------
# MODEL LOAD / LIVE INFERENCE
# ---------------------------------------------------------------------------

def load_latest_model():
    with db_connect() as db:
        row = db.execute(
            """
            SELECT id, created_at, score, payload
            FROM models
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return None

    model_id, created_at, score, payload = row
    saved = pickle.loads(payload)
    return model_id, created_at, score, saved["model"]


async def live_monitor() -> None:
    loaded = load_latest_model()
    if loaded is None:
        print("\nNo trained model exists yet. Record a cycle and train first.")
        return

    model_id, created_at, score, model = loaded
    labels = state_activity_labels(model)

    print()
    print(
        f"Loaded model #{model_id} from {created_at} "
        f"(training score {score:.2f})."
    )
    describe_model(model)

    feed: Optional[LaundryCounterFeed] = None
    try:
        feed, baseline = await connect_sensor()
        previous_running = baseline.running
        recent = deque(maxlen=max(30, int(LIVE_WINDOW_SECONDS)))

        print()
        print("Live HMM monitor running. Ctrl+C to stop.")
        print()

        while True:
            sample = await feed.next_sample()

            if sample.running != previous_running:
                message = (
                    "Laundry started"
                    if sample.running
                    else "Laundry inactive: no pulses for 120 seconds"
                )
                await feed.send_significant(message)
                previous_running = sample.running

            pulse_count = sample.delta
            recent.append(pulse_count)
            X = np.asarray(recent, dtype=np.int64).reshape(-1, 1)

            if len(X) < 5:
                print(
                    f"{datetime.now().strftime('%H:%M:%S')}  "
                    f"events/sec={pulse_count:3d}  "
                    f"running={'ON' if sample.running else 'OFF'}  warming up..."
                )
                continue

            states = model.predict(X)
            probabilities = model.predict_proba(X)
            state = int(states[-1])
            confidence = float(probabilities[-1, state])

            print(
                f"{datetime.now().strftime('%H:%M:%S')}  "
                f"events/sec={pulse_count:3d}  "
                f"running={'ON' if sample.running else 'OFF'}  "
                f"state={state} ({labels[state]})  "
                f"p={confidence:5.1%}"
            )

    finally:
        if feed is not None:
            await feed.close()


# ---------------------------------------------------------------------------
# STATUS / MAINTENANCE
# ---------------------------------------------------------------------------

def show_status() -> None:
    with db_connect() as db:
        sessions = db.execute(
            """
            SELECT s.id, s.started_at, s.ended_at, s.note, COUNT(x.id)
            FROM sessions s
            LEFT JOIN samples x ON x.session_id = s.id
            GROUP BY s.id
            ORDER BY s.id
            """
        ).fetchall()

        models = db.execute(
            """
            SELECT id, created_at, n_states, score, samples, sessions
            FROM models
            ORDER BY id
            """
        ).fetchall()

    print(f"\nDatabase: {DB_PATH}")

    print("\nRecorded sessions:")
    if not sessions:
        print("  none")
    else:
        for row in sessions:
            session_id, started, ended, note, count = row
            suffix = f" | {note}" if note else ""
            print(
                f"  #{session_id}: {started} | "
                f"{count} sec | ended {ended or 'open'}{suffix}"
            )

    print("\nTrained models:")
    if not models:
        print("  none")
    else:
        for row in models:
            model_id, created, states, score, samples, session_count = row
            print(
                f"  #{model_id}: {created} | {states} states | "
                f"{samples} samples/{session_count} sessions | "
                f"score {score:.2f}"
            )

    loaded = load_latest_model()
    if loaded is not None:
        print()
        _model_id, _created_at, _score, model = loaded
        describe_model(model)


def delete_last_session() -> None:
    with db_connect() as db:
        row = db.execute(
            "SELECT id, started_at FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if row is None:
            print("\nNo sessions to delete.")
            return

        session_id, started_at = row
        answer = input(
            f"\nDelete session #{session_id} ({started_at})? [y/N]: "
        ).strip().lower()

        if answer == "y":
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            db.commit()
            print("Deleted.")
        else:
            print("Canceled.")


# ---------------------------------------------------------------------------
# MENU
# ---------------------------------------------------------------------------

def run_async(coro) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"\nError: {exc}")


def menu() -> None:
    init_db()

    while True:
        print(
            """
============================================================
 Laundry HMM
============================================================
  1. Record a laundry cycle
  2. Train / retrain HMM from all recorded cycles
  3. Watch live HMM state
  4. Show recordings and model
  5. Delete most recent recording
  0. Quit
"""
        )

        choice = input("> ").strip()

        if choice == "1":
            note = input(
                "Optional note (e.g. normal, towels, quick wash): "
            ).strip()
            run_async(record_cycle(note))

        elif choice == "2":
            try:
                train_hmm()
            except Exception as exc:
                print(f"\nTraining error: {exc}")

        elif choice == "3":
            run_async(live_monitor())

        elif choice == "4":
            show_status()

        elif choice == "5":
            delete_last_session()

        elif choice == "0":
            print("Bye.")
            return

        else:
            print("Pick 0-5.")


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\nBye.")
        sys.exit(0)
