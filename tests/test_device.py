"""Unit tests for device detection and configuration."""

from reconciliator.device import (
    configure_execution_threads,
    configure_mixed_precision,
    get_optimal_batch_size,
    resolve_device,
)


def test_resolve_device_auto() -> None:
    device = resolve_device("auto")
    assert device in ("cuda", "mps", "cpu")


def test_resolve_device_cpu() -> None:
    device = resolve_device("cpu")
    assert device == "cpu"


def test_configure_execution_threads() -> None:
    threads = configure_execution_threads(4)
    assert threads == 4


def test_get_optimal_batch_size() -> None:
    assert get_optimal_batch_size(32, "cpu") == 32
    assert get_optimal_batch_size(64, "cuda") == 256
    assert get_optimal_batch_size(32, "mps") == 64


def test_configure_mixed_precision() -> None:
    assert configure_mixed_precision("cpu") is False
    assert configure_mixed_precision("mps") is False
