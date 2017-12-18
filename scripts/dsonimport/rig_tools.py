import math
from maya import cmds, mel
from pymel import core as pm
import pymel.core.datatypes as dt

def find_ikfk_node(node):
    for conn in node.attr('message').connections(d=True, s=False):
        if pm.hasAttr(conn, 'nodeType') and conn.attr('nodeType').get() == 'FKIKReferences':
            return conn
    raise Exception('Node %s is not an IK/FK control.' % node)

def get_control(node, name):
    return node.attr(name).connections()[0]

def dist(p1, p2):
    vec = (p1[0]-p2[0], p1[1]-p2[1], p1[2]-p2[2])
    return math.pow(vec[0]*vec[0] + vec[1]*vec[1] + vec[2]*vec[2], 0.5)

def angle_between_quaternions(q1, q2):
    dot = q1.x*q2.x + q1.y*q2.y + q1.z*q2.z + q1.w*q2.w
    v = 2*dot*dot - 1
    if v > .9999:
        return 0
    return math.acos(v)

def fk_to_ik(mode='angle'):
    """
    Match IK to the current FK pose.
    
    FK can reach poses that IK can't, this can give bad pole vector positions in those cases.
    """
    n = pm.ls(sl=True)[0]
    node = find_ikfk_node(n)

    # Align the IK handle to the last FK control's position and rotation.
    t = pm.xform(get_control(node, 'FK_3'), q=True, ws=True, t=True)
    pm.xform(get_control(node, 'IK_Handle'), ws=True, t=t)

    r = pm.xform(get_control(node, 'FK_3'), q=True, ws=True, ro=True)
    pm.xform(get_control(node, 'IK_Handle'), ws=True, ro=r)

    # The pole vector is trickier.  It only rotates on one axis, so we'll just do a search to find the orientation
    # that most closely matches the FK position, measured by comparing the rotation of the IK_2 joint against the
    # FK_2 joint.
    # no, measure world space distance?
    pole_vector_node = get_control(node, 'Pole_Vector')
    pole_vector = pole_vector_node.attr('rx')
    fk_2 = get_control(node, 'FK_2')
    ik_2 = get_control(node, 'IKJoint_2')

    def current_distance():
        # Return the distance between the IK and FK positions.  This measures the error in our current pole
        # vector angle.
        #
        # We can use both distance and orientation to decide how close the pose is.  Distance doesn't work when
        # the elbow is locked straight, since the pole vector will only rotate the elbow and not rotate it.
        if mode == 'angle':
            # Read the difference in orientation.        
            r1 = pm.xform(fk_2, q=True, ws=True, ro=True)
            r2 = pm.xform(ik_2, q=True, ws=True, ro=True)
    
            q1 = dt.EulerRotation(*r1).asQuaternion()
            q2 = dt.EulerRotation(*r2).asQuaternion()

            angle = angle_between_quaternions(q1, q2)
            angle = angle/math.pi*180.0
            return angle
        else:
            # Read the difference in position.
            t1 = pm.xform(fk_2, q=True, ws=True, t=True)
            t2 = pm.xform(ik_2, q=True, ws=True, t=True)
        
            return dist(t1, t2)
        
    def distance_at_angle(angle):
        pole_vector.set(angle)
        return current_distance()
        
    # Do a search to find an enclosing range around the correct value.
    best_distance = 999999
    start_angle, end_angle = 0, 30
   
    for angle1 in xrange(-180, 210, 30):
        angle2 = angle1 + 30
        distance = distance_at_angle(angle1) + distance_at_angle(angle2)
        if distance < best_distance:
            best_distance = distance
            start_angle = angle1
            end_angle = angle2

    # Narrow the range until we're within the tolerance.  Note that we're rotating the point in an arc, so
    # we can overshoot: the distance might get further away before it approaches.
    for _ in xrange(25):
        half_angle = (start_angle + end_angle) / 2
        d1 = distance_at_angle(start_angle)
        d2 = distance_at_angle(half_angle)
        d3 = distance_at_angle(end_angle)
        error1 = abs(d1 - d2)
        error2 = abs(d3 - d2)
        if error1 < error2:
            end_angle = half_angle
        else:
            start_angle = half_angle
            
        if abs(error1 - error2) < 0.0001:
            break

    pole_vector.set(half_angle)

def ik_to_fk():
    """
    Match FK to the current IK pose.
    """
    n = pm.ls(sl=True)[0]
    node = find_ikfk_node(n)

    # Read the IK pose.  IKJoint_# points to the current IK transforms, regardless of the current IK/FK weight.
    joint1 = get_control(node, 'IKJoint_1')
    joint2 = get_control(node, 'IKJoint_2')
    joint3 = get_control(node, 'IKJoint_3')
    
    r1 = pm.xform(joint1, q=True, ws=True, ro=True)
    r2 = pm.xform(joint2, q=True, ws=True, ro=True)
    r3 = pm.xform(joint3, q=True, ws=True, ro=True)

    pm.xform(get_control(node, 'FK_1'), ws=True, ro=r1)
    pm.xform(get_control(node, 'FK_2'), ws=True, ro=r2)
    pm.xform(get_control(node, 'FK_3'), ws=True, ro=r3)

def find_coordinate_space_node(node):
    for conn in node.attr('message').connections(d=True, s=False):
        if pm.hasAttr(conn, 'nodeType') and conn.attr('nodeType').get() == 'CoordinateSpaceReferences':
            return conn
    raise Exception('Node %s doesn\'t have a coordinate space attribute.' % node)

def get_control(node, name):
    return node.attr(name).connections()[0]

def switch_coordinate_space(n):
    """
    Switch the coordinate space of the selected node to the value of changeCoordinateSpace, preserving
    the current transform.
    
    Note that only the transform at the current time will be preserved.  If the transform is keyed in
    the future, animation will break.  The coordinate space can be manually restored at the next keyframe.
    """
    # Find the RigReferences node for this node's coordinate space.
    node = find_coordinate_space_node(n)
    if not pm.hasAttr(n, 'changeCoordinateSpace'):
        raise Exception('Node %s doesn\'t have a coordinate space attribute.' % n)
    if n.attr('changeCoordinateSpace').get() == n.attr('coordinateSpace').get():
        print 'The coordinate space for %s is already up to date.' % n
        return

    controlled_transforms = []
    for i in range(10):
        try:
            control = get_control(node, 'controlledTransform%i' % i)
            print control
        except pm.MayaAttributeError:
            break
        controlled_transforms.append(control)

    # Record the current position of the user handles.
    translates = [pm.xform(node, q=True, ws=True, t=True) for node in controlled_transforms]
    rotates = [pm.xform(node, q=True, ws=True, ro=True) for node in controlled_transforms]

    # Change the coordinate space.    
    n.attr('coordinateSpace').set(n.attr('changeCoordinateSpace').get())

    # Restore the position we had before we changed coordinate spaces.    
    for node, t, r in zip(controlled_transforms, translates, rotates):
        if not node.attr('translateX').isLocked():
            pm.xform(node, ws=True, t=t)

        # Do try to set rotations even if rotateX is locked, since pole vector rotations are
        # locked on all but Y.
        #if not node.attr('rotateX').isLocked() or True:
        pm.xform(node, ws=True, ro=r)

def switch_selected_coordinate_spaces():
    for node in pm.ls(sl=True):
        switch_coordinate_space(node)

