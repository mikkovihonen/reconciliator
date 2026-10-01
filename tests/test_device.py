"""Unit tests for device detection and configuration."""

import torch

from reconciliator.utils.device import (
    clear_accelerator_memory,
    configure_execution_threads,
    configure_mixed_precision,
    get_optimal_batch_size,
    is_gpu_available,
    resolve_compute_device,
    resolve_device,
)


def test_resolve_device_auto() -> None:
    device = resolve_device("auto")
    assert device in ("cuda", "mps", "cpu")


def test_resolve_device_cpu() -> None:
    device = resolve_device("cpu")
    assert device == "cpu"


def test_resolve_device_with_torch_device() -> None:
    cpu_torch_dev = torch.device("cpu")
    assert resolve_device(cpu_torch_dev) == "cpu"


def test_resolve_compute_device() -> None:
    cpu_dev = resolve_compute_device("cpu")
    assert isinstance(cpu_dev, torch.device)
    assert cpu_dev.type == "cpu"

    auto_dev = resolve_compute_device("auto")
    assert auto_dev.type in ("cuda", "mps", "cpu")

    # Already a torch.device
    dev_passthrough = resolve_compute_device(cpu_dev)
    assert dev_passthrough is cpu_dev


def test_is_gpu_available() -> None:
    avail = is_gpu_available()
    assert isinstance(avail, bool)


def test_clear_accelerator_memory() -> None:
    # Test clearing with None, str, and torch.device
    clear_accelerator_memory()
    clear_accelerator_memory("cpu")
    clear_accelerator_memory(torch.device("cpu"))


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
