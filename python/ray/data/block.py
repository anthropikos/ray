import collections
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    Union,
)

import numpy as np

import ray
from ray import DynamicObjectRefGenerator
from ray.air.util.tensor_extensions.arrow import ArrowConversionError
from ray.data._internal.util import _check_pyarrow_version, _truncated_repr
from ray.types import ObjectRef
from ray.util import log_once
from ray.util.annotations import DeveloperAPI

import psutil

try:
    import resource
except ImportError:
    resource = None

if TYPE_CHECKING:
    import pandas
    import pyarrow

    from ray.data._internal.block_builder import BlockBuilder
    from ray.data._internal.planner.exchange.sort_task_spec import SortKey
    from ray.data.aggregate import AggregateFn


T = TypeVar("T", contravariant=True)
U = TypeVar("U", covariant=True)

KeyType = TypeVar("KeyType")
AggType = TypeVar("AggType")


# Represents a batch of records to be stored in the Ray object store.
#
# Block data can be accessed in a uniform way via ``BlockAccessors`` like`
# ``ArrowBlockAccessor``.
Block = Union["pyarrow.Table", "pandas.DataFrame"]


logger = logging.getLogger(__name__)


@DeveloperAPI
class BlockType(Enum):
    ARROW = "arrow"
    PANDAS = "pandas"


# User-facing data batch type. This is the data type for data that is supplied to and
# returned from batch UDFs.
DataBatch = Union["pyarrow.Table", "pandas.DataFrame", Dict[str, np.ndarray]]

# User-facing data column type. This is the data type for data that is supplied to and
# returned from column UDFs.
DataBatchColumn = Union[
    "pyarrow.ChunkedArray", "pyarrow.Array", "pandas.Series", np.ndarray
]


# A class type that implements __call__.
CallableClass = type


class _CallableClassProtocol(Protocol[T, U]):
    def __call__(self, __arg: T) -> Union[U, Iterator[U]]:
        ...


# A user defined function passed to map, map_batches, ec.
UserDefinedFunction = Union[
    Callable[[T], U],
    Callable[[T], Iterator[U]],
    "_CallableClassProtocol",
]

# A list of block references pending computation by a single task. For example,
# this may be the output of a task reading a file.
BlockPartition = List[Tuple[ObjectRef[Block], "BlockMetadata"]]

# The metadata that describes the output of a BlockPartition. This has the
# same type as the metadata that describes each block in the partition.
BlockPartitionMetadata = List["BlockMetadata"]

# TODO(ekl/chengsu): replace this with just
# `DynamicObjectRefGenerator` once block splitting
# is on by default. When block splitting is off, the type is a plain block.
MaybeBlockPartition = Union[Block, DynamicObjectRefGenerator]

VALID_BATCH_FORMATS = ["pandas", "pyarrow", "numpy", None]
DEFAULT_BATCH_FORMAT = "numpy"


def _apply_batch_format(given_batch_format: Optional[str]) -> str:
    if given_batch_format == "default":
        given_batch_format = DEFAULT_BATCH_FORMAT
    if given_batch_format not in VALID_BATCH_FORMATS:
        raise ValueError(
            f"The given batch format {given_batch_format} isn't allowed (must be one of"
            f" {VALID_BATCH_FORMATS})."
        )
    return given_batch_format


def _apply_batch_size(
    given_batch_size: Optional[Union[int, Literal["default"]]]
) -> Optional[int]:
    if given_batch_size == "default":
        return ray.data.context.DEFAULT_BATCH_SIZE
    else:
        return given_batch_size


@DeveloperAPI
class BlockExecStats:
    """Execution stats for this block.

    Attributes:
        wall_time_s: The wall-clock time it took to compute this block.
        cpu_time_s: The CPU time it took to compute this block.
        node_id: A unique id for the node that computed this block.
    """

    def __init__(self):
        self.start_time_s: Optional[float] = None
        self.end_time_s: Optional[float] = None
        self.wall_time_s: Optional[float] = None
        self.udf_time_s: Optional[float] = 0
        self.cpu_time_s: Optional[float] = None
        self.node_id = ray.runtime_context.get_runtime_context().get_node_id()
        # Max memory usage. May be an overestimate since we do not
        # differentiate from previous tasks on the same worker.
        self.max_rss_bytes: int = 0
        self.task_idx: Optional[int] = None

    @staticmethod
    def builder() -> "_BlockExecStatsBuilder":
        return _BlockExecStatsBuilder()

    def __repr__(self):
        return repr(
            {
                "wall_time_s": self.wall_time_s,
                "cpu_time_s": self.cpu_time_s,
                "udf_time_s": self.udf_time_s,
                "node_id": self.node_id,
            }
        )


class _BlockExecStatsBuilder:
    """Helper class for building block stats.

    When this class is created, we record the start time. When build() is
    called, the time delta is saved as part of the stats.
    """

    def __init__(self):
        self.start_time = time.perf_counter()
        self.start_cpu = time.process_time()

    def build(self) -> "BlockExecStats":
        self.end_time = time.perf_counter()
        self.end_cpu = time.process_time()

        stats = BlockExecStats()
        stats.start_time_s = self.start_time
        stats.end_time_s = self.end_time
        stats.wall_time_s = self.end_time - self.start_time
        stats.cpu_time_s = self.end_cpu - self.start_cpu
        if resource is None:
            # NOTE(swang): resource package is not supported on Windows. This
            # is only the memory usage at the end of the task, not the peak
            # memory.
            process = psutil.Process(os.getpid())
            stats.max_rss_bytes = int(process.memory_info().rss)
        else:
            stats.max_rss_bytes = int(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1e3
            )
        return stats


@DeveloperAPI
@dataclass
class BlockMetadata:
    """Metadata about the block."""

    #: The number of rows contained in this block, or None.
    num_rows: Optional[int]
    #: The approximate size in bytes of this block, or None.
    size_bytes: Optional[int]
    #: The pyarrow schema or types of the block elements, or None.
    schema: Optional[Union[type, "pyarrow.lib.Schema"]]
    #: The list of file paths used to generate this block, or
    #: the empty list if indeterminate.
    input_files: Optional[List[str]]
    #: Execution stats for this block.
    exec_stats: Optional[BlockExecStats]

    def __post_init__(self):
        if self.input_files is None:
            self.input_files = []
        if self.size_bytes is not None:
            # Require size_bytes to be int, ray.util.metrics objects
            # will not take other types like numpy.int64
            assert isinstance(self.size_bytes, int)


@DeveloperAPI
class BlockAccessor:
    """Provides accessor methods for a specific block.

    Ideally, we wouldn't need a separate accessor classes for blocks. However,
    this is needed if we want to support storing ``pyarrow.Table`` directly
    as a top-level Ray object, without a wrapping class (issue #17186).
    """

    def num_rows(self) -> int:
        """Return the number of rows contained in this block."""
        raise NotImplementedError

    def iter_rows(self, public_row_format: bool) -> Iterator[T]:
        """Iterate over the rows of this block.

        Args:
            public_row_format: Whether to cast rows into the public Dict row
                format (this incurs extra copy conversions).
        """
        raise NotImplementedError

    def slice(self, start: int, end: int, copy: bool) -> Block:
        """Return a slice of this block.

        Args:
            start: The starting index of the slice.
            end: The ending index of the slice.
            copy: Whether to perform a data copy for the slice.

        Returns:
            The sliced block result.
        """
        raise NotImplementedError

    def take(self, indices: List[int]) -> Block:
        """Return a new block containing the provided row indices.

        Args:
            indices: The row indices to return.

        Returns:
            A new block containing the provided row indices.
        """
        raise NotImplementedError

    def select(self, columns: List[Optional[str]]) -> Block:
        """Return a new block containing the provided columns."""
        raise NotImplementedError

    def random_shuffle(self, random_seed: Optional[int]) -> Block:
        """Randomly shuffle this block."""
        raise NotImplementedError

    def to_pandas(self) -> "pandas.DataFrame":
        """Convert this block into a Pandas dataframe."""
        raise NotImplementedError

    def to_numpy(
        self, columns: Optional[Union[str, List[str]]] = None
    ) -> Union[np.ndarray, Dict[str, np.ndarray]]:
        """Convert this block (or columns of block) into a NumPy ndarray.

        Args:
            columns: Name of columns to convert, or None if converting all columns.
        """
        raise NotImplementedError

    def to_arrow(self) -> "pyarrow.Table":
        """Convert this block into an Arrow table."""
        raise NotImplementedError

    def to_block(self) -> Block:
        """Return the base block that this accessor wraps."""
        raise NotImplementedError

    def to_default(self) -> Block:
        """Return the default data format for this accessor."""
        return self.to_block()

    def to_batch_format(self, batch_format: Optional[str]) -> DataBatch:
        """Convert this block into the provided batch format.

        Args:
            batch_format: The batch format to convert this block to.

        Returns:
            This block formatted as the provided batch format.
        """
        if batch_format is None:
            return self.to_block()
        elif batch_format == "default" or batch_format == "native":
            return self.to_default()
        elif batch_format == "pandas":
            return self.to_pandas()
        elif batch_format == "pyarrow":
            return self.to_arrow()
        elif batch_format == "numpy":
            return self.to_numpy()
        else:
            raise ValueError(
                f"The batch format must be one of {VALID_BATCH_FORMATS}, got: "
                f"{batch_format}"
            )

    def size_bytes(self) -> int:
        """Return the approximate size in bytes of this block."""
        raise NotImplementedError

    def schema(self) -> Union[type, "pyarrow.lib.Schema"]:
        """Return the Python type or pyarrow schema of this block."""
        raise NotImplementedError

    def get_metadata(
        self,
        input_files: Optional[List[str]] = None,
        exec_stats: Optional[BlockExecStats] = None,
    ) -> BlockMetadata:
        """Create a metadata object from this block."""
        return BlockMetadata(
            num_rows=self.num_rows(),
            size_bytes=self.size_bytes(),
            schema=self.schema(),
            input_files=input_files,
            exec_stats=exec_stats,
        )

    def zip(self, other: "Block") -> "Block":
        """Zip this block with another block of the same type and size."""
        raise NotImplementedError

    @staticmethod
    def builder() -> "BlockBuilder":
        """Create a builder for this block type."""
        raise NotImplementedError

    @classmethod
    def batch_to_block(
        cls,
        batch: DataBatch,
        block_type: Optional[BlockType] = None,
    ) -> Block:
        """Create a block from user-facing data formats."""

        if isinstance(batch, np.ndarray):
            raise ValueError(
                f"Error validating {_truncated_repr(batch)}: "
                "Standalone numpy arrays are not "
                "allowed in Ray 2.5. Return a dict of field -> array, "
                "e.g., `{'data': array}` instead of `array`."
            )

        elif isinstance(batch, collections.abc.Mapping):
            if block_type is None or block_type == BlockType.ARROW:
                try:
                    return cls.batch_to_arrow_block(batch)
                except ArrowConversionError as e:
                    if log_once("_fallback_to_pandas_block_warning"):
                        logger.warning(
                            f"Failed to convert batch to Arrow due to: {e}; "
                            f"falling back to Pandas block"
                        )

                    if block_type is None:
                        return cls.batch_to_pandas_block(batch)
                    else:
                        raise e
            else:
                assert block_type == BlockType.PANDAS
                return cls.batch_to_pandas_block(batch)
        return batch

    @classmethod
    def batch_to_arrow_block(cls, batch: Dict[str, Any]) -> Block:
        """Create an Arrow block from user-facing data formats."""
        from ray.data._internal.arrow_block import ArrowBlockBuilder

        return ArrowBlockBuilder._table_from_pydict(batch)

    @classmethod
    def batch_to_pandas_block(cls, batch: Dict[str, Any]) -> Block:
        """Create a Pandas block from user-facing data formats."""
        from ray.data._internal.pandas_block import PandasBlockAccessor

        return PandasBlockAccessor.numpy_to_block(batch)

    @staticmethod
    def for_block(block: Block) -> "BlockAccessor[T]":
        """Create a block accessor for the given block."""
        _check_pyarrow_version()
        import pandas
        import pyarrow

        if isinstance(block, pyarrow.Table):
            from ray.data._internal.arrow_block import ArrowBlockAccessor

            return ArrowBlockAccessor(block)
        elif isinstance(block, pandas.DataFrame):
            from ray.data._internal.pandas_block import PandasBlockAccessor

            return PandasBlockAccessor(block)
        elif isinstance(block, bytes):
            from ray.data._internal.arrow_block import ArrowBlockAccessor

            return ArrowBlockAccessor.from_bytes(block)
        elif isinstance(block, list):
            raise ValueError(
                f"Error validating {_truncated_repr(block)}: "
                "Standalone Python objects are not "
                "allowed in Ray 2.5. To use Python objects in a dataset, "
                "wrap them in a dict of numpy arrays, e.g., "
                "return `{'item': batch}` instead of just `batch`."
            )
        else:
            raise TypeError("Not a block type: {} ({})".format(block, type(block)))

    def sample(self, n_samples: int, sort_key: "SortKey") -> "Block":
        """Return a random sample of items from this block."""
        raise NotImplementedError

    def sort_and_partition(
        self, boundaries: List[T], sort_key: "SortKey"
    ) -> List["Block"]:
        """Return a list of sorted partitions of this block."""
        raise NotImplementedError

    def combine(self, key: "SortKey", aggs: Tuple["AggregateFn"]) -> Block:
        """Combine rows with the same key into an accumulator."""
        raise NotImplementedError

    @staticmethod
    def merge_sorted_blocks(
        blocks: List["Block"], sort_key: "SortKey"
    ) -> Tuple[Block, BlockMetadata]:
        """Return a sorted block by merging a list of sorted blocks."""
        raise NotImplementedError

    @staticmethod
    def aggregate_combined_blocks(
        blocks: List[Block], sort_key: "SortKey", aggs: Tuple["AggregateFn"]
    ) -> Tuple[Block, BlockMetadata]:
        """Aggregate partially combined and sorted blocks."""
        raise NotImplementedError

    def block_type(self) -> BlockType:
        """Return the block type of this block."""
        raise NotImplementedError
