import logging
from dson import modifiers

import maya_helpers as mh
import pymel.core as pm
import mayadson

log = logging.getLogger('DSONImporter')

# This contains the Maya-specific logic for DSONModifier and DSONFormula.
def _get_attr_for_property_value(formula, input_prop):
    result = mayadson.get_maya_output_attribute(input_prop)

    # Add the formula to the list of formulas that depend on this property.  We don't
    # need this for evaluation, but it helps us clean up the scene later.
    input_prop.node.formulas_output.setdefault(input_prop.path, []).append(formula)

    return result

class DSONMayaFormula(object):
    def __init__(self, value, is_maya_property):
        self.value = value
        self.is_maya_property = is_maya_property

def create_maya_output_for_formula(formula):
    """
    Create a Maya node network to evaluate this formula, and return a PyNode for
    an attribute path to its result.

    If the value of this expression is a constant, the constant will be returned
    instead of a PyNode.

    Note that if this formula simply pushes the value of another property, we'll just
    return that property's path and not create any extra nodes.
    """
    stack = []
    
    for op in formula.operations:
        op_type = op['op']
        if op_type == 'push':
            if 'prop' in op:
                # Look up the input property.
                input_prop = op['prop']

                # Non-dynamic properties should be optimized out in optimize(), leaving us with only
                # dynamic ones.
                assert input_prop.is_dynamic

                # This is pointing to another dynamic DSONProperty.  Push the attribute that will
                # hold the result of that property.  Note that calling this will cause the property
                # holder node to be created, so we're careful to only do this now when we're really
                # creating the network, after optimize() has had a chance to optimize it out.
                input_attr = _get_attr_for_property_value(formula, input_prop)
                stack.append({
                    'path': input_attr
                })
                    
            elif 'val' in op:
                # We're pushing a constant value.
                stack.append({
                    'val': op['val']
                })

        elif op_type == 'mult':
            # Pop the top two values from the stack.
            assert len(stack) >= 2
            vals = [stack[-1], stack[-2]]
            stack = stack[:-2]

            # Create a multiplyDivide node to do the multiplication.
            math_node = mh.createNewNode('multiplyDivide', nodeName='DSON_Math')
            pm.setAttr(math_node.attr('operation'), 1) # multiply

            # Push the resulting multiplyDivide node onto the stack.
            stack.append({
                'path': math_node.attr('outputX'),
            })

            # Set or connect the two inputs.
            # If we support more than a couple operations, we should handle the arguments more generically.
            for idx in xrange(len(vals)):
                op = vals[idx]
                if 'val' in op:
                    value_attr = op['val']
#                    pm.setAttr(input_attr, op['val'])
                elif 'path' in op:
                    # This is the Maya path to a node that we created previously.
                    value_attr = op['path']
#                    pm.connectAttr(op['path'], input_attr)
                else:
                    raise RuntimeError('Unsupported \"push\" operand: %s' % op)

                mh.set_or_connect(math_node.attr('input%iX' % (idx+1)), value_attr)
        elif op_type in ('spline_tcb', 'spline_linear', 'spline_constant'):
            # The number of values in the array for each keyframe.  We don't currently try
            # to support TCB values in spline keyframes, since nothing appears to use it.
            values_per_key_map = {
                'spline_tcb': 5,
                'spline_linear': 2,
                'spline_constant': 2,
            }
            values_per_key = values_per_key_map[op_type]

            # The keyframe type on the remapValue node.
            types_per_key_map = {
                'spline_tcb': 3,
                'spline_linear': 1,
                'spline_constant': 0,
            }
            keyframe_type = types_per_key_map[op_type]

            # Keyframes.  These aren't supported yet, but parse them out.
            # XXX
            def pop():
                assert len(stack) >= 1
                result = stack[-1]
                stack[-1:] = []
                return result
            num_keyframes = pop()

            # We only expect to see a constant value for the keyframe count.  It doesn't make sense
            # to reference a variable value.
            assert 'val' in num_keyframes
            num_keyframes = num_keyframes['val']

            keyframes = []
            for idx in xrange(num_keyframes):
                keyframe = pop()

                # We expect the keyframes to be constant.
                assert 'val' in keyframe, keyframe
                assert len(keyframe['val']) == values_per_key, keyframe
                keyframes.append(keyframe['val'])

            # The next stack entry is the input into the spline.  We should pop it and replace it
            # with a reference to the result.  Since we don't support this yet, we just pop it and
            # replace it with a dummy output value.
            value = pop()

            # We should be using animCurveUU here, like set driven key, but Maya makes keyframes
            # unreasonably hard to use: unlike every other Maya node, you can't set properties on
            # it with setAttr (even though that's what .ma files do), so we can't create the node
            # and return the property like we need to.
            math_node = mh.createNewNode('remapValue', nodeName='DSON_Keyframe')

            for idx, keyframe in enumerate(keyframes):
                input_value = keyframe[0]
                output_value = keyframe[1]

                key = math_node.attr('value').elementByLogicalIndex(idx)
                pm.setAttr(key.attr('value_Position'), input_value)
                pm.setAttr(key.attr('value_FloatValue'), output_value)
                pm.setAttr(key.attr('value_Interp'), keyframe_type)

            # Set the input.
            if 'val' in value:
                value_attr = value['val']
            elif 'path' in value:
                # This is the Maya path to a node that we created previously.
                value_attr = value['path']
            else:
                raise RuntimeError('Unsupported \"push\" operand: %s' % value)

            mh.set_or_connect(math_node.attr('inputValue'), value_attr)

            stack.append({
                'path': math_node.attr('outValue'),
            })
        else:
            raise RuntimeError('Unsupported op: %s' % op_type)

    # We should be left with one result.
    assert len(stack) == 1, 'Unbalanced formula stack'
    result = stack[0]

    if 'val' in result:
        return DSONMayaFormula(result['val'], is_maya_property=False)
    else:
        return DSONMayaFormula(result['path'], is_maya_property=True)


