import logging, os, re, sys
from time import time
import pymel.core as pm
import pymel.core.datatypes as dt
from maya import OpenMaya as om
from maya import cmds, mel
import config, util

log = logging.getLogger('DSONImporter')

def config_internal_control(node):
    if not pm.ls(node):
        return
    
    if not pm.attributeQuery('useOutlinerColor', node=node, exists=True):
        # Don't throw errors if we're given non-DAG nodes.
        return

    if config.get('internal_control_hide_in_outliner'):
        pm.setAttr('%s.hiddenInOutliner' % node, 1)
    if config.get('internal_control_outliner_color'):
        pm.setAttr('%s.useOutlinerColor' % node, 1)
        pm.setAttr('%s.outlinerColor' % node, *config.get('internal_control_outliner_color'))

def config_driven_control(node):
    if not pm.ls(node):
        return

    if not pm.attributeQuery('useOutlinerColor', node=node, exists=True):
        # Don't throw errors if we're given non-DAG nodes.
        return

    if config.get('driven_control_outliner_color'):
        pm.setAttr('%s.useOutlinerColor' % node, 1)
        pm.setAttr('%s.outlinerColor' % node, *config.get('driven_control_outliner_color'))

def load_plugin(plugin_name, required=True):
    if not pm.pluginInfo(plugin_name, q=True, loaded=True):
        try:
            pm.loadPlugin(plugin_name, quiet=True)
        except RuntimeError as e:
            pass

    if not pm.pluginInfo(plugin_name, q=True, registered=True):
        if required:
            raise RuntimeError('Plugin "%s" isn\'t available.' % plugin_name)
        return False
    return True

def unescape_fbx(name):
    """
    Unescape a name escaped by Maya FBX import.
    """
    def unescape(text):
        # text is "FBXASC012".
        value = int(text.group(0)[6:])
        return chr(value)
    return re.sub(r'(FBXASC\d\d\d)', unescape, name)

def escape_fbx(name):
    """
    Return a name escaped like a Maya FBX import.
    """
    def needs_escaping(c, first=False):
        is_alpha = ('a' <= c <= 'z') or ('A' <= c <= 'Z')
        is_alphanumeric = is_alpha or ('0' <= c <= '9')

        if first:
            return not is_alpha
        else:
            return not is_alphanumeric

    def escape(c, first=False):
        if not needs_escaping(c, first):
            return c

        return 'FBXASC%03i' % ord(c)

    return ''.join(escape(c, idx == 0) for idx, c in enumerate(name))

def cleanup_node_name(name):
    '''
    Return a valid Maya node name.  Invalid sequences of characters will be replaced
    with underscores.

    Note that this is a single node name, not a node path like '|parent|child'.
    '''
    first = re.sub(r'[^A-Za-z_]+', '_', name[0:1])
    rest = re.sub(r'[^A-Za-z_0-9]+', '_', name[1:])
    return first + rest

def createVector3(node, name, niceName=None, angle=False):
    if niceName is None:
        niceName = name

    pm.addAttr(node, longName=name, shortName=name, niceName=niceName, at='float3')
    node_type = 'floatAngle' if angle else 'float'
    pm.addAttr(node, longName='%sX' % name, at=node_type, p=name, niceName='%s X' % niceName)
    pm.addAttr(node, longName='%sY' % name, at=node_type, p=name, niceName='%s Y' % niceName)
    pm.addAttr(node, longName='%sZ' % name, at=node_type, p=name, niceName='%s Z' % niceName)
    pm.setAttr('%s.%sX' % (node, name), e=True, keyable=True)
    pm.setAttr('%s.%sY' % (node, name), e=True, keyable=True)
    pm.setAttr('%s.%sZ' % (node, name), e=True, keyable=True)

def addAttr(node, longName, shortName=None, *args, **kwargs):
    """
    A wrapper for pm.addAttr.

    Maya throws an error if an attribute name exists instead of making a unique name.
    Check for an existing node with the same shortName or longName, and make the names
    unique if necessary.

    Return a PyNode for the new attribute.
    """
    assert 'ln' not in kwargs
    assert 'sn' not in kwargs

    existing_attrs = pm.listAttr(node)
    longName = util.make_unique(longName, existing_attrs)

    if shortName is None:
        shortName = longName
    shortName = util.make_unique(shortName, existing_attrs)

    pm.addAttr(node, shortName=shortName, longName=longName, *args, **kwargs)
    return node.attr(longName)

def aliasAttr(alias_name, attr):
    # An attribute can only have one alias.  If an existing alias matches alias_name,
    # just return it.
    old_alias = pm.aliasAttr(attr, q=True)
    if old_alias == alias_name:
        return src_attr

    node = attr.node()
    existing_attrs = pm.listAttr(node)
    existing_aliases = pm.aliasAttr(node, q=True)
    if existing_aliases:
        existing_attrs.extend(pm.aliasAttr(node, q=True))
    
    unique_alias_name = util.make_unique(alias_name, existing_attrs)
    pm.aliasAttr(unique_alias_name, attr)
    return unique_alias_name

def createNewNode(nodeType, nodeName=None, parent=None):
    # Work around a Maya bug: passing parent=None isn't the same as omitting it.
    args = {}
    if parent is not None:
        args['parent'] = parent
    if nodeName is not None:
        args['name'] = cleanup_node_name(nodeName)

    node = pm.createNode(nodeType, skipSelect=True, **args)

    if 'shape' in node.nodeType(inherited=True):
        # This is a shape node.  Move up to the transform node.
        node = node.listRelatives(p=True)[0]

    return node

def is_arg_vector(arg):
    """
    Return true if arg is a constant vector, eg. (1,1,1), or a PyNode pointing
    to a float3.
    """
    if isinstance(arg, tuple) or isinstance(arg, list):
        return True
    elif isinstance(arg, pm.PyNode) and arg.type() in ('float3', 'double3'):
        return True
    return False

def set_or_connect(dest, source):
    """
    If source is a PyNode attribute, connect it to dest.
    If source is a number, set dest to it.

    This allows chaining inputs to outputs without caring whether the
    previous value is a constant or dynamic value:

    value = 0.5

    target = create_node()
    set_or_connect(target.attr('input'), value)
    value = target.attr('output')

    target = create_node()
    set_or_connect(target.attr('input'), value)
    value = target.attr('output')
    """
    if is_arg_vector(dest) != is_arg_vector(source):
        # One argument is a vector and the other isn't.  If we're told to set a vector to
        # a scalar, eg. set_or_connect(node.attr('color'), 1), set each component of the
        # vector, as if we were given (1,1,1).  This way, more code can work with vectors
        # and scalars without caring which they have.
        #
        # It doesn't make sense to give us a scalar and a vector, only a vector and a scalar.
        # We could set the scalar to the first element of the vector, but this is more likely
        # to be a bug that we should catch.
        assert is_arg_vector(dest), 'Can\'t set a scalar to a vector: %s = %s' % (dest, source)

        r, g, b = get_channels_cached(dest)
        if isinstance(source, pm.PyNode):
            source.connect(r)
            source.connect(g)
            source.connect(b)
        else:
            r.set(source)
            g.set(source)
            b.set(source)

        return
    
    if isinstance(source, pm.PyNode):
        pm.connectAttr(source, dest)
    else:
        pm.setAttr(dest, source)

class MathOp(object):
    """
    This class implements scalar and vector math using Maya nodes.

    Each input can be a constant or a PyNode attribute.  If all of the inputs are constant,
    a constant result will be returned:

    >>> Add().run(1, 2)
    3

    If one or more arguments are a Maya attribute, a new Maya attribute will usually be
    returned:

    >>> Add().run(numeric_attr, 2)
    PyNode('result')

    In some cases, the result can be determined statically even if the inputs are variable:

    >>> Mult().run(numeric_attr, 0)
    0

    Vectors of three values can be used.  Vectors must always be of length 3.  Vector operations
    are always equivalent to doing the scalar operation on each set of arguments:

    >>> Mult().run([1,2,3],[0,1,2])
    [0,2,6]

    For PyNode values, vector arguments are the parent attribute, eg. attr.color:

    >>> Mult().run(node.attr('color'),[0,1,2])
    PyNode('result')

    Attribute vector operations can also optimize constant expressions:

    >>> Mult().run(node.attr('color'),[0,0,0])
    [0,0,0]

    Some operations auto-expand vector and scalar arguments.  For example:
    >>> Mult().run([2,3,4], 2)
    [4,6,8]

    >>> Mult().run(node1.attr('color'), node2.attr('alpha'))
    PyNode('result')
    """
    arg_count = None

    # If supports_vector is true then the subclass supports vector operations natively, eg. using
    # the vector input of a Maya node.  If it's false, it doesn't natively support it and we'll
    # emulate it by doing the scalar operation three times (possibly creating three nodes).
    supports_vector = False

    # If true, arguments can mix vectors and scalars, eg. [1,1,1]*2.  If false, arguments
    # must be either all vectors or all scalars.
    allow_mixed_vectors_and_scalars = False

    def run(self, args):
        # It doesn't make sense for a subclass to allow_mixed_vectors_and_scalars if
        # it doesn't support vector ops.
        if self.allow_mixed_vectors_and_scalars:
            assert self.supports_vector

        assert len(args) == self.arg_count, args

        # This is a vector operation if the arguments are a list/tuple of length 3, or a PyNode
        # with a float3/double3 type.
        is_vector_op = False
        any_non_vector_arguments = False
        all_args_constant = True
        for arg in args:
            if isinstance(arg, pm.PyNode):
                all_args_constant = False

            if is_arg_vector(arg):
                is_vector_op = True
            else:
                any_non_vector_arguments = True

        if not self.allow_mixed_vectors_and_scalars and is_vector_op:
            assert not any_non_vector_arguments, 'All or no arguments must be vectors: %s' % args

        if all_args_constant:
            # All arguments are constant, so just run the operation and return the result.
            # If this is a vector, do this using _run_vector_as_scalar so the subclasses
            # don't have to support vectors and can just handle scalars.
            if is_vector_op:
                # If allow_mixed_vectors_and_scalars is true and we have any scalar arguments,
                # expand them to a vector.  For example, [(1,2,3),10] becomes [(1,2,3),(10,10,10)].
                def expand_argument(arg):
                    if not isinstance(arg, pm.PyNode) and not isinstance(arg, list) and not isinstance(arg, tuple):
                        return (arg, arg, arg)
                    else:
                        return arg
                args = [expand_argument(arg) for arg in args]

                return self._run_vector_as_scalar(args)
            else:
                return self.run_scalar(args)

        if is_vector_op and not self.supports_vector:
            # This node doesn't support vector ops, so emulate it with scalar ops.
            return self._run_vector_as_scalar(args)
        
        # Call create() to let the subclass do the rest.
        return self.create(args)

    def run_scalar(self, args):
        """
        Return the value of the operation.  args will always contain only scalar values: no Maya
        nodes and no vectors.
        """
        raise NotImplemented

    def run_optimized(self, args):
        """
        If we can determine the result of the operation now, return it.  Otherwise, return None.
        """
        return None

    def _run_vector_as_scalar(self, args):
        # Run the operation on each argument.
        result = []
        for idx in range(3):
            single_args = []
            for arg in args:
                if isinstance(arg, pm.PyNode):
                    single_args.append(get_channels_cached(arg)[idx])
                else:
                    single_args.append(arg[idx])
            scalar_result = self.run(single_args)
            result.append(scalar_result)

        # If the result is a constant, just return the result.
        if all(not isinstance(e, pm.PyNode) for e in result):
            return result

        # One or more of the result elements is a Maya attribute.  Combine the three elements into
        # a single attribute using a colorConstant node.
        combined_result = pm.createNode('colorConstant')
        set_or_connect(combined_result.attr('inColorR'), result[0])
        set_or_connect(combined_result.attr('inColorG'), result[1])
        set_or_connect(combined_result.attr('inColorB'), result[2])
        return combined_result.attr('outColor')

# Math operations based on plusMinusAverage and multiplyDivide.
class MathOp_Basic(MathOp):
    # All floatMath operations have two arguments.
    arg_count = 2

    # This is set by the subclass to the value of 'operation' on floatMath.
    operation = None

    supports_vector = True
    allow_mixed_vectors_and_scalars = True

    # If true, this uses plusMinusAverage.  Otherwise, this uses multiplyDivide.
    is_plus_minus = True

    def _create_internal(self):
        """
        Create the math node, and return (input1, input2, output) plugs.
        """
        if self.is_plus_minus:
            math_node = pm.shadingNode('plusMinusAverage', asUtility=True)
            math_node.attr('operation').set(self.operation)
            input1 = math_node.attr('input3D[0]')
            input2 = math_node.attr('input3D[1]')
            output = math_node.attr('output3D')
            return input1, input2, output
        else:
            math_node = pm.shadingNode('multiplyDivide', asUtility=True)
            math_node.attr('operation').set(self.operation)
            input1 = math_node.attr('input1')
            input2 = math_node.attr('input2')
            output = math_node.attr('output')
            return input1, input2, output

    def create(self, args):
        assert self.operation is not None

        if is_arg_vector(args[0]) or is_arg_vector(args[1]):
            input1, input2, output = self._create_internal()
            input1R, input1G, input1B = get_channels_cached(input1)
            input2R, input2G, input2B = get_channels_cached(input2)

            # If an argument is already a vector, just set/connect it.  If it's a scalar, connect
            # it to each input of colorA/B.
            if is_arg_vector(args[0]):
                set_or_connect(input1, args[0])
            else:
                set_or_connect(input1R, args[0])
                set_or_connect(input1G, args[0])
                set_or_connect(input1B, args[0])

            if is_arg_vector(args[1]):
                set_or_connect(input2, args[1])
            else:
                set_or_connect(input2R, args[1])
                set_or_connect(input2G, args[1])
                set_or_connect(input2B, args[1])

            return output
        else:
            optimized_result = self.run_optimized(args)
            if optimized_result is not None:
                return optimized_result
            
            input1, input2, output = self._create_internal()
            input1R = get_channels_cached(input1)[0]
            input2R = get_channels_cached(input2)[0]

            set_or_connect(input1R, args[0])
            set_or_connect(input2R, args[1])
            return get_channels_cached(output)[0]

def _is_list(value):
    return isinstance(value, tuple) or isinstance(value, list)

class Add(MathOp_Basic):
    operation = 1 # Add
    def run_scalar(self, args):
        return args[0] + args[1]

    def run(self, args):
        # Some special-case optimizations:
        if args[0] == 0: return args[1]
        if args[1] == 0: return args[0]
        if _is_list(args[0]) and tuple(args[0]) == (0,0,0): return args[1]
        if _is_list(args[1]) and tuple(args[1]) == (0,0,0): return args[0]

        return super(Add, self).run(args)

    def run_optimized(self, args):
        if args[0] == 0:
            # N + 0 = N
            return args[1]
        if args[1] == 0:
            # 0 + N = N
            return args[0]
        return None

class Sub(MathOp_Basic):
    operation = 2 # Subtract
    def run_scalar(self, args):
        return args[0] - args[1]

    def run_optimized(self, args):
        if args[1] == 0:
            # N - 0 = N
            return args[0]
        return None

class Mult(MathOp_Basic):
    operation = 1 # Multiply
    is_plus_minus = False
    def run_scalar(self, args):
        return args[0] * args[1]

    def run(self, args):
        # Some special-case optimizations:
        if args[0] == 0: return 0
        if args[1] == 0: return 0
        if _is_list(args[0]) and tuple(args[0]) == (0,0,0): return [0,0,0]
        if _is_list(args[1]) and tuple(args[1]) == (0,0,0): return [0,0,0]
        if _is_list(args[0]) and tuple(args[0]) == (1,1,1): return args[1]
        if _is_list(args[1]) and tuple(args[1]) == (1,1,1): return args[0]

        return super(Mult, self).run(args)

    def run_optimized(self, args):
        if args[0] == 0 or args[1] == 0:
            # 0 * N = 0
            # N * 0 = 0
            return 0

        if args[0] == 1:
            # N * 1 = N
            return args[1]
        if args[1] == 1:
            # 1 * N = N
            return args[0]

        return None

class Div(MathOp_Basic):
    operation = 2 # Divide
    is_plus_minus = False
    def run_scalar(self, args):
        return float(args[0]) / args[1]

    def run_optimized(self, args):
        if args[0] == 0:
            # 0 / N = 0
            return 0
        return None

class Pow(MathOp_Basic):
    operation = 3 # Power
    is_plus_minus = False

    def run_scalar(self, args):
        return pow(args[0], args[1])

    def run_optimized(self, args):
        if args[1] == 0:
            # N^0 = 1
            return 1
        if args[1] == 1:
            # N^1 = N
            return args[0]

        return None

class Clamp(MathOp):
    arg_count = 3
    def run_scalar(self, args):
        # Clamp arg 0 between [arg1,arg2].
        return max(min(args[0], args[2]), args[1])

    def create(self, args):
        optimized_result = self.run_optimized(args)
        if optimized_result is not None:
            return optimized_result

        math_node = pm.shadingNode('clamp', asUtility=True)
        set_or_connect(math_node.attr('inputR'), args[0])
        set_or_connect(math_node.attr('minR'), args[1])
        set_or_connect(math_node.attr('maxR'), args[2])
        return math_node.attr('outputR')

class Condition(MathOp):
    arg_count = 4

    def create(self, args):
        first_term = args[0]
        second_term = args[1]
        color_if_true = args[2]
        color_if_false = args[3]

        optimized_result = self.run_optimized(args)
        if optimized_result is not None:
            return optimized_result

        math_node = pm.shadingNode('condition', asUtility=True)
        math_node.attr('operation').set(self.condition_mode)
        set_or_connect(math_node.attr('firstTerm'), first_term)
        set_or_connect(math_node.attr('secondTerm'), second_term)
        set_or_connect(math_node.attr('colorIfTrueR'), color_if_true)
        set_or_connect(math_node.attr('colorIfFalseR'), color_if_false)
        return math_node.attr('outColorR')

class ConditionGreaterThan(Condition):
    condition_mode = 2
    def run_scalar(self, args):
        return args[2] if args[0] > args[1] else args[3]

class ConditionLessThan(Condition):
    condition_mode = 4
    def run_scalar(self, args):
        return args[2] if args[0] < args[1] else args[3]


math_ops = {
    'add': Add,
    'sub': Sub,
    'mult': Mult,
    'div': Div,
    'pow': Pow,
    'clamp': Clamp,
    'gt': ConditionGreaterThan,
    'lt': ConditionLessThan,
}

def math_op(op, *args):
    operation = math_ops[op]()
    return operation.run(args)

def bake_joint_rotation_to_joint_orient(maya_node):
    """
    Bake the rotation on a joint to its jointOrient.
    """
    # We'll use makeIdentity to combine the rotation and orientation.  makeIdentity recurses
    # to children and there's no way to prevent that from happening, so create a helper and
    # copy the rotation to it, so we can makeIdentity just that node.
    temp_transform = pm.createNode('joint')
    try:
        temp_transform.attr('jointOrient').set(maya_node.attr('jointOrient').get())
        temp_transform.attr('rotate').set(maya_node.attr('rotate').get())
        temp_transform.attr('rotateOrder').set(maya_node.attr('rotateOrder').get())

        pm.makeIdentity(temp_transform, apply=True, t=0, r=1, s=0, n=0)

        # Copy the result back.  rotate should be (0,0,0).
        maya_node.attr('jointOrient').set(temp_transform.attr('jointOrient').get())
        maya_node.attr('rotate').set(temp_transform.attr('rotate').get())
    finally:
        pm.delete(temp_transform)

def find_next_disconnected_idx(path):
    # Get the input connections.  This will return a list of [(path.node[1], input1), (path.node[2], input2)]
    conns = pm.listConnections(path, s=True, d=False, p=True, c=True) or []

    max_connected_idx = -1
    for dst, src in zip(conns[0::2], conns[1::2]):
        # Parse out the array portion in: Daz_Sum_rNasolabialMiddle_translationZ.input1D[1]
        if dst[-1] != ']':
            # This is annoying for arrays of vectors, where we get eg. Daz_Sum_rNasolabialMiddle_translationZ.input3D[1].input3Dx.
            # If the last part isn't an array element, strip off the extra property.
            dst = dst[:dst.rindex('.')]

        assert '[' in dst
        assert dst[-1] == ']'
        dst = dst[dst.rindex('[')+1:-1]
        idx = int(dst)
        max_connected_idx = max(max_connected_idx, idx)
    return max_connected_idx

def find_uvset_by_name(shape, uvset_name, required=False):
    """
    Find a UVset on a mesh by its name.

    This returns the .ovSetName attribute, which is what is needed for UV linking.
    """
    uvsets = shape.attr('uvSet')

    for uvset in uvsets:
        uvset = uvset.attr('uvSetName')
        if uvset.get() == uvset_name:
            return uvset

    if required:
        raise RuntimeError('Couldn\'t find UV set \"%s\" on %s' % (uvset_name, shape))

    return None

def assign_uvset(uv_set, texture_node):
    # pm.uvLink is really buggy.  It only works if the texture is actually connected
    # to a material that the shape is using, even though none of the connections it's
    # making need that.  Let's just do it ourself.
    # Find the place2dTexture node for the material.
    conns = pm.listConnections('%s.uvCoord' % texture_node, s=True, d=False)
    if not conns:
        raise RuntimeException('Texture %s has no place2dTexture node attached' % texture_node)
    place2d = conns[0]

    # See if there's already a uvChooser for this place2dTexture.
    conns = pm.listConnections('%s.uvCoord' % place2d, s=True, d=False)
    if not conns:
        chooser = createNewNode('uvChooser', nodeName='uvChooser_%s' % texture_node)
        pm.connectAttr('%s.outUv' % chooser, '%s.uvCoord' % place2d)
        pm.connectAttr('%s.outVertexCameraOne' % chooser, '%s.vertexCameraOne' % place2d)
        pm.connectAttr('%s.outVertexUvOne' % chooser, '%s.vertexUvOne' % place2d)
        pm.connectAttr('%s.outVertexUvTwo' % chooser, '%s.vertexUvTwo' % place2d)
        pm.connectAttr('%s.outVertexUvThree' % chooser, '%s.vertexUvThree' % place2d)
    else:
        chooser = conns[0]
        assert pm.nodeType(chooser) == 'uvChooser'

    # See if this texture is already connected to this UV set.
    existing_connections = pm.listConnections('%s.uvSets' % chooser, s=True, d=False, p=True) or []
    if uv_set in existing_connections:
        print 'Already connected'
        return

    # Find the next free index in chooser.uvSets.
    indices = pm.getAttr('%s.uvSets' % chooser, mi=True) or [-1]
    next_idx = max(indices)+1
    uv_set.connect(chooser.attr('uvSets[%i]' % next_idx))

def create_file_2d():
    """
    Create a file node, with a place2dTexture node attached.

    This is similar to importImageFile, but that function spews a lot of junk to
    the console.
    """
    texture = pm.shadingNode('file', asTexture=True, isColorManaged=True, ss=True)
    place = pm.shadingNode('place2dTexture', asUtility=True, ss=True)
    pm.connectAttr('%s.coverage' % place, '%s.coverage' % texture)
    pm.connectAttr('%s.translateFrame' % place, '%s.translateFrame' % texture)
    pm.connectAttr('%s.rotateFrame' % place, '%s.rotateFrame' % texture)
    pm.connectAttr('%s.mirrorU' % place, '%s.mirrorU' % texture)
    pm.connectAttr('%s.mirrorV' % place, '%s.mirrorV' % texture)
    pm.connectAttr('%s.stagger' % place, '%s.stagger' % texture)
    pm.connectAttr('%s.wrapU' % place, '%s.wrapU' % texture)
    pm.connectAttr('%s.wrapV' % place, '%s.wrapV' % texture)
    pm.connectAttr('%s.repeatUV' % place, '%s.repeatUV' % texture)
    pm.connectAttr('%s.offset' % place, '%s.offset' % texture)
    pm.connectAttr('%s.rotateUV' % place, '%s.rotateUV' % texture)
    pm.connectAttr('%s.noiseUV' % place, '%s.noiseUV' % texture)
    pm.connectAttr('%s.vertexUvOne' % place, '%s.vertexUvOne' % texture)
    pm.connectAttr('%s.vertexUvTwo' % place, '%s.vertexUvTwo' % texture)
    pm.connectAttr('%s.vertexUvThree' % place, '%s.vertexUvThree' % texture)
    pm.connectAttr('%s.vertexCameraOne' % place, '%s.vertexCameraOne' % texture)
    pm.connectAttr('%s.outUV' % place, '%s.uv' % texture)
    pm.connectAttr('%s.outUvFilterSize' % place, '%s.uvFilterSize' % texture)
    return texture, place

def make_node_name_from_filename(path):
    result = os.path.basename(path)

    # Strip off the extension.
    parts = result.split('.')
    if len(parts) > 1:
        parts = parts[:-1]

    return cleanup_node_name(''.join(parts))

def _find_create_texture(path, required_attributes=None):
    pass

_texture_glossiness_remaps = {}
def remap_glossiness_to_roughness_for_texture(texture_node):
    if texture_node in _texture_glossiness_remaps:
        return _texture_glossiness_remaps[texture_node]

    # Create a remapValue to convert this texture's glossiness map to a roughness map.
    input_prop, output_prop = _glossiness_to_roughness_node()
    pm.connectAttr(texture_node.attr('outAlpha'), input_prop)

    _texture_glossiness_remaps[texture_node] = output_prop
    return output_prop


def _convert_glossiness_to_roughness(value):
    """
    Convert from material glossiness to roughness.
    """
    try:
        return pow((1 - value), 1/3.0)
    except ValueError:
        return 0

def _glossiness_to_roughness_node():
    """
    Create a remapValue to approximate the conversion from glossiness to roughness.
    Returns (input_attribute, output_attribute).
    """
    remap = pm.createNode('remapValue')
    pm.setAttr(remap.attr('inputMin'), 0)
    pm.setAttr(remap.attr('inputMax'), 1)
    pm.setAttr(remap.attr('outputMin'), 0)
    pm.setAttr(remap.attr('outputMax'), 1)
    for i in xrange(20):
        in_value = float(i) / 20
        in_value = pow(in_value, .5)
        out_value = pow((1 - in_value), 1/3.0)
        item = remap.attr('value').elementByLogicalIndex(i)
        pm.setAttr(item.attr('value_Position'), in_value)
        pm.setAttr(item.attr('value_FloatValue'), out_value)
        
    return remap.attr('inputValue'), remap.attr('outValue')

def create_ramp(nodeName):
    ramp_node = pm.shadingNode('ramp', asTexture=True)
    if nodeName:
        ramp_node = pm.rename(ramp_node, nodeName)

    place = pm.shadingNode('place2dTexture', asUtility=True)
    pm.connectAttr('%s.outUV' % place, '%s.uv' % ramp_node)
    pm.connectAttr('%s.outUvFilterSize' % place, '%s.uvFilterSize' % ramp_node)
    return ramp_node, place

def create_clamp_node(min_input, max_input, value_attr, nodeName=None):
    """
    Return an attribute which is 1 where value_attr is between min_input and max_input,
    and 0 outside that range.

    For example, create_clamp_node(0, 100, 'node.outAlpha' returns a connectable attribute
    which is 1 when node.outAlpha is between [0,100].
    """
    cond_node = createNewNode('remapValue', nodeName=nodeName)
    pm.setAttr(cond_node.attr('inputMin'), min_input)
    pm.setAttr(cond_node.attr('inputMax'), max_input)

    # Set the first position to -0.01 instead of 0 to avoid a bug where remapValue
    # points are lost if their data is all zero.
    pm.setAttr('%s.value[0].value_Position' % cond_node, -0.01)
    pm.setAttr('%s.value[0].value_Interp' % cond_node, 0)
    pm.setAttr('%s.value[0].value_FloatValue' % cond_node, 0)

    pm.setAttr('%s.value[1].value_Position' % cond_node, 0.25)
    pm.setAttr('%s.value[1].value_Interp' % cond_node, 0)
    pm.setAttr('%s.value[1].value_FloatValue' % cond_node, 1)

    pm.setAttr('%s.value[2].value_Position' % cond_node, 0.75)
    pm.setAttr('%s.value[2].value_Interp' % cond_node, 0)
    pm.setAttr('%s.value[2].value_FloatValue' % cond_node, 0)

    pm.connectAttr(value_attr, cond_node.attr('inputValue'))

    return cond_node.attr('outValue')

def get_contiguous_indices(indices):
    """
    >>> list(get_contiguous_indices([0,1,2,4,5,6]))
    [(0, 2), (4, 6)]
    """
    range_start = -1
    range_end = -1
    for idx in indices:
        if range_start == -1:
            range_start = idx
            range_end = idx
            continue

        if idx == range_end + 1:
            range_end += 1
            continue

        yield range_start, range_end
        range_start = idx
        range_end = idx

    if range_start != -1:
        yield range_start, range_end


_cached_channels = {}
def flush_channel_cache():
    """
    Flush the cache used by get_channels_cached.
    """
    global _cached_channels
    _cached_channels = {}

def get_channels_cached(attr):
    """
    Maya's vectors are silly: each channel has a name, and you can only access the channels
    if you know the name.  You can't say .color[0], you can only say .color for the whole
    vector or .color.colorR for the red channel.

    Look up the channel names, caching the result.  Call flush_channel_cache to flush the
    cache.
    """
    assert isinstance(attr, pm.PyNode)
    try:
        return _cached_channels[attr]
    except KeyError:
        # Separate everything but the last period-separated part.
#        parts = attr_path.split('.') # a.b.c.d
#        assert len(parts) > 1
#
#        node  = '.'.join(parts[0:-1]) # a.b.c
#        attr = parts[-1] # d
        
        channels = tuple(pm.attributeQuery(attr.attrName(), node=attr.node(), lc=True))

        # We should be able to just say node.attr(attr), but this is broken for things like
        # ramp.colorEntryList[0].colorR, where it returns color instead.
        def get_attr(node, attr):
            return pm.PyNode('%s.%s.%s' % (node.node(), node.longName(), attr))
        node_channels = tuple(get_attr(attr, channel) for channel in channels)

        result = node_channels
        _cached_channels[attr] = result
        return result

def assign_matrix(to_node, from_node):
    pm.setAttr(to_node, pm.getAttr(from_node), type='matrix')

def lock_transforms(node):
    for hide in ('tx', 'ty', 'tz', 'rx', 'ry', 'rz', 'sx', 'sy', 'sz'):
        pm.setAttr(node.attr(hide), lock=True)

def hide_common_attributes(node, transforms=True, visibility=False):
    """
    Hide transform and/or visibility controls on a node.

    Transforms aren't useful on grouping nodes.  Hiding them avoids clutter,
    and making them not keyable prevents them from being automatically added
    to character sets.  Hiding visibility is also useful for nodes that only
    group other grouping nodes and have nothing inside them to hide.
    """
    if transforms:
        for hide in ('tx', 'ty', 'tz', 'rx', 'ry', 'rz', 'sx', 'sy', 'sz'):
            pm.setAttr(node.attr(hide), keyable=False, channelBox=False)

    if visibility:
        pm.setAttr(node.attr('visibility'), keyable=False, channelBox=False)

class MayaLogHandler(logging.Handler):
    def emit(self, record):
        s = self.format(record)
        if record.levelname == 'WARNING':
            pm.warning(s)
            # pm.warning doesn't output to the console.
            print s
        elif record.levelname in ('ERROR', 'CRITICAL'):
            # pm.error shows the error as red in the status bar, but it also only works if
            # you let it throw an exception and kill your script.  It also shows a stack at
            # the place it's called (here), which we don't want.  So, we need to use warning
            # for errors.
            pm.warning(s)
        elif record.levelname == 'INFO':
            print s

        # Write all messages to sys.__stdout__, which goes to the output window.  Only write
        # debug messages here.  The script editor is incredibly slow and can easily hang Maya
        # for an hour if we have a lot of debug logging on, but the output window is reasonably
        # fast.
	sys.__stdout__.write('%s\n' % s)

def setup_logging():
    # Don't propagate logs to the root when we're in Maya.  Maya's default handler for outputting
    # logs to the console is really ugly, so we need to override it.  Clear the handlers list before
    # adding our own, in case this is a reload.
    log.propagate = False
    log.handlers = []
    log.setLevel('DEBUG')
    log.addHandler(MayaLogHandler())

def make_transform_matrix(tr):
    return dt.Matrix(
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            tr[0], tr[1], tr[2], 1)

def parent(node, parent, *args, **kwargs):
    if parent is None:
        kwargs['w'] = True
    else:
        args.append(parent)
    pm.parent(node, parent, *args, **kwargs)

def _create_wrap(control_object, target,
        threshold=0,
        max_distance=0,
        influence_type=2, # 1 for point, 2 for face
        exclusive=False,
        auto_weight_threshold=False,
        render_influences=False,
        falloff_mode=0): # 0 for volume, 1 for surface
    old_selection = pm.ls(sl=True)

    pm.select(target)
    pm.select(control_object, add=True)

    cmd = 'doWrapArgList "7" { "1", "%(threshold)s", "%(max_distance)s", "%(influence_type)s", "%(exclusive)s", "%(auto_weight_threshold)s",  ' \
            '"%(render_influences)s", "%(falloff_mode)s" };' % {
        'threshold': threshold,
        'max_distance': max_distance,
        'influence_type': influence_type,
        'exclusive': 1 if exclusive else 0,
        'auto_weight_threshold': 1 if auto_weight_threshold else 0,
        'render_influences': 1 if render_influences else 0,
        'falloff_mode': falloff_mode,
    }

    deformer_node = mel.eval(cmd)[0]

    # Restore the old selection.
    pm.select(old_selection)

    return pm.PyNode(deformer_node)

def _create_cvwrap(control_object, target):
    """
    Create a wrap deformer with cvwrap, if available.  If the cvwrap plugin isn't available,
    return None.
    """
    if not load_plugin('cvwrap.mll', required=False):
        return None
        
    old_selection = pm.ls(sl=True)

    pm.select(target)
    pm.select(control_object, add=True)
    deformer_node = cmds.cvWrap()

    # Restore the old selection.
    pm.select(old_selection)

    return pm.PyNode(deformer_node)

def wrap_deformer(control_mesh, target,
        use_cvwrap_if_available=False,
        threshold=0,
        max_distance=0,
        influence_type=2, # 1 for point, 2 for face
        exclusive=False,
        auto_weight_threshold=False,
        render_influences=False,
        falloff_mode=0): # 0 for volume, 1 for surface
    # If any nodes are meshes, move up to the transform.
    selection = target.getParent() if target.nodeType() == 'mesh' else target

    # Work around a bit of Maya nastiness.  Creating a wrap deformer doesn't hide the influence
    # mesh normally, it turns a bunch of renderer flags off instead, to make it look like the
    # mesh hasn't been changed and then screw you up later when you render.  We have to save and
    # restore a bunch of properties manually to fix this.
    attributes_hijacked_by_wrap = ('castsShadows', 'receiveShadows', 'motionBlur',
            'primaryVisibility', 'visibleInReflections', 'visibleInRefractions')
    saved_attrs = {attr: control_mesh.attr(attr).get() for attr in attributes_hijacked_by_wrap}

    control_transform = control_mesh.getParent() if control_mesh.nodeType() == 'mesh' else control_mesh

    deformer_node = None
    if use_cvwrap_if_available:
        deformer_node = _create_cvwrap(control_transform, selection)
        if deformer_node is None:
            log.warning('The cvwrap plugin isn\'t available.')

    if deformer_node is None:
        deformer_node = _create_wrap(control_transform, selection, threshold, max_distance, influence_type,
            exclusive, auto_weight_threshold, render_influences, falloff_mode)

    # Restore the attributes that wrap screwed up.
    for attr, value in saved_attrs.items():
        control_mesh.attr(attr).set(value)

    return deformer_node

def vertices_equal(mesh1, mesh2, tolerance=0.001):
    """
    Given two meshes, return true if their vertices are equal.  Faces and other
    shape properties aren't checked.

    This is used to check if a wrap deformer has made a change to a mesh.
    """
    if mesh1.nodeType() == 'transform':
        mesh1 = mesh1.getShape()
    if mesh2.nodeType() == 'transform':
        mesh2 = mesh2.getShape()

    # Use cmds instead of pm here, since pm is slow for retrieving large lists of data.
    mesh1_vertices = cmds.getAttr('%s.vt[*]' % mesh1.name())
    mesh2_vertices = cmds.getAttr('%s.vt[*]' % mesh2.name())
    if len(mesh1_vertices) != len(mesh2_vertices):
        return False

    channels = (0,1,2)
    for idx in xrange(len(mesh1_vertices)):
        v1 = mesh1_vertices[idx]
        v2 = mesh2_vertices[idx]

        for channel in channels:
            delta = abs(v1[channel] - v2[channel])
            if delta > tolerance:
                return False

    return True


def normalize_rotation(node):
    """
    Normalize Euler rotations on a node, outputting the result to an attribute.

    Different Euler rotations can cause the same final rotation.  For example, (0,0,0),
    (360,0,0) and (180,180,180) are the same.  Our input modifiers for corrective morphs
    are triggered by euler rotations, which causes them to break when they're driven
    by constraints or IK that doesn't necessarily give the "simplest" rotation.

    Within the [180,-180] range, there are eight alternative rotations for any rotation,
    found by adding (180,180,180) with the sign of each channel set to each direction.
    For example, (0,0,0) has (180,180,180), (180,180,-180), (180,-180,180), and so on.

    Normalize by first wrapping the rotation to the [-180,+180] range.  Then, check
    each alternative rotation, and find the one which gives the smallest magnitude
    rotation when treating the rotation as a vector.  For example, we prefer (0,0,0)
    over (180,180,180).
    """
    pm.addAttr(node, longName='normalizedRotation', sn='normRot', at='double3')
    pm.addAttr(node, longName='normalizedRotationX', sn='normRotX', at='doubleAngle', p='normalizedRotation')
    pm.addAttr(node, longName='normalizedRotationY', sn='normRotY', at='doubleAngle', p='normalizedRotation')
    pm.addAttr(node, longName='normalizedRotationZ', sn='normRotZ', at='doubleAngle', p='normalizedRotation')

    # Note that we don't currently use uc='none'.  We can, by setting $pi to pi, and it results
    # in fewer nodes.  However, it breaks rigging._create_twist_rig, which reconnects our inputs,
    # since it causes a unitConversion node to be inserted.
    expr = '''
$pi = 180;
$pi2 = $pi*2;
$pi3 = <<$pi,$pi,$pi>>;

vector $vec1 = <<%(node)s.rotateX, %(node)s.rotateY, %(node)s.rotateZ>>;

// Wrap the rotation to [-180,180] on each axis.
$vec1 = <<$vec1.x - (floor($vec1.x / $pi2 + 0.5) * $pi2),
          $vec1.y - (floor($vec1.y / $pi2 + 0.5) * $pi2),
          $vec1.z - (floor($vec1.z / $pi2 + 0.5) * $pi2)>>;

vector $vec2 = <<$vec1.x+$pi, $pi-$vec1.y, $vec1.z+$pi>>;
$vec2 = <<$vec2.x - (floor($vec2.x / $pi2 + 0.5) * $pi2),
          $vec2.y - (floor($vec2.y / $pi2 + 0.5) * $pi2),
          $vec2.z - (floor($vec2.z / $pi2 + 0.5) * $pi2)>>;

vector $closest = $vec1;
$mag = mag($vec1);

$vec2_mag = mag($vec2);
if($vec2_mag < $mag)
    $closest = $vec2;

%(node)s.normalizedRotationX = $closest.x;
%(node)s.normalizedRotationY = $closest.y;
%(node)s.normalizedRotationZ = $closest.z;
    '''
#    expr = '''
#%(node)s.normalizedRotationX = %(node)s.rotateX;
#%(node)s.normalizedRotationY = %(node)s.rotateY;
#%(node)s.normalizedRotationZ = %(node)s.rotateZ;
#    '''

    expr = expr % {
        'node': node.name(),
    }

    pm.expression(string=expr, uc='all', alwaysEvaluate=False)
#    pm.connectAttr(node.attr('rotate'), node.attr('normalizedRotation'))
  
    return node.attr('normalizedRotation')

def create_rigidity_transform(rigidity_group, name):
    """
    Create a zOBBTransform configured to the given rigidity group.
    """
    load_plugin('zOBBTransform.py')
    obb_transform = pm.createNode('zOBBTransform', n='RigidTransform_' + name)

    # Read the rotation_mode property, and map it to the closest equivalent on OBBTransform.
    rotation_mode_string = rigidity_group.get_value('rotation_mode')
    rotation_modes = {
        'none': 0,
        'full': 1,
        'primary': 2,
        'secondary': 3,
    }
    rotation_mode = rotation_modes.get(rotation_mode_string.lower(), 0)
    obb_transform.attr('rotationMode').set(rotation_mode)

    # Read the rotation_mode property, and map it to the scale weight vectors.
    scale_mode_strings = rigidity_group.get_value('scale_modes')
    scale_modes = {
        'none': (0,0,0),
        'primary': (1,0,0),
        'secondary': (0,1,0),
        'tertiary': (0,0,1),
    }
    obb_transform.attr('scaleWeightPrimary').set(scale_modes.get(scale_mode_strings[0], (1,0,0)))
    obb_transform.attr('scaleWeightSecondary').set(scale_modes.get(scale_mode_strings[1], (1,0,0)))
    obb_transform.attr('scaleWeightTertiary').set(scale_modes.get(scale_mode_strings[2], (1,0,0)))

    return obb_transform

class BlendShapeInfo(object):
    def __init__(self, name, blend_shape_idx):
        self.name = name
        self.blend_shape_idx = blend_shape_idx
        self.weight_maya_attr = None

    def __repr__(self):
        if self.weight_maya_attr is not None:
            return 'BlendShapeInfo(%s on %s)' % (self.name, self.weight_maya_attr.nodeName())
        else:
            return 'BlendShapeInfo(%s)' % (self.name)

class BlendShapeDeformer(object):
    """
    This class simplifies creation of blendShape deformers.
    """
    def __init__(self, meshes, deformer_name, origin='local'):
        """
        meshes is a list of Maya shape nodes that this deformer will affect.

        Note that the deformer will not be created until a target is added.
        """
        assert len(meshes) >= 1
        assert origin in ('local', 'world'), origin
        self.meshes = meshes
        self.deformer_name = deformer_name
        self.blend_shape_node = None
        self._morphs = {}
        self.next_blend_shape_index = 0
        self.origin = origin

    def __repr__(self):
        return 'BlendShapeDeformer(%s on %s)' % (self.deformer_name, self.meshes)

    def _create(self):
        """
        Create the deformer if it doesn't already exist.
        """
        if self.blend_shape_node is not None:
            # The blendShape deformer has already been created.
            return

        # Create the blendShape deformer for this mesh.
        blend_shape_node = pm.blendShape(self.meshes[0])[0]
        for other_mesh in self.meshes[1:]:
            pm.blendShape(blend_shape_node, edit=True, geometry=other_mesh)

        # Use the node's name to name the blendShape.
        pm.rename(blend_shape_node, cleanup_node_name(self.deformer_name))

        blend_shape_node.attr('origin').set(0 if self.origin == 'world' else 1)

        self.blend_shape_node = blend_shape_node

    def get_blend_shape_info_by_name(self, name):
        try:
            return self._morphs[name]
        except KeyError:
            return None

    def add_blend_shape(self, mesh, target_mesh, name):
        """
        Create a blend shape target from a target Maya shape.
        """
        # Create the blendShape deformer, if we haven't yet.
        self._create()

        morph_info = self._morphs.get(name)
        is_new = morph_info is None
        if is_new:
            morph_info = BlendShapeInfo(name, self.get_unused_blend_shape_index())
            self._morphs[name] = morph_info
            
            # Set the weight to zero before we create it.
            self.blend_shape_node.attr('weight').elementByLogicalIndex(morph_info.blend_shape_idx).set(0)

        blend_shape_idx = morph_info.blend_shape_idx

        pm.blendShape(self.blend_shape_node, edit=True, t=(mesh, blend_shape_idx, target_mesh, 1.0))

        # Remove the alias pm.blendShape created, since we can't control the name it uses,
        # and create a new one.  Note that blendShape will rename the attribute each time we
        # add a target to a different member of the deformer, so we need to do this every
        # time.
        weight_attr = self.blend_shape_node.attr('weight').elementByLogicalIndex(blend_shape_idx)
        old_alias = pm.aliasAttr(weight_attr, q=True)
        pm.aliasAttr(self.blend_shape_node.attr(old_alias), rm=True)

        blend_shape_target_name = cleanup_node_name(name)
        blend_shape_target_name = aliasAttr(blend_shape_target_name, weight_attr)

        morph_info.weight_maya_attr = self.blend_shape_node.attr(blend_shape_target_name)
        
        return self._morphs[name]

    def get_blend_shape_deltas(self, mesh, name):
        """
        Return (deltas, delta_vertex_idx) for a blend shape.
        """
        assert mesh in self.meshes, 'Mesh %s not in (%s)' % (mesh, self.meshes)

        # Find the index of this mesh in the deformer.
        input_idx = self.meshes.index(mesh)

        morph_info = self._morphs.get(name)
        assert morph_info is not None, 'Shape "%s" doesn\'t exist' % name

        # Create the blend shape as a delta target directly.  Maya's data format and DSON's line
        # up here, so this is fast.
        input_target = self.blend_shape_node.attr('it').elementByLogicalIndex(input_idx)
        input_target_group = input_target.attr('itg').elementByLogicalIndex(morph_info.blend_shape_idx)
        input_target_item = input_target_group.attr('iti').elementByLogicalIndex(6000)
        input_points_target = input_target_item.attr('ipt')
        input_components_target = input_target_item.attr('ict')

        existing_components = input_components_target.get() or []

        # cmds instead of pm for performance:
        deltas = cmds.getAttr(input_points_target.name())
        delta_vertex_idx = util.ComponentList(existing_components).get_flat_list()

        return deltas, delta_vertex_idx

    def add_blend_shape_from_deltas(self, mesh, deltas, delta_vertex_idx, name):
        """
        Create a blend shape target from a list of deltas and vertex indices.
        """
        # Find the index of this mesh in the deformer.
        input_idx = self.meshes.index(mesh)

        # If we already have a target with this name, then we're creating deltas for a different
        # mesh on the same shape, so we'll use the same index.  Otherwise, allocate a new blend
        # shape index.
        self._create()
        morph_info = self._morphs.get(name)
        is_new = morph_info is None
        if is_new:
            morph_info = BlendShapeInfo(name, self.get_unused_blend_shape_index())
            self._morphs[name] = morph_info
            
            # Set the weight to zero before we create it.
            self.blend_shape_node.attr('weight').elementByLogicalIndex(morph_info.blend_shape_idx).set(0)

        blend_shape_idx = morph_info.blend_shape_idx

        # Create the blend shape as a delta target directly.  Maya's data format and DSON's line
        # up here, so this is fast.
        input_target = self.blend_shape_node.attr('it').elementByLogicalIndex(input_idx)
        input_target_group = input_target.attr('itg').elementByLogicalIndex(blend_shape_idx)
        input_target_item = input_target_group.attr('iti').elementByLogicalIndex(6000)
        input_points_target = input_target_item.attr('ipt')
        input_components_target = input_target_item.attr('ict')

        existing_components = input_components_target.get()
        assert not existing_components
#        if existing_components:
#            # There are already components here.  We need to merge the blend shape data into it.
#            # This is annoying, since there doesn't seem to be any interface for working with
#            # component lists.  MFnComponentListData doesn't accept the MObject, and PyMel doesn't
#            # seem to have anything either.
#            #
#            # This is an array of ['vtx[1]', 'vtx[10:20]', ...].  There shouldn't be anything except
#            # for vertices, so the prefix is always vtx.
#            flat_list = util.ComponentList(existing_components).get_flat_list()
#            num_components = max(max(delta_vertex_idx), max(flat_list)) + 1
#
#            # Expand the deltas to a simple list.
#            existing_values = input_points_target.get()
#
#            new_deltas = [(0,0,0)] * num_components
#            for idx, value in zip(flat_list, existing_values):
#                new_deltas[idx] = value
#
#            # Apply any new deltas.
#            for idx, value in zip(delta_vertex_idx, deltas):
#                new_deltas[idx] = value
#
#            # Make a list of the nonzero indices in the new delta list.
#            def is_nonzero(value):
#                return any(abs(v) > 0.001 for v in value)
#
#            delta_vertex_idx = [idx for idx, delta in enumerate(new_deltas) if is_nonzero(delta)]
#            deltas = [new_deltas[idx] for idx in delta_vertex_idx]

        ict_list = []
        for start, end in get_contiguous_indices(delta_vertex_idx):
            ict_list.append('vtx[%i:%i]' % (start, end))

        pm.setAttr(input_components_target, len(ict_list), *ict_list, type='componentList')
        pm.setAttr(input_points_target, len(deltas), *deltas, type='pointArray')

        if is_new:
            weight_attr = self.blend_shape_node.attr('weight').elementByLogicalIndex(blend_shape_idx)

            blend_shape_target_name = cleanup_node_name(name)
            blend_shape_target_name = aliasAttr(blend_shape_target_name, weight_attr)

            morph_info.weight_maya_attr = self.blend_shape_node.attr(blend_shape_target_name)

        return self._morphs[name]

    def set_blend_shape_weights(self, morph_info, mesh, weights):
        """
        Set the weights for a blendShape target.  morph_info is the result of a previous
        call to add_blend_shape or add_blend_shape_from_deltas, or None to apply weights
        to the overall blendShape.  mesh is the deformed mesh to apply weights for.
        """
        # Find the index of this mesh in the deformer.
        input_idx = self.meshes.index(mesh)
        input_target = self.blend_shape_node.attr('it').elementByLogicalIndex(input_idx)
        
        if morph_info is not None:
            blend_shape_idx = morph_info.blend_shape_idx

            input_target_group = input_target.attr('itg').elementByLogicalIndex(blend_shape_idx)
            weight_attr = input_target_group.attr('tw')
        else:
            weight_attr = input_target.attr('bw')

        # Why can't we set these all at once?
        for idx, weight in enumerate(weights):
            weight_attr.elementByLogicalIndex(idx).set(weight)

    def get_input_geometry(self, mesh):
        """
        Return the input[] attribute for the given mesh, which must be one of the meshes
        specified when this object was created.  The blendShape deformer will be created
        if it doesn't exist.

        The inputGeometry and groupId attributes can be accessed from here, to read the
        input into the blendShape.
        """
        self._create()
        input_idx = self.meshes.index(mesh)
        return self.blend_shape_node.attr('input').elementByLogicalIndex(input_idx)

    def get_output_geometry(self, mesh):
        """
        Return the output geometry for the given mesh, which must be one of the meshes
        specified when this object was created.  The blendShape deformer will be created
        if it doesn't exist.
        """
        self._create()
        input_idx = self.meshes.index(mesh)
        return self.blend_shape_node.attr('outputGeometry').elementByLogicalIndex(input_idx)

    @property
    def morph_names(self):
        return self._morphs.keys()

    @property
    def morphs(self):
        return self._morphs.values()

    def get_unused_blend_shape_index(self):
        idx = self.next_blend_shape_index
        self.next_blend_shape_index += 1
        return idx

class ProgressWindowMaya(util.ProgressWindow):
    main_progress_value = 0

    def __init__(self):
        super(ProgressWindowMaya, self).__init__()
        self.window = None
        self.last_refresh = None

    def show(self, title, total_progress_values):
        super(ProgressWindowMaya, self).show(title, total_progress_values)

        self.window = cmds.window(title=title)
        cmds.columnLayout()
        
        cmds.text('status', w=300, align='left')
        self.progressControl1 = cmds.progressBar(maxValue=total_progress_values, width=300)

        cmds.text('status2', w=300, align='left')
        self.progressControl2 = cmds.progressBar(maxValue=100, width=300, pr=5)
        cmds.button(label='Cancel', command=self._cancel_clicked)
        cmds.showWindow(self.window)
        pm.refresh()

    def hide(self):
        super(ProgressWindowMaya, self).hide()
        
        cmds.deleteUI(self.window)
        self.window = None

    def _cancel_clicked(self, unused):
        log.debug('Cancel button clicked')
        self.cancel()

    def set_main_progress(self, job):
        super(ProgressWindowMaya, self).set_main_progress(job)
        
        # Reset the sub-task refresh timer when we change the main task.
        self.last_refresh = None
        self.last_task_percent = 0
        
        log.info(job)

        if self.window is None:
            return

        pm.text('status', e=True, label=job)
        pm.text('status2', e=True, label='')
        cmds.progressBar(self.progressControl1, edit=True, progress=self.main_progress_value)
        cmds.progressBar(self.progressControl2, edit=True, progress=0)

        # Hack: The window sometimes doesn't update if we don't call this twice.
        pm.refresh()
        pm.refresh()

        self.main_progress_value += 1

    def set_task_progress(self, label, percent=None, force=False):
        super(ProgressWindowMaya, self).set_task_progress(label, percent=percent, force=force)

#        log.debug(label)

        if percent is None:
            percent = self.last_task_percent
            
        self.last_task_percent = percent

        if self.window is None:
            return

        # Only refresh if we haven't refreshed in a while.  This is slow enough that it
        # can make the import slower if we're showing fine-grained progress.
        if not force and self.last_refresh is not None and time() - self.last_refresh < 0.1:
            return

        self.last_refresh = time()

        pm.text('status2', e=True, label=label)
        cmds.progressBar(self.progressControl2, edit=True, progress=round(percent * 100))

#        pm.refresh()
#        pm.refresh()

