import logging, math, os, urllib
from pprint import pprint, pformat
from dsonimport import maya_helpers as mh
import pymel.core as pm
from material_base import MaterialBase

log = logging.getLogger('DSONImporter.Materials')

class MaterialViewport(MaterialBase):
    def create(self, dson_material, sg_node):
        self.material = pm.shadingNode('lambert', asShader=True)
        pm.rename(self.material, 'Mat_Viewport_%s' % mh.cleanup_node_name(self.name))
        self.material.attr('diffuse').set(1)

        material_type = self._get_dson_material_type(dson_material)

        self.set_attr_to_texture_with_color(self.material.attr('color'), self.images.get('diffuse'), self.channels['diffuse'], nodeName=self.name)

        if material_type == 'studio/material/uber_iray':
            # Refraction isn't really opacity, but we'll approximate it that way in the viewport.
            refraction_opacity = self.get_texture_with_alpha(self.images.get('Refraction Weight'), self.channels['Refraction Weight'], zero_is_invisible=True)

            transparency = self.get_texture_with_alpha(self.images.get('Cutout Opacity'), self.channels['Cutout Opacity'])

            # "Transparency" is a terrible way to represent opacity, because instead of just multiplying
            # values to combine them, you have to do 1-((1-t1)*(1-t2)).  That gives an ugly shader.
            # Cutout opacity and refraction are used for very different types of materials and I've never
            # seen them used together, so we cheat here and just add them.
            transparency = mh.math_op('add', transparency, refraction_opacity)
        else:
            transparency = self.get_texture_with_alpha(self.images.get('transparency'), self.channels['transparency'])

        # If transparency is constant, don't let it be 0.  Clamp it to 0.5, so it's not completely
        # invisible in the viewport.  In the real materials there are usually other things causing
        # it to be visible, like reflections or refraction.  Hack: don't do this for certain eye
        # materials, or it'll wash out eyes.
        allow_completely_transparent_shaders = [
            'EyeMoisture',
            'Cornea',
        ]
        allow_completely_transparent = any(s in str(dson_material) for s in allow_completely_transparent_shaders)
        if not isinstance(transparency, pm.PyNode) and not allow_completely_transparent:
            transparency = min(transparency, 0.5)

        mh.set_or_connect(self.material.attr('transparency'), transparency)

        # Connect the material.  Force this connection, so if it's already connected to lambert1
        # we'll override it.
        self.material.attr('outColor').connect(sg_node.attr('surfaceShader'), f=True)

