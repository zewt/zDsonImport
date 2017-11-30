import copy, logging, math, os
from pprint import pprint, pformat
import config, util
import maya_helpers as mh
from dson import DSON

from pymel import core as pm
import pymel.core.datatypes as dt
import pymel
from maya import mel

log = logging.getLogger('DSONImporter')

def _create_rotation_rbf(name):
    """
    Create an RBF solver.  This approximates the rotation on an input plane, with clean falloff
    before we flip at 180 degrees.
    #
    At 1,0, we're at rest.  The vector is in its original position, so the angle is 0.
    At 0,1, we've rotated 90 degrees. At 0,-1 we've rotated -90 degrees.
    We'll place a number of samples around the unit circle, keying them to the angle.
    #
    We stop before 180 degrees, since 180 degrees could be either 180 or -180 degrees.
    There's no way to figure this out, and if we give duplicate inputs to the solver
    we'll end up with an unsolvable key set.  We stop at 165 degrees in either direction.
    If the rotation goes beyond that it'll flip.
    """

    mh.load_plugin('zRBF.py')

    rbf_node = pm.createNode('zRBF', n=name)
    min_angle = -165
    max_angle = 165
    intervals = 6
    step = (max_angle - min_angle) / (intervals*2)
    for idx, interval in enumerate(xrange(-intervals, intervals+1)):
        angle = step * interval
        angle = angle * math.pi / 180.0
        x = math.cos(angle)
        y = math.sin(angle)
        point = (x, y, 0)

        value_attr = rbf_node.attr('value').elementByLogicalIndex(idx)
        pm.setAttr(value_attr.attr('value_Position'), point)
        pm.setAttr(value_attr.attr('value_Value'), angle)
    return rbf_node

def load_plugin(plugin):
    # Don't call loadPlugin if the plugin is already loaded.  Even though it doesn't do anything,
    # it takes about half a second.
    if not pm.pluginInfo(plugin, q=True, loaded=True):
        pm.loadPlugin(plugin, quiet=True)

    if not pm.pluginInfo(plugin, q=True, registered=True):
        raise RuntimeError('Plugin "%s" isn\'t available.' % plugin)

_twist_rig_map = {
    # The key is the roll joint.  aim_vector is the vector down the roll joint.
    # The up vector is the axis that receives most of the rotation (other than aim_vector).
    # For example, the arm joints are down the X axis, and the wrist receives the
    # least motion on the Y axis, so we use the Z axis for the wrist's up_vector.
    'lShldrBend': {
        'twist_joint_asset_id': 'lShldrTwist',
        'end_joint_asset_id': 'lForearmBend',
        'aim_vector': (1,0,0),
        'up_vector': (0,1,0),
        'roll_orient_joint': 'xyz',
        'roll_orient_sao': 'yup',
    },
    'rShldrBend': {
        'twist_joint_asset_id': 'rShldrTwist',
        'end_joint_asset_id': 'rForearmBend',
        'aim_vector': (1,0,0),
        'up_vector': (0,1,0),
        'roll_orient_joint': 'xyz',
        'roll_orient_sao': 'yup',
    },
    'lForearmBend': {
        'twist_joint_asset_id': 'lForearmTwist',
        'end_joint_asset_id': 'lHand',
        'aim_vector': (1,0,0),
        'up_vector': (0,0,1),
        'roll_orient_joint': 'xyz',
        'roll_orient_sao': 'yup',
    },
    'rForearmBend': {
        'twist_joint_asset_id': 'rForearmTwist',
        'end_joint_asset_id': 'rHand',
        'aim_vector': (1,0,0),
        'up_vector': (0,0,1),
        'roll_orient_joint': 'xyz',
        'roll_orient_sao': 'yup',
    },
    'lThighBend': {
        'twist_joint_asset_id': 'lThighTwist',
        'end_joint_asset_id': 'lShin',
        'aim_vector': (0,1,0),
        'up_vector': (1,0,0),

        # This will change the orientation of the joint.  Maya's joint orient command doesn't
        # give a way to orient a joint away from the child.
        'roll_orient_joint': 'yzx',
        'roll_orient_sao': 'zup',
    },
    'rThighBend': {
        'twist_joint_asset_id': 'rThighTwist',
        'end_joint_asset_id': 'rShin',
        'aim_vector': (0,1,0),
        'up_vector': (1,0,0),
        'roll_orient_joint': 'yxz',
        'roll_orient_sao': 'xup',
    },
    'neckLower': {
        'twist_joint_asset_id': 'neckUpper',
        'end_joint_asset_id': 'head',
        'aim_vector': (0,1,0),
        'up_vector': (1,0,0),
        'roll_orient_joint': 'yzx',
        'roll_orient_sao': 'zup',
    },
}

def create_twist_rigs(env):
    if not config.get('create_twist_rigs'):
        return

    if pymel.versions.current() < 201650:
        # Prior to 2016 ext2, changing preBindMatrix on a skinCluster didn't take effect, which
        # leads to this twisting the mesh out of shape.
        log.warning('Not reating twist joint rigs.  Please update to at least Maya 2016 EXT2.')
        return

    log.debug('Creating twist joint rigs...')

    for dson_node in env.scene.depth_first():
        if dson_node.node_type != 'figure':
            continue

        # If these figure is conforming, don't change it.  The twist rigs will go
        # on the target skeleton.
        if 'conform_target' in dson_node:
            continue

        for bone_node in dson_node._get_nodes_within_figure():
            _create_twist_rig(bone_node)

def get_twist_rig_asset_names():
    """
    Return a list of asset names that will have twist rigs applied.
    """
    if not config.get('create_twist_rigs'):
        return []

    result = []
    for roll_joint_asset_name, part in _twist_rig_map.iteritems():
        result.append(roll_joint_asset_name)
        result.append(part['twist_joint_asset_id'])
        result.append(part['end_joint_asset_id'])
    return result
    
def _create_twist_rig(dson_node):
    """
    Some models have an unusual roll and twist joint setup: they put elbow rotation
    on the shoulder twist joint, and put elbow rotation control on the shoulder control
    as an alias (which we don't import).  All twisting is on the twist joint; there's
    no weighting to put some of the rotation on any other joints.  The deformations this
    gives are fine, but it's a bit weird, so clean it up.

    Reparent the elbow directly under the shoulder, so the twist joint is by itself,
    and use an aim constraint to make the twist joint follow the elbow.  This way,
    the elbow joint can be rotated normally, and the twist joint will follow.

    This depends on the structure of the skeleton and should be turned off with skeletons
    that use different structures.
    """
    if dson_node.node_type != 'bone':
        return

    # We use the asset to figure out which bone is which.
    if not dson_node.asset:
        return

    twist_rig_info = _twist_rig_map.get(dson_node.asset.get_value('name'))
    if not twist_rig_info:
        return

    # The roll joint is the joint we're on.
    roll_joint = dson_node
    roll_joint_maya_node = roll_joint.maya_node

    # Find the twist joint.
    twist_joint = roll_joint.find_asset_name(twist_rig_info['twist_joint_asset_id'])
    twist_joint_maya_node = twist_joint.maya_node

    # Find the end joint, which is the joint after the twist joint.
    end_joint = roll_joint.find_asset_name(twist_rig_info['end_joint_asset_id'])
    end_joint_maya_node = end_joint.maya_node

    # Don't apply this if there are incoming connections to nodes we're going to change.
    # It's OK for there to be outgoing connections.  For example, we don't want a "knee
    # bend" constraint that targets the thigh, since we're going to put constraints on
    # the thigh, but corrective modifiers that read the position of the thigh are fine.
    # These twist joints are intended to make external rigging easier, and if you're
    # putting a rig on the figure, you want all controls that take over joints disabled
    # anyway.
    def has_incoming_connections(node):
        attrs_to_check = (
            'translate', 'translateX', 'translateY', 'translateZ',
            'rotate', 'rotateX', 'rotateY', 'rotateZ',
            'scale', 'scaleX', 'scaleY', 'scaleZ')
        nodes = [node.attr(attr) for attr in attrs_to_check]
        connections = pm.listConnections(nodes, s=True, d=False, p=True)
        if connections:
            log.warning('Not creating twist rig for %s because it has incoming connections: %s' % (node, connections))
            return True
        return False

    if has_incoming_connections(roll_joint_maya_node) or has_incoming_connections(twist_joint_maya_node) or has_incoming_connections(end_joint_maya_node):
        return

    # The roll joint is oriented towards the twist joint, but we need it oriented towards
    # the end joint.  We don't want to reorient the joint, since it'll break anything constrained
    # to it.  Instead, create a new joint to take its place, and hide and parent the skinned
    # roll joint to the new joint.  That lets us reorient the new joint however we want.
    roll_joint_name = roll_joint_maya_node.nodeName()
    pm.rename(roll_joint_maya_node, '%s_Skinned' % roll_joint_name)

    # Create the new roll joint, and position it in the same place as the skinned one.
    new_roll_joint = pm.duplicate(roll_joint_maya_node, parentOnly=True, n=roll_joint_name)[0]
    pm.reorder(new_roll_joint, front=True)

    # Mark the roll joint that we're controlling internal.
    mh.config_internal_control(roll_joint_maya_node)
    roll_joint_maya_node.attr('visibility').set(0)

    # The roll joint may have rotations on it from straighten_poses in addition to jointOrient.
    # Freeze the rotations, or orient joints won't work ("has non-zero rotations").  This
    # is only freezing this control, not the underlying joint.
    pm.makeIdentity(new_roll_joint, apply=True, t=0, r=1, s=0, n=0, pn=1)

    # Create a copy of the end joint to orient towards.  We need to freeze rotations on this too,
    # or pm.joint will spew warnings about non-zero rotations.  (That looks like a bug, since we're
    # not telling it to orient that joint.)
    temporary_joint = pm.createNode('joint', n='temp')
    pm.parent(temporary_joint, end_joint_maya_node, r=True)
    pm.parent(temporary_joint, new_roll_joint)
    pm.makeIdentity(temporary_joint, apply=True, t=0, r=1, s=0, n=0, pn=1)

    # Orient the roll joint towards the end joint.
    pm.joint(new_roll_joint, e=True, orientJoint=twist_rig_info['roll_orient_joint'], secondaryAxisOrient=twist_rig_info['roll_orient_sao'])
    pm.delete(temporary_joint)

    def create_rotation_node():
        # Don't do this if there aren't any connections to rotation.
        attrs_to_check = ('rotate', 'rotateX', 'rotateY', 'rotateZ')
        for attr in attrs_to_check:
            if pm.listConnections(end_joint_maya_node.attr(attr), s=False, d=True, p=True):
                break
        else:
            return

        # Create a placeholder node.  This follows the end joint around and is parented to the twist joint,
        # so it represents the rotation of the end joint relative to the twist joint.  We'll move outgoing
        # connections from the end joint's rotation to this.  That way, even though we'll be reparenting the
        # end joint to under the bend joint, other nodes still see rotation relative to twist, like they
        # did before.
        rotation_output_node = pm.createNode('joint', n=end_joint_maya_node.nodeName() + '_RelativeRotation', p=end_joint_maya_node)
        mh.config_internal_control(rotation_output_node)
        pm.parent(rotation_output_node, twist_joint_maya_node)

        for attr in attrs_to_check:
            for connected_attr in pm.listConnections(end_joint_maya_node.attr(attr), s=False, d=True, p=True):
                pm.connectAttr(rotation_output_node.attr(attr), connected_attr, force=True)

        pm.parentConstraint(end_joint_maya_node, rotation_output_node, mo=False)

    create_rotation_node()

    # Move the end joint out from inside the twist joint into the new roll joint.
    pm.parent(end_joint_maya_node, new_roll_joint)

    # Put the children of the old roll joint under the new roll joint.
    for child in pm.listRelatives(roll_joint_maya_node, children=True):
        pm.parent(child, new_roll_joint)

    # Constrain the old roll joint to the new one.  We're keeping this joint around unchanged,
    # since there may be other things constrained to it.  For example, clothing conforms often
    # connect to these joints.
    pm.parentConstraint(new_roll_joint, roll_joint_maya_node, maintainOffset=True)
    pm.scaleConstraint(new_roll_joint, roll_joint_maya_node, maintainOffset=True)

    # Turn off segmentScaleCompensate on the original roll joint that we've scale constrained.
    # We have it on so modifiers work, but modifiers are now pointing at our new joint, and
    # scale constraints don't work correctly with segmentScaleCompensate.
    roll_joint_maya_node.attr('segmentScaleCompensate').set(0)

    # For some reason, the YZX rotate order on some thigh twist joints causes the aim constraint
    # to flip out, and changing it to XYZ fixes it.  The rotate order on the twist joint shouldn't
    # matter since we should only ever be rotating it on one axis anyway.
    pm.setAttr(twist_joint_maya_node.attr('rotateOrder'), 0)

    pm.aimConstraint(end_joint_maya_node, twist_joint_maya_node, mo=True, worldUpType='objectrotation', worldUpObject=end_joint_maya_node,
            aimVector=twist_rig_info['aim_vector'],
            upVector=twist_rig_info['up_vector'], worldUpVector=twist_rig_info['up_vector'])

    # Bump the twist joint down one in the outliner, so pressing down from the parent joint
    # goes to the end joint and not the twist joint.
    pm.reorder(end_joint_maya_node, relative=1)

    # Put the twist joint inside two empty groups.  This prevents a bone from being
    # drawn from the roll joint to the twist joint, since Maya only searches up two
    # parenting levels for a parent joint.
    group_inner = pm.group(twist_joint_maya_node, name='%s_Grp1' % twist_joint_maya_node.nodeName())
    group_outer = pm.group(group_inner, name='%s_Grp' % twist_joint_maya_node.nodeName())
    mh.config_internal_control(group_outer)
    pm.setAttr(group_outer.attr('visibility'), 0)

    # Point the roll joint node at the new roll joint.
    roll_joint.maya_node = new_roll_joint

def straighten_poses(env):
    """
    Figures are generally in a relaxed T-pose.  Move figures to a full T-pose.
    Note that this doesn't bring the arms parallel to the X axis.
    """

    if not config.get('straighten_pose'):
        return

    log.debug('Straightening poses')
    for dson_node in env.scene.depth_first():
        if dson_node.node_type != 'figure':
            continue

        # Ignore eg. SkinBindings.
        if dson_node.node_source != 'node':
            continue
        if 'conform_target' in dson_node:
            continue

        # The feet in bind pose are usually pointing slightly outwards.  Aim them along
        # the Z axis, so they're pointing straight ahead.  This is important for HIK floor
        # contact, since its contact planes assume that feet are aligned when in bind pose.
        # The foot joints aren't aligned to the XZ plane, so there's no axis for us to simply
        # align to zero.  Instead, look at the world space angle going down to the next joint,
        # and rotate by the inverse of that.  Do the same for the arm joints and hands.  We
        # want the hands to be square with the world, so floor contact planes are aligned
        # with HIK later.
        joints = [
            # Joint to aim        End joints                Cross, aim, rotate axis   Invert
            ('lShldrBend',        ('lForearmBend',),        (1, 0, 2),                False),
            ('rShldrBend',        ('rForearmBend',),        (1, 0, 2),                False),
            ('lForeArm',          ('lHand',),               (1, 0, 2),                False),
            ('rForeArm',          ('rHand',),               (1, 0, 2),                False),
            ('lForeArm',          ('lHand',),               (2, 0, 1),                True),
            ('rForeArm',          ('rHand',),               (2, 0, 1),                True),

            # Aim the hand towards the average of the middle and ring finger.
            #('lHand',             ('lMid1', 'lRing1'),      (2, 0, 1),                True),
            #('rHand',             ('rMid1', 'rRing1'),      (2, 0, 1),                True),
            ('lHand',             ('lRing1', ),      (2, 0, 1),                True),
            ('rHand',             ('rRing1', ),      (2, 0, 1),                True),
            ('rFoot',             ('rMetatarsals',),        (0, 2, 1),                False),
            ('lFoot',             ('lMetatarsals',),        (0, 2, 1),                False),
        ]

        # First, find and check all of the joints.  If there are problem with any joints, we
        # won't apply any changes.
        for aim_joint, end_joints, (cross_axis_idx, aim_axis_idx, rotate_axis_idx), invert in joints:
            def make_rotations():
                total_angle = 0
                try:
                    j1 = dson_node.find_asset_name(aim_joint)
                except KeyError as e:
                    log.warning('Couldn\'t straighten %s %s: %s', dson_node.node_id, aim_joint, e.message)
                    return

                # Average the angle towards each of the target joints.
                for end_joint in end_joints:
                    try:
                        j2 = dson_node.find_asset_name(end_joint)
                    except KeyError as e:
                        log.warning('Couldn\'t straighten %s %s: %s', dson_node.node_id, aim_joint, e.message)
                        return

                    pos1 = pm.xform(j1.maya_node, q=True, ws=True, t=True)
                    pos2 = pm.xform(j2.maya_node, q=True, ws=True, t=True)
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
                    raise RuntimeError('Unexpected angle while orienting joint %s to %s: %f' % (j1.maya_node, j2.maya_node, angle))

                rotate = [0,0,0]
                rotate[rotate_axis_idx] = -angle
                pm.xform(j1.maya_node, ws=True, r=True, ro=rotate)

            rotations = make_rotations()

