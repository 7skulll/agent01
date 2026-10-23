from scripts.audit_crud_results import coverage


def test_chunk_cutoff_precedes_file_deduplication():
    row = {
        "case": {"tags": ["crud-case"], "relevant_file_ids": ["a", "b"]},
        "status": "completed",
        "sources": [{"file_id": "a"}] * 10 + [{"file_id": "b"}],
    }
    result = coverage([row], chunks=10)
    assert result["known_source_coverage"] == 0.5
    assert result["all_known_sources_found"] == 0
    assert coverage([row], chunks=11)["all_known_sources_found"] == 1


def test_exclusion_and_failure_denominator_are_explicit():
    row = {
        "case": {"tags": ["flagged"], "relevant_file_ids": ["a"]},
        "status": "failed",
        "sources": [],
    }
    assert coverage([row], chunks=10)["known_source_coverage"] == 0
    assert coverage([row], chunks=10, excluded={"flagged"})["known_source_coverage"] is None
