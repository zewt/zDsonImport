import copy, logging, json, os, time, urllib
from pprint import pprint, pformat
from collections import defaultdict
import DSON, modifiers
from DSONURL import DSONURL
from dsonimport import util

log = logging.getLogger('DSONImporter')

# DSON search paths can have a lot of content.  There's no one place that defines
# which resources depend on which, or an easy way of finding out what we should load.
#
# If a modifier is configured as non-dynamic and not instanced in the scene explicitly,
# then we try to figure out in advance whether we need to load it.  There are many
# modifiers that generally aren't instanced that we need to load, like corrective morphs.
# However, as each character can have its own set of corrective morphs and they can
# be large, we don't want to just load all of them.
#
# Correctives have a mult stage formula that multiplies the corrective by the value of
# the main character morph.  This turns them off when the character isn't used.  This
# happens recursively: modifiers may depend on other modifiers which are weighted off
# in this way.  We'll cache which assets are a factor in a mult stage, so we can determine
# this quickly.
#
# All modifiers are parented either to the geometry or transform of the figure they affect,
# and we're always querying within the context of a particular figure.
#
# "Posing" and "shaping" morphs are only distinguished by the presentation/type property
# on the modifier.  This should be "Modifier/Shape" or "Modifier/Pose".  We treat these
# the same functionally, but we can use this to give better defaults: pose modifiers should
# be dynamic by default, and shape modifiers should be non-dynamic.
#
# We need to be able to determine a few things statically:
#
# - For non-dynamic modifiers with no formulas, we only want to load it by default if it has
# a nonzero value, either on the asset or the instance.
# - For non-dynamic modifiers with formulas, we should load them by default unless one of
# their formulas multiplies by another modifier which is zero.  For example, a corrective
# morph should only be loaded if the character it's for is enabled.  This is recursive, since
# modifiers can depend on other modifiers.
#
# Given a loaded DSONNode figure, return a list of modifiers in the search path which can
# ever possibly be used, and basic information about the modifier like its name and type.
# This can be used to present a UI to the user, to edit the list further, and import the
# files.

class AssetConfig(object):
    def __init__(self):
        self._dynamic_modifiers = {}
        self._modifiers_with_external_inputs = set()

    # Dynamic properties configuration
    #
    # Dynamic properties will be rigged, so (where supported) they can be changed dynamically
    # in the resulting scene.
    def set_dynamic(self, modifier_info, value):
        """
        Override a modifier to be dynamic or non-dynamic.  If value is None, remove any
        override.
        """
        assert isinstance(modifier_info, ModifierAsset)
        self._dynamic_modifiers[modifier_info.asset_url] = value

    def get_dynamic(self, modifier_info):
        """
        Return the dynamic override for a modifier.  If the modifier isn't overridden, return
        None.
        """
        assert isinstance(modifier_info, ModifierAsset)
        return self._dynamic_modifiers.get(modifier_info.asset_url, False)

    @classmethod
    def is_figure_morph(cls, modifier_info):
        """
        Return true if this seems to be a figure modifier.

        These are usually not dynamic, since we don't support dynamic skeleton alignments.
        """
        assert isinstance(modifier_info, ModifierAsset)
        
        if modifier_info.data['presentation_type'] in ('Modifier/Clone', ):
            return True
        
        if modifier_info.data['presentation_type'] == 'Modifier/Shape':
            return modifier_info.data['channel'].get('visible', True)

        return False

    @classmethod
    def is_corrective_morph(cls, modifier_info):
        # Shape modifiers are usually not dynamic, like character morphs.  However, corrective morphs
        # can also be marked "shape".  Try to distinguish these by whether they're visible or not.  Corrective
        # morphs are usually hidden.
        assert isinstance(modifier_info, ModifierAsset)
        if modifier_info.data['presentation_type'] == 'Modifier/Corrective':
            return True
        if modifier_info.data['presentation_type'] != 'Modifier/Shape':
            return False
        return not modifier_info.data['channel'].get('visible', True)

    def set_modifiers_with_external_inputs(self, modifier_urls):
        self._modifiers_with_external_inputs = set(modifier_urls)

    def channel_has_external_inputs(self, modifier_info):
        """
        Return true if this modifier will have an external input, and should be treated as
        dynamic even if it doesn't have dynamic inputs from modifiers.

        To configure additional properties as having external inputs, see set_modifiers_with_external_inputs.
        """
        assert isinstance(modifier_info, ModifierAsset)

        # If the channel is visible, we'll create a user control for it.
        if modifier_info.data['channel'].get('visible', True):
            return True

        # Check if the channel is in the list set by set_modifiers_with_external_inputs.
        if modifier_info.asset_url in self._modifiers_with_external_inputs:
            return True

        return False

# This is something that a modifier input or output can reference: either an instanced
# modifier, an uninstanced modifier, or something that isn't a modifier.
class ModifierReferenceBase(object):
    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other

class NonModifierReference(ModifierReferenceBase):
    def __init__(self, url):
        self._url = url

    @property
    def asset_url(self):
        # This points to an input to a modifier that isn't a modifier, such as a joint.
        # It doesn't refer to a modifier asset.
        return None

    def __repr__(self):
        return 'NonModifierReference(%s)' % (self._url)

class ModifierAsset(ModifierReferenceBase):
    def __init__(self, data, file_info):
        self.data = data
        self.file_info = file_info

    @property
    def asset_url(self):
        return self.data['url']

    def __repr__(self):
        return 'ModifierAsset(%s)' % (self.data['id'])

    @property
    def absolute_path(self):
        """
        Return the absolute path on the filesystem to the file containing this modifier.
        """
        return self.file_info['absolute_path']

    @property
    def relative_path(self):
        """
        Return the path within the DSON search path to the file containing this modifier.
        """
        return self.file_info['relative_path']

    def substring_matches(self, string):
        """
        Return true if string matches this modifier.

        This matches the label, ID and name, for flexible UI searching.
        """
        string = string.lower()
        label = self.data.get('presentation_label')
        if label is not None and string in label.lower():
            return True

        label = self.data['channel'].get('label')
        if label is not None and string in label.lower():
            return True

        if string in self.data['id'].lower():
            return True

        return False

    @property
    def label(self):
        label = self.data.get('presentation_label')
        if label:
            return label

        label = self.data['channel'].get('label')
        if label:
            return label

        return self.data['id']

    @property
    def group(self):
        return self.data.get('group', '')

class AssetCache(object):
    """
    This is a simple helper for finding resources like morphs that apply to an asset.
    This uses the DSON module to find files, but doesn't actually import data into
    an environment, since we don't need the full parsing functionality and since we
    may be parsing a lot of data, it's faster to just access what we need.
    """
    def __init__(self, cache_file):
        self.cache_file = cache_file
        self._info_per_file = {}
        self._load()

    def _load(self):
        try:
            data = open(self.cache_file).read()
            _cache_data = json.loads(data)
            self._info_per_file = _cache_data.get('info_per_file', {})
        except IOError:
            return

    def get_resolver(self, env):
        dson_nodes = []
        # Find all figures in the scene.
        for node in env.scene.depth_first():
            if node.node_source != 'node' or node.node_type not in ('figure', 'node'):
                continue
            dson_nodes.append(node)
        
        return AssetCacheResolver(dson_nodes, self._info_per_file)

    def _save(self):
        data = json.dumps({
            'info_per_file': self._info_per_file,
        }, indent=4)
        data += '\n'
        open(self.cache_file, 'w').write(data)

    @classmethod
    def _get_path_from_url(cls, url):
        path = DSONURL(url).path
        if not path:
            return None
        return path

    @classmethod
    def _get_asset_info_fast(cls, dson_file):
        """
        Read and return the asset info from an open DSON file.
        """
        # This is a bit of a hack.  The asset_info is always the first key in the dictionary.
        # We can read some data at the beginning and parse just that, and use object_hook to
        # get it even though parsing the file as a whole will fail.  This lets us get the
        # asset info for a file without having to read and parse the whole thing, which makes
        # a big difference since we don't parse a bunch of geometry data when we don't need
        # it.
        pos = dson_file.tell()
        data = dson_file.read(1024*4)
        dson_file.seek(pos)

        asset_info = [None]
        def object_hook(obj):
            # We'll get dictionaries inside asset_info before asset_info itself, eg. "contributor".
            if 'revision' not in obj:
                return obj

            # We'll get a couple other dictionaries 
            if asset_info[0] is None:
                asset_info[0] = obj
            return obj

        try:
            json_data = json.loads(data, object_hook=object_hook)
        except ValueError:
            pass

        return asset_info[0]

    def _scan_asset(self, entry, relative_path, absolute_path):
        """
        Update the cache for a single asset.

        The cache file won't be saved.
        """
        asset_data = self._info_per_file.get(relative_path)

        # If there's already a cache entry for this file, see if the file time is the same.
        mtime = entry.stat().st_mtime
        if asset_data is not None:
            if mtime == asset_data['last_mtime']:
                return False

        # Note that we do write entries for all files, so we can short circuit them above
        # when they haven't changed.

        # Check the asset info to see if this is a file we're interested in.
        dson_file = DSON.Helpers.open_file(absolute_path)

        modifier_info = {}

        def scan_modifier():
            # We only do a full scan on modifiers.  Note that modifiers can appear in files with
            # other asset types, but those appear to only be SkinBindings and DzMeshSmoothModifier.
            # We don't need to index those.
            asset_info = self._get_asset_info_fast(dson_file)
            if asset_info is not None and asset_info.get('type', '') != 'modifier':
                return

            # Read the full file.
            data = dson_file.read()
            json_data = json.loads(data)

            # Check again, in case the fast path check above failed.
            if json_data['asset_info']['type'] != 'modifier':
                return

            # Find modifiers and the asset they apply to.  We only care about the parent of the modifier.
            modifiers = json_data.get('modifier_library', [])
            for modifier in modifiers:
                info = self.cache_modifier_info(relative_path, modifier)
                if info is None:
                    continue

                # Our top-level key is the path.  Store the modifier info using its ID and channel name,
                # eg. "modifier?value", so concatenating the two gives the URL for the modifier's channel,
                # eg. "/path#modifier?value".
                modifier_info[info['channel_path']] = info

        scan_modifier()

        # Hack: If we're in Cygwin, convert the path to a Windows path.  Don't call cygpath, since
        # it would be very slow to call it for each file and we're not in a good place to batch it.
        if absolute_path.startswith('/cygdrive/'):
            # /cygdrive/c/path -> C:/path
            parts = absolute_path.split('/')
            assert parts[0] == ''
            assert parts[1] == 'cygdrive'
            absolute_path = parts[2].upper() + ':/' + '/'.join(parts[3:])

        asset_data = {
            'modifiers': modifier_info,
            'last_mtime': mtime,
            'relative_path': relative_path,
            'absolute_path': absolute_path,
        }
        self._info_per_file[relative_path] = asset_data
        return True

    def cache_modifier_info(self, relative_path, modifier):
        parent_url = modifier['parent']
        parsed_url = DSONURL(parent_url)

        # Note that modifiers can have parent URLs with no path, but as far as I've seen that only
        # happens in modifiers inside non-modifier assets, like SkinBindings, which we don't index.
        assert parsed_url.path, (relative_path, parent_url)
        assert parsed_url.fragment, (relative_path, parent_url)

        # Modifier parent URLs always seem to be of the form "/path#id".
        parent_path = self._get_path_from_url(modifier['parent'])
#            modifier_url = '%s#%s' % (urllib.quote(relative_path), urllib.quote(modifier['id']))
#            modifier_info[modifier_url] = parent_path

        # The #id?property for this modifier, local to this file.  Prefixing the filename will
        # give the URL for the modifier's property.
        channel_path = '%s?%s' % (urllib.quote(modifier['id']), urllib.quote(modifier['channel']['id']))

        info = {
            'id': modifier['id'],
            'name': modifier.get('name'),

            'url': '%s#%s' % (urllib.quote(relative_path), urllib.quote(modifier['id'])),

            'channel_path': channel_path,
            'channel_url': '%s#%s' % (urllib.quote(relative_path), channel_path),
            
            # The top-level keys in this modifier, eg. "morph", "formulas".
            'keys': modifier.keys(),

            # eg. "/Pose Controls/Head/Eyes":
            'group': modifier.get('group', ''),

            # Used by channel_type "Modifier/Shape", eg. "Actor":
            'region': modifier.get('region'),

            # This can be None.
            'parent': modifier['parent'],

            'formulas': modifier.get('formulas', []),

            'channel': modifier['channel'],
            
            'presentation_type': modifier.get('presentation', {}).get('type', ''),
            'presentation_label': modifier.get('presentation', {}).get('label', ''),
        }

        if modifier['channel']['type'] == 'alias':
            info['target_channel'] = modifier['channel']['target_channel']

        # If there are formulas on this modifier, look for inputs to mult stage formulas.  We
        # currently assume that if a value is used at all in this stage, it's a dependency.  Most
        # of these are simply "PUSH value".
        #
        # The formulas on this node may output to formulas on any other node.  We still track the
        # formula on this node's entry, so it's easy to clean up if a file is removed.
        def _make_absolute_url(url):
            # Formula URLs are either local references, eg. "id1:#id2?value", where id1 seems to
            # always be the parent node and id2 is a modifier in this file, or a remote reference
            # with a pathname in it, eg. "id1:/data/path.dsf#id2?value".
            #
            # Return an absolute URL to the target path.
            #print url
            parsed_url = DSONURL(url)
            parsed_url.scheme = ''
            if parsed_url.path:
                # Return "/data#id?value".
                parsed_url.scheme = ''
            else:
                # Add our path.
                parsed_url.path = relative_path
            return str(parsed_url)
            
        modifier_dependencies = defaultdict(list)

        for formula in modifier.get('formulas', []):
            if formula.get('stage', 'sum') != 'mult':
                continue

            output_url = formula['output']
            output_url = _make_absolute_url(output_url)

            for op in formula.get('operations', []):
                input_url = op.get('url')
                if not input_url:
                    continue

                input_url = _make_absolute_url(input_url)
                # print input_url
                modifier_dependencies[output_url].append(input_url)

        modifier_dependencies = dict(modifier_dependencies)
        info['modifier_dependencies'] = modifier_dependencies
#        pprint(modifier_dependencies)

        return info

    def scan(self, env, progress):
        """
        Scan the search path in the given DSONEnv for modifiers, and update the cache.

        progress is an instance of ProgressWindow.
        """
        seen_paths = set()

        # Scan all DSF files in all search paths.
        any_changed = False
        all_files = list(env.find_all_files())

        # Filter the list, so we know how many files we'll have for the progress bar.
        all_files = [(entry, relative_path, absolute_path) for entry, relative_path, absolute_path in all_files if relative_path.endswith('.dsf')]

        for idx, (entry, relative_path, absolute_path) in enumerate(all_files):
            # Update the task to allow cancellation.  Only do this periodically, or updating the UI
            # will slow down the scan.
            if (idx % 100) == 0:
                progress.set_task_progress(os.path.basename(relative_path)[0:40], percent=float(idx) / len(all_files))

            seen_paths.add(relative_path)
            if self._scan_asset(entry, relative_path, absolute_path):
                any_changed = True

        # Remove files that are cached, but weren't found by the scan.
        cached_paths = set(self._info_per_file)
        removed_paths = cached_paths - seen_paths
        for key in removed_paths:
            del self._info_per_file[key]
            any_changed = True

        # Saving the file takes a bit of time, so only do it if something has actually changed.
        if any_changed:
            progress.set_task_progress('Saving cache', force=True)
            
            self._save()

    def get_modifiers_for_path(self, path):
        """
        Return the asset paths to all known modifiers that affect the given path.
        """
        result = []
        for modifier_path, asset_data in self._info_per_file.iteritems():
            for modifier_id, modifier_data in asset_data.get('modifiers', {}).iteritems():
                if modifier_data['parent'] == path:
                    result.append(modifier_path)

        result.sort()
        return result

class AssetCacheResults(object):
    # Modifiers can be switched between dynamic and non-dynamic via AssetConfig.  If that changes,
    # the list needs to be refreshed.
    def __init__(self):
        # All modifiers loaded from cache.
        self.all_modifiers = {}

        # unavailable_modifiers is a list of modifiers that will never be used with the current
        # configuration, because non-dynamic modifiers cause it to always be zero.  These can be
        # hidden.
        self.unavailable_modifiers = {}

        self.available_for_dynamic = {}

        # The current value of each modifier in the scene, with formulas applied.  This is the
        # value that will be used if a modifier is non-dynamic.  A value of zero on a non-dynamic
        # modifier will cause the modifier to be listed in unavailable_modifiers rather than
        # available_modifiers.
        self.modifier_static_values = {}

        self.unused_modifiers = {}
        self.used_modifiers = {}
 
    def is_zero(self, modifier_info):
        """
        Return true if the static value of this modifier is zero.

        If a modifier is static and zero, it's not used.
        """
        return abs(self.modifier_static_values[modifier_info]) < 0.0001

def make_asset_id(url):
    if isinstance(url, basestring):
        url = DSONURL(url)
    else:
        assert isinstance(url, DSONURL), url

    # Canonicalize the path and fragment by re-escaping it.
    path = urllib.quote(urllib.unquote(url.escaped_path))
    fragment = urllib.quote(urllib.unquote(url.escaped_fragment))
    return path.lower() + '#' + fragment


class AssetCacheResolver(object):
    def for_each_modifier(self):
        for instance_for_modifier, file_info in self._modifiers_on_instance.iteritems():
            for modifier_info in file_info.itervalues():
                yield instance_for_modifier, modifier_info

    def breadth_first_within_figure(self, node):
        """
        Yield all nodes and their parent node recursively, in breadth-first order.
        """
        queue = [node]
        while queue:
            node = queue.pop(0)

            yield node

            # If this is a modifier asset and not a DSONNode, we're done.
            if isinstance(node, ModifierAsset):
                continue

            modifier_assets = {}
            if node.asset is not None:
                modifier_assets = self.modifiers_per_parent_by_id.get(node, {})
            modifier_assets = dict(modifier_assets)

            # Queue the node's instanced children.
            for child in node.children:
                if child.is_top_node:
                    continue

                if child.asset is not None:
                    asset_id = make_asset_id(child.asset_url)
                    if asset_id in modifier_assets:
                        # This node has an instanced child for a modifier.  Remove the asset from modifier_assets,
                        # so we don't queue both the instance and the asset.
                        del modifier_assets[asset_id]
                
                queue.append(child)

            # Queue modifier assets that don't have an instance.
            for asset_child in modifier_assets.itervalues():
                queue.append(asset_child)

    def _make_current_info(self, dson_node, cached_info_per_file):
        # Find all node instances within this figure.  These are the nodes that modifiers can apply to.
        instances_by_asset_url = {}
        for node in dson_node._get_nodes_within_figure():
            if node.asset is None:
                continue
            
            # Is there any case where the same asset can be instanced multiple times on the same figure?
            assert node.asset.url not in instances_by_asset_url

            instances_by_asset_url[make_asset_id(node.asset.url)] = node

        # Get all modifiers that can apply to the target figure or any of its children, mapped to the
        # instance they could apply to.
        for path, file_info in cached_info_per_file.iteritems():
            for modifier_raw_info in file_info['modifiers'].itervalues():
                parent_url = modifier_raw_info['parent']
                instance = instances_by_asset_url.get(make_asset_id(parent_url))
                if instance is None:
                    # There's no instanced node for this modifier to apply to.
                    continue

                modifiers_for_instance = self._modifiers_on_instance.setdefault(instance, {})
                # assert modifier_raw_info['id'] not in info_for_file

                modifier_asset = ModifierAsset(modifier_raw_info, file_info)

                asset_id = make_asset_id(modifier_asset.asset_url)
                modifiers_for_instance[asset_id] = modifier_asset
     
        for instance_for_modifier, modifier_info in self.for_each_modifier():
            self.modifiers_per_parent_by_id[instance_for_modifier][make_asset_id(modifier_info.asset_url)] = modifier_info

        for node in self.breadth_first_within_figure(dson_node):
            if isinstance(node, ModifierAsset):
                continue

            if node.asset is None:
                continue

            # For each instance, map from asset IDs to descendant node.  This must be
            # breadth first, so if there are multiple descendants with the same ID we find
            # the right one.
            descendants = {}
            assert node not in self.descendants_per_instance, node.node_id
            self.descendants_per_instance[node] = descendants

            for descendant in self.breadth_first_within_figure(node):
                if isinstance(descendant, ModifierAsset):
                    if descendant.data['id'] not in descendants:
                        descendants[descendant.data['id']] = descendant

                    continue

                assert isinstance(descendant, DSON.DSONNode)

                # XXX: commented out for matching generated FID_ nodes
                # if descendant.asset is None:
                #     continue

                if descendant.asset_id not in descendants:
                    descendants[descendant.asset_id] = descendant

    def _resolve_url_to_property(self, parsed_url, parent_node):
        if parsed_url.scheme == '~grandparent':
            log.debug('XXX: moving from %s to %s for %s', parent_node, parent_node.parent, parsed_url)
            parent_node = parent_node.parent

        descendants_on_scheme = self.descendants_per_instance.get(parent_node)
        if descendants_on_scheme is None:
            return None

        target_node = descendants_on_scheme.get(parsed_url.fragment)
        if target_node is None:
            return None

        # If this points at an alias instance, resolve it.
        if isinstance(target_node, ModifierAsset) and target_node.data['channel']['type'] == 'alias':
            target_channel_url = DSONURL(target_node.data['target_channel'])
            return self._resolve_url_to_property(target_channel_url, target_node)
        elif isinstance(target_node, DSON.DSONNode) and target_node.get_value('channel/type') == 'alias':
            # XXX untested
            raise RuntimeError('test me')
            target_channel_url = target_node.get_value('channel/target_channel')
            return self._resolve_url_to_property(target_channel_url, target_node)

        if isinstance(target_node, ModifierAsset):
            return target_node

        try:
            return target_node.get_property(parsed_url.query)
        except DSON.NodeNotFound:
            return None

    def get_cached_modifier_from_node(self, modifier_node):
        """
        Given a DSONNode for a modifier, return its cached ModifierAsset, or None if it
        isn't found (or the DSONNode isn't a modifier).
        """
        assert isinstance(modifier_node, DSON.DSONNode)
        modifier_assets = self.modifiers_per_parent_by_id.get(modifier_node.parent, {})

        if modifier_node.asset is None:
            return None

        asset_id = make_asset_id(modifier_node.asset.asset_url)
        return modifier_assets.get(asset_id)

    def get_cached_modifier_from_node_name(self, target_node, node_name):
        """
        Given a node and the name of a modifier that can be applied to it, return the ModifierAsset
        if found.
        """
        assert isinstance(target_node, DSON.DSONNode)
        modifier_assets = self.modifiers_per_parent_by_id.get(target_node, {})

        for asset_id, modifier in modifier_assets.iteritems():
            if modifier.data['name'] == node_name:
                return modifier
        return None

    def _cache_property_values(self):
        # Map each URL used in modifiers to a DSONProperty on an instance in the scene, if there is one.
        self.cached_property_values = {}

        cache = {}
        
        for instance_for_modifier, modifier_info in self.for_each_modifier():
            instance_for_modifier = instance_for_modifier.find_top_node()

            def store_url_for_property(url):
                # See if we've processed this URL before.  Note that this may still have duplicates, eg. if
                # the same resource is referred to with different capitalization or URL escaping.  This cache
                # is just for performance.
                url_string = str(url)
                result = cache.get(url_string)
                if result is not None:
                    return result

                value = 0

                modifier_property = self._resolve_url_to_property(url, instance_for_modifier)
#                log.debug('url: %s', url)
#                log.debug('instance: %s', instance_for_modifier)
#                log.debug('modifier_property: %s %s', modifier_property, id(modifier_property))

                if isinstance(modifier_property, DSON.DSONProperty):
                    # There's an instance for this channel.  It might be an instanced modifier, or
                    # just a joint or other non-modifier node.  Get the value out of the instance.
                    value = modifier_property.get_value_with_default()

                    # If this is an instance of a modifier, find its cached ModifierAsset to assign the value to.
                    resolved_url = self.get_cached_modifier_from_node(modifier_property.node)
                    if resolved_url is None:
                        resolved_url = NonModifierReference(url)

                elif modifier_property is not None:
                    # We didn't find an instance, but we found a ModifierAsset.  Use its default value.
                    assert isinstance(modifier_property, ModifierAsset), modifier_property

                    assert 'value' in modifier_property.data['channel'], modifier_property.data['channel']
                    value = modifier_property.data['channel']['value']
                    resolved_url = modifier_property
                else:
                    # If we can't find the node, it's usually a non-modifier channel like a joint.
                    resolved_url = NonModifierReference(url)

                # For each URL in the formula, store its current value.  This is either an instance's
                # value, or the default value if there are no instances.
                self.cached_property_values[resolved_url] = value

                cache[url_string] = resolved_url

                return resolved_url

            if modifier_info.data['channel']['type'] == 'alias':
                continue

            if modifier_info.data['channel'].get('auto_follow'):
                # auto_follow modifiers add the value of any node with the same name in the parent of the
                # node they're affecting.  Add an implicit formula to do this.  See add_auto_follow_formulas
                # for a more in-depth explanation of this.
                modifier_name = modifier_info.data['name']

                parent_of_instance = instance_for_modifier.parent
                if parent_of_instance:
    #                log.debug('parent_of_instance %s', parent_of_instance)
                    following_node = parent_of_instance.find_asset_name(modifier_name, default=None)
                    if following_node:
                        log.debug('following_node %s %s', following_node, following_node.url)
                        
                        # auto_follow is annoying: it follows a node inside the parent's parent, which
                        # regular formulas can't do.  We have a special scheme "~grandparent", which
                        # refers to the parent of the node this modifier is conforming to.
                        # XXX: Can we create this pointing directly at the ModifierAsset, like we create them
                        # normally?
                        following_url = DSONURL('')
                        following_url.scheme = '~grandparent'
                        following_url.fragment = following_node.asset.get_value('id')
                        following_url.query = 'value'

                        channel_url = DSONURL('')
                        channel_url.scheme = instance_for_modifier.asset.get_value('name')
                        channel_url.fragment = modifier_info.data['name']
                        channel_url.query = 'value'

                        follow_formula = {
                            'operations': [{
                                'url': str(following_url),
                                'op': 'push',
                            }],
                            'output': str(channel_url),
                        }
                        modifier_info.data['formulas'].append(follow_formula)

            modifier_url = DSONURL(modifier_info.data['channel_url'])
#            log.debug('do cached_property_values: %s, %s %s', modifier_info, id(modifier_info), modifier_url)

            store_url_for_property(modifier_url)

            # Formula inputs and outputs are URLs.  Replace them with references to properties
            # via ModifierAsset and NonModifierReference instances.
            for formula in modifier_info.data['formulas']:
                formula['output'] = store_url_for_property(DSONURL(formula['output']))

                for op in formula['operations']:
                    if op['op'] != 'push' or 'url' not in op:
                        continue

                    modifier_property = self._resolve_url_to_property(DSONURL(op['url']), instance_for_modifier)

                    op['url'] = store_url_for_property(DSONURL(op['url']))

    def __init__(self, dson_nodes, cached_info_per_file):
        self._modifiers_on_instance = {}

        # Make a mapping from each node to all of its descendants, so we can efficiently look
        # up nodes from any parent node.  This maps to either ModifierAssets or DSONNodes.
        self.descendants_per_instance = {}
        self.modifiers_per_parent_by_id = defaultdict(dict)
        for dson_node in dson_nodes:
            self._make_current_info(dson_node, cached_info_per_file)

        self._cache_property_values()

        # Scan over each modifier that we know about, starting with base modifiers with no dependencies
        # and working downwards.
        formulas = set()

        for instance_for_modifier, modifier_info in self.for_each_modifier():
            if not isinstance(modifier_info, ModifierAsset):
                continue

            for idx, dson_formula in enumerate(modifier_info.data['formulas']):
                formula_name = '%s#%i' % (modifier_info.data['channel_path'], idx)

                # Note that the formula inputs and outputs may be a ModifierAssets or a NonModifierReference.
#                log.debug('output: %s', dson_formula['output'])
#                log.debug('ops: %s', pformat(dson_formula['operations']))
                formula = modifiers.DSONFormula(dson_formula['output'], dson_formula['operations'], dson_formula.get('stage', 'sum'), formula_name)
                formula.optimize()
                formula.modifier_info = modifier_info
                formulas.add(formula)

        self.formulas_by_output = defaultdict(set)
        for dson_formula in formulas:
            output_url = dson_formula.output_property
            self.formulas_by_output[output_url].add(dson_formula)

        self.modifiers_required = defaultdict(set)
        self.modifiers_required_by = defaultdict(set)
        modifiers_required_by_id = defaultdict(set)
        properties_by_id = {}
        for instance_for_modifier, modifier in self.for_each_modifier():
            if not isinstance(modifier, ModifierAsset):
                continue

            if modifier.data['channel']['type'] == 'alias':
                continue

            properties_by_id[id(modifier)] = modifier
            modifiers_required_by_id[id(modifier)]

        for dson_formula in formulas:
            output = dson_formula.output_property

            if isinstance(output, ModifierAsset) and output.asset_url != dson_formula.modifier_info.asset_url:
                # This modifier is outputting to another modifier, so this modifier requires the
                # other.
                self.modifiers_required[dson_formula.modifier_info.asset_url].add(output.asset_url)
                self.modifiers_required_by[output.asset_url].add(dson_formula.modifier_info.asset_url)

            deps = {}
            for op in dson_formula.operations:
                if op['op'] != 'push':
                    continue
                url = op.get('url')
                if url is None:
                    continue

                assert output != url, (output, url)

                # Make a simple dependency list by URL.  This is for the UI.
                if isinstance(url, ModifierAsset) and url.asset_url != dson_formula.modifier_info.asset_url:
                    # This is unintuitive: this modifier is using another modifier as an input, so
                    # the other modifier requires this one.  For example, pCTRLlFingersIn is a hidden
                    # control that adjusts the skeleton pose.  It sets its value to the value of the
                    # visible control, CTRLlFingersInOut.  CTRLlFingersInOut itself has no effect
                    # without pCTRLlFingersIn and is only a placeholder for the user control.
                    self.modifiers_required[url.asset_url].add(dson_formula.modifier_info.asset_url)
                    self.modifiers_required_by[dson_formula.modifier_info.asset_url].add(url.asset_url)

                # Store the dependencies as pointers rather than values.  topological_sort does a lot
                # of set operations and our classes compare based on id anyway, so this is a lot faster.
                properties_by_id[id(output)] = output
                properties_by_id[id(url)] = url
                modifiers_required_by_id[id(output)].add(id(url))
                modifiers_required_by_id[id(url)]

        self.modifiers_required = dict(self.modifiers_required)
        self.modifiers_required_by = dict(self.modifiers_required_by)

        self.properties_in_dependency_order = list(util.topological_sort(modifiers_required_by_id))

        # Map the results back to the objects.
        self.properties_in_dependency_order = [properties_by_id[prop] for prop in self.properties_in_dependency_order]
       
        # Evaluate the current value of all channels, including formulas.  We do this by
        # running through all properies in order of their dependency, replacing PUSH references
        # to other properties with their value, and then evaluating it.  The references should
        # always have their value available, since we run formulas before other formulas that
        # depend on them.
        self.static_property_values = {}
        
        for property_url in self.properties_in_dependency_order:
            formulas = self.formulas_by_output[property_url]
            formula_list = []

            for formula in formulas:
                # Replace each PUSH of another channel with the value of the target after
                # formulas.  We're evaluating formulas in dependency order, so inputs will
                # always be calculated.
                operations = []
                
                for op in formula.operations:
                    if op['op'] != 'push' or 'url' not in op:
                        operations.append(op)
                        continue

                    url = op['url']
                    value = self.static_property_values[url]

                    operations.append({
                        'op': 'push',
                        'val': value,
                    })

                new_formula = formula.copy_with_new_operations(operations)

                formula_list.append(new_formula)

            if isinstance(property_url, NonModifierReference):
                # Why was this always 0 before?  XXX: get rid of NonModifierReference special case once this
                # is tested
                static_value = self.cached_property_values.get(property_url, 0)
            else:
                # We should always have a cached value.  One case where we're not working is when multiple files
                # with different filenames define the same node ID on the same parent, in which case we don't
                # match the output URL correctly and one of them ends up without a value.
                static_value = self.cached_property_values.get(property_url, 0)
            property_value = modifiers.DSONFormula.evaluate_formula_list(formula_list, static_value=static_value)
            
            self.static_property_values[property_url] = property_value

        # Make a list of all modifiers available.
        self.all_modifiers = {}
        for channel in self.cached_property_values.iterkeys():
            if not isinstance(channel, ModifierAsset):
                continue
        
            if channel.data['channel']['type'] == 'alias':
                continue

            self.all_modifiers[channel.asset_url] = channel

    def get_modifier_info(self, asset_config):
        # Determine which channels are constant.  A channel is constant if it's a modifier
        # and the asset_config.get_dynamic says we won't create a dynamic network for it.
        # It's also constant if it has inputs that cause its value to become constant.  For example, if a formula is
        # "A * B", and B is a constant 0, then the value is always 0, even if A is dynamic.
        #
        # Do this by replacing all PUSH operations that point to non-dynamic properties with
        # their current value, and then re-optimizing the formula.  This will reduce the formula
        # down to a constant if possible.


        # Make a list of modifiers that can usefully be shown to the user to enable or disable.
        #
        # If a modifier isn't dynamic and has a zero weight, exclude it.
        #
        # If a modifier's inputs cause it to always have a constant value of zero because of a
        # zero mult stage formula, mark it constant and exclude it.
        #
        # If all of a modifier's inputs are constant, mark it constant.  If they result in a value
        # of zero, exclude it.




        # - If a property has a formula that multiplies it by a constant value of zero, the value
        # of the modifier is always zero.  
        # There's no point in including it, or listing it as an
        # option for the user to include.  This is true even if it would be a user visible control.
        # - If a modifier isn't visible (eg. corrective modifiers), and all of its inputs are
        # constant, the modifier is constant.  If the constant value is zero, we should also
        # exclude it.
        # - If a modifier is visible, we're giving a user control for it.  In that case, the modifier
        # is never constant.

        # forced_constant_properties are properties that are constant because another modifier is
        # causing them to be constant.  They'll still be constant even if they're configured dynamic.
        # configured_constant_properties are properties that are only constant because they're
        # configured non-dynamic (so it's meaningful to offer to make them dynamic).
        forced_constant_properties = set()
        configured_constant_properties = set()
        for channel in self.properties_in_dependency_order:
            formulas = self.formulas_by_output[channel]
            formula_list = []

            # If this property is something other than a modifier channel, such as a joint transform,
            # we never treat it as constant.
            if not isinstance(channel, ModifierAsset):
                continue

            for formula in formulas:
                def add_formula():
                    operations = []

                    for op in formula.operations:
                        if op['op'] != 'push' or 'url' not in op:
                            operations.append(op)
                            continue

                        prop = op['url']

                        # If this is pointing to an InstancedProperty, look at its ModifierAsset.
                        if prop not in forced_constant_properties and prop not in configured_constant_properties:
                            # print 'dynamic input', op['url']
                            operations.append(op)
                            continue

                        # print 'input is constant', op['url']

                        value = self.static_property_values[prop]
                        operations.append({
                            'op': 'push',
                            'val': value,
                        })

                    # Create a new formula with the modified operation list.  This will re-optimize the
                    # list, so we can tell if it's constant.
                    new_formula = formula.copy_with_new_operations(operations)
                    return new_formula

                new_formula = add_formula()
                if new_formula is not None:
                    formula_list.append(new_formula)

            # If the modifier is multiplied by zero, it's always zero, even if it would have a user control.
            # Check if the modifier is multiplied by zero.  
            if modifiers.DSONFormula.formula_list_multiplies_by_zero(formula_list):
                # log.debug('forced constant (multiplied by zero): %s', channel)
                forced_constant_properties.add(channel)
                continue

            if isinstance(channel, ModifierAsset) and not asset_config.channel_has_external_inputs(channel):
                # This control has no external inputs, like a user control.  If its result is constant, this
                # control is constant.  This doesn't apply if there are external inputs like user controls,
                # since the user could change it.  This check allows us to hide character morphs that are set
                # to zero.
                if modifiers.DSONFormula.formula_list_is_constant(formula_list):
                    # log.debug('Non-visible control %s has only constant inputs, so this control is constant', channel)
                    forced_constant_properties.add(channel)
                    continue

            # If a formula isn't dynamic, it's constant regardless of whether its inputs are constant
            # or not.  Check this after we're done seeing if this is forced constant.
            if isinstance(channel, ModifierAsset) and not asset_config.get_dynamic(channel):
                # log.debug('configured static: %s', channel)
                configured_constant_properties.add(channel)

        # Modifiers can be switched between dynamic and non-dynamic via AssetConfig.  If that changes,
        # the list needs to be refreshed.
        result = AssetCacheResults()
        result.modifier_static_values = dict(self.static_property_values)
        result.all_modifiers = self.all_modifiers

        result.dynamic_modifiers = set()

        # Separate modifiers into a set of modifiers that are useful to offer to the user and ones
        # which aren't.  Note that unused_modifiers should only include modifiers which wouldn't do
        # anything if they were enabled.  This doesn't determine whether the modifier should be
        # enabled by default, only whether it should show up in the list at all.
        for modifier_info in self.cached_property_values.iterkeys():
            if not isinstance(modifier_info, ModifierAsset):
                continue
            
            if modifier_info.data['channel']['type'] == 'alias':
                continue

            # Make a list of dynamic modifiers.  You can query this with asset_config.get_dynamic yourself,
            # but this encapsulates the results into the AssetCacheResults.
            if asset_config.get_dynamic(modifier_info):
                result.dynamic_modifiers.add(modifier_info.asset_url)

            if result.is_zero(modifier_info):
                if modifier_info in forced_constant_properties:
                    # This modifier is zero, and something else is forcing it to zero.  This is a modifier
                    # that isn't used by the scene and won't do anything even if it's made dynamic, so we
                    # won't show it to the user at all.
                    # log.debug('unavailable: %s %s', modifier_info, id(modifier_info))
                    result.unavailable_modifiers[modifier_info.asset_url] = modifier_info
                elif modifier_info in configured_constant_properties:
                    # This modifier is zero because it's configured that way.  It would be available if
                    # it was configured dynamic.
                    # log.debug('dynamic: %s %s', modifier_info, id(modifier_info))
                    result.available_for_dynamic[modifier_info.asset_url] = modifier_info
                else:
                    # This modifier is zero, but isn't constant.  It's not used by the scene, but the user
                    # can enable it.  If he does, it'll always be dynamic, since it would have no effect
                    # otherwise.
                    # log.debug('unused: %s %s', modifier_info, id(modifier_info))
                    result.unused_modifiers[modifier_info.asset_url] = modifier_info
            else:
                # This modifier has a value, which means it's enabled in the scene (or the asset
                # has an enabled default for some reason).  This modifier can be enabled or disabled,
                # and set dynamic or static if it's enabled.
                # log.debug('used: %s %s', modifier_info, id(modifier_info))
                result.used_modifiers[modifier_info.asset_url] = modifier_info

        # available_modifiers is modifiers that aren't unavailable (this is also the same as
        # used_modifiers | unused_modifiers).
        result.available_modifiers = dict(result.used_modifiers)
        result.available_modifiers.update(result.unused_modifiers)
        result.available_modifiers.update(result.available_for_dynamic)

        return result

    def get_favorite_modifiers_in_scene(self, env):
        """
        Return a set of ModifierAssets which are flagged as favorites in the scene.
        """
        # This is a list of IDs (not URLs), sometimes with "/Value" appended, which seems
        # to correspond to the "value" name of most channels but with a different case.  Also,
        # favorites are stored on the figure, even if the favorited channel is on geometry
        # or something else.  Why isn't this just a URL like everything else?
        def find_modifier_within_figure(dson_node, favorite_node):
            # Search this node, and any geometry on it.
            for child_node in self.breadth_first_within_figure(dson_node):
                if isinstance(child_node, ModifierAsset):
                    continue

                modifier = self.get_cached_modifier_from_node_name(child_node, favorite_node)
                if modifier is None:
                    continue

                if modifier.data['channel']['type'] == 'alias':
                    target_channel_url = DSONURL(modifier.data['target_channel'])
                    modifier = self._resolve_url_to_property(target_channel_url, dson_node)
                
                return modifier

            return None

        result = set()

        for dson_node in env.scene.depth_first():
            # For some reason, favorites are sometimes added to "channels" instead of "favorites".
            favorites = list(dson_node.get_value('extra/studio_node_channels/favorites', []))
            favorites.extend(list(dson_node.get_value('extra/studio_node_channels/channels', [])))

            for favorite_node in favorites:
                # "channels" can contain channel definition dictionaries.  Ignore those for now.
                if not isinstance(favorite_node, basestring):
                    continue

                # Sometimes entries are just a node ID, and sometimes they're "Node/Value".
                # We only need the modifier.
                if '/' in favorite_node:
                    favorite_node = favorite_node.split('/')[0]

                favorite_node = urllib.unquote(favorite_node)

                # First, search the scene for an asset with this name.  This finds favorite modifiers that are
                # saved in the scene itself.
                modifier = dson_node.find_asset_name(favorite_node, default=None)
                if modifier is None:
                    # The more common case, find favorite modifiers stored in the library.
                    # Search each node within the hierarchy for a modifier with this ID.
                    modifier = find_modifier_within_figure(dson_node, favorite_node)

                if modifier is None:
                    log.warning('Couldn\'t find favorite modifier: %s', favorite_node)
                    continue

                result.add(modifier.asset_url)
        
        return result


