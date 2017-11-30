import logging, math, os, urllib
from pprint import pprint, pformat
from maya import mel
from dsonimport import util
from dsonimport import maya_helpers as mh
import pymel.core as pm

log = logging.getLogger('DSONImporter.Materials')

class MaterialBase(object):
    """
    This is a helper base class for classes that create and register textures.
    """
    def __init__(self, env=None, name=None, dson_material=None, source_dson_materials=None, uvsets=None, texture_manager=None, attach_to_viewport=False):
        self.env = env
        self.name = name
        self.uvsets = uvsets
        self.texture_manager = texture_manager

        self.channels, self.images = self._get_combined_material_channels(dson_material, source_dson_materials)
        self.horiz_tiles = 1
        self.vert_tiles = 1
        self.horiz_offset = 0
        self.vert_offset = 0

        # We'll only attach to the viewport shader (surfaceShader) if this is true.
        self.attach_to_viewport = attach_to_viewport

    @classmethod
    def apply_mesh_properties(cls, env, meshes):
        pass

    @classmethod
    def _get_material_channels(cls, dson_material):
        """
        Gather all studio_material_channels properties to make them easier to access.

        Return (channels, images), where channels is a dictionary of keys to values, and
        images is a dictionary of keys to texture paths.
        """
        raw_channels = []
        raw_channels.extend(dson_material.get_value('extra/studio_material_channels/channels', []))
        if dson_material.asset:
            raw_channels.extend(dson_material.asset.get_value('extra/studio_material_channels/channels', []))

        channel_ids = {}
        for channel in raw_channels:
            channel_id = channel['channel']['id']

            # Some of these channels have slashes in their name, so we have to quote it.
            path = 'extra/studio_material_channels/channels/%s' % urllib.quote(channel_id, '')
            channel_ids[channel_id] = path

        # Add the top-level properties.
        for attr in ['diffuse', 'transparency', 'diffuse_strength', 'specular', 'specular_strength', 'glossiness',
                'ambient', 'ambient_strength', 'reflection', 'reflection_strength', 'refraction', 'refraction_strength',
                'ior', 'bump', 'bump_min', 'bump_max', 'displacement', 'displacement_min', 'displacement_max',
                'normal', 'u_offset', 'u_scale', 'v_offset', 'v_scale']:
            channel_ids[attr] = '%s/%s' % (attr, attr)

        channels = {}
        images = {}
        for channel_id, path in channel_ids.items():

            # We could call evaluate() here to run modifiers on these channels, but these channels
            # are inconsistent with all others: they put colors in a single [0,1,2] array instead
            # of putting them in sub-channels.  We don't support that right now and I'm not sure
            # if modifiers are even actually supported on these channels, so for now we just use
            # the static value.
            try:
                value = dson_material[path].get_value_with_default(apply_limits=True)
            except KeyError:
                # Values with only maps may have no value.
                value = None

            channels[channel_id] = value

            # XXX: There's also "image" to reference image_library, including layered maps
            try:
                image_file = dson_material[path].get('image_file')
                if image_file:
                    images[channel_id] = urllib.unquote(image_file.value)
            except KeyError:
                pass

        return channels, images

    @classmethod
    def _get_combined_material_channels(cls, dson_material, source_dson_materials):
        channels, images = cls._get_material_channels(dson_material)

        if source_dson_materials is not None:
            # If there's a source material set, images is eg. { 'diffuse': ['path1', 'path2'] }.  Each element
            # in the array is the file for the corresponding source material.  If a source material doesn't
            # have a file, set that element to None.
            images = {}
            for idx, source_dson_material in enumerate(source_dson_materials):
                _, source_images = cls._get_material_channels(source_dson_material)
                for key, image in source_images.iteritems():
                    list_for_property = images.setdefault(key, [])
                    if len(list_for_property) < idx + 1:
                        list_for_property.extend([None] * (idx + 1 - len(list_for_property)))
                    list_for_property[idx] = image
                    
        # log.debug('Textures per source material:\n%s', pformat(images))

        # Replace any remaining paths with [path].
        for key, path in images.items():
            if isinstance(path, basestring):
                images[key] = [path]

        return channels, images


    def __repr__(self):
        result = self.__class__.__name__
        if self.name is not None:
            result += '(%s)' % self.name
        return result

    def create(self, dson_material):
        raise NotImplemented()

    @classmethod
    def _get_dson_material_type(cls, dson_material):
        for extra in dson_material.iter_get('extra'):
            extra_type = extra.get('type')
            if not extra_type:
                continue

            if not extra_type.value.startswith('studio/material/'):
                continue

            return extra_type.value

        return None

    def make_layer_name(self, layer_name='', prefix='Layer'):
        if layer_name == '':
            return '%s_%s' % (prefix, self.name)
        else:
            return '%s_%s_%s' % (prefix, self.name, layer_name)

    def find_or_create_texture(self, *args, **kwargs):
        """
        Return a Maya texture node for a given path.  Track textures that we've already created,
        and return an existing texture if one exists with the same parameters.

        """
        kwargs = dict(kwargs)
        kwargs.update({
            'horiz_tiles': self.horiz_tiles,
            'vert_tiles': self.vert_tiles,
            'horiz_offset': self.horiz_offset,
            'vert_offset': self.vert_offset,
        })
        
        texture = self.texture_manager.find_or_create_texture(*args, **kwargs)
        self.register_texture(texture)

        return texture

    def set_tiles(self, horiz_tiles, vert_tiles, horiz_offset, vert_offset):
        """
        Set texture tiling.  This will affect all future textures loaded by this material.
        """
        self.horiz_tiles = horiz_tiles
        self.vert_tiles = vert_tiles
        self.horiz_offset = horiz_offset
        self.vert_offset = vert_offset

    def set_attr_to_texture_with_color(self, output_attr, *args, **kwargs):
        texture_node = self.get_texture_with_color(*args, **kwargs)
        mh.set_or_connect(output_attr, texture_node)

    def get_texture_with_color(self, texture, color, mode='rgb', nodeName=None, texture_args={}, srgb_to_linear=True):
        """
        If mode is 'rgb', we're connecting the color component of the texture, and color
        is (r,g,b).  If it's 'alpha', we're connecting the alpha component, and color is
        a single float.

        If srgb_to_linear is true, "color" will be converted from SRGB to linear color space.
        The texture is unaffected.
        """
        texture_args = dict(texture_args)
        if color is None:
            color = 1

        # Alpha colors are already linear.
        if color is not None and srgb_to_linear and mode != 'alpha':
            if isinstance(color, tuple) or isinstance(color, list):
                color = util.srgb_vector_to_linear(color)
            else:
                color = util.srgb_to_linear(color)

        if texture is None:
            # We have just a constant diffuse color, with no texture.
            return color

        if isinstance(texture, pm.PyNode):
            texture_node = texture
        else:
            if mode == 'alpha':
                # If we connect to texture.outAlpha then the color space doesn't matter, but if we use .outTransparency
                # it does, and we need to explicitly set the color space to raw.
                if 'colorSpace' not in texture_args:
                    texture_args['colorSpace'] = 'Raw'

            alphaIsLuminance = (mode == 'alpha')
            texture_node = self.find_or_create_texture(path=texture, alphaIsLuminance=alphaIsLuminance, **texture_args)

            channels = {
                'rgb': 'outColor',
                'r': 'outColorR',
                'alpha': 'outAlpha',
            }
            texture_node = texture_node.attr(channels[mode])

        if color is not None:
            # We have both a texture and a static color.  Create a multiplyDivide node to combine them.
            texture_node = mh.math_op('mult', texture_node, color)

        return texture_node

    def set_attr_to_transparency(self, output_attr, *args, **kwargs):
        texture_node = self.get_texture_with_alpha(*args, **kwargs)
        mh.set_or_connect(output_attr, texture_node)

    def get_texture_with_alpha(self, texture, alpha, mode='rgb', zero_is_invisible=False):
        """
        Set a transparency attribute.  See also set_attr_to_texture_with_color.

        DSON transparency looks like everything else in the universe: an alpha value, with 0 being
        transparent.  Maya is extra special and does it backwards, so we have to handle this
        differently.

        If mode is rgb, the output is a color channel and we'll connect texture.transparency.
        If mode is r, the output is a numeric attribute and we'll connect texture.transparencyR.
        """
        # XXX: zero_is_invisible meant the output's 0 is invisible, eg. this is for opacity and
        # not transparency.  Since the input was also opacity, this meant "don't invert".  This
        # is pretty confusing.
        if alpha is None:
            alpha = 1

        if texture is None:
            if not zero_is_invisible:
                # alpha -> transparency
                alpha = mh.math_op('sub', 1, alpha)
            return alpha

        # Enable alphaIsLuminance (which really means "luminance is alpha"), and use alphaGain
        # to apply the static alpha multiplier.
        texture_node = self.find_or_create_texture(path=texture, alphaIsLuminance=1, alphaGain=alpha, colorGain=(alpha,alpha,alpha), colorSpace='Raw')

        if zero_is_invisible:
            channels = {
                'rgb': 'outColor',
                'r': 'outAlpha',
            }
        else:
            channels = {
                'rgb': 'outTransparency',
                'r': 'outTransparencyR',
            }
        texture_node_color = texture_node.attr(channels[mode])

        return texture_node_color

    def set_attr_to_roughness_from_glossiness(self, texture, value, output_attr):
        value = self.get_roughness_from_glossiness(texture, value)
        mh.set_or_connect(output_attr, value)

    def get_roughness_from_glossiness(self, texture, value):
        """
        Set a roughness attribute from glossiness.
        """
        if texture is None:
            # If we don't have a texture, just set the value directly.
            return mh._convert_glossiness_to_roughness(value)

        # Set alphaIsLuminance, since it's required by remap_glossiness_to_roughness_for_texture.  Note that
        # it'll also convert the constant value, so we don't want to _convert_glossiness_to_roughness
        # the constant value in this case.
        texture_node = self.find_or_create_texture(path=texture, alphaIsLuminance=True, alphaGain=value)

        return mh.remap_glossiness_to_roughness_for_texture(texture_node)

    def register_texture(self, maya_texture_node):
        """
        Register that this material uses maya_texture_node.
        """
        for uvset_node in self.uvsets:
            # Assign UV sets to the texture.
            mh.assign_uvset(uvset_node, maya_texture_node)


