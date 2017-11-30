import copy, logging
from . import DSON
from dsonimport.tcb import KochanekBartelsSpline

from pprint import pprint, pformat

log = logging.getLogger('DSONImporter')
xlog = logging.getLogger('DSONImporter.modifiers')
xlog.setLevel(logging.INFO)

def _load_modifier_dependencies(dson_modifier):
    if 'formulas' not in dson_modifier:
        return
    
    dson_formulas = dson_modifier['formulas']
    for dson_formula in dson_formulas.array_children:
        output_prop = dson_formula['output']
        parsed_url = output_prop.get_parsed_url()
        if parsed_url.path:
            dson_formula.node.env.get_or_load_file(parsed_url.path, allow_fail=True)

        # Resolve all URLs in the formula now, so we don't have to keep the modifier reference around
        # and so formulas can be moved around without breaking references.
        for op in dson_formula.get_property('operations').array_children:
            if 'url' not in op:
                continue

            url = op['url']
            parsed_url = url.get_parsed_url()
            if not parsed_url.path:
                continue

            dson_formula.node.env.get_or_load_file(parsed_url.path, allow_fail=True)

def recursively_load_modifier_dependencies(env):
    # Modifiers can cause new modifiers to be loaded as we process them.  Keep looking for
    # new modifiers until we make a pass and don't find any we didn't see before.
    seen_modifiers = set()
    while True:
        saw_new_modifiers = False

        # Search both the scene and library.
        for node in env.depth_first():
            if node in seen_modifiers:
                # We've already loaded this modifier.
                continue
            seen_modifiers.add(node)
            saw_new_modifiers = True

            # If this is an alias, load the target of the alias.
            if node.get_value('channel/type') == 'alias':
                target_channel_url = node['channel/target_channel']

                path = target_channel_url.get_parsed_url().path
                if path:
                    node.env.get_or_load_file(path)

            # Load inputs and outputs of formulas.
            _load_modifier_dependencies(node)

        if not saw_new_modifiers:
            break

class DSONFormula(object):
    def __repr__(self):
        return 'DSONFormula(%s)' % self.name

    @classmethod
    def create_from_modifier(cls, target_node, dson_formula):
        output_property_url = dson_formula['output']
        output_property = target_node.get_modifier_url(output_property_url)

        # 'url' in each operation is a DSONProperty.  Replace it with its value.
        operations = []
        for op in dson_formula.get_property('operations').array_children:
            # Copy the raw value of the DSONProperty operation, so we have a simple dictionary
            # that we can modify.
            operation = copy.deepcopy(op.value)
            if 'url' in operation:
                # Resolve URLs.  We need the original DSONProperty to do this, so it's easiest
                # to do it now.
                prop = target_node.get_modifier_url(op['url'])
                operation['prop'] = prop
                del operation['url']

            operations.append(operation)

        stage = dson_formula.value.get('stage', 'sum')
        return cls.create(repr(dson_formula), output_property, operations, stage)

    @classmethod
    def create(cls, name, output_property, operations, stage='sum'):
        formula = cls(output_property, operations, stage, name)

        # Save the formula to the property path on the node.
        formula_list = output_property.node.formulas.setdefault(output_property.path, [])
        formula_list.append(formula)

        return formula

    def copy_with_new_operations(self, operations):
        return DSONFormula(self.output_property, operations, self.stage, self.name)

    def __init__(self, output_property, operations, stage, name):
        self.output_property = output_property
        self.operations = operations
        self.stage = stage
        self.name = name

    def dump(self):
        xlog.debug('%s: output %s', self, self.output_property)
        xlog.debug('Operations (%i):', len(self.operations))
        for operation in self.operations:
            xlog.debug('    %s', operation)

    @classmethod
    def property_is_always_zero(cls, prop):
        formulas = prop.node.formulas.get(prop.path) or []
        for formula in formulas:
            if formula.get_stage() != 'mult':
                continue
            value = formula.constant_value
            if value is None:
                # This property isn't constant.
                continue

            if value == 0:
                return True

        return False

#    @classmethod
#    def property_is_constant(cls, prop):
#        """
#        Return true if the value of this DSONProperty is constant.
#
#        A DSONProperty is constant if it has no formulas, or if all of its formulas
#        have constant values.
#        """
#        formulas = prop.node.formulas.get(prop.path) or []
#        for formula in formulas:
#            if not formula.is_constant:
#                return False
#
#        return True

    @classmethod
    def evaluate_formula_list(cls, formula_list, static_value=0, use_default_values=False):
        """
        Evaluate a list of formulas.

        static_value is the value/current_value of the node, which is added in the sum stage.
        If a mult stage formula multiplies by zero, the output value is zero regardless of the
        static value.
        """
        result = static_value

        # First, evaluate all sum stage formulas.
        for formula in formula_list:
            if formula.stage != 'sum':
                continue

            # xlog.debug('-- Evaluate: %s', formula)
            result += formula.evaluate(use_default_values=use_default_values)
            # xlog.debug('-- Result: %s', result)

        # Next, evaluate multiply stage formulas. XXX untested
        for formula in formula_list:
            if formula.stage != 'mult':
                continue
            result *= formula.evaluate(use_default_values=use_default_values)
        return result

    @classmethod
    def formula_list_multiplies_by_zero(cls, formula_list):
        """
        Return true if formula_list always evaluates to zero.
        """
        # If any formula in formula_list multiplies by zero, the result is always zero.
        for formula in formula_list:
            if formula.get_stage() != 'mult':
                continue

            constant_value = formula.constant_value
            if constant_value is not None and abs(constant_value) < 0.0001:
                return True

        return False

    @classmethod
    def formula_list_is_constant(cls, formula_list):
        """
        Return true if the result of formula_list is always zero.

        This differs from formula_list_multiplies_by_zero due to user controls.  If a
        property's sum stage is always 0, the result is always 0 as long as there's no
        user control to add to it.  If there's a user control, the value is always 0
        only if there's a mult stage multiplying it by 0.
        """
        if cls.formula_list_multiplies_by_zero(formula_list):
            return True

        # If all formulas (sum and mult) are constant, the result is constant.
        if all(formula.constant_value is not None for formula in formula_list):
            return True

        # A formula list is (sum1+sum2+sum3)*mult1*mult2.  We checked if the mult part
        # evaluates to zero above.  If all of the sum values are constant and they add
        # to 0, then we're also multiplying by zero and the result is constant.
        sum_total = 0
        for formula in formula_list:
            if formula.get_stage() != 'sum':
                continue

            constant_value = formula.constant_value
            if constant_value is None:
                return False

            sum_total += constant_value

        return abs(sum_total) < 0.0001

    @property
    def input_properties(self):
        """
        Return a set of the DSONProperties that are inputs into this formula.
        """
        result = set()
        for op in self.operations:
            op_type = op['op']
            if op_type == 'push' and 'prop' in op:
                prop = op['prop']
                result.add(prop)
                    
        return result

    def is_simple_multiply_by_ref(self):
        """
        If this formula is a simple "input * constant", return the Maya path to the variable
        input and the constant factor.  Otherwise, return None, None.

        This is the most common expression, for multiplying a constant value by the value
        of a modifier.  We optimize this case by not creating a formula for it, instead using
        the weighting on blendWeighted.  This is how set driven key works.
        """
        value = self.operations
        if len(value) != 3:
            return None, None

        # Check that this is "push, push, mult".
        if value[0]['op'] != 'push' or value[1]['op'] != 'push' or value[2]['op'] != 'mult':
            return None, None

        def find_push_by_type(op_type):
            for op in self.operations:
                if op_type in op:
                    return op[op_type]
            return None

        # One of the pushes needs to be a node input, and the other a constant.
        prop = find_push_by_type('prop')
        val = find_push_by_type('val')
        if prop is None or val is None:
            return None, None

        return prop, val

    @property
    def is_constant(self):
        return self.constant_value is not None

    @property
    def constant_value(self):
        """
        If this formula is really just a constant value, return it.  Otherwise, return None.
        """
        if len(self.operations) != 1:
            return None

        # If there's only one entry, it has to be a PUSH.
        assert self.operations[0]['op'] == 'push'

        return self.operations[0].get('val')

    def get_stage(self):
        assert self.stage in ('sum', 'mult')
        return self.stage

    # Since we don't import every property dynamically, there's a lot of extra work happening
    # in formulas that we need to optimize out.
    # 
    # - If a formula pushes a property that we're only evaluating statically and not creating
    # dynamic nodes for, evaluate the static value and push that instead.
    # - If math is performed that only involves constant values, collapse it to a constant.
    # 
    # For example, if a formula does:
    # 
    # PUSH arm/rotation/x
    # PUSH .25
    # MULT
    # 
    # and arm/rotation/x isn't dynamic, evaluate its current value and push that instead:
    # 
    # PUSH 45
    # PUSH .25
    # MULT
    # 
    # This then collapses to a single PUSH:
    # 
    # PUSH 11.25
    # 
    # Aside from giving a simpler network, this allows us to tell in advance that a formula
    # doesn't actually read from a property, and otherwise behave as if it didn't have the
    # dependency on the input property at all.
    def _bake_constants(self):
        """
        Optimize by replacing PUSH operations that refer to non-dynamic properties with the current
        value of the property.

        For example, if a formula is

        PUSH value
        PUSH 2
        MULT

        and value points to a non-dynamic property with a value of 3, replace the PUSH, giving

        PUSH 3
        PUSH 2
        MULT
        """
        stack = []
        for op in self.operations:
            op_type = op['op']
            if op_type != 'push':
                # Just push unknown ops back onto the stack.
                stack.append(op)
                continue

            # Look up the input property.  If this is any kind of PUSH except for a property reference,
            # just push the operation as-is.
            input_prop = op.get('prop')
            if input_prop is None:
                # We're pushing a constant value.
                stack.append(op)
                continue

            if input_prop.is_dynamic:
                # This is pointing to another dynamic DSONProperty.  Keep the reference.
                stack.append(op)
                continue

            # This points to a DSONProperty that we're not going to evaluate dynamically.
            # Just evaluate its current value and push it.
            value = input_prop.evaluate()

            stack.append({
                'op': 'push',
                'val': value
            })

        # Replace our operations list with the optimized list.
        self.operations = stack 

    def optimize(self):
        """
        Optimize constant expressions in this formula.

        For example, change (2*3) with 6, and (0*N) with 0, ignoring N.
        """
        self._bake_constants()

        stack = []
        def pop():
            assert len(stack) >= 1
            value = stack[-1]
            stack[-1:] = []
            return value

        for op in self.operations:
            op_type = op['op']
            if op_type == 'mult':
                # Pop the top two values from the stack.
                vals = [pop(), pop()]

                is_constant = 'val' in vals[0] and 'val' in vals[1]
                if is_constant:
                    # Optimization: Both inputs are constant, so just output a constant.
                    stack.append({
                        'op': 'push',
                        'val': vals[0]['val'] * vals[1]['val']
                    })
                    continue

                if ('val' in vals[0] and vals[0]['val'] == 0) or ('val' in vals[1] and vals[1]['val'] == 0):
                    # Optimization: This is val1*0 or 0*val2.  Output 0.
                    stack.append({
                        'op': 'push',
                        'val': 0
                    })
                    continue

                if 'val' in vals[1] and vals[1]['val'] == 1:
                    # Optimization: This is just val1*1, so just output val1.
                    stack.append(vals[0])
                    continue

                if 'val' in vals[0] and vals[0]['val'] == 1:
                    # Optimization: This is just 1*val2, so just output val2.
                    stack.append(vals[1])
                    continue

                # We can't optimize this, so just push the operands and operation back onto the stack.
                stack.append(vals[0])
                stack.append(vals[1])
                stack.append(op)
            elif op_type == 'spline_tcb':
                num_keyframes = pop()
                assert 'val' in num_keyframes

                keyframes = []
                for idx in xrange(num_keyframes['val']):
                    keyframe = pop()
                    assert 'val' in keyframe
                    keyframes.append(keyframe)

                value = pop()

                # If any of the inputs aren't constant, just push everything back onto the stack
                # and continue.
                if not all('val' in key for key in keyframes) or 'val' not in value:
                    stack.append(value)
                    for keyframe in keyframes:
                        stack.append(keyframe)
                    stack.append(num_keyframes)
                    stack.append(op)
                    continue

                # All spline inputs are constant, so evaluate and push constant result.
                keyframe_values = []
                for key in keyframes:
                    assert len(key['val']) == 5
                    keyframe_values.append(key['val'])

                spline = KochanekBartelsSpline(keyframe_values)
                result = spline.evaluate(value['val'])
                stack.append({
                    'op': 'push',
                    'val': result
                })

            else:
                # Just push unknown ops back onto the stack.
                stack.append(op)

        # Replace our operations list with the optimized list.
        self.operations = stack 

    def evaluate(self, use_default_values=False):
        """
        Statically evaluate the value of this formula, using the scene's current values.
        """
        # This stack only contains simple numeric values.
        stack = []
        def pop():
            assert len(stack) >= 1
            result = stack[-1]
            stack[-1:] = []
            return result
        
        for op in self.operations:
            op_type = op['op']
            if op_type == 'push':
                if 'prop' in op:
                    value = op['prop'].evaluate(use_default_values=use_default_values)
                elif 'val' in op:
                    value = op['val']
                else:
                    raise RuntimeError('Unsupported \"push\" operand: %s' % op)

                stack.append(value)
            elif op_type == 'mult':
                # Pop the top two values from the stack.
                assert len(stack) >= 2
                vals = [stack[-1], stack[-2]]
                stack = stack[:-2]

                value = vals[0] * vals[1]

                # log.debug('mult %f * %f = %f', vals[0], vals[1], value)
                stack.append(value)
            elif op_type == 'spline_tcb':
                num_keyframes = pop()

                keyframes = []
                for idx in xrange(num_keyframes):
                    keyframes.append(pop())

                input_value = pop()

                keyframe_values = []
                for key in keyframes:
                    assert len(key) == 5
                    keyframe_values.append(key)

                spline = KochanekBartelsSpline(keyframe_values)
                result = spline.evaluate(input_value)
                stack.append(result)

            elif op_type in ('spline_linear', 'spline_constant'):
                # Keyframes.  These aren't supported yet, but parse them out.
                # XXX
                assert len(stack) >= 1
                num_keyframes = stack[-1]
                stack = stack[:-1]

                assert len(stack) >= num_keyframes, '%i < %i' % (len(stack), num_keyframes)
                stack = stack[:-num_keyframes]

                # The next stack entry is the input into the spline.  We should pop it and replace it
                # with a reference to the result.  Since we don't support this yet, we just pop it and
                # replace it with a dummy output value.
                assert len(stack) >= 1, '%i < %i' % (len(stack), num_keyframes)
                stack = stack[:-1]

                stack.append(0)

            else:
                raise RuntimeError('Unsupported op: %s' % op_type)

        # We should be left with one result.
        assert len(stack) == 1, 'Unbalanced formula stack'
        result = stack[0]

        return result

class Helpers(object):
    @staticmethod
    def load_all_modifiers(env):
        """
        Create DSONModifiers for all modifiers in the scene.
        """
        for dson_node in env.scene.breadth_first():
            assert dson_node.is_instanced, dson_node

            # Find all modifiers that affect this node.  Modifiers are immediate children of the ndoe
            # they're attached to.
            modifiers = []
            for child in dson_node.children:
                # We're actually looking for nodes with formulas, not just modifiers.  Nodes can have
                # formulas on them without being modifiers.
                if 'formulas' not in child:
                    continue

                assert child.is_instanced, child

                # Search up through our parents to find a top-level node, which is usually a figure.
                target_node = child.find_top_node()

                # Create DSONFormulas for each formula in this modifier.  We don't need to track the
                # DSONFormula objects, since they'll attach themselves to properties.
                for dson_formula in child['formulas'].array_children:
                    try:
                        DSONFormula.create_from_modifier(target_node, dson_formula)
                    except DSON.NodeNotFound as e:
                        # This will be raised if the output or any inputs refer to a node that isn't loaded.
                        log.warning('Ignoring formula %s with unknown dependency: %s', dson_formula, e.message)
                        continue

    @staticmethod
    def optimize_all_modifiers(env):
        """
        Optimize all formulas.

        This will remove redundant and static formulas.  For example, if a modifier isn't dynamic,
        then its constant value will be substituted in each formula that uses it, so that formula
        can be collapsed as far as possible.

        Once this is done, the use_default_values parameter to evaluate() will no longer work,
        since constant values will have been optimized into formulas.
        """
        for dson_node in env.scene.breadth_first():
            for formulas in dson_node.formulas.itervalues():
                for formula in formulas:
                    formula.optimize()

    @staticmethod
    def evaluate(dson_property, use_modifiers=True, use_default_values=False, skip_constant_value=False):
        """
        Evaluate the value of a channel, applying modifiers.

        As a convenience, if use_modifiers is false, the initial value of the channel without
        modifiers is used.

        If use_default_values is true, only default values for channels will be used as inputs, as if no changes
        have been made by the user.

        If skip_constant_value is true, this property will ignore its static value, but formula evaluations
        won't.  This is a hack for apply_transform_orientation_modifiers.
        """
        if skip_constant_value:
            static_value = 0
        elif use_default_values:
            static_value = dson_property.get_default()
        else:
            static_value = dson_property.get_value_with_default()

        if not use_modifiers:
            # It doesn't make sense to use_default_values without use_modifiers.  It would
            # always be zero.
            assert not use_default_values
            return static_value

        formulas = dson_property.node.formulas.get(dson_property.path) or []
        xlog.debug('Evaluating %s (exclude constant %s, static %f, formulas: %i)', dson_property, use_default_values, static_value, len(formulas))

        result = DSONFormula.evaluate_formula_list(formulas, static_value=static_value, use_default_values=use_default_values)
        xlog.debug('Formula result for %s: %f', dson_property, result)

        result = dson_property.apply_limit_to_value(result)

        return result

    @classmethod
    def get_vec3(cls, dson_property, *args, **kwargs):
        x = dson_property.get_property('x')
        y = dson_property.get_property('y')
        z = dson_property.get_property('z')

        x = cls.evaluate(x, *args, **kwargs)
        y = cls.evaluate(y, *args, **kwargs)
        z = cls.evaluate(z, *args, **kwargs)
        return [x,y,z]

    @classmethod
    def add_auto_follow_formulas(cls, env):
        # auto_follow modifiers add the value of any node with the same name in the parent of the
        # node they're affecting.  Add an implicit formula to do this.
        #
        # If this is a "MyCharacter" modifier on top of a "Hair" figure, and the Hair figure is conforming
        # to a TopLevelFigure, then this auto_follow causes this MyCharacter to add the value of a
        # modifier named MyCharacter inside TopLevelFigure:
        #
        # - TopLevelFigure
        #   - MyCharacter
        #   - Hair
        #     - MyCharacter
        #
        # This is used to trigger morphs when a modifier (Hair) is applied to a figure that has another
        # specific morph (MyCharacter) applied.
        #
        # Add a formula to the formula list, adding the target's value to this one.
        for dson_node in env.scene.depth_first():
            if dson_node.node_source != 'modifier' or not dson_node.get_value('channel/auto_follow'):
                continue

            # Read the conform_target of this modifier.
            conform_target = dson_node.get_conform_target()
            if conform_target is None:
                continue

            # Find a name in conform_target that has the same name as this one.  That's the
            # node whose value we'll follow.
            following_node = conform_target.find_asset_name(dson_node.get_value('name'), default=None)
            if following_node is None:
                log.debug('%s (in %s) is auto-follow, but no matching channel with name "%s" was found', dson_node, dson_node.parent, dson_node.get_value('name'))
                continue

            log.debug('%s (in %s) is following %s (in %s)', dson_node, dson_node.parent, following_node, following_node.parent)

            operations = [{
                'prop': following_node['value'],
                'op': 'push',
            }]
            
            x = DSONFormula.create('auto_follow(%s follows %s)' % (dson_node, following_node), dson_node['value'], operations)
            log.debug(x)


