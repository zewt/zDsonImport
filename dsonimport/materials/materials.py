import logging, math, os, urllib
from pprint import pprint, pformat
from time import time
from dsonimport.dson import DSON
from dsonimport import util
from dsonimport import maya_helpers as mh
import pymel.core as pm
from dsonimport import uvset

log = logging.getLogger('DSONImporter.Materials')

# DSON files often assign several materials to an object.  The materials sometimes differ only by
# textures, and sometimes are entirely separate materials.  We have a few options for importing this:
#
# - We can import the materials as-is, creating one material per DSON material and assigning them
# separately to the mesh, or create a single material for several DSON materials.
# - We can split the mesh across material boundaries, and assign each material to a single mesh, or
# leave the mesh as a single piece and assign per face.
#
# Splitting meshes across material boundaries generally gives better meshes, since DSON meshes are
# often not logically split, eg. putting eyeballs in the same mesh as the body and unrelated clothing
# parts in the same mesh.  However, if we split a mesh that really is a single seamless piece that
# just happens to have multiple materials can cause seams in lighting, since normals won't interpolate
# cleanly across the split.  This is controlled by MeshGrouping.
#
# Scatter materials are annoying: even though they usually come in as multiple materials, eg. "torso"
# and "arms", we need to collapse them down to a single material.  Scatter doesn't cross shadingEngines,
# so if we don't do this we'll end up with seams at the boundaries.
#
# This creates shadingEngine nodes, assigns them to meshes, creates UV sets, and then just stores pointers
# to the materials in the shadingEngines.  We don't create the actual materials here, instead just pointing
# the shadingEngines at lambert1.  This allows materials to be created later, independent from the time-
# consuming main import, by just creating new material networks and connecting to the existing shadingEngines,
# without needing to know anything about the geometry.  This makes developing material support much easier,
# allows the user to iterate by tweaking the DUF file and reapplying materials, and allows exporting once,
# then applying materials for different renderers.
 
# We only use this to check which materials are using scatter.
import material_arnold

class MaterialSet(object):
    def __init__(self, dson_material, name, source_dson_materials_to_uv_sets=None):
        """
        When we're combining materials into a single material for scatter, source_dson_materials_to_uv_sets
        is a dictionary of the underlying DSON materials and their UV sets.  We'll read the texture paths
        from these and assign them to tiles.
        """
        self.dson_material = dson_material
        self.name = name
        self.uvset_node = None
        self.source_dson_materials = source_dson_materials_to_uv_sets
        if self.source_dson_materials is not None:
            # Sort the material set.  The order will determine tile assignments, so this keeps us
            # from randomly changing the tile order.

            # XXX: if we support multiple layered materials, we should keep the tiles in sync, so
            # tile 0 on each layer is the same thing, even if there's nothing in that tile
            self.source_material_uvsets = source_dson_materials_to_uv_sets
            self.source_dson_materials = sorted(self.source_dson_materials.keys(), key=lambda item: item.node_id)

    def __repr__(self):
        return 'MaterialSet(%s)' % self.name

    @classmethod
    def _create_tiled_uv_set(cls, loaded_mesh, material_set, dson_material_to_faces):
        log.debug('Check source material set: %s (%s)', material_set, ', '.join(str(s) for s in dson_material_to_faces.keys()))

        # Create an empty UV set.  We'll currently only create at most one of these per mesh for
        # the shared scatter material, so just call it "scatter".
        empty_uv_indices = []
        for poly in loaded_mesh.polys:
            empty_uv_indices.append([0]*len(poly['vertex_indices']))
        new_uvset = uvset.UVSet('Scatter', [(0,0)], empty_uv_indices)

        for tile_idx, source_material in enumerate(material_set.source_dson_materials):
            face_list_for_source_material = dson_material_to_faces[source_material]

            # The UV set used by this source material.  Due to grafts, source materials may use
            # multiple source UV sets.
            source_uvset = material_set.source_material_uvsets[source_material]
            log.debug('Source %s has %i faces and uses UV set %s', source_material, len(face_list_for_source_material), source_uvset)

            # If this is a combined material, it shares textures from multiple other materials and
            # we've assigned tiles according to the order in source_dson_materials_to_uv_sets.
            #Create a UV set for this material, with UVs for each material moved to its assigned tile.
            # ... if we support layered textures, each layer might come from a different source UV
            # set and need a separate generated UV set

            # Get the UV tiles used by these faces.  We expect the faces to all lie within the same
            # single UV tile.
            bounds = source_uvset.get_uv_tile_bounds(face_list_for_source_material)
            if bounds[0][0]+1 != bounds[1][0] or bounds[0][1]+1 != bounds[1][1]:
                raise RuntimeError('Faces in UV set %s for source %s cross UV tiles.  This is unsupported with scatter materials.' % (source_uvset, source_material))

            # If the UVs in the source UV set are at U = 5 and we're outputting to tile 2, move the
            # UVs left by 3.  Always move V to 0.
            u_offset = tile_idx - bounds[0][0]
            v_offset = -bounds[0][1]

            # Add the UVs from the source UV set to this UV set, and offset them by u_offset/v_offset.
            # Note that while we're creating a lot of overlapping UVs here since we're adding all UVs
            # and not just the ones we need, we simply won't use the overlapping ones and they'll be
            # culled before the UV set is created.
            first_uv_idx = len(new_uvset.uv_values)
            new_uvset.uv_values.extend(source_uvset.uv_values)
            for idx in xrange(first_uv_idx, first_uv_idx+len(source_uvset.uv_values)):
                value = new_uvset.uv_values[idx]
                new_uvset.uv_values[idx] = (value[0] + u_offset, value[1] + v_offset)

            for face_idx in face_list_for_source_material:
                uv_indices_for_face = [idx+first_uv_idx for idx in source_uvset.uv_indices_per_poly[face_idx]]
                new_uvset.uv_indices_per_poly[face_idx] = uv_indices_for_face

        # Save the new UV set to the mesh.  The key doesn't matter and only needs to be
        # unique.  Save this as the default UV map, since in the common case we'll split
        # skin parts of figures into an isolated mesh, and this will be the only UV set
        # we use.  The default UV set has fewer problems (UV linking is buggy).
        key = object()
        loaded_mesh.default_uv_set_dson_node = key
        loaded_mesh.uv_sets[key] = new_uvset

        return new_uvset

    @classmethod
    def _collect_materials(cls, env):
        """
        Collect a list of materials that we may need to create.

        DSON scenes have a material for each object inheriting from their base object.  Combine
        these into a list of identical materials, so we don't create hundreds of duplicate materials.

        Note that some of these descriptions are over 1 MB of JSON, so we need to be fairly
        efficient here.  Some scenes have multiple identical base materials and then multiple
        identical material instances using each of them, so we do need to be thorough in
        collapsing these back down.
        """
        fields_to_ignore = {'id', 'geometry', 'groups', 'uv_set', 'url'}

        # Find all materials, and sort them by node ID, so we consistently use the same nodes.
        materials = [node for node in env.scene.depth_first() if node.node_source == 'material']
        materials.sort(key=lambda item: item.node_id)

        material_group_mapping = {}
        log.debug('Collecting materials...')
        hashed_materials = {}

        for material in materials:
            flat = {}
            if material.asset:
                flat.update(util.flatten_dictionary(material.asset._data))
                
            flat.update(util.flatten_dictionary(material._data))

            for field in fields_to_ignore:
                if field in flat:
                    del flat[field]

            hashed = util.make_frozen(flat)

            if hashed not in hashed_materials:
                hashed_materials[hashed] = material

            # Point this material at the first material we found that has the same properties.
            material_group_mapping[material] = hashed_materials[hashed]
        return material_group_mapping

def load_and_assign_materials(env, all_mesh_sets, onprogress=None):
    """
    Create Maya materials used by LoadedMeshes, and assign them to each LoadedMesh
    so they can be assigned when the Maya mesh is created.
    """
    # This gives us a mapping from each DSON material to the material we'll use for it,
    # which may be a different material with identical properties.  Note that not all of
    # these materials may actually be used in the scene.  This doesn't look at geometry,
    # it only finds materials.
    material_group_mapping = MaterialSet._collect_materials(env)

    log.debug('Total materials: %i' % len(material_group_mapping))
    log.debug('Unique materials: %i' % len(set(material_group_mapping.values())))

    # Make a list of material sets.  Each material set is a group of materials used by one
    # or more mesh.  All meshes that use the same collection of materials will share a
    # material set.
    #
    # Since this is built from the actual list of meshes, this will only contain materials
    # that we'll actually use.
    dson_material_to_material_set = {}
    loaded_mesh_to_material_sets = {}
    material_sets_to_loaded_mesh_to_faces = {}
    loaded_mesh_to_dson_material_to_faces = {}
    
    for mesh_set in all_mesh_sets.values():
        for loaded_mesh in mesh_set.meshes.values():
            if loaded_mesh in loaded_mesh_to_dson_material_to_faces:
                # If we've already handled this LoadedMesh, this is a shared mesh used by more
                # than one MeshSet.
                continue

            # Store an array of face indices that will have each material assigned to it.
            # For example: { DSONNode(head): [0,1,2,3,4], DSONNode(body): [5,6,7,8,9] }
            dson_material_to_faces = loaded_mesh_to_dson_material_to_faces[loaded_mesh] = {}
            for poly_idx, poly in enumerate(loaded_mesh.polys):
                dson_geometry = poly['dson_geometry']
                dson_material = poly['dson_material']
                dson_material = material_group_mapping[dson_material]

                poly_list = dson_material_to_faces.setdefault(dson_material, [])
                poly_list.append(poly_idx)

            #dson_material_to_dson_uv_set = {material_group_mapping[dson_material]: dson_uv_set
            #        for dson_material, dson_uv_set in loaded_mesh.dson_material_to_dson_uv_set.items()}
            dson_material_to_dson_uv_set = {}
            for dson_material, dson_uv_set in loaded_mesh.dson_material_to_dson_uv_set.items():
                actual_dson_material = material_group_mapping[dson_material]
                dson_material_to_dson_uv_set[actual_dson_material] = dson_uv_set

            # Any materials with SSS need to be flattened to a single material, since scatter won't cross
            # materials.  Make a set of materials that use scatter.
            scatter_materials_to_uv_sets = {}
            for dson_material in dson_material_to_faces.iterkeys():
                log.debug('-> Check material %s for scatter', dson_material)
                if material_arnold.MaterialArnold.grouped_material(dson_material):
                    dson_uv_set = dson_material_to_dson_uv_set[dson_material]
                    uv_sets = loaded_mesh.uv_sets[dson_uv_set]
                    scatter_materials_to_uv_sets[dson_material] = uv_sets

            combined_scatter_material_set = None
            if len(scatter_materials_to_uv_sets) > 1:
                log.debug('Multiple scatter materials in %s: %s', loaded_mesh, ', '.join(s.node_id for s in scatter_materials_to_uv_sets.iterkeys()))

                # If collapse_multi_materials is on and this MaterialSet has more than one material
                # because it uses scatter, reduce it to a single material.  To do this:
                # - Texture channels create a tiled texture combining all texture inputs, instead of
                # using the texture directly.
                # - All other texture properties come from one material input.  We assume that the
                # materials on a shared group are the same.
                # - A material may be used by scatter in multiple meshes.  For example, fingernails are
                # separated from the body, and often have scatter turned on.  We won't share the material
                # layer in this case: each unique scatter layer will have its own copy of the material.
                primary_scatter_dson_material = sorted(scatter_materials_to_uv_sets.keys(), key=lambda key: key.asset_id)[0]
                log.debug('Using material %s as the primary scatter material definition', primary_scatter_dson_material)

                # It's hard to pick a good name for this material, since it's coming from a bunch of other
                # materials with their own names.  Name it after the mesh we're creating it for.  This isn't
                # correct if there are multiple meshes using the material, but the common case for scatter
                # is a single figure.
                name = loaded_mesh.name + '_Scatter'
                # name = primary_scatter_dson_material.node_id
                combined_scatter_material_set = MaterialSet(primary_scatter_dson_material, name, source_dson_materials_to_uv_sets=scatter_materials_to_uv_sets)
                dson_material_to_material_set['scatter'] = combined_scatter_material_set

            # Create non-scatter MaterialSets.
            for dson_material in dson_material_to_faces.iterkeys():
                if dson_material in scatter_materials_to_uv_sets and combined_scatter_material_set:
                    # This material uses combined_scatter_material_set.
                    continue

                # See if we have a MaterialSet with this set of materials, and create a new one if we don't.
                if dson_material in dson_material_to_material_set:
                    continue

                # XXX: This sometimes gives bad names.  For example, if arms and fingernails have the
                # same properties and we've lumped them together in material_group_mapping, we'd normally
                # create one material using the name of one or the other (arbitrarily).  However, if
                # arms are a scatter material and we're not going to create a regular material at all
                # for the arms, we can still choose "arms" as the name for the fingernails, even if
                # the only thing using it is the fingernails.
                name = dson_material.node_id
                dson_material_to_material_set[dson_material] = MaterialSet(dson_material, name)

            # Create the MaterialSets for this mesh, associated with the faces that'll use it.
            for dson_material, faces in dson_material_to_faces.iteritems():
                if dson_material in scatter_materials_to_uv_sets and combined_scatter_material_set:
                    material_set = combined_scatter_material_set
                else:
                    # The usual case: just a single material in the MaterialSet.
                    material_set = dson_material_to_material_set[dson_material]

                # Add this MaterialSet/face list to this LoadedMesh.
                loaded_mesh_to_material_sets.setdefault(loaded_mesh, set()).add(material_set)

                loaded_mesh_to_faces = material_sets_to_loaded_mesh_to_faces.setdefault(material_set, {})
                faces_for_loaded_mesh = loaded_mesh_to_faces.setdefault(loaded_mesh, [])
                faces_for_loaded_mesh.extend(faces)

    # Create shading groups for each material, and assign them to meshes.
    log.debug('Creating shading groups for material sets: %i' % len(dson_material_to_material_set))
    material_set_to_shading_engine = {}
    for material_set in dson_material_to_material_set.values():
        # Create the shadingEngine for this material.
        sg_node = pm.sets(renderable=True, noSurfaceShader=True, empty=True, name='Shader_%s' % material_set.name)

        # For now, assign lambert1 to the shadingEngines so they have a material.  This will be
        # replaced with the actual material later.
        default_material = pm.ls('lambert1')[0]
        default_material.attr('outColor').connect(sg_node.attr('surfaceShader'))

        # Remember the shadingEngine node for this material set.
        material_set_to_shading_engine[material_set] = sg_node

        loaded_mesh_to_faces = material_sets_to_loaded_mesh_to_faces[material_set]

        # Assign mesh faces to this shading group.
        for loaded_mesh, face_indices in loaded_mesh_to_faces.iteritems():
            if loaded_mesh.maya_mesh is None:
                continue

            face_indices.sort()

            # Assign materials to each instance.
            for maya_instance in loaded_mesh.maya_mesh.getInstances():
                # Make a ['mesh.f[100:200]', 'mesh.f[300:400'] list of the faces.  PyMel's MeshFace is
                # unusably slow, so we don't use it here.
                face_ranges = []
                for start, end in mh.get_contiguous_indices(face_indices):
                    face_ranges.append('%s.f[%i:%i]' % (maya_instance, start, end)) 

                # Assign the shading group to the mesh.
                pm.sets(sg_node, edit=True, forceElement=face_ranges)
 
    # Create UV sets for each material on each mesh.
    uvset_nodes_per_material_set = {}
    processed_loaded_meshes = set()
    for mesh_set in all_mesh_sets.values():
        for loaded_mesh in mesh_set.meshes.values():
            # Skip meshes that didn't have a Maya mesh created.
            if loaded_mesh.maya_mesh is None:
                continue

            material_sets_for_loaded_mesh = loaded_mesh_to_material_sets.get(loaded_mesh)
            if material_sets_for_loaded_mesh is None:
                # We aren't loading materials for this mesh.
                continue

            if loaded_mesh in processed_loaded_meshes:
                # This is another instance of another mesh we've already handled.
                continue

            processed_loaded_meshes.add(loaded_mesh)
            # Find the UV set to use for each material set on this mesh.  This can create extra

            # UV sets, so we have to do this before calling create_uv_sets().
            uvset_per_material_set = {}
            for material_set in material_sets_for_loaded_mesh:
                if material_set.source_dson_materials and len(material_set.source_dson_materials) > 1:
                    # For material sets with a source material list, create the UV set for the shared
                    # material (this is only used for scatter materials).
                    #
                    # The faces that will be assigned to each material in this MaterialSet:
                    dson_material_to_faces = loaded_mesh_to_dson_material_to_faces[loaded_mesh]

                    uvset = MaterialSet._create_tiled_uv_set(loaded_mesh, material_set, dson_material_to_faces)
                else:
                    dson_material_to_dson_uv_set = {material_group_mapping[dson_material]: dson_uv_set
                            for dson_material, dson_uv_set in loaded_mesh.dson_material_to_dson_uv_set.items()}

                    dson_uv_set = dson_material_to_dson_uv_set[material_set.dson_material]
                    uvset = loaded_mesh.uv_sets[dson_uv_set]

                uvset_per_material_set[material_set] = uvset

            log.debug('Creating UV sets for %s %s', loaded_mesh, id(loaded_mesh))
            loaded_mesh.create_uv_sets()

            # Collect attributes for all of the UV sets for this mesh.
            for material_set in material_sets_for_loaded_mesh:
                uv_set = uvset_per_material_set[material_set]

                uvset_node = mh.find_uvset_by_name(loaded_mesh.maya_mesh, uv_set.name, required=True)
                default_uv_set = loaded_mesh.maya_mesh.attr('uvSet[0]').attr('uvSetName')
                if uvset_node.get() == default_uv_set.get():
                    # This UV set is the default, so we don't need to assign it.  This avoids creating unneeded
                    # uvChooser nodes.
                    continue
                
                uvset_nodes_per_material_set.setdefault(material_set, []).append(uvset_node)

    for material_set in dson_material_to_material_set.values():
        sg_node = material_set_to_shading_engine[material_set]

        source_dson_materials = material_set.source_dson_materials
        if not source_dson_materials:
            source_dson_materials = [material_set.dson_material]

        pm.addAttr(sg_node, longName='dson_materials', niceName='DSON materials', multi=True, numberOfChildren=1, attributeType='compound' )
        pm.addAttr(sg_node, longName='material_url', niceName='Material URL', dt='string', parent='dson_materials')
        dson_material_array_attr = sg_node.attr('dson_materials')

        for idx, source_dson_material in enumerate(source_dson_materials):
            dson_material_attr = dson_material_array_attr.elementByLogicalIndex(idx)
            dson_material_attr.attr('material_url').set(source_dson_material.url)

        pm.addAttr(sg_node, longName='dson_uvsets', niceName='UV sets', at='message', multi=True)
        uvset_nodes = uvset_nodes_per_material_set.get(material_set, [])
        for idx, uvset_node in enumerate(uvset_nodes):
            if uvset_node is None:
                continue
            uvset_node.connect(sg_node.attr('dson_uvsets').elementByLogicalIndex(idx))

