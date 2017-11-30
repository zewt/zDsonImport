import logging, os, shutil, subprocess, sys
from pprint import pprint, pformat
from maya import mel
from dsonimport import util
from dsonimport import maya_helpers as mh
import pymel.core as pm

log = logging.getLogger('DSONImporter.Materials')

def _remove_ext(path):
    parts = path.split('.')[:-1]
    if len(parts) == 1:
        return path
    return '.'.join(parts[:1])

def _get_ext(path):
    ext = path.split('.')[-1].lower()
    return ext

def _has_src_file_changed(src, dst):
    """
    Return true if src is newer than dst, or if dst doesn't exist.
    """
    if not os.path.exists(src) or not os.path.exists(dst):
        return True

    return os.stat(src).st_mtime > os.stat(dst).st_mtime

def _convert_image(src, dst):
    # Create a temporary filename in the same directory.
    path = os.path.dirname(dst)
    filename = os.path.basename(dst)
    temp_path = '%s/temp_%s' % (path, filename)

    # Make sure the temp file doesn't exist, so we can check that it was created below.
    if os.path.exists(temp_path):
        os.unlink(temp_path)

    # Use imconvert to convert files.  It would be cleaner to do this with something like PIL,
    # but that's not installed with Maya's Python.
    binary_path = os.environ['MAYA_LOCATION']
    exe = '%s/bin/imconvert' % binary_path

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags = subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
    
    p = subprocess.Popen([
        exe,
        '-compress', 'lzw',
        src,
        temp_path],
        startupinfo=startupinfo, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    out, err = p.communicate()
    
    # This stupid tool doesn't report errors correctly, so we have to see whether the output file
    # exists to try to guess if it actually worked.
    if not os.path.exists(temp_path):
        log.error('Error converting %s -> %s', src, dst)
        log.error('%s%s', out, err)
        raise RuntimeError('Error converting %s -> %s', src, dst)

    try:
        # Rename the file to its final filename, overwriting any file that was there before.
        os.rename(temp_path, dst)
    except:
        os.unlink(temp_path)
        raise

def _copy_images_to_path(path, output_path):
    """
    Given a list of paths, copy them to output_path, converting them to the same file
    type and giving them a Mudbox tile filename pattern.
    """
    assert len(path) > 0

    # Pick an arbitrary filename to use as the base for the path.  We could base this on the
    # material slot, if we wanted to propagate that down from the caller.
    filename = [_remove_ext(os.path.basename(absolute_path)) for absolute_path in path if absolute_path is not None]
    assert len(filename) > 0
    filename = filename[0]

    # Remove single quotes from filenames.  It triggers a bug in Arnold TX conversion.
    filename = filename.replace('\'', '')

    # Check file extensions.  Mudbox patterns can't handle files having different extensions,
    # so if the input files use more than one file type we need to convert some of them.
    extensions = { _get_ext(absolute_path) for absolute_path in path if absolute_path is not None }
    assert len(extensions) > 0
    if len(extensions) == 1 and list(extensions)[0].lower() in ('exr', 'hdr'):
        # Leave 32-bit file types alone.
        ext = list(extensions)[0]
    else:
        # Convert files to TIFF.  Just always convert and don't try to be clever, since there
        # are too many edge cases.  For example, we might be called twice, first with
        # ['a.jpg', None, 'c.jpg'] and then with ['a.jpg', 'b.tif', 'c.jpg'], and if we
        # copy the files as .jpg the first time around we'll end up with two copies of
        # the file.
        ext = 'tif'

#    if len(extensions) == 1:
#        # Only one file type is used, so we can just copy the files.
#        ext = list(extensions)[0]
#    else:
#        # If we have more than one file type, convert all files to TIFF.  We could try to be clever
#        # and convert to the most-used file extension, but different file types support different
#        # feature and this could do unwanted things.  We could check for EXR/HDR to see if we need
#        # to convert to a different format for 32-bit textures.  We don't bad extensions, like *.TIF
#        # files named "*.TIFF" (fix them in the source if you want to prevent a conversion).
#        log.info('Material\'s textures use more than one file type (%s).  Converting to TIF.', ', '.join(extensions))
#        ext = 'tif'

    for idx, src_path in enumerate(path):
        if src_path is None:
            continue

        dst_path = '%s/%s.%i.1.%s' % (output_path, filename, idx+1, ext)

        if not _has_src_file_changed(src_path, dst_path):
            # The target file already exists and the source hasn't changed, so save time and skip
            # the copy/conversion.
            continue

        file_ext = _get_ext(src_path)
        if file_ext == ext:
            # The file is already in this format, so just copy it.
            shutil.copyfile(src_path, dst_path)
        else:
            _convert_image(src_path, dst_path)

    return '%s/%s.<U>.<V>.%s' % (output_path, filename, ext)

class TextureManager(object):
    """
    This class handles creating file nodes for textures.

    - Many textures will be used by more than one texture.  We'll keep track of the nodes we've
    created and reuse them.  We don't currently serialize this, so if multiple renderer materials
    are created (eg. viewport and mental ray), we'll still create duplicate nodes.
    - Some renderers don't support explicit tiles.  In this case, we need to copy off all textures
    that would use it so they use Mudbox tiling.  This can lead to duplicated texture files, if the
    same texture is used in multiple UV positions.
    """
    def __init__(self, find_file):
        """
        find_file is a function that takes a path as a parameter, and returns the absolute
        path to the image.
        """
        self.find_file = find_file
        self.path = None
        self.created_textures = {}

    def set_path(self, path):
        """
        Set the directory to store generated/converted textures in.
        """
        self.path = path

    def _create_texture(self, path):
        # Get the first non-None path to use as the node name.
        first_path = [p for p in path if p is not None][0]

        path = [self.find_file(p)[1] if p is not None else None for p in path]
        assert len(path) > 0

        texture, place = mh.create_file_2d()

        path_node_name = 'tex_' + mh.make_node_name_from_filename(first_path)
        pm.rename(texture, path_node_name)

        util.mkdir_p(self.path)

#        self.mudbox_tiles = False
        self.mudbox_tiles = True
        if len(path) > 1:
            # There are multiple tiles for this texture.
            if self.mudbox_tiles:
                texture.attr('uvTilingMode').set(2) # Mudbox (1-based)
                pattern_path = _copy_images_to_path(path, self.path)
                pattern_path = pattern_path.replace('\\', '/')

                # This doesn't make much sense, but this is what it expects in Mudbox mode.
                texture.attr('fileTextureName').set(pattern_path.replace('<U>', '1').replace('<V>', '1'))
                texture.attr('fileTextureNamePattern').set(pattern_path)
            else:
                texture.attr('uvTilingMode').set(4) # explicit tiles
                        
                # Entries that are None don't exist for that material.  We'll skip over that entry in
                # explicitUvTilePosition, so the UVs stay lined up, but not in the explicitUvTiles list,
                # so we don't leave blank file entries that the file node may not understand.
                for idx, absolute_path in enumerate(path):
                    if absolute_path is None:
                        continue
                    absolute_path = absolute_path.replace('\\', '/')
                    if idx == 0:
                        texture.attr('fileTextureName').set(absolute_path)
                    else:
                        tile = texture.attr('explicitUvTiles').elementByLogicalIndex(idx-1)
                        tile.attr('explicitUvTileName').set(absolute_path)
                        tile.attr('explicitUvTilePosition').set((idx, 0))

            mel.eval('generateUvTilePreview %s' % texture.name())
        else:
            # Hack: Arnold in Maya 2017 breaks if texture filenames contain '.  In that case, always
            # copy the file as if this is a tiled texture so we can rename it.
            filename = path[0]
            if '\'' in filename:
                dst_path = '%s/%s' % (self.path, os.path.basename(filename.replace('\'', '')))
                if _has_src_file_changed(filename, dst_path):
                    log.debug('Copying %s to %s to work around Arnold bug...', filename, dst_path)
                    shutil.copyfile(filename, dst_path)
                    
                filename = dst_path
            
            texture.attr('fileTextureName').set(filename)

        return texture, place

    def find_or_create_texture(self, path, alphaIsLuminance=False, alphaGain=1.0,
            colorGain=(1,1,1), defaultColor=(0,0,0), colorSpace='sRGB',
            horiz_tiles=1, vert_tiles=1, horiz_offset=0, vert_offset=0):
        if isinstance(path, list):
            path = tuple(path)
        assert isinstance(path, tuple), path

        key = {
            'alphaIsLuminance': alphaIsLuminance,
            'alphaGain': alphaGain,
            'colorGain': colorGain,
            'colorSpace': colorSpace,
            'path': path,
            'horiz_tiles': horiz_tiles,
            'vert_tiles': vert_tiles,
            'horiz_offset': horiz_offset,
            'vert_offset': vert_offset,
            'defaultColor': defaultColor,
        }

        key = tuple(sorted(key.items()))
        if key in self.created_textures:
            return self.created_textures[key]

        # We don't have the texture imported.  Search the Daz3d path for it and import it.
        texture, place = self._create_texture(path)

        texture.attr('alphaIsLuminance').set(alphaIsLuminance)
        texture.attr('alphaGain').set(alphaGain)
        texture.attr('colorGain').set(colorGain)
        texture.attr('colorSpace').set(colorSpace)
        texture.attr('defaultColor').set(defaultColor)
        texture.attr('ignoreColorSpaceFileRules').set(True)
        
        place.attr('repeatU').set(horiz_tiles)
        place.attr('repeatV').set(vert_tiles)
        place.attr('offsetU').set(horiz_offset)
        place.attr('offsetV').set(vert_offset)

        # log.debug('Created texture %s: %s', texture, key)
        self.created_textures[key] = texture
        return texture


