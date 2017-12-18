import unittest
import DSON

import logging
logging.basicConfig()
logging.getLogger().setLevel('DEBUG')

def get_one(entries):
    entries = list(entries)
    assert len(entries) == 1, entries
    return entries[0]


class Tests(unittest.TestCase):
    def test_basic(self):
        # Quick tests that don't require loading assets.
        env = DSON.DSONEnv()

        # These are both root nodes of the scene.
        self.assertTrue(env.scene.is_root)
        self.assertTrue(env.library.is_root)

    def test_foo(self):
        env = DSON.DSONEnv()
        env.get_or_load_file('/t.duf')

        # Test referencing array elements.  This is an extension.
        figure = env.scene.get_url('#Genesis3Female-1')
        joints = figure.get_url('#SkinBinding?skin/joints')
        for idx in xrange(0, 5):
            joint = figure.get_url('#SkinBinding?skin/joints/~%i' % idx)
            assert joint.value is joints.value[idx]

        # Test iterating over children.  This will create DSONProperty objects with URLs using the indexing
        # extension.
        for idx, joint in enumerate(joints.array_children):
            same_joint = joint.node.get_property(joint.path)
            assert joint.value is same_joint.value
            if idx > 5:
                break

#        asset_file = env.get_or_load_file('data/DAZ 3D/Genesis 3/Female/Morphs/DAZ 3D/Base Correctives/pJCMHandDwn_70_L.dsf')
#        modifier = env.scene.get_url('#pJCMHandDwn_70_L')
#        env.scene.get_url('/data/DAZ%203D/Genesis%203/Female/UV%20Sets/DAZ%203D/Base/Base%20Female.dsf#SkinBinding')
#        self.assertIsNotNone(modifier.get_url('rThumb3:'))
        
    def test_loading_uninstanced_assets(self):
        env = DSON.DSONEnv()

        # This file doesn't override #eCTRLMouthSmileOpen.  Until we load the modifier asset, it won't
        # have this modifier at all.
        env.get_or_load_file('/t.duf')
        figure = env.scene.get_url('Genesis3Female:')

        # The modifier isn't loaded yet.
        assert sum(1 for c in figure.children if c.node_id == 'eCTRLMouthSmileOpen') == 0

        # Load the modifier.  This will add the modifier asset under the Genesis3Female asset, and
        # the instance created in t.duf will inherit it.
        asset_file = env.get_or_load_file('/data/DAZ 3D/Genesis 3/Female/Morphs/DAZ 3D/Base Pose/eCTRLMouthSmileOpen.dsf')

        modifier_asset = env.library.get_url('#eCTRLMouthSmileOpen')

        # This modifier is an asset, not an instance.
        self.assertFalse(modifier_asset.is_instanced)

        # Children of assets don't automatically show up in instances.  They need to be
        # instanced explicitly.
        assert not any(c for c in figure.children if c.node_id == 'eCTRLMouthSmileOpen')

        # Explicitly instance the asset.
        DSON.Helpers.recursively_instance_assets(env.scene)

        # Now that we've instanced the modifier, we'll see an instance of it in the figure.
        modifier = get_one(c for c in figure.children if c.node_id == 'eCTRLMouthSmileOpen-1')

        # The instanced asset will be registered in the file of the figure, not the asset.
        assert modifier.dson_file is figure.dson_file

        # This modifier is an instance.
        self.assertTrue(modifier.is_instanced)

        self.assertEqual(modifier.node_type, 'modifier')

        # Test searching for URLs referenced by the modifier.
        assert modifier.get_url('lNasolabialMouthCorner:/data/DAZ%203D/Genesis%203/Female/Genesis3Female.dsf#lNasolabialMouthCorner?translation/x')
        assert modifier.get_url('Genesis3Female:#eCTRLMouthSmileOpen-1?value')

        # The modifier can be found by ID.
        result = figure.get_url('#eCTRLMouthSmileOpen-1')
        assert result is not None

        # The asset of the instance is the original asset.
        self.assertIs(result.asset, modifier_asset)

        # We can read the value of the modifier on the figure.
        result = figure.get_url('Genesis3Female:#eCTRLMouthSmileOpen-1?value')
        assert result is not None

        # Since this property is from an instance, instance is the same as the node.
        result = modifier.get_property('id')
        self.assertIs(result.instance, result.node)

    def test_loading_instanced_assets(self):
        env = DSON.DSONEnv()

        # This file does override #eCTRLMouthSmileOpen.  It'll load the modifier automatically.
        scene_file = env.get_or_load_file('/t2.duf')
        figure = env.scene.get_url('Genesis3Female:')

        # We should see one instance of the modifier, and it should be from the scene file, not
        # the one inherited from the asset.
        modifier = get_one(c for c in figure.children if c.node_id == 'eCTRLMouthSmileOpen')
        assert modifier.dson_file == scene_file

        # The type of this modifier is inherited from the asset, which is inherited from
        # the asset's asset_info.
        self.assertEqual(modifier.node_type, 'modifier')

        # This file's already loaded, so loading it again won't do anything.
        asset_file = env.get_or_load_file('/data/DAZ 3D/Genesis 3/Female/Morphs/DAZ 3D/Base Pose/eCTRLMouthSmileOpen.dsf')
        modifier = get_one(c for c in figure.children if c.node_id == 'eCTRLMouthSmileOpen')
        assert modifier.dson_file == scene_file

        # This modifier is an instance.
        self.assertTrue(modifier.is_instanced)




        # Instanced assets are tricky.  When you look up a property on an instanced modifier and the
        # instance isn't overriding it, you get the property on the underlying asset.  If this is a URL,
        # that URL is resolved relative to the asset, not the instance.
        formulas = modifier['formulas']
        self.assertIs(formulas.__class__, DSON.DSONProperty)
        self.assertIs(formulas.node, modifier.asset)

        # When you look up a property on an instance that isn't overridden, the property comes from the
        # underlying asset.  In this case, .instance points back at the instance you started with.
        self.assertIs(formulas.instance, modifier)

        formula = formulas.value[8]
        self.assertEqual(formula['output'], 'lLipCorver:/data/DAZ%203D/Genesis%203/Female/Genesis3Female.dsf#lLipCorver?translation/x')
        self.assertIsNotNone(formulas.node.get_url(formula['output']))

        # Test searching for URLs referenced by the modifier.
        assert modifier.get_url('lNasolabialMouthCorner:/data/DAZ%203D/Genesis%203/Female/Genesis3Female.dsf#lNasolabialMouthCorner?translation/x')
        assert modifier.get_url('Genesis3Female:#eCTRLMouthSmileOpen?value')

        # This property has no parent, so parent_property raises KeyError.
        with self.assertRaises(KeyError):
            formulas.parent_property

        # Get the output from the first formula.  This property keeps the instance you started with.
        output = formulas.get_property('~0/output')
        self.assertIs(output.instance, modifier)

        # The parent of the output is the array, and the parent of that is the original formulas property.
        # This will point to the same place, but it won't be the same instance of DSONProperty.
        formulas2 = output.parent_property.parent_property
        self.assertIs(formulas2.instance, modifier)
        self.assertEquals(formulas, formulas2)

        # The instance follows through array_children.
        for formula in formulas.array_children:
            self.assertIs(formulas.instance, modifier)
            break

    def test(self):
        #modifier = DSON.env.get_url('/data/DAZ 3D/Genesis 3/Female/Morphs/DAZ 3D/Base Pose/eCTRLMouthSmileOpen.dsf#eCTRLMouthSmileOpen')
        #modifier = DSON.env.get_url('/data/Age%20of%20Armour/Subsurface%20Shaders/AoA_Subsurface/AoA_Subsurface.dsf')
    #    scene = DSON.env.get_url('/Light%20Presets/omnifreaker/UberEnvironment2/!UberEnvironment2%20Base.duf#environmentSphere_1923?extra/studio_node_channels/channels/Renderable/current_value')

        env = DSON.DSONEnv()

        env.get_or_load_file('/t.duf')
        # result = env.scene.get_url('Genesis3Female:')
        # assert result

        # Check that our geometry instance was loaded.
        result = env.scene.get_url('#Genesis3Female-1')
        assert result

        # Test reading the vertex count.  This will search the geometry instance in t.duf, not find it,
        # and then search the base geometry library.
        assert result['vertices/count'] > 0

        # Check that material instances were loaded, and added to the geometry's material list.
        # Materials aren't normally parented under the geometry.
        assert result.materials.get('Face')

        # Test searching for an ID within another ID, by putting the outer ID on the scheme
        # and the inner ID in the fragment.  Note that the path is actually unused if the
        # scheme is present.
        # result = env.scene.get_url('Genesis3Female:/data/DAZ%203D/Genesis%203/Female/Morphs/DAZ%203D/Base%20Pose/eCTRLMouthSmileOpen.dsf#hip')
        # assert result

        # Test searching for a property directly on a node.
        # assert result.get_url('#?label')

        # Search for a property through a scheme search.
        self.assertIsNotNone(env.scene.get_url('Genesis3Female:/data/DAZ%203D/Genesis%203/Female/Morphs/DAZ%203D/Base%20Pose/eCTRLMouthSmileOpen.dsf#hip?label'))

        # Test searching for IDs containing slashes.
        self.assertIsNotNone(env.scene.get_url('/Light%20Presets/omnifreaker/UberEnvironment2/!UberEnvironment2%20Base.duf#DzShaderLight%2FomUberEnvironment2'))

        # Test searching for properties containing slashes.
        result = env.scene.get_url('/data/Age%20of%20Armour/Subsurface%20Shaders/AoA_Subsurface/AoA_Subsurface.dsf#AoA_Subsurface')
        self.assertIsNotNone(result)
        result = result.get_url('#?extra/studio%2Fmaterial%2Fdaz_brick')
        self.assertIsNotNone(result)

        # This returns a DSONProperty.
        assert isinstance(result, DSON.DSONProperty)

        # Force Genesis3Female.dsf to be loaded.
        env.get_or_load_file('/data/DAZ 3D/Genesis 3/Female/Genesis3Female.dsf')

        # Test searching the whole scene by scheme.
        result = env.scene.get_url('Genesis3Female:')
        assert result

        # Test searching the whole scene by fragment.
        result = env.scene.get_url('#Genesis3Female')
        assert result

        # Test searching from one node to another, where they're in the same file but not the
        # same hierarchy.  Fragment searches are local to the file, not the node.
        result = env.scene.get_url('/data/DAZ%203D/Genesis%203/Female/Morphs/DAZ%203D/Base%20Pose/eCTRLMouthSmileOpen.dsf#eCTRLMouthSmileOpen')
        assert result
        result = result.get_url('#eCTRLMouthSmileOpen')
        assert result

        # Check the "extra-type" property lookup extension.  Note that "value" comes from the base asset,
        # which is 1, and "current_value" is overridden to a different value in the scene to 2.
        geometry = env.scene.get_url('#Genesis3Female-1')
        assert geometry
        self.assertEqual(geometry.get_property('extra-type/studio_geometry_channels/channels/SubDRenderLevel/value').value, 1)
        self.assertEqual(geometry.get_property('extra-type/studio_geometry_channels/channels/SubDRenderLevel/current_value').value, 2)

        # This won't find the node.  The scheme only searches nodes in the scene, not in the library,
        # and we haven't loaded a character into the scene with this name.
        # XXX: need better exceptions so we can test this
    #    result = env.scene.get_url('Genesis3Female:/data/DAZ%203D/Genesis%203/Female/Morphs/DAZ%203D/Base%20Pose/eCTRLMouthSmileOpen.dsf#eCTRLMouthSmileOpen-1')
    #    assert result is None

if __name__ == '__main__':
#    import cProfile
#    import re
#    cProfile.run('unittest.main()', sort='tottime')
    
    unittest.main()

