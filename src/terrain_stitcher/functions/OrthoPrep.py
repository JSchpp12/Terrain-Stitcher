import enum
import io
import itertools
import json
import os
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass

from tqdm import tqdm
from zipfile import ZipFile
from PIL import Image as pImage
import psutil

from terrain_stitcher.sources import ImageDataWriter


# Image file extensions accepted inside an ortho archive.
# The ArcGIS import path ships PNGs; the USGS path ships TIFFs.
class ImageExtensionType(enum.Enum):
    TIF = ".tif"
    TIFF = ".tiff"
    PNG = ".png"


def _extension_values(key) -> set:
    """Normalize a compare key to a set of lowercase extension strings.

    Accepts an :class:`ImageExtensionType` enum class (any of its members),
    a single enum member, a single extension string, or a collection of
    any of those.
    """
    if isinstance(key, type) and issubclass(key, enum.Enum):
        return {m.value.lower() for m in key}
    if isinstance(key, enum.Enum):
        return {key.value.lower()}
    if isinstance(key, (tuple, list, set, frozenset)):
        out = set()
        for k in key:
            out.add(k.value.lower() if isinstance(k, enum.Enum) else str(k).lower())
        return out
    return {str(key).lower()}


def compareExtension(element: os.PathLike, key) -> bool:
    """Match a file's extension against ``key``.

    ``key`` may be an :class:`ImageExtensionType` enum class (matches any of
    its members), a single enum member, a single extension string, or a
    collection of those. Comparison is case-insensitive.
    """
    _, extension = os.path.splitext(element)
    if not extension:
        return False
    return extension.lower() in _extension_values(key)


@dataclass
class OrthoTask:
    zipPath: str
    outputDir: str
    scaleFactor: float


def _findMetadataPath(zipPath: str) -> str:
    """Return the path to the ``.json``/``.txt`` sidecar sharing the zip's name.

    The metadata sidecar sits next to the zip and shares its base name
    (e.g. ``chunk.zip`` -> ``chunk.json`` or ``chunk.txt``).
    """
    base = os.path.splitext(zipPath)[0]
    for ext in (".json", ".txt"):
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError(f"No .json/.txt metadata sidecar found next to {zipPath}")


def _findImageEntryName(zf: ZipFile) -> str:
    """Return the name of the first image entry inside ``zf``."""
    for name in zf.namelist():
        if compareExtension(name, ImageExtensionType):
            return name
    raise Exception("No image entry found in zip archive")


def processOrthoImage(task: OrthoTask) -> tuple:
    """Process one ortho zip end-to-end entirely in memory.

    Reads the metadata sidecar and the image directly from the zip archive
    (no extraction to disk), resizes if ``scaleFactor`` differs from 1.0,
    and writes the output PNG plus a copy of the sidecar JSON into
    ``outputDir``.

    Returns ``(chunkName, boundsJSON)`` for the aggregate manifest.
    """
    base = os.path.basename(task.zipPath)
    chunkName, _ = os.path.splitext(base)

    # Read the metadata sidecar that shares the zip's base name.
    metadataPath = _findMetadataPath(task.zipPath)
    with open(metadataPath) as f:
        jData = json.load(f)
    imageInfo = ImageDataWriter.fromDict(jData)

    # Read the image straight from the zip into memory.
    with ZipFile(task.zipPath) as zf:
        entryName = _findImageEntryName(zf)
        imageBytes = zf.read(entryName)

    im = pImage.open(io.BytesIO(imageBytes))

    if task.scaleFactor != 1.0:
        new_width = im.width * task.scaleFactor
        new_height = im.height * task.scaleFactor
        im = im.resize((int(new_width), int(new_height)), resample=pImage.LANCZOS)

    dstOrthoPath = os.path.join(task.outputDir, f"{chunkName}.png")
    im.save(dstOrthoPath)

    # Write the sidecar JSON (metadata) alongside the output image.
    sidecarPath = os.path.join(task.outputDir, f"{chunkName}.json")
    with open(sidecarPath, "w") as fJson:
        json.dump(imageInfo.toJSON(), fJson)

    return chunkName, imageInfo.bounds.toJSON()


class _StreamingInfoFile:
    """Write ``height_info.json`` incrementally.

    Instead of building a list of every image entry in memory and dumping
    it at the end, entries are written to the file handle as each image
    finishes processing.  This keeps the manifest's memory footprint
    constant regardless of how many images are processed.
    """

    def __init__(self, path, elevation_files=None):
        self._path = path
        self._f = open(path, "w")
        self._first = True
        self._f.write("{")
        if elevation_files:
            self._f.write(
                '"elevation_files": ' + json.dumps(list(elevation_files)) + ", "
            )
        self._f.write('"images": [')

    def append(self, name, boundsJSON):
        """Write one ``{"name": ..., "bounds": ...}`` entry."""
        if not self._first:
            self._f.write(", ")
        self._first = False
        self._f.write(json.dumps({"name": name, "bounds": boundsJSON}))

    def close(self):
        if self._f is not None:
            self._f.write("]}")
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# --- Concurrency window sizing ----------------------------------------------
#
# Each worker holds one decoded ortho image (and, during resize, a second
# buffer for the result) in memory for the duration of its task.  The number
# of concurrent workers is therefore bounded by available RAM divided by the
# per-image pixel-buffer footprint, not by CPU core count.  WorkerWindowSizer
# probes the first image to estimate that footprint and sizes the thread-pool
# window accordingly.


class WorkerWindowSizer:
    """Size the concurrency window for ortho-image processing.

    The number of concurrent workers is bounded by available RAM divided
    by the per-image pixel-buffer footprint, not by CPU core count.  This
    class probes the first image to estimate that footprint and divides
    available RAM by that cost, so peak memory stays proportional to the
    number of workers rather than to the dataset size.
    """

    # Extra headroom for LANCZOS intermediate buffers and Python/PIL object
    # overhead.  The (1 + scaleFactor**2) term already accounts for the
    # source and destination buffers coexisting during resize, so this only
    # needs to cover the smaller per-image overheads.
    _MEMORY_SAFETY_FACTOR = 1.25

    MIN_WORKERS = 1
    MAX_WORKERS = os.cpu_count() or 4

    def __init__(self, scaleFactor: float):
        self._scaleFactor = scaleFactor

    @staticmethod
    def _availableMemoryBytes() -> int:
        """Available physical memory in bytes (via psutil)."""
        return psutil.virtual_memory().available

    @staticmethod
    def _probeImageFootprintBytes(zipPath: str) -> int:
        """Decode the first image from a zip and return its pixel-buffer size.

        Used to estimate the per-image memory cost so the concurrency
        window can be sized against available RAM.  The cost of decoding
        one image is negligible relative to processing the full dataset.
        """
        with ZipFile(zipPath) as zf:
            entryName = _findImageEntryName(zf)
            imageBytes = zf.read(entryName)
        with pImage.open(io.BytesIO(imageBytes)) as im:
            im.load()
            return len(im.tobytes())

    def computeNumWorkers(self, zipPath: str) -> int:
        """Size the worker pool from a probe image and available RAM.

        Returns a worker count in ``[MIN_WORKERS, MAX_WORKERS]`` that
        keeps peak memory proportional to the number of workers.
        """
        perImageBytes = self._probeImageFootprintBytes(zipPath)

        # During resize both the source and destination pixel buffers are
        # alive simultaneously.  The destination scales quadratically
        # (area = width * height, both scale linearly).  When scaleFactor
        # is 1.0 no resize occurs, so only the single source buffer counts.
        if self._scaleFactor != 1.0:
            peakPerImage = perImageBytes * (1 + self._scaleFactor * self._scaleFactor)
        else:
            peakPerImage = perImageBytes

        # Modest safety margin for LANCZOS intermediates and Python/PIL
        # object overhead.
        peakPerImage = max(1, int(peakPerImage * self._MEMORY_SAFETY_FACTOR))

        memoryBudget = self._availableMemoryBytes()
        return max(
            self.MIN_WORKERS,
            min(self.MAX_WORKERS, int(memoryBudget // peakPerImage)),
        )


#
# ConcurrentZipReader drives a rolling futures window over the lazy zip
# iterator so only a small batch of Future objects is alive at any time.
# Worker-pool sizing is delegated to :class:`WorkerWindowSizer`.


class ConcurrentZipReader:
    """Process ortho zips through a memory-bounded concurrency window.

    Zips are consumed lazily from an iterator and submitted through a
    rolling futures window.  Worker-pool sizing is delegated to
    :class:`WorkerWindowSizer`, which probes the first image's
    pixel-buffer footprint and divides available RAM by that cost.
    """

    def __init__(self, scaleFactor: float):
        self._scaleFactor = scaleFactor
        self._sizer = WorkerWindowSizer(scaleFactor)

    @staticmethod
    def iterZipPaths(inputDir):
        """Lazily yield paths to ``.zip`` files in ``inputDir`` via ``os.scandir``.

        Uses ``os.scandir`` (an iterator) rather than ``os.listdir`` (which
        materializes the full directory listing) so directories with millions
        of entries don't require holding every name in memory at once.
        """
        with os.scandir(inputDir) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.lower().endswith(".zip"):
                    yield entry.path

    def process(self, zipPaths, outputDir, onResult, total=None):
        """Run the rolling-window executor over ``zipPaths``.

        ``zipPaths`` is a lazily-consumed iterable of zip file paths.
        ``onResult(name, boundsJSON)`` is called in the main thread for
        each completed image.  ``total``, when provided, drives the tqdm
        progress bar.
        """
        zipIter = iter(zipPaths)

        # Pull the first zip to probe its memory footprint; if there are
        # no zips at all there is nothing to do.
        try:
            firstZip = next(zipIter)
        except StopIteration:
            return

        numWorkers = self._sizer.computeNumWorkers(firstZip)

        # Re-chain the first zip so it is also processed (not just probed).
        allZips = itertools.chain([firstZip], zipIter)

        with tqdm(total=total, desc="Processing Ortho Images") as pbar:
            with ThreadPoolExecutor(max_workers=numWorkers) as executor:
                futures = set()
                # Prime the window with up to numWorkers tasks.
                for zipPath in itertools.islice(allZips, numWorkers):
                    futures.add(
                        executor.submit(
                            processOrthoImage,
                            OrthoTask(zipPath, outputDir, self._scaleFactor),
                        )
                    )

                while futures:
                    done, futures = wait(futures, return_when=FIRST_COMPLETED)
                    for f in done:
                        name, bounds = f.result()
                        onResult(name, bounds)
                        pbar.update(1)
                    # Refill: submit one new task for each that completed.
                    for zipPath in itertools.islice(allZips, len(done)):
                        futures.add(
                            executor.submit(
                                processOrthoImage,
                                OrthoTask(zipPath, outputDir, self._scaleFactor),
                            )
                        )


def main(inputDir, outputDir, scaleFactor, elevation_files=None):
    if not os.path.isdir(inputDir):
        raise Exception("Input directory does not exist")

    if not os.path.isdir(outputDir):
        os.mkdir(outputDir)

    # Count zips for the progress bar (fast: just directory-entry iteration,
    # no file reads).
    with os.scandir(inputDir) as entries:
        totalZips = sum(
            1 for e in entries if e.is_file() and e.name.lower().endswith(".zip")
        )

    infoFilePath = os.path.join(outputDir, "height_info.json")

    if totalZips == 0:
        with open(infoFilePath, "w") as f:
            data = {"images": []}
            if elevation_files:
                data["elevation_files"] = list(elevation_files)
            json.dump(data, f)
        return

    # Write to a temp file and atomically rename on success so a crash or
    # exception never leaves a half-written manifest.
    tempInfoPath = infoFilePath + ".tmp"

    try:
        with _StreamingInfoFile(tempInfoPath, elevation_files) as infoWriter:
            reader = ConcurrentZipReader(scaleFactor)
            reader.process(
                ConcurrentZipReader.iterZipPaths(inputDir),
                outputDir,
                infoWriter.append,
                total=totalZips,
            )

        os.replace(tempInfoPath, infoFilePath)
    except BaseException:
        if os.path.exists(tempInfoPath):
            os.remove(tempInfoPath)
        raise
