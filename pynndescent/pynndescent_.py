# Author: Leland McInnes <leland.mcinnes@gmail.com>
#
# License: BSD 2 clause

from warnings import warn

import numba
import numpy as np
from sklearn.utils import check_random_state, check_array
from sklearn.preprocessing import normalize
from sklearn.base import BaseEstimator, TransformerMixin
from scipy.sparse import csr_matrix, coo_matrix, isspmatrix_csr, vstack as sparse_vstack

import heapq

import pynndescent.sparse as sparse
import pynndescent.sparse_nndescent as sparse_nnd
import pynndescent.distances as pynnd_dist

from pynndescent.utils import (
    tau_rand_int,
    tau_rand,
    make_heap,
    deheap_sort,
    new_build_candidates,
    ts,
    simple_heap_push,
    flagged_heap_push,
    checked_flagged_heap_push,
    has_been_visited,
    mark_visited,
    apply_graph_updates_high_memory,
    apply_graph_updates_low_memory,
    initalize_heap_from_graph_indices,
    sparse_initalize_heap_from_graph_indices,
)

from pynndescent.rp_trees import (
    make_forest,
    rptree_leaf_array,
    convert_tree_format,
    FlatTree,
    denumbaify_tree,
    renumbaify_tree,
    select_side,
    sparse_select_side,
    score_linked_tree,
)

update_type = numba.types.List(
    numba.types.List((numba.types.int64, numba.types.int64, numba.types.float64))
)

INT32_MIN = np.iinfo(np.int32).min + 1
INT32_MAX = np.iinfo(np.int32).max - 1

FLOAT32_EPS = np.finfo(np.float32).eps

EMPTY_GRAPH = make_heap(1, 1)


@numba.njit(parallel=True, cache=True)
def generate_leaf_updates(leaf_block, dist_thresholds, data, dist):

    updates = [[(-1, -1, np.inf)] for i in range(leaf_block.shape[0])]

    for n in numba.prange(leaf_block.shape[0]):
        for i in range(leaf_block.shape[1]):
            p = leaf_block[n, i]
            if p < 0:
                break

            for j in range(i + 1, leaf_block.shape[1]):
                q = leaf_block[n, j]
                if q < 0:
                    break

                d = dist(data[p], data[q])
                if d < dist_thresholds[p] or d < dist_thresholds[q]:
                    updates[n].append((p, q, d))

    return updates


@numba.njit(locals={"d": numba.float32, "p": numba.int32, "q": numba.int32}, cache=True)
def init_rp_tree(data, dist, current_graph, leaf_array):

    n_leaves = leaf_array.shape[0]
    block_size = 65536
    n_blocks = n_leaves // block_size

    for i in range(n_blocks + 1):
        block_start = i * block_size
        block_end = min(n_leaves, (i + 1) * block_size)

        leaf_block = leaf_array[block_start:block_end]
        dist_thresholds = current_graph[1][:, 0]

        updates = generate_leaf_updates(leaf_block, dist_thresholds, data, dist)

        for j in range(len(updates)):
            for k in range(len(updates[j])):
                p, q, d = updates[j][k]

                if p == -1 or q == -1:
                    continue

                # heap_push(current_graph, p, d, q, 1)
                # heap_push(current_graph, q, d, p, 1)
                checked_flagged_heap_push(
                    current_graph[1][p],
                    current_graph[0][p],
                    current_graph[2][p],
                    d,
                    q,
                    np.uint8(1),
                )
                checked_flagged_heap_push(
                    current_graph[1][q],
                    current_graph[0][q],
                    current_graph[2][q],
                    d,
                    p,
                    np.uint8(1),
                )


@numba.njit(
    fastmath=True,
    locals={"d": numba.float32, "idx": numba.int32, "i": numba.int32},
    cache=True,
)
def init_random(n_neighbors, data, heap, dist, rng_state):
    for i in range(data.shape[0]):
        if heap[0][i, 0] < 0.0:
            for j in range(n_neighbors - np.sum(heap[0][i] >= 0.0)):
                idx = np.abs(tau_rand_int(rng_state)) % data.shape[0]
                d = dist(data[idx], data[i])
                # heap_push(heap, i, d, idx, 1)
                checked_flagged_heap_push(
                    heap[1][i], heap[0][i], heap[2][i], d, idx, np.uint8(1)
                )

    return


@numba.njit(cache=True)
def init_from_neighbor_graph(heap, indices, distances):
    for p in range(indices.shape[0]):
        for k in range(indices.shape[1]):
            q = indices[p, k]
            d = distances[p, k]
            # unchecked_heap_push(heap, p, d, q, 0)
            flagged_heap_push(heap[0][p], heap[1][p], heap[2][p], q, d, 0)

    return


@numba.njit(parallel=True, cache=True)
def generate_graph_updates(
    new_candidate_block, old_candidate_block, dist_thresholds, data, dist
):

    block_size = new_candidate_block.shape[0]
    updates = [[(-1, -1, np.inf)] for i in range(block_size)]
    max_candidates = new_candidate_block.shape[1]

    for i in numba.prange(block_size):
        for j in range(max_candidates):
            p = int(new_candidate_block[i, j])
            if p < 0:
                continue

            for k in range(j, max_candidates):
                q = int(new_candidate_block[i, k])
                if q < 0:
                    continue

                d = dist(data[p], data[q])
                if d <= dist_thresholds[p] or d <= dist_thresholds[q]:
                    updates[i].append((p, q, d))

            for k in range(max_candidates):
                q = int(old_candidate_block[i, k])
                if q < 0:
                    continue

                d = dist(data[p], data[q])
                if d <= dist_thresholds[p] or d <= dist_thresholds[q]:
                    updates[i].append((p, q, d))

    return updates


@numba.njit(cache=True)
def process_candidates(
    data,
    dist,
    current_graph,
    new_candidate_neighbors,
    old_candidate_neighbors,
    n_blocks,
    block_size,
    n_threads,
):
    c = 0
    n_vertices = new_candidate_neighbors.shape[0]
    for i in range(n_blocks + 1):
        block_start = i * block_size
        block_end = min(n_vertices, (i + 1) * block_size)

        new_candidate_block = new_candidate_neighbors[block_start:block_end]
        old_candidate_block = old_candidate_neighbors[block_start:block_end]

        dist_thresholds = current_graph[1][:, 0]

        updates = generate_graph_updates(
            new_candidate_block, old_candidate_block, dist_thresholds, data, dist
        )

        c += apply_graph_updates_low_memory(current_graph, updates, n_threads)

    return c


@numba.njit()
def nn_descent_internal_low_memory_parallel(
    current_graph,
    data,
    n_neighbors,
    rng_state,
    max_candidates=50,
    dist=pynnd_dist.euclidean,
    n_iters=10,
    delta=0.001,
    verbose=False,
):
    n_vertices = data.shape[0]
    block_size = 16384
    n_blocks = n_vertices // block_size
    n_threads = numba.get_num_threads()

    for n in range(n_iters):
        if verbose:
            print("\t", n + 1, " / ", n_iters)

        (new_candidate_neighbors, old_candidate_neighbors) = new_build_candidates(
            current_graph, max_candidates, rng_state, n_threads
        )

        c = process_candidates(
            data,
            dist,
            current_graph,
            new_candidate_neighbors,
            old_candidate_neighbors,
            n_blocks,
            block_size,
            n_threads,
        )

        if c <= delta * n_neighbors * data.shape[0]:
            if verbose:
                print("\tStopping threshold met -- exiting after", n + 1, "iterations")
            return


@numba.njit()
def nn_descent_internal_high_memory_parallel(
    current_graph,
    data,
    n_neighbors,
    rng_state,
    max_candidates=50,
    dist=pynnd_dist.euclidean,
    n_iters=10,
    delta=0.001,
    verbose=False,
):
    n_vertices = data.shape[0]
    block_size = 16384
    n_blocks = n_vertices // block_size
    n_threads = numba.get_num_threads()

    in_graph = [
        set(current_graph[0][i].astype(np.int64))
        for i in range(current_graph[0].shape[0])
    ]

    for n in range(n_iters):
        if verbose:
            print("\t", n + 1, " / ", n_iters)

        (new_candidate_neighbors, old_candidate_neighbors) = new_build_candidates(
            current_graph, max_candidates, rng_state, n_threads
        )

        c = 0
        for i in range(n_blocks + 1):
            block_start = i * block_size
            block_end = min(n_vertices, (i + 1) * block_size)

            new_candidate_block = new_candidate_neighbors[block_start:block_end]
            old_candidate_block = old_candidate_neighbors[block_start:block_end]
            dist_thresholds = current_graph[1][:, 0]

            updates = generate_graph_updates(
                new_candidate_block, old_candidate_block, dist_thresholds, data, dist
            )

            c += apply_graph_updates_high_memory(current_graph, updates, in_graph)

        if c <= delta * n_neighbors * data.shape[0]:
            if verbose:
                print("\tStopping threshold met -- exiting after", n + 1, "iterations")
            return


@numba.njit()
def nn_descent(
    data,
    n_neighbors,
    rng_state,
    max_candidates=50,
    dist=pynnd_dist.euclidean,
    n_iters=10,
    delta=0.001,
    init_graph=EMPTY_GRAPH,
    rp_tree_init=True,
    leaf_array=None,
    low_memory=True,
    verbose=False,
):

    if init_graph[0].shape[0] == 1:  # EMPTY_GRAPH
        current_graph = make_heap(data.shape[0], n_neighbors)

        if rp_tree_init:
            init_rp_tree(data, dist, current_graph, leaf_array)

        init_random(n_neighbors, data, current_graph, dist, rng_state)
    elif (
        init_graph[0].shape[0] == data.shape[0]
        and init_graph[0].shape[1] == n_neighbors
    ):
        current_graph = init_graph
    else:
        raise ValueError("Invalid initial graph specified!")

    if low_memory:
        nn_descent_internal_low_memory_parallel(
            current_graph,
            data,
            n_neighbors,
            rng_state,
            max_candidates=max_candidates,
            dist=dist,
            n_iters=n_iters,
            delta=delta,
            verbose=verbose,
        )
    else:
        nn_descent_internal_high_memory_parallel(
            current_graph,
            data,
            n_neighbors,
            rng_state,
            max_candidates=max_candidates,
            dist=dist,
            n_iters=n_iters,
            delta=delta,
            verbose=verbose,
        )

    return deheap_sort(current_graph)


@numba.njit(parallel=True)
def diversify(indices, distances, data, dist, rng_state, prune_probability=1.0):

    for i in numba.prange(indices.shape[0]):

        new_indices = [indices[i, 0]]
        new_distances = [distances[i, 0]]
        for j in range(1, indices.shape[1]):
            if indices[i, j] < 0:
                break

            flag = True
            for k in range(len(new_indices)):

                c = new_indices[k]

                d = dist(data[indices[i, j]], data[c])
                if new_distances[k] > FLOAT32_EPS and d < distances[i, j]:
                    if tau_rand(rng_state) < prune_probability:
                        flag = False
                        break

            if flag:
                new_indices.append(indices[i, j])
                new_distances.append(distances[i, j])

        for j in range(indices.shape[1]):
            if j < len(new_indices):
                indices[i, j] = new_indices[j]
                distances[i, j] = new_distances[j]
            else:
                indices[i, j] = -1
                distances[i, j] = np.inf

    return indices, distances


@numba.njit(parallel=True)
def diversify_csr(
    graph_indptr,
    graph_indices,
    graph_data,
    source_data,
    dist,
    rng_state,
    prune_probability=1.0,
):
    n_nodes = graph_indptr.shape[0] - 1

    for i in numba.prange(n_nodes):

        current_indices = graph_indices[graph_indptr[i] : graph_indptr[i + 1]]
        current_data = graph_data[graph_indptr[i] : graph_indptr[i + 1]]

        order = np.argsort(current_data)
        retained = np.ones(order.shape[0], dtype=np.int8)

        for idx in range(1, order.shape[0]):
            j = order[idx]
            for k in range(idx):
                l = order[k]
                if retained[l] == 1:

                    d = dist(
                        source_data[current_indices[j]], source_data[current_indices[k]]
                    )
                    if current_data[l] > FLOAT32_EPS and d < current_data[j]:
                        if tau_rand(rng_state) < prune_probability:
                            retained[j] = 0
                            break

        for idx in range(order.shape[0]):
            j = order[idx]
            if retained[j] == 0:
                graph_data[graph_indptr[i] + j] = 0

    return


@numba.njit(parallel=True)
def degree_prune_internal(indptr, data, max_degree=20):
    for i in numba.prange(indptr.shape[0] - 1):
        row_data = data[indptr[i] : indptr[i + 1]]
        if row_data.shape[0] > max_degree:
            cut_value = np.sort(row_data)[max_degree]
            for j in range(indptr[i], indptr[i + 1]):
                if data[j] > cut_value:
                    data[j] = 0.0

    return


def degree_prune(graph, max_degree=20):
    """Prune the k-neighbors graph back so that nodes have a maximum
    degree of ``max_degree``.

    Parameters
    ----------
    graph: sparse matrix
        The adjacency matrix of the graph

    max_degree: int (optional, default 20)
        The maximum degree of any node in the pruned graph

    Returns
    -------
    result: sparse matrix
        The pruned graph.
    """
    degree_prune_internal(graph.indptr, graph.data, max_degree)
    graph.eliminate_zeros()
    return graph


def resort_tree_indices(tree, tree_order):
    """Given a new data indexing, resort the tree indices to match"""
    new_tree = FlatTree(
        tree.hyperplanes,
        tree.offsets,
        tree.children,
        tree.indices[tree_order].astype(np.int32, order="C"),
        tree.leaf_size,
    )
    return new_tree


class NNDescent(object):
    """NNDescent for fast approximate nearest neighbor queries. NNDescent is
    very flexible and supports a wide variety of distances, including
    non-metric distances. NNDescent also scales well against high dimensional
    graph_data in many cases. This implementation provides a straightfoward
    interface, with access to some tuning parameters.

    Parameters
    ----------
    data: array os shape (n_samples, n_features)
        The training graph_data set to find nearest neighbors in.

    metric: string or callable (optional, default='euclidean')
        The metric to use for computing nearest neighbors. If a callable is
        used it must be a numba njit compiled function. Supported metrics
        include:
            * euclidean
            * manhattan
            * chebyshev
            * minkowski
            * canberra
            * braycurtis
            * mahalanobis
            * wminkowski
            * seuclidean
            * cosine
            * correlation
            * haversine
            * hamming
            * jaccard
            * dice
            * russelrao
            * kulsinski
            * rogerstanimoto
            * sokalmichener
            * sokalsneath
            * yule
            * hellinger
        Metrics that take arguments (such as minkowski, mahalanobis etc.)
        can have arguments passed via the metric_kwds dictionary. At this
        time care must be taken and dictionary elements must be ordered
        appropriately; this will hopefully be fixed in the future.

    metric_kwds: dict (optional, default {})
        Arguments to pass on to the metric, such as the ``p`` value for
        Minkowski distance.

    n_neighbors: int (optional, default=30)
        The number of neighbors to use in k-neighbor graph graph_data structure
        used for fast approximate nearest neighbor search. Larger values
        will result in more accurate search results at the cost of
        computation time.

    n_trees: int (optional, default=None)
        This implementation uses random projection forests for initializing the index
        build process. This parameter controls the number of trees in that forest. A
        larger number will result in more accurate neighbor computation at the cost
        of performance. The default of None means a value will be chosen based on the
        size of the graph_data.

    leaf_size: int (optional, default=None)
        The maximum number of points in a leaf for the random projection trees.
        The default of None means a value will be chosen based on n_neighbors.

    pruning_degree_multiplier: float (optional, default=1.5)
        How aggressively to prune the graph. Since the search graph is undirected
        (and thus includes nearest neighbors and reverse nearest neighbors) vertices
        can have very high degree -- the graph will be pruned such that no
        vertex has degree greater than
        ``pruning_degree_multiplier * n_neighbors``.

    diversify_prob: float (optional, default=1.0)
        The search graph get "diversified" by removing potentially unnecessary
        edges. This controls the volume of edges removed. A value of 0.0 ensures
        that no edges get removed, and larger values result in significantly more
        aggressive edge removal. A value of 1.0 will prune all edges that it can.

    tree_init: bool (optional, default=True)
        Whether to use random projection trees for initialization.

    random_state: int, RandomState instance or None, optional (default: None)
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by `np.random`.

    algorithm: string (optional, default='standard')
        This implementation provides an alternative algorithm for
        construction of the k-neighbors graph used as a search index. The
        alternative algorithm can be fast for large ``n_neighbors`` values.
        The``'alternative'`` algorithm has been deprecated and is no longer
        available.

    low_memory: boolean (optional, default=False)
        Whether to use a lower memory, but more computationally expensive
        approach to index construction. This defaults to false as for most
        cases it speeds index construction, but if you are having issues
        with excessive memory use for your dataset consider setting this
        to True.

    max_candidates: int (optional, default=20)
        Internally each "self-join" keeps a maximum number of candidates (
        nearest neighbors and reverse nearest neighbors) to be considered.
        This value controls this aspect of the algorithm. Larger values will
        provide more accurate search results later, but potentially at
        non-negligible computation cost in building the index. Don't tweak
        this value unless you know what you're doing.

    n_iters: int (optional, default=None)
        The maximum number of NN-descent iterations to perform. The
        NN-descent algorithm can abort early if limited progress is being
        made, so this only controls the worst case. Don't tweak
        this value unless you know what you're doing. The default of None means
        a value will be chosen based on the size of the graph_data.

    delta: float (optional, default=0.001)
        Controls the early abort due to limited progress. Larger values
        will result in earlier aborts, providing less accurate indexes,
        and less accurate searching. Don't tweak this value unless you know
        what you're doing.

    n_jobs: int or None, optional (default=None)
        The number of parallel jobs to run for neighbors index construction.
        ``None`` means 1 unless in a :obj:`joblib.parallel_backend` context.
        ``-1`` means using all processors.

    compressed: bool (optional, default=False)
        Whether to prune out data not needed for searching the index. This will
        result in a significantly smaller index, particularly useful for saving,
        but will remove information that might otherwise be useful.

    verbose: bool (optional, default=False)
        Whether to print status graph_data during the computation.
    """

    def __init__(
        self,
        data,
        metric="euclidean",
        metric_kwds=None,
        n_neighbors=30,
        n_trees=None,
        leaf_size=None,
        pruning_degree_multiplier=1.5,
        diversify_prob=1.0,
        n_search_trees=1,
        tree_init=True,
        init_graph=None,
        random_state=None,
        low_memory=True,
        max_candidates=None,
        n_iters=None,
        delta=0.001,
        n_jobs=None,
        compressed=True,
        verbose=False,
    ):

        if n_trees is None:
            n_trees = 5 + int(round((data.shape[0]) ** 0.25))
            n_trees = min(32, n_trees)  # Only so many trees are useful
        if n_iters is None:
            n_iters = max(5, int(round(np.log2(data.shape[0]))))

        self.n_trees = n_trees
        self.n_neighbors = n_neighbors
        self.metric = metric
        self.metric_kwds = metric_kwds
        self.leaf_size = leaf_size
        self.prune_degree_multiplier = pruning_degree_multiplier
        self.diversify_prob = diversify_prob
        self.n_search_trees = n_search_trees
        self.max_candidates = max_candidates
        self.low_memory = low_memory
        self.n_iters = n_iters
        self.delta = delta
        self.dim = data.shape[1]
        self.n_jobs = n_jobs
        self.compressed = compressed
        self.verbose = verbose

        data = check_array(data, dtype=np.float32, accept_sparse="csr", order="C")
        self._raw_data = data

        if not tree_init or n_trees == 0 or init_graph is not None:
            self.tree_init = False
        else:
            self.tree_init = True

        metric_kwds = metric_kwds or {}
        self._dist_args = tuple(metric_kwds.values())

        self.random_state = random_state

        current_random_state = check_random_state(self.random_state)

        self._distance_correction = None

        if callable(metric):
            _distance_func = metric
        elif metric in pynnd_dist.named_distances:
            if metric in pynnd_dist.fast_distance_alternatives:
                _distance_func = pynnd_dist.fast_distance_alternatives[metric]["dist"]
                self._distance_correction = pynnd_dist.fast_distance_alternatives[
                    metric
                ]["correction"]
            else:
                _distance_func = pynnd_dist.named_distances[metric]
        else:
            raise ValueError("Metric is neither callable, " + "nor a recognised string")

        # Create a partial function for distances with arguments
        if len(self._dist_args) > 0:
            dist_args = self._dist_args

            @numba.njit()
            def _partial_dist_func(x, y):
                return _distance_func(x, y, *dist_args)

            self._distance_func = _partial_dist_func
        else:
            self._distance_func = _distance_func

        if metric in (
            "cosine",
            "dot",
            "correlation",
            "dice",
            "jaccard",
            "hellinger",
            "hamming",
        ):
            self._angular_trees = True
        else:
            self._angular_trees = False

        if metric == "dot":
            data = normalize(data, norm="l2", copy=False)

        self.rng_state = current_random_state.randint(INT32_MIN, INT32_MAX, 3).astype(
            np.int64
        )
        self.search_rng_state = current_random_state.randint(
            INT32_MIN, INT32_MAX, 3
        ).astype(np.int64)
        # Warm up the rng state
        for i in range(10):
            _ = tau_rand_int(self.search_rng_state)

        if self.tree_init:
            if verbose:
                print(ts(), "Building RP forest with", str(n_trees), "trees")
            self._rp_forest = make_forest(
                data,
                n_neighbors,
                n_trees,
                leaf_size,
                self.rng_state,
                current_random_state,
                self.n_jobs,
                self._angular_trees,
            )
            leaf_array = rptree_leaf_array(self._rp_forest)
        else:
            self._rp_forest = None
            leaf_array = np.array([[-1]])

        if self.max_candidates is None:
            effective_max_candidates = min(60, self.n_neighbors)
        else:
            effective_max_candidates = self.max_candidates

        # Set threading constraints
        self._original_num_threads = numba.get_num_threads()
        if self.n_jobs != -1 and self.n_jobs is not None:
            numba.set_num_threads(self.n_jobs)

        if isspmatrix_csr(self._raw_data):

            self._is_sparse = True

            if not self._raw_data.has_sorted_indices:
                self._raw_data.sort_indices()

            if metric in sparse.sparse_named_distances:
                if metric in sparse.sparse_fast_distance_alternatives:
                    _distance_func = sparse.sparse_fast_distance_alternatives[metric][
                        "dist"
                    ]
                    self._distance_correction = (
                        sparse.sparse_fast_distance_alternatives[metric]["correction"]
                    )
                else:
                    _distance_func = sparse.sparse_named_distances[metric]
            elif callable(metric):
                _distance_func = metric
            else:
                raise ValueError(
                    "Metric {} not supported for sparse data".format(metric)
                )

            if metric in sparse.sparse_need_n_features:
                metric_kwds["n_features"] = self._raw_data.shape[1]
            self._dist_args = tuple(metric_kwds.values())

            # Create a partial function for distances with arguments
            if len(self._dist_args) > 0:

                dist_args = self._dist_args

                @numba.njit()
                def _partial_dist_func(ind1, data1, ind2, data2):
                    return _distance_func(ind1, data1, ind2, data2, *dist_args)

                self._distance_func = _partial_dist_func
            else:
                self._distance_func = _distance_func

            if init_graph is None:
                _init_graph = EMPTY_GRAPH
            else:
                if init_graph.shape[0] != self._raw_data.shape[0]:
                    raise ValueError("Init graph size does not match dataset size!")
                _init_graph = make_heap(init_graph.shape[0], self.n_neighbors)
                _init_graph = sparse_initalize_heap_from_graph_indices(
                    _init_graph,
                    init_graph,
                    self._raw_data.indptr,
                    self._raw_data.indices,
                    self._raw_data.data,
                    self._distance_func,
                )

            if verbose:
                print(ts(), "metric NN descent for", str(n_iters), "iterations")

            self._neighbor_graph = sparse_nnd.nn_descent(
                self._raw_data.indices,
                self._raw_data.indptr,
                self._raw_data.data,
                self.n_neighbors,
                self.rng_state,
                max_candidates=effective_max_candidates,
                dist=self._distance_func,
                n_iters=self.n_iters,
                delta=self.delta,
                rp_tree_init=True,
                leaf_array=leaf_array,
                init_graph=_init_graph,
                low_memory=self.low_memory,
                verbose=verbose,
            )

        else:

            self._is_sparse = False

            if init_graph is None:
                _init_graph = EMPTY_GRAPH
            else:
                if init_graph.shape[0] != self._raw_data.shape[0]:
                    raise ValueError("Init graph size does not match dataset size!")
                _init_graph = make_heap(init_graph.shape[0], self.n_neighbors)
                _init_graph = initalize_heap_from_graph_indices(
                    _init_graph, init_graph, data, self._distance_func
                )

            if verbose:
                print(ts(), "NN descent for", str(n_iters), "iterations")

            self._neighbor_graph = nn_descent(
                self._raw_data,
                self.n_neighbors,
                self.rng_state,
                effective_max_candidates,
                self._distance_func,
                self.n_iters,
                self.delta,
                low_memory=self.low_memory,
                rp_tree_init=True,
                init_graph=_init_graph,
                leaf_array=leaf_array,
                verbose=verbose,
            )

        if np.any(self._neighbor_graph[0] < 0):
            warn(
                "Failed to correctly find n_neighbors for some samples."
                "Results may be less than ideal. Try re-running with"
                "different parameters."
            )

        numba.set_num_threads(self._original_num_threads)

    def __getstate__(self):
        if not hasattr(self, "_search_graph"):
            self._init_search_graph()
        if not hasattr(self, "_search_function"):
            if self._is_sparse:
                self._init_sparse_search_function()
            else:
                self._init_search_function()
        result = self.__dict__.copy()
        if hasattr(self, "_rp_forest"):
            del result["_rp_forest"]
        result["_search_forest"] = tuple(
            [denumbaify_tree(tree) for tree in self._search_forest]
        )
        return result

    def __setstate__(self, d):
        self.__dict__ = d
        self._search_forest = tuple(
            [renumbaify_tree(tree) for tree in d["_search_forest"]]
        )
        if self._is_sparse:
            self._init_sparse_search_function()
        else:
            self._init_search_function()

    def _init_search_graph(self):

        # Set threading constraints
        self._original_num_threads = numba.get_num_threads()
        if self.n_jobs != -1 and self.n_jobs is not None:
            numba.set_num_threads(self.n_jobs)

        if not hasattr(self, "_search_forest"):
            if self._rp_forest is None:
                # We don't have a forest, so make a small search forest
                current_random_state = check_random_state(self.random_state)
                rp_forest = make_forest(
                    self._raw_data,
                    self.n_neighbors,
                    self.n_search_trees,
                    self.leaf_size,
                    self.rng_state,
                    current_random_state,
                    self.n_jobs,
                    self._angular_trees,
                )
                self._search_forest = [
                    convert_tree_format(tree, self._raw_data.shape[0])
                    for tree in rp_forest
                ]
            else:
                # convert the best trees into a search forest
                tree_scores = [
                    score_linked_tree(tree, self._neighbor_graph[0])
                    for tree in self._rp_forest
                ]
                if self.verbose:
                    print(ts(), "Worst tree score: {:.8f}".format(np.min(tree_scores)))
                    print(ts(), "Mean tree score: {:.8f}".format(np.mean(tree_scores)))
                    print(ts(), "Best tree score: {:.8f}".format(np.max(tree_scores)))
                best_tree_indices = np.argsort(tree_scores)[: self.n_search_trees]
                best_trees = [self._rp_forest[idx] for idx in best_tree_indices]
                del self._rp_forest
                self._search_forest = [
                    convert_tree_format(tree, self._raw_data.shape[0])
                    for tree in best_trees
                ]

        nnz_pre_diversify = np.sum(self._neighbor_graph[0] >= 0)
        if self._is_sparse:
            if self.compressed:
                diversified_rows, diversified_data = sparse.diversify(
                    self._neighbor_graph[0],
                    self._neighbor_graph[1],
                    self._raw_data.indices,
                    self._raw_data.indptr,
                    self._raw_data.data,
                    self._distance_func,
                    self.rng_state,
                    self.diversify_prob,
                )
            else:
                diversified_rows, diversified_data = sparse.diversify(
                    self._neighbor_graph[0].copy(),
                    self._neighbor_graph[1].copy(),
                    self._raw_data.indices,
                    self._raw_data.indptr,
                    self._raw_data.data,
                    self._distance_func,
                    self.rng_state,
                    self.diversify_prob,
                )
        else:
            if self.compressed:
                diversified_rows, diversified_data = diversify(
                    self._neighbor_graph[0],
                    self._neighbor_graph[1],
                    self._raw_data,
                    self._distance_func,
                    self.rng_state,
                    self.diversify_prob,
                )
            else:
                diversified_rows, diversified_data = diversify(
                    self._neighbor_graph[0].copy(),
                    self._neighbor_graph[1].copy(),
                    self._raw_data,
                    self._distance_func,
                    self.rng_state,
                    self.diversify_prob,
                )

        self._search_graph = coo_matrix(
            (self._raw_data.shape[0], self._raw_data.shape[0]), dtype=np.float32
        )

        # Preserve any distance 0 points
        diversified_data[diversified_data == 0.0] = FLOAT32_EPS

        self._search_graph.row = np.repeat(
            np.arange(diversified_rows.shape[0], dtype=np.int32),
            diversified_rows.shape[1],
        )
        self._search_graph.col = diversified_rows.ravel()
        self._search_graph.data = diversified_data.ravel()

        # Get rid of any -1 index entries
        self._search_graph = self._search_graph.tocsr()
        self._search_graph.data[self._search_graph.indices == -1] = 0.0
        self._search_graph.eliminate_zeros()

        if self.verbose:
            print(
                ts(),
                "Forward diversification reduced edges from {} to {}".format(
                    nnz_pre_diversify, self._search_graph.nnz
                ),
            )

        # Reverse graph
        pre_reverse_diversify_nnz = self._search_graph.nnz
        reverse_graph = self._search_graph.transpose()
        if self._is_sparse:
            sparse.diversify_csr(
                reverse_graph.indptr,
                reverse_graph.indices,
                reverse_graph.data,
                self._raw_data.indptr,
                self._raw_data.indices,
                self._raw_data.data,
                self._distance_func,
                self.rng_state,
                self.diversify_prob,
            )
        else:
            diversify_csr(
                reverse_graph.indptr,
                reverse_graph.indices,
                reverse_graph.data,
                self._raw_data,
                self._distance_func,
                self.rng_state,
                self.diversify_prob,
            )
        reverse_graph.eliminate_zeros()

        if self.verbose:
            print(
                ts(),
                "Reverse diversification reduced edges from {} to {}".format(
                    pre_reverse_diversify_nnz, reverse_graph.nnz
                ),
            )
        reverse_graph = reverse_graph.tocsr()
        reverse_graph.sort_indices()
        self._search_graph = self._search_graph.tocsr()
        self._search_graph.sort_indices()
        self._search_graph = self._search_graph.maximum(reverse_graph).tocsr()

        # Eliminate the diagonal
        self._search_graph.setdiag(0.0)
        self._search_graph.eliminate_zeros()

        pre_prune_nnz = self._search_graph.nnz
        self._search_graph = degree_prune(
            self._search_graph,
            int(np.round(self.prune_degree_multiplier * self.n_neighbors)),
        )
        self._search_graph.eliminate_zeros()
        self._search_graph = (self._search_graph != 0).astype(np.uint8)

        if self.verbose:
            print(
                ts(),
                "Degree pruning reduced edges from {} to {}".format(
                    pre_prune_nnz, self._search_graph.nnz
                ),
            )

        self._visited = np.zeros(
            (self._raw_data.shape[0] // 8) + 1, dtype=np.uint8, order="C"
        )

        # reorder according to the search tree leaf order
        if self.verbose:
            print(ts(), "Resorting data and graph based on tree order")
        self._vertex_order = self._search_forest[0].indices
        row_ordered_graph = self._search_graph[self._vertex_order, :].tocsc()
        self._search_graph = row_ordered_graph[:, self._vertex_order]
        self._search_graph = self._search_graph.tocsr()
        self._search_graph.sort_indices()

        if self._is_sparse:
            self._raw_data = self._raw_data[self._vertex_order, :]
        else:
            self._raw_data = np.ascontiguousarray(self._raw_data[self._vertex_order, :])

        tree_order = np.argsort(self._vertex_order)
        self._search_forest = tuple(
            resort_tree_indices(tree, tree_order)
            for tree in self._search_forest[: self.n_search_trees]
        )

        if self.compressed:
            if self.verbose:
                print(ts(), "Compressing index by removing unneeded attributes")
            if hasattr(self, "_rp_forest"):
                del self._rp_forest
            del self._neighbor_graph

        numba.set_num_threads(self._original_num_threads)

    def _init_search_function(self):

        if self.verbose:
            print(ts(), "Building and compiling search function")

        tree_hyperplanes = self._search_forest[0].hyperplanes
        tree_offsets = self._search_forest[0].offsets
        tree_indices = self._search_forest[0].indices
        tree_children = self._search_forest[0].children

        @numba.njit(
            [
                numba.types.Array(numba.types.int32, 1, "C", readonly=True)(
                    numba.types.Array(numba.types.float32, 1, "C", readonly=True),
                    numba.types.Array(numba.types.int64, 1, "C", readonly=False),
                )
            ],
            locals={"node": numba.types.uint32, "side": numba.types.boolean},
        )
        def tree_search_closure(point, rng_state):
            node = 0
            while tree_children[node, 0] > 0:
                side = select_side(
                    tree_hyperplanes[node], tree_offsets[node], point, rng_state
                )
                if side == 0:
                    node = tree_children[node, 0]
                else:
                    node = tree_children[node, 1]

            return -tree_children[node]

        self._tree_search = tree_search_closure

        alternative_dot = pynnd_dist.alternative_dot
        alternative_cosine = pynnd_dist.alternative_cosine

        data = self._raw_data
        indptr = self._search_graph.indptr
        indices = self._search_graph.indices
        dist = self._distance_func
        n_neighbors = self.n_neighbors

        @numba.njit(
            fastmath=True,
            locals={
                "current_query": numba.types.float32[::1],
                "i": numba.types.uint32,
                "j": numba.types.uint32,
                "heap_priorities": numba.types.float32[::1],
                "heap_indices": numba.types.int32[::1],
                "candidate": numba.types.int32,
                "vertex": numba.types.int32,
                "d": numba.types.float32,
                "d_vertex": numba.types.float32,
                "visited": numba.types.uint8[::1],
                "indices": numba.types.int32[::1],
                "indptr": numba.types.int32[::1],
                "data": numba.types.float32[:, ::1],
                "heap_size": numba.types.int16,
                "distance_scale": numba.types.float32,
                "distance_bound": numba.types.float32,
                "seed_scale": numba.types.float32,
            },
        )
        def search_closure(query_points, k, epsilon, visited, rng_state):

            result = make_heap(query_points.shape[0], k)
            distance_scale = 1.0 + epsilon
            internal_rng_state = np.copy(rng_state)

            for i in range(query_points.shape[0]):
                visited[:] = 0
                if dist == alternative_dot or dist == alternative_cosine:
                    norm = np.sqrt((query_points[i] ** 2).sum())
                    if norm > 0.0:
                        current_query = query_points[i] / norm
                    else:
                        continue
                else:
                    current_query = query_points[i]

                heap_priorities = result[1][i]
                heap_indices = result[0][i]
                seed_set = [(np.float32(np.inf), np.int32(-1)) for j in range(0)]
                # heapq.heapify(seed_set)

                ############ Init ################
                index_bounds = tree_search_closure(current_query, internal_rng_state)
                candidate_indices = tree_indices[index_bounds[0] : index_bounds[1]]

                n_initial_points = candidate_indices.shape[0]
                n_random_samples = min(k, n_neighbors) - n_initial_points

                for j in range(n_initial_points):
                    candidate = candidate_indices[j]
                    d = dist(data[candidate], current_query)
                    # indices are guaranteed different
                    simple_heap_push(heap_priorities, heap_indices, d, candidate)
                    heapq.heappush(seed_set, (d, candidate))
                    mark_visited(visited, candidate)

                if n_random_samples > 0:
                    for j in range(n_random_samples):
                        candidate = np.int32(
                            np.abs(tau_rand_int(internal_rng_state)) % data.shape[0]
                        )
                        if has_been_visited(visited, candidate) == 0:
                            d = dist(data[candidate], current_query)
                            simple_heap_push(
                                heap_priorities, heap_indices, d, candidate
                            )
                            heapq.heappush(seed_set, (d, candidate))
                            mark_visited(visited, candidate)

                ############ Search ##############
                distance_bound = distance_scale * heap_priorities[0]

                # Find smallest seed point
                d_vertex, vertex = heapq.heappop(seed_set)

                while d_vertex < distance_bound:

                    for j in range(indptr[vertex], indptr[vertex + 1]):

                        candidate = indices[j]

                        if has_been_visited(visited, candidate) == 0:
                            mark_visited(visited, candidate)

                            d = dist(data[candidate], current_query)

                            if d < distance_bound:
                                simple_heap_push(
                                    heap_priorities, heap_indices, d, candidate
                                )
                                heapq.heappush(seed_set, (d, candidate))
                                # Update bound
                                distance_bound = distance_scale * heap_priorities[0]

                    # find new smallest seed point
                    if len(seed_set) == 0:
                        break
                    else:
                        d_vertex, vertex = heapq.heappop(seed_set)

            return result

        self._search_function = search_closure
        # Force compilation of the search function (hardcoded k, epsilon)
        query_data = self._raw_data[:1]
        _ = self._search_function(
            query_data, 5, 0.0, self._visited, self.search_rng_state
        )

    def _init_sparse_search_function(self):

        if self.verbose:
            print(ts(), "Building and compiling sparse search function")

        tree_hyperplanes = self._search_forest[0].hyperplanes
        tree_offsets = self._search_forest[0].offsets
        tree_indices = self._search_forest[0].indices
        tree_children = self._search_forest[0].children

        @numba.njit(
            [
                numba.types.Array(numba.types.int32, 1, "C", readonly=True)(
                    numba.types.Array(numba.types.int32, 1, "C", readonly=True),
                    numba.types.Array(numba.types.float32, 1, "C", readonly=True),
                    numba.types.Array(numba.types.int64, 1, "C", readonly=False),
                )
            ],
            locals={"node": numba.types.uint32, "side": numba.types.boolean},
        )
        def sparse_tree_search_closure(point_inds, point_data, rng_state):
            node = 0
            while tree_children[node, 0] > 0:
                side = sparse_select_side(
                    tree_hyperplanes[node],
                    tree_offsets[node],
                    point_inds,
                    point_data,
                    rng_state,
                )
                if side == 0:
                    node = tree_children[node, 0]
                else:
                    node = tree_children[node, 1]

            return -tree_children[node]

        self._tree_search = sparse_tree_search_closure

        from pynndescent.distances import alternative_dot, alternative_cosine

        data_inds = self._raw_data.indices
        data_indptr = self._raw_data.indptr
        data_data = self._raw_data.data
        indptr = self._search_graph.indptr
        indices = self._search_graph.indices
        dist = self._distance_func
        n_neighbors = self.n_neighbors

        @numba.njit(
            fastmath=True,
            locals={
                "current_query": numba.types.float32[::1],
                "i": numba.types.uint32,
                "heap_priorities": numba.types.float32[::1],
                "heap_indices": numba.types.int32[::1],
                "candidate": numba.types.int32,
                "d": numba.types.float32,
                "visited": numba.types.uint8[::1],
                "indices": numba.types.int32[::1],
                "indptr": numba.types.int32[::1],
                "data": numba.types.float32[:, ::1],
                "heap_size": numba.types.int16,
                "distance_scale": numba.types.float32,
                "seed_scale": numba.types.float32,
            },
        )
        def search_closure(
            query_inds, query_indptr, query_data, k, epsilon, visited, rng_state
        ):

            n_query_points = query_indptr.shape[0] - 1
            n_index_points = data_indptr.shape[0] - 1
            result = make_heap(n_query_points, k)
            distance_scale = 1.0 + epsilon
            internal_rng_state = np.copy(rng_state)

            for i in range(n_query_points):
                visited[:] = 0

                current_query_inds = query_inds[query_indptr[i] : query_indptr[i + 1]]
                current_query_data = query_data[query_indptr[i] : query_indptr[i + 1]]

                if dist == alternative_dot or dist == alternative_cosine:
                    norm = np.sqrt((current_query_data ** 2).sum())
                    if norm > 0.0:
                        current_query_data = current_query_data / norm
                    else:
                        continue

                heap_priorities = result[1][i]
                heap_indices = result[0][i]
                seed_set = [(np.float32(np.inf), np.int32(-1)) for j in range(0)]
                heapq.heapify(seed_set)

                ############ Init ################
                index_bounds = sparse_tree_search_closure(
                    current_query_inds, current_query_data, internal_rng_state
                )
                candidate_indices = tree_indices[index_bounds[0] : index_bounds[1]]

                n_initial_points = candidate_indices.shape[0]
                n_random_samples = min(k, n_neighbors) - n_initial_points

                for j in range(n_initial_points):
                    candidate = candidate_indices[j]

                    from_inds = data_inds[
                        data_indptr[candidate] : data_indptr[candidate + 1]
                    ]
                    from_data = data_data[
                        data_indptr[candidate] : data_indptr[candidate + 1]
                    ]

                    d = dist(
                        from_inds, from_data, current_query_inds, current_query_data
                    )
                    # indices are guaranteed different
                    simple_heap_push(heap_priorities, heap_indices, d, candidate)
                    heapq.heappush(seed_set, (d, candidate))
                    mark_visited(visited, candidate)

                if n_random_samples > 0:
                    for j in range(n_random_samples):
                        candidate = np.int32(
                            np.abs(tau_rand_int(internal_rng_state)) % n_index_points
                        )
                        if has_been_visited(visited, candidate) == 0:
                            from_inds = data_inds[
                                data_indptr[candidate] : data_indptr[candidate + 1]
                            ]
                            from_data = data_data[
                                data_indptr[candidate] : data_indptr[candidate + 1]
                            ]

                            d = dist(
                                from_inds,
                                from_data,
                                current_query_inds,
                                current_query_data,
                            )

                            simple_heap_push(
                                heap_priorities, heap_indices, d, candidate
                            )
                            heapq.heappush(seed_set, (d, candidate))
                            mark_visited(visited, candidate)

                ############ Search ##############
                distance_bound = distance_scale * heap_priorities[0]

                # Find smallest seed point
                d_vertex, vertex = heapq.heappop(seed_set)

                while d_vertex < distance_bound:

                    for j in range(indptr[vertex], indptr[vertex + 1]):

                        candidate = indices[j]

                        if has_been_visited(visited, candidate) == 0:
                            mark_visited(visited, candidate)

                            from_inds = data_inds[
                                data_indptr[candidate] : data_indptr[candidate + 1]
                            ]
                            from_data = data_data[
                                data_indptr[candidate] : data_indptr[candidate + 1]
                            ]

                            d = dist(
                                from_inds,
                                from_data,
                                current_query_inds,
                                current_query_data,
                            )

                            if d < distance_bound:
                                simple_heap_push(
                                    heap_priorities, heap_indices, d, candidate
                                )
                                heapq.heappush(seed_set, (d, candidate))
                                # Update bound
                                distance_bound = distance_scale * heap_priorities[0]

                    # find new smallest seed point
                    if len(seed_set) == 0:
                        break
                    else:
                        d_vertex, vertex = heapq.heappop(seed_set)

            return result

        self._search_function = search_closure

        # Force compilation of the search function (hardcoded k, epsilon)
        query_data = self._raw_data[:1]
        _ = self._search_function(
            query_data.indices,
            query_data.indptr,
            query_data.data,
            5,
            0.0,
            self._visited,
            self.search_rng_state,
        )

    @property
    def neighbor_graph(self):
        if self.compressed and not hasattr(self, "_neighbor_graph"):
            warn("Compressed indexes do not have neighbor graph information.")
            return None
        if self._distance_correction is not None:
            result = (
                self._neighbor_graph[0].copy(),
                self._distance_correction(self._neighbor_graph[1]),
            )
        else:
            result = (self._neighbor_graph[0].copy(), self._neighbor_graph[1].copy())

        return result

    def compress_index(self):
        import gc

        self.prepare()
        self.compressed = True

        if hasattr(self, "_rp_forest"):
            del self._rp_forest
        if hasattr(self, "_neighbor_graph"):
            del self._neighbor_graph

        gc.collect()
        return

    def prepare(self):
        if not hasattr(self, "_search_graph"):
            self._init_search_graph()
        if not hasattr(self, "_search_function"):
            if self._is_sparse:
                self._init_sparse_search_function()
            else:
                self._init_search_function()
        return

    def query(self, query_data, k=10, epsilon=0.1):
        """Query the training graph_data for the k nearest neighbors

        Parameters
        ----------
        query_data: array-like, last dimension self.dim
            An array of points to query

        k: integer (default = 10)
            The number of nearest neighbors to return

        epsilon: float (optional, default=0.1)
            When searching for nearest neighbors of a query point this values
            controls the trade-off between accuracy and search cost. Larger values
            produce more accurate nearest neighbor results at larger computational
            cost for the search. Values should be in the range 0.0 to 0.5, but
            should probably not exceed 0.3 without good reason.

        Returns
        -------
        indices, distances: array (n_query_points, k), array (n_query_points, k)
            The first array, ``indices``, provides the indices of the graph_data
            points in the training set that are the nearest neighbors of
            each query point. Thus ``indices[i, j]`` is the index into the
            training graph_data of the jth nearest neighbor of the ith query points.

            Similarly ``distances`` provides the distances to the neighbors
            of the query points such that ``distances[i, j]`` is the distance
            from the ith query point to its jth nearest neighbor in the
            training graph_data.
        """
        if not hasattr(self, "_search_graph"):
            self._init_search_graph()

        if not self._is_sparse:
            # Standard case
            if not hasattr(self, "_search_function"):
                self._init_search_function()

            query_data = np.asarray(query_data).astype(np.float32, order="C")
            result = self._search_function(
                query_data, k, epsilon, self._visited, self.search_rng_state
            )
        else:
            # Sparse case
            if not hasattr(self, "_search_function"):
                self._init_sparse_search_function()

            query_data = check_array(query_data, accept_sparse="csr", dtype=np.float32)
            if not isspmatrix_csr(query_data):
                query_data = csr_matrix(query_data, dtype=np.float32)
            if not query_data.has_sorted_indices:
                query_data.sort_indices()

            result = self._search_function(
                query_data.indices,
                query_data.indptr,
                query_data.data,
                k,
                epsilon,
                self._visited,
                self.search_rng_state,
            )

        indices, dists = deheap_sort(result)
        # Sort to input graph_data order
        indices = self._vertex_order[indices]

        if self._distance_correction is not None:
            dists = self._distance_correction(dists)

        return indices, dists

    def update(self, X):
        current_random_state = check_random_state(self.random_state)
        rng_state = current_random_state.randint(INT32_MIN, INT32_MAX, 3).astype(
            np.int64
        )
        X = check_array(X, dtype=np.float32, accept_sparse="csr", order="C")

        original_order = np.argsort(self._vertex_order)

        if self._is_sparse:
            self._raw_data = sparse_vstack([self._raw_data, X])
        else:
            self._raw_data = np.ascontiguousarray(
                np.vstack([self._raw_data[original_order, :], X])
            )

        if self._is_sparse:
            raise NotImplementedError("Sparse update not complete yet")
        else:
            self.n_trees = int(np.round(self.n_trees / 3))
            self._rp_forest = make_forest(
                self._raw_data,
                self.n_neighbors,
                self.n_trees,
                self.leaf_size,
                rng_state,
                current_random_state,
                self.n_jobs,
                self._angular_trees,
            )
            leaf_array = rptree_leaf_array(self._rp_forest)
            current_graph = make_heap(self._raw_data.shape[0], self.n_neighbors)
            init_from_neighbor_graph(
                current_graph, self._neighbor_graph[0], self._neighbor_graph[1]
            )
            init_rp_tree(self._raw_data, self._distance_func, current_graph, leaf_array)

            if self.max_candidates is None:
                effective_max_candidates = min(60, self.n_neighbors)
            else:
                effective_max_candidates = self.max_candidates

            self._neighbor_graph = nn_descent(
                self._raw_data,
                self.n_neighbors,
                self.rng_state,
                effective_max_candidates,
                self._distance_func,
                self.n_iters,
                self.delta,
                init_graph=current_graph,
                low_memory=self.low_memory,
                rp_tree_init=False,
                leaf_array=np.array([[-1], [-1]]),
                verbose=self.verbose,
            )


class PyNNDescentTransformer(BaseEstimator, TransformerMixin):
    """PyNNDescentTransformer for fast approximate nearest neighbor transformer.
    It uses the NNDescent algorithm, and is thus
    very flexible and supports a wide variety of distances, including
    non-metric distances. NNDescent also scales well against high dimensional
    graph_data in many cases.

    Transform X into a (weighted) graph of k nearest neighbors

    The transformed graph_data is a sparse graph as returned by kneighbors_graph.

    Parameters
    ----------
    n_neighbors: int (optional, default=5)
        The number of neighbors to use in k-neighbor graph graph_data structure
        used for fast approximate nearest neighbor search. Larger values
        will result in more accurate search results at the cost of
        computation time.

    metric: string or callable (optional, default='euclidean')
        The metric to use for computing nearest neighbors. If a callable is
        used it must be a numba njit compiled function. Supported metrics
        include:
            * euclidean
            * manhattan
            * chebyshev
            * minkowski
            * canberra
            * braycurtis
            * mahalanobis
            * wminkowski
            * seuclidean
            * cosine
            * correlation
            * haversine
            * hamming
            * jaccard
            * dice
            * russelrao
            * kulsinski
            * rogerstanimoto
            * sokalmichener
            * sokalsneath
            * yule
        Metrics that take arguments (such as minkowski, mahalanobis etc.)
        can have arguments passed via the metric_kwds dictionary. At this
        time care must be taken and dictionary elements must be ordered
        appropriately; this will hopefully be fixed in the future.

    metric_kwds: dict (optional, default {})
        Arguments to pass on to the metric, such as the ``p`` value for
        Minkowski distance.

    n_trees: int (optional, default=None)
        This implementation uses random projection forests for initialization
        of searches. This parameter controls the number of trees in that
        forest. A larger number will result in more accurate neighbor
        computation at the cost of performance. The default of None means
        a value will be chosen based on the size of the graph_data.

    leaf_size: int (optional, default=None)
        The maximum number of points in a leaf for the random projection trees.
        The default of None means a value will be chosen based on n_neighbors.

    pruning_degree_multiplier: float (optional, default=1.5)
        How aggressively to prune the graph. Since the search graph is undirected
        (and thus includes nearest neighbors and reverse nearest neighbors) vertices
        can have very high degree -- the graph will be pruned such that no
        vertex has degree greater than
        ``pruning_degree_multiplier * n_neighbors``.

    diversify_prob: float (optional, default=1.0)
        The search graph get "diversified" by removing potentially unnecessary
        edges. This controls the volume of edges removed. A value of 0.0 ensures
        that no edges get removed, and larger values result in significantly more
        aggressive edge removal. A value of 1.0 will prune all edges that it can.

    n_search_trees: float (optional, default=1)
        The number of random projection trees to use in initializing searching or
        querying.

    search_epsilon: float (optional, default=0.1)
        When searching for nearest neighbors of a query point this values
        controls the trade-off between accuracy and search cost. Larger values
        produce more accurate nearest neighbor results at larger computational
        cost for the search. Values should be in the range 0.0 to 0.5, but
        should probably not exceed 0.3 without good reason.

    tree_init: bool (optional, default=True)
        Whether to use random projection trees for initialization.

    random_state: int, RandomState instance or None, optional (default: None)
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by `np.random`.

    n_jobs: int or None (optional, default=None)
        The maximum number of parallel threads to be run at a time. If none
        this will default to using all the cores available. Note that there is
        not perfect parallelism, so at several pints the algorithm will be
        single threaded.

    low_memory: boolean (optional, default=False)
        Whether to use a lower memory, but more computationally expensive
        approach to index construction. This defaults to false as for most
        cases it speeds index construction, but if you are having issues
        with excessive memory use for your dataset consider setting this
        to True.

    max_candidates: int (optional, default=20)
        Internally each "self-join" keeps a maximum number of candidates (
        nearest neighbors and reverse nearest neighbors) to be considered.
        This value controls this aspect of the algorithm. Larger values will
        provide more accurate search results later, but potentially at
        non-negligible computation cost in building the index. Don't tweak
        this value unless you know what you're doing.

    n_iters: int (optional, default=None)
        The maximum number of NN-descent iterations to perform. The
        NN-descent algorithm can abort early if limited progress is being
        made, so this only controls the worst case. Don't tweak
        this value unless you know what you're doing. The default of None means
        a value will be chosen based on the size of the graph_data.

    early_termination_value: float (optional, default=0.001)
        Controls the early abort due to limited progress. Larger values
        will result in earlier aborts, providing less accurate indexes,
        and less accurate searching. Don't tweak this value unless you know
        what you're doing.

    verbose: bool (optional, default=False)
        Whether to print status graph_data during the computation.

    Examples
    --------
    >>> from sklearn.manifold import Isomap
    >>> from pynndescent import PyNNDescentTransformer
    >>> from sklearn.pipeline import make_pipeline
    >>> estimator = make_pipeline(
    ...     PyNNDescentTransformer(n_neighbors=5),
    ...     Isomap(neighbors_algorithm='precomputed'))
    """

    def __init__(
        self,
        n_neighbors=30,
        metric="euclidean",
        metric_kwds=None,
        n_trees=None,
        leaf_size=None,
        search_epsilon=0.1,
        pruning_degree_multiplier=1.5,
        diversify_prob=1.0,
        n_search_trees=1,
        tree_init=True,
        random_state=None,
        n_jobs=None,
        low_memory=True,
        max_candidates=None,
        n_iters=None,
        early_termination_value=0.001,
        verbose=False,
    ):

        self.n_neighbors = n_neighbors
        self.metric = metric
        self.metric_kwds = metric_kwds
        self.n_trees = n_trees
        self.leaf_size = leaf_size
        self.search_epsilon = search_epsilon
        self.pruning_degree_multiplier = pruning_degree_multiplier
        self.diversify_prob = diversify_prob
        self.n_search_trees = n_search_trees
        self.tree_init = tree_init
        self.random_state = random_state
        self.low_memory = low_memory
        self.max_candidates = max_candidates
        self.n_iters = n_iters
        self.early_termination_value = early_termination_value
        self.n_jobs = n_jobs
        self.verbose = verbose

    def fit(self, X, compress_index=True):
        """Fit the PyNNDescent transformer to build KNN graphs with
        neighbors given by the dataset X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Sample graph_data

        Returns
        -------
        transformer : PyNNDescentTransformer
            The trained transformer
        """
        self.n_samples_fit = X.shape[0]

        if self.metric_kwds is None:
            metric_kwds = {}
        else:
            metric_kwds = self.metric_kwds

        if self.verbose:
            print(ts(), "Creating index")

        # Compatibility with sklearn, which doesn't consider
        # a point its own neighbor for these purposes.
        effective_n_neighbors = self.n_neighbors + 1

        self.index_ = NNDescent(
            X,
            metric=self.metric,
            metric_kwds=metric_kwds,
            n_neighbors=effective_n_neighbors,
            n_trees=self.n_trees,
            leaf_size=self.leaf_size,
            pruning_degree_multiplier=self.pruning_degree_multiplier,
            diversify_prob=self.diversify_prob,
            n_search_trees=self.n_search_trees,
            tree_init=self.tree_init,
            random_state=self.random_state,
            low_memory=self.low_memory,
            max_candidates=self.max_candidates,
            n_iters=self.n_iters,
            delta=self.early_termination_value,
            n_jobs=self.n_jobs,
            compressed=compress_index,
            verbose=self.verbose,
        )

        return self

    def transform(self, X, y=None):
        """Computes the (weighted) graph of Neighbors for points in X

        Parameters
        ----------
        X : array-like, shape (n_samples_transform, n_features)
            Sample graph_data

        Returns
        -------
        Xt : CSR sparse matrix, shape (n_samples_transform, n_samples_fit)
            Xt[i, j] is assigned the weight of edge that connects i to j.
            Only the neighbors have an explicit value.
        """

        if X is None:
            n_samples_transform = self.n_samples_fit
        else:
            n_samples_transform = X.shape[0]

        if X is None:
            indices, distances = self.index_.neighbor_graph
        else:
            indices, distances = self.index_.query(
                X, k=self.n_neighbors, epsilon=self.search_epsilon
            )

        if self.verbose:
            print(ts(), "Constructing neighbor matrix")
        result = coo_matrix((n_samples_transform, self.n_samples_fit), dtype=np.float32)
        result.row = np.repeat(
            np.arange(indices.shape[0], dtype=np.int32), indices.shape[1]
        )
        result.col = indices.ravel()
        result.data = distances.ravel()

        return result.tocsr()

    def fit_transform(self, X, y=None, **fit_params):
        """Fit to graph_data, then transform it.

        Fits transformer to X and y with optional parameters fit_params
        and returns a transformed version of X.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            Training set.

        y : ignored

        Returns
        -------
        Xt : CSR sparse matrix, shape (n_samples, n_samples)
            Xt[i, j] is assigned the weight of edge that connects i to j.
            Only the neighbors have an explicit value.
            The diagonal is always explicit.
        """
        self.fit(X, compress_index=False)
        result = self.transform(X=None)

        if self.verbose:
            print(ts(), "Compressing index")
        self.index_.compress_index()

        return result
