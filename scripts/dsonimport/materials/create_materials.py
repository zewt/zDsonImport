import logging, os, time
from pprint import pprint, pformat
from dsonimport.dson import DSON
from dsonimport import util
from dsonimport import maya_helpers as mh
from dsonimport.dson.DSONURL import DSONURL
import pymel.core as pm

log = logging.getLogger('DSONImporter.Materials')

from texture_manager import TextureManager
import material_viewport
import material_arnold

material_classes = {
    'Arnold': material_arnold.MaterialArnold,
    'viewport': material_viewport.MaterialViewport,
}

# The material classes to apply, in order.  We don't currently have a UI for this.
material_class_names = ['Arnold', 'viewport'] 

def _create_material_for_shading_group(env, sg_node, texture_manager, material_class, attach_to_viewport):
    # Load the materials we'll use for this shadingEngine.
    #
    # If there are multiple entries in dson_materials, the first is the actual material, and the
    # rest are just references to other materials merged for scatter.  They'll only be used to
    # get their texture paths.
    dson_material_array_attr = sg_node.attr('dson_materials')
    material_indices = dson_material_array_attr.get(mi=True) or []
    dson_materials = []
    for idx in dson_material_array_attr.get(mi=True) or []:
        # We can't just get_url this URL, since it points inside a user scene and not something in the
        # search path.  (DSON search paths unfortuantely look like absolute URLs but aren't, so we can't
        # tell which is which, but these are always pointing at the scene we imported.)  To access it, first
        # load it with load_user_scene, and then get_url will find it without trying to search for it.
        material_url = dson_material_array_attr.elementByLogicalIndex(idx).attr('material_url').get()
        parsed_url = DSONURL(material_url)
        
        env.load_user_scene(parsed_url.path)
        
        dson_material = env.scene.get_url(material_url)
        dson_materials.append(dson_material)

    # If we need to convert textures for this renderer, we need a place to put them.
    #
    # If multiple shading groups use a texture and they both tiling more than one texture, the textures
    # might be in different order.  If we put the textures in the same place they'll overwrite each other
    # and one of the materials will be wrong, but if we put every material's texture in a different
    # directory then we won't share any textures across materials.
    #
    # If the texture has more than one file, put it in a directory named by the shading group.  This will
    # usually only happen for scatter materials, and we usually only have one scatter material in the scene.
    # The other textures aren't tiled, so we can safely put them in the same directory.
    # Look at
    # the material URL, which is normally the user scene that this was imported from, and put
    # them in a textures directory underneath it.
    #
    # XXX: If a material has two color inputs that share some textures and not others
    # and they're copied in Mudbox mode, they'll overwrite each other.
    if len(dson_materials) == 1:
        first_material_path = dson_material_array_attr.elementByLogicalIndex(0).attr('material_url').get()
        parsed_url = DSONURL(first_material_path)
        path = os.path.dirname(parsed_url.path)
        name = os.path.basename(parsed_url.path)
        path = '%s/DSONTextures/%s' % (path, name)
    else:
        path = os.path.dirname(parsed_url.path)
        path = '%s/DSONTextures/%s' % (path, sg_node.name())
    texture_manager.set_path(path)

    # The dson_uvsets array is the UV set connections that we need to make for each texture.  This
    # will often be empty, if we're only using the default UV set.
    uvsets = []
    dson_uvsets = sg_node.attr('dson_uvsets')
    for idx in dson_uvsets.get(mi=True) or []:
        uvset_attr = dson_uvsets.elementByLogicalIndex(idx)

        # If there's no UV set connection on this index, omit the entry.  It'll use the
        # default UV set and don't need to set up a uvChooser.
        connections = uvset_attr.listConnections(s=True, d=False, p=True) or []
        assert len(connections) <= 1, connections
        if len(connections) == 1:
            uvsets.append(connections[0])

    # Create the material, and attach it to the shadingEngine.
    main_dson_material = dson_materials[0]
    name = main_dson_material.node_id

    if len(dson_materials) > 1:
        # If there's more than one material, these were materials merged due to scatter.  Instaed
        # of naming it after an arbitrarily-selected material, just call it "Scatter".  We usually
        # won't have more than one scatter material.
        name = 'Scatter'

    material_node = material_class(env=env, name=name, dson_material=main_dson_material, source_dson_materials=dson_materials, uvsets=uvsets, texture_manager=texture_manager, attach_to_viewport=attach_to_viewport)
    material_node.create(dson_materials[0], sg_node)

def _create_materials(progress):
    env = DSON.DSONEnv()
   
    # Create a shared TextureManager.  All materials that we create will use this to share
    # file nodes.
    texture_manager = TextureManager(env.find_file)

    progress.show('Applying materials...', len(material_class_names) + 1)
    progress.set_main_progress('Loading materials...')

    shading_groups = set()
    selection = pm.ls(sl=True)
    if selection:
        # Get shading groups used by the selection.
        for node in selection:
            shapes = node.listRelatives(ad=True, shapes=True)
            for shape in shapes:
                sg_nodes = pm.listConnections(shape, type='shadingEngine')
                shading_groups.update(pm.listConnections(shape, type='shadingEngine'))
    else:
        shading_groups = pm.ls(type='shadingEngine')

    # Filter to shadingEngines created by load_and_assign_materials.
    shading_groups = [sg for sg in shading_groups if sg.hasAttr('dson_materials')]
    log.debug('Shading groups to apply materials to: %s', shading_groups)

    # Make a mapping from each mesh in the scene to its main DSON transform node.
    meshes = {}
    for sg_node in shading_groups:
        for mesh in pm.sets(sg_node, q=True):
            mesh = mesh.node()

            if not mesh.hasAttr('dson_transform'):
                continue

            transforms = mesh.attr('dson_transform').listConnections(s=True, d=False)
            if not transforms:
                continue

            transform = transforms[0]
            meshes[mesh] = transform

    # The order we create materials matters.  The renderer materials like MaterialArnold
    # will connect to surfaceShader, so they'll be used in the viewport if we don't create
    # a viewport-specific material.  MaterialViewport will then override that connection.
    # If we do it the other way around, the wrong material will end up on surfaceShader.
    for material_class_name in material_class_names:
        material_class = material_classes[material_class_name]
        progress.set_main_progress('Applying %s materials...' % material_class_name)
        for idx, sg_node in enumerate(shading_groups):
            percent = float(idx) / len(shading_groups)
            progress.set_task_progress('Creating material: %s' % sg_node, percent)
            log.debug('Creating %s material for %s', material_class_name, sg_node)

            # Only attach to the viewport shader if this is the viewport material, unless we're not creating
            # the viewport material.  In that case, connect the first material.
            attach_to_viewport = material_class_name == 'viewport'
            if 'viewport' not in material_class_names and material_class_name in material_class_names[0]:
                attach_to_viewport = True

            _create_material_for_shading_group(env, sg_node, texture_manager=texture_manager, material_class=material_class, attach_to_viewport=attach_to_viewport)

        # Let the material class apply renderer-specific mesh properties.  This isn't specific to
        # a material.
        material_class.apply_mesh_properties(env, meshes)

def run_import():
    progress = mh.ProgressWindowMaya()

    try:
        pm.waitCursor(state=True)

        _create_materials(progress)
    except util.CancelledException as e:
        pass
    except BaseException as e:
        # Log the exception, then swallow it, since throwing an exception up to Maya will just
        # log it again.
        log.exception(e)
    finally:
        progress.hide()
        pm.waitCursor(state=False)

def go():
    mh.setup_logging()
    run_import()

