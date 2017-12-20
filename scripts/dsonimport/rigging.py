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

                rotate = [0,0,0]
                rotate[rotate_axis_idx] = -angle
                pm.xform(j1.maya_node, ws=True, r=True, ro=rotate)

            rotations = make_rotations()

