import errno, logging, gzip, math, os, re, json, sys, urllib
from pprint import pprint, pformat
from  fnmatch import fnmatch
from dsonimport import util
from DSONURL import DSONURL

try:
    from pymel import core as pm
except ImportError:
    pm = None
    pass

log = logging.getLogger('DSONImporter.DSONCore')

class NodeNotFound(KeyError):
    def __init__(self, node, missing_node, message=None, *args, **kwargs):
        self.node = node
        self.missing_node = missing_node
        if message is None:
            message = 'Not found on %s: %s' % (node.node_id, missing_node)

        super(NodeNotFound, self).__init__(message, *args, **kwargs)

class PathNotFound(IOError):
    def __init__(self, *args, **kwargs): super(PathNotFound, self).__init__(*args, **kwargs)

_not_set = object()

class DSONBase(object):
    def get(self, path, default=None):
        try:
            return self[path]
        except KeyError:
            return default
        
    def __getitem__(self, path):
        return self.get_property(path)

    def __contains__(self, path):
        return self.get_property(path, None) is not None

    def iter_get(self, path):
        """
        Convenience method: Find self[path], expect it to be an array, and iterate over
        its elements.  If the path doesn't exist, no error is raised.
        """
        prop = self.get(path)
        if prop is None:
            return iter([])
        return prop.__iter__()

    def get_label(self):
        """
        Return a name for this node, suitable for use in node names.

        For properties, this will only work if this is a channel property.
        """
        label_prop = self.get('label')
        if label_prop is None:
            label_prop = self['name'] # throw if we don't find this one
        return label_prop.value

    def get_value(self, path, default=None):
        """
        Return the value of a property relative to this one.  If the property doesn't exist,
        return default.
        """
        prop = self.get_property(path, default=None)
        if prop is not None:
            return prop.value
        else:
            return default

#import weakref
#_property_cache = weakref.WeakKeyDictionary()

class DSONProperty(DSONBase):
    """
    DSONProperty is a thin wrapper around a DSON property, to keep track of the
    node it came from and its original path.
    """
    def __init__(self, node, value, path, source_node):
        assert value is not None
        self.value = value
        self.node = node
        self.path = path
        self.source_node = source_node

    @property
    def last_path(self):
        """
        Return the final path in the property path.  For example, if this is translation/x, return "x".
        """
        return self.path.split('/')[1]

    @property
    def url(self):
        return self.node.url + '?' + urllib.quote(self.path)

    @property
    def asset_url(self):
        return self.node.asset_url + '?' + urllib.quote(self.path)

    def __repr__(self):
        s = '%s(%s)' % (self.node, str(self.path))
        return s

    @property
    def is_dynamic(self):
        """
        Return true of this DSONProperty is dynamic.

        Dynamic properties will have Maya networks created to evaluate them.  Non-dynamic
        properties are evaluated once at import.  Selectively marking properties as non-dynamic
        when they're not useful can significantly reduce the complexity of the output scene.
        """
        return self.node.env.is_property_dynamic(self)

    def get_property(self, path, default=_not_set):
        """
        Return a property relative to this one.
        """
        return self.node.get_property('%s/%s' % (self.path, path), default=default)

    @property
    def is_modifier(self):
        return self.node.is_modifier

    def __eq__(self, other):
        if not isinstance(other, DSONProperty):
            return False

        return self.path == other.path and \
            self.node is other.node

    def __hash__(self):
        return hash(self.path) + hash(self.node)

    @property
    def parent_property(self):
        """
        Return the property above this one.  For example, if this is translation/x, return
        translation.

        This will only return a property, and won't traverse into the parent node.  If this
        is a top-level property, raise KeyError.
        """
        parts = self.path.split('/')
        if len(parts) == 1:
            raise NodeNotFound(self, 'parent')
        path = '/'.join(parts[:-1])

        return self.node.get_property(path)

    @property
    def array_children(self):
        assert isinstance(self.value, list)
        for idx, child in enumerate(self.value):
            yield DSONProperty(self.node, child, self.path + '/~%i' % idx, self.source_node)

    @property
    def channel_children(self):
        """
        Iterate over the channels in a channel array.

        These arrays can be iterated with array_children, but this will give properties
        with channels URL paths, eg. ?translation/x, instead of array paths like ?translation/~0.
        """
        # Get all of the channels in this property.  If our node has an asset, include
        # the asset's channels too, since an instance can partially override an asset's
        # channels.
        assert isinstance(self.value, list)
        channels = {child['id'] for child in self.value}
        asset_prop = self.get_asset_property()
        if asset_prop is not None:
            channels.update(child['id'].value for child in asset_prop)

        for channel in sorted(list(channels)):
            yield self[channel]

    def get_asset_property(self):
        """
        If this is a property on an instance, return the corresponding property on
        the asset.  If this isn't an instance or the asset doesn't have this property,
        return None.
        """
        asset = self.node.asset
        if asset is None:
            return None
        return asset.get_property(self.path, None)

    def __iter__(self):
        if isinstance(self.value, list):
            for item in self.array_children:
                yield item
        else:
            raise RuntimeError('Iteration not supported on %s' % self.value.__class__)

    def load_url(self, default=_not_set):
        """
        Return the node referenced by this URL property.
        """
        # find_property may return a property on the underlying asset.  If that happens, it's
        # important that get_url also be called on the asset node, since URLs in assets are relative
        # to the asset file, not the instance.
        return self.source_node.get_url(self.value, default=default)

    def get_parsed_url(self):
        return DSONURL(self.value)

    def get_default(self):
        """
        Get the default value.

        In theory, this is "path/value".  However, while "value" is supposed to be the default value,
        it gets overwritten with the current value in instances.  If this is an instance, read "value"
        directly from the asset, to get the real default.

        Limits are not applied.
        """
        source = self.get_asset_property()
        if source is None:
            source = self

        return source.get_value('value')

    def apply_limit_to_value(self, value):
        """
        If this property has limits, return value clamped to the limits.  Otherwise, just
        return the value.
        """
        if not self.get_value('clamped'):
            return value

        min_value = self.get_value('min')
        max_value = self.get_value('max')
        if isinstance(value, list):
            return [min(max_value, max(min_value, v)) for v in value]
        else:
            return min(max_value, max(min_value, value))

    def get_value_with_default(self, apply_limits=False):
        """
        Return the value of "path/current_value".  If that doesn't exist, try "path/value".
        If neither exist, raise KeyError.

        This is a convenience function for material values.
        """
        result = self.get('current_value')
        if result is None:
            result = self['value']

        value = result.value

        if apply_limits:
            # Apply limits.  Some materials have out-of-range values stored in current_value for
            # some reason.
            value = self.apply_limit_to_value(value)

        return value

    # Convenience shortcuts:
    def evaluate(self, *args, **kwargs):
        import modifiers
        return modifiers.Helpers.evaluate(self, *args, **kwargs)

    def get_vec3(self, *args, **kwargs):
        import modifiers
        return modifiers.Helpers.get_vec3(self, *args, **kwargs)

class DSONNode(DSONBase):
    def __init__(self, env, node_source, node_data, url, dson_file=None, parent_node=None, root_node=None):
        # If root_node is None, then we're the root node.
        if root_node is None:
            root_node = self

        self.env = env
        self.dson_file = dson_file
        self._data = node_data
        self.node_source = node_source
        self.child_nodes = []
        self.url = url
        self.materials = {}
        self.root_node = root_node

        # If a Maya node is created for this node, its PyNode will be stored here.  We don't
        # use this internally, it's just storage for the user.
        self.maya_node = None

        # A list of DSONFormulas that affect properties on this node:
        self.formulas = {}

        # A list of DSONFormulas that use properties on this node as an input, eg. things
        # that depend on us.  Formulas add themself to this if they create a Maya input
        # connection to us; statically evaluating a formula with evaluate() won't add to
        # this.
        self.formulas_output = {}

        # The Maya attributes associated with each property path.
        self.property_maya_attributes = {}

        self.asset = None
        is_scene_node = root_node is not None and root_node == getattr(env, 'scene', None)

        # Scene nodes have a URL pointing at the library asset they're an instance of.
        #
        # When a modifier asset is loaded, it contains a small scene { modifier } entry.  This
        # instantiates the modifier with no changes.  Modifiers have a parent pointing to the
        # asset they're for, eg. /data/DAZ%203D/Genesis%203/Female/Genesis3Female.dsf#Genesis3Female,
        # so the modifier is parented under that node and adds the modifier to the asset.
        #
        # The user's Genesis3Female figure then shows the modifier, since the instance has that
        # asset as its parent.
        #
        # If the user adjusts the modifier, the modifier is instanced.  A new entry is added to
        # scene { modifier } in the user's scene .duf pointing at the same underlying modifier asset.
        # (The modifier added previously is now unused.)  However, this modifier instance has a parent
        # field, which parents the new modifier instance under the figure in the scene, instead of
        # under the modifier asset.
        asset_url = self._data.get('url')
        if asset_url:
            self.asset = self.get_url(asset_url)

        # Cache the node ID and type.  Not all nodes have a type.
        self.node_id = self.find_property_fast('id', search_asset=False)
        if node_source == 'node':
            self.node_type = self.find_property_fast('type', search_asset=True)
        else:
            self.node_type = None

        # Queue the parent nodes to load.  Note that parent fields aren't inherited from assets.
        # The asset itself is parented according to its own parent field, but if you instantiate
        # an asset you specify the parent node to put the instance in.  If you instantiate an asset
        # without specifying a parent, the asset is on the root (this happens with the scene list
        # in modifier .dsf files).
        parent_url = self._data.get('parent')

        if parent_url is not None:
            # If we have a parent field, remove it from the data.  We don't want instances of an
            # asset looking up the "parent" attribute to find the parent of the asset.
            del self._data['parent']

            # We don't expect to see a node that has both an implicit parent (like a
            # node's geometries list) also having a "parent" value.
            assert parent_node is None

            parent_node = self.get_url(parent_url)
            assert parent_node.is_instanced == self.is_instanced, (self, parent_node)

        if parent_node is None and root_node is not self:
            # If we don't have a parent, add ourself to the root.  This will also be None if
            # we're the root.
            parent_node = root_node

        # log.debug('Parent of %s: %s (%s)' % (self, parent_url, parent_node))
 
        # If we have a parent, add ourself to it.
        self.parent = parent_node
        if parent_node is not None:
            parent_node.child_nodes.append(self)

        # If this node has any geometries, add them.  Node types that have geometries include "figure"
        # and "node".
        if self.dson_file:
            self.dson_file._add_nodes(self._data.get('geometries', []), 'geometry', parent_node=self, root_node=root_node)

        if node_source == 'material':
            # This one's a bit odd: instances of materials don't parent themselves under the geometry
            # they're for, but instead have a "geometry" field pointing at it.  Add ourself to a
            # list of geometries on that node.
            geometry_url = self.get('geometry')
            if geometry_url:
                # Register this material for each of the group names in the "groups" list.
                geometry = self.get_url(geometry_url.value)
                assert geometry, self

                for group in self['groups'].array_children:
                    assert group.value not in geometry.materials
                    geometry.materials[group.value] = self

    def print_hierarchy(self, depth=0):
        log.debug('%s%s' % ('    '*depth, self))

        for child in self.children:
            child.print_hierarchy(depth+1)

    def get_url(self, url, default=_not_set):
        try:
            return self._get_url(url)
        except NodeNotFound as e:
            if default is not _not_set:
                return default
            raise e
        
    def _get_url(self, url, default=_not_set):
        """
        Return the node for a URL.  If the URL is relative, it will be resolved relative to
        the URL of this node.
        """
        if isinstance(url, DSONProperty):
            url = url.get_parsed_url()
        else:
            assert isinstance(url, basestring)

        parsed_url = DSONURL(url)
        
        if not parsed_url.scheme and not parsed_url.fragment:
            # Actual DSF files never have URLs that only refer to a file.  They always refer to
            # something specific inside the file, not the file itself.  Enforce this, so we don't
            # have to worry about this function returning something other than a DSONNode.  If you
            # want to load a file directly, call scene.get_or_load_file().
            raise RuntimeError('URL points to a file, not a resource: %s' % url)

        node = self

        if parsed_url.path:
            # If the URL specifies a file explicitly, retrieve it and continue resolving the
            # URL from there.
            node = self.env.get_or_load_file(parsed_url.path)

        if parsed_url.scheme:
            # Are schemes used by anything other than modifiers?
            assert False
            #node = self.root_node.find_node_id(parsed_url.scheme)
            #node = node.find_node_id(parsed_url.scheme)
            #if node is None:
            #    raise NodeNotFound(self, url, 'Referenced ID in scheme that isn\'t loaded: %s (context: %s)' % (url, self))

        if parsed_url.fragment:
            # If we're on a DSONNode, move up to the containing DSONFile before doing a fragment
            # search.
            if isinstance(node, DSONNode) and node.dson_file:
                node = node.dson_file
                
            node = node.find_node_id(parsed_url.fragment)
            if node is None:
                raise NodeNotFound(self, url, 'Referenced ID in fragment that isn\'t loaded: %s (context: %s)' % (url, self))

        if parsed_url.query:
            node = node.get_property(parsed_url.escaped_query)

        return node

    def _get_modifier_url(self, parsed_output_property, load_files=True):
        # If the URL includes a file, load it.
        #
        # Some modifiers include invalid paths.  Warn about this but keep going.
        if parsed_output_property.path and load_files:
            dson_file = self.env.get_or_load_file(parsed_output_property.path, allow_fail=True) 
            if dson_file is None:
                log.warning('Couldn\'t find file "%s"', parsed_output_property.path)

        parent_node = self
        if parsed_output_property.scheme:
            # Search the top-level node for the asset ID in the scheme.  This is only important in a
            # few cases.  For example, lCollar_CTRLMD_N_XRotate_n30.dsf and rCollar_CTRLMD_N_XRotate_n30.dsf
            # have the same asset name, so they have to specifically say "rCollar:#CTRLMD_N_XRotate_n30"
            # to indicate which one they mean.  If we just search for the fragment asset name, we'll
            # get the same modifier for both of these.
            parent_node = parent_node.breadth_first_search_for_instance_with_asset_id(parsed_output_property.scheme)

        # Find the target node inside the parent node.
        target_node = parent_node.breadth_first_search_for_instance_with_asset_id(parsed_output_property.fragment)

        # If this points at an alias, resolve the reference.
        if target_node.get_value('channel/type') == 'alias':
            target_channel_url = target_node['channel/target_channel'].get_parsed_url()
            return target_node._get_modifier_url(target_channel_url, load_files=load_files)

        if not parsed_output_property.escaped_query:
            return target_node

        # Resolve the property.
        return target_node.get_property(parsed_output_property.escaped_query)

    def get_modifier_url(self, url, load_files=True):
        """
        Look up a URL from a modifier reference.

        Modifier URLs search starting at the top node.  For example, a SkinCluster parented
        inside geometry finds joint URLs: we first search up for the top node (the figure
        above the geometry), then down within the figure to find the joint.

        There may be a way to merge get_url() and get_modifier_url().
        """
        if isinstance(url, DSONProperty):
            url = url.get_parsed_url()
        else:
            assert isinstance(url, basestring)

        target_node = self.find_top_node()
        return target_node._get_modifier_url(url, load_files=load_files)

    @property
    def asset_id(self):
        if self.asset is not None:
            return self.asset.node_id
        else:
            return self.node_id

    @property
    def children(self):
        return iter(self.child_nodes)

    @property
    def is_instanced(self):
        """
        Return true if this is an instance, and not an asset template (eg. this node is
        in the scene).
        """
        return self.root_node is self.env.scene

    @property
    def asset_url(self):
        if self.asset is not None:
            return self.asset.url
        else:
            return self.url

    @property
    def is_modifier(self):
        """
        Return true if this node is a modifier.
        """
        return self.node_source == 'modifier'
        #return self.node_type == 'modifier'

    def create_instance_for_asset(self, parent):
        """
        Create an instance from an asset.

        The instance will be created and added to the scene as if it had been present in
        the file the parent came from.
        """
        # We usually aren't trying to create a new instance from a node that's already
        # an instance.
        assert not self.is_instanced

        node_id = parent.env.get_unique_id(self.node_id)
        instance_node = {
            'id': node_id,

            # 'url' specifies the asset for the instance, which is this node.
            "url" : self.url,
        }

        return DSONNode(self.env, self.node_source, instance_node, urllib.quote(parent.dson_file.path) + '#' + urllib.quote(node_id),
            dson_file=parent.dson_file, parent_node=parent, root_node=parent.root_node)

    def get_children_of_type(self, node_type):
        for child in self.children:
            if child.node_type != node_type:
                continue
            yield child

    def find_node_id(self, node_id):
        """
        Find a node by ID, searching under this node recursively.
        """
        assert node_id is not None

        if node_id == self.node_id:
            return self

        for child in self.children:
            result = child.find_node_id(node_id)
            if result:
                return result

        return None

    def delete(self):
        """
        Remove this ndoe from its parent.
        """
        self.parent.child_nodes.remove(self)

        assert self.dson_file.nodes[self.node_id] is self
        del self.dson_file.nodes[self.node_id]

        self.parent = None
        self.dson_file = None

    @property
    def is_root(self):
        """
        Return true if this node is the root of its hierarchy, eg. the scene itself or the
        root of the library.
        """
        return self.url == '/'

    @property
    def file_asset_type(self):
        """
        Return the asset type registered in the underlying asset's asset_info.
        """
        if self.asset:
            return self.asset.dson_file.asset_info['type']
        else:
            return self.dson_file.asset_info['type']
        
    def get_property(self, path, default=_not_set):
        parts = path.split('/')
        parts = [urllib.unquote(part) for part in parts]

        # Check this node for the property.  If we can't find it and we have a base asset,
        # check that too.
        try:
            return self._get_property_in_self_from_path(path, parts, self)
        except KeyError:
            try:
                if self.asset is None:
                    raise
                return self.asset._get_property_in_self_from_path(path, parts, self)
            except KeyError:
                if default is not _not_set:
                    return default
                raise

    def _get_property_in_self_from_path(self, path, parts, instance):
        """
        Search for a property by name in this node.

        This doesn't recurse into base nodes.
        """
        assert parts

        # A property query is a slash-separated list of field names.  For example,
        #
        # first/second/third
        #
        # returns obj.first.second.third.
        #
        # If obj.first is an array, we check for channels in the array.  A channel is a dictionary
        # containing a "channel" dictionary, which has an "id" used as the name.
        #
        # If a dictionary contains a key "channel", its id is also checked.
        node = self._data

        if parts and parts[0] == 'extra-type':
            assert len(parts) >= 2
            wanted_extra = parts[1]

            # The type field of extras can contain slashes, so unescape lookups to allow
            # searching for those.
            wanted_extra = urllib.unquote(wanted_extra)

            parts = parts[2:]
            try:
                extras = node['extra']
            except KeyError:
                raise NodeNotFound(self, path)

            for extra in extras:
                if extra.get('type') == wanted_extra:
                    node = extra
                    break
            else:
                raise NodeNotFound(self, path)

        for part in parts:
            if part.startswith('~'):
                # This is an array element (extension).
                idx = int(part[1:])
                assert isinstance(node, list)
                node = node[idx]
            elif isinstance(node, dict):
                if part in node:
                    node = node.get(part)
                elif isinstance(node.get('channel'), dict) and node['channel'].get('id') == part:
                    node = node['channel']
                else:
                    raise NodeNotFound(self, path)
            elif isinstance(node, list):
                def find_channel():
                    for channel in node:
                        node_type = channel.get('type')
                        if node_type == part:
                            return channel

                        if 'channel' in channel and isinstance(channel['channel'], dict):
                            channel = channel.get('channel')

                        if channel.get('id') == part:
                            return channel

                node = find_channel()
            else:
                raise RuntimeError('Unknown property path type %s on %s' % (node.__class__, path))

            if node is None:
                raise NodeNotFound(self, path)
        return DSONProperty(instance, node, path, self)

    def find_property_fast(self, name, search_asset=True):
        """
        Find a property by name.

        This doesn't search property paths or unescape the name.  If search_asset
        is false, it also doesn't check the base asset.  This returns the raw value,
        rather than a DSONProperty wrapper.

        If the property isn't found, raises KeyError.
        """
        try:
            return self._data[name]
        except KeyError:
            if search_asset and self.asset is not None:
                return self.asset._data[name]
            raise

    @property
    def node_name(self):
        # Not all nodes have names.
        return self.get('name')

    def __repr__(self):
        s = 'DSONNode(%s:%s' % (self.node_source or self.node_type, self.node_id)

        # Only include the file if this is from a library node.
        if not self.is_instanced:
            s += ' in %s' % self.dson_file

        s += ')'
        
        return s

    def __hash__(self):
        return hash(self.node_id)

    def __deepcopy__(self, memo):
        """
        DSONNodes are never deep copied.

        If we deep copy a dictionary containing references to DSONNodes, we want the copy
        to contain the same DSONNode as the source.
        """
        return self

    @property
    def first_geometry(self):
        """
        Return the first geometry node which is an immediate child of this one.
        """
        for geometry in self.children:
            if geometry.node_source == 'geometry':
                return geometry
        return None

    def breadth_first(self, only_node_type=None):
        """
        Yield all nodes and their parent node recursively, in breadth-first order.

        If only_node_type is set, only yield and traverse into nodes of this type.
        """
        queue = [self]
        while queue:
            node = queue.pop(0)
            if only_node_type is not None and node.node_type != only_node_type:
                continue

            skip_subtree = yield node
            if skip_subtree:
                continue

            queue.extend(node.children)

    def depth_first(self):
        """
        Yield all nodes and their parent node recursively, in depth-first order.
        """
        yield self

        for child in self.children:
            for child2 in child.depth_first():
                yield child2

    def has_modifier_dependants(self, property_paths=None):
        """
        Return true if modifiers are reading or writing properties in this node.

        If property_paths is an array of property paths, return true if any property in
        the list is used by modifiers.  For example:

        has_modifier_dependants(('rotation/x', 'rotation/y')

        If it's None, return true if any modifiers use properties on this node.
        """
        if property_paths is not None:
            return any(path in self.formulas or path in self.formulas_output for path in property_paths)

        if self.formulas or self.formulas_output:
            log.debug('%s: %s, %s', self, self.formulas, self.formulas_output)
            return True
        else:
            return False

    def _get_nodes_within_figure(self):
        """
        Yield all nodes underneath (and including) this one, stopping if we reach a
        top node, such as a different figure.
        """
        # XXX: ugly
        first = True
        iterator = self.breadth_first()
        try:
            child = next(iterator)
            while True:
                skip_tree = False
                if not first and child.is_top_node:
                    skip_tree = True
                else:
                    yield child

                child = iterator.send(skip_tree)
                first = False
        except StopIteration:
            pass

    def find_descendant_with_name(self, name, default=_not_set):
        for child in self._get_nodes_within_figure():
            if child.get_value('name', None) == name:
                return child
            if name in child.get_value('name_aliases', []):
                return child

        if default is _not_set:
            raise NodeNotFound(self, str(name))
        else:
            return default
        
    def find_asset_name(self, asset_name, default=_not_set):
        # It doesn't make sense to search for an instance inside an asset.
        assert self.is_instanced

        for child in self._get_nodes_within_figure():
            if not child.asset:
                continue
            if child.asset.get_value('name', None) == asset_name:
                return child
            if asset_name in child.asset.get_value('name_aliases', []):
                return child

        if default is _not_set:
            raise NodeNotFound(self, str(asset_name))
        else:
            return default

    def breadth_first_search_for_instance_with_asset_id(self, asset_id):
        """
        Search inside this node for a node whose asset's node ID is asset_id.
        """
        # It doesn't make sense to search for an instance inside an asset.
        assert self.is_instanced

        for child in self._get_nodes_within_figure():
            if child.asset and child.asset.node_id == asset_id:
                return child

        raise NodeNotFound(self, str(asset_id))

    @property
    def is_top_node(self):
        return self.node_type not in ('bone', ) and self.node_source not in ('geometry', 'modifier')

    @property
    def is_graft(self):
        """
        Return true if this node is a graft geometry.
        """
        # It's possible for a node that isn't a graft to contain an empty "graft" dictionary.
        return 'graft/hidden_polys/values' in self

    def find_top_node(self):
        """
        Search up through our parents to find a top-level node, which is usually a figure.
        """
        node = self
        while node.parent and not node.is_top_node:
            node = node.parent
        assert node.is_top_node
        return node

    def find_root_joint(self):
        """
        Return the root joint of this figure's skeleton hierarchy.  If this figure doesn't
        have a skeleton, return None.
        """
        root = self.find_top_node()
        for child in root.children:
            while child.node_type == 'bone':
                return child
        return None

    def get_conform_target(self):
        """
        If this is a modifier with a conform_target, return the node conform_target
        points to.
        """
        top_node = self.find_top_node()

        conform_target = top_node.get('conform_target')
        if conform_target is None:
            return None

        # Find the node conform_target points to.
        return conform_target.load_url()

class DSONFile(object):
    # DSON uses URLs to identify nodes, but there's no way to know what type of resource something is from
    # its URL or its contents.  You have to know where it came from, eg. whether it's a node or modifier.
    def __init__(self, env, path, data):
        """
        path is the relative path within the DSON search path, or the filename if
        this is a scene loaded from outside the path.
        """
        self.env = env
        self.nodes = {}
        self.path = path

        self._data = data

        self.asset_info = self._data['asset_info']

        # Add all of the nodes in this file.  Note that the DSONFile node isn't the parent
        # of these nodes, it's just the file that they came from.  If a node has a parent, it'll
        # be another DSONNode.
        self.asset_type = self.asset_info['type']

        # Load library nodes.  These aren't actually in the scene, and can only be referenced
        # as a #fragment local to the file, or by including the path in the URL.  We won't find
        # these by searching the scene.  Note that the order that we load these is significant.
        self._add_nodes(self._data.get('uv_set_library', []), 'geometry', root_node=env.library)
        self._add_nodes(self._data.get('geometry_library', []), 'geometry', root_node=env.library)
        self._add_nodes(self._data.get('node_library', []), 'node', root_node=env.library)
        self._add_nodes(self._data.get('modifier_library', []), 'modifier', root_node=env.library)
        self._add_nodes(self._data.get('material_library', []), 'material', root_node=env.library)

        scene_node = self._data.get('scene', {})
        self._add_nodes(scene_node.get('nodes', []), 'node', root_node=env.scene)
        self._add_nodes(scene_node.get('modifiers', []), 'modifier', root_node=env.scene)
        self._add_nodes(scene_node.get('materials', []), 'material', root_node=env.scene)

    def __repr__(self):
        return '%s(%s)' % (self.asset_type, self.path)

    def print_hierarchy(self, depth=0):
        log.debug('%s%s' % ('    '*depth, self))

        for node in self.nodes.values():
            node.print_hierarchy(depth+1)

#    # DSONFiles are always primary nodes.
#    @property
#    def primary_node(self):
#        return self
#
#    @property
#    def is_primary_node(self):
#        return True

    def find_node_id(self, node_id):
        """
        Find a node by ID, searching under this node recursively.
        """
        for child in self.nodes.itervalues():
            result = child.find_node_id(node_id)
            if result:
                return result

        return None

    # XXX: This is ugly.  It's only for self.nodes, which is used to search for nodes inside
    # this file.  Can we invalidate self.nodes when new nodes are created or deleted, and
    # recreate it by traversing the environment and looking for node.dson_file is self?
    def _add_nodes(self, nodes, node_source, root_node, parent_node=None):
        for node in nodes:
            self.add_node(node, node_source, root_node, parent_node)

    def add_node(self, node, node_source, root_node, parent_node=None):
        node_id = node['id']

        # Each modifier asset instantiates the modifier into the scene.  We don't want this, so
        # ignore these.
        is_scene_node = root_node is not None and root_node == getattr(self.env, 'scene', None)
        if node_source == 'modifier' and is_scene_node and node.get('parent') is None:
            # log.debug('--> ignoring modifier instance with no parent', node)
            return None

        dson_node = DSONNode(self.env, node_source, node, urllib.quote(self.path) + '#' + urllib.quote(node_id), dson_file=self,
                parent_node=parent_node, root_node=root_node)
        if node_id in self.nodes:
            raise RuntimeError('Duplicate node ID \"%s\"' % node_id)

        self.nodes[node_id] = dson_node
        return dson_node

class DSONEnv(object):
    """
    This holds the overall environment, including a scene and libraries.  All nodes
    and files point back here.
    """
    _sections = ()
    def __init__(self):
        self._search_path = self._load_search_paths()
        self._next_internal_file = 0

        # A mapping from filenames to DSONFiles.  This is used to prevent loading files twice.  Filenames
        # are lowercased in these keys, for case-insensitivity.  To get the filename in the case it had when
        # we loaded it, use DSONFile.path.
        self.files = {}

        # This is assigned a function to decide whether DSONProperties should be
        # rigged dynamically.
        self.is_property_dynamic_handler = None

        # Create a root node for the scene, and one for each library type.
        for root in ('scene', 'library'):
            root_node = DSONNode(self, root, {
                'id': root,
                'type': root,
            }, '/')
            setattr(self, root, root_node)
 
    def add_to_search_path(self, path):
        """
        Add path to the beginning of the search path used for DSON files and other
        resources referenced from them.
        """
        self._search_path[0:0] = [path]

    def load_user_scene(self, absolute_path, allow_fail=False):
        if absolute_path.lower() in self.files:
            return self.files[absolute_path.lower()]

        result = self._load_file(absolute_path, absolute_path, allow_fail=allow_fail)
        self.files[absolute_path.lower()] = result
        return result

    def get_or_load_file(self, relative_path, allow_fail=False):
        """
        Find and return the given DUF/DSF file, loading it into the environment if necessary.

        If the file isn't found, return None if allow_fail is True, otherwise raise an exception.
        """
        # Some files have URLs with paths missing the initial slash.  Don't add this if we're
        # loading an absolute Windows path.
        if not relative_path.startswith('/') and relative_path[1:2] != ':':
            relative_path = '/' + relative_path

        if relative_path.lower() in self.files:
            return self.files[relative_path.lower()]

        try:
            relative_path, absolute_path = self.find_file(relative_path)
        except PathNotFound as e:
            if allow_fail:
                log.info('Couldn\'t find resource: %s' % relative_path)
                return None
            raise
            
        result = self._load_file(relative_path, absolute_path, allow_fail=False)
        self.files[relative_path.lower()] = result
        return result

    def create_generated_file(self):
        """
        Create an empty DSONFile with an internal path, add it to the environment and return it.

        This is used to hold generated nodes.
        """
        # Find the next unused internal filename.
        relative_path = '/~internal/%i.dsf' % self._next_internal_file
        self._next_internal_file += 1
        data = {
            'asset_info': {
                'type': 'scene',
            },
        }
        result = DSONFile(self, relative_path, data)
        self.files[relative_path.lower()] = result
        return result

    def _load_file(self, relative_path, absolute_path, allow_fail=False):
        try:
            log.info('Loading DSONFile: %s' % absolute_path)
            data = self.read_file(absolute_path)
            data = json.loads(data)
        except PathNotFound as e:
            if allow_fail:
                log.info('Couldn\'t find resource: %s' % absolute_path)
                return None
            raise

        result = DSONFile(self, relative_path, data)

        return result

    def find_file(self, relative_path):
        assert relative_path.startswith('/'), relative_path

        for search in self._search_path:
            attempted_path = '%s%s' % (search, relative_path)

            if os.access(attempted_path, os.R_OK):
                normalized_absolute_path, normalized_relative_path = util.normalize_filename_and_relative_path(attempted_path, relative_path)
                return normalized_relative_path, normalized_absolute_path

        raise PathNotFound('Couldn\'t find file: %s' % relative_path)

    def read_file(cls, path):
        return Helpers.open_file(path).read()

    def find_all_files(self, patterns=None):
        """
        Yield all files in the search path.
        
        If absolute_path is false, return a path relative to the search path that can
        be found with find_file.  If false, return an absolute path.

        If patterns is a list of strings, filter the list with fnmatch.
        """
        for search in self._search_path:
            for root, dirs, files in util.scandir_walk(search):
                root = root.replace('\\', '/')
                assert root.startswith(search)
                relative_root = root[len(search):]
                for entry in files:
                    if patterns is not None:
                        if not any(fnmatch(entry.name, pattern) for pattern in patterns):
                            continue
                    absolute = '%s/%s' % (root, entry.name.replace('\\', '/'))
                    relative = '%s/%s' % (relative_root, entry.name.replace('\\', '/'))
                    yield entry, relative, absolute

    @classmethod
    def _load_search_paths(cls):
        """
        Return the DSON search path.

        This is only implemented on Windows.
        """
        try:
            import _winreg
        except ImportError as e:
            return []

        # Not the most shining example of a Python API.
        result = []
        with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, r'SOFTWARE\DAZ\Studio4') as key:
            for base_key in ('ContentDir', 'PoserDir'):
                idx = 0
                while True:
                    try:
                        path, _ = _winreg.QueryValueEx(key, '%s%i' % (base_key, idx))
                        log.debug(path)
                        result.append(path)
                    except WindowsError as e:
                        break
                
                    idx += 1
        return result

    def print_hierarchy(self, depth=0):
        log.debug('%s%s' % ('    '*depth, self))

        log.debug('--- Library')
        self.library.print_hierarchy(depth+1)

        log.debug('--- Scene')
        self.scene.print_hierarchy(depth+1)

    def depth_first(self):
        for node in self.scene.depth_first():
            yield node
        for node in self.library.depth_first():
            yield node

    def get_unique_id(self, base_id):
        """
        Generate a new ID from base_id which is unique in this environment.

        base_id is a node ID, like "character" or "character-1".  If the ID has a numeric suffix
        like "character-1", we'll remove it.
        """
        def split_id(node_id):
            m = re.match('(.*)-(\d+)$', node_id)
            if not m:
                return node_id, 0
            base_id = m.group(1)
            return m.group(1), int(m.group(2))

        base_id = split_id(base_id)[0]
        base_id += '-'

        # Search through all IDs in this environment that start with base_id, and find the
        # greatest numeric suffix among them.
        ids = set()
        greatest_suffix = [0]
        def add(node_id):
            if not node_id.startswith(base_id):
                return
            node_id, idx = split_id(node_id)
            greatest_suffix[0] = max(greatest_suffix[0], idx)

        for node in self.scene.breadth_first():
            add(node.node_id)
        for node in self.library.breadth_first():
            add(node.node_id)

        return '%s%i' % (base_id, greatest_suffix[0]+1)

    def is_property_dynamic(self, dson_property):
        """
        Return true if dson_property is dynamic.

        By default, returns true for all properties.  To set a handler to decide which properties
        are dynamic, see set_is_property_dynamic_handler.
        """
        if self.is_property_dynamic_handler is None:
            return True
        return self.is_property_dynamic_handler(dson_property)


    def set_is_property_dynamic_handler(self, handler):
        self.is_property_dynamic_handler = handler

class Helpers(object):
    @classmethod
    def open_file(cls, path):
        # DSF files can be compressed or not.  There's no way to tell other than reading the file and checking
        # for the GZIP magic.
        f = open(path, 'rb')
        data = f.read(2)
        f.seek(0)
        if data == '\x1f\x8b':
            f = gzip.GzipFile(fileobj=f)
        return f

    @classmethod
    def recursively_instance_assets(cls, node):
        """
        When an instance is created from an asset, its children are inherited as well.  These are
        explicitly instanced if thre are changed properties, but if nothing is changed, the asset's
        child is used implicitly.

        Recursively reference all inherited assets in the scene.  This is important for modifiers,
        so we always have a DSONNode representation of all modifiers specific to the node the
        modifier is applied to, even if the modifier isn't instanced in the file.
        """
        if node.asset is not None:
            # Look through the asset's children for nodes that we don't have an instance of.
            child_assets = {c.asset for c in node.child_nodes}
            for asset_child in node.asset.child_nodes:
                if asset_child in child_assets:
                    # We have an instance for this already.
                    continue

                asset_child.create_instance_for_asset(node)

        for child in node.child_nodes:
            cls.recursively_instance_assets(child)

    @classmethod
    def create_fid_modifiers(cls, env):
        """
        Figures have an implicit modifier named "FID_FigureName", with a value of 1.  This is used
        by modifier formulas and auto-follow.
        """
        for dson_node in env.scene.depth_first():
            if not dson_node.is_top_node:
                continue
            if dson_node.node_type != 'figure':
                continue
            asset_name = dson_node.asset.get_value('name')
            node_name = 'FID_%s' % asset_name
            log.debug('Creating %s for node %s', node_name, dson_node)

            # Create both an asset and an instance of the asset, so this modifier works like real ones.
            # Put this in its own file, so there won't be ID clashes.  Give the instance an ID and name
            # that are both the name of the figure, so matching it with a URL is easy.
            dson_file = env.create_generated_file()
            
            asset_node = {
                'id': node_name + '-1',
                'name': node_name,
                'parent': dson_node.asset.url,
                'group': 'Figure ID',
                'channel': {
                    'id': 'value',
                    'name': 'value',
                    'type': 'float',
                    'value': 1,
                    'min': 1,
                    'max': 1,
                    'clamped': True,
                }
            }

            asset = dson_file.add_node(asset_node, 'modifier', root_node=env.library)

            instance_node = {
                'id': node_name,
                'name': node_name,
                'url': asset.url,
                'parent': dson_node.url,
            }

            node = dson_file.add_node(instance_node, 'modifier', root_node=env.scene)
            
    @classmethod
    def find_bones(cls, node):
        """
        Yield all joints in the node.

        This will traverse into child joints, but not into other sub-hierarchies.
        """
        for child in node.children:
            for joint in child.breadth_first(only_node_type='bone'):
                yield joint

    @classmethod
    def find_ancestor_bone_by_asset_id(cls, node, asset_id):
        while node.node_type == 'bone':
            if node.asset_id == asset_id:
                return node
            node = node.parent

        return None


