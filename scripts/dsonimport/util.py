import bisect, cProfile, errno, logging, math, os, platform, pstats, re
from pprint import pprint
from contextlib import contextmanager
from StringIO import StringIO
import subprocess

try:
    import scandir
except ImportError:
    from wscandir import scandir

log = logging.getLogger('DSONImporter')

@contextmanager
def run_profiling():
    pr = cProfile.Profile()
    pr.enable()
    try:
        yield
    finally:
        pr.disable()
        s = StringIO()

        sortby = 'cumulative'
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        ps.print_stats()
        log.debug(s.getvalue())

def flatten_dictionary(data):
    """
    Flatten a DSON value dictionary.

    >>> result = flatten_dictionary({
    ...    'a': 1,
    ...    'b': {
    ...        'c': 3,
    ...        'd': {
    ...            'e': 4
    ...        },
    ...    },
    ... })
    >>> sorted(result.keys())
    ['a', 'b.c', 'b.d.e']
    >>> result['a']
    1
    >>> result['b.c']
    3
    >>> result['b.d.e']
    4

    >>> result = flatten_dictionary({
    ...    'a': [
    ...        1, 2, {'x': 1}
    ...    ]
    ... })
    >>> result['a']
    [1, 2, {'x': 1}]

    >>> result = flatten_dictionary({
    ...     'extra': [{
    ...         'type': 'test',
    ...         'a': 1
    ...     }]
    ... })
    >>> result['extra.test.a']
    1
    >>> result['extra.test.type']
    'test'
    """
    result = {}
    queue = []
    def queue_dictionary(item, top_path):
        if top_path:
            top_path += '.'

        for key, value in item.iteritems():
            queue.append((top_path + key, value))
    
    queue_dictionary(data, '')
    while len(queue):
        top_path, item = queue.pop()
        if top_path == 'extra':
            for extra in item:
                queue.append(('extra.' + extra['type'], extra))
            continue

        if isinstance(item, dict):
            queue_dictionary(item, top_path)
        else:
            result[top_path] = item
    return result

def make_frozen(value):
    """
    Recursively convert lists to tuples, sets to frozensets and dictionaries
    to tuples of (key, value) tuples.
    """
    if isinstance(value, dict):
        return tuple((k, make_frozen(v)) for k, v in sorted(value.items()))
    elif isinstance(value, set):
        return frozenset(make_frozen(v) for v in value)
    elif isinstance(value, tuple):
        return tuple(make_frozen(v) for v in value)
    elif isinstance(value, list):
        return tuple(make_frozen(v) for v in value)
    else:
        return value

def srgb_to_linear(value):
    """
    Convert an SRGB color value to linear.
    """
    if value <= 0.03928:
        return value / 12.92
    else:
        return math.pow((value+0.055) / 1.055, 2.4)

def srgb_vector_to_linear(value):
    """
    Convert an SRGB color value vector to linear.
    """
    return [srgb_to_linear(v) for v in value]

def arrays_equal(a, b, tolerance=0.001):
    """
    Return true if the values of the lists a and b are the same, within the
    specified tolerance.

    This is used with vectors that we expect to be the same size, so an assertion
    is thrown if they differ.

    >>> arrays_equal([0,0,0], [0,0,0])
    True
    >>> arrays_equal([0,0,0], [1,0,0])
    False
    >>> arrays_equal([0,0,0], [1,0,0], tolerance=1)
    True
    """
    assert len(a) == len(b)
    return all(abs(a[idx] - b[idx]) <= tolerance for idx in xrange(len(a)))

def mat4_equal(a, b, tolerance=0.001):
    """
    Return true if the given matrices are identical within the given tolerance.

    >>> mat4_equal([[0,0],[0,0]], [[0,0],[0,0]])
    True
    >>> mat4_equal([[0,0],[0,0]], [[1,0],[0,0]])
    False
    >>> mat4_equal([[0,0],[0,0]], [[1,0],[0,0]], tolerance=1)
    True
    """
    assert len(a) == len(b)

    return all(arrays_equal(a[idx], b[idx], tolerance=tolerance) for idx in xrange(len(a)))

def delta_list_to_deltas_and_indices(delta_list, tolerance=0.001):
    """
    Given a list of vectors, eg. [(0,0,0), (1,0,0), (0,0,0)], return a list of indices
    and vectors, filtering out zero vectors: [1], [(1,0,0)].

    >>> delta_list_to_deltas_and_indices([(1,0,0), (0,0,0.000001), (0,1,0)])
    ([0, 2], [(1, 0, 0), (0, 1, 0)])
    """
    # Make a list of the nonzero indices in the new delta list.
    def is_nonzero(value):
        return any(abs(v) > tolerance for v in value)

    delta_vertex_idx = [idx for idx, delta in enumerate(delta_list) if is_nonzero(delta)]
    deltas = [delta_list[idx] for idx in delta_vertex_idx]
    return delta_vertex_idx, deltas

def blend_merge_shape_deltas(deltas_and_indices):
    """
    Given a list of [(indices, deltas), (indices, deltas), ...)], sum the deltas and return
    a new (indices, deltas).

    >>> deltas1 = [[0, 1], [(1,0,0), (0,1,1)]]
    >>> blend_merge_shape_deltas([deltas1])
    ([0, 1], [(1, 0, 0), (0, 1, 1)])
    >>> blend_merge_shape_deltas([deltas1, deltas1])
    ([0, 1], [(2, 0, 0), (0, 2, 2)])

    >>> deltas2 = [[2], [(1,0,0)]]
    >>> blend_merge_shape_deltas([deltas1, deltas2])
    ([0, 1, 2], [(1, 0, 0), (0, 1, 1), (1, 0, 0)])

    >>> deltas3 = [[0], [(-1,0,0)]]
    >>> blend_merge_shape_deltas([deltas1, deltas3])
    ([1], [(0, 1, 1)])

    >>> deltas4 = [[0], [(1,2,3)]]
    >>> result = blend_merge_shape_deltas([deltas4])
    >>> result[0] is deltas4[0]
    True
    >>> result[1] is deltas4[1]
    True
    """
    # If there's only one entry, there's nothing to merge.  Return the entry without making a copy,
    # for performance.
    if len(deltas_and_indices) == 1:
        return (deltas_and_indices[0][0], deltas_and_indices[0][1])

    max_vertex_idx = max(max(delta_vertex_idx) for delta_vertex_idx, deltas in deltas_and_indices) + 1
    merged_deltas = [(0,0,0)] * max_vertex_idx

    for delta_vertex_idx, deltas in deltas_and_indices:
        for idx, value in zip(delta_vertex_idx, deltas):
            total = (merged_deltas[idx][0] + value[0], merged_deltas[idx][1] + value[1], merged_deltas[idx][2] + value[2])
            merged_deltas[idx] = total

    delta_vertex_idx, deltas = delta_list_to_deltas_and_indices(merged_deltas)
    return delta_vertex_idx, deltas

def make_unique(name, name_list):
    """
    Make name unique, given a list of existing names.

    If name ends in a number, the result will always end in a numeric suffix.  For
    example, "foo0" will result in attributes named "foo0", "foo1", "foo2" and so on,
    compared to "foo" which will first give the requested "foo" followed by "foo1".
    This is useful for array-like attributes.

    >>> make_unique('foo', ['abcd'])
    'foo'
    >>> make_unique('foo', ['foo'])
    'foo1'
    >>> make_unique('foo', ['foo', 'foo1'])
    'foo2'
    >>> make_unique('foo', ['foo', 'foo1', 'foo100'])
    'foo101'
    >>> make_unique('foo0', [])
    'foo0'
    >>> make_unique('foo0', ['foo0'])
    'foo1'
    >>> make_unique('foo0', ['foo0', 'foo1'])
    'foo2'
    """
    if name not in name_list:
        return name
        
    prefix = re.match('^(.*?)([0-9]*)$', name).group(1)
    next_unused_suffix = 1
    
    for existing in name_list:
        m = re.match('^(.*?)([0-9]+)$', existing)
        if not m:
            continue
        if m.group(1) != prefix:
            continue

        idx = int(m.group(2))
        next_unused_suffix = max(next_unused_suffix, idx+1)

    return '%s%s' % (prefix, next_unused_suffix)

class VertexMapping(object):
    """
    Manage a simple mapping from vertex indices to vertex indices.
    """
    def __init__(self, value=None):
        if value is None:
            value = {}
        self.mapping = value

    def __repr__(self):
        return 'VertexMapping(mappings: %i)' % len(self.mapping)

    def __setitem__(self, src, dst):
        self.mapping[src] = dst
    def __getitem__(self, src):
        return self.mapping[src]
    def get(self, idx):
        return self.mapping.get(idx)
    def __iter__(self):
        return iter(self.mapping)
    def iteritems(self):
        return self.mapping.iteritems()
    def items(self):
        return self.mapping.items()
    def __eq__(self, rhs):
        if not isinstance(rhs, VertexMapping):
            return False
        return self.mapping == rhs.mapping

    def __deepcopy__(self, memo):
        result = VertexMapping()
        result.mapping = dict(self.mapping)
        return result

    def invert(self):
        """
        Given a {1:10, 2:20} mapping, return {10:1, 20:2}.

        This requires a many-to-one relationship.  This will raise an error if more than one
        source vertex is mapped to the same destination vertex, since that can't be inverted
        without discarding indices.

        >>> m = VertexMapping({1: 10, 2: 20})
        >>> m2 = m.invert()
        >>> m.invert() == m
        False
        >>> m.invert().invert() == m
        True
        >>> m2[10]
        1
        >>> m2.mapping
        {10: 1, 20: 2}
        """
        result = VertexMapping()
        for orig, new in self.mapping.iteritems():
            assert new not in result.mapping
            result.mapping[new] = orig
        return result

    @classmethod
    def map_destination_indices(cls, src, dst):
        """
        Given two mappings, {1:2, 3:4} and {1:100, 3:200}, map the source from each mapping
        and return a map from the destination of src to the destination of dst: {2:100, 4:200}.

        >>> m1 = VertexMapping({1: 2, 3: 4})
        >>> m2 = VertexMapping({1: 100, 3: 200})
        >>> VertexMapping.map_destination_indices(m1, m2).mapping
        {2: 100, 4: 200}
        """
        vertex_mapping = {}
        for graft_orig_vertex_idx, graft_new_vertex_idx in src.iteritems():
            target_new_vertex_idx = dst.get(graft_orig_vertex_idx)
            if target_new_vertex_idx is None:
                continue
            vertex_mapping[graft_new_vertex_idx] = target_new_vertex_idx
        return VertexMapping(vertex_mapping)

    def remap_indexed_data(self, indices, data):
        """
        Given a list of indices and data, remap the indices using this VertexMapping.  If an
        index isn't present in this mapping, discard it and its accompianing data.

        >>> m = VertexMapping({1: 100, 3: 400})
        >>> indices = [1, 2, 3, 4]
        >>> data = ['a', 'b', 'c', 'd']
        >>> m.remap_indexed_data(indices, data)
        ([100, 400], ['a', 'c'])
        """
        remapped_indices = []
        remapped_data = []
        for idx, delta in zip(indices, data):
            if idx not in self.mapping:
                continue

            remapped_data.append(delta)
            remapped_indices.append(self.mapping[idx])

        return remapped_indices, remapped_data

class ComponentList(object):
    """
    >>> c = ComponentList(['vtx[1]', 'vtx[10:20]'])
    >>> 0 in c
    False
    >>> 1 in c
    True
    >>> 2 in c
    False
    >>> 9 in c
    False
    >>> 10 in c
    True
    >>> 15 in c
    True
    >>> 20 in c
    True
    >>> 21 in c
    False
    >>> c.get_flat_list()
    [1, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]

    """
    def __init__(self, components):
	# Use a list to store the component ranges, so we can search with bisect.
	component_list = []

	for component in components:
	    assert component.startswith('vtx[') and component.endswith(']')
	    component = component[4:-1]

	    # We either have a number or a start:end range.
	    values = component.split(':')
	    if len(values) == 1:
		component = (int(values[0]), int(values[0])+1)
	    else:
		assert len(values) == 2
		component = (int(values[0]), int(values[1])+1)

	    component_list.append(component)

	self.component_list = component_list

    def __contains__(self, idx):
        """
        Return true if idx is in this component list.
        """
	entry_idx = bisect.bisect(self.component_list, (idx,))
        if entry_idx == len(self.component_list):
            entry_idx -= 1
    
        if entry_idx > 0 and idx < self.component_list[entry_idx][0]:
            entry_idx -= 1

        entry = self.component_list[entry_idx]
        return idx >= entry[0] and idx < entry[1]
    
    def get_flat_list(self):
        """
        Return a simple list of indices in this component list.
        """
        result = []
        for entry in self.component_list:
            for value in xrange(entry[0], entry[1]):
                result.append(value)
        return result

def browse_to_file(path):
    """
    Open a path in a file browser.

    This is only implemented for Windows.
    """
    # Almost every Windows program accepts forward slashes, but Explorer doesn't here.
    path = path.replace('/', '\\')
    subprocess.Popen('explorer /select,' + path)

# http://stackoverflow.com/questions/11557241
def topological_sort(source):
    """
    Perform topo sort on elements.

    source is a "{name: {set of dependancies}}" dictionary.
    Yield a list of names, with dependancies listed first.
    """
    all_names = set(source.keys())

    pending = []
    for name, deps in source.iteritems():
        # Filter out nonexistant dependencies.
        deps = deps & all_names

        pending.append((name, deps))

    emitted = []
    while pending:
        next_pending = []
        next_emitted = []
        for entry in pending:
            name, deps = entry
            deps.difference_update(emitted) # remove deps we emitted last pass
            if deps: # still has deps? recheck during next pass
                next_pending.append(entry)
                continue

            yield name
            emitted.append(name) # <-- not required, but helps preserve original ordering
            next_emitted.append(name) # remember what we emitted for difference_update() in next pass
        if not next_emitted:
            # We have remaining items but we didn't yield anything.
            log.warning('Cyclic or missing dependancy detected: %s', pformat(next_pending))
            return

        pending = next_pending
        emitted = next_emitted

# Backslashes for path separators are purely cosmetic.  Forward slashes are accepted
# almost everywhere.
if platform.system() == 'Windows':
    def _map_path_separators(s):
        return s.replace('/', '\\')
else:
    def _map_path_separators(s):
        return s

_filename_cache = {}

def normalize_filename(path):
    """
    Find a file case-insensitively and return its actual filename case.
    """
    path = path.replace('\\', '/')
    if path.endswith('/'):
        path = path[:-1]
        
    directory = os.path.dirname(path)

    # Capitalize drive letters on Windows.
    if directory[1:2] == ':':
        directory = directory[0].upper() + directory[1:]
        
    if not directory.endswith('/'):
        directory += '/'

    directory_lower = directory.lower()
    filename = os.path.basename(path)
    filename_lower = filename.lower()

    if not filename:
        return directory

    paths = _filename_cache.get(directory_lower)
    if paths is None:
        # We haven't cached this directory yet.
        directory = normalize_filename(directory)
        if not directory.endswith('/'):
            directory += '/'

        paths = os.listdir(directory)
        paths = { path.lower(): '%s%s' % (directory, path) for path in paths }

        _filename_cache[directory_lower] = paths

    normalized_filename = paths.get(filename.lower())
    if normalized_filename:
        return normalized_filename
    else:
        return path

def remove_redundant_path_slashes(path):
    """
    If a relative filename contains multiple consecutive / characters (except at the beginning,
    in case of //server/host paths), remove them.
    >>> remove_redundant_path_slashes('/test//test2')
    '/test/test2'
    >>> remove_redundant_path_slashes('//test///test2')
    '//test/test2'
    >>> remove_redundant_path_slashes('')
    ''
    """
    path_suffix = path[1:]
    path_suffix = re.sub(r'//+', '/', path[1:])
    
    return path[0:1] + path_suffix

def normalize_filename_and_relative_path(absolute_path, relative_path):
    """
    Given an absolute path and a relative path to the same file, normalize the
    absolute path with normalize_filename(), and then make the same adjustment
    to relative_path.  relative_path must be a suffix of absolute_path.
    """
    # If a relative filename contains multiple consecutive / characters (except at the beginning,
    # in case of //server/host paths), remove them.  normalize_filename will remove them, and we
    # need the path lengths to line up.
    absolute_path = remove_redundant_path_slashes(absolute_path)
    relative_path = remove_redundant_path_slashes(relative_path)
    assert absolute_path.endswith(relative_path), (absolute_path, relative_path)

    # Normalize filename case to the actual case on the filesystem.
    normalized_absolute_path = normalize_filename(absolute_path)

    # We don't expect the length to change, since we need to map the case adjustment
    # back to relative_path.
    assert len(normalized_absolute_path) == len(absolute_path), (normalized_absolute_path, absolute_path)

    normalized_relative_path = normalized_absolute_path[-len(relative_path):]
    return normalized_absolute_path, normalized_relative_path

def scandir_walk(top, topdown=True, onerror=None, followlinks=False):
    """
    Like scandir, but yield DirEntry instead of pathnames.

    scandir.walk is faster than os.walk, but for some reason it still yields pathnames
    instead of DirEntry, and we have to copy over the whole implementation to fix this.
    """
    from os.path import join, islink
    
    dirs = []
    nondirs = []

    # We may not have read permission for top, in which case we can't
    # get a list of the files the directory contains.  os.walk
    # always suppressed the exception then, rather than blow up for a
    # minor reason when (say) a thousand readable directories are still
    # left to visit.  That logic is copied here.
    try:
        scandir_it = scandir.scandir(top)
    except OSError as error:
        if onerror is not None:
            onerror(error)
        return

    while True:
        try:
            try:
                entry = next(scandir_it)
            except StopIteration:
                break
        except OSError as error:
            if onerror is not None:
                onerror(error)
            return

        try:
            is_dir = entry.is_dir()
        except OSError:
            # If is_dir() raises an OSError, consider that the entry is not
            # a directory, same behaviour than os.path.isdir().
            is_dir = False

        if is_dir:
            dirs.append(entry)
        else:
            nondirs.append(entry)

        if not topdown and is_dir:
            # Bottom-up: recurse into sub-directory, but exclude symlinks to
            # directories if followlinks is False
            if followlinks:
                walk_into = True
            else:
                try:
                    is_symlink = entry.is_symlink()
                except OSError:
                    # If is_symlink() raises an OSError, consider that the
                    # entry is not a symbolic link, same behaviour than
                    # os.path.islink().
                    is_symlink = False
                walk_into = not is_symlink

            if walk_into:
                for entry in scandir_walk(entry.path, topdown, onerror, followlinks):
                    yield entry

    # Yield before recursion if going top down
    if topdown:
        yield top, dirs, nondirs

        # Recurse into sub-directories
        for name in dirs:
            new_path = join(top, name.name)
            # Issue #23605: os.path.islink() is used instead of caching
            # entry.is_symlink() result during the loop on os.scandir() because
            # the caller can replace the directory entry during the "yield"
            # above.
            if followlinks or not islink(new_path):
                for entry in scandir_walk(new_path, topdown, onerror, followlinks):
                    yield entry
    else:
        # Yield after recursion if going bottom up
        yield top, dirs, nondirs

def mkdir_p(path):
    """
    Create a directory and its parents if they don't exist.
    """
    # Why does makedirs raise an error if the directory exists?  Nobody ever wants
    # that.
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

# This exception is raised if the operation is cancelled by the user.
class CancelledException(Exception): pass

class ProgressWindow(object):
    def __init__(self):
        self._cancel = False

    def show(self, title, total_progress_values):
        pass

    def hide(self):
        pass

    def cancel(self):
        self._cancel = True

    def check_cancellation(self):
        if self._cancel:
            raise CancelledException()

    def set_main_progress(self, job):
        # Check for cancellation when we update progress.
        self.check_cancellation()

    def set_task_progress(self, label, percent=None, force=False):
        # Check for cancellation when we update progress.
        self.check_cancellation()

if __name__ == "__main__":
    import doctest
    doctest.testmod()

