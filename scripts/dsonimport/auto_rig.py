import json, logging, math, os, re, time
from pprint import pprint, pformat
from pymel import core as pm
from pymel.core import system as ps
from pymel.core.datatypes import Vector
import pymel.core.datatypes as dt
import maya_helpers as mh
from maya import cmds, mel

log = logging.getLogger('DSONImporter')

axes = [(1,0,0), (0,1,0), (0,0,1)]
xy_rot_handle_shape_radius = 2.5

def multiply_vector(v1, v2):
    # PyMel's Vector class is completely screwed.  v /= v2 divides component-wise, but
    # v *= v2 seems to do v * (v2.x + v2.y + v2.z).
    return Vector(v1.x * v2.x, v1.y * v2.y, v1.z*v2.z)

def get_world_vector(node, local_vector):
    """
    Transform local_vector into the coordinate space of node.
    """
    # This is just matrix math, but pymel's matrix class isn't very helpful.
    transform = pm.createNode('transform')
    try:
        pm.parent(transform, node)
        pm.xform(transform, r=True, t=local_vector)
        vec = pm.xform(transform, q=True, t=True, ws=True)
        return vec
    finally:
        pm.delete(transform)

def find_figures_in_scene(exclude_following=True, only_following=None, only_asset_names=None):
    if only_following is not None:
        exclude_following = False

    # Find all transforms that came from top-level DSON nodes, eg. figures.
    # dagObjects is just for performance.
    top_nodes = []
    for node in pm.ls(dagObjects=True):
        node_type = node.nodeType()
        if node_type not in ('transform', 'joint'):
            continue

        if not node.hasAttr('dson_top_node'):
            continue

        if node.hasAttr('following_figure') and exclude_following:
            # Ignore figures that are conforming to other figures.
            continue

        if only_following is not None:
            # Ignore figures that aren't conforming to only_following.
            if not node.hasAttr('following_figure'):
                continue

            following = node.attr('following_figure').get()
            if following != only_following:
                continue

        if only_asset_names is not None:
            asset_name = node.getAttr('dson_asset_name')
            if asset_name not in only_asset_names:
                continue

        top_nodes.append(node)

    return top_nodes

def recurse_within_top_figure(node):
    """
    Recursively find all transforms inside each figure.
    """
    # Only yield nodes that came from DSON transforms.
    if node.hasAttr('dson_asset_name'):
        yield node

    for child in pm.listRelatives(node, children=True, type='joint'):
        # Don't recurse into other figures.
        if child.hasAttr('dson_top_node'):
            continue
        for c in recurse_within_top_figure(child):
            yield c

def get_joints_in_figure(node):
    joints = {}
    for child in recurse_within_top_figure(node):
        if child.hasAttr('dson_conformed'):
            # Ignore joints that are conformed to other joints.
            continue

        asset_name = child.attr('dson_asset_name').get()
        if child.attr('dson_type').get() == 'modifier':
            # This is a property node representing a modifier.  Find the node's target.
            connections = pm.listConnections(child.attr('dson_parent'), s=True, d=False)
            if not connections:
                log.debug('Ignoring DSON modifier with no parent: %s', child)
                continue

            assert len(connections) == 1, 'Expected one DSON parent connection on %s: %s' % (child, connections)

            modifier_target = connections[0]

        assert asset_name not in joints, 'Duplicate joint asset: %s (%s, %s)' % (asset_name, child.name(), joints[asset_name].name())
        joints[asset_name] = child
    return joints

def distance_between_nodes(n1, n2):
    p1 = pm.xform(n1, q=True, ws=True, t=True)
    p2 = pm.xform(n2, q=True, ws=True, t=True)
    vec = (p1[0]-p2[0], p1[1]-p2[1], p1[2]-p2[2])
    return math.pow(vec[0]*vec[0] + vec[1]*vec[1] + vec[2]*vec[2], 0.5)

def world_space_translate_local_scale(node, t, **kwargs):
    """
    Like pm.xform(t=vector), but scale the vector by the inverse of the node's local scale
    first.

    For example, if t=(10,0,0) to move the object along the X axis by 10 units, but the local
    scale is 0.5, translate by t=(5,0,0) instead.
    """
    t = Vector(*t)
    local_scale = Vector(pm.xform(node, q=True, s=True, ws=True))
    t = multiply_vector(t, local_scale)
    pm.xform(node, t=t, **kwargs)

def set_handle_local_position_in_world_space_units(handle, pos):
    """
    Set the localPosition of a zRigHandle from world space units.
    """
    pos = Vector(pos)

    transform = handle.getTransform()

    local_scale = Vector(*pm.xform(transform, q=True, s=True, ws=True))
    pos /= local_scale
    pm.setAttr(handle.attr('localPosition'), pos)

def align_node(node, target, with_scale=True):
    old_scale = pm.xform(node, q=True, s=True, ws=True)

    old_parent = node.getParent()
    pm.parent(node, target, r=True)
    pm.parent(node, old_parent)

    if not with_scale:
        pm.xform(node, s=old_scale, ws=True)

def lock_attr(attr, lock='lock'):
    """
    If lock is 'lock', lock attr and hide it in the CB.

    If lock is 'hide', hide it in the CB and make it unkeyable, but don't lock it.
    We do this with the transform of control nodes which are visible in the viewport
    but whose position doesn't matter, so you can still move them around and put them
    where you want, without cluttering the CB.

    If lock is "unkeyable", make it unkeyable but leave it in the CB.  This is for
    internal nodes where the property is meaningful and which we need unlocked, but
    that shouldn't be keyed by the user.

    It's important to mark nodes under the Rig hierarchy as unkeyable or locked
    if they're not user controls.  This prevents them from being added to character
    sets and auto-keyed, which removes a huge amount of clutter.  It also prevents
    accidental bad changes, such as using "preserve children" and accidentally moving
    an alignment node when you think you're moving a control.
    """
    if lock == 'lock':
        pm.setAttr(attr, lock=True, cb=False, keyable=False)
    elif lock == 'hide':
        pm.setAttr(attr, lock=False, cb=False, keyable=False)
    elif lock == 'unkeyable':
        pm.setAttr(attr, lock=False, cb=True, keyable=False)
    elif lock == 'keyable':
        pm.setAttr(attr, lock=False, cb=False, keyable=True)
    else:
        raise RuntimeError('Invalid lock state: %s' % lock)

def lock_translate(node, lock='lock'):
    for attr in ('translateX', 'translateY', 'translateZ'):
        try:
            lock_attr(node.attr(attr), lock=lock)
        except pm.MayaAttributeError:
            pass

def lock_rotate(node, lock='lock'):
    for attr in ('rotateX', 'rotateY', 'rotateZ'):
        try:
            lock_attr(node.attr(attr), lock=lock)
        except pm.MayaAttributeError:
            pass

def lock_scale(node, lock='lock'):
    for attr in ('scaleX', 'scaleY', 'scaleZ'):
        try:
            lock_attr(node.attr(attr), lock=lock)
        except pm.MayaAttributeError:
            pass

def lock_trs(node, lock='lock'):
    lock_translate(node, lock=lock)
    lock_rotate(node, lock=lock)
    lock_scale(node, lock=lock)

def lock_trs_recursive(node, lock='lock'):
    lock_trs(node, lock=lock)
    for child in pm.listRelatives(node, allDescendents=True):
        lock_trs(child, lock=lock)

def make_all_attributes_unkeyable(node):
    for child in pm.listRelatives(node, allDescendents=True):
        for attr in pm.listAttr(child):
            try:
                pm.setAttr(child.attr(attr), keyable=False)
            except pm.MayaAttributeError:
                pass

def disconnectIncomingConnections(node, t=False, ro=False, s=False):
    attrs = []
    if t: attrs.extend(['translateX', 'translateY', 'translateZ'])
    if ro: attrs.extend(['rotateX', 'rotateY', 'rotateZ'])
    if s: attrs.extend(['scaleX', 'scaleY', 'scaleZ'])
    for attr in attrs:
        conn = node.attr(attr).connections(s=True, d=False, p=True)
        if conn:
            conn[0].disconnect(node.attr(attr))

def get_constraint_weight(constraint, idx):
    """
    Return the attribute for the idx'th weight of constraint.
    """
    connections = pm.listConnections(constraint.attr('target').elementByLogicalIndex(idx).attr('targetWeight'), p=True)
    assert len(connections) == 1
    return connections[0]

def connect_constraint_weights(constraint, idx, source):
    """
    Creating a constraint creates a weight array, and a named attribute connected to the
    weight for each target.  Find the named attribute for target idx and connect source
    to it.
    """
    pm.connectAttr(source, get_constraint_weight(constraint, idx))

def create_proxy_joint(target, parent, name):
    joint = pm.createNode('joint', name=name, p=parent)
    temp_constraint = pm.parentConstraint(target, joint, mo=False)
    pm.delete(temp_constraint)
    return joint

def create_plus_minus(value1, value2, operation='add'):
    node = pm.createNode('plusMinusAverage')
    operations = {
        'none': 0,
        'add': 1,
        'subtract': 2,
        'average': 3,
    }
    pm.setAttr(node.attr('operation'), operations[operation])
    mh.set_or_connect(node.attr('input3D[0]'), value1)
    mh.set_or_connect(node.attr('input3D[1]'), value2)
    return node

def add_reference(node, name, group):
    """
    Add a reference to a node in the specified node.

    This can be used by rig scripts later on to locate individual controls in the rig.
    """
    attr = mh.addAttr(group, name, at='message')
    node.attr('message').connect(attr)
    return attr

def disable_inherit_transform(node):
    """
    Disable inheritsTransform, preserving the current world space transform.
    """
    # Make sure the translation isn't locked.  This can happen if the node is bound to
    # a skin before doing this.
    assert not node.attr('translate').isLocked()
    assert not node.attr('rotate').isLocked()
    assert not node.attr('scale').isLocked()

    translate = pm.xform(node, q=True, translation=True, ws=True)
    rotate = pm.xform(node, q=True, rotation=True, ws=True)
    scale = pm.xform(node, q=True, scale=True, ws=True)

    pm.setAttr(node.attr('inheritsTransform'), 0)

    translate = pm.xform(node, translation=translate, ws=True)
    rotate = pm.xform(node, rotation=rotate, ws=True)
    scale = pm.xform(node, scale=scale, ws=True)

def optimize_skeleton(node):
    # The skeleton output is inefficient if there are a lot of conformed meshes, since each
    # mesh has its own skeleton mirroring the body.  Look for these, and redirect the skeletons
    # to the figure skeleton where possible.  This should probably be done by the main output.
    def same(a, b):
        return abs(a[0]-b[0]) < 0.001 and abs(a[1]-b[1]) < 0.001 and abs(a[2]-b[2]) < 0.001
    
    asset_name = node.attr('dson_asset_name').get()
    t1 = pm.xform(node, q=True, ws=True, t=True)
    r1 = pm.xform(node, q=True, ws=True, ro=True)
    s1 = pm.xform(node, q=True, ws=True, s=True)
    for n in pm.listRelatives(node, c=True, type='joint'):
        # We're only looking for conformed skeletons.  These always have the same asset
        # name as their parent.
        # The clothing joints that are following skeleton joints usually have the exact same transform.
        if not n.hasAttr('dson_asset_name'): continue
        if n.attr('dson_asset_name').get() != asset_name: continue

        t2 = pm.xform(n, q=True, ws=True, t=True)
        r2 = pm.xform(n, q=True, ws=True, ro=True)
        s2 = pm.xform(n, q=True, ws=True, s=True)
        if not same(t1, t2): continue
        if not same(r1, r2): continue
        if not same(s1, s2): continue
        
        # Move all connections from this joint to the parent joint, which we're following.
        for src, dst in pm.listConnections(n, s=False, d=True, c=True, p=True):
            # Skip bindPose connections.
            if dst.node().nodeType() == 'dagPose': continue
            if not node.hasAttr(src.attrName()):
                # Skip nodes that don't exist.  We may not have all of the attributes like
                # .lockInfluenceWeights, etc., but they're not needed for skinning.
                continue
            node.attr(src.attrName()).connect(dst, f=True)

        # .normalizedRotation on conformed joints doesn't have any outbound connections.  If it
        # did, we could reconnect them to normalizedRotation on the parent.
        for attr in ['normalizedRotationX', 'normalizedRotationY', 'normalizedRotationZ']:
            if n.hasAttr(attr):
                assert not pm.listConnections(n.attr(attr), s=False, d=True, c=True, p=True)

        # We've moved everything using this node to its parent.  Delete it.
        pm.delete(n)

class AutoRig(object):
    no_humanik_finger_rigs = True

    def create(self, humanik):
        self.humanik = humanik
        try:
            self.create_inner()
        except BaseException as e:
            log.exception(e)
            
    def create_inner(self):
        # Load the rig handle plugin.
        mh.load_plugin('zRigHandle.py')
        mh.load_plugin('matrixNodes.mll')
        
        self._read_config()

        # Add controls configured in joint_based_controls and face_control_positions to
        # node_names_for_controls.
        self.node_names_for_controls[-1:-1] = [item['name'] for item in self.joint_based_controls]
        self.node_names_for_controls[-1:-1] = sorted(self.face_control_positions.keys())

        top_nodes = find_figures_in_scene(only_asset_names=['Genesis3Male', 'Genesis3Female'])

        # We only actually expect a single figure in the scene.
        if len(top_nodes) > 1:
            raise RuntimeError('Found more than one character: %s', ','.join(top_nodes))

        for node in top_nodes:
            self.rig_figure(node)

    def _set_default_config(self):
        # This is a list of names of places where DSON controls can be accessed.  If a name is in this
        # list it'll be shown in the UI, so the user can place controls there.  These don't have to
        # directly correspond with node names: for example, LeftArm includes all IK and FK controls for
        # the left arm.  Call register_node_for_controls() when a node is created that should be in one
        # of these sets.
        self.node_names_for_controls = [
            'Hip',
            'Spine_Bottom',
            'Spine_Middle',
            'Spine_Top',
            'Breast_Left',
            'Breast_Right',
            'Arm_Left',
            'Arm_Right',
            'Hand_Left',
            'Hand_Right',
            'Head',
            'Leg_Left',
            'Leg_Right',
            'Foot_Left',
            'Foot_Right',
        ]

        # Face controls that follow joints.
        self.joint_based_controls = [{
            'name': 'Face_Brow_Center',
            'align_to': {'rBrowInner': 1, 'lBrowInner': 1},
            'follow': {'CenterBrow': 1},
        }, {
            'name': 'Face_MouthUpper_Right',
            'align_to': {'rLipNasolabialCrease': 1, 'rLipUpperInner': 1},
            'follow': {'rLipUpperOuter': 1},
        }, {
            'name': 'Face_MouthUpper_Center',
            'align_to': {'rLipBelowNose': 1, 'lLipBelowNose': 1},
            'pre_snap_ws_offset': (0,0,1),
            'follow': {'rLipUpperOuter': 1, 'lLipUpperOuter': 1},
        }, {
            'name': 'Face_MouthUpper_Left',
            'align_to': {'lLipNasolabialCrease': 1, 'lLipUpperInner': 1},
            'follow': {'lLipUpperOuter': 1},
        }, {
            'name': 'Face_MouthLower_Right',
            'align_to': {'rLipLowerOuter': 1},
        }, {
            'name': 'Face_MouthLower_Center',
            'align_to': {'LipBelow': 1},
        }, {
            'name': 'Face_MouthLower_Left',
            'align_to': {'lLipLowerOuter': 1},
        }, {
            'name': 'Face_Chin_Center',
            'align_to': {'Chin_End': 1},
        }, {
            'name': 'Face_Eye_Right',
            'align_to': {'rEyelidInner_End': 1},
            'snap': False,
            'os_offset': (0,0.25,0.2),
        }, {
            'name': 'Face_Eye_Left',
            'align_to': {'lEyelidInner_End': 1},
            'snap': False,
            'os_offset': (0,0.25,-0.2),
        }]

        # Positions of face controls, where U = 0 is the left side and V = 0 is the top of the brow.
        # We use this for positions that we can't easily align to directly from joints.  The first
        # entry in each list is the joint to follow after positioning.
        self.face_control_positions = {
            'Face_Ear_Right1':          ('head', 0.0,  0.35),
            'Face_Ear_Right2':          ('head', 0.05, 0.5),
            'Face_Ear_Right3':          ('head', 0.1,  0.65),
            'Face_EarBelow_Right':      ('head', 0.1,  0.85),
            'Face_Ear_Left1':           ('head', 1.0,  0.35),
            'Face_Ear_Left2':           ('head', 0.95, 0.5),
            'Face_Ear_Left3':           ('head', 0.9,  0.65),
            'Face_EarBelow_Left':       ('head', 0.9,  0.85),

            'Face_Brow_Right':          ('rBrowMid', 0.35, 0.325),
            'Face_Brow_Left':           ('lBrowMid', 0.65, 0.325),

            'Face_CheekUpper_Right':    ('head', 0.3,  0.6),
            'Face_Nose_Right':          ('head', 0.4,  0.5),
            'Face_Nose_Left':           ('head', 0.6,  0.5),
            'Face_CheekUpper_Left':     ('head', 0.7,  0.6),

            'Face_Chin1_Right':         ('rJawClench', 0.2,  0.9),
            'Face_Chin2_Right':         ('lowerJaw', 0.35,  0.9),
            'Face_Chin3_Right':         ('lowerJaw', 0.4,  0.8),
            'Face_Chin1_Left':          ('lJawClench', 0.8,  0.9),
            'Face_Chin2_Left':          ('lowerJaw', 0.7,  0.9),
            'Face_Chin3_Left':          ('lowerJaw', 0.6,  0.8),
        }

        self.control_groups = [
            ['Head/Brow/Brow .* Left', ['Face_Brow_Left']],
            ['Head/Brow/Brow .* Right', ['Face_Brow_Right']],
            ['Head/Brow/.*', ['Face_Brow_Center']],

            # These are sort of backwards: "side-side left" means "move left", which affects the right
            # side, so put it on the right.
            ['Head/Mouth/Mouth Side-Side Left', ['Face_MouthUpper_Right']],
            ['Head/Mouth/Mouth Side-Side Right', ['Face_MouthUpper_Left']],
            ['Head/Mouth/Mouth Side-Side.*', ['Face_MouthUpper_Right', 'Face_MouthUpper_Left']],

            ['Head/Mouth/Mouth .* Left', ['Face_MouthUpper_Left']],
            ['Head/Mouth/Mouth .* Right', ['Face_MouthUpper_Right']],
            ['Head/Mouth/Mouth (Narrow|Corner|Frown).*', ['Face_MouthLower_Left', 'Face_MouthLower_Right']],

            ['Head/Mouth/Mouth (Open|Smile).*', ['Face_Chin_Center']],

            ['Head/Mouth/Lips/Lip Top .* Left', ['Face_MouthUpper_Left']],
            ['Head/Mouth/Lips/Lip Top .* Right', ['Face_MouthUpper_Right']],
            ['Head/Mouth/Lips/Lip Top.*', ['Face_MouthUpper_Center']],
            ['Head/Mouth/Lips/Lip Bottom .* Left', ['Face_MouthLower_Left']],
            ['Head/Mouth/Lips/Lip Bottom .* Right', ['Face_MouthLower_Right']],
            ['Head/Mouth/Lips/Lip Bottom.*', ['Face_MouthLower_Center']],
            ['Head/Mouth/.*', ['Face_MouthLower_Center']],

            ['Head/Nose/.*', ['Face_MouthUpper_Center']],

            ['Head/Cheeks_and_Jaw/Cheek (Crease|Eye).*Right', ['Face_Nose_Right']],
            ['Head/Cheeks_and_Jaw/Cheek (Crease|Eye).*Left', ['Face_Nose_Left']],
            ['Head/Cheeks_and_Jaw/Cheek (Crease|Eye).*', ['Face_Nose_Right', 'Face_Nose_Left']],

            ['Head/Cheeks_and_Jaw/Jaw.*', ['Face_Chin_Center']],

            ['Head/Cheeks_and_Jaw/Cheek.* Right', ['Face_CheekUpper_Right']], # "Cheek" and "Cheeks"
            ['Head/Cheeks_and_Jaw/Cheek.* Left', ['Face_CheekUpper_Left']],
            ['Head/Cheeks_and_Jaw/Cheek.*', ['Face_CheekUpper_Left', 'Face_CheekUpper_Right']],

            ['Head/Visemes/.*', ['Face_MouthLower_Center']],

            # Hide eye controls that only affect the eyes, which are replaced by the eye control.
            ['Head/Eyes/Eyes (Side-SIde|Crossed|Closed|Up-Down)', []],

            ['Head/Eyes/(Eyelids Lower Up-Down|Eyelids Upper Down-Up)$', []],
            ['Head/Eyes/Eye.* Left', ['Face_Eye_Left']],
            ['Head/Eyes/Eye.* Right', ['Face_Eye_Right']],
            ['Head/Eyes/Eyes Squint', ['Face_Eye_Left', 'Face_Eye_Right']],
            ['Head/Eyes.*', ['Face_Brow_Center']],

            ['Head/Expressions/.*', ['Head']],

            ['Real_World/(Eyelids Fold).*', ['Face_Eye_Right', 'Face_Eye_Left']],
            ['Real_World/(Mouth Curves|Philtrum Width|Lips Thin)', ['Face_MouthUpper_Center']],
            ['Real_World/Navel', ['Spine_Bottom']],

            # Default:
            ['.*', ['Hip']],
        ]

        self.pick_walk_parents = [
            # We can't put both IK and FK in the same hierarchy without invisible nodes being selected, so we
            # give one of each its own isolated hierarchy.  Put FK for the arms and IK for the legs in the main
            # hierarchy, since they're the defaults shown.
            ('Tongue2', 'Neck'),
            ('Tongue3', 'Tongue2'),
            ('Tongue4', 'Tongue3'),
        ]

        # Pick walk parenting for the regular, non-HumanIK rig.  This is added to pick_walk_parents
        # when in this mode.
        self.pick_walk_parents_for_rig = [
            ('Hip', None),
            ('Spine_Bottom', 'Hip'),
#            ('ChestMid', 'Spine_Bottom'),
            ('Spine_Top', 'Spine_Bottom'),
            ('RightCollar', 'Spine_Top'),
            ('Neck', 'Spine_Top'),
            ('LeftCollar', 'Spine_Top'),
            ('Head', 'Neck'),

            # Arm FK:
            ('FK_RightShoulder', 'RightCollar'),
            ('FK_RightArm', 'FK_RightShoulder'),
            ('FK_RightHand', 'FK_RightArm'),
            ('FK_LeftShoulder', 'LeftCollar'),
            ('FK_LeftArm', 'FK_LeftShoulder'),
            ('FK_LeftHand', 'FK_LeftArm'),

            # Arm IK:
            ('RightArm_Direction', 'RightCollar'),
            ('RightHand', 'RightArm_Direction'),
            ('LeftArm_Direction', 'LeftCollar'),
            ('LeftHand', 'LeftArm_Direction'),

            # Hands:
            ('rPinky1', 'FK_RightHand'),
            ('rPinky2', 'rPinky1'),
            ('rPinky3', 'rPinky2'),
            ('rRing1', 'FK_RightHand'),
            ('rRing2', 'rRing1'),
            ('rRing3', 'rRing2'),
            ('rMid1', 'FK_RightHand'),
            ('rMid2', 'rMid1'),
            ('rMid3', 'rMid2'),
            ('rIndex1', 'FK_RightHand'),
            ('rIndex2', 'rIndex1'),
            ('rIndex3', 'rIndex2'),
            ('rThumb1', 'FK_RightHand'),
            ('rThumb2', 'rThumb1'),
            ('rThumb3', 'rThumb2'),

            ('lThumb1', 'FK_LeftHand'),
            ('lThumb2', 'lThumb1'),
            ('lThumb3', 'lThumb2'),
            ('lIndex1', 'FK_LeftHand'),
            ('lIndex2', 'lIndex1'),
            ('lIndex3', 'lIndex2'),
            ('lMid1', 'FK_LeftHand'),
            ('lMid2', 'lMid1'),
            ('lMid3', 'lMid2'),
            ('lRing1', 'FK_LeftHand'),
            ('lRing2', 'lRing1'),
            ('lRing3', 'lRing2'),
            ('lPinky1', 'FK_LeftHand'),
            ('lPinky2', 'lPinky1'),
            ('lPinky3', 'lPinky2'),

            # Legs:
            ('LeftLeg_Direction', 'Hip'),
            ('RightLeg_Direction', 'Hip'),
            ('LeftFoot', 'LeftLeg_Direction'),
            ('RightFoot', 'RightLeg_Direction'),

            ('FK_LeftThigh', 'Hip'),
            ('FK_LeftLeg', 'FK_LeftThigh'),
            ('FK_LeftFoot', 'FK_LeftLeg'),

            ('FK_RightThigh', 'Hip'),
            ('FK_RightLeg', 'FK_RightThigh'),
            ('FK_RightFoot', 'FK_RightLeg'),

            # Toes:
            ('rSmallToe4', 'RightFoot'),
            ('rSmallToe4_2', 'rSmallToe4'),
            ('rSmallToe3',  'RightFoot'),
            ('rSmallToe3_2', 'rSmallToe3'),
            ('rSmallToe2', 'RightFoot'),
            ('rSmallToe2_2', 'rSmallToe2'),
            ('rSmallToe1', 'RightFoot'),
            ('rSmallToe1_2', 'rSmallToe1'),
            ('rBigToe', 'RightFoot'), 
            ('rBigToe_2', 'rBigToe'),

            ('lBigToe', 'LeftFoot'), 
            ('lBigToe_2', 'lBigToe'),
            ('lSmallToe1', 'LeftFoot'),
            ('lSmallToe1_2', 'lSmallToe1'),
            ('lSmallToe2', 'LeftFoot'),
            ('lSmallToe2_2', 'lSmallToe2'),
            ('lSmallToe3',  'LeftFoot'),
            ('lSmallToe3_2', 'lSmallToe3'),
            ('lSmallToe4', 'LeftFoot'),
            ('lSmallToe4_2', 'lSmallToe4'),
        ]

    def _read_config(self):
        self._set_default_config()

        config_path = self._get_data_path() + 'rig_config.js'
        if not os.access(config_path, os.R_OK):
            return

        data = open(config_path).read()
        data = json.loads(data)

        if 'joint_based_controls' in data:
            self.joint_based_controls.extend(data['joint_based_controls'])
        if 'face_control_positions' in data:
            self.face_control_positions.update(data['face_control_positions'])
        if 'control_groups' in data:
            self.control_groups[0:0] = data['control_groups']

    def straighten_poses(self):
        """
        Figures are generally in a relaxed T-pose.  Move figures to a full T-pose.
        Note that this doesn't bring the arms parallel to the X axis.
        """
        log.debug('Straightening poses')

        joints = [
            # Joint to aim        End joints                Cross, aim, rotate axis   Invert
            ('lShldrBend',        ('lForearmBend',),        (1, 0, 2),                False),
            ('rShldrBend',        ('rForearmBend',),        (1, 0, 2),                False),
            ('lForearmBend',      ('lHand',),               (1, 0, 2),                False),
            ('rForearmBend',      ('rHand',),               (1, 0, 2),                False),
            ('lForearmBend',      ('lHand',),               (2, 0, 1),                True),
            ('rForearmBend',      ('rHand',),               (2, 0, 1),                True),

            # Aim the hand towards the average of the middle and ring finger.
            #('lHand',             ('lMid1', 'lRing1'),      (2, 0, 1),                True),
            #('rHand',             ('rMid1', 'rRing1'),      (2, 0, 1),                True),
            ('lHand',             ('lRing1', ),             (2, 0, 1),                True),
            ('rHand',             ('rRing1', ),             (2, 0, 1),                True),
            ('rFoot',             ('rMetatarsals',),        (0, 2, 1),                False),
            ('lFoot',             ('lMetatarsals',),        (0, 2, 1),                False),
        ]

        # The feet in bind pose are usually pointing slightly outwards.  Aim them along
        # the Z axis, so they're pointing straight ahead.  This is important for HIK floor
        # contact, since its contact planes assume that feet are aligned when in bind pose.
        # The foot joints aren't aligned to the XZ plane, so there's no axis for us to simply
        # align to zero.  Instead, look at the world space angle going down to the next joint,
        # and rotate by the inverse of that.  Do the same for the arm joints and hands.  We
        # want the hands to be square with the world, so floor contact planes are aligned
        # with HIK later.
        # First, find and check all of the joints.  If there are problem with any joints, we
        # won't apply any changes.
        for aim_joint, end_joints, (cross_axis_idx, aim_axis_idx, rotate_axis_idx), invert in joints:
            total_angle = 0
            j1 = self.joints[aim_joint]

            # Average the angle towards each of the target joints.
            for end_joint in end_joints:
                j2 = self.joints[end_joint]

                pos1 = pm.xform(j1, q=True, ws=True, t=True)
                pos2 = pm.xform(j2, q=True, ws=True, t=True)
                if pos2[aim_axis_idx] < pos1[aim_axis_idx]:
                    pos1, pos2 = pos2, pos1

                angle = math.atan2(pos2[cross_axis_idx] - pos1[cross_axis_idx], pos2[aim_axis_idx] - pos1[aim_axis_idx])
                angle = angle * 180 / math.pi 

                if invert:
                    angle = -angle
                total_angle += angle

            total_angle /= len(end_joints)

            # If the angle is too wide, something is probably wrong.  Stop rather than twisting
            # a figure into a weird shape.
            if angle < -45 or angle > 45:
                raise RuntimeError('Unexpected angle while orienting joint %s to %s: %f' % (j1, j2, angle))

            rotate = [0,0,0]
            rotate[rotate_axis_idx] = -angle
            pm.xform(j1, ws=True, r=True, ro=rotate)


    def create_twist_proxies(self):
        """
        The arm and leg twist joints are a bit annoying: they have translation on all axes, not just
        down the joint orient.  This causes rotating the twist joint to wiggle the end joint around,
        when it should just rotate in place.  We don't want to make changes to the skeleton itself,
        since that can confuse modifier rigs attached to it.

        Work around this by creating proxy joints, which are positioned in the same place as the
        final joints but which have their joint orient fixed.  The real joints will be parent constrained
        to these.

        The twist_aim_vectors have been chosen to give these joints the same world space orientation
        as the original joints.
        """
        twist_joints = [{
            'shoulder': 'lShldrBend',
            'shoulder_twist': 'lShldrTwist',
            'elbow': 'lForearmBend',
            'elbow_twist': 'lForearmTwist',
            'hand': 'lHand',

            'twist_aim_vector': (1,0,0),
            'shoulder_twist_up_vector': (0,1,0),
            'elbow_twist_up_vector': (0,0,1),

            'fk_parent': 'lCollar',
        }, {
            'shoulder': 'rShldrBend',
            'shoulder_twist': 'rShldrTwist',
            'elbow': 'rForearmBend',
            'elbow_twist': 'rForearmTwist',
            'hand': 'rHand',

            'twist_aim_vector': (-1,0,0),
            'shoulder_twist_up_vector': (0,1,0),
            'elbow_twist_up_vector': (0,0,1),

            'fk_parent': 'rCollar',
        }, {
            'shoulder': 'lThighBend',
            'shoulder_twist': 'lThighTwist',
            'elbow': 'lShin',
            'elbow_twist': None,
            'hand': 'lFoot',

            'twist_aim_vector': (0,-1,0),
            'shoulder_twist_up_vector': (1,0,0),
            'elbow_twist_up_vector': (1,0,0),

            'fk_parent': 'pelvis',
        }, {
            'shoulder': 'rThighBend',
            'shoulder_twist': 'rThighTwist',
            'elbow': 'rShin',
            'elbow_twist': None,
            'hand': 'rFoot',

            'twist_aim_vector': (0,-1,0),
            'shoulder_twist_up_vector': (1,0,0),
            'elbow_twist_up_vector': (1,0,0),

            'fk_parent': 'pelvis',
        }]

        joint_proxy_group = pm.createNode('transform', n='JointProxies', p=self.rig_internal_node)
        for twist in twist_joints:
            group = pm.createNode('transform', n='JointProxies_' + twist['shoulder'], p=joint_proxy_group)

            parent_of_shoulder = self.joints[twist['shoulder']].getParent()
            pm.parentConstraint(parent_of_shoulder, group, mo=False)

            proxy_group = pm.createNode('transform', n='Proxy_' + twist['shoulder'], p=group)

            # Aim the shoulder joint towards the elbow joint, skipping the shoulder twist joint.  Set
            # the worldUpVector of the constraint down the up vector of the joint, so the proxy joint
            # gets the same twist as the joint.  For example, the knees of figures are usually not pointing
            # straight ahead.
            proxy_shoulder = create_proxy_joint(self.joints[twist['shoulder']], proxy_group, 'Proxy_' + twist['shoulder'])
            temp_constraint = pm.aimConstraint(self.joints[twist['elbow']], proxy_shoulder, mo=False, aimVector=twist['twist_aim_vector'], upVector=twist['shoulder_twist_up_vector'],
                    worldUpType='vector', worldUpVector=get_world_vector(proxy_shoulder, twist['shoulder_twist_up_vector']))
            pm.delete(temp_constraint)

            proxy_shoulder_twist = create_proxy_joint(self.joints[twist['shoulder_twist']], proxy_shoulder, 'Proxy_' + twist['shoulder_twist'])

            proxy_elbow = create_proxy_joint(self.joints[twist['elbow']], proxy_shoulder, 'Proxy_' + twist['elbow'])
            temp_constraint = pm.aimConstraint(self.joints[twist['hand']], proxy_elbow, mo=False, aimVector=twist['twist_aim_vector'], upVector=twist['elbow_twist_up_vector'],
                    worldUpType='vector', worldUpVector=get_world_vector(proxy_elbow, twist['elbow_twist_up_vector']))
            pm.delete(temp_constraint)

            if twist['elbow_twist'] is not None:
                proxy_elbow_twist = create_proxy_joint(self.joints[twist['elbow_twist']], proxy_elbow, 'Proxy_' + twist['elbow_twist'])
            else:
                # There's no elbow (lower leg) twist on the legs.
                proxy_elbow_twist = None

            proxy_hand = create_proxy_joint(self.joints[twist['hand']], proxy_elbow, 'Proxy_' + twist['hand'])

            # Aim constrain the twist joints to the bend joints.
            pm.aimConstraint(proxy_elbow, proxy_shoulder_twist, mo=True, worldUpType='objectrotation', worldUpObject=proxy_elbow,
                    aimVector=twist['twist_aim_vector'],
                    upVector=get_world_vector(proxy_elbow_twist, twist['shoulder_twist_up_vector']),
                    worldUpVector=get_world_vector(proxy_elbow_twist, twist['shoulder_twist_up_vector']))

            if proxy_elbow_twist is not None:
                pm.aimConstraint(proxy_hand, proxy_elbow_twist, mo=True, worldUpType='objectrotation', worldUpObject=proxy_hand,
                        aimVector=twist['twist_aim_vector'],
                        upVector=get_world_vector(proxy_elbow_twist, twist['elbow_twist_up_vector']),
                        worldUpVector=get_world_vector(proxy_elbow_twist, twist['elbow_twist_up_vector']))

            # Constrain the output joints to our proxy joints.  Note that we only need orien constrained,
            # and using parentConstraint shifts the joints slightly where orienConstraint matches the orientation
            # exactly.
            pm.parentConstraint(proxy_shoulder, self.joints[twist['shoulder']], mo=True)
            pm.parentConstraint(proxy_shoulder_twist, self.joints[twist['shoulder_twist']], mo=True)
            pm.parentConstraint(proxy_elbow, self.joints[twist['elbow']], mo=True)
            if twist['elbow_twist'] is not None:
                pm.parentConstraint(proxy_elbow_twist, self.joints[twist['elbow_twist']], mo=True)
            pm.parentConstraint(proxy_hand, self.joints[twist['hand']], mo=True)

            self.joints['proxy_' + twist['shoulder']] = proxy_shoulder
            self.joints['proxy_' + twist['shoulder_twist']] = proxy_shoulder_twist
            self.joints['proxy_' + twist['elbow']] = proxy_elbow
            if twist['elbow_twist']:
                self.joints['proxy_' + twist['elbow_twist']] = proxy_elbow_twist
            self.joints['proxy_' + twist['hand']] = proxy_hand

    def create_arms_and_legs(self):
        ### IK

        # List the info we need for each IK system.  This is written in terms of an arm, but we use
        # it for the leg too.
        iks = [{
            'names': ['LeftShoulder', 'LeftArm', 'LeftHand'],

            # Nodes in this system will be registered for controls to be placed on, under this name.
            'control_group': 'Arm_Left',

            # Only the last FK control and then IK control will be placed in this control group.
            'hand_control_group': 'Hand_Left',

            'shoulder': 'proxy_lShldrBend',
            'elbow': 'proxy_lForearmBend',
            'hand': 'proxy_lHand',
            'preferred_angle': (0, -45, 0),
            'rotation_axis': 'x',

            # FK arms default to the chest, so they don't follow collar movement.  This makes manual adjustments
            # to the collar easier.
            'fk_spaces': ['chestUpper', 'lCollar', 'WorldSpace'],
            'ik_spaces': ['Root', 'COGSpace', 'WorldSpace', 'chestUpper', 'lCollar'],

            # By default, IK follows this node around.  For arms, we follow the chest, so IK follows the body
            # but not shoulder movement.
            'ik_origin': 'chestUpper',

            # The parent of the shoulder, which the FK controls follow.  This is also used to orient the PV
            # controls.
            'fk_parent': 'lCollar',

            'fk_offsets': [Vector(5,0,0), Vector(5,0,0), Vector(0,0,0)],
            'fk_size': [0.2, 0.15, 0.12],

            'pv_control_position': (0,0,-1),
            'pv_control_rotation': (45,0,0),

            'is_left': True,
            'default_ik_weight': 1,
            'ik_reverse_pole_vector': False,
        }, {
            'names': ['RightShoulder', 'RightArm', 'RightHand'],
            'control_group': 'Arm_Right',
            'hand_control_group': 'Hand_Right',

            'shoulder': 'proxy_rShldrBend',
            'elbow': 'proxy_rForearmBend',
            'hand': 'proxy_rHand',
            'preferred_angle': (0, 45, 0),
            'rotation_axis': 'x',

            'fk_spaces': ['chestUpper', 'rCollar', 'WorldSpace'],
            'ik_spaces': ['Root', 'COGSpace', 'WorldSpace', 'chestUpper', 'lCollar'],

            'ik_origin': 'chestUpper',
            'fk_parent': 'rCollar',

            'fk_offsets': [Vector(5,0,0), Vector(5,0,0), Vector(0,0,0)],
            'fk_size': [0.2, 0.15, 0.12],

            'pv_control_position': (0,0,-1),
            'pv_control_rotation': (45,0,0),
            'default_ik_weight': 1,
            'ik_reverse_pole_vector': True,
        }, {
            'names': ['LeftThigh', 'LeftLeg', 'LeftFoot'],
            'control_group': 'Leg_Left',
            'hand_control_group': 'Foot_Left',

            'shoulder': 'proxy_lThighBend',
            'elbow': 'proxy_lShin',
            'hand': 'proxy_lFoot',
            'preferred_angle': (45, 0, 0),
            'rotation_axis': 'y',

            # Legs are COG-space by default, so the legs don't move if you bend the hip.
            'fk_spaces': ['COGSpace', 'Root', 'WorldSpace'],
            'ik_spaces': ['Root', 'WorldSpace', 'COGSpace', 'pelvis'],

            'ik_origin': 'pelvis',
            'fk_parent': 'pelvis',

            'fk_offsets': [Vector(0,-8,0), Vector(0,0,0), Vector(0,0,0)],
            'fk_size': [0.15, 0.15, 0.1],

            'pv_control_position': (0,0,4),
            'pv_control_rotation': (-135,0,90),

            'is_left': True,
            'default_ik_weight': 1,
            'ik_reverse_pole_vector': True,
        }, {
            'names': ['RightThigh','RightLeg', 'RightFoot'],
            'control_group': 'Leg_Right',
            'hand_control_group': 'Foot_Right',

            'shoulder': 'proxy_rThighBend',
            'elbow': 'proxy_rShin',
            'hand': 'proxy_rFoot',
            'preferred_angle': (45, 0, 0),
            'rotation_axis': 'y',

            'fk_spaces': ['COGSpace', 'Root', 'WorldSpace'],
            'ik_spaces': ['Root', 'WorldSpace', 'COGSpace', 'pelvis'],

            'ik_origin': 'pelvis',
            'fk_parent': 'pelvis',

            'fk_offsets': [Vector(0,-8,0), Vector(0,0,0), Vector(0,0,0)],
            'fk_size': [0.15, 0.15, 0.1],

            'pv_control_position': (0,0,4),
            'pv_control_rotation': (-135,0,90),
            'default_ik_weight': 1,
            'ik_reverse_pole_vector': True,
        }]

        pm.addAttr(self.root_handle, ln='FKColor', type='float3', usedAsColor=True)
        self.fk_color_attr = self.root_handle.attr('FKColor')
        self.fk_color_attr.set((0.88, 0.93, 0))
        lock_trs(self.fk_color_attr, lock='unkeyable')

        for ik in iks:
            self.create_ik_fk(ik)

    def create_ik_fk(self, ik):
        part = ik['names'][1]

        ikfk_group = pm.createNode('transform', name=part + '_IKFK', p=self.rig_internal_node)

        # Create the main IK weight attribute.
        ik_attr_name = ik['names'][1] + 'IK'
        pm.addAttr(self.hip_handle, ln=ik_attr_name, type='float', minValue=0, maxValue=1)
        ik_attr = self.hip_handle.attr(ik_attr_name)
        pm.setAttr(ik_attr, keyable=True)
        pm.setAttr(ik_attr, ik['default_ik_weight'])

        # Create the FK weight attribute, which is simply 1 - the IK weight.
        minus_node = pm.createNode('plusMinusAverage')
        pm.setAttr(minus_node.attr('input1D[0]'), 1)
        pm.connectAttr(ik_attr, minus_node.attr('input1D[1]'))
        pm.setAttr(minus_node.attr('operation'), 2) # subtract
        fk_attr = minus_node.attr('output1D')

        # To allow scripts to work on the whole IK/FK system, create a helper node that has
        # attributes pointing to each control.  This can be used by IK/FK matching scripts,
        # so they can find the IK and FK controls without having to guess based on names.
        # The NodeType attribute is so this node can be identified from the list of nodes
        # connected to .message.
        references_group = pm.createNode('transform', n=ik['names'][1], p=self.references_group)
        lock_trs(references_group)
        pm.addAttr(references_group, ln='nodeType', dt='string')
        references_group.attr('nodeType').set('FKIKReferences')

        def create_user_handle(name, parent, position, ik_handle=False, *args, **kwargs):
            transform, handle = self.create_zeroed_handle(name=name, parent=parent, position=position, *args, **kwargs)
            self.register_node_for_controls(ik['control_group'], transform)

            alignment_node = transform.getParent()

#            if ik.get('is_left'):
#                pm.xform(alignment_node, scale=(-1,-1,-1))

#            if ik.get('is_left'):
#                pm.xform(transform, scale=(-1,-1,-1))

            # For convenience, add an alias to the IK weight for this limb to each control.
            pm.addAttr(transform, ln='IKFK', niceName='IK/FK', proxy=ik_attr.name())
            pm.setAttr(transform.attr('IKFK'), keyable=True)

            # Fade on the active controls.
            source_for_color = transform.attr('IKFK') if ik_handle else fk_attr
            remap_value = pm.createNode('remapValue', n='IKColor' if ik_handle else 'FKColor')
            pm.connectAttr(source_for_color, remap_value.attr('inputValue'))
            pm.setAttr(remap_value.attr('value[0].value_FloatValue'), 0)
            pm.setAttr(remap_value.attr('value[0].value_Position'), 0)
            pm.setAttr(remap_value.attr('value[0].value_Interp'), 1)
            pm.setAttr(remap_value.attr('value[1].value_FloatValue'), 0.5)
            pm.setAttr(remap_value.attr('value[1].value_Position'), 1)
            pm.setAttr(remap_value.attr('value[1].value_Interp'), 1)
            pm.connectAttr(remap_value.attr('outValue'), handle.attr('alpha'))
            
            # When the handle's weight is completely disabled, hide the parent, so we hide the node entirely.
            visibility_cond = pm.createNode('condition', name='Visibility_' + name)
            pm.connectAttr(source_for_color, visibility_cond.attr('firstTerm'))
            pm.setAttr(visibility_cond.attr('colorIfTrueR'), 1)
            pm.setAttr(visibility_cond.attr('colorIfFalseR'), 0)
            pm.setAttr(visibility_cond.attr('secondTerm'), 0.002)
            pm.setAttr(visibility_cond.attr('operation'), 2) # greater than
            pm.connectAttr(visibility_cond.attr('outColorR'), alignment_node.attr('visibility'))

            return transform, handle

        def create_fk_handle(idx, position, axis, parent=None, size=None):
            transform, handle = create_user_handle(ik_handle=False, name='FK_' + ik['names'][idx], parent=parent, size=size, position=position, shape=self.fk_handle_shape)
            self.fk_color_attr.connect(handle.attr('color'), f=True)
            pm.setAttr(handle.attr('borderColor'), (0.5, 0.5, 0))
            lock_translate(transform)

            # Rotate this FK handle to match the orientation of the bone.
            if axis == 'y':
                pm.setAttr(handle.attr('localRotate'), (0,0,90))
            elif axis == 'z':
                pm.setAttr(handle.attr('localRotate'), (0,90,0))

            # Scale the control to match the character.
            length = distance_between_nodes(self.joints[ik['shoulder']], self.joints[ik['elbow']])
            size_scale = length / 27.5

            scale = 1 / Vector(pm.xform(transform, q=True, s=True, ws=True))
            pm.setAttr(handle.attr('localScale'), ik['fk_size'][idx] * length * scale)
            
            if 'fk_offsets' in ik:
                pm.setAttr(handle.attr('localPosition'), size_scale * ik['fk_offsets'][idx])

            return transform, handle

        parent_of_shoulder = self.joints[ik['fk_parent']]
        proxy_shoulder = self.joints[ik['shoulder']]
        proxy_elbow = self.joints[ik['elbow']]
        proxy_hand = self.joints[ik['hand']]

        group = pm.createNode('transform', n='FKIKGroup_' + ik['shoulder'], p=ikfk_group)
        pm.parentConstraint(parent_of_shoulder, group, mo=False)

        # Create a group to hold the FK controls.  This is parented to the collar, so the shoulder handle
        # follows the position of the shoulder.  We do it this way because we can't constrain to the shoulder
        # itself (that would cause a cycle), and we can't put a parent constraint like this on the shoulder
        # handle align transform itself, since we need that for constrain_spaces.
        arm_group = pm.createNode('transform', n=ik['shoulder'] + 'Group', parent=self.hip_handle)
        lock_trs(arm_group, lock='unkeyable')
        pm.parentConstraint(parent_of_shoulder, arm_group, n=ik['shoulder'], mo=False)

        # Create the FK controls.
        fk_shoulder, _ = create_fk_handle(0, self.joints[ik['shoulder']], ik['rotation_axis'], parent=arm_group, size=2)
        fk_elbow, _ = create_fk_handle(1, self.joints[ik['elbow']], ik['rotation_axis'], parent=fk_shoulder, size=2)
        fk_hand, _ = create_fk_handle(2, self.joints[ik['hand']], ik['rotation_axis'], parent=fk_elbow, size=3)

        # The IK reference are named based on the control, so scripts can work on any IK/FK system without
        # having to deal with different names.
        self.add_reference(fk_shoulder, 'FK_1', group=references_group)
        self.add_reference(fk_elbow, 'FK_2', group=references_group)
        self.add_reference(fk_hand, 'FK_3', group=references_group)

        # Add references to the underlying joints, so scripts can read the final pose.
        self.add_reference(self.joints[ik['shoulder']], 'Joint_1', group=references_group)
        self.add_reference(self.joints[ik['elbow']], 'Joint_2', group=references_group)
        self.add_reference(self.joints[ik['hand']], 'Joint_3', group=references_group)

        # For FK, only use rotation from the coordinate space.  Translate always comes from the parent.
        self.constrain_spaces(fk_shoulder.getParent(), ik['fk_spaces'], t=False, r=True, control=fk_shoulder, proxies=[fk_elbow, fk_hand], handle=fk_shoulder)

        # Create joints to receive the IK results.
        ik_shoulder = create_proxy_joint(proxy_shoulder, group, 'IKTarget_' + ik['shoulder'])
        ik_elbow = create_proxy_joint(proxy_elbow, ik_shoulder, 'IKTarget_' + ik['elbow'])
        ik_hand = create_proxy_joint(proxy_hand, ik_elbow, 'IKTarget_' + ik['hand'])
        pm.parentConstraint(parent_of_shoulder, ik_shoulder, mo=True)

        # Add references to the underlying joints, so scripts can read the IK pose without having
        # to change the weight.
        self.add_reference(ik_shoulder, 'IKJoint_1', group=references_group)
        self.add_reference(ik_elbow, 'IKJoint_2', group=references_group)
        self.add_reference(ik_hand, 'IKJoint_3', group=references_group)

        # Create joints to combine FK and IK.
        fkik_shoulder = create_proxy_joint(proxy_shoulder, group, 'FKIK_' + ik['shoulder'])
        fkik_elbow = create_proxy_joint(proxy_elbow, fkik_shoulder, 'FKIK_' + ik['elbow'])
        fkik_hand = create_proxy_joint(proxy_hand, fkik_elbow, 'FKIK_' + ik['hand'])

        # Constrain the three joints to the IK and FK outputs, and connect the weights to the IK/FK weights.
        constraint = pm.parentConstraint(fk_shoulder, ik_shoulder, fkik_shoulder, mo=True)
        connect_constraint_weights(constraint, 0, fk_attr)
        connect_constraint_weights(constraint, 1, ik_attr)
        pm.setAttr(constraint.attr('interpType'), 2) # shortest

        constraint = pm.parentConstraint(fk_elbow, ik_elbow, fkik_elbow, mo=True)
        connect_constraint_weights(constraint, 0, fk_attr)
        connect_constraint_weights(constraint, 1, ik_attr)
        pm.setAttr(constraint.attr('interpType'), 2) # shortest

        constraint = pm.parentConstraint(fk_hand, ik_hand, fkik_hand, mo=True)
        connect_constraint_weights(constraint, 0, fk_attr)
        connect_constraint_weights(constraint, 1, ik_attr)
        pm.setAttr(constraint.attr('interpType'), 2) # shortest

        pm.orientConstraint(fkik_shoulder, proxy_shoulder, mo=True)
        pm.orientConstraint(fkik_elbow, proxy_elbow, mo=True)
        pm.orientConstraint(fkik_hand, proxy_hand, mo=True)

        # Create a group to hold the user IK handle and pole vector controls.
        control_group = pm.createNode('transform', n='IK_' + ik['names'][1], p=self.rig_node)
        lock_trs(control_group)
        pm.editDisplayLayerMembers(self.display_layer_handles, control_group, noRecurse=True)

        # Set the preferredAngle, so the IK knows which way to bend.
        pm.setAttr(ik_elbow.attr('preferredAngle'), ik['preferred_angle'])

        # The user IK handle for the hand:
        hand_handle, _ = create_user_handle(ik_handle=True, name=ik['names'][2], position=ik_hand, parent=control_group, size=3)
#        self.constrain_spaces(hand_handle.getParent(), ik['ik_spaces'], t=True, r=True, control=hand_handle, handle=hand_handle)

        pm.orientConstraint(hand_handle, ik_hand, mo=True)
        self.register_node_for_controls(ik['hand_control_group'], hand_handle)
        self.add_reference(hand_handle, 'IK_Handle', group=references_group)

        # Create the actual user pole vector control.
        hand_pv_handle, handle = create_user_handle(ik_handle=True, name=part + '_Direction', parent=control_group, position=ik_elbow, size=3, shape=self.fk_handle_90deg_shape)
        pm.setAttr(handle.attr('localPosition'), ik['pv_control_position'])
        pm.setAttr(handle.attr('localRotate'), ik['pv_control_rotation'])
        self.add_reference(hand_pv_handle, 'Pole_Vector', group=references_group)

        # The hand control moves with the coordinate space normally.  The pole vector control is
        # constrained differently and doesn't follow the coordinate space directly, but add it as
        # a space switching control so it'll be repositioned when the coordinate space is changed.
        coordinate_space = self.create_space_switcher(hand_handle.getParent(), ik['ik_spaces'], control=hand_handle)
        coordinate_space.add_transform(hand_handle.getParent(), hand_handle, t=True, r=True)
        coordinate_space.add_transform_space_switching_only(hand_pv_handle)
        coordinate_space.add_control(hand_pv_handle)

        # create_user_handle created a positioning node above the handle.  Constrain it to the elbow.
        # If we orient to the shoulder then the control will follow the upper arm, and if we orient to
        # the elbow then it'll follow the lower arm.  Instead, orient 50% to each, so it'll blend the two.
        # This makes it follow the elbow.
        hand_pv_direction = hand_pv_handle.getParent()
        pm.pointConstraint(ik_elbow, hand_pv_direction)
        constraint = pm.orientConstraint(self.joints[ik['shoulder']], hand_pv_direction, mo=True)
        pm.orientConstraint(self.joints[ik['elbow']], hand_pv_direction, mo=True)
        pm.setAttr(constraint.attr('interpType'), 2) # shortest

        # Create a group to put the pole vector control in.  This is only used to correct the double-
        # transform on the control: rotating the control causes the elbow to rotate, which rotates the
        # control.  Fix this by rotating the correct_double_transform by the reverse of the rotation of
        # the control.
        hand_pv_handle_alignment = hand_pv_handle.getParent()
        correct_double_transform = pm.createNode('transform', n=hand_pv_handle.nodeName() + 'Invert', parent=hand_pv_handle.getParent())
        align_node(correct_double_transform, hand_pv_handle)
        pm.parent(hand_pv_handle, correct_double_transform)

        correct_double_transform_multiply = pm.createNode('multiplyDivide', n='Mult_%s_Invert' % hand_pv_handle.nodeName())
        hand_pv_handle.attr('rotate' + ik['rotation_axis'].upper()).connect(correct_double_transform_multiply.attr('input1X'))
        correct_double_transform_multiply.attr('input2X').set(-1)
        correct_double_transform_multiply.attr('outputX').connect(correct_double_transform.attr('rotate' + ik['rotation_axis'].upper()))

        # Create the IK handle for the arm.
        arm_ik_handle, arm_ik_effector = pm.ikHandle(startJoint=ik_shoulder, endEffector=ik_hand, solver='ikRPsolver')
        pm.rename(arm_ik_handle, part + '_IK')
        pm.rename(arm_ik_effector, part + '_Effector')
        pm.parent(arm_ik_handle, ikfk_group)
        arm_ik_handle.attr('poleVector').set(0,0,0)

        self.register_node_for_controls(ik['hand_control_group'], arm_ik_handle)

        # Connect the end effector to the visible handle.
        pm.pointConstraint(hand_handle, arm_ik_handle, mo=True)
 
        # Connect the rotation of the handle to the twist attribute on the IK handle, and
        # lock the other rotations.
        for axis in ('x', 'y', 'z'):
            if axis == ik['rotation_axis']:
                rotation_output = arm_ik_handle.attr('twist')
                if ik.get('ik_reverse_pole_vector'):
                    invert_node = pm.createNode('multiplyDivide')
                    invert_node.attr('input2X').set(-1)
                    invert_node.attr('outputX').connect(rotation_output)
                    rotation_output = invert_node.attr('input1X')

                hand_pv_handle.attr('rotate' + axis.upper()).connect(rotation_output)
            else:
                lock_attr(hand_pv_handle.attr('rotate' + axis.upper()))

        lock_translate(hand_pv_handle)
        lock_scale(hand_pv_handle)

    def create_head_control(self):
        # Create neck and head controls.  Position these far enough apart to be easy to select.  
        # Try to keep the controls out of the way when viewing the face from the front, eg. don't put
        # xray controls at the base or back of the head.  We'll place the neck control at the back
        # of the neck, and try to put the head control at the top of the head.
        neck_group = pm.createNode('transform', n='NeckGroup', parent=self.hip_handle)
        align_node(neck_group, self.joints['neckLower'])
        pm.parentConstraint(self.joints['chestUpper'], neck_group, mo=True)
        lock_trs(neck_group, lock='unkeyable')

        neck_control, handle = self.create_zeroed_handle(name='Neck', parent=neck_group, position=self.joints['neckLower'], shape=self.fk_xy_rot_handle_shape, size=3)
        lock_translate(neck_control)
        pm.orientConstraint(neck_control, self.joints['neckLower'], mo=True)
        self.register_node_for_controls('Head', neck_control)

        self.head_control, handle = self.create_zeroed_handle(name='Head', parent=neck_control, position=self.joints['head'], shape=self.fk_xy_rot_handle_shape, size=3)
        lock_translate(self.head_control)
        log.debug('xxx top %f', self.find_top_of_head())
        y_offset = self.find_top_of_head() - pm.xform(self.head_control, q=True, ws=True, t=True)[1]
#        y_offset -= xy_rot_handle_shape_radius * pm.xform(self.head_control, q=True, s=True, ws=True)[1]

        set_handle_local_position_in_world_space_units(handle, (0, y_offset, 0))
        pm.setAttr(handle.attr('localRotate'), (90, 0, 0))
        pm.orientConstraint(self.head_control, self.joints['head'], mo=True)
        self.register_node_for_controls('Head', self.head_control)

        self.constrain_spaces(neck_control.getParent(), ['chestUpper', 'COGSpace', 'Root', 'WorldSpace'], t=False, r=True, control=neck_control, proxies=[self.head_control], handle=neck_control)

    def create_collar(self):
        # Create collar controls.
        for joint, name in (('rCollar', 'RightCollar'), ('lCollar', 'LeftCollar')):
            transform, handle = self.create_zeroed_handle(name=name, parent=self.hip_handle, position=self.joints[joint], shape=self.fk_xy_rot_handle_shape, size=3)
            pm.setAttr(handle.attr('localPosition'), (-4 if joint == 'rCollar' else 4, 0, 0))
            pm.setAttr(handle.attr('localRotate'), (0, 90 if joint == 'rCollar' else -90, 0))
            pm.parentConstraint(self.joints['chestUpper'], transform.getParent(), mo=True)
            pm.orientConstraint(transform, self.joints[joint], mo=True)
            lock_translate(transform)
            lock_scale(transform)

    def _get_data_path(self):
        return os.path.dirname(__file__) + '/data/'

    def load_handle_shapes(self):
        old_render_layers = set(pm.ls(type='renderLayer'))

        # Importing a file isn't undoable.  Import shapes first, and check if the shape already
        # exists before importing, so undoing a rig leaves us with just the shape.
        path = self._get_data_path() + 'FKHandleShapes.ma'
        if not pm.ls('HandleShapes'):
            nodes = ps.importFile(path, renameAll=False, ignoreVersion=True, returnNewNodes=True)

        # Due to a bug in 2016.5, importing that file will import its defaultRenderLayer, which will cause
        # render setup warnings on everything that references this file.  Delete the extra render layer
        # node.
        new_render_layers = set(pm.ls(type='renderLayer'))
        for node in new_render_layers - old_render_layers:
            pm.delete(node)

        handle_shapes = pm.ls('|HandleShapes')[0]
        pm.setAttr(handle_shapes.attr('visibility'), False)
        pm.setAttr(handle_shapes.attr('hiddenInOutliner'), True)

        self.fk_handle_shape = pm.ls('HandleShapes|FKHandleTorus')[0]
        self.fk_xy_rot_handle_shape = pm.ls('HandleShapes|FKHandleXY')[0]
        self.fk_handle_90deg_shape = pm.ls('HandleShapes|FKHandleCurve')[0]

    def create_spine_ribbon(self):
        # Ribbon spine.  This isn't currently used.  Note that the top chest control currently
        # only has rotation control on the Y axis, so upper body coordinate spaces don't lock
        # correctly eg. when in world space.
        spine_group = pm.createNode('transform', n='Spine', p=self.rig_internal_node)

        spine_joints = ['abdomenLower', 'abdomenUpper', 'chestLower', 'chestUpper']
        spine_nodes = [self.joints[node] for node in spine_joints]

        # Find the world space positions of each spine joint.
        positions = [pm.xform(node,q=True, t=True, ws=True) for node in spine_nodes]

        # Create two curves, and translate their points in each direction.  These are linear
        # curves, so they fit the joints exactly.  We'll rebuild the resulting surface later.
        spine_curve1 = pm.curve(d=1, p=positions)
        spine_curve2 = pm.curve(d=1, p=positions)

        pm.move(-10,0,0, '%s.cv[*]' % spine_curve1.name(), wd=True, ws=True, r=True)
        pm.move(+10,0,0, '%s.cv[*]' % spine_curve2.name(), wd=True, ws=True, r=True)

        # Create a surface between the two curves.
        spine_surface = pm.loft(spine_curve1, spine_curve2, ch=False, u=0, ar=1, d=4, ss=1, rn=0, rsn=True, rb=False)[0]
        spine_surface.rename('SpineRibbon')
        pm.parent(spine_surface, spine_group)

        # We're finished with the curves.  We only used them to create the surface.
        pm.delete(spine_curve1)
        pm.delete(spine_curve2)

        # We've built a linear surface.  Rebuild on U.
        pm.rebuildSurface(spine_surface, ch=False, rpo=True, end=1, kr=0, kcp=0, kc=0, su=len(spine_nodes), du=3, sv=4, dv=3, dir=0)

        u_range = spine_surface.attr('minMaxRangeU').get()
        v_range = spine_surface.attr('minMaxRangeV').get()

        # The rebuilt curve won't have U values matching up with the joints.  Use a closestPointOnSurface
        # node to find the actual U/V values closest to each joint.
        closest_point_node = pm.createNode('closestPointOnSurface')
        pm.connectAttr(spine_surface.attr('local'), closest_point_node.attr('inputSurface'))

        for node in spine_nodes:
            closest_point_node.attr('inPosition').set(pm.xform(node, q=True, t=True, ws=True))
            result_u = closest_point_node.attr('u').get()
            result_v = closest_point_node.attr('v').get()

            # Normalize U and V.
            result_u = (result_u - u_range[0]) / (u_range[1] - u_range[0])
            result_v = (result_v - v_range[0]) / (v_range[1] - v_range[0])

            # Create a follicle that follows the point on the surface closest to this joint.
            follicle_node = pm.createNode('follicle', n=node.nodeName() + 'SpineRibbonFollicleShape')
            follicle_transform = follicle_node.getTransform()
            pm.parent(follicle_transform, spine_group)

            pm.connectAttr(spine_surface.attr('worldMatrix[0]'), follicle_node.attr('inputWorldMatrix'))
            pm.connectAttr(spine_surface.attr('local'), follicle_node.attr('inputSurface'))
            follicle_node.attr('parameterU').set(result_u)
            follicle_node.attr('parameterV').set(result_v)
            pm.connectAttr(follicle_node.attr('outRotate'), follicle_transform.attr('rotate'))
            pm.connectAttr(follicle_node.attr('outTranslate'), follicle_transform.attr('translate'))

            pm.parentConstraint(follicle_transform, node, mo=True)

        pm.delete(closest_point_node)

        int_group = pm.createNode('transform', n='SpineRibbon', p=self.rig_internal_node)
        ribbon_top_joint1 = pm.createNode('joint', n='RibbonTopJoint1', p=int_group)
        align_node(ribbon_top_joint1, self.joints['abdomenLower'])

        ribbon_top_joint2 = pm.createNode('joint', n='RibbonTopJoint2', p=ribbon_top_joint1)
        align_node(ribbon_top_joint2, self.joints['abdomenLower'])
        pm.xform(ribbon_top_joint2, r=True, t=(0,1,0))

        ribbon_bottom_joint1 = pm.createNode('joint', n='RibbonBottomJoint1', p=int_group)
        align_node(ribbon_bottom_joint1, self.joints['chestUpper'])

        ribbon_bottom_joint2 = pm.createNode('joint', n='RibbonBottomJoint2', p=ribbon_bottom_joint1)
        align_node(ribbon_bottom_joint2, self.joints['chestUpper'])
        pm.xform(ribbon_bottom_joint2, r=True, t=(0,-1,0))

        ribbon_middle_joint = pm.createNode('joint', n='RibbonMiddleJoint', p=int_group)
        temp_constraint = pm.parentConstraint(ribbon_top_joint2, ribbon_bottom_joint2, ribbon_middle_joint, mo=False)
        pm.delete(temp_constraint)

        group = pm.createNode('transform', n='Spine', p=self.rig_node)
        top_pos, _ = self.create_zeroed_handle('Spine_Top', position=self.joints['chestUpper'], parent=group, size=2)
        bottom_pos, _ = self.create_zeroed_handle('Spine_Bottom', position=self.joints['abdomenLower'], parent=group, size=2)

        middle_pos = pm.createNode('transform', n='ChestMidPos', parent=group)
        pm.pointConstraint(bottom_pos, top_pos, middle_pos, mo=False)

        self.register_node_for_controls('Spine_Top', top_pos)
        self.register_node_for_controls('Spine_Middle', middle_pos)
        self.register_node_for_controls('Spine_Bottom', bottom_pos)

        bottom_aim = pm.createNode('transform', n='Aim', p=bottom_pos)
        top_aim = pm.createNode('transform', n='Aim', p=top_pos)
        middle_aim = pm.createNode('transform', n='Aim', p=middle_pos)

        top_up = pm.createNode('transform', n='Up', p=bottom_pos)
        pm.xform(top_up, t=(5,0,0))

        bottom_up = pm.createNode('transform', n='Up', p=top_pos)
        pm.xform(bottom_up, t=(5,0,0))

        middle_up = pm.createNode('transform', n='Up', p=middle_pos)
        pm.xform(middle_up, t=(5,0,0))

        pm.pointConstraint(top_up, bottom_up, middle_up)

        chest_mid_offset, _ = self.create_zeroed_handle('ChestMid', parent=middle_aim, size=2)

        pm.aimConstraint(top_pos.getTransform(), bottom_aim, aimVector=(0,1,0), upVector=(1,0,0), worldUpType='object', worldUpObject=top_up)
        pm.aimConstraint(top_pos.getTransform(), middle_aim, aimVector=(0,1,0), upVector=(1,0,0), worldUpType='object', worldUpObject=middle_up)
        pm.aimConstraint(bottom_pos.getTransform(), top_aim, aimVector=(0,-1,0), upVector=(1,0,0), worldUpType='object', worldUpObject=bottom_up)

        # Bind the joints to the curve using distance falloff.  Don't parent the joints until
        # after binding, to avoid a bug where weights are wrong even though bindMethod is 0
        # (distance).  Also, it's not clear how to bind to non-joints without first binding to
        # a dummy joint, then removing it after adding the real transforms.
        curve_skin_cluster = pm.skinCluster(ribbon_top_joint1, ribbon_top_joint2, ribbon_middle_joint, ribbon_bottom_joint1, ribbon_bottom_joint2, spine_surface,
                bindMethod=0, skinMethod=0, toSelectedBones=True, dropoffRate=1)

        pm.parentConstraint(bottom_aim, ribbon_top_joint1)
        pm.parentConstraint(chest_mid_offset.getTransform(), ribbon_middle_joint)
        pm.parentConstraint(top_aim, ribbon_bottom_joint1)

        # Save the chest handle, since we'll need it for pick walk setup later.
        self.chest_handle = top_pos
        
        self.coordinate_spaces['Spine_Top'] = top_pos

        self.constrain_spaces(bottom_pos.getParent(), ['COGSpace', 'Root', 'WorldSpace'], t=True, r=True, control=bottom_pos, handle=bottom_pos)
        self.constrain_spaces(top_pos.getParent(), ['COGSpace', 'Root', 'WorldSpace'], t=True, r=True, control=top_pos, handle=top_pos)

    def create_spine(self):
        # Create the spline IK for the spine.
        spine_handle, spine_effector, spine_curve = pm.ikHandle(startJoint=self.joints['abdomenLower'], endEffector=self.joints['chestUpper'], solver='ikSplineSolver')
        pm.rename(spine_handle, 'Spine_IK')
        pm.rename(spine_effector, 'Spine_Effector')
        pm.rename(spine_curve, 'Spine_Curve')
        pm.parent(spine_handle, self.rig_internal_node)
        pm.parent(spine_curve, self.rig_internal_node)

        disable_inherit_transform(spine_curve)

        # Figure out where the extra joint between the spine controls will go.
        temp_transform = pm.createNode('transform')
        pm.xform(temp_transform, ws=True, s=pm.xform(self.joints['abdomenLower'], q=True, ws=True, s=True))
        temp_constraint = pm.pointConstraint(self.joints['abdomenLower'], self.joints['chestUpper'], temp_transform)
        pm.delete(temp_constraint)
        offset_by = Vector(0,0,5)
        offset_by = multiply_vector(offset_by, Vector(*pm.xform(temp_transform, q=True, s=True, ws=True)))
        pm.xform(temp_transform, t=offset_by, ws=True, r=True)

        # Create three spine controls.  The first and last are actually bound to the IK curve, and the middle is
        # just a helper.
        abdomen_handle, _ = self.create_zeroed_handle('Spine_Bottom', position=self.joints['abdomenLower'], parent=self.hip_handle, size=2)
        chest_mid_handle, _ = self.create_zeroed_handle('ChestMid', position=temp_transform, parent=abdomen_handle, size=2)
        chest_handle, _ = self.create_zeroed_handle('Spine_Top', position=self.joints['chestUpper'], parent=abdomen_handle, size=3)
        chest_mid_handle.attr('visibility').set(False)

        self.register_node_for_controls('Spine_Top', chest_handle)
        self.register_node_for_controls('Spine_Middle', chest_mid_handle)
        self.register_node_for_controls('Spine_Bottom', abdomen_handle)

        self.create_coordinate_space('Spine_Bottom', abdomen_handle)

        # Save the chest handle, since we'll need it for pick walk setup later.
        self.chest_handle = chest_handle

        self.constrain_spaces(chest_handle.getParent(), ['Spine_Bottom', 'COGSpace', 'Root', 'WorldSpace'], control=chest_handle, handle=chest_handle)

        pm.delete(temp_transform)

        # Handles are scale locked by default.  We need to temporarily unparent the handles, and if scale
        # is locked the scale will be wrong, so we have to temporarily unlock scale too.
        lock_scale(abdomen_handle, lock='keyable')
        lock_scale(chest_mid_handle, lock='keyable')
        lock_scale(chest_handle, lock='keyable')

        # Temporarily unparent the two controls that we'll bind to the skinCluster.  If we leave them
        # parented, the skinCluster will generate bad weights, even though skinMethod=0 should tell it
        # to ignore hierarchy.
        abdomen_handle_parent = abdomen_handle.getParent()
        chest_handle_parent = chest_handle.getParent()
        pm.parent(abdomen_handle, None)
        pm.parent(chest_handle, None)

        # Create a temporary joint.  We'll only use this to create the skinCluster.
        temp_joint = pm.createNode('joint')

        # Bind the joints to the curve using distance falloff.  Don't parent the joints until
        # after binding, to avoid a bug where weights are wrong even though bindMethod is 0
        # (distance).  Also, it's not clear how to bind to non-joints without first binding to
        # a dummy joint, then removing it after adding the real transforms.
        curve_skin_cluster = pm.skinCluster(temp_joint, spine_curve, bindMethod=0, skinMethod=0, toSelectedBones=True, dropoffRate=1)
        pm.skinCluster(curve_skin_cluster, edit=True, addInfluence=abdomen_handle)
#        pm.skinCluster(curve_skin_cluster, edit=True, addInfluence=chest_mid_handle)
        pm.skinCluster(curve_skin_cluster, edit=True, removeInfluence=temp_joint)
        pm.skinCluster(curve_skin_cluster, edit=True, addInfluence=chest_handle)
        pm.skinPercent(curve_skin_cluster, spine_curve, resetToDefault=True)
        pm.delete(temp_joint)
        
        # Restore the parents.
        pm.parent(abdomen_handle, abdomen_handle_parent)
        pm.parent(chest_handle, chest_handle_parent)

        # Restore scale lock.
        lock_scale(abdomen_handle)
        lock_scale(chest_mid_handle)
        lock_scale(chest_handle)

        # Lock uneeded attributes.  We need to do this after reparenting, since locked attributes break parenting.
        lock_translate(abdomen_handle)
        lock_translate(chest_mid_handle)

        # Create nodes to figure out the twist.  The chest twist is the Y rotation on the chest, but a
        # child of the abdomen to get the rotation in that coordinate space.
        abdomen_twist_node = pm.createNode('transform', n='Abdomen_Twist', p=self.hip_handle)
        align_node(abdomen_twist_node, abdomen_handle)
        lock_trs(abdomen_twist_node, lock='unkeyable')
        pm.parentConstraint(abdomen_handle, abdomen_twist_node, mo=True)
        abdomen_twist_node.attr('rotateOrder').set(1) # yzx, so twist is first

        chest_twist_node = pm.createNode('transform', n='Chest_Twist', p=abdomen_handle)
        align_node(chest_twist_node, chest_handle)
        lock_trs(chest_twist_node, lock='unkeyable')
        pm.parentConstraint(chest_handle, chest_twist_node, mo=True)
        chest_twist_node.attr('rotateOrder').set(1) # yzx, so twist is first

        add_twist = pm.createNode('plusMinusAverage', n='AddTwist')
        add_twist.attr('operation').set(2) # subtract
        pm.connectAttr(chest_twist_node.attr('rotateY'), add_twist.attr('input1D[0]'))
        pm.connectAttr(abdomen_twist_node.attr('rotateY'), add_twist.attr('input1D[1]'))
        pm.connectAttr(add_twist.attr('output1D'), spine_handle.attr('twist'))
        pm.connectAttr(abdomen_twist_node.attr('rotateY'), spine_handle.attr('roll'))

        pm.orientConstraint(chest_handle, self.joints['chestUpper'], mo=True)

        # Set up spine twist.
        # pm.setAttr(spine_handle.attr('dTwistControlEnable'), True)
        # pm.setAttr(spine_handle.attr('dWorldUpType'), 4) # Object Rotation Up (Start/End)
        # pm.setAttr(spine_handle.attr('dForwardAxis'), 2) # +Y
        # pm.setAttr(spine_handle.attr('dWorldUpAxis'), 3) # +Z
        # pm.setAttr(spine_handle.attr('dWorldUpVector'), (0,0, 1))
        # pm.setAttr(spine_handle.attr('dWorldUpVectorEnd'), (0,0, 1))
        # pm.connectAttr(abdomen_handle.attr('worldMatrix[0]'), spine_handle.attr('dWorldUpMatrix'))
        # pm.connectAttr(chest_handle.attr('worldMatrix[0]'), spine_handle.attr('dWorldUpMatrixEnd'))

    def rig_figure(self, node):
        log.debug('Rigging: %s', node)

        self.handles = {}
        self.control_target_nodes = { name: [] for name in self.node_names_for_controls }
        self.top_node = node

        # Find the joints in the figure by their asset name.  The asset name is what we'll
        # use to figure out which joint is which.
        self.joints = get_joints_in_figure(node)

        # The figure has a root node named after the import.  That makes sense when importing
        # generic scenes, but we only have one character, so this makes character scenes consistent
        # with each other.
        pm.rename(node, 'Top')

        for node in self.joints.values():
            optimize_skeleton(node)

        # Load the shapes used by control handles.
        #
        # Maya can't undo this.
        self.load_handle_shapes()

        # An overall scaling ratio for controls:
        self.overall_scale = pm.xform(self.joints['neckLower'], q=True, t=True, ws=True)[1] / 150

#        self.straighten_poses()

        self.create_display_layers()
        self.create_base()

        self.create_universal_rig_parts()
        if self.humanik:
            self.create_humanik()
        else:
            self.create_rig_parts()

        self.finalize_rig()

    def create_universal_rig_parts(self):
        # Create rigs that we want whether we're in direct riggnig mode or HumanIK.
        pm.addAttr(self.root_handle, ln='faceColor', type='float3', usedAsColor=True)
        self.face_color = self.root_handle.attr('faceColor')
        self.face_color.set((0, 0.5, 1))
        lock_trs(self.face_color, lock='unkeyable')

        self.create_eyes()
        self.create_tongue()
        self.create_face_control_positions()
        self.create_controls()
        self.create_twist_proxies()
        self.create_attachment_points()
        self.create_breasts()

        # We can either use our own finger/toe rigs or use HumanIK's, even if we're using a HIK
        # rig.  The advantage of using ours with HIK is that HIK's is very slow and can drop it
        # from 40 FPS to 20, but if you're using this with mocap import/retargetting you'll want
        # to use HIK's.
        if not self.humanik or self.no_humanik_finger_rigs:
            self.create_fingers()

    def create_rig_parts(self):
        #self.create_spine_ribbon()
        self.create_spine()

        self.create_collar()
        self.create_head_control()
        self.create_arms_and_legs()

    # To output these constants:
    # for x in xrange(250):
    #     print x, mel.eval('GetHIKNodeName(%i)' % x)
    _hik_joint_map = {
        'hip': 1,

        # Spine:
        'abdomenLower': 8,
        'abdomenUpper': 23,
        'chestLower': 24,
        'chestUpper': 25,

        'neckLower': 20,
    #    'neckUpper': 32,
        'head': 15,

        'lCollar': 18,
        'rCollar': 19,

        'proxy_rShldrBend': 12,
        'proxy_lShldrBend': 9,

        'proxy_lForearmBend': 10,
        'proxy_rForearmBend': 13,

        'proxy_rHand': 14,
        'proxy_lHand': 11,

        'proxy_lThighBend': 2,
        'proxy_rThighBend': 5,

        'proxy_lShin': 3,
        'proxy_rShin': 6,

        'proxy_lFoot': 4,
        'proxy_rFoot': 7,
    }

    _hik_joint_map_fingers = {
        'lThumb1': 50,
        'lThumb2': 51,
        'lThumb3': 52,
        'lThumb3_End': 53,
        'lCarpal1': 147,
        'lIndex1': 54,
        'lIndex2': 55,
        'lIndex3': 56,
        'lIndex3_End': 57,
        'lCarpal2': 148,
        'lMid1': 58,
        'lMid2': 59,
        'lMid3': 60,
        'lMid3_End': 61,
        'lCarpal3': 149,
        'lRing1': 62,
        'lRing2': 63,
        'lRing3': 64,
        'lRing3_End': 65,
        'lCarpal4': 150,
        'lPinky1': 66,
        'lPinky2': 67,
        'lPinky3': 68,
        'lPinky3_End': 69,

        'rThumb1': 74,
        'rThumb2': 75,
        'rThumb3': 76,
        'rThumb3_End': 77,
        'rCarpal1': 153,
        'rIndex1': 78,
        'rIndex2': 79,
        'rIndex3': 80,
        'rIndex3_End': 81,
        'rCarpal2': 154,
        'rMid1': 82,
        'rMid2': 83,
        'rMid3': 84,
        'rMid3_End': 85,
        'rCarpal3': 155,
        'rPinky1': 90,
        'rPinky2': 91,
        'rPinky3': 92,
        'rPinky3_End': 93,
        'rCarpal4': 156,
        'rRing1': 86,
        'rRing2': 87,
        'rRing3': 88,
        'rRing3_End': 89,

    #    'lMetatarsals': 16,
    #    'rMetatarsals': 17,

        # Watch out: the names for "big toe" and "extra toe" are reversed in HumanIK.
        'lBigToe': 118,
        'lBigToe_2': 119,
        'lBigToe_2_End': 120,
        'lSmallToe1': 102,
        'lSmallToe1_2': 103,
        'lSmallToe1_2_End': 104,
        'lSmallToe2': 106,
        'lSmallToe2_2': 107,
        'lSmallToe2_2_End': 108,
        'lSmallToe3': 110,
        'lSmallToe3_2': 111,
        'lSmallToe3_2_End': 112,
        'lSmallToe4': 114,
        'lSmallToe4_2': 115,
        'lSmallToe4_2_End': 116,

        'rBigToe': 142,
        'rBigToe_2': 143,
        'rBigToe_2_End': 144,
        'rSmallToe1': 126,
        'rSmallToe1_2': 127,
        'rSmallToe1_2_End': 128,
        'rSmallToe2': 130,
        'rSmallToe2_2': 131,
        'rSmallToe2_2_End': 132,
        'rSmallToe3': 134,
        'rSmallToe3_2': 135,
        'rSmallToe3_2_End': 136,
        'rSmallToe4': 138,
        'rSmallToe4_2': 139,
        'rSmallToe4_2_End': 140,
    }

    def create_humanik(self):
        node = self.top_node

        # Note that we don't use HIK roll bones.  They're buggy (the rotation manipulators get screwy
        # when they're in use), and it's simpler to use our twist joints, so HIK never sees them at all.
        log.debug('Creating HumanIK control rig for %s', node)
           
        try:
            # Unfortunately, the HIK commands don't try to be usable by external scripts, so we have to make
            # a bunch of undocumented MEL calls to do this.

            # The scripts assume the HumanIK interface has been opened.
            mel.eval('HIKCharacterControlsTool')

            character_node = mel.eval('hikCreateCharacter("HIK")')
            mel.eval('hikUpdateCharacterList()')
            mel.eval('hikSetCurrentCharacter("%s")' % character_node)

            joint_map = dict(self._hik_joint_map)
            if not self.no_humanik_finger_rigs:
                joint_map.update(self._hik_joint_map_fingers)
            for joint_asset_id, humanik_joint_id in joint_map.items():
                joint = self.joints[joint_asset_id]
                mel.eval('setCharacterObject("%s", "%s", %i, 0)' % (joint, character_node, humanik_joint_id))

            if not mel.eval('hikValidateSkeleton("%s")' % character_node):
                log.info('HumanIK skeleton failed to validate: %s', character_node)
                return
            
            # Lock the skeleton before changing properties, or the first time we lock it it'll overwrite
            # our changes.
            mel.eval('hikCheckDefinitionLocked("%s")' % character_node)

            self._estimate_hik_floor_contact_settings(character_node)

            mel.eval('hikCreateControlRig()')
            mel.eval('hikSetRigIkLookAndFeel("%s", "Stick")' % character_node)
            mel.eval('hikSetRigFkLookAndFeel("%s", "Stick")' % character_node)

            # Move the HIK node underneath Rig.
            ref_node = pm.PyNode('HIK_Ctrl_Reference')
            pm.parent(ref_node, self.rig_node)
            disable_inherit_transform(ref_node)

            # Add the HIK controls to display_layer_handles.
            pm.editDisplayLayerMembers(self.display_layer_handles, ref_node)
            
        except RuntimeError as e:
            # This API is brittle, so log errors and keep going.
            log.error('Runtime error creating HumanIK control rig: %s', e, exc_info=True)
            return
       
    def _estimate_hik_floor_contact_settings(self, character_node):
        # When we lock the skeleton, HIK tries to guess the dimensions of the hands and feet for
        # floor contact.  Most of this is derived from the distance between the upper leg and ankle.
        # We can make better guesses since we know more about the skeleton.
        #
        # It's better to slightly underestimate than overestimate, to give a slight interpenetration
        # with the floor rather than having the character's toes float.
        #
        # Note that we have two similar joints, rFoot and rHeel.  rFoot is where we actually put the
        # HIK foot joint, so it's where HIK thinks the heel is.  rHeel is for skinning just the heel
        # which HIK doesn't control, so its position doesn't matter here, but its end joint is where
        # the end of the heel is.
        #
        # These estimations assume that the skeleton has been straighted with straighten_poses().
        hik_properties_node = mel.eval('hikGetProperty2StateFromCharacter("%s")' % character_node)
        hik_properties_node = pm.PyNode(hik_properties_node)

        def joint_distance(joint1, joint2, axis):
            pos1 = pm.xform(joint1, q=True, ws=True, t=True)
            pos2 = pm.xform(joint2, q=True, ws=True, t=True)
            return pos1[axis] - pos2[axis]

        # FootBackToAnkle is the distance from the back of the foot to the ankle, measuring straight
        # back, not to the outer corner of the heel where rHeel's end joint is.
        foot_back_to_ankle = joint_distance(self.joints['rFoot'], self.joints['rHeel_End'], 2)

        # Reduce the length slightly, so it sits slightly inside the heel.
        foot_back_to_ankle *= 0.9
        pm.setAttr(hik_properties_node.attr('FootBackToAnkle'), foot_back_to_ankle)

        # Estimate the distance from the heel to the inner and outer edge of the foot.
        # For the outside, use the distance from the heel to the small toe on the X axis.
        # The foot extends beyond that and we could estimate it with the distance between
        # the toe and the next toe, but we want to underestimate a little anyway for floor
        # contact to behave correctly.
        outer_distance = joint_distance(self.joints['rFoot'], self.joints['rSmallToe4'], 0)
        pm.setAttr(hik_properties_node.attr('FootOutToAnkle'), outer_distance)

        inner_distance = joint_distance(self.joints['rBigToe'], self.joints['rFoot'], 0)
        pm.setAttr(hik_properties_node.attr('FootInToAnkle'), inner_distance)

        # Set the middle position to the distance between the ankle and the first small toe
        # joint on the Z axis.  This is the position used when contact type is set to "ankle",
        # and makes the foot make contact just before the toes.  This is useful when toe
        # contact is turned on.
        middle_distance = joint_distance(self.joints['rSmallToe4'], self.joints['rFoot'], 2)
        pm.setAttr(hik_properties_node.attr('FootMiddleToAnkle'), middle_distance)

        # Set the end position to the end of the big toe.
        front_distance = joint_distance(self.joints['rBigToe_2_End'], self.joints['rSmallToe4'], 2)
        pm.setAttr(hik_properties_node.attr('FootFrontToMiddle'), front_distance)

        # Now set up the hand contacts.  This is sensitive to the initial positioning set up
        # by straighten_poses.  If the hands are at an angle, the hand contacts will still
        # start axis-aligned.
        inner_distance = joint_distance(self.joints['rIndex1'], self.joints['rHand'], 2)
        pm.setAttr(hik_properties_node.attr('HandInToWrist'), inner_distance)

        outer_distance = joint_distance(self.joints['rHand'], self.joints['rPinky1'], 2)
        pm.setAttr(hik_properties_node.attr('HandOutToWrist'), outer_distance)

        middle_distance = joint_distance(self.joints['rHand'], self.joints['rRing1'], 0)
        pm.setAttr(hik_properties_node.attr('HandMiddleToWrist'), middle_distance)

        front_distance = joint_distance(self.joints['rRing1'], self.joints['rRing3_End'], 0)
        pm.setAttr(hik_properties_node.attr('HandFrontToMiddle'), front_distance)

        # We don't have a good measurement for wrist to bottom of hand, so use one of the other
        # hand measurements and hope that hands are proportional to each other.
        bottom_distance = middle_distance * 0.2
        pm.setAttr(hik_properties_node.attr('HandBottomToWrist'), bottom_distance)
           
    def create_base(self):
        # create_handle will connect the color to self.control_color, but we haven't created it yet.
        # We'll create the attribute once we create the root node.
        self.control_color_attr = None

        # Create the "Rig" node.  This should hold all nodes that the user might want to keyframe.
        # Anything inside it that shouldn't be keyed by default if the hierarchy is added to a
        # character set should be set to nonkeyable, such as the rig node itself.
        self.rig_node = pm.createNode('transform', n='Rig', p=self.top_node)
        lock_trs(self.rig_node, lock='unkeyable')
        lock_scale(self.rig_node)

        # RigInternal contains nodes that we don't need parented elsewhere, and that don't need
        # to be manipulated by the user.
        self.rig_internal_node = pm.createNode('transform', n='RigInternal', p=self.top_node)
        pm.setAttr(self.rig_internal_node.attr('inheritsTransform'), 0)
        pm.setAttr(self.rig_internal_node.attr('visibility'), 0)

        # Reference nodes are placed in here.  These are dummy nodes that just have references to
        # parts of the rig via message attributes, to allow them to be located by scripts later.
        #
        # Note that these are intended to be used from scripts, but not edited, so they're inside
        # RigInternal.
        self.references_group = pm.createNode('transform', n='RigReferences', p=self.rig_internal_node)
        lock_trs(self.references_group)

        # Create the root transform.
        #
        # For now, just make this a transform and lock it.  This seems redundant with the hip.
        #self.root_handle = pm.createNode('transform', n='Root', p=self.rig_node)
        self.root_handle, root_handle_control = self.create_handle('Root', parent=self.rig_node, size=3, connect_color=False)
        pm.editDisplayLayerMembers(self.display_layer_handles, self.root_handle, noRecurse=True)

        # Create an attribute to control the main rig handle color.
        pm.addAttr(self.root_handle, ln='controlColor', type='float3', usedAsColor=True)
        self.control_color_attr = self.root_handle.attr('controlColor')
        self.control_color_attr.set((0.38, 0, 0))
        lock_trs(self.control_color_attr, lock='unkeyable')

        if self.humanik:
            # For HumanIK, we don't want our root control, but keep the transform for attributes
            # to live on.
            pm.delete(root_handle_control)
        else:
            # Connect the color to the root node itself.  Other nodes will have this automatically, but we
            # create the root control before the attribute for the color, which is on that handle.
            self.control_color_attr.connect(root_handle_control.attr('color'))

        # Create the hip transform.  Place it between the thigh joints, since it gives a more
        # useful pivot than the actual hip.
        temp_node = pm.createNode('transform')
        pm.xform(temp_node, ws=True, s=pm.xform(self.joints['hip'], q=True, ws=True, s=True))
        thigh_y = pm.xform(self.joints['lThighBend'], q=True, t=True, ws=True)[1]
        pm.xform(temp_node, ws=True, t=(0, thigh_y, 0))
        self.hip_handle, _ = self.create_zeroed_handle('Hip', parent=self.root_handle, position=temp_node, size=3)
        pm.delete(temp_node)
        self.register_node_for_controls('Hip', self.hip_handle)
        pm.parentConstraint(self.hip_handle, self.joints['hip'], mo=True)
#        self.joints['root'] = self.hip_handle

        # Create our base coordinate spaces.
        self.coordinate_spaces_group = pm.createNode('transform', n='CoordinateSpaces', p=self.rig_internal_node)
        self.coordinate_spaces = {}
        self.coordinate_spaces['WorldSpace'] = pm.createNode('transform', n='WorldSpace', p=self.coordinate_spaces_group)
        self.create_coordinate_space('COGSpace', self.hip_handle)
        self.create_coordinate_space('Root', self.root_handle)
#        self.create_coordinate_space('Hip', self.hip_handle)

#        self.coordinate_spaces['COGSpace'] = pm.createNode('transform', n='COGSpace', p=self.coordinate_spaces_group)
#        pm.parentConstraint(self.hip_handle, self.coordinate_spaces['COGSpace'], mo=False)

        # Create a couple helper coordinate spaces.  These can be constrained to external objects, to allow constraining
        # parts of the rig without putting a bunch of constraints on the handles themselves.
        extra_coordinate_spaces_group = pm.createNode('transform', n='ExtraCoordinateSpaces', p=self.rig_node)
        extra_coordinate_spaces_group.attr('inheritsTransform').set(0)
        lock_trs(extra_coordinate_spaces_group, lock='unkeyable')

        for name in ('CustomSpace1', 'CustomSpace2'):
            self.coordinate_spaces[name], handle = self.create_handle(name, parent=extra_coordinate_spaces_group, size=2)
            handle.attr('shape').set(1)
            handle.attr('borderColor').set((0, 0, 0))
            self.coordinate_spaces[name].attr('visibility').set(0)

            # Even though this is a user control, make it nonkeyable.  This is intended to be parent
            # constrained to something else, and if it's keyable it's be added to the character set
            # with everything else, which will cause pairBlends to be added when it's parent constrained.
            lock_trs(self.coordinate_spaces[name], lock='unkeyable')

    @classmethod
    def get_meshes_in_figure(cls, node):
        # The meshes in a figure are either an immediate child of the top node, or grouped inside
        # a "Meshes" group.
        meshes = pm.ls(node.longName() + '|Meshes')
        if meshes:
            node = meshes

        # listRelatives is wonky, so this is annoying.  Find all transforms immediately underneath
        # node, then narrow it down to transforms that contain at least one shape.  Don't use ad=True
        # since that will recurse into the whole hierarchy (which might include other figures).
        transforms = [child for child in pm.listRelatives(node)]
        transforms = [t for t in transforms if pm.listRelatives(t, shapes=True)]
        return transforms #[child.getTransform() for child in pm.listRelatives(node, shapes=True)]

    def create_display_layers(self):
        # Create display layers.  Do these all at once, to make it easier to set up their order.  We could
        # set displayOrder, but the numbering is weird (higher numbers are first), so just create layers
        # in reverse order from what we want displayed.

        # Create a display layer for figures following this one.
        display_layer = None
        for following_figure in reversed(sorted(find_figures_in_scene(only_following=self.top_node), key=lambda item: item.nodeName())):
            # Only create display layers for followers that have geometry.
            following_meshes = self.get_meshes_in_figure(following_figure)
            if not following_meshes:
                continue

            if display_layer is None:
                display_layer = pm.createDisplayLayer(name='Props', empty=True)
            pm.editDisplayLayerMembers(display_layer, *following_meshes, noRecurse=True)

        # Create a display layer for the main figure's mesh.
        meshes = self.get_meshes_in_figure(self.top_node)
        display_layer = pm.createDisplayLayer(name='Mesh', empty=True)
        pm.editDisplayLayerMembers(display_layer, *meshes, noRecurse=True)

        # Create a display layer for controls.
        self.controls_layer = pm.createDisplayLayer(name='ControlsLayer', empty=True)

        # Create a display layer for the handles.
        self.display_layer_handles = pm.createDisplayLayer(name='HandlesLayer', empty=True)

        # Create a separate layer for the digits.  These have a lot of tiny controls which
        # currently slow down the viewport due to a Maya bug.
        self.display_layer_digits = pm.createDisplayLayer(name='DigitsLayer', empty=True)

    def create_fingers(self):
        # Is there any benefit to having controls for the carpal joints, or are they just noise?
        fingers = [
            ('rHand', ['rIndex1', 'rIndex2', 'rIndex3'], (0, 90, 0)),
            ('rHand', ['rMid1', 'rMid2', 'rMid3'], (0, 90, 0)),
            ('rHand', ['rRing1', 'rRing2', 'rRing3'], (0, 90, 0)),
            ('rHand', ['rPinky1', 'rPinky2', 'rPinky3'], (0, 90, 0)),
            ('rHand', ['rThumb1', 'rThumb2', 'rThumb3'], (0, 90, 0)),
            ('lHand', ['lIndex1', 'lIndex2', 'lIndex3'], (0, -90, 0)),
            ('lHand', ['lMid1', 'lMid2', 'lMid3'], (0, -90, 0)),
            ('lHand', ['lRing1', 'lRing2', 'lRing3'], (0, -90, 0)),
            ('lHand', ['lPinky1', 'lPinky2', 'lPinky3'], (0, -90, 0)),
            ('lHand', ['lThumb1', 'lThumb2', 'lThumb3'], (0, -90, 0)),
            ('lMetatarsals', ['lBigToe', 'lBigToe_2'], (0, 180, 0)),
            ('lMetatarsals', ['lSmallToe1', 'lSmallToe1_2'], (0, 180, 0)),
            ('lMetatarsals', ['lSmallToe2', 'lSmallToe2_2'], (0, 180, 0)),
            ('lMetatarsals', ['lSmallToe3', 'lSmallToe3_2'], (0, 180, 0)),
            ('lMetatarsals', ['lSmallToe4', 'lSmallToe4_2'], (0, 180, 0)),
            ('rMetatarsals', ['rBigToe', 'rBigToe_2'], (0, 180, 0)),
            ('rMetatarsals', ['rSmallToe1', 'rSmallToe1_2'], (0, 180, 0)),
            ('rMetatarsals', ['rSmallToe2', 'rSmallToe2_2'], (0, 180, 0)),
            ('rMetatarsals', ['rSmallToe3', 'rSmallToe3_2'], (0, 180, 0)),
            ('rMetatarsals', ['rSmallToe4', 'rSmallToe4_2'], (0, 180, 0)),
        ]
        
        parents = {}

        pm.addAttr(self.root_handle, ln='FingerColor', type='float3', usedAsColor=True)
        finger_color = self.root_handle.attr('FingerColor')
        finger_color.set((0, 0.3, .8))
        lock_trs(finger_color, lock='unkeyable')

        for constrained_to, finger, handle_rot in fingers:
            parent = parents.get(constrained_to)
            if parent is None:
                parent = pm.createNode('transform', n='Digits_' + constrained_to, parent=self.hip_handle)
                pm.editDisplayLayerMembers(self.display_layer_digits, parent, noRecurse=True)
                pm.parentConstraint(self.joints[constrained_to], parent)
                lock_trs(parent, lock='unkeyable')
                parents[constrained_to] = parent
            
            for joint in finger:
                # If there are already connections here, remove them.  The toes are often connected
                # to a joint that controls all of the toes, which we won't use.  If we don't connect
                # this we'll get an unwanted pairBlend node (or an error if those are disabled in
                # settings).
                disconnectIncomingConnections(self.joints[joint], ro=True)
                transform, handle = self.create_zeroed_handle(name=joint, parent=parent, position=self.joints[joint], shape=self.fk_xy_rot_handle_shape, size=.5)
                pm.orientConstraint(transform, self.joints[joint], mo=True)
                handle.attr('localRotate').set(*handle_rot)
                finger_color.connect(handle.attr('color'), f=True)
                lock_translate(transform)
                parent = transform

    def create_breasts(self):
        for control_name, joint_name, end_joint_name in (('Breast_Right', 'rPectoral', 'rPectoral_End'), ('Breast_Left', 'lPectoral', 'lPectoral_End')):
            # Create a group to align us to the joint and parent to the chest.
            joint = self.joints[joint_name]
            end_joint = self.joints[end_joint_name]
            pectoral_group = pm.createNode('transform', n=control_name + 'Group', p=self.rig_node)
            align_node(pectoral_group, joint)
            pm.parentConstraint(self.joints['chestLower'], pectoral_group, mo=True)
            lock_trs(pectoral_group, lock='unkeyable')
            pm.editDisplayLayerMembers(self.display_layer_handles, pectoral_group, noRecurse=True)

            # If correctives are enabled, there may be a connection into the pectoral joint
            # from the collarbones.  Maintain what that's doing, but move it into a helper group
            # so we can put controls on top of it.  Create the extra group even if there are no
            # connections, so the node structure stays the same either way.
            corrective_node = pm.createNode('transform', n=control_name + '_Corrective', p=pectoral_group)
            lock_trs(corrective_node, lock='unkeyable')

            conns = {}
            for attr in ('rotateX', 'rotateY', 'rotateZ'):
                input_conns = pm.listConnections(joint.attr(attr), s=True, d=False, p=True)
                if not input_conns:
                    continue

                pm.connectAttr(input_conns[0], corrective_node.attr(attr))
                pm.disconnectAttr(input_conns[0], joint.attr(attr))
               
            # Create a dummy node.  This is a placeholder for dynamics.
            dynamics_node = pm.createNode('transform', n=control_name + '_Dynamics', p=corrective_node)
            lock_trs(dynamics_node, lock='unkeyable')

            # Create the user control.
            transform, handle = self.create_zeroed_handle(name=control_name, parent=dynamics_node, position=dynamics_node, shape=self.fk_xy_rot_handle_shape, size=2)
            lock_translate(transform)
            
            self.register_node_for_controls(control_name, transform)

            pm.setAttr(handle.attr('localRotate'), (0,180,0))
            distance = distance_between_nodes(end_joint, joint) * 0.75
            set_handle_local_position_in_world_space_units(handle, (0,0,distance))

            pm.orientConstraint(transform, joint, mo=True)

            # Hide these controls by default.  They just look goofy when you're not using them.
            transform.attr('visibility').set(0)

    def create_eyes(self):
        # Create controls for the eyes and eyelids.
        #
        # For primary eyelid control, we don't control eyelid joints directly.  The weighting for each
        # eyelid joint is part of a modifier, and modifiers can apply corrective blendshapes to the
        # eyelids.  To control eyelids, these modifiers must be loaded:
        #
        # eCTRLEyelidsUpperUpDownL
        # eCTRLEyelidsUpperUpDownR
        # eCTRLEyelidsLowerUpDownL
        # eCTRLEyelidsLowerUpDownR
        #
        # Soft eyelid control (eyelids moving when the eyes move) is also part of a modifier.  We assume
        # all outputs from the eye joint rotation go to soft eyelids, and redirect them to separate controls
        # to allow us to turn it on and off.
        #
        # Create the eye handle.  This will control the eyes, and hold properties for the eyes and eyelids.
        # This will be reparented and set up further when we create the eye rig.  We're just creating it
        # now so we can start putting attributes on it.
        self.eye_controls, self.eye_controls_handle = self.create_handle('Eyes', p=self.rig_node, size=2)
        pm.editDisplayLayerMembers(self.display_layer_handles, self.eye_controls, noRecurse=True)

        pm.addAttr(self.eye_controls, ln='SoftEyes', minValue=0, maxValue=1, defaultValue=1)
        soft_eye_value = self.eye_controls.attr('SoftEyes')
        pm.setAttr(soft_eye_value, keyable=True)

        # Set up soft eyelids.  Most of the work for this is done by existing DSON modifiers.
        for side, joint_name in (('Left', 'lEye'), ('Right', 'rEye')):
            eye = self.joints[joint_name]

            # Create an origin node, to put us in the same coordinate space as the eye joint.
            direction_origin = pm.createNode('transform', n='SoftEyeOrigin_' + eye.nodeName(), p=self.rig_internal_node)
            pm.parentConstraint(eye.getParent(), direction_origin, mo=False)

            # Create a dummy node with no rotation.  If soft eye is disabled, the orient constraint below
            # will weight towards this to turn it off.  "Rest position" isn't actually usable, since it's
            # ignored until weight is completely zero and then snaps to rest instead of actually weighting.
            direction_zero = pm.createNode('joint', n='SoftEyeNoRotation_' + eye.nodeName(), p=direction_origin)
            align_node(direction_zero, eye)

            # direction_node receives the direction to apply to the soft eye inputs.
            direction_node = pm.createNode('joint', n='SoftEyeDirection_' + eye.nodeName(), p=direction_origin)
            align_node(direction_node, eye)

            # Constrain the soft eye rotation towards the eye joint and the zero joint.
            constraint = pm.orientConstraint(direction_zero, eye, direction_node)

            # Set constraint.direction_zero_weight = 1 - constraint.eye_weight.
            minus_node = pm.createNode('plusMinusAverage')
            pm.setAttr(minus_node.attr('operation'), 2) # subtract
            pm.setAttr(minus_node.attr('input1D[0]'), 1)
            pm.connectAttr(get_constraint_weight(constraint, 1), minus_node.attr('input1D[1]'))
            pm.connectAttr(minus_node.attr('output1D'), get_constraint_weight(constraint, 0))

            pm.connectAttr(soft_eye_value, get_constraint_weight(constraint, 1))

            # Redirect connections to eye rotation to the soft eye vector.
            for attr in ('rotateX', 'rotateY', 'rotateZ'):
                rotate_attr = eye.attr(attr)
                conns = pm.listConnections(eye.attr(attr), d=True, s=False, p=True)
                for conn in conns:
                    pm.connectAttr(direction_node.attr(attr), conn, f=True)

        # Create the main eye control.  Do this after the above, so we don't try to change parent
        # constraint connections that this creates on the joints.
        self.create_eye_controls()

    def create_tongue(self):
        parts = [
            ('Tongue2', 'tongue02', 'tongue03'),
            ('Tongue3', 'tongue03', 'tongue04'),
            ('Tongue4', 'tongue04', 'tongue04_End'),
        ]

        parent = self.rig_node
        for name, part, end_part in parts:
            joint = self.joints[part]
            end_joint = self.joints[end_part]

            disconnectIncomingConnections(joint, t=True, ro=True, s=True)

            # Position the helper on the joint, and point it towards the next joint.  The tongue
            # joints are usually not actually pointing directly at their child.
            helper_transform = pm.createNode('transform')
            align_node(helper_transform, joint)
            temp_constraint = pm.aimConstraint(end_joint, helper_transform, mo=False)
            pm.delete(temp_constraint)

            transform, handle = self.create_zeroed_handle(name=name, parent=parent, position=helper_transform, size=0.5)
            handle.attr('shape').set(1)
            handle.attr('localRotateZ').set(-90)
            self.face_color.connect(handle.attr('color'), f=True)
            
            pm.delete(helper_transform)

            if name == 'Tongue2':
                pm.parentConstraint(self.joints['tongue01'], transform.getParent(), mo=True)

            pm.parentConstraint(transform, joint, mo=True)

            lock_attr(transform.attr('translateY'), lock='lock')
            lock_attr(transform.attr('translateZ'), lock='lock')

            # Scaling is probably only useful on the last control.
            if name == 'Tongue4':
                # Create a helper underneath the control to constrain scale to.  Something about the joint
                # transform confuses the scale constraint, so we have to create a helper transform aligned
                # to the joint, then a transform inside it with no transform that the joint will be constrained
                # to.
                scale_transform1 = pm.createNode('transform', n=name + 'Scale1', p=transform)
                align_node(scale_transform1, joint)
                
                scale_transform2 = pm.createNode('transform', n=name + 'Scale2', p=scale_transform1)
                pm.scaleConstraint(scale_transform2, joint, mo=True)
                lock_attr(transform.attr('scaleY'), lock='lock')
                lock_attr(transform.attr('scaleY'), lock='keyable')
                lock_attr(transform.attr('scaleZ'), lock='keyable')

            parent = transform

    def create_eye_controls(self):
        # Create an eye control.  This can be moved around in space or rotated (or both) to move the
        # eyes.  It always points back at the character's eyes, so if you have multiple characters
        # nearby it's easy to tell which control is whcih.  It defaults to keeping the eyes pointed
        # the same distance apart, even if the control is moved towards or away from the character.
        # If this EyesFocused attribute is set to 1, the eyes will point directly at the control.
        default_distance = 20
       
        joints = [self.joints['lEye'], self.joints['rEye']]

        # Create the group that will hold the control.  This node sets the origin for the control, and
        # follows the parents of the eye joints (typically the head joint).
        #
        # We normally use a transform to do this.  To work around an odd quirk of the skeleton hierarchy:
        # Although almost every joint is joint aligned roughly to world space, the eyes are flipped on Z.
        # We normally do alignment with a transform, but to work around this, use a joint instead and 
        # flip the jointOrient.  Otherwise, our rotateX/rotateY connection later will be backwards.
        container_node = pm.createNode('joint', n='Align_EyeHandle', parent=self.rig_node)
        container_node.attr('jointOrient').set((0,0,180))
        container_node.attr('drawStyle').set(2) # none
        lock_trs(container_node, lock='unkeyable')
        temp_constraint = pm.pointConstraint(joints[0], joints[1], container_node, mo=False)
        pm.delete(temp_constraint)
        pm.xform(container_node, ws=True, r=True, t=(0,0,default_distance))

        # The node to hold internal parts of the control.
        internal_node = mh.createNewNode('transform', nodeName='EyeRigInternal', parent=self.rig_internal_node)
        align_node(internal_node, container_node)
        pm.parentConstraint(container_node, internal_node, mo=True)
        pm.scaleConstraint(container_node, internal_node, mo=True)

        # Create a null centered between the eyes.  This is what the control will aim at.
        center_node = mh.createNewNode('transform', nodeName='Eye_Center', parent=internal_node)
        temp_constraint = pm.pointConstraint(joints[0], joints[1], center_node)
        pm.delete(temp_constraint)

        # Create the handle.  Give it the same orientation as the eyes, so rotating the handle
        # down rotates the eyes down.  This makes the rotateX/rotateY connections later line up.
        # This won't work if the eyes have opposite orientations.
        pm.parent(self.eye_controls, container_node, r=True)
        
        pm.xform(self.eye_controls_handle, os=True, t=(0,0,0))
        self.eye_controls_handle.attr('shape').set(1)
        self.eye_controls_handle.attr('localRotateX').set(90)
        pm.setAttr(self.eye_controls_handle.attr('v'), keyable=False, channelBox=True)

        self.constrain_spaces(container_node, ['head', 'COGSpace', 'WorldSpace'], control=self.eye_controls, handle=self.eye_controls)

        # Set the rotation order to ZXY.  The Z rotation has no effect on the output, since we only
        # connect to X and Y, so this keeps things transforming correctly.
        self.eye_controls.attr('rotateOrder').set(2)

        # Scaling the control won't work as expected, so lock it.  Note that we don't
        # lock rz here, since that confuses the rotation manipulator.
        lock_scale(self.eye_controls)

        # Create a transform.  Point constrain it to the handle, and aim constrain
        # it to the eyes.  The handle will add the rotation of this transform, so
        # the handle points towards the eyes as it's moved around.
        handle_aim_node = mh.createNewNode('transform', nodeName='EyeRig_HandleAim', parent=internal_node)
        pm.pointConstraint(self.eye_controls, handle_aim_node, mo=False)
        pm.aimConstraint(center_node, handle_aim_node, mo=True)
        
        # Set the transform of the handle to the rotation of the EyeHandleAim transform, so it's
        # added to the visible rotation of the handle.  We could just connect the rotation to the
        # localRotation of the handle, but we're using that to orient the handle correctly.
        comp_node = mh.createNewNode('composeMatrix', nodeName='EyeRig_CompMatrix1')
        pm.connectAttr(handle_aim_node.attr('rotate'), comp_node.attr('inputRotate'))
        pm.connectAttr(comp_node.attr('outputMatrix'), self.eye_controls_handle.attr('transform'))

        # Create a group to hold the eye locators.  This is point constrained to the control.
        eye_locator_group = mh.createNewNode('transform', nodeName='EyeTargets', parent=internal_node)
        pm.xform(eye_locator_group, ws=True, t=pm.xform(self.eye_controls, q=True, ws=True, t=True))
        eye_locator_group.attr('visibility').set(0)
        pm.pointConstraint(self.eye_controls, eye_locator_group, mo=True)

        # Create locators for the eye targets.  This is what the eyes actually aim towards (via
        # the orient locators).
        eye_locators = []
        for idx, node in enumerate(joints):
            eye_locator = mh.createNewNode('transform', nodeName=['EyeLeft', 'EyeRight'][idx], parent=eye_locator_group)
            pm.xform(eye_locator, ws=True, t=pm.xform(node, q=True, ws=True, t=True))
            pm.xform(eye_locator, ws=True, r=True, t=(0,0,default_distance))
            eye_locators.append(eye_locator)

        # Create nulls which will sit on top of the joints.  This is what we'll actually aim,
        # so we can attach any rigging we want to them, and the eye joints only need a simple
        # orient constraint to these.
        orientLocators = []
        for idx, node in enumerate(joints):
            pos_node, pos_locator = self.create_locator('%s_Align' % node.nodeName(), parent=internal_node)
            align_node(pos_node, node)

            orient_node, orient_locator = self.create_locator('%s_Orient' % node.nodeName(), parent=pos_node)
            lock_trs(orient_node, lock='unkeyable')

            # Create an up vector for the aim constraint.
            up_node = mh.createNewNode('transform', nodeName='%s_Up' % node.nodeName(), parent=internal_node)
            pm.xform(up_node, ws=True, t=pm.xform(node, q=True, ws=True, t=True))
            pm.xform(up_node, ws=True, t=(0,1,0), r=True)
        
            # Note that we don't need maintain offset here, only on the final orient constraint.
            # This way, the orient of these is always 0, making it easier to adjust.
            pm.aimConstraint(eye_locators[idx], orient_node, mo=True, worldUpType='object', worldUpObject=up_node)

            # Create a transform inside the orient node.  This rotates along with the control.
            # By making this a child of the top-level orient transform, the aim and the rotation
            # will combine additively.  This is the node we actually orient contrain the eye
            # joints to.
            orient_inner_node = mh.createNewNode('transform', nodeName='%s_OrientInner' % node.nodeName(), parent=orient_node)
            lock_trs(orient_inner_node, lock='unkeyable')

            # We're going to connect RX and RY to self.eye_controls, so give this node the same
            # rotation order as self.eye_controls_handle.
            orient_inner_node.attr('rotateOrder').set(2)
            orientLocators.append(orient_inner_node)

            pm.connectAttr(self.eye_controls.attr('rotateX'), orient_inner_node.attr('rotateX'))
            pm.connectAttr(self.eye_controls.attr('rotateY'), orient_inner_node.attr('rotateY'))

            # Now, create a helper to figure out the X/Y angle.  We don't need this for the eye
            # control itself, since an orient constraint will do that for us, but this gives us
            # a clean rotation value, which driven keys like eyelid controls can be placed against.
            attr_name = 'angle_%s' % ['left', 'right'][idx]
            pm.addAttr(self.eye_controls, ln=attr_name, type='double3')
            pm.setAttr(self.eye_controls.attr('%sX' % attr_name), e=True, keyable=True)
            pm.setAttr(self.eye_controls.attr('%sY' % attr_name), e=True, keyable=True)
            pm.setAttr(self.eye_controls.attr('%sZ' % attr_name), e=True, channelBox=False, keyable=False)

            forwards_node, forwards_locator = self.create_locator('%s_Forwards' % node.nodeName(), parent=orient_inner_node)
            pm.xform(forwards_node, t=(0,0,10), os=True)
            pm.setAttr(forwards_node.attr('visibility'), False)
            lock_trs(forwards_node, lock='unkeyable')

            forwards2_node, forwards2_locator = self.create_locator('%s_Forwards2' % node.nodeName(), parent=pos_node)
            pm.xform(forwards2_node, t=(0,0,10)) #, os=True)
#            pm.setAttr(forwards2_node.attr('visibility'), False)
            lock_trs(forwards2_node, lock='unkeyable')

            minus_node_1 = create_plus_minus(forwards_locator.attr('worldPosition[0].worldPosition'), pos_locator.attr('worldPosition[0].worldPosition'), operation='subtract')
            minus_node_2 = create_plus_minus(forwards2_locator.attr('worldPosition[0].worldPosition'), pos_locator.attr('worldPosition[0].worldPosition'), operation='subtract')

            angle_1 = pm.createNode('angleBetween')
            pm.connectAttr(minus_node_1.attr('output3Dy'), angle_1.attr('vector1Y'))
            pm.connectAttr(minus_node_1.attr('output3Dz'), angle_1.attr('vector1Z'))
            pm.connectAttr(minus_node_2.attr('output3Dy'), angle_1.attr('vector2Y'))
            pm.connectAttr(minus_node_2.attr('output3Dz'), angle_1.attr('vector2Z'))

            angle_2 = pm.createNode('angleBetween')
            pm.connectAttr(minus_node_1.attr('output3Dx'), angle_2.attr('vector1X'))
            pm.connectAttr(minus_node_1.attr('output3Dz'), angle_2.attr('vector1Z'))
            pm.connectAttr(minus_node_2.attr('output3Dx'), angle_2.attr('vector2X'))
            pm.connectAttr(minus_node_2.attr('output3Dz'), angle_2.attr('vector2Z'))

            pm.connectAttr(angle_1.attr('eulerX'), self.eye_controls.attr(attr_name + 'X'))
            pm.connectAttr(angle_2.attr('eulerY'), self.eye_controls.attr(attr_name + 'Y'))
        
        # Constrain the eye joints to the orient transform.  Remove any existing connections,
        # which are eye orientation modifiers that we're replacing.
        for idx, node in enumerate(joints):
            disconnectIncomingConnections(joints[idx], ro=True)
            pm.orientConstraint(orientLocators[idx], joints[idx], mo=True)
        
        # Create a setRange node.  This will translate from the eye locator X position (distance from
        # the center to the locator) to a 0..1 range that can be used as a control.
        #
        # setRange nodes actually clamp to their min and max, rather than just adjusting the range.
        # We don't really want that, since it can be useful to set the distance to a slightly negative
        # number to move the eyes apart a bit.  Work around this: instead of scaling 0..1 to eye_locatorXPos..0,
        # scale from -1..1 to eye_locatorXPos*2..0.  This gives a wider range, if wanted.
        # In:  -5 -4 -3 -2 -1  0  1  2  3  4  5
        # Out:  6  5  4  3  2  1  0 -1 -2 -3 -4
        locator_distance_range = mh.createNewNode('setRange', nodeName='EyeRig_SetRangeEyeDistance')
        eye_locator1XPos = pm.xform(eye_locators[0], q=True, t=True, os=True)[0]
        eye_locator2XPos = pm.xform(eye_locators[1], q=True, t=True, os=True)[0]
        pm.setAttr(locator_distance_range.attr('oldMin'), -5, -5, 0)
        pm.setAttr(locator_distance_range.attr('oldMax'), 5, 5, 0)
        pm.setAttr(locator_distance_range.attr('min'), eye_locator1XPos*6, eye_locator2XPos*6, 0)
        pm.setAttr(locator_distance_range.attr('max'), eye_locator1XPos*-4, eye_locator2XPos*-4, 0)

        # The output of the setRange controls the distance of the eye locator from the main center control.
        pm.connectAttr(locator_distance_range.attr('outValueX'), eye_locators[0].attr('translateX'))
        pm.connectAttr(locator_distance_range.attr('outValueY'), eye_locators[1].attr('translateX'))

        # Add an attribute to move the eye locators to the center.  The most useful values of this are
        # 0 and 1, but support moving further and going crosseyed.
        pm.addAttr(self.eye_controls, ln='EyesFocused', at='double', min=-5, max=5, dv=0)
        pm.setAttr(self.eye_controls.attr('EyesFocused'), e=True, keyable=True)
        pm.connectAttr(self.eye_controls.attr('EyesFocused'), locator_distance_range.attr('valueX'))
        pm.connectAttr(self.eye_controls.attr('EyesFocused'), locator_distance_range.attr('valueY'))

    def find_figure_controls(self, figure_node):
        """
        Find all ControlValues nodes.  There can be one per figure, eg. hair will have its
        own ControlValues node.

        Return the controls group (or None if not found) and a map from group names to attributes:

        {
            'Eyes': [Attribute('Character|Control|LookUp'), Attribute('Character|Control|LookDown')],
        }
        """
        controls_node = pm.ls(figure_node.name() + '|Controls')
        if not controls_node:
            return None, {}

        assert len(controls_node) == 1, controls_node
        controls_node = controls_node[0]
        controls = {}

        # Children of ControlValues sit between the user controls in "Controls" and their targets.
        for control_sub_node in pm.listRelatives(controls_node, ad=True):
            # Find connected properties to figure out which attributes are actually control attributes.
            conns = pm.listConnections(control_sub_node, d=True, s=False, p=True, c=True)
            if not conns:
                continue

            # We only care about the control_sub_node connection.
            conns = [c[0] for c in conns]

            conns = [c for c in conns if pm.getAttr(c, keyable=True)]

            # The longName of the node is eg. "|Character|Controls|Pose_Controls|Head".  Get everything
            # after "Controls".
            group_name = control_sub_node.longName()[len(controls_node.longName())+1:]
            group_name = group_name.replace('|', '/')
            controls.setdefault(group_name, set()).update(conns)

        return controls_node, controls

    def register_node_for_controls(self, name, node):
        assert name in self.control_target_nodes, name
        self.control_target_nodes[name].append(node)

    def create_face_control_positions(self):
        """
        Give access to generic controls.

        There can be a lot of these, and we generally won't know what they are.

        - Keyframes are placed directly on the Controls group.  The Controls group is moved into
        RigInternal, so they'll be included in character sets.  Only the Rig group should be added
        to character sets.  By placing keys here rather than on the control handles we'll create
        below, the control handles can be reconfigured without changing the rig and breaking references.
        - Allow placing proxies for controls on any node group in control_target_nodes.  This can
        be used for most non-facial controls.  We need more control for the face.
        - Create a surface around the front of the head based on the curves of the facial joints, and
        allow positioning controls along it.  This way, we can specify positions for controls as a U/V
        position that will generalize reasonably well to different faces.  The surface is deleted once
        we're done placing controls.
        - Let the user configure where control groups go.  For example, since the "Head/Nose"
        group is often small, you may want to combine it with another group.  
        """
        body = self.get_body_shape()

        # Group the controls.
        head_group = pm.createNode('transform', n='Controls', p=self.rig_node)
        lock_trs(head_group, lock='unkeyable')

        # Add the controls group to the controls display layer.
        pm.editDisplayLayerMembers(self.controls_layer, head_group, noRecurse=True)

        def create_face_handle(name, size=1, parent=None):
            if parent is None:
                parent = head_group
            handle_transform, handle = self.create_handle(name, size=size, parent=parent)
            handle.attr('shape').set(1) # pyramid
            self.face_color.connect(handle.attr('color'), f=True)

            # Reduce clutter by disabling xray on these.  You probably don't need to access a head
            # expression control when you're not in front of the face to see it.
            handle.attr('xray').set(0)

            # Hide these by default.  We'll show them if any controls are added to them.
            handle_transform.attr('visibility').set(0)

            lock_trs(handle_transform, lock='hide')

            return handle_transform, handle

        # Create control handles that sit on or between joints.
        for item in self.joint_based_controls:
            joints = item['align_to']
            follow = item.get('follow', joints)

            handle_transform, handle = create_face_handle(item['name'], size=item.get('size', 0.5))
            joints = {self.joints[joint]: weight for joint, weight in joints.iteritems()}

            # Align and orient the handle to the joints, and set constraint weights to weight
            # which joints we're aligning to.
            def create_constraint(constraint_type, node, targets_and_weights, temporary=True, mo=False):
                nodes = targets_and_weights.keys() + [node]
                constraint = pm.parentConstraint(*nodes, mo=mo)

                for idx, (joint, weight) in enumerate(targets_and_weights.iteritems()):
                    get_constraint_weight(constraint, idx).set(weight)

                # If this is just a temporay constraint used to align the node, delete it.  Otherwise,
                # return the constraint.
                if not temporary:
                    return constraint

                pm.delete(constraint)

            create_constraint(pm.parentConstraint, handle_transform, joints)

            offset = item.get('pre_snap_ws_offset')
            if offset is not None:
                pm.xform(handle_transform, ws=True, r=True, t=offset)

            if item.get('snap', True):
                # Now that it's aligned, snap it to the geometry.
                temp_constraint = pm.geometryConstraint(body, handle_transform)
                pm.delete(temp_constraint)

            # Use a normalConstraint to point the handle away from the mesh.
            temp_constraint = pm.normalConstraint(body, handle_transform, aimVector=(0,1,0), upVector=(1,0,0))
            pm.delete(temp_constraint)
            self.register_node_for_controls(item['name'], handle_transform)

            offset = item.get('ws_offset')
            if offset is not None:
                world_space_translate_local_scale(handle_transform, ws=True, r=True, t=offset)

            offset = item.get('os_offset')
            if offset is not None:
                offset = Vector(*offset)
                world_space_translate_local_scale(handle_transform, os=True, r=True, t=offset)

            # Now that the position is finalized, recreate the parent constraint so we follow the joints normally.
            joints = {self.joints[joint]: weight for joint, weight in follow.iteritems()}
            create_constraint(pm.parentConstraint, handle_transform, joints, temporary=False, mo=True)

        # Create a NURBS surface in front of the head for us to position controls on.
        # Create curves along the body to place generic controls along.
        control_curves = [{
            'nodes': ['rEar', 'rBrowOuter', 'CenterBrow', 'lBrowOuter', 'lEar'],
            'translate': (0,10,-4),
            'scale': (1.1,1.2,1.2),
        }, {
            'nodes': ['rEar', 'rNasolabialMouthCorner', 'LipBelow', 'lNasolabialMouthCorner', 'lEar'],
            'translate': (0,0,0),
            'scale': (1.2,1.3,1.3),
        }, {
            'nodes': ['rEar', 'rJawClench', 'BelowJaw', 'lJawClench', 'lEar'],
            'translate': (0,-2,-3.5),
            'scale': (1.1,1.1,1.1),
        }]
        result_curves = []
        for control_curve in control_curves:
            nodes = [self.joints[node] for node in control_curve['nodes']]

            # Find the world space positions of each node.
            positions = [pm.xform(node,q=True, t=True, ws=True) for node in nodes]

            # Create a linear curve following the points, then fit a spline curve to it.
            curve = pm.curve(d=1, p=positions)
            fit_curve, _ = pm.fitBspline(curve)
            pm.delete(curve)

            # Center the curve's pivot, so we can transform it around its center.
            pm.select(fit_curve)
            mel.eval('CenterPivot')

            scale = Vector(*pm.xform(self.joints['head'], q=True, ws=True, s=True))

            # world_space_translate_local_scale(fit_curve, t=control_curve['translate'])
            # continue
            pm.xform(fit_curve, t=multiply_vector(Vector(control_curve['translate']), scale))
            pm.xform(fit_curve, s=Vector(control_curve['scale']))
            result_curves.append(fit_curve)

        # Create the surface.  We could keep construction history and constrain the curves to the head joints
        # to make the controls sort of follow the expression, but since this is a slider UI and the controls
        # don't follow too well, it seems gimmicky and not worth the extra scene complexity.
        surface, = pm.loft(result_curves, ch=False, range=0, polygon=0)
        surface.attr('visibility').set(0)

        # Optionally, rebuild the surface to have more spans and shrink wrap it to the face.  This gives a
        # closer fit for the controls.  This may not generalize very well and can be disabled if needed.
        # Use cmds instead of PyMel to work around a bug that causes lots of warnings to spam the output
        # if we access a shrinkWrap through PyMel.
        pm.rebuildSurface(surface, ch=False, rpo=True, rt=0, end=1, kr=0, kcp=0, kc=0, du=3, su=10, sv=10, dv=3, dir=0)
        wrap, = cmds.deformer(surface.name(), type='shrinkWrap')
        for attr in ('boundaryRule','continuity','keepBorder','keepHardEdge','keepMapBorders','propagateEdgeHardness','smoothUVs'):
            cmds.connectAttr('%s.%s' % (body, attr), '%s.%s' % (wrap, attr))
        cmds.connectAttr('%s.worldMesh[0]' % body, '%s.targetGeom' % wrap)
        cmds.setAttr('%s.projection' % wrap, 1) # toward center
        cmds.setAttr('%s.closestIfNoIntersection' % wrap, 1)
        cmds.setAttr('%s.reverse' % wrap, 1)
        cmds.setAttr('%s.boundingBoxCenter' % wrap, 1)

        # Create a pointOnSurfaceInfo to evaluate the position of the surface.
        pos_info = pm.createNode('pointOnSurfaceInfo')
        surface.getShape().attr('worldSpace[0]').connect(pos_info.attr('inputSurface'))
        pos_info.attr('turnOnPercentage').set(1)

        for name, (parent, u, v) in self.face_control_positions.iteritems():
            handle_transform, handle = create_face_handle(name, parent=head_group, size=0.5)

            # The U and V axes of the surface are actually switched from what we want.  Just switch them here, so
            # the positions in face_control_positions are intuitive.
            pos_info.attr('parameterU').set(v)
            pos_info.attr('parameterV').set(u)
            t = pos_info.attr('position').get()
            pm.xform(handle_transform, t=pos_info.attr('position').get(), ws=True)

            # Now that it's aligned, snap it to the geometry.
            temp_constraint = pm.geometryConstraint(body, handle_transform)
            pm.delete(temp_constraint)

            # Orient the handle to the surface.
            temp_constraint = pm.normalConstraint(body, handle_transform, aimVector=(0,1,0), upVector=(1,0,0))
            pm.delete(temp_constraint)
           
            # Now that the position is finalized, recreate the parent constraint so we follow the joints normally.
            pm.parentConstraint(self.joints[parent], handle_transform, mo=True)

            self.register_node_for_controls(name, handle_transform)

#            motion_path = pm.createNode('motionPath')
#            motion_path.attr('follow').set(1)
#            motion_path.attr('fractionMode').set(1)
#            motion_path.attr('worldUpType').set(3) # vector
#            motion_path.attr('allCoordinates').connect(handle_transform.attr('translate'))
#            fit_curve.getShape().attr('worldSpace[0]').connect(motion_path.attr('geometryPath'))
#            motion_path.attr('rotate').connect(handle_transform.attr('rotate'))
#            motion_path.attr('rotateOrder').connect(handle_transform.attr('rotateOrder'))
#            motion_path.attr('uValue').set(idx / 2.0)

        # Clean up.
        pm.delete(surface)
        for curve in result_curves:
            pm.delete(curve)

    def create_controls(self):
        # All names in node_names_for_controls should have at least one node registered by now.
        for name, nodes in self.control_target_nodes.iteritems():
            if not nodes:
                log.warning('Control target "%s" doesn\'t have any nodes associated with it.', name)

        def find_closest_control_group_match(group_name, control, warn=True):
            name = '%s/%s' % (group_name, control)
            for regex, groups in self.control_groups:
                if re.match(regex, name):
                    return groups

            if warn:
                log.warning('Control %s is not in any control groups.  This control won\'t be accessible.', name)
                
            return None

        figure_controls = { }
        accessory_handles_group = None
        handle_x_pos = -4
        handle_y_pos = -4
        for figure_node in find_figures_in_scene(exclude_following=False):
            controls_node, controls = self.find_figure_controls(figure_node)
            if controls_node is None:
                continue

            # If this isn't the main figure, it's something else, like hair or clothing.  The main figure's
            # controls will be placed on the rig controls.  The rest will be given their own control handle,
            # with its visibility connected to that figure's top visibility.
            is_main_figure = (self.top_node == figure_node)

            # Hide the underlying controls group.
            controls_node.attr('hiddenInOutliner').set(True)
            figure_controls[figure_node] = controls

            if not is_main_figure:
                if accessory_handles_group is None:
                    accessory_handles_group = pm.createNode('transform', n='AccessoryControls', p=self.rig_node)
                    align_node(accessory_handles_group, self.hip_handle)
                    pm.xform(accessory_handles_group, ws=True, r=True, t=(-40,0,0))
                    pm.parentConstraint(self.joints['hip'], accessory_handles_group, mo=True)

                handle_transform, handle = self.create_handle(figure_node.nodeName(), parent=accessory_handles_group, position=accessory_handles_group, size=2, p=accessory_handles_group, connect_color=False)
                pm.xform(handle_transform, t=(handle_x_pos * 4,handle_y_pos*4,0))
                lock_trs(handle_transform, lock='hide')
                handle.attr('color').set((0, 1, 0))
                handle.attr('xray').set(0)
                figure_node.attr('visibility').connect(handle_transform.attr('visibility'))

                handle_x_pos += 1
                if handle_x_pos == 5:
                    handle_x_pos = -4
                    handle_y_pos += 1

                # We can't hide/unhide the control node directly since it's connected to the mesh.  Add a proxy to the
                # real visibility attribute to make it easier to access.
                pm.addAttr(handle_transform, ln='meshVisibility', sn='mvis', at='boolean', proxy=figure_node.attr('visibility'))
                pm.setAttr(handle_transform.attr('meshVisibility'), keyable=True)

            # need to figure out how to get the original node names
            #'Head': ['Head'],
            #'Head/Brow/Brow Inner Up-Down': ['Face_Brow_Center'],
            for group_name, control_list in controls.iteritems():
                # Get the niceName of each attribute.  This is the original property name.
                attribute_names = {control: pm.attributeQuery(control.attrName(), node=control.node(), niceName=True) for control in control_list}

                # Add attributes in order.  We can't reorder them after adding them.
                for control in sorted(control_list, key=lambda item: attribute_names[item]):
                    # Get the niceName of the attribute.  This is the original property name.
                    attr_name = pm.attributeQuery(control.attrName(), node=control.node(), niceName=True)

                    all_control_nodes = []

                    if is_main_figure:
                        control_groups = find_closest_control_group_match(group_name, attr_name)

                        for group in control_groups:
                            control_nodes = self.control_target_nodes.get(group, [])
                            if not control_nodes:
                                log.warning('Control %s is in a control group %s that doesn\'t exist or has no nodes.', attr_name, group)
                                continue

                            all_control_nodes.extend(control_nodes)

                            # Show control nodes if they're hidden.
                            for control_node in control_nodes:
                                control_node.attr('visibility').set(1)
                    else:
                        all_control_nodes.append(handle_transform)

                    if not all_control_nodes:
                        continue

                    # long_name = pm.attributeQuery(control.attrName(), node=control.node(), longName=True)
                    # short_name = pm.attributeQuery(control.attrName(), node=control.node(), shortName=True)
                    attr_type = pm.attributeQuery(control.attrName(), node=control.node(), attributeType=True)

                    # Hack: Replace "Right" and "Left" with "Side" in control attribute names, so left and right
                    # controls have the same name on each of their nodes.  This way, the control box will match
                    # them up so you can select a left and right control and changing attributes will change both
                    # of them.  The niceName is still unique.
                    symmetric_name = re.sub(r'( Right| Left)$', ' Side', attr_name)
                    long_name = mh.cleanup_node_name(symmetric_name)

                    for control_node in all_control_nodes:
                        mh.addAttr(control_node, long_name, type=attr_type, niceName=attr_name, proxy=control)
                        pm.setAttr(control_node.attr(long_name), keyable=True)

    def create_attachment_points(self):
        # Create nodes that follow the hands, to parent props to.
        group = pm.createNode('transform', n='AttachmentPoints', p=self.rig_node)
        lock_trs(group, lock='unkeyable')

        attachments = [{
            'name': 'RightHand',
            'offset': (0,-1,0),
            'align_to': ('rThumb2', 'rIndex1', 'rMid1', 'rRing1', 'rPinky1', 'rHand'),
            'orient_to': ('rHand',),
            'orientation': (0,0,0),
            'follow': ('rCarpal1', 'rCarpal2', 'rCarpal3', 'rCarpal4'),
            'rotate_handle': (180,0,0),
            'offset_handle': (0,0,0),
        }, {
            'name': 'LeftHand',
            'offset': (0,-1,0),
            'align_to': ('lThumb2', 'lIndex1', 'lMid1', 'lRing1', 'lPinky1', 'lHand'),
            'orient_to': ('lHand',),
            'orientation': (0,0,0),
            'follow': ('lCarpal1', 'lCarpal2', 'lCarpal3', 'lCarpal4'),
            'rotate_handle': (180,0,0),
            'offset_handle': (0,0,0),
        }, {
            # Create a head attachment point even though we have a head FK handle that always follows the
            # head.  The FK handle is actually positioned inside the head, and this handle sits at the top
            # where the FK handle appears, so it's appropriate for hats.  The FK handle would also not
            # necessarily follow the head if an IK handle is added for the head later.
            'name': 'Head',
            'offset': (0,self.find_top_of_head(without_scaling=True),0),
            'align_to': None,
            'orient_to': None,
            'orientation': (0,0,0),
            'follow': ('head',),
            'rotate_handle': (0,0,0),

            # Move the handle up visually a bit, so it's not right on top of the head control handle.
            'offset_handle': (0,1,0),
        }]
        for attachment in attachments:
            handle_transform, handle = self.create_handle('Attachment_' + attachment['name'], size=1, parent=group, connect_color=False)
            lock_trs(handle_transform, lock='hide')

            if attachment['align_to']:
                nodes = [self.joints[node] for node in attachment['align_to']]
                nodes.append(handle_transform)
                temp_constraint = pm.pointConstraint(*nodes, mo=False)
                pm.delete(temp_constraint)

            if attachment['orient_to']:
                nodes = [self.joints[node] for node in attachment['orient_to']]
                nodes.append(handle_transform)
                temp_constraint = pm.orientConstraint(*nodes, mo=False)
                pm.delete(temp_constraint)

            world_space_translate_local_scale(handle_transform, t=attachment['offset'], ws=True, r=True)
            pm.xform(handle_transform, ro=attachment['orientation'], r=True)

            nodes = [self.joints[node] for node in attachment['follow']]
            nodes.append(handle_transform)
            pm.parentConstraint(*nodes, mo=True)

            pm.setAttr(handle.attr('xray'), 0)
            pm.setAttr(handle.attr('localRotate'), attachment['rotate_handle'])
            pm.setAttr(handle.attr('localPosition'), attachment['offset_handle'])

            pm.setAttr(handle.attr('shape'), 1)
            pm.setAttr(handle.attr('color'), (0, 1, 1))
            pm.setAttr(handle_transform.attr('visibility'), 0)

    def create_pick_walk_controllers(self):
        controller_roots = []
        controller_children = {}

        parents = list(self.pick_walk_parents)
        if not self.humanik:
            parents.extend(self.pick_walk_parents_for_rig)
        for node, parent in parents:
            if node not in self.handles:
                log.warning('Pick walk controller handle wasn\'t created: %s', node)
                continue

            node = self.handles[node]
            
            if parent is None:
                controller_roots.append(node)
            else:
                if parent not in self.handles:
                    log.warning('Parent of pick walk controller %s created: %s', parent, node)
                    continue
                parent = self.handles[parent]
                controller_children.setdefault(parent, []).append(node)

        def _create_pick_walk_controllers_recurse(root, parent):
            if parent is None:
                pm.select(root)
                pm.controller()
            else:
                pm.select([root, parent])
                pm.controller(p=True)

            children = controller_children.get(root, [])
            for child in children:
                _create_pick_walk_controllers_recurse(child, root)

        for root in controller_roots:
            _create_pick_walk_controllers_recurse(root, None)

    def finalize_rig(self):
        self.create_pick_walk_controllers()

        # Hide the skeleton.
        self.joints['hip'].attr('visibility').set(0)

        # Hide rig controls in the CB, so they don't get in the way, and make them unkeyable so they
        # don't clutter character sets.
        for handle in pm.listRelatives([self.rig_node, self.rig_internal_node], allDescendents=True, type='zRigHandle'):
            for attr in ('localRotateX', 'localRotateY', 'localRotateZ', 'localPositionX', 'localPositionY', 'localPositionZ',
                    'localScaleX', 'localScaleY', 'localScaleZ', 'xray', 'shape'):
                pm.setAttr(handle.attr(attr), lock=False, cb=False, keyable=False)

        # No nodes in the internal group should be keyable.
        # make_all_attributes_unkeyable(self.rig_internal_node)

        # Move mesh groups into a group, since they clutter the top-level group and usually don't need
        # to be accessed directly.
        parts_node = pm.createNode('transform', n='Parts', parent=self.top_node)
        for figure_node in find_figures_in_scene(exclude_following=False):
            if self.top_node == figure_node:
                continue

            if self.top_node not in figure_node.listRelatives(ap=True):
                continue

            pm.parent(figure_node, parts_node)

        top_meshes = pm.ls(self.top_node.longName() + '|Meshes')
        if top_meshes:
            pm.parent(top_meshes, parts_node)

        # Move the rig nodes to the top of the group, so they're not buried under figure groups.
        # We have to do this in reverse order.
        pm.reorder(self.rig_internal_node, f=True)
        pm.reorder(self.rig_node, f=True)

    def get_body_shape(self):
        # The body shape is either directly under self.top_node or inside Meshes.
        f = self.get_meshes_in_figure(self.top_node)
        for node in self.get_meshes_in_figure(self.top_node):
            shape = pm.ls(node, 'BodyShape')[0]
            if shape:
                return shape
        raise RuntimeError('Couldn\'t find BodyShape in %s', self.top_node)

    def find_top_of_head(self, without_scaling=False):
        """
        Try to figure out the Y position of the top of the character's head.

        This is tricky, since there are no joints telling us this.  Place a transform above
        the character and use a geometry constraint to find the closest point.

        We don't currently do anything robust to find the geometry, we assume there's a
        "Body" mesh.
        """
        transform = pm.createNode('transform')
        pm.select(transform)
        pm.xform(transform, ws=True, t=(0, 220, 0))
        pm.geometryConstraint(self.get_body_shape(), transform)
        y = pm.xform(transform, q=True, t=True, ws=True)[1]
        pm.delete(transform)

        if without_scaling:
            # Return the position as if there's no scaling.
            scale = pm.xform(self.joints['head'], q=True, ws=True, s=True)
            y /= scale[1]

        return y

    def create_coordinate_space(self, name, parent):
        space = self.coordinate_spaces.get(name)
        if space is not None:
            return
        if name in self.coordinate_spaces:
            return

        space = pm.createNode('transform', n=name + 'Space')
        pm.parent(space, self.coordinate_spaces_group, r=True)
        pm.xform(space, ws=True, t=(0,0,0))
        pm.xform(space, ws=True, ro=(0,0,0))
        pm.xform(space, ws=True, s=(0,0,0))

        # align_node(space, space)
        pm.parentConstraint(parent, space, mo=False)
        lock_trs(space, lock='unkeyable')

        self.coordinate_spaces[name] = space

    def create_space_switcher(self, node, spaces, control):
        spaces = list(spaces)
        spaces.append('CustomSpace1')
        spaces.append('CustomSpace2')

        pm.addAttr(control, ln='coordinateSpace', sn='space', type='enum', enumName=spaces)
        pm.setAttr(control.attr('space'), keyable=True)
        control_attr = control.attr('space')

        # If any named spaces don't exist, create them from joints with the same name.
        for space in spaces:
            if space not in self.coordinate_spaces:
                self.create_coordinate_space(space, self.joints[space])

        # Create another attribute with the coordinate space list.  This is used by the "change and
        # sync coordinate space" script to allow changing the coordinate space of an object and syncing
        # its position to where it was in the previous space.  This attribute has no direct effect on
        # the rig and doesn't need to be saved.
        pm.addAttr(control, ln='changeCoordinateSpace', sn='changeSpace', type='enum', enumName=spaces)
        change_space_attr = control.attr('changeSpace')
        pm.setAttr(change_space_attr, keyable=False, channelBox=True)

        weight_attrs = []
        for idx, space in enumerate(spaces):
            space_chooser_name = '%sSpaceChooser%i' % (control.nodeName(), idx)
            space_chooser_cond = pm.createNode('condition', n=space_chooser_name)
            pm.setAttr(space_chooser_cond.attr('colorIfTrue'), (1,1,1))
            pm.setAttr(space_chooser_cond.attr('colorIfFalse'), (0,0,0))
            pm.connectAttr(control_attr, space_chooser_cond.attr('firstTerm'))
            pm.setAttr(space_chooser_cond.attr('secondTerm'), idx)

            weight_attrs.append(space_chooser_cond.attr('outColorR'))

        # Create references, so scripts can find out which node is controlled by a coordinate space,
        # and which coordinate space can be controlled from each node.
        references_group_name = node.nodeName() + '_CS'
        references_group = pm.createNode('transform', n=references_group_name, p=self.references_group)
        lock_trs(references_group)
        pm.addAttr(references_group, ln='nodeType', dt='string')
        references_group.attr('nodeType').set('CoordinateSpaceReferences')

        coordinate_space_nodes = [self.coordinate_spaces[space] for space in spaces]

        class CoordinateSpace(object):
            def __init__(self):
                self.spaces = spaces
                self.weight_attrs = weight_attrs
                self.references_group = references_group
                self.control_attr = control_attr
                self.change_space_attr = change_space_attr
                self.coordinate_space_nodes = coordinate_space_nodes

                add_reference(node, 'Controller0', group=self.references_group)

            def add_transform(self, node, handle, t=True, r=True, skipTranslate=(), skipRotate=()):
                """
                Add a transform to be in this coordinate space.

                node is the node which receives the parent constraint, and should be hidden.  handle is
                a child of that node, which is the node the user moves around.
                """
                args = list(self.coordinate_space_nodes)
                args.append(node)

                # Note that we need to always use parentConstraint and not orientConstraint or pointConstraint
                # even if we're only constraining t or r.  parentConstraint behaves better with multiple targets
                # for some reason: orientConstraint sometimes moved the output even with mo=True.
                if not t and not r:
                    raise 'constrain_spaces should specify at least one of t or r'
                if not t:
                    skipTranslate=('x', 'y', 'z')
                if not r:
                    skipRotate=('x', 'y', 'z')
                constraint = pm.parentConstraint(*args, mo=True, skipRotate=skipRotate, skipTranslate=skipTranslate)

                for idx, attr in enumerate(self.weight_attrs):
                    connect_constraint_weights(constraint, idx, attr)

                self.add_transform_space_switching_only(handle)

            def add_transform_space_switching_only(self, handle):
                add_reference(handle, 'controlledTransform0', group=self.references_group)

            def add_control(self, proxy):
                """
                Add the IK/FK switch control to the specified node.  Any number of nodes can have a copy
                of the control.
                """
                pm.addAttr(proxy, ln='coordinateSpace', sn='space', type='enum', enumName=spaces, proxy=self.control_attr.name())
                pm.setAttr(proxy.attr('space'), keyable=True)

                pm.addAttr(proxy, ln='changeCoordinateSpace', sn='changeSpace', type='enum', enumName=spaces, proxy=self.change_space_attr.name())
                pm.setAttr(proxy.attr('changeSpace'), keyable=False, channelBox=True)

                add_reference(proxy, 'Controller0', group=self.references_group)

        return CoordinateSpace()

    def constrain_spaces(self, node, spaces, control=None, t=True, r=True, skipTranslate=(), skipRotate=(), proxies=(), handle=None):
        """
        Constrain node to one or more coordinate spaces.  If t is true, constrain translation.
        If r is true, constrain rotation.

        spaces is a list of coordinsate space names, which much already exist.

        If control is not None, the space selection control will be placed on the specified
        node.  Otherwise, it will be placed on node.

        If control is already associated with a coordinate space list, node and handle will just be added to
        the existing switcher.
        """
        # This is a convenience shortcut for create_space_switcher.
        coordinate_space = self.create_space_switcher(node, spaces, control)

        coordinate_space.add_transform(node, handle, t, r, skipTranslate, skipRotate)

        # If any other nodes are listed to hold the control, create proxy attributes.
        for proxy in proxies:
            coordinate_space.add_control(proxy)

    def create_locator(self, name, parent=None):
        transform = pm.createNode('transform', name=name, parent=parent)
        locator = pm.createNode('locator', p=transform)
        pm.setAttr(locator.attr('hideOnPlayback'), True)
        return transform, locator

    def add_reference(self, node, name, group=None):
        """
        Add a reference to a node to the RigReferences node, or the specified node if group is
        specified.

        This can be used by rig scripts later on to locate individual controls in the rig.
        """
        if group is None:
            group = self.references_group
        return add_reference(node, name, group)

    def create_handle(self, name, parent=None, size=None, shape=None, connect_color=True, *args, **kwargs):
        # Work around annoying behavior: pm.createNode() for a shape normally creates a transform with the
        # shape inside, but pm.createNode(parent=p) just puts the shape inside the transform.
        transform = pm.createNode('transform', name=name, parent=parent)
        handle = pm.createNode('zRigHandle', p=transform)
        pm.setAttr(handle.attr('hideOnPlayback'), True)
        pm.rename(handle, name + 'Shape')

        # We don't need scale on most controls.
        lock_scale(transform)

        if shape is not None:
            # Set a custom shape.
            pm.setAttr(handle.attr('shape'), -1)
            pm.connectAttr(shape.attr('outMesh'), handle.attr('inCustomMesh'))

        if size is None:
            size = 1

        # Scale controls by the overall size ratio of the figure.
        size *= self.overall_scale

        size = Vector(size, size, size)
        size /= Vector(pm.xform(transform, q=True, s=True, ws=True))

        if connect_color:
            self.control_color_attr.connect(handle.attr('color'))

        pm.setAttr(handle.attr('localScale'), size)

        self.add_reference(transform, name)

        return transform, handle

    def create_zeroed_handle(self, name, parent=None, position=None, *args, **kwargs):
        # Create a transform to align to the joint.  We'll put the handle inside this, so the
        # bind transform is zero.  IK/FK controls also control visibility here.
        alignment_node = pm.createNode('transform', name='Align_' + name, parent=parent)
        if position is not None:
            align_node(alignment_node, position)

        lock_trs(alignment_node, lock='unkeyable')

        transform, handle = self.create_handle(name=name, parent=alignment_node, *args, **kwargs)

        # Store the handle by name.
        assert name not in self.handles, name
        self.handles[name] = transform

        return transform, handle

def go(humanik):
    mh.setup_logging()

    rig = AutoRig()
    rig.create(humanik)

