"""Tests for dynamic parameter-safe batch sizing in WEMI entity repository."""

from unittest.mock import MagicMock, patch

from reconciliator.database import (
    agents_table,
    entity_keys_table,
    identifiers_table,
    manifestations_table,
    work_relationships_table,
    works_table,
)
from reconciliator.database.ops import calculate_batch_size
from reconciliator.database.repository import WEMIRepository
from reconciliator.models import (
    AgentEntity,
    ManifestationEntity,
    WorkEntity,
)


def test_calculate_batch_size_per_table() -> None:
    """Verify that calculate_batch_size scales inversely with column count."""
    max_params = 32767

    # Narrow tables
    id_size = calculate_batch_size(len(identifiers_table.c), max_parameters_per_batch=max_params)
    assert id_size >= 4000  # identifiers has ~5 columns: ~6,553 rows

    key_size = calculate_batch_size(len(entity_keys_table.c), max_parameters_per_batch=max_params)
    assert key_size >= 5000  # entity_keys has 4 columns: ~8,191 rows

    rel_size = calculate_batch_size(
        len(work_relationships_table.c), max_parameters_per_batch=max_params
    )
    assert rel_size >= 5000  # work_relationships has 6 columns: ~5,461 rows

    # Wide tables
    work_size = calculate_batch_size(len(works_table.c), max_parameters_per_batch=max_params)
    assert 1000 <= work_size <= 3500

    manif_size = calculate_batch_size(
        len(manifestations_table.c), max_parameters_per_batch=max_params
    )
    assert 900 <= manif_size <= 2000

    # Ensure wider tables have strictly smaller or equal batch sizes than narrower tables
    assert manif_size < id_size
    assert work_size < key_size


def test_insert_agents_batch_uses_dynamic_chunk_size() -> None:
    """Verify insert_agents_batch dynamically calculates chunk size when chunk_size is None."""
    repo = MagicMock(spec=WEMIRepository)
    agents = [
        AgentEntity(
            source="src", source_id=f"a_{i}", name=f"Agent {i}", name_normalized=f"agent {i}"
        )
        for i in range(10)
    ]

    # Call the actual unbound method with mocked connection
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [
        (i + 1, "src", f"a_{i}") for i in range(10)
    ]

    with patch(
        "reconciliator.database.repository.calculate_batch_size", wraps=calculate_batch_size
    ) as mock_calc:
        WEMIRepository.insert_agents_batch(repo, agents, conn=mock_conn, chunk_size=None)
        # calculate_batch_size should have been called with agents_table column count
        mock_calc.assert_any_call(num_columns=len(agents_table.c))


def test_insert_works_batch_uses_dynamic_chunk_size() -> None:
    """Verify insert_works_batch dynamically calculates chunk size when chunk_size is None."""
    repo = MagicMock(spec=WEMIRepository)
    works = [
        WorkEntity(
            source="src", source_id=f"w_{i}", title=f"Work {i}", title_normalized=f"work {i}"
        )
        for i in range(10)
    ]

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [
        (i + 1, "src", f"w_{i}") for i in range(10)
    ]

    with patch(
        "reconciliator.database.repository.calculate_batch_size", wraps=calculate_batch_size
    ) as mock_calc:
        WEMIRepository.insert_works_batch(repo, works, conn=mock_conn, chunk_size=None)
        mock_calc.assert_any_call(num_columns=len(works_table.c))


def test_insert_manifestations_batch_uses_dynamic_chunk_size() -> None:
    """Verify insert_manifestations_batch dynamically calculates chunk size when chunk_size is None."""
    repo = MagicMock(spec=WEMIRepository)
    manifs = [
        ManifestationEntity(
            source="src", source_id=f"m_{i}", title=f"Manif {i}", title_normalized=f"manif {i}"
        )
        for i in range(10)
    ]

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [
        (i + 1, "src", f"m_{i}") for i in range(10)
    ]

    with patch(
        "reconciliator.database.repository.calculate_batch_size", wraps=calculate_batch_size
    ) as mock_calc:
        WEMIRepository.insert_manifestations_batch(repo, manifs, conn=mock_conn, chunk_size=None)
        mock_calc.assert_any_call(num_columns=len(manifestations_table.c))
