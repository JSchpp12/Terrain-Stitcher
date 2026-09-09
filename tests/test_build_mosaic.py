"""Tests for build_mosaic and the hierarchical VRT (VRT-of-VRTs) path.

The hierarchical path is exercised by monkeypatching _build_vrt so no real
GDAL dependency is needed; the tests verify dispatch logic, batch sizes,
sorting, and sub-VRT/top-level wiring.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from terrain_stitcher.functions.DownloaderBase import (
    _HIERARCHICAL_VRT_THRESHOLD,
    _SUB_VRT_BATCH_SIZE,
    _build_hierarchical_vrt,
    _parse_chunk_coords,
    build_mosaic,
)


# ---------------------------------------------------------------------------
# _parse_chunk_coords
# ---------------------------------------------------------------------------


class TestParseChunkCoords:
    def test_standard_filename(self):
        assert _parse_chunk_coords(Path("aoi_chunks/chunk_3_5.tif")) == (5, 3)

    def test_large_coords(self):
        assert _parse_chunk_coords(Path("chunk_1352_800.tif")) == (800, 1352)

    def test_full_path(self):
        assert _parse_chunk_coords(Path("F:/data/aoi_chunks/chunk_0_100.tif")) == (
            100,
            0,
        )

    def test_non_chunk_filename_returns_default(self):
        assert _parse_chunk_coords(Path("mosaic.vrt")) == (0, 0)

    def test_non_numeric_returns_default(self):
        assert _parse_chunk_coords(Path("chunk_abc_def.tif")) == (0, 0)


# ---------------------------------------------------------------------------
# build_mosaic dispatch
# ---------------------------------------------------------------------------


def _make_chunks(tmp_path, n):
    """Create n dummy chunk files named chunk_{col}_{row}.tif."""
    paths = []
    for i in range(n):
        col = i % 100
        row = i // 100
        p = tmp_path / f"chunk_{col}_{row}.tif"
        p.write_bytes(b"x")
        paths.append(p)
    return paths


class TestBuildMosaicDispatch:
    def test_below_threshold_uses_single_vrt(self, tmp_path, monkeypatch):
        """Below the threshold the existing single-VRT subprocess path runs."""
        chunks = _make_chunks(tmp_path, 100)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            # gdalbuildvrt writes the VRT; mimic by creating the file
            vrt_arg = cmd[-1]
            Path(vrt_arg).write_text("<VRTDataset/>")
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", fake_run)

        result = build_mosaic(chunks, tmp_path, 1)
        assert result == tmp_path / "mosaic.vrt"
        assert len(calls) == 1  # exactly one gdalbuildvrt call
        assert result.is_file()

    def test_above_threshold_uses_hierarchical(self, tmp_path, monkeypatch):
        """Above the threshold the hierarchical VRT path runs."""
        n = _HIERARCHICAL_VRT_THRESHOLD + 1
        chunks = _make_chunks(tmp_path, n)

        built = []

        def fake_build_vrt(src_paths, vrt_path, td):
            vrt_path.write_text("<VRTDataset/>")
            built.append((vrt_path, len(src_paths)))

        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            fake_build_vrt,
        )

        result = build_mosaic(chunks, tmp_path, 1)
        assert result == tmp_path / "mosaic.vrt"

        # sub-VRT calls + 1 top-level call
        sub_calls = [b for b in built if b[0].name.startswith("sub_")]
        top_calls = [b for b in built if b[0].name.startswith("mosaic")]
        assert len(top_calls) == 1

        expected_batches = (
            n + _SUB_VRT_BATCH_SIZE - 1
        ) // _SUB_VRT_BATCH_SIZE
        assert len(sub_calls) == expected_batches
        # top-level VRT references every sub-VRT
        assert top_calls[0][1] == expected_batches


# ---------------------------------------------------------------------------
# _build_hierarchical_vrt internals
# ---------------------------------------------------------------------------


class TestBuildHierarchicalVrt:
    def test_batch_sizes(self, tmp_path, monkeypatch):
        """Sub-VRTs receive exactly _SUB_VRT_BATCH_SIZE chunks, except the
        last batch which gets the remainder."""
        n = 25_000
        chunks = _make_chunks(tmp_path, n)

        built = []

        def fake_build_vrt(src_paths, vrt_path, td):
            vrt_path.write_text("<VRTDataset/>")
            built.append((vrt_path.name, len(src_paths)))

        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            fake_build_vrt,
        )

        result = _build_hierarchical_vrt(chunks, tmp_path, 1)
        assert result.name == "mosaic.vrt"

        sub_entries = [b for b in built if b[0].startswith("sub_")]
        assert len(sub_entries) == 3  # 10000 + 10000 + 5000
        assert sub_entries[0][1] == _SUB_VRT_BATCH_SIZE
        assert sub_entries[1][1] == _SUB_VRT_BATCH_SIZE
        assert sub_entries[2][1] == 5_000

        # top-level references 3 sub-VRTs
        top = [b for b in built if b[0].startswith("mosaic")]
        assert top[0][1] == 3

    def test_chunks_sorted_spatially(self, tmp_path, monkeypatch):
        """Chunks are sorted by (row, col) before batching so each sub-VRT
        covers a contiguous spatial block."""
        # Create chunks in reverse order
        chunks = []
        for row in range(10):
            for col in range(10):
                p = tmp_path / f"chunk_{col}_{row}.tif"
                p.write_bytes(b"x")
                chunks.append(p)

        # Reverse the list to simulate unsorted input
        chunks.reverse()

        batch_contents = []

        def fake_build_vrt(src_paths, vrt_path, td):
            vrt_path.write_text("<VRTDataset/>")
            if vrt_path.name.startswith("sub_"):
                batch_contents.append(list(src_paths))

        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            fake_build_vrt,
        )

        _build_hierarchical_vrt(chunks, tmp_path, 1)

        # First chunk in the first batch should be (row=0, col=0)
        first = Path(batch_contents[0][0])
        assert _parse_chunk_coords(first) == (0, 0)

        # Second chunk should be (row=0, col=1)
        second = Path(batch_contents[0][1])
        assert _parse_chunk_coords(second) == (0, 1)

    def test_top_level_vrt_references_sub_vrts(self, tmp_path, monkeypatch):
        """The top-level VRT build call receives the sub-VRT file paths."""
        n = 100
        chunks = _make_chunks(tmp_path, n)

        top_call_paths = []

        def fake_build_vrt(src_paths, vrt_path, td):
            vrt_path.write_text("<VRTDataset/>")
            if vrt_path.name.startswith("mosaic"):
                top_call_paths.extend(src_paths)

        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            fake_build_vrt,
        )

        _build_hierarchical_vrt(chunks, tmp_path, 1)

        # Should reference 1 sub-VRT (100 chunks < batch size)
        assert len(top_call_paths) == 1
        assert "sub_00000.vrt" in top_call_paths[0]

    def test_single_batch(self, tmp_path, monkeypatch):
        """When chunks fit in one batch there is exactly one sub-VRT."""
        n = 50
        chunks = _make_chunks(tmp_path, n)

        built = []

        def fake_build_vrt(src_paths, vrt_path, td):
            vrt_path.write_text("<VRTDataset/>")
            built.append(vrt_path.name)

        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            fake_build_vrt,
        )

        result = _build_hierarchical_vrt(chunks, tmp_path, 1)
        assert result.name == "mosaic.vrt"
        assert "sub_00000.vrt.partial" in built
        assert any(n.startswith("mosaic") for n in built)

    def test_empty_chunk_list(self, tmp_path, monkeypatch):
        """An empty chunk list produces zero sub-VRTs and a top-level VRT
        with zero sources."""
        built = []

        def fake_build_vrt(src_paths, vrt_path, td):
            vrt_path.write_text("<VRTDataset/>")
            built.append((vrt_path.name, len(src_paths)))

        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            fake_build_vrt,
        )

        result = _build_hierarchical_vrt([], tmp_path, 1)
        assert result.name == "mosaic.vrt"
        # No sub-VRTs, one top-level with 0 sources
        top = [b for b in built if b[0].startswith("mosaic")]
        assert len(top) == 1
        assert top[0][1] == 0


# ---------------------------------------------------------------------------
# Error visibility in the single-VRT path
# ---------------------------------------------------------------------------


class TestSingleVrtErrorVisibility:
    def test_subprocess_failure_surfaces_stderr(self, tmp_path, monkeypatch):
        """When gdalbuildvrt fails the captured stderr is included in the
        RuntimeError message."""
        chunks = _make_chunks(tmp_path, 10)

        def fake_run(cmd, **kwargs):
            raise __import__("subprocess").CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr="Error: could not open chunk_0_0.tif",
            )

        monkeypatch.setattr("subprocess.run", fake_run)

        with pytest.raises(RuntimeError) as exc:
            build_mosaic(chunks, tmp_path, 1)

        assert "gdalbuildvrt failed" in str(exc.value)
        assert "could not open chunk_0_0.tif" in str(exc.value)
# ---------------------------------------------------------------------------
# VRT state persistence (vrt_state.json)
# ---------------------------------------------------------------------------

import json
import os

from terrain_stitcher.functions.DownloaderBase import (
    VRT_STATE_FILENAME,
    _VRT_STATE_VERSION,
    _batch_fingerprint,
    _build_sub_vrt_file,
    _empty_vrt_state,
    _load_vrt_state,
    _plan_sub_vrt_batches,
    _save_vrt_state,
    _top_level_fingerprint,
)


def _make_grid_chunks(tmp_path, n_rows, n_cols, skip=None):
    """Create chunk files for a grid, optionally skipping some coordinates."""
    paths = []
    for row in range(n_rows):
        for col in range(n_cols):
            if skip and (row, col) in skip:
                continue
            p = tmp_path / f"chunk_{col}_{row}.tif"
            p.write_bytes(b"x")
            paths.append(p)
    return paths


class TestVRTState:
    def test_load_missing_returns_empty(self, tmp_path):
        state = _load_vrt_state(tmp_path)
        assert state["version"] == _VRT_STATE_VERSION
        assert state["sub_vrts"] == {}
        assert state["top_level"] is None

    def test_load_corrupt_returns_empty(self, tmp_path):
        (tmp_path / VRT_STATE_FILENAME).write_text("not json{{{")
        state = _load_vrt_state(tmp_path)
        assert state["version"] == _VRT_STATE_VERSION
        assert state["sub_vrts"] == {}

    def test_load_incompatible_version_returns_empty(self, tmp_path):
        (tmp_path / VRT_STATE_FILENAME).write_text('{"version": 99, "sub_vrts": {}}')
        state = _load_vrt_state(tmp_path)
        assert state["version"] == _VRT_STATE_VERSION

    def test_save_and_roundtrip(self, tmp_path):
        state = _empty_vrt_state()
        state["sub_vrts"]["00000"] = {"file": "sub_00000.vrt", "complete": True}
        _save_vrt_state(tmp_path, state)
        loaded = _load_vrt_state(tmp_path)
        assert loaded["sub_vrts"]["00000"]["complete"] is True

    def test_save_is_atomic_no_tmp_left(self, tmp_path):
        state = _empty_vrt_state()
        _save_vrt_state(tmp_path, state)
        assert (tmp_path / VRT_STATE_FILENAME).is_file()
        assert not (tmp_path / (VRT_STATE_FILENAME + ".tmp")).is_file()


# ---------------------------------------------------------------------------
# Deterministic batch planning
# ---------------------------------------------------------------------------

class TestPlanSubVrtBatches:
    def test_batch_ids_deterministic(self, tmp_path):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        batches, (nr, nc) = _plan_sub_vrt_batches(chunks, (3, 4), batch_size=4)
        assert nc == 4
        assert nr == 3
        # (row * 4 + col) // 4 => batch 0: row 0 all; batch 1: row 1; batch 2: row 2
        assert len(batches) == 3
        assert batches[0][0] == 0
        assert batches[1][0] == 1
        assert batches[2][0] == 2
        # Each batch has exactly 4 chunks
        for bid, paths in batches:
            assert len(paths) == 4

    def test_adding_failed_chunk_keeps_batch(self, tmp_path):
        """Adding a previously failed chunk does not shift other chunks into
        different batches."""
        n_rows, n_cols = 3, 4
        batch_size = 4
        grid_shape = (n_rows, n_cols)

        full = _make_grid_chunks(tmp_path, n_rows, n_cols)
        batches_full, _ = _plan_sub_vrt_batches(full, grid_shape, batch_size)
        full_map = {}
        for bid, paths in batches_full:
            for p in paths:
                full_map[_parse_chunk_coords(p)] = bid

        # Subset: remove (1,2) and (2,3)
        missing = {(1, 2), (2, 3)}
        subset = _make_grid_chunks(tmp_path, n_rows, n_cols, skip=missing)
        batches_subset, _ = _plan_sub_vrt_batches(subset, grid_shape, batch_size)
        subset_map = {}
        for bid, paths in batches_subset:
            for p in paths:
                subset_map[_parse_chunk_coords(p)] = bid

        for rc, bid in subset_map.items():
            assert full_map[rc] == bid, (
                f"Chunk {rc} moved from batch {full_map[rc]} to {bid}"
            )

    def test_batch_size_can_be_overridden(self, tmp_path):
        chunks = _make_grid_chunks(tmp_path, 2, 4)
        batches, _ = _plan_sub_vrt_batches(chunks, (2, 4), batch_size=2)
        assert len(batches) == 4  # 8 chunks / 2 per batch
        for bid, paths in batches:
            assert len(paths) == 2

    def test_fallback_grid_from_coords(self, tmp_path):
        chunks = _make_grid_chunks(tmp_path, 3, 5)
        batches, (nr, nc) = _plan_sub_vrt_batches(chunks, None, batch_size=100)
        assert nc == 5
        assert nr == 3


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

class TestBatchFingerprint:
    def test_fingerprint_deterministic_regardless_of_order(self):
        coords_a = [(0, 0), (0, 1), (1, 0)]
        coords_b = [(1, 0), (0, 1), (0, 0)]
        assert _batch_fingerprint(0, coords_a, 100) == _batch_fingerprint(0, coords_b, 100)

    def test_fingerprint_changes_with_new_coord(self):
        fp1 = _batch_fingerprint(0, [(0, 0)], 100)
        fp2 = _batch_fingerprint(0, [(0, 0), (0, 1)], 100)
        assert fp1 != fp2

    def test_fingerprint_changes_with_batch_id(self):
        fp1 = _batch_fingerprint(0, [(0, 0)], 100)
        fp2 = _batch_fingerprint(1, [(0, 0)], 100)
        assert fp1 != fp2

    def test_fingerprint_changes_with_batch_size(self):
        fp1 = _batch_fingerprint(0, [(0, 0)], 100)
        fp2 = _batch_fingerprint(0, [(0, 0)], 200)
        assert fp1 != fp2

    def test_top_level_fingerprint_changes_with_sub_fps(self):
        fp1 = _top_level_fingerprint(["sha256:aaa", "sha256:bbb"])
        fp2 = _top_level_fingerprint(["sha256:aaa", "sha256:ccc"])
        assert fp1 != fp2

    def test_top_level_fingerprint_order_independent(self):
        fp1 = _top_level_fingerprint(["sha256:aaa", "sha256:bbb"])
        fp2 = _top_level_fingerprint(["sha256:bbb", "sha256:aaa"])
        assert fp1 == fp2


# ---------------------------------------------------------------------------
# Hierarchical VRT: resume, parallel, atomic publication
# ---------------------------------------------------------------------------

class TestHierarchicalResume:
    """Tests using a small monkeypatched batch size for fast multi-batch runs."""

    @pytest.fixture(autouse=True)
    def _small_batch_size(self, monkeypatch):
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._SUB_VRT_BATCH_SIZE", 5
        )

    def _make_fake(self, built, fail_batches=None):
        """Return a fake _build_vrt that records calls and optionally fails."""
        fail_batches = fail_batches or set()

        def fake(src_paths, vrt_path, td, force_subprocess=False):
            vrt_path = Path(vrt_path)
            name = vrt_path.name
            if name.startswith("sub_") and name.endswith(".partial"):
                bid = int(name.replace("sub_", "").replace(".vrt.partial", ""))
                if bid in fail_batches:
                    raise RuntimeError(f"simulated failure for batch {bid}")
            vrt_path.write_text("<VRTDataset/>")
            built.append((name, len(src_paths)))

        return fake

    def test_first_run_builds_all(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        built = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built),
        )
        result = _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))
        assert result == tmp_path / "mosaic.vrt"
        assert result.is_file()
        # 12 chunks / batch_size 5 = 3 batches (5+5+2)
        sub_calls = [b for b in built if b[0].startswith("sub_")]
        assert len(sub_calls) == 3
        # State file has all 3 entries
        state = _load_vrt_state(tmp_path)
        assert len(state["sub_vrts"]) == 3
        for k, v in state["sub_vrts"].items():
            assert v["complete"] is True
        assert state["top_level"] is not None

    def test_resume_skips_completed(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        built_first = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built_first),
        )
        _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))
        n_first = len(built_first)

        # Second run: nothing missing => zero build calls
        built_second = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built_second),
        )
        result = _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))
        assert result.is_file()
        assert len(built_second) == 0  # nothing rebuilt

    def test_resume_rebuilds_only_missing(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        built_first = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built_first),
        )
        _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))

        # Delete one sub-VRT to simulate interruption
        (tmp_path / "sub_00001.vrt").unlink()

        built_second = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built_second),
        )
        _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))

        # Only batch 1 (and its sub-VRT partial) should be rebuilt + top-level
        sub_rebuilt = [b for b in built_second if b[0].startswith("sub_")]
        assert len(sub_rebuilt) == 1
        assert sub_rebuilt[0][0] == "sub_00001.vrt.partial"
        # Top-level is also rebuilt (fingerprint set unchanged but we don't track
        # that; mosaic.vrt still exists so top-level may be reused)
        # Actually since top_fp doesn't change (same sub-fps), top-level is reused.

    def test_atomic_publication_no_final_on_failure(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        built = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built, fail_batches={1}),
        )
        with pytest.raises(RuntimeError, match="batch 00001"):
            _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))

        # Failed batch: no final .vrt, no state entry
        assert not (tmp_path / "sub_00001.vrt").exists()
        state = _load_vrt_state(tmp_path)
        assert "00001" not in state["sub_vrts"]
        # Other batches: recorded
        assert state["sub_vrts"].get("00000", {}).get("complete") is True
        assert state["sub_vrts"].get("00002", {}).get("complete") is True

    def test_partial_file_rebuilt(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        # Leave a stale partial file
        (tmp_path / "sub_00000.vrt.partial").write_text("stale")
        built = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built),
        )
        result = _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))
        # The stale partial is cleaned up; a fresh build happens
        sub_calls = [b for b in built if b[0].startswith("sub_")]
        assert len(sub_calls) == 3  # all 3 rebuilt (no prior state)
        assert (tmp_path / "sub_00000.vrt").is_file()
        assert not (tmp_path / "sub_00000.vrt.partial").exists()

    def test_state_corruption_rebuilds_all(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        # Corrupt the state file before running
        (tmp_path / VRT_STATE_FILENAME).write_text("corrupt{")
        built = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built),
        )
        result = _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))
        # All 3 sub-VRTs + top-level built from scratch
        sub_calls = [b for b in built if b[0].startswith("sub_")]
        assert len(sub_calls) == 3
        assert result.is_file()
        state = _load_vrt_state(tmp_path)
        assert len(state["sub_vrts"]) == 3

    def test_fingerprint_invalidation_single_batch(self, tmp_path, monkeypatch):
        # Grid 3x4, batch_size=5 (from the fixture)
        # row 0: cols 0-4 → batch 0; row 1: cols 0-4 → batch 1; row 2 → batch 2
        # Wait: batch_size=5, n_cols=4 → linear=row*4+col
        # row 0: 0-3 → batch 0; row 1: 4-7 → batch 0; row 2: 8-11 → batch 2
        # Hmm, batch_size=5 means: batch 0 = linear 0-4, batch 1 = linear 5-9, batch 2 = linear 10-14
        # With n_cols=4: batch 0 = (0,0),(0,1),(0,2),(0,3),(1,0); batch 1 = (1,1),(1,2),(1,3),(2,0),(2,1); batch 2 = (2,1),(2,2),(2,3)
        all_chunks = _make_grid_chunks(tmp_path, 3, 4)

        # Run 1: skip (1,2) (linear = 1*4+2 = 6, batch 1)
        subset = _make_grid_chunks(tmp_path, 3, 4, skip={(1, 2)})
        built1 = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built1),
        )
        _build_hierarchical_vrt(subset, tmp_path, 1, grid_shape=(3, 4))
        n_sub_1 = len([b for b in built1 if b[0].startswith("sub_")])

        # Run 2: all chunks (including the previously missing one)
        built2 = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built2),
        )
        _build_hierarchical_vrt(all_chunks, tmp_path, 1, grid_shape=(3, 4))
        sub_rebuilt = [b for b in built2 if b[0].startswith("sub_")]
        # Only batch 1 should be rebuilt (fingerprint changed)
        assert len(sub_rebuilt) == 1
        assert sub_rebuilt[0][0] == "sub_00001.vrt.partial"

    def test_worker_failure_records_successful(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        built = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built, fail_batches={1}),
        )
        with pytest.raises(RuntimeError):
            _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))
        # Batches 0 and 2 should be recorded before the error is raised
        state = _load_vrt_state(tmp_path)
        assert state["sub_vrts"].get("00000", {}).get("complete") is True
        assert state["sub_vrts"].get("00002", {}).get("complete") is True
        assert "00001" not in state["sub_vrts"]

    def test_top_level_rebuilt_when_fingerprint_changes(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        built1 = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built1),
        )
        _build_hierarchical_vrt(chunks, tmp_path, 1, grid_shape=(3, 4))

        # Add a new chunk (changing batch 0's fingerprint)
        new_chunk = tmp_path / "chunk_4_0.tif"
        new_chunk.write_bytes(b"x")
        chunks_with_new = chunks + [new_chunk]

        built2 = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built2),
        )
        _build_hierarchical_vrt(chunks_with_new, tmp_path, 1, grid_shape=(3, 5))
        # Top-level should be rebuilt (fingerprint set changed)
        top_rebuilt = [b for b in built2 if b[0].startswith("mosaic")]
        assert len(top_rebuilt) == 1


class TestParallelDispatch:
    """Tests that verify parallel dispatch behavior."""

    @pytest.fixture(autouse=True)
    def _small_batch_size(self, monkeypatch):
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._SUB_VRT_BATCH_SIZE", 5
        )

    def test_all_pending_batches_submitted(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        built = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built),
        )
        _build_hierarchical_vrt(chunks, tmp_path, 2, grid_shape=(3, 4))
        # All 3 sub-VRT batches were submitted
        sub_calls = [b for b in built if b[0].startswith("sub_")]
        assert len(sub_calls) == 3

    def test_final_order_sorted_by_batch_id(self, tmp_path, monkeypatch):
        chunks = _make_grid_chunks(tmp_path, 3, 4)
        top_sources = []
        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            self._make_fake(built=[], top_capture=top_sources),
        )
        _build_hierarchical_vrt(chunks, tmp_path, 2, grid_shape=(3, 4))
        assert len(top_sources) == 3
        # Verify they are in batch-id order
        names = [Path(s).name for s in top_sources]
        assert names == sorted(names)

    def _make_fake(self, built, top_capture=None, fail_batches=None):
        fail_batches = fail_batches or set()
        def fake(src_paths, vrt_path, td, force_subprocess=False):
            vrt_path = Path(vrt_path)
            name = vrt_path.name
            if name.startswith("sub_") and name.endswith(".partial"):
                bid = int(name.replace("sub_", "").replace(".vrt.partial", ""))
                if bid in fail_batches:
                    raise RuntimeError(f"simulated failure for batch {bid}")
            vrt_path.write_text("<VRTDataset/>")
            built.append((name, len(src_paths)))
            if name.startswith("mosaic") and top_capture is not None:
                top_capture.extend(src_paths)
        return fake

# ---------------------------------------------------------------------------
# Caller wiring (OrthoDownloader.run -> build_mosaic)
# ---------------------------------------------------------------------------

class TestCallerWiring:
    def test_ortho_run_passes_grid_and_workers(self, tmp_path, monkeypatch):
        """Verify OrthoDownloader.run passes grid_shape and vrt_workers to
        build_mosaic."""
        from unittest.mock import MagicMock
        from terrain_stitcher.functions.OrthoDownloader import OrthoDownloader
        import terrain_stitcher.functions.OrthoDownloader as ortho_mod

        # Mock ParseArea
        mock_area = MagicMock()
        mock_area.center.get_lat.return_value = 40.0
        mock_area.center.get_lon.return_value = -100.0
        mock_area.view_distance = 10.0
        monkeypatch.setattr(
            "terrain_stitcher.functions.OrthoDownloader.ParseArea",
            type("MockParseArea", (), {
                "fromJSONFile": staticmethod(lambda p: mock_area)
            }),
        )

        # Mock bbox functions
        monkeypatch.setattr(
            "terrain_stitcher.functions.OrthoDownloader.bbox_latlon_from_radius",
            lambda lat, lon, r: (39.0, -101.0, 41.0, -99.0),
        )
        monkeypatch.setattr(
            "terrain_stitcher.functions.OrthoDownloader.bbox_from_radius",
            lambda lat, lon, r: (-1000.0, -1000.0, 1000.0, 1000.0),
        )

        # Mock service resolution
        downloader = OrthoDownloader.__new__(OrthoDownloader)
        mock_service = MagicMock()
        mock_service.srs = 3857
        monkeypatch.setattr(downloader, "resolve_service", lambda loader, bbox: mock_service)
        monkeypatch.setattr(
            "terrain_stitcher.functions.OrthoDownloader.assert_lod_within_native",
            lambda zoom, svc: None,
        )

        # Mock chunk grid: 2 rows x 3 cols = 6 chunks
        mock_chunks = []
        for row in range(2):
            for col in range(3):
                mock_chunks.append({
                    "row": row, "col": col, "w": 256, "h": 256,
                    "xmin": 0.0, "ymin": 0.0, "xmax": 256.0, "ymax": 256.0,
                })
        monkeypatch.setattr(
            "terrain_stitcher.functions.OrthoDownloader.build_chunk_grid",
            lambda *a, **kw: mock_chunks,
        )

        # Mock download: return chunk paths
        chunk_paths = [
            tmp_path / f"chunk_{c['col']}_{c['row']}.tif" for c in mock_chunks
        ]
        monkeypatch.setattr(downloader, "download_chunks", lambda *a, **kw: (chunk_paths, []))

        # Capture build_mosaic call
        mosaic_calls = []
        def fake_build_mosaic(paths, td, vrt_workers=None, grid_shape=None):
            mosaic_calls.append({
                "paths": paths,
                "tmp_dir": td,
                "grid_shape": grid_shape,
                "vrt_workers": vrt_workers,
            })
            return td / "mosaic.vrt"
        monkeypatch.setattr(
            "terrain_stitcher.functions.OrthoDownloader.build_mosaic",
            fake_build_mosaic,
        )

        # Mock gdal2tiles
        monkeypatch.setattr(
            "terrain_stitcher.functions.OrthoDownloader.run_gdal2tiles",
            lambda *a, **kw: None,
        )

        downloader.run(
            shapefile_path="fake.json",
            outdir=str(tmp_path / "tiles"),
            zoom=19,
            xyz=True,
            resampling="lanczos",
            timeout=30,
            num_workers=2,
            chunk_px=256,
        )

        assert len(mosaic_calls) == 1
        call = mosaic_calls[0]
        assert call["grid_shape"] == (2, 3)
        assert call["vrt_workers"] == 2
        assert call["tmp_dir"] == Path("aoi_chunks")
