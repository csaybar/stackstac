from __future__ import annotations

import itertools
from typing import ClassVar, Optional, Tuple, Type, Union
import warnings

import dask
import dask.array as da
from dask.blockwise import BlockwiseDep, blockwise
from dask.highlevelgraph import HighLevelGraph
import numpy as np
from rasterio import windows
from rasterio.enums import Resampling

from .raster_spec import Bbox, RasterSpec
from .rio_reader import AutoParallelRioReader, LayeredEnv
from .reader_protocol import Reader


def items_to_dask(
    asset_table: np.ndarray,
    spec: RasterSpec,
    chunksize: int,
    resampling: Resampling = Resampling.nearest,
    dtype: np.dtype = np.dtype("float64"),
    fill_value: Union[int, float] = np.nan,
    rescale: bool = True,
    reader: Type[Reader] = AutoParallelRioReader,
    gdal_env: Optional[LayeredEnv] = None,
    errors_as_nodata: Tuple[Exception, ...] = (),
) -> da.Array:
    errors_as_nodata = errors_as_nodata or ()  # be sure it's not None

    if not np.can_cast(fill_value, dtype):
        raise ValueError(
            f"The fill_value {fill_value} is incompatible with the output dtype {dtype}. "
            f"Either use `dtype={np.array(fill_value).dtype.name!r}`, or pick a different `fill_value`."
        )

    # The overall strategy in this function is to materialize the outer two dimensions (items, assets)
    # as one dask array, then the chunks of the inner two dimensions (y, x) as another dask array, then use
    # Blockwise to represent the cartesian product between them, to avoid materializing that entire graph.
    # Materializing the (items, assets) dimensions is unavoidable: every asset has a distinct URL, so that information
    # has to be included somehow.

    # make URLs into dask array with 1-element chunks (one chunk per asset)
    asset_table_dask = da.from_array(
        asset_table,
        chunks=1,
        inline_array=True,
        name="asset-table-" + dask.base.tokenize(asset_table),
    )

    # map a function over each chunk that opens that URL as a rasterio dataset
    with dask.annotate(fuse=False):
        # ^ HACK: prevent this layer from fusing to the next `fetch_raster_window` one.
        # This relies on the fact that blockwise fusion doesn't happen when the layers' annotations
        # don't match, which may not be behavior we can rely on.
        # (The actual content of the annotation is irrelevant here.)
        reader_table = asset_table_dask.map_blocks(
            asset_table_to_reader_and_window,
            spec,
            resampling,
            dtype,
            fill_value,
            rescale,
            gdal_env,
            errors_as_nodata,
            reader,
            dtype=object,
        )

    shape_yx = spec.shape
    chunks_yx = da.core.normalize_chunks(chunksize, shape_yx)
    chunks = reader_table.chunks + chunks_yx

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=da.core.PerformanceWarning)

        name = f"fetch_raster_window-{dask.base.tokenize(reader_table, chunks)}"
        # TODO use `da.blockwise` once it supports `BlockwiseDep`s as arguments
        lyr = blockwise(
            fetch_raster_window,
            name,
            "tbyx",
            reader_table.name,
            "tb",
            Slices(chunks_yx),
            "yx",
            numblocks={reader_table.name: reader_table.numblocks},  # ugh
        )
        dsk = HighLevelGraph.from_collections(name, lyr, [reader_table])
        rasters = da.Array(dsk, name, chunks, meta=np.ndarray((), dtype=dtype))

    return rasters


ReaderTableEntry = Union[tuple[Reader, windows.Window], np.ndarray]


def asset_table_to_reader_and_window(
    asset_table: np.ndarray,
    spec: RasterSpec,
    resampling: Resampling,
    dtype: np.dtype,
    fill_value: Union[int, float],
    rescale: bool,
    gdal_env: Optional[LayeredEnv],
    errors_as_nodata: Tuple[Exception, ...],
    reader: Type[Reader],
) -> np.ndarray:
    """
    "Open" an asset table by creating a `Reader` for each asset.

    This function converts the asset table (or chunks thereof) into an object array,
    where each element contains a tuple of the `Reader` and `Window` for that asset,
    or a one-element array of ``fill_value``, if the element has no URL.
    """
    reader_table = np.empty_like(asset_table, dtype=object)
    entry: ReaderTableEntry
    for index, asset_entry in np.ndenumerate(asset_table):
        url = asset_entry["url"]
        if url is None:
            entry = np.array(fill_value, dtype)
            # ^ signifies empty value; will be broadcast to output chunk size upon read
        else:
            asset_bounds: Bbox = asset_entry["bounds"]
            asset_window = windows.from_bounds(
                *asset_bounds,
                transform=spec.transform,
                precision=0.0
                # ^ `precision=0.0`: https://github.com/rasterio/rasterio/issues/2374
            )

            entry = (
                reader(
                    url=url,
                    spec=spec,
                    resampling=resampling,
                    dtype=dtype,
                    fill_value=fill_value,
                    rescale=rescale,
                    gdal_env=gdal_env,
                    errors_as_nodata=errors_as_nodata,
                ),
                asset_window,
            )
        reader_table[index] = entry
    return reader_table


def fetch_raster_window(
    reader_entry: np.ndarray,
    slices: Tuple[slice, slice],
) -> np.ndarray:
    assert len(slices) == 2, slices
    assert reader_entry.size == 1, reader_entry.size
    entry: ReaderTableEntry = reader_entry.item()
    # ^ TODO handle >1 asset, i.e. chunking of time/band dims
    current_window = windows.Window.from_slices(*slices)
    if isinstance(entry, tuple):
        reader, asset_window = entry
        # check that the window we're fetching overlaps with the asset
        if windows.intersect(current_window, asset_window):
            data = reader.read(current_window)

            return data[None, None]  # add empty outer time, band dims
        fill_arr = np.array(reader.fill_value, reader.dtype)
    else:
        fill_arr = entry

    # no dataset, or we didn't overlap it: return empty data.
    # use the broadcast trick for even fewer memz
    return np.broadcast_to(fill_arr, (1, 1) + windows.shape(current_window))


class Slices(BlockwiseDep):
    starts: list[tuple[int, ...]]
    produces_tasks: ClassVar[bool] = False

    def __init__(self, chunks: Tuple[Tuple[int, ...], ...]):
        self.starts = [tuple(itertools.accumulate(c, initial=0)) for c in chunks]

    def __getitem__(self, idx: Tuple[int, ...]) -> Tuple[slice, ...]:
        return tuple(
            slice(start[i], start[i + 1]) for i, start in zip(idx, self.starts)
        )

    @property
    def numblocks(self) -> list[int]:
        return [len(s) - 1 for s in self.starts]

    def __dask_distributed_pack__(
        self, required_indices: Optional[list[Tuple[int, ...]]] = None
    ) -> list[Tuple[int, ...]]:
        return self.starts

    @classmethod
    def __dask_distributed_unpack__(cls, state: list[Tuple[int, ...]]) -> Slices:
        self = cls.__new__(cls)
        self.starts = state
        return self
