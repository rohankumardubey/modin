# Licensed to Modin Development Team under one or more contributor license agreements.
# See the NOTICE file distributed with this work for additional information regarding
# copyright ownership.  The Modin Development Team licenses this file to you under the
# Apache License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

"""Module provides a partition manager class for ``HdkOnNativeDataframe`` frame."""

from modin.error_message import ErrorMessage
from modin.pandas.utils import is_scalar
import numpy as np

from modin.core.dataframe.pandas.partitioning.partition_manager import (
    PandasDataframePartitionManager,
)
from ..dataframe.utils import ColNameCodec
from ..partitioning.partition import HdkOnNativeDataframePartition
from ..db_worker import DbWorker
from ..calcite_builder import CalciteBuilder
from ..calcite_serializer import CalciteSerializer
from modin.config import DoUseCalcite

import pyarrow
import pandas
import re


class HdkOnNativeDataframePartitionManager(PandasDataframePartitionManager):
    """
    Frame manager for ``HdkOnNativeDataframe``.

    This class handles several features of ``HdkOnNativeDataframe``:
      - frame always has a single partition
      - frame cannot process some data types
      - frame has to use mangling for index labels
      - frame uses HDK storage format for execution
    """

    _partition_class = HdkOnNativeDataframePartition

    @classmethod
    def _compute_num_partitions(cls):
        """
        Return a number of partitions a frame should be split to.

        `HdkOnNativeDataframe` always has a single partition.

        Returns
        -------
        int
        """
        return 1

    @classmethod
    def from_pandas(cls, df, return_dims=False, encode_col_names=True):
        """
        Create ``HdkOnNativeDataframe`` from ``pandas.DataFrame``.

        Parameters
        ----------
        df : pandas.DataFrame
            Source frame.
        return_dims : bool, default: False
            Include resulting dimensions into the returned value.
        encode_col_names : bool, default: True
            Encode column names.

        Returns
        -------
        tuple
            Tuple holding array of partitions, list of columns with unsupported
            data and optionally partitions' dimensions.
        """
        at, unsupported_cols = cls._get_unsupported_cols(df)

        if len(unsupported_cols) > 0:
            # Putting pandas frame into partitions instead of arrow table, because we know
            # that all of operations with this frame will be default to pandas and don't want
            # unnecessaries conversion pandas->arrow->pandas

            def tuple_wrapper(obj):
                """
                Wrap non-tuple object into a tuple.

                Parameters
                ----------
                obj : Any.
                    Wrapped object.

                Returns
                -------
                tuple
                """
                if not isinstance(obj, tuple):
                    obj = (obj,)
                return obj

            return (
                *tuple_wrapper(super().from_pandas(df, return_dims)),
                unsupported_cols,
            )
        else:
            # Since we already have arrow table, putting it into partitions instead
            # of pandas frame, to skip that phase when we will be putting our frame to HDK
            return cls.from_arrow(at, return_dims, unsupported_cols, encode_col_names)

    @classmethod
    def from_arrow(
        cls, at, return_dims=False, unsupported_cols=None, encode_col_names=True
    ):
        """
        Build frame from Arrow table.

        Parameters
        ----------
        at : pyarrow.Table
            Input table.
        return_dims : bool, default: False
            True to include dimensions into returned tuple.
        unsupported_cols : list of str, optional
            List of columns holding unsupported data. If None then
            check all columns to compute the list.
        encode_col_names : bool, default: True
            Encode column names.

        Returns
        -------
        tuple
            Tuple holding array of partitions, list of columns with unsupported
            data and optionally partitions' dimensions.
        """
        if encode_col_names:
            encoded_names = [ColNameCodec.encode(n) for n in at.column_names]
            encoded_at = at
            if encoded_names != at.column_names:
                encoded_at = at.rename_columns(encoded_names)
        else:
            encoded_at = at

        put_func = cls._partition_class.put_arrow
        parts = [[put_func(encoded_at)]]
        if unsupported_cols is None:
            _, unsupported_cols = cls._get_unsupported_cols(at)

        if not return_dims:
            return np.array(parts), unsupported_cols
        else:
            row_lengths = [at.num_rows]
            col_widths = [at.num_columns]
            return np.array(parts), row_lengths, col_widths, unsupported_cols

    @classmethod
    def _get_unsupported_cols(cls, obj):
        """
        Return a list of columns with unsupported by HDK data types.

        Parameters
        ----------
        obj : pandas.DataFrame or pyarrow.Table
            Object to inspect on unsupported column types.

        Returns
        -------
        tuple
            Arrow representation of `obj` (for future using) and a list of
            unsupported columns.
        """
        if isinstance(obj, (pandas.Series, pandas.DataFrame)):
            # picking first rows from cols with `dtype="object"` to check its actual type,
            # in case of homogen columns that saves us unnecessary convertion to arrow table

            if obj.empty:
                unsupported_cols = []
            elif isinstance(obj.columns, pandas.MultiIndex):
                unsupported_cols = [str(c) for c in obj.columns]
            else:
                cols = [name for name, col in obj.dtypes.items() if col == "object"]
                type_samples = obj.iloc[0][cols]
                unsupported_cols = [
                    name
                    for name, col in type_samples.items()
                    if not isinstance(col, str)
                    and not (is_scalar(col) and pandas.isna(col))
                ]

            if len(unsupported_cols) > 0:
                return None, unsupported_cols

            try:
                at = pyarrow.Table.from_pandas(obj, preserve_index=False)
            except (
                pyarrow.lib.ArrowTypeError,
                pyarrow.lib.ArrowInvalid,
                ValueError,
                TypeError,
            ) as err:
                # The TypeError could be raised when converting a sparse data to
                # arrow table - https://github.com/apache/arrow/pull/4497. If this
                # is the case - fall back to pandas, otherwise - rethrow the error.
                if type(err) == TypeError:
                    if any([isinstance(t, pandas.SparseDtype) for t in obj.dtypes]):
                        ErrorMessage.single_warning(
                            "Sparse data is not currently supported!"
                        )
                    else:
                        raise err

                # The ValueError is raised by pyarrow in case of duplicate columns.
                # We catch and handle this error here. If there are no duplicates
                # (is_unique is True), then the error is caused by something different
                # and we just rethrow it.
                if (type(err) == ValueError) and obj.columns.is_unique:
                    raise err

                regex = r"Conversion failed for column ([^\W]*)"
                unsupported_cols = []
                for msg in err.args:
                    match = re.findall(regex, msg)
                    unsupported_cols.extend(match)

                if len(unsupported_cols) == 0:
                    unsupported_cols = obj.columns
                return None, unsupported_cols
            else:
                obj = at

        def is_supported_dtype(dtype):
            """Check whether the passed pyarrow `dtype` is supported by HDK."""
            if (
                pyarrow.types.is_string(dtype)
                or pyarrow.types.is_time(dtype)
                or pyarrow.types.is_dictionary(dtype)
                or pyarrow.types.is_null(dtype)
            ):
                return True
            try:
                pandas_dtype = dtype.to_pandas_dtype()
                return pandas_dtype != np.dtype("O")
            except NotImplementedError:
                return False

        return (
            obj,
            [field.name for field in obj.schema if not is_supported_dtype(field.type)],
        )

    @classmethod
    def run_exec_plan(cls, plan, columns):
        """
        Run execution plan in HDK storage format to materialize frame.

        Parameters
        ----------
        plan : DFAlgNode
            A root of an execution plan tree.
        columns : list of str
            A frame column names.

        Returns
        -------
        np.array
            Created frame's partitions.
        """
        omniSession = DbWorker()

        # First step is to make sure all partitions are in HDK.
        frames = plan.collect_frames()
        for frame in frames:
            if frame._partitions.size != 1:
                raise NotImplementedError(
                    "HdkOnNative engine doesn't suport partitioned frames"
                )
            for p in frame._partitions.flatten():
                if p.frame_id is None:
                    obj = p.get()
                    if isinstance(obj, (pandas.DataFrame, pandas.Series)):
                        p.frame_id = omniSession.import_pandas_dataframe(obj)
                    else:
                        assert isinstance(obj, pyarrow.Table)
                        if obj.num_columns == 0:
                            # Tables without columns are not supported.
                            # Creating an empty table with index columns only.
                            idx_names = (
                                frame.index.names
                                if frame.has_materialized_index
                                else [None]
                            )
                            idx_names = frame._mangle_index_names(idx_names)
                            obj = pyarrow.table(
                                {n: [] for n in idx_names},
                                schema=pyarrow.schema(
                                    {n: pyarrow.int64() for n in idx_names}
                                ),
                            )
                        p.frame_id = omniSession.import_arrow_table(obj)

        calcite_plan = CalciteBuilder().build(plan)
        calcite_json = CalciteSerializer().serialize(calcite_plan)

        cmd_prefix = "execute relalg "

        if DoUseCalcite.get():
            cmd_prefix = "execute calcite "

        at = omniSession.executeRA(cmd_prefix + calcite_json)

        res = np.empty((1, 1), dtype=np.dtype(object))
        # workaround for https://github.com/modin-project/modin/issues/1851
        if DoUseCalcite.get():
            at = at.rename_columns([ColNameCodec.encode(c) for c in columns])
        res[0][0] = cls._partition_class.put_arrow(at)

        return res

    @classmethod
    def _names_from_index_cols(cls, cols):
        """
        Get index labels.

        Deprecated.

        Parameters
        ----------
        cols : list of str
            Index columns.

        Returns
        -------
        list of str
        """
        if len(cols) == 1:
            return cls._name_from_index_col(cols[0])
        return [cls._name_from_index_col(n) for n in cols]

    @classmethod
    def _name_from_index_col(cls, col):
        """
        Get index label.

        Deprecated.

        Parameters
        ----------
        col : str
            Index column.

        Returns
        -------
        str
        """
        if col.startswith(ColNameCodec.IDX_COL_NAME):
            return None
        return col

    @classmethod
    def _maybe_scalar(cls, lst):
        """
        Transform list with a single element to scalar.

        Deprecated.

        Parameters
        ----------
        lst : list
            Input list.

        Returns
        -------
        Any
        """
        if len(lst) == 1:
            return lst[0]
        return lst
