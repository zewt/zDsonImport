import logging, weakref
from pymel import core as pm
import maya_helpers as mh

log = logging.getLogger('DSONImporter')

def set_maya_transform_attrs(dson_node, mesh_set):
    """
    Record where this transform came from.  This makes it easier to figure out what nodes are
    in later auto-rigging.
    """
    if not dson_node.maya_node:
        return

    assert dson_node.maya_node.exists(), dson_node

    maya_node = dson_node.maya_node

    if dson_node.is_top_node:
        pm.addAttr(maya_node, longName='dson_top_node', at=bool)
        pm.setAttr(maya_node.attr('dson_top_node'), True)

        conform_target = dson_node.get('conform_target')
        if conform_target is not None:
            conform_target = conform_target.load_url()

            # Save this conform in an attribute for reference.
            pm.addAttr(dson_node.maya_node, longName='following_figure', at='message', niceName='Following figure')
            conform_target.maya_node.attr('message').connect(dson_node.maya_node.attr('following_figure'))

    # Connect the main DSON transform to each of its meshes, to make them easier to find
    # in scripts later.
    if mesh_set is not None:
        pm.addAttr(dson_node.maya_node, longName='dson_meshes', at='message', niceName='DSON meshes')
        for loaded_mesh in mesh_set.meshes.values():
            if loaded_mesh.maya_mesh is None:
                continue
        
            pm.addAttr(loaded_mesh.maya_mesh, longName='dson_transform', at='message', niceName='DSON transform')
            dson_node.maya_node.attr('dson_meshes').connect(loaded_mesh.maya_mesh.attr('dson_transform'))

    # Store the node type, to allow distinguishing nodes that represent eg. modifiers.
    pm.addAttr(maya_node, longName='dson_type', dt='string', niceName='DSON type')
    if dson_node.node_source == 'node':
        pm.setAttr(maya_node.attr('dson_type'), dson_node.get_value('type'))
    else:
        pm.setAttr(maya_node.attr('dson_type'), dson_node.node_source)

    # Store the node's parent's name.  For modifiers, this won't be the same as the Maya
    # parent.
    pm.addAttr(maya_node, longName='dson_parent_name', dt='string', niceName='DSON parent name')
    if dson_node.parent and 'name' in dson_node.parent:
        maya_node.attr('dson_parent_name').set(dson_node.parent.get_value('name'))

    if dson_node.node_source == 'modifier':
        # For modifiers, store a direct connection to the parent.  We don't do this for all
        # nodes since it doesn't seem useful and adds a ton of DG connections.
        pm.addAttr(maya_node, longName='dson_parent', at='message', niceName='DSON parent')
        if dson_node.parent and dson_node.parent.maya_node:
            dson_node.parent.maya_node.attr('message').connect(dson_node.maya_node.attr('dson_parent'))

    pm.addAttr(maya_node, longName='dson_url', dt='string', niceName='DSON URL')
    pm.setAttr(maya_node.attr('dson_url'), dson_node.url)

    if dson_node.asset:
        pm.addAttr(maya_node, longName='dson_asset_name', dt='string', niceName='DSON asset name')
        pm.setAttr(maya_node.attr('dson_asset_name'), dson_node.asset.get_value('name'))

        pm.addAttr(maya_node, longName='dson_asset_url', dt='string', niceName='DSON asset URL')
        pm.setAttr(maya_node.attr('dson_asset_url'), dson_node.asset_url)

def get_maya_property_name(prop, ignore_channel=False):
    """
    Given a property, return a reasonable Maya name to use for it.
    If ignore_channel is True, return the property for the whole vector, eg. return
    '.translate' instead of '.translateX'.

    This doesn't create or query anything.  It just generates a name to use elsewhere.
    """
    prop_parts = prop.path.split('/')

    # Get the property key, without any channel suffixes attached.
    prop_key = prop_parts[0]
    mapping = {
        'translation': 'translate',
        'rotation': 'rotate',
        'scale': 'scale',
    }
    maya_key = None
    if prop_key in mapping:
        prop_key = mapping[prop_key]

    if prop.path.count('/') == 1 and not ignore_channel:
        # If we've been given a single channel, eg. rotation/x, return it.
        assert len(prop_parts) == 2, prop_parts
        assert prop_parts[1] in ('x', 'y', 'z'), prop_parts
        return '%s%s' % (prop_key, prop_parts[1].upper())
    else:
        # Otherwise, return the vector itself.
        return prop_key

_transforms_on_node = weakref.WeakKeyDictionary()
def create_transform_with_parents(parent, name, internal_control=False):
    """
    Create a transform, and any transforms above it that don't exist.
    """
    for part in name.split('|'):
        # Keep track of the transforms we create.  This way, if Maya renames part of the
        # path, we'll use the node for the next node created inside it, rather than trying
        # to find the original name and creating a new one.
        part = mh.cleanup_node_name(part)
        transforms_on_parent = _transforms_on_node.setdefault(parent, {})
        node = transforms_on_parent.get(part)
        if node is not None:
            parent = node
            continue

        # This group doesn't exist.  Recurse, to make sure its parent exists.
        node = pm.createNode('transform', p=parent, n=part)

        # Hide the transform properties.  This is a data node, so they're just noise.
        mh.hide_common_attributes(node, transforms=True, visibility=True)

        if internal_control:
            mh.config_internal_control(node)

        transforms_on_parent[part] = node
        parent = node
    
    return node

def _pretty_print_label(label):
    # Rename "X Translate" to "Translate X", to match Maya conventions.
    parts = label.split()
    if len(parts) == 2 and parts[0] in ('X', 'Y', 'Z'):
        return ' '.join([parts[1], parts[0]])
    return label

def _get_placeholder_group_for_property(node, grouping):
    """
    Return a Maya node path to store data for this DSON node.  For example:

    |Controls|Genesis3Female|eCTRLMouthSmile
    """
    ancestors = []
    next_node = node
    while next_node:
        if next_node.is_root or next_node.is_top_node:
            # Don't include the scene/library node.
            break

        name = mh.cleanup_node_name(next_node.get_label())

        ancestors.append(name)
        next_node = next_node.parent

    # Group everything inside top_group, eg. "Controls", and then inside the top node.
    ancestors.append(grouping)

    ancestors.reverse()

    return '|'.join(ancestors)

def _get_control_group_for_property(prop):
    # There are two groups of modifier properties: ones with regions and ones without.  If
    # we just concatenate them we get something reasonable, but pretty sparse with a few
    # controls scattered among a lot of controls:
    #
    # |Pose_Controls|Head|Eyes
    # |Pose_Controls|Head|Mouth
    # |Pose_Controls|Head|Mouth|Lips
    # |Eyes|Real_World
    # |Mouth|Real_World
    #
    # Clean this up so it makes more sense in our tree organization.  Remove "Pose_Controls",
    # moving its contents up.  Move the root controls that we know match with groups inside
    # Head into Head, so |Eyes|Real_World becomes |Head|Eyes|Real_World.
    group_name = ''
    region = prop.node.get_value('region', '')
    if region:
        group_name += region

    group = prop.node.get_value('group', '').replace('/', '|')
    if group:
        if group.startswith('|'):
            group = group[1:]
        if group_name:
            group_name += '|'
        group_name += group

    parts = group_name.split('|')

    # Remove "Actor" prefixes.
    if len(parts) > 0 and parts[0] == 'Actor':
        parts = parts[1:]

    # Shaping groups (controls not inside Pose Controls) tend to be small, since we usually
    # don't create shaping controls for a lot of controls.  For example, it's common to have
    # "|Eyes|Real World", with nothing inside Eyes.  Remove everything beyond the first entry
    # if this isn't inside Pose Controls or Morphs.
    if len(parts) > 0 and parts[0] not in ('Pose Controls', 'Morphs'):
        parts[1:] = []

    # Remove the "Pose Controls" or "Morphs" prefix.
    if len(parts) > 0 and parts[0] in ('Pose Controls', 'Morphs'):
        parts = parts[1:]

    # Move shaping controls in the root to inside the Head group.
    if len(parts) > 0 and parts[0] in ('Eyes', 'Mouth', 'Nose', 'Brow'):
        parts[0:0] = ['Head']

    group_name = '|'.join(parts)
    if group_name:
        group_name = '|' + group_name
    return group_name

def create_attribute_on_control_node(prop, initial_value):
    search_node = prop.node.find_top_node()

    # Add the modifier group, if any.  We'll usually have a "group", eg. "/Morphs", and
    # we may have a region, eg. "Legs".  Prefix the region if we have one.
    group_name = _get_control_group_for_property(prop)

    # Create the control node for this Maya node if we haven't yet.
    control_output_node = create_transform_with_parents(search_node.maya_node, 'Controls'+group_name)
    
    property_name = mh.cleanup_node_name('%s_%s' % (prop.node.node_id, prop.get_label()))

    # Create the attribute.
    if isinstance(prop.value, list):
        # This code path isn't currently used.
        raise RuntimeError('test me')

        # We're expecting vector3s, eg. translation, rotation, etc. values.  Sometimes DSON files contain
        # a partial vector, with only one or two axes actually in the array.  Check that this actually
        # looks like an x/y/z property.
        assert len(prop.value) <= 3, 'Property length not handled: %s' % prop
        for sub_prop in prop.value:
            assert sub_prop['id'] in ('x', 'y', 'z'), 'Array property not handled: %s' % prop

        # This is an array property, which is usually eg. a translation or rotation.  We want
        # to group these into vector3s, but DSON doesn't have full properties for the group as
        # a whole, eg. it doesn't have a label.  All we have is the top-level key, like "translation".
        pm.addAttr(control_output_node, longName=property_name, shortName=property_name, niceName=property_name, at='double3') # niceName=niceName, 
        # Group the sub-properties by ID.
        sub_properties = {sub_prop['id']: sub_prop for sub_prop in prop.value}

        property_names = []
        for axis in ('x', 'y', 'z'):
            # We'll set up all three axes, even if some of them aren't listed in the property.
            sub_prop = sub_properties.get(axis, {
                'name': axis,
            })

            sub_property_name = '%s%s' % (property_name, axis.upper())
            property_names.append(sub_property_name)
            label = _pretty_print_label(sub_prop.get_label())

            pm.addAttr(control_output_node, shortName=sub_property_name, longName=sub_property_name, at='float', p=property_name, niceName=label)

        # Set up attribute properties.  Maya gets confused if we don't add all three sub-attributes
        # before doing this.
        for sub_property_name in property_names:
            pm.setAttr(control_output_node.attr(sub_property_name), e=True, keyable=True)
        attr = control_output_node.attr(property_name)
    elif isinstance(prop.value, dict):
        # This is a modifier channel.
        attr = mh.addAttr(control_output_node, longName=property_name, shortName=property_name, niceName=prop.get_label(), at='float')

        if prop.get_value('visible', True):
            pm.setAttr(attr, e=True, keyable=True)
        else:
            pm.setAttr(attr, e=True, cb=False)

        mh.set_or_connect(attr, initial_value)

        # Create an extra hidden node, and pass the user control values through it instead of using them
        # directly.  Otherwise, all of the outputs will show up in the CB when the control node is
        # selected.  There can be hundreds of these and they're all our internal math nodes, so it makes
        # a mess.
        control_value_node = create_transform_with_parents(search_node.maya_node, 'ControlValues'+group_name, internal_control=True)
        value_attr = mh.addAttr(control_value_node, longName=property_name, shortName=property_name, niceName=prop.get_label(), at='float')
        pm.connectAttr(attr, value_attr)
        attr = value_attr
    else:
        raise RuntimeError('unknown property type on %s (%s on %s)' % (prop, prop.path, prop.node))

    return attr

# Associated Maya attributes
#
# Properties may have Maya attributes associated to tell us where modifiers will connect to.
#
# A property can have different input and output attributes.  This is used for rotations.
# Modifiers that change rotation write directly to the rotation, but modifiers that read
# rotations may have a pose reader inserted to give more predictable values.  The input
# attribute is where properties write into the attribute; the output attribute is where
# the attribute outputs to other properties.
#
# We may have a callback registered for a property instead of a Maya property PyNode.  This
# allows lazily creating the property, for properties that only need to exist if someone is
# using them.  If a node has separate input and output properties, we'll call
class MayaPropertyAssociation(object):
    def __init__(self, input_attr, property_map=None):
        assert isinstance(input_attr, pm.PyNode), input_attr
        self._input_attr = input_attr
        self._output_attr = None
        self._property_map = property_map

    def _get_channel(self, maya_attr, prop):
        # If we have no property list, just use the attribute.
        if self._property_map is None:
            return maya_attr

        # Get a list of the channels on this Maya attribute.
        maya_channels = mh.get_channels_cached(maya_attr)

        # If this is the second channel on this property, eg. translate/y, return the second Maya attribute,
        # eg. translateY.
        maya_channel_idx = self._property_map.index(prop.last_path)
        return maya_channels[maya_channel_idx]

    def _get_input_attr(self, prop):
        # Look up the Maya property name for this channel.
        return self._get_channel(self._input_attr, prop)

    def get_output_attr(self, prop):
        # Create the output if we haven't yet.
        if self._output_attr is None:
            self._output_attr = self._create_output_attr()

        # Look up the Maya property name for this channel.
        return self._get_channel(self._output_attr, prop)

    def set_value(self, prop, value):
        # The output of the DSONProperty is the input to the Maya attribute.
        output_attr = self._get_input_attr(prop)

        mh.set_or_connect(output_attr, value)

        # Mark the property as driven if we've connected something to it, but not if it's constant.
        if isinstance(value, pm.PyNode):
            mh.config_driven_control(output_attr.node())

    def _create_output_attr(self):
        # By default, the output attribute is the same as the input attribute.
        return self._input_attr

def _add_maya_attribute(prop, assoc):
    """
    Assign a Maya property to a DSON property in this node.

    Some DSONProperties have corresponding Maya properties.  Properties that do
    this will support modifiers.

    maya_path can be a PyNode pointing to a Maya property, or a function.
    If a function is given, it'll be called if we turn out to actually need the property.
    It should do any work needed to create it, and return the Maya property path.
    This can be used to avoid creating nodes when we support a property dynamically,
    but no modifiers are actually using it.
    """
    assert isinstance(assoc, MayaPropertyAssociation), assoc
    
    assert prop.path not in prop.node.property_maya_attributes, 'Property %s is already registered' % prop
    prop.node.property_maya_attributes[prop.path] = assoc

def get_maya_output_attribute(prop):
    """
    Given a DSON property, return the Maya attribute path to its value.  The attribute
    will be created if it doesn't exist.
    """
    # See if this is a property with an associated Maya attribute.
    assoc = prop.node.property_maya_attributes.get(prop.path)
    if assoc is not None:
        return assoc.get_output_attr(prop)

    return _create_property_placeholder(prop).get_output_attr(prop)

def _create_property_placeholder(prop):
    # This is a property with no equivalent Maya attribute, such as a modifier value.  Create
    # an attribute to hold the value.

    if prop.node.maya_node is None:
        # This property doesn't have a Maya node, either.  Create a placeholder for it, and
        # set this as the node's Maya node.
        property_node_path = _get_placeholder_group_for_property(prop.node, grouping='Properties')
        prop.node.maya_node = create_transform_with_parents(prop.node.find_top_node().maya_node, property_node_path, internal_control=True)

    # Create an attribute to hold the value.
    property_name = 'Property_' + get_maya_property_name(prop)
    pm.addAttr(prop.node.maya_node, at='float', longName=property_name)

    # Save the new paths to property_maya_attributes, so we'll reuse them in later calls.
    value_node = pm.PyNode(prop.node.maya_node).attr(property_name)
    assert 'rotati' not in str(prop), prop
    assoc = MayaPropertyAssociation(value_node)
    _add_maya_attribute(prop, assoc)

    return assoc
#    return value_node

# When we create attributes, we create the input and value properties at the same
# time.  Keep track of these, so we don't have to go searching for them when we
# look it up again later.
_input_attributes_set = weakref.WeakKeyDictionary()

def set_final_property_value(prop, value):
    """
    value may be a constant value or a PyNode attribute.
    """
    # We only set final property values once, when we create formulas for the property.  We may
    # set values on the underlying Maya attribute earlier, but that's set directly, not through here.
    input_attrs = _input_attributes_set.setdefault(prop.node, {})    
    assert prop.path not in input_attrs, 'Final property value set more than once: %s, value %s' % (prop, value)
    input_attrs[prop.path] = True

    if prop.get_value('clamped', False):
        # If this node is clamped, hook up clamping.  This is important, since modifier formulas
        # may depend on it.  We only create the clamp node when somebody actually asks us for
        # the input property.  If we create this in get_maya_output_attribute for nodes that
        # don't need it, we'll leave attributes connected and unchangeable when there's nothing
        # connected to the clamp.
        min_value = prop.get_value('min')
        max_value = prop.get_value('max')

        if isinstance(value, pm.PyNode):
            name = mh.cleanup_node_name('Clamp_%s_%s' % (prop.node.node_id, prop['name'].value))
            clamp_node = pm.createNode('clamp', n=name)

            pm.setAttr(clamp_node.attr('minR'), min_value)
            pm.setAttr(clamp_node.attr('maxR'), max_value)
            pm.connectAttr(value, clamp_node.attr('inputR'))
            value = clamp_node.attr('outputR')
        else:
            # This is just a constant value.  Clamp the value directly, so we don't create an unneeded
            # clamp node.
            value = max(min_value, value)
            value = min(max_value, value)

    # Get the MayaPropertyAssociation for this property, creating one if it doesn't exist.
    property_association = prop.node.property_maya_attributes.get(prop.path)
    if property_association is None:
        property_association = _create_property_placeholder(prop)

    property_association.set_value(prop, value)

def set_attr_to_prop_vector(prop, maya_attr, dson_to_maya_attrs, property_association=None, dynamic=True):
    assert isinstance(prop.value, list)

    maya_channels = mh.get_channels_cached(maya_attr)
    for maya_channel_idx, channel_prop_name in enumerate(dson_to_maya_attrs):
        channel_prop = prop[channel_prop_name]
        maya_channel = maya_channels[maya_channel_idx]
        set_attr_to_prop(channel_prop, maya_channel, property_association=property_association, dynamic=dynamic)

def set_attr_to_prop(prop, maya_attr, property_association=None, dynamic=True):
    """
    Set a Maya attribute to the value of a DSONProperty, and make it dynamic, so
    when modifiers are applied they can affect this property.

    Note that the given node must be both input and output connectable, so modifiers
    can use it as both an input and an output.

    If dynamic is False, just set the attribute's property without making it dynamic.
    """
    value = prop.evaluate()
    if property_association is None:
        pm.setAttr(maya_attr, value)
    else:
        property_association.set_value(prop, value)

    if not dynamic:
        return

    if property_association is None:
        property_association = MayaPropertyAssociation(maya_attr)
    _add_maya_attribute(prop, property_association)

