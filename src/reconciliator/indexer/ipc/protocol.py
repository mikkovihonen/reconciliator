"""Binary framing and serialization protocol for Reconciliator Vector Index IPC."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
from pathlib import Path
from typing import Any, Final, Optional

import numpy as np

MAGIC: Final[bytes] = b"RVCD"
HEADER_FORMAT: Final[str] = "!4sBII"
HEADER_SIZE: Final[int] = struct.calcsize(HEADER_FORMAT)  # 13 bytes

# Operation Codes
OP_PING: Final[int] = 1
OP_PONG: Final[int] = 2
OP_ERROR: Final[int] = 3
OP_GET_STATUS: Final[int] = 4
OP_STATUS_RESP: Final[int] = 5
OP_OPEN_INDEX: Final[int] = 6
OP_OPEN_RESP: Final[int] = 7
OP_ADD_VECTORS: Final[int] = 8
OP_ADD_RESP: Final[int] = 9
OP_REMOVE_IDS: Final[int] = 10
OP_REMOVE_RESP: Final[int] = 11
OP_SEARCH: Final[int] = 12
OP_SEARCH_RESP: Final[int] = 13
OP_SAVE: Final[int] = 14
OP_SAVE_RESP: Final[int] = 15
OP_LOAD: Final[int] = 16
OP_LOAD_RESP: Final[int] = 17
OP_VERIFY_METADATA: Final[int] = 18
OP_VERIFY_RESP: Final[int] = 19
OP_TOTAL_VECTORS: Final[int] = 20
OP_TOTAL_RESP: Final[int] = 21
OP_GET_METADATA: Final[int] = 22
OP_METADATA_RESP: Final[int] = 23
OP_GET_IDS: Final[int] = 24
OP_IDS_RESP: Final[int] = 25
OP_SHUTDOWN: Final[int] = 26
OP_SHUTDOWN_RESP: Final[int] = 27
OP_RESET: Final[int] = 28
OP_RESET_RESP: Final[int] = 29

# Dtype mappings
DTYPE_FLOAT32: Final[int] = 0
DTYPE_FLOAT16: Final[int] = 1
DTYPE_FLOAT64: Final[int] = 2

_DTYPE_TO_CODE = {
    np.dtype("float32"): DTYPE_FLOAT32,
    np.dtype("float16"): DTYPE_FLOAT16,
    np.dtype("float64"): DTYPE_FLOAT64,
}
_CODE_TO_DTYPE = {
    DTYPE_FLOAT32: np.float32,
    DTYPE_FLOAT16: np.float16,
    DTYPE_FLOAT64: np.float64,
}


def get_default_socket_path(base_dir: Path | str) -> Path:
    """Return stable, length-safe Unix domain socket path for given directory.

    On macOS, sockaddr_un.sun_path is capped at 104 chars. Using /tmp with a short
    SHA-256 hash ensures the socket path remains ~32 chars regardless of working directory depth.
    """
    abs_str = str(Path(base_dir).resolve())
    h = hashlib.sha256(abs_str.encode("utf-8")).hexdigest()[:12]
    return Path(f"/tmp/rec_vectord_{h}.sock")


def get_default_lock_path(base_dir: Path | str) -> Path:
    """Return stable lockfile path for coordinating daemon startup."""
    abs_str = str(Path(base_dir).resolve())
    h = hashlib.sha256(abs_str.encode("utf-8")).hexdigest()[:12]
    return Path(f"/tmp/rec_vectord_{h}.lock")


def send_frame(sock: socket.socket, opcode: int, payload: bytes = b"", msg_id: int = 0) -> None:
    """Pack and transmit a single frame over socket."""
    header = struct.pack(HEADER_FORMAT, MAGIC, opcode, msg_id, len(payload))
    sock.sendall(header + payload)


def recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Read exactly num_bytes from socket, raising EOFError on unexpected closure."""
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = sock.recv(min(num_bytes - len(buf), 65536))
        if not chunk:
            raise EOFError(
                f"Socket closed prematurely: expected {num_bytes} bytes, received {len(buf)} bytes"
            )
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> tuple[int, int, bytes]:
    """Read and unpack a single frame from socket.

    Returns:
        Tuple of (opcode, msg_id, payload).
    """
    header = recv_exact(sock, HEADER_SIZE)
    magic, opcode, msg_id, payload_len = struct.unpack(HEADER_FORMAT, header)
    if magic != MAGIC:
        raise ConnectionError(f"Invalid protocol magic: expected {MAGIC!r}, got {magic!r}")
    payload = recv_exact(sock, payload_len) if payload_len > 0 else b""
    return opcode, msg_id, payload


def encode_json(obj: Any) -> bytes:
    """Serialize Python dictionary or primitive to JSON bytes."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def decode_json(payload: bytes) -> Any:
    """Deserialize JSON bytes to Python object."""
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def encode_error(message: str, exc_type: str = "RuntimeError") -> bytes:
    """Serialize error payload."""
    return encode_json({"error": message, "type": exc_type})


def decode_error(payload: bytes) -> tuple[str, str]:
    """Deserialize error payload returning (message, exc_type)."""
    data = decode_json(payload)
    return data.get("error", "Unknown server error"), data.get("type", "RuntimeError")


def encode_add_vectors(
    index_key: str,
    vectors: np.ndarray,
    ids: Optional[list[int] | np.ndarray] = None,
) -> bytes:
    """Encode add_vectors request into binary payload."""
    key_bytes = index_key.encode("utf-8")
    key_len = len(key_bytes)

    if vectors.ndim == 1:
        vectors = np.expand_dims(vectors, axis=0)

    rows, cols = vectors.shape
    vec_c = np.ascontiguousarray(vectors)
    dtype_code = _DTYPE_TO_CODE.get(vec_c.dtype, DTYPE_FLOAT32)
    if dtype_code == DTYPE_FLOAT32 and vec_c.dtype != np.float32:
        vec_c = vec_c.astype(np.float32)

    vec_bytes = vec_c.tobytes()

    if ids is not None:
        id_arr = np.ascontiguousarray(ids, dtype=np.int64)
        if id_arr.ndim != 1:
            id_arr = id_arr.flatten()
        has_ids = 1
        id_count = len(id_arr)
        id_bytes = id_arr.tobytes()
    else:
        has_ids = 0
        id_count = 0
        id_bytes = b""

    # Layout:
    # 2 bytes: key_len (H)
    # key_bytes
    # 4 bytes: rows (I)
    # 4 bytes: cols (I)
    # 1 byte:  dtype_code (B)
    # 4 bytes: vec_bytes_len (I)
    # vec_bytes
    # 1 byte:  has_ids (B)
    # 4 bytes: id_count (I)
    # id_bytes
    header = struct.pack("!H", key_len) + key_bytes
    vec_header = struct.pack("!IIBI", rows, cols, dtype_code, len(vec_bytes))
    id_header = struct.pack("!BI", has_ids, id_count)

    return header + vec_header + vec_bytes + id_header + id_bytes


def decode_add_vectors(payload: bytes) -> tuple[str, np.ndarray, Optional[list[int]]]:
    """Decode add_vectors request payload."""
    offset = 0
    key_len = struct.unpack_from("!H", payload, offset)[0]
    offset += 2
    index_key = payload[offset : offset + key_len].decode("utf-8")
    offset += key_len

    rows, cols, dtype_code, vec_len = struct.unpack_from("!IIBI", payload, offset)
    offset += struct.calcsize("!IIBI")

    dt = _CODE_TO_DTYPE.get(dtype_code, np.float32)
    vec_bytes = payload[offset : offset + vec_len]
    offset += vec_len
    vectors = np.frombuffer(vec_bytes, dtype=dt).copy().reshape((rows, cols))

    has_ids, id_count = struct.unpack_from("!BI", payload, offset)
    offset += struct.calcsize("!BI")

    ids: Optional[list[int]] = None
    if has_ids == 1 and id_count > 0:
        id_bytes = payload[offset : offset + id_count * 8]
        ids = np.frombuffer(id_bytes, dtype=np.int64).tolist()

    return index_key, vectors, ids


def encode_add_resp(assigned_ids: list[int]) -> bytes:
    """Encode add_vectors response (assigned integer IDs)."""
    id_arr = np.ascontiguousarray(assigned_ids, dtype=np.int64)
    return struct.pack("!I", len(id_arr)) + id_arr.tobytes()


def decode_add_resp(payload: bytes) -> list[int]:
    """Decode add_vectors response."""
    if not payload:
        return []
    count = struct.unpack_from("!I", payload, 0)[0]
    if count == 0:
        return []
    return np.frombuffer(payload[4 : 4 + count * 8], dtype=np.int64).tolist()


def encode_search(index_key: str, query_vectors: np.ndarray, top_k: int = 50) -> bytes:
    """Encode search request payload."""
    key_bytes = index_key.encode("utf-8")
    key_len = len(key_bytes)

    if query_vectors.ndim == 1:
        query_vectors = np.expand_dims(query_vectors, axis=0)

    rows, cols = query_vectors.shape
    q_c = np.ascontiguousarray(query_vectors)
    dtype_code = _DTYPE_TO_CODE.get(q_c.dtype, DTYPE_FLOAT32)
    if dtype_code == DTYPE_FLOAT32 and q_c.dtype != np.float32:
        q_c = q_c.astype(np.float32)

    vec_bytes = q_c.tobytes()
    return (
        struct.pack("!H", key_len)
        + key_bytes
        + struct.pack("!III", top_k, rows, cols)
        + struct.pack("!BI", dtype_code, len(vec_bytes))
        + vec_bytes
    )


def decode_search(payload: bytes) -> tuple[str, np.ndarray, int]:
    """Decode search request payload."""
    offset = 0
    key_len = struct.unpack_from("!H", payload, offset)[0]
    offset += 2
    index_key = payload[offset : offset + key_len].decode("utf-8")
    offset += key_len

    top_k, rows, cols, dtype_code, vec_len = struct.unpack_from("!IIIBI", payload, offset)
    offset += struct.calcsize("!IIIBI")

    dt = _CODE_TO_DTYPE.get(dtype_code, np.float32)
    vec_bytes = payload[offset : offset + vec_len]
    query_vectors = np.frombuffer(vec_bytes, dtype=dt).copy().reshape((rows, cols))

    return index_key, query_vectors, top_k


def encode_search_resp(scores: np.ndarray, indices: np.ndarray) -> bytes:
    """Encode search response (similarity scores and matched IDs)."""
    s_arr = np.ascontiguousarray(scores, dtype=np.float32)
    idx_arr = np.ascontiguousarray(indices, dtype=np.int64)

    rows = s_arr.shape[0] if s_arr.ndim > 0 else 0
    k = s_arr.shape[1] if s_arr.ndim > 1 else 0

    s_bytes = s_arr.tobytes()
    idx_bytes = idx_arr.tobytes()

    return struct.pack("!II", rows, k) + s_bytes + idx_bytes


def decode_search_resp(payload: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Decode search response into (scores, indices) NumPy arrays."""
    if not payload:
        return np.empty((0, 0), dtype=np.float32), np.empty((0, 0), dtype=np.int64)

    rows, k = struct.unpack_from("!II", payload, 0)
    offset = 8
    total_elements = rows * k
    s_len = total_elements * 4  # float32 = 4 bytes
    idx_len = total_elements * 8  # int64 = 8 bytes

    scores = np.frombuffer(payload[offset : offset + s_len], dtype=np.float32).reshape((rows, k))
    indices = np.frombuffer(
        payload[offset + s_len : offset + s_len + idx_len], dtype=np.int64
    ).reshape((rows, k))

    return scores, indices


def encode_remove_ids(index_key: str, ids: list[int] | np.ndarray) -> bytes:
    """Encode remove_ids request payload."""
    key_bytes = index_key.encode("utf-8")
    key_len = len(key_bytes)
    id_arr = np.ascontiguousarray(ids, dtype=np.int64)
    return struct.pack("!HI", key_len, len(id_arr)) + key_bytes + id_arr.tobytes()


def decode_remove_ids(payload: bytes) -> tuple[str, list[int]]:
    """Decode remove_ids request payload."""
    key_len, count = struct.unpack_from("!HI", payload, 0)
    offset = 6
    index_key = payload[offset : offset + key_len].decode("utf-8")
    offset += key_len
    ids = np.frombuffer(payload[offset : offset + count * 8], dtype=np.int64).tolist()
    return index_key, ids


def encode_ids_resp(ids: list[int] | np.ndarray) -> bytes:
    """Encode active index IDs response."""
    id_arr = np.ascontiguousarray(ids, dtype=np.int64)
    return struct.pack("!I", len(id_arr)) + id_arr.tobytes()


def decode_ids_resp(payload: bytes) -> np.ndarray:
    """Decode active index IDs response."""
    if not payload:
        return np.empty((0,), dtype=np.int64)
    count = struct.unpack_from("!I", payload, 0)[0]
    if count == 0:
        return np.empty((0,), dtype=np.int64)
    return np.frombuffer(payload[4 : 4 + count * 8], dtype=np.int64)
