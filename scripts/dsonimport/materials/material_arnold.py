import logging, math, os, urllib
from pprint import pprint, pformat
from time import time
from dsonimport.dson import DSON
from dsonimport import util
from dsonimport import maya_helpers as mh
import pymel.core as pm
from dsonimport import uvset
import schlick
import material_viewport

from material_base import MaterialBase

log = logging.getLogger('DSONImporter.Materials')

class MaterialArnold(MaterialBase):
    @classmethod
    def _get_material_class(cls, dson_material):
        # Daz only has a few base shaders: studio/material/daz_brick, studio/material/uber_iray,
        # and studio/material/daz_shader.  The material type is in an entry in the "extra" array.
        # I haven't found any documentation about how this actually works, but it seems like
        # having an entry in "extra" with a given type adds that type's functionality to the
        # node.  We'll scan through the extra entries for a recognized material type.

        # Read the shader, if any.  We don't support reading these directly, but the string
        # tells us which material properties to look for.
        material_type = cls._get_dson_material_type(dson_material)
        log.debug('    Material type: %s', material_type)
        if material_type == 'studio/material/daz_shader':
            shader = dson_material.get_value('extra-type/studio%2fmaterial%2fdaz_shader/definition')
            log.debug('    Shader: %s', shader)

        if material_type == 'studio/material/uber_iray':
            return MaterialUberIray
        if material_type == 'studio/material/daz_shader':
            return MaterialDazShader
        elif material_type == 'studio/material/daz_brick':
            # We only support a very limited daz_brick configuration (it's actually a generic layered shader
            # with a weird name).
            if 'extra/studio_material_channels/channels/value23' not in dson_material:
                log.warning('Unsupported daz_brick: %s, using generic fallback' % dson_material)
                return material_viewport.MaterialViewport
            
            return MaterialDazBrick

        elif material_type is not None:
            log.warning('Material type for %s not supported: %s' % (dson_material, material_type))
            return material_viewport.MaterialViewport

        # If there's no studio/material, there should be a type, eg. "Plastic" or "Glossy (Plastic)".
        material_type = dson_material.get_value('type')
        if material_type in ('Plastic', 'Glossy (Plastic)', 'Metallic', 'Glossy (Metallic)', 'Skin', 'Matte'):
            return MaterialPlastic
        else:
            log.warning('Basic material type for %s not supported: %s' % (dson_material, material_type))
            return material_viewport.MaterialViewport

    def __new__(cls, dson_material, *args, **kwargs):
        # Create the correct subclass based on the material type.
        material_class = cls._get_material_class(dson_material)
        result = object.__new__(material_class, dson_material=dson_material, *args, **kwargs)
        result.__init__(dson_material=dson_material, *args, **kwargs)
        return result

    def create(self, dson_material, sg_node):
        mh.load_plugin('mtoa.mll')

        # Grr.  Arnold wants us to set a flag on every mesh that uses transparency, instead of just using
        # the info from the material.  We need to do this if opacity or refraction are used.
        self.uses_transparency = False

    def _mark_shapes_not_opaque(self, sg_node):
        """
        Clear aiOpaque on all shapes with this material assigned.
        """
        shapes = set()
        for node in pm.sets(sg_node, q=True):
            shapes.add(node.node())

        for node in shapes:
            node.attr('aiOpaque').set(0)

    @classmethod
    def apply_mesh_properties(cls, env, meshes):
        # Enable render-time subdivision on meshes that have subdivision enabled in the source.
        for mesh, transform in meshes.iteritems():
            dson_geometry_url = mesh.attr('dson_geometry_url').get()
            dson_geometry = env.scene.get_url(dson_geometry_url)

            mesh_type = dson_geometry.get_value('type')
            if mesh_type != 'subdivision_surface':
                continue

            subdiv_level = dson_geometry['extra/studio_geometry_channels/channels/SubDRenderLevel'].get_value_with_default(apply_limits=True)
            if subdiv_level == 0:
                continue

            mesh.attr('aiSubdivType').set(1) # catclark
            mesh.attr('aiSubdivIterations').set(subdiv_level)

    def _post_create(self, sg_node, material):
        material.attr('outColor').connect(sg_node.attr('aiSurfaceShader'), f=True)
        if self.attach_to_viewport:
            material.attr('outColor').connect(sg_node.attr('surfaceShader'), f=True)

        # If this material uses transparency, clear the "opaque" flag on shapes using it.
        if self.uses_transparency:
            self._mark_shapes_not_opaque(sg_node)

    def _set_normal_map(self, material, normal_texture=None, bump_texture=None, bump_scale=1):
        # Normals:
        out_normal_attr = None
        if normal_texture:
            # There's a normal map strength attribute, but that doesn't make sense.
            normal_node = mh.createNewNode('bump2d', nodeName=self.make_layer_name(prefix='bump2d_Normals'))

            # Set the default color of normal maps so if a tiled texture is missing a tile,
            # it'll get reasonable normals.
            self.set_attr_to_texture_with_color(normal_node.attr('bumpValue'), normal_texture, 1, mode='alpha', texture_args={ 'defaultColor': (0.5,0.5,1.0) })
            normal_node.attr('bumpInterp').set(1) # Tangent Space Normals
            normal_node.attr('aiFlipR').set(0)
            normal_node.attr('aiFlipG').set(0)
            out_normal_attr = normal_node.attr('outNormal')

        # Bump:
        if bump_scale > 0 and bump_texture is not None:
            bump_node = mh.createNewNode('bump2d', nodeName=self.make_layer_name(prefix='bump2d_Bump'))

            # bump_scale could be sent as the scalar value here, but instead we'll put it in bumpDepth,
            # which saves a multiplyDivide node and makes the material easier to tweak.
            self.set_attr_to_texture_with_color(bump_node.attr('bumpValue'), bump_texture, 1, mode='alpha')
            bump_node.attr('bumpDepth').set(bump_scale)

            # If we created a bump2d for normals, connect it.
            if out_normal_attr is not None:
                out_normal_attr.connect(bump_node.attr('normalCamera'))

            out_normal_attr = bump_node.attr('outNormal')

        if out_normal_attr is not None:
            out_normal_attr.connect(material.attr('normalCamera'))

    @classmethod
    def grouped_material(cls, dson_material):
        """
        Materials need to be grouped together if they use scatter.  Return true if dson_material needs
	to be grouped together with any other materials on the same object that also return true.
        """
        material_layer = cls(dson_material=dson_material)
        return material_layer._uses_scatter

    @property
    def _uses_scatter(self):
        """
        Return True if this material uses scatter.
        """
        return False

# PBR Metallicity gives this:
# 
# Refraction
# Metallic
#  - Weight: Metallicity
#  - Fresnel: 0.7
# Backscatter
# Dielectric
#  - Weight: 1
#     Dielectric reflection
#     - Weight: Glossy Layered Weight
#     - Fresnel set to Glossy Reflectivity
#     Diffuse
#     - Weight: 1 - (Glossy Layered Weight)
# 
# The top-level mixing is clamped: mix until the weights reach 1, then discard, so the
# order matters.  With 100% refraction, no layers underneath it are visible, and so on.
# Since we only have one glossy layer (reflection doesn't help here) and this shader has
# two in different layering positions, we separate this into two shaders: one for metallicity 1
# and one for metallicity 0 (plastic).  This way, we only have to deal with "refraction > metallic"
# or "refraction > sss > reflection > diffuse", and not both.  The result is merged with
# a layeredShader in the uncommon case of metallicity not being 0 or 1.
#
# (Actually, this has three reflective layers, since refraction has its own reflection.)
# 
# Refraction:
# 
# The refraction layer has a glossy layer built in.  When you have refraction at 100%, you
# don't see the metallic or dielectric glossy layers, but you still get glossiness
# from the refraction layer itself (with roughness applied).
# 
# The "refraction index" on refraction isn't the IOR of refractions!  This material never
# applies an IOR to refractions.  This IOR is actually the weighting for the reflection layer
# inside the refraction layer (a refraction index of 1 gives no reflections).
#
# To simplify, we only support reflections on or off for refraction's reflections.  If the IOR
# is 1 reflections are off, otherwise they're on.
# 
# Metallic:
# 
# This is a glossy layer.  Fresnel is always applied, with a facing reflectivity of 0.7.
# (Glossy Reflectivity doesn't affect this layer.)
# 
# In the MDL file this seems to not use color at all, but it actually seems to use the
# diffuse color.  The glossy color is ignored by this layer.
# 
# SSS:
# 
# This is a simple backscatter layer.  There's a color channel in the MDL but it doesn't
# seem to be exposed by the UI.
#
#
# This layer is important even if we're not implementing SSS.  A lot of skin materials have
# a very high weight to SSS (eg. 75%), and the rest of the color on dielectric (25%).  This
# means that changes to diffuse color have very little effect. The diffuse color may be darkened
# substantially, because the effect of darkening it is reduced to 25%.  If we ignore SSS entirely
# and only render diffuse, we'll render something very dark because the darkened diffuse has
# full effect instead of 25% effect.
#
# XXX: Only support this if metallicity is a fixed 1 or 0, which should be the case for actual
# skin materials.  (Materials that have mapped metallicity are usually things like floor tiles,
# and those probably don't use scatter.)  We don't support this for variable metallicity because
# we only have one glossy layer to work with.
# 
# Dielectric:
# 
# This layer mixes a reflection layer and diffuse, based on Glossy Layered Weight multiplied
# by fresnel, using Glossy Reflectivity as the fresnel facing reflectivity.
# 
# Misc:
# 
# Since all of the glossy layers can have roughness, we can't generally use the aiStandard
# reflection layer to handle part of it (unless roughness is 0).
# 
# The dielectric layer is always a mix between diffuse and reflection.
# 
# The fresnel grazing reflectivity and exponent are always 1 and 5 and aren't exposed to
# be changed, which is good because aiStandard doesn't let us change those for some reason.
#
# We don't implement Top Coat, which is yet another glossy layer.  We don't have anywhere
# to put it.

def _ior_to_schlick(ior):
    """
    Convert IOR to the Fresnel normal reflectivity (Schlick F0).  Some materials give IOR,
    but Arnold uses normal reflectivity.
    """
    return math.pow((1.0-ior) / (1+ior), 2)

class MaterialUberIray(MaterialArnold):
    def create(self, dson_material, sg_node):
        super(MaterialUberIray, self).create(dson_material, sg_node)

        material = self._create_layered(dson_material, sg_node)
        self._post_create(sg_node, material)

    def _create_layered(self, dson_material, sg_node):
        """
        Create and return the material.
        
        If this is a blended metallic/plastic material, create both materials and combine them
        with a layeredShader.
        """
        # These flags apply to all textures on this material.
        self.set_tiles(
                self.channels.get('Horizontal Tiles', 1),
                self.channels.get('Vertical Tiles', 1),
                self.channels.get('Horizontal Offset', 0),
                self.channels.get('Vertical Offset', 0))

        # If we're not in Metallicity mode, just create a regular single-layer shader.
        base_mixing = self.channels['Base Mixing']
        if base_mixing != 0:
            material = self._create_glossy_or_weighted(dson_material, sg_node)
            pm.rename(material, 'Mat_Arnold_%s' % mh.cleanup_node_name(self.name))
            return material

        # We're in Metallicity mode.  If the metallicity value is a constant 0 or 1, then also create a
        # regular single-layer shader.  This is the most common case for this mode.
        metallic_weight = self.get_texture_with_color(self.images.get('Metallic Weight'), self.channels['Metallic Weight'], mode='alpha')
        if metallic_weight in (0, 1):
            material = self._create_metallic(dson_material, sg_node, metallic_weight == 1)
            pm.rename(material, 'Mat_Arnold_%s' % mh.cleanup_node_name(self.name))
            return material

        # We're in metallicity mode, and we have a mix of metallicity and platic.  These two use
        # very different mixing modes (the mixing order is different between the layers).  Do this
        # by creating a material for 0 metallicity and 1 metallicity, then combining them with
        # a layeredShader.  This is slower, but most materials don't use this.
        #
        # Multiply the transparency of this layer by metallic_weight.
        metallic_material = self._create_metallic(dson_material, sg_node, metallic=True, top_weight=metallic_weight)
        pm.rename(metallic_material, 'Mat_Arnold_Metal_%s' % mh.cleanup_node_name(self.name))

        # This layer is underneath metallic, so set its weight to 1.
        plastic_material = self._create_metallic(dson_material, sg_node, metallic=False, top_weight=1)
        pm.rename(plastic_material, 'Mat_Arnold_Plastic_%s' % mh.cleanup_node_name(self.name))

        material = pm.shadingNode('layeredShader', asShader=True)
        pm.rename(material, 'Mat_Arnold_%s' % mh.cleanup_node_name(self.name))

        metallic_material.attr('outColor').connect(material.attr('inputs[0].color'))
        metallic_material.attr('outTransparency').connect(material.attr('inputs[0].transparency'))

        plastic_material.attr('outColor').connect(material.attr('inputs[1].color'))
        plastic_material.attr('outTransparency').connect(material.attr('inputs[1].transparency'))

        return material

    # XXX: This duplicates a bunch of code.  This is to figure out the very different mixing modes without
    # breaking other modes in the process.  Once this has settled down, the duplication here might be reduced.
    def _create_glossy_or_weighted(self, dson_material, sg_node):
        material = pm.shadingNode('aiStandard', asShader=True)

        diffuse_weight = 1

        opacity = self.channels['Cutout Opacity']
        if self.images.get('Cutout Opacity') or self.channels['Cutout Opacity'] != 1:
            self.set_attr_to_transparency(material.attr('opacity'), self.images.get('Cutout Opacity'), opacity, mode='rgb', zero_is_invisible=True)
            self.uses_transparency = True

        # Bump:
        bump_strength = self.channels.get('Bump Strength', 0)
        self._set_normal_map(material, normal_texture=self.images.get('Normal Map'), bump_texture=self.images.get('Bump Strength'), bump_scale=bump_strength*0.025)

        # Glossiness
                       
        # 0: PBR Metallicity/Roughness
        # 1: PBR Specular/Glossiness
        # 2: Weighted
        base_mixing = self.channels['Base Mixing']
        log.debug('%s: %s, %s', sg_node, dson_material, base_mixing)

        assert base_mixing in (1,2)
        if base_mixing == 1:
            # "Specular/Glossiness"
            #
            # Glossy Layered Weight has the same effect here as in the above mode, blending from diffuse to glossy.
            glossy_weight = self.get_texture_with_color(self.images.get('Glossy Layered Weight'), self.channels['Glossy Layered Weight'], mode='alpha')
            mh.set_or_connect(material.attr('Ks'), glossy_weight)
            diffuse_weight = mh.math_op('sub', diffuse_weight, glossy_weight)

            # In this mode, the diffuse color is not mixed into the glossy color: if you have 100% glossy
            # layered weight and a blue diffuse, the blue isn't visible at all.
            self.set_attr_to_texture_with_color(material.attr('KsColor'), self.images.get('Glossy Color'), self.channels['Glossy Color'])
            roughness = self.get_roughness_from_glossiness(self.images.get('Glossiness'), self.channels['Glossiness'])
        else:
            # "Weighted"
            #
            # This mode replaces Glossy Layered Weight with a Glossy Weight and Diffuse Weight.
            #
            # The docs say that it normalizes them, but that's wrong: if you set them both to 0.25 the result
            # is darker than if you set them both to 1.  If they were normalized, 0.25+0.25 would be normalized
            # to 0.5+0.5.  It actually only normalizes if the sum is greater than 1.  Note that most of the
            # time we don't have textures on both of these and all of this math is just done at setup time,
            # so this doesn't always create a complicated node network.
            #
            # This mode doesn't have fresnel reflections.
            unnormalized_glossy = self.get_texture_with_color(self.images.get('Glossy Weight'), self.channels['Glossy Weight'], mode='alpha')
            unnormalized_diffuse = self.get_texture_with_color(self.images.get('Diffuse Weight'), self.channels['Diffuse Weight'], mode='alpha')
            log.debug('... %s, %s', unnormalized_glossy, unnormalized_diffuse)

            total_weight = mh.math_op('add', unnormalized_glossy, unnormalized_diffuse)
            normalized_glossy = mh.math_op('div', unnormalized_glossy, total_weight)
            normalized_diffuse = mh.math_op('div', unnormalized_diffuse, total_weight)

            # If the total is less than one, use the original weight.  Otherwise, use the normalized weight.
            glossy_weight = mh.math_op('lt', total_weight, 1, unnormalized_glossy, normalized_glossy)
            assert diffuse_weight == 1 # should not have been changed yet
            diffuse_weight = mh.math_op('lt', total_weight, 1, unnormalized_diffuse, normalized_diffuse)

            mh.set_or_connect(material.attr('Ks'), glossy_weight)

            self.set_attr_to_texture_with_color(material.attr('KsColor'), self.images.get('Glossy Color'), self.channels['Glossy Color'])
            roughness = self.get_texture_with_color(self.images.get('Glossy Roughness'), self.channels['Glossy Roughness'], mode='alpha')

        # Grr.  Arnold's specular behaves completely differently when it has a value of 0 than 0.001, and always
        # reflects a ton of light even with low weights.  This makes it act like a mirror, and makes texture
        # mapped roughness behave strangely.  Clamp roughness to 0.001 so this doesn't happen.
        roughness = mh.math_op('clamp', roughness, 0.001, 1)
        mh.set_or_connect(material.attr('specularRoughness'), roughness)

        # Convert anisotropy from [0,1] to [0.5,1].  With this material, 0.5 is isotropic and values towards 0 and 1
        # are anisotropic in each axis.
        anisotropy = self.get_texture_with_color(self.images.get('Glossy Anisotropy'), self.channels['Glossy Anisotropy'], mode='r')
        anisotropy = mh.math_op('mult', anisotropy, 0.5)
        anisotropy = mh.math_op('add', anisotropy, 0.5)
        mh.set_or_connect(material.attr('specularAnisotropy'), anisotropy)

        self.set_attr_to_texture_with_color(material.attr('specularRotation'), self.images.get('Glossy Anisotropy Rotations'), self.channels['Glossy Anisotropy Rotations'], mode='alpha')

        # Refraction
        #
        # Refraction in this material is strange.  For example, if we're in weighted mode and the diffuse weight
        # is 1, refraction still makes the material transparent, but the refraction color isn't applied at all.
        refraction_weight = self.get_texture_with_color(self.images.get('Refraction Weight'), self.channels['Refraction Weight'], mode='alpha')
        diffuse_weight = mh.math_op('sub', diffuse_weight, refraction_weight)
        if isinstance(refraction_weight, pm.PyNode) or refraction_weight > 0:
            # Only set these if we have any refraction, so we don't create connections to glossiness if we're not using it.
            mh.set_or_connect(material.attr('Kt'), refraction_weight)
            self.uses_transparency = True

            # If the Share Glossy Inputs setting is true, use the reflection settings for roughness/glossiness and color.
            # Note that we don't connect the roughness value, since the metallicity adjustments we make for
            # specular roughness shouldn't be made to refraction roughness (a non-metallic surface should have
            # rough reflections, but not rough refraction).
            share_glossy_inputs = self.channels['Share Glossy Inputs']
            diffuse_channel_name = 'Glossy Color' if share_glossy_inputs else 'Refraction Color'
            self.set_attr_to_texture_with_color(material.attr('KtColor'), self.images.get(diffuse_channel_name), self.channels[diffuse_channel_name])

            # Use the base mixing mode to determine whether it uses Refraction Roughness or Refraction Glossiness.
            if base_mixing == 1:
                # "Specular/Glossiness"
                self.set_attr_to_roughness_from_glossiness(
                        self.images.get('Glossiness' if share_glossy_inputs else 'Refraction Glossiness'),
                        self.channels['Glossiness' if share_glossy_inputs else 'Refraction Glossiness'],
                        material.attr('refractionRoughness'))
            else:
                # "Weighted"
                self.set_attr_to_texture_with_color(
                        material.attr('refractionRoughness'),
                        self.images.get('Glossy Roughness' if share_glossy_inputs else 'Refraction Roughness'),
                        self.channels['Glossy Roughness' if share_glossy_inputs else'Refraction Roughness'],
                        mode='alpha')

        # XXX
        material.attr('IOR').set(self.channels['Refraction Index'])
        material.attr('dispersionAbbe').set(self.channels['Abbe'])

        # XXX: Backscatter

        # XXX: Thin film using the reflection layer?

        # Diffuse
        self.set_attr_to_texture_with_color(material.attr('color'), self.images.get('diffuse'), self.channels['diffuse'])
        self.set_attr_to_texture_with_color(material.attr('diffuseRoughness'), self.images.get('Diffuse Roughness'), self.channels['Diffuse Roughness'], mode='alpha')

        # Diffuse, glossy and transparency are additive.  Set the diffuse weight to the remainder after
        # subtracting the other parts.  A completely transparent or reflective object shouldn't have any
        # diffuse.  If refraction + glossy > 1, set diffuse to 0.
        diffuse_weight = mh.math_op('clamp', diffuse_weight, 0, 1)
        mh.set_or_connect(material.attr('Kd'), diffuse_weight)
        return material

    def _create_metallic(self, dson_material, sg_node, metallic=False, top_weight=1):
        """
        Create a material for "PBM Metallicity",.

        If metallic is true, create a material for metallicity 1.  Otherwise, create metallicity 0.

        top_weight is the weight for this layer.  If we're not being added to a layeredShader this
        will be 1.  This is multiplied into the cutout opacity.
        """
        assert self.channels['Base Mixing'] == 0

        material = pm.shadingNode('aiStandard', asShader=True)

        cutout_opacity = self.get_texture_with_alpha(self.images.get('Cutout Opacity'), self.channels['Cutout Opacity'], mode='rgb', zero_is_invisible=True)
        if cutout_opacity != 1:
            # Tricky: We need to set uses_transparency if the final material will be transparent.  In this
            # case, that's only if the actual cutout opacity value isn't 1, not the value combined with
            # top_weight.  If top_weight is 0.25, then the other metallicity layer will have a top_weight
            # of 0.75 and the final material won't be transparent due to that.
            self.uses_transparency = True

        # Include the layer's weight in opacity.
        cutout_opacity = mh.math_op('mult', cutout_opacity, top_weight)
        mh.set_or_connect(material.attr('opacity'), cutout_opacity)

        # Turn off diffuse by default.  We'll turn it on later if we want it.
        mh.set_or_connect(material.attr('Kd'), 0)

        # Bump:
        bump_strength = self.channels.get('Bump Strength', 0)
        self._set_normal_map(material, normal_texture=self.images.get('Normal Map'), bump_texture=self.images.get('Bump Strength'), bump_scale=bump_strength*0.025)

        # Diffuse.  Set this even though in some cases we won't use it, so texture connections are available
        # during material tweaking.
        self.set_attr_to_texture_with_color(material.attr('color'), self.images.get('diffuse'), self.channels['diffuse'])
        self.set_attr_to_texture_with_color(material.attr('diffuseRoughness'), self.images.get('Diffuse Roughness'), self.channels['Diffuse Roughness'], mode='alpha')

        # Shared glossy settings
        #
        # Grr.  Arnold's specular behaves completely differently when it has a value of 0 than 0.001, and always
        # reflects a ton of light even with low weights.  This makes it act like a mirror, and makes texture
        # mapped roughness behave strangely.  Clamp roughness to 0.001 so this doesn't happen.
        roughness = self.get_texture_with_color(self.images.get('Glossy Roughness'), self.channels['Glossy Roughness'], mode='alpha')
        roughness = mh.math_op('clamp', roughness, 0.001, 1)
        mh.set_or_connect(material.attr('specularRoughness'), roughness)

        # Convert anisotropy from [0,1] to [0.5,1].  With this material, 0.5 is isotropic and values towards 0 and 1
        # are anisotropic in each axis.
        anisotropy = self.get_texture_with_color(self.images.get('Glossy Anisotropy'), self.channels['Glossy Anisotropy'], mode='r')
        anisotropy = mh.math_op('mult', anisotropy, 0.5)
        anisotropy = mh.math_op('add', anisotropy, 0.5)
        mh.set_or_connect(material.attr('specularAnisotropy'), anisotropy)

        self.set_attr_to_texture_with_color(material.attr('specularRotation'), self.images.get('Glossy Anisotropy Rotations'), self.channels['Glossy Anisotropy Rotations'], mode='alpha')
        material.attr('enableInternalReflections').set(0)

        # Always enable fresnel.  If we don't want fresnel, we'll just set Ksn to 1.  This has the same
        # effect on weighting, but avoids the weird side-effect of making the diffuse channel blending mode
        # change as if FresnelAffectDiff is false.
        material.attr('specularFresnel').set(1)
        material.attr('Ksn').set(1)

        # The top-level mixing is clamped: each layer is added in order with its weight, and once
        # we reach 100% no further layers are added.
        remaining_weight = 1

        # Refraction
        #
        # This is on top regardless of whether we're metallic or not.
        refraction_weight = self.get_texture_with_color(self.images.get('Refraction Weight'), self.channels['Refraction Weight'], mode='alpha')
        mh.set_or_connect(material.attr('Kt'), refraction_weight)

        material.attr('dispersionAbbe').set(self.channels['Abbe'])

        # Refraction Index in refraction isn't actually the IOR of refraction.  It's really the IOR for reflections
        # on top of refractions.  There seems to be no IOR built into refractions for this material.
        # material.attr('IOR').set(self.channels['Refraction Index'])

        if isinstance(refraction_weight, pm.PyNode) or refraction_weight > 0:
            self.uses_transparency = True

            # Only set these if we have any refraction, so we don't create connections to glossiness if we're not using it.
            #
            # If the Share Glossy Inputs setting is true, use the reflection settings for roughness/glossiness and color.
            # Note that we don't connect the roughness value, since the metallicity adjustments we make for
            # specular roughness shouldn't be made to refraction roughness (a non-metallic surface should have
            # rough reflections, but not rough refraction).
            share_glossy_inputs = self.channels['Share Glossy Inputs']
            diffuse_channel_name = 'Glossy Color' if share_glossy_inputs else 'Refraction Color'
            self.set_attr_to_texture_with_color(material.attr('KtColor'), self.images.get(diffuse_channel_name), self.channels[diffuse_channel_name])

            self.set_attr_to_texture_with_color(
                    material.attr('refractionRoughness'),
                    self.images.get('Glossy Roughness' if share_glossy_inputs else 'Refraction Roughness'),
                    self.channels['Glossy Roughness' if share_glossy_inputs else'Refraction Roughness'],
                    mode='alpha')

        # Subtract the weight used by refraction.  The remainder is the amount available for the remaining layers.
        remaining_weight = mh.math_op('sub', remaining_weight, refraction_weight)

        # Refraction reflections
        #
        # The top refraction layer has its own built-in reflections.  If you have Glossy Layered Weight on with
        # 100% refraction, this is the reflection you're seeing, not anything in the metallic or plastic layer.
        #
        # Unlike the other reflection layers, this one isn't multiplied by diffuse color.  This makes this tricky,
        # since we only have one main glossy layer to work with.  Currently we only implement this if refraction
        # is 100%, which means none of the other layers are visible.  If refraction is less than 100% or textured,
        # we won't set up this glossy layer (but you'll get the glossiness layers beneath it instead).
        glossy_layered_weight = self.get_texture_with_color(self.images.get('Glossy Layered Weight'), self.channels['Glossy Layered Weight'], mode='alpha')
        reflection_ior = self.channels['Refraction Index']
        if remaining_weight == 0 and glossy_layered_weight != 0 and reflection_ior != 1:
            facing_reflectance = _ior_to_schlick(reflection_ior)
            log.debug('%s using refraction reflections, %s %s', dson_material, reflection_ior, facing_reflectance)

            mh.set_or_connect(material.attr('Ksn'), facing_reflectance)
#            fresnel_ramp = schlick.create_ramp_for_schlick(facing_reflectance, max_points=16)
#            glossy_layered_weight = mh.math_op('mult', glossy_layered_weight, fresnel_ramp)
            mh.set_or_connect(material.attr('Ks'), glossy_layered_weight)

            return material

        # Metallic glossiness
        if metallic:
            # Disable diffuse for the metallic layer.  We're using up the rest of the layering weight, so there's
            # no diffuse underneath it.
            mh.set_or_connect(material.attr('Kd'), 0)

            # This is the metallic layer.  This layer is completely specular, minus any weight used up by refraction.
            # (The glossiness weight for the metallicity layer is the metallicity, and we're implementing metallicity 1.)
            mh.set_or_connect(material.attr('Ks'), remaining_weight)

            # When metallic, the fresnel reflectance is always 0.7.
            mh.set_or_connect(material.attr('Ksn'), 0.7)

            # top_coat_directional_normal_color for metallic seems to be the base color, and top_coat_directional_grazing_color
            # is white.  The IOR is 0.7 which gives a lot of reflectance at the facing angle anyway, so we just mix the base
            # color into the glossy color.
            glossy_color = self.get_texture_with_color(self.images.get('Glossy Color'), self.channels['Glossy Color'])
            diffuse_color = self.get_texture_with_color(self.images.get('diffuse'), self.channels['diffuse'])
            glossy_color_combined = mh.math_op('mult', diffuse_color, glossy_color)
            mh.set_or_connect(material.attr('KsColor'), glossy_color_combined)

            return material

        # This is the plastic layer.  None of the rest applies to the metallic layer.
        #
        # Backscatter (not sub-surface scatter)
        #
        # When this is enabled, an object can be lit from behind.  The usual example is paper.
        # In the original material, diffuse weight had backscatter weight subtracted.  The
        # backscatter layer probably includes diffuse, so this prevents it from being doubled.
        # Here, backscatter isn't a separate layer but just a weight on diffuse, so we just apply
        # the backscatter weight and don't subtract it from diffuse.
        #
        # Backscatter has color, roughness and anisotropy, but we don't have separate control over
        # that.  All we can do is set a weight.  We don't need to set uses_transparency here.
        self.set_attr_to_texture_with_color(material.attr('Kb'), self.images.get('Backscattering Weight'), self.channels['Backscattering Weight'], mode='alpha')

        # The remaining weight not taken up by refraction and scatter is shared by the glossy and diffuse
        # layer.  These two layers are mixed with reflection on top, using fresnel for weighting (Glossy Reflectivity)
        # multiplied by Glossy Layered Weight.  That is, if refraction is 10% and scatter is 15%, we have 75%
        # remaining.  That layer is weighted to glossiness with fresnel multiplied by Glossy Layered Weight.
        #
        # Glossy Reflectivity doesn't map 1:1 to Schlick reflectivity.  It's not clear what the
        # translation is.  Empirically, the MDL receives 0.28 for 1.0, 0.24 for 0.75, 0.20 for 0.5,
        # 0.14 for 0.25, and 0 for 0.  Just approximate it by scaling.
        glossy_reflectivity = self.get_texture_with_color(self.images.get('Glossy Reflectivity'), self.channels['Glossy Reflectivity'], mode='alpha')
        glossy_reflectivity = mh.math_op('mult', glossy_reflectivity, 0.25)
        mh.set_or_connect(material.attr('Ksn'), glossy_reflectivity)

        glossy_weight = mh.math_op('mult', glossy_layered_weight, remaining_weight)
        mh.set_or_connect(material.attr('Ks'), glossy_weight)

        # remaining_weight is the weight remaining for diffuse.  aiStandard will subtract the weight of
        # the specular (and reflective) layer, since FresnelAffectDiff is true.
        mh.set_or_connect(material.attr('Kd'), remaining_weight)

        # The dialectric layer sets its base to diffuse, which seems to effectively mix in the
        # diffuse color with the glossy color.
        glossy_color = self.get_texture_with_color(self.images.get('Glossy Color'), self.channels['Glossy Color'])
        diffuse_color = self.get_texture_with_color(self.images.get('diffuse'), self.channels['diffuse'])
        glossy_color_combined = mh.math_op('mult', diffuse_color, glossy_color)
        mh.set_or_connect(material.attr('KsColor'), glossy_color_combined)

        # The scatter component of diffuse isn't implemented.  The SSS model is complex: a translucency weight, reflectance
        # tint, translucency color, transmission color and lots of weights, deriving an absorbance coefficient and a scattering
        # coefficient.  It's hard to estimate what the results are in order to even emulate the overall basic color.  If
        # your material uses scatter, you'll need to set this up manually.
        #
        # If we have a SSS texture, hook that up to make it easier to turn this on manually.
        self.set_attr_to_texture_with_color(material.attr('KsssColor'), self.images.get('Translucency Color'), self.channels['Translucency Color'])

        # XXX: Thin film using the reflection layer?

        return material

    @property
    def _uses_scatter(self):
	# We don't enable scatter by default, but group the material if it has an SSS texture, so the
	# texture loads correctly if it's turned on.
	return self.images.get('Translucency Color') is not None

class MaterialDazBrick(MaterialArnold):
    def _scatter_weight(self):
        assert 'value23' in self.channels, '%s: unsupported DazBrick' % self
        subSurfaceOffOn = self.channels['value23']
        subSurfaceStrength = self.channels['Ambient Strength']

        subSurfaceStrength *= subSurfaceOffOn
        return 0.5 if subSurfaceStrength > 0 else 0

    @property
    def _uses_scatter(self):
        return self._scatter_weight() > 0

    def create(self, dson_material, sg_node):
        super(MaterialDazBrick, self).create(dson_material, sg_node)

        material = pm.shadingNode('aiStandard', asShader=True)
        pm.rename(material, 'Mat_Arnold_%s' % mh.cleanup_node_name(self.name))

        # Don't apply transparency if there's no texture and transparency is 1 (transparency is really alpha).
        if self.channels['transparency'] < 1 or self.images.get('transparency'):
            self.set_attr_to_transparency(material.attr('opacity'), self.images.get('transparency'), self.channels['transparency'], zero_is_invisible=True)
            self.uses_transparency = True

        # Normals:
        #
        # This material has a positive and negative bump value.  That's odd and I'm not sure if anyone actually uses this.
        bump_negative = self.channels.get('Negative Bump', 0)
        bump_positive = self.channels.get('Positive Bump', 0)
        bump_scale = abs(bump_positive - bump_negative)
        bump_scale = mh.math_op('mult', bump_scale, self.channels['Bump Strength'])
        self._set_normal_map(material, normal_texture=self.images.get('Normal Map'), bump_texture=self.images.get('Bump Strength'), bump_scale=bump_scale*0.5)

#        # reflectionStrength = self.channels['Reflection Strength']
#        # if reflectionStrength > 0:
#        #     pass

        # Specular 1.
        specularStrength = self.channels.get('Specular Strength', 0)
        if specularStrength > 0:
            # XXX: calibrate
            material.attr('Ks').set(specularStrength * 0.075)

            # We currently ignore any glossiness map.  You can set one, but Iray seems to ignore it.
            # self.set_attr_to_roughness_from_glossiness(self.images.get('Glossiness'), self.channels['Glossiness'], material.attr('specularRoughness'))
            self.set_attr_to_roughness_from_glossiness(None, self.channels['Glossiness'], material.attr('specularRoughness'))

            self.set_attr_to_texture_with_color(material.attr('KsColor'), self.images.get('Specular Color'), self.channels['Specular Color'])
#
#        # Specular 2.  This is a phong specular.  This isn't very well tested.
#        specular2Strength = self.channels['value222']
#        if specular2Strength > 0:
#	    pass

        # This material allows Diffuse Strength to be greater than 1, but aiStandard doesn't,
        # so apply diffuse strength as part of the diffuse color instead.
        #diffuse_color = util.srgb_vector_to_linear(self.channels['diffuse'])
        #diffuse_color = mh.math_op('mult', diffuse_color, self.channels['Diffuse Strength'])
        #diffuse_texture_node = self.find_or_create_texture(path=self.images.get('diffuse'))
        #if diffuse_texture_node is not None:
        #    diffuse_color = mh.math_op('mult', diffuse_color, diffuse_texture_node.attr('outColor'))
        
        #mh.set_or_connect(material.attr('color'), diffuse_color)
        #mh.set_or_connect(material.attr('Kd'), 1)

        diffuse_color = self.get_texture_with_color(self.images.get('diffuse'), self.channels['diffuse'])
        mh.set_or_connect(material.attr('color'), diffuse_color)
        
        # Note that the diffuse strength isn't quite the same as a multiplier to the diffuse color,
        # since it's a layering weight and affected by other material layers.
        diffuse_strength = self.get_texture_with_color(self.images.get('Diffuse Strength'), self.channels['Diffuse Strength'], mode='alpha')
        if not isinstance(diffuse_strength, pm.PyNode):
            diffuse_strength = min(diffuse_strength, 1)
        mh.set_or_connect(material.attr('Kd'), diffuse_strength)

        # Subsurface
        #
        # "value23" is "Subsurface Off - On".
        # "Ambient Strength" is "Subsurface Strength" (huh?).
        # "Ambient Color" is "Subsurface Color"
        # Multiply these together to get the SSS weight, and to see if we need to set up SSS at
        # all.  "Subsurface Off - On" is probably 0 or 1 most of the time.
        #
        # This material's scatter is very different from ours, so we don't really try to emulate
        # it.  We just set up basic texture maps if available, so it's easier to turn it on manually.
        scatter_weight = self._scatter_weight()

        # If we have an ambient color (which is actually Subsurface Color), connect it to SSS color.
        # Otherwise, connect the diffuse color, since it's the usually what you want if you're turning
        # on simple SSS.
        self.set_attr_to_texture_with_color(material.attr('KsssColor'), self.images.get('Ambient Color'), [1,1,1])

        # Set a reasonable default for skin SSS.  This won't be used unless SSS is actually weighted on.
        material.attr('sssRadius').set((1, 0.5, 0.25))

        # XXX: Not implemented: ambient, displacement, opacity, reflection, shadows, tiling, velvet

        self._post_create(sg_node, material)

class MaterialDazShader(MaterialArnold):
    def create(self, dson_material, sg_node):
        super(MaterialDazShader, self).create(dson_material, sg_node)

        material = pm.shadingNode('aiStandard', asShader=True)
        pm.rename(material, 'Mat_Arnold_%s' % mh.cleanup_node_name(self.name))

        diffuse_weight = 1

        # Bump:
        if self.channels['Bump Active']:
            # XXX: Normal maps?
            self._set_normal_map(material, bump_texture=self.images.get('Bump Strength'), bump_scale=self.channels.get('Bump Strength', 0) * 0.025)

        if self.channels['Opacity Active'] and (self.channels['transparency'] < 1 or self.images.get('transparency')):
            self.set_attr_to_transparency(material.attr('opacity'), self.images.get('transparency'), self.channels['transparency'], zero_is_invisible=True)
            self.uses_transparency = True

        # Specular (Primary)
        if self.channels['Specular Active']:
            self.set_attr_to_texture_with_color(material.attr('KsColor'), self.images.get('Specular Color'), self.channels['Specular Color'])
            self.set_attr_to_roughness_from_glossiness(self.images.get('Glossiness'), self.channels['Glossiness'], material.attr('specularRoughness'))

            specular_strength = self.channels['Specular Strength']
#            diffuse_weight = mh.math_op('sub', diffuse_weight, specular_strength)
            mh.set_or_connect(material.attr('Ks'), specular_strength)

        # Diffuse
        if self.channels['Diffuse Active'] and self.channels['Diffuse Strength'] > 0:
            self.set_attr_to_texture_with_color(material.attr('color'), self.images.get('diffuse'), self.channels['diffuse'])

            diffuse_weight = mh.math_op('mult', diffuse_weight, self.channels['Diffuse Strength'])
            mh.set_or_connect(material.attr('Kd'), diffuse_weight)

        self.set_attr_to_texture_with_color(material.attr('diffuseRoughness'), self.images.get('Diffuse Roughness'), self.channels['Diffuse Roughness'], mode='alpha')

        self._post_create(sg_node, material)

class MaterialPlastic(MaterialArnold):
    def create(self, dson_material, sg_node):
        super(MaterialPlastic, self).create(dson_material, sg_node)

        material = pm.shadingNode('aiStandard', asShader=True)
        pm.rename(material, 'Mat_Arnold_%s' % mh.cleanup_node_name(self.name))

        # Don't apply transparency if there's no texture and transparency is 1 (transparency is really alpha).
        if self.channels['transparency'] < 1 or self.images.get('transparency'):
            self.set_attr_to_transparency(material.attr('opacity'), self.images.get('transparency'), self.channels['transparency'], zero_is_invisible=True)
            self.uses_transparency = True

        # Bump
        # Daz has a positive and negative bump value.  That's odd and I'm not sure if anyone actually uses this.
        # This isn't used for now.
        bump_negative = self.channels['bump_min']
        bump_positive = self.channels['bump_max']
        bump_scale = abs(bump_positive - bump_negative)
        if bump_scale > 0:
            self._set_normal_map(material, bump_texture=self.images.get('bump'), bump_scale=bump_scale * 0.5)

#        # reflectionStrength = self.channels['Reflection Strength']
#        # if reflectionStrength > 0:
#        #     pass
#
        # Specular (XXX untested)
        specular_strength = self.channels.get('specular_strength', 0)
        if specular_strength > 0:
            mh.set_or_connect(material.attr('Ks'), specular_strength)

            # We currently ignore any glossiness map.  You can set one, but Iray seems to ignore it.
            # self.set_attr_to_roughness_from_glossiness(self.images.get('glossiness'), self.channels['glossiness'], spec_layer.node.attr('roughness'))
            self.set_attr_to_roughness_from_glossiness(None, self.channels['glossiness'], material.attr('specularRoughness'))

            self.set_attr_to_texture_with_color(material.attr('KsColor'), self.images.get('specular'), self.channels['specular'])

        # Diffuse color.  We don't currently implement textures for diffuse strength.
        self.set_attr_to_texture_with_color(material.attr('color'), self.images.get('diffuse'), self.channels['diffuse'])
        self.set_attr_to_texture_with_color(material.attr('Kd'), self.images.get('diffuse_strength'), self.channels['diffuse_strength'], mode='r')

        # Unimplemented: ambient/ambient_strength, reflection/reflection_strength, refraction/refraction_strength/ior,
        # displacement/displacement_min/displacement_max, normal, u_offset/u_scale/v_offset/v_scale

        self._post_create(sg_node, material)

