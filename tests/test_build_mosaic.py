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

        result = build_mosaic(chunks, tmp_path)
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

        result = build_mosaic(chunks, tmp_path)
        assert result == tmp_path / "mosaic.vrt"

        # sub-VRT calls + 1 top-level call
        sub_calls = [b for b in built if b[0].name.startswith("sub_")]
        top_calls = [b for b in built if b[0].name == "mosaic.vrt"]
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

        result = _build_hierarchical_vrt(chunks, tmp_path)
        assert result.name == "mosaic.vrt"

        sub_entries = [b for b in built if b[0].startswith("sub_")]
        assert len(sub_entries) == 3  # 10000 + 10000 + 5000
        assert sub_entries[0][1] == _SUB_VRT_BATCH_SIZE
        assert sub_entries[1][1] == _SUB_VRT_BATCH_SIZE
        assert sub_entries[2][1] == 5_000

        # top-level references 3 sub-VRTs
        top = [b for b in built if b[0] == "mosaic.vrt"]
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

        _build_hierarchical_vrt(chunks, tmp_path)

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
            if vrt_path.name == "mosaic.vrt":
                top_call_paths.extend(src_paths)

        monkeypatch.setattr(
            "terrain_stitcher.functions.DownloaderBase._build_vrt",
            fake_build_vrt,
        )

        _build_hierarchical_vrt(chunks, tmp_path)

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

        result = _build_hierarchical_vrt(chunks, tmp_path)
        assert result.name == "mosaic.vrt"
        assert "sub_00000.vrt" in built
        assert "mosaic.vrt" in built

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

        result = _build_hierarchical_vrt([], tmp_path)
        assert result.name == "mosaic.vrt"
        # No sub-VRTs, one top-level with 0 sources
        top = [b for b in built if b[0] == "mosaic.vrt"]
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
            build_mosaic(chunks, tmp_path)

        assert "gdalbuildvrt failed" in str(exc.value)
        assert "could not open chunk_0_0.tif" in str(exc.value)
