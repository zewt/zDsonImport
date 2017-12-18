import collections, copy, logging, math, os, time
from pprint import pprint, pformat

from dson import DSON, modifiers
from dson.DSONURL import DSONURL
import maya_helpers as mh
import config, modifiers_maya, mayadson
from materials import materials
from . import rigging, util, uvset

from maya import OpenMaya as om
from maya import cmds, mel
from pymel import core as pm
import pymel.core.datatypes as dt
import pymel

import sys

log = logging.getLogger('DSONImporter')

def assert_equal(val1, val2):
    assert val1.__class__ == val2.__class__, (val1, val2)
    if isinstance(val1, list):
        assert len(val1) == len(val2), (val1, val2)
        for idx in xrange(len(val1)):
            assert abs(val1[idx] - val2[idx]) < 0.001, (val1, val2)

def subtract_vector3(v1, v2):
    return (v1[0]-v2[0], v1[1]-v2[1], v1[2]-v2[2])

def vector_length(vec):
    return math.pow((vec[0]*vec[0]) + (vec[1]*vec[1]) + (vec[2]*vec[2]), 0.5)

def quaternion_slerp(q1, q2, t):
    # Maya's Quaternion classes in Python don't have slerp?  Seriously?

    # dt.Quaternion is actually a matrix.  Convert it to a vector, which makes a lot
    # more sense.
    v1 = dt.VectorN([q1.x, q1.y, q1.z, q1.w])
    v2 = dt.VectorN([q2.x, q2.y, q2.z, q2.w])

    dot = v1.dot(v2)
    angle = math.acos(dot)
    sin = math.sin(angle)

    if abs(sin) < 0.001:
        t1 = 1-t
        t2 = t
    else:
        t1 = math.sin((1.0 - t) * angle) / sin
        t2 = math.sin(t * angle) / sin

    result = v1*t1 + v2*t2
    return dt.Quaternion(*result)
 
class SharedGeometry(object):
    def __init__(self, loaded_meshes):
        self.loaded_meshes = loaded_meshes
        self.maya_meshes = {}

    def created_maya_mesh(self, loaded_mesh, maya_mesh):
        self.maya_meshes[loaded_mesh] = maya_mesh

    def create_maya_mesh_instance(self, loaded_mesh):
        maya_mesh = self.maya_meshes.get(loaded_mesh)
        if maya_mesh is None:
            return None

        new_transform = pm.duplicate(maya_mesh, instanceLeaf=True)[0]
        new_mesh = new_transform.getShape()
        return new_mesh

class MeshUnaffected(Exception): pass
class RigidityTransform(object):
    def __init__(self, rigidity_group, loaded_mesh, reference_target_mesh, deformed_target_mesh):
        """
        Create a transform for a rigidity group.

        loaded_mesh is the LoadedMesh affected by this rigidity group.
        reference_target_mesh is the original Maya mesh.
        deformed_target_mesh is the target Maya mesh.

        If loaded_mesh isn't affected by this rigidity group, raise MeshUnaffected.
        """
        self.rigidity_group = rigidity_group
        self.reference_mesh = reference_target_mesh

        # First, see if any weights apply to this mesh.  Since we may have split the original
        # mesh into pieces, many parts may not be influenced by a RG at all.  If there's no weights
        # array, vertices in all references_vertices are affected.
        if 'rigidity/weights/values' in loaded_mesh.geom:
            rigidity_weights = loaded_mesh.geom.get_value('rigidity/weights/values', [])
            rigidity_weights = { vertex_idx: weight for vertex_idx, weight in rigidity_weights }
        else:
            rigidity_weights = { vertex_idx: 1 for vertex_idx in rigidity_group['mask_vertices/values'].value }

        self.rigidity_weights = [0] * len(loaded_mesh.verts)

        submesh_info = loaded_mesh.submesh_info[loaded_mesh.geom]
        any_matched = False
        for src_vertex_idx in rigidity_group['mask_vertices/values'].value:
            dst_vertex_idx = submesh_info['orig_to_new_vertex_mappings'].get(src_vertex_idx)
            if dst_vertex_idx is None:
                continue

            # If a vertex isn't in the mask, it has a value of 0 (not rigid).
            self.rigidity_weights[dst_vertex_idx] = rigidity_weights.get(src_vertex_idx, 0)
            any_matched = True

        if not any_matched:
            # No vertices in this RG are in this mesh.
            raise MeshUnaffected()

        log.debug('Creating rigidity group: %s', rigidity_group)

        # Each rigidity group has a list of rigid vertices (mask_vertices), and a list of vertices for
        # them to follow (reference_vertices).  We've split meshes apart, so these may be on different
        # LoadedMeshes, and they may cross multiple meshes.  We'll only match against a single mesh,
        # since it's not worth the complexity of mapping against multiply target meshes.
        #
        # Note that vertices can follow themselves, and in some rigidity groups, all vertices simply follow
        # themselves.  That's valid and not cyclical: it means that the geometry is rigid, but follows
        # the average position of its non-rigid counterpart.
        #
        # Search all LoadedMeshes to find the one containing the most vertices that this RG wants to
        # follow.  We could do this without checking each group again, but it would be a bit more complex.
        reference_vertices = rigidity_group['reference_vertices/values'].value

        submesh_info = loaded_mesh.submesh_info[loaded_mesh.geom]
        vertices = []
        for src_vertex_idx in reference_vertices:
            dst_vertex_idx = submesh_info['orig_to_new_vertex_mappings'].get(src_vertex_idx)
            vertices.append(dst_vertex_idx)

        log.debug('target %s', loaded_mesh)

        name = '%s_%s' % (loaded_mesh.maya_mesh.name(), rigidity_group.get_value('id'))
        name = mh.cleanup_node_name(name)
        self.name = name

        vertices = ['vtx[%i]' % idx for idx in vertices]

        # Create a groupParts (and associated groupId) for the components of the reference geometry that
        # we want.
        reference_group_id = pm.createNode('groupId')
        reference_group_parts = pm.createNode('groupParts')
        self.reference_mesh.connect(reference_group_parts.attr('inputGeometry'))
        reference_group_id.attr('groupId').connect(reference_group_parts.attr('groupId'))
        pm.setAttr(reference_group_parts.attr('inputComponents'), len(vertices), *vertices, type='componentList')

        # Create a groupParts for the equivalent geometry on the blendShape's output.
        deformed_group_id = pm.createNode('groupId')
        deformed_group_parts = pm.createNode('groupParts')
        deformed_target_mesh.connect(deformed_group_parts.attr('inputGeometry'))
        deformed_group_id.attr('groupId').connect(deformed_group_parts.attr('groupId'))
        pm.setAttr(deformed_group_parts.attr('inputComponents'), len(vertices), *vertices, type='componentList')

        # Create an OBBTransform.  This will read the mesh before and after the wrap, and give us
        # a transform to apply to the rigid meshes.  The input mesh is the geometry that went into the
        # wrap deformer (narrowed to the components we're interested in) and the current mesh is the
        # geometry that came out of the wrap.
        self.obb_transform = mh.create_rigidity_transform(rigidity_group, self.name)

        reference_group_parts.attr('outputGeometry').connect(self.obb_transform.attr('inputMesh'))
        reference_group_id.attr('groupId').connect(self.obb_transform.attr('inputGroupId'))

        deformed_group_parts.attr('outputGeometry').connect(self.obb_transform.attr('currentMesh'))
        deformed_group_id.attr('groupId').connect(self.obb_transform.attr('currentGroupId'))

        # Create a pivot and a transform to receive the zOBBTransform position.
        self._pivot_node = pm.createNode('transform', n='TempPivot_%s' % self.name)
        position_node = pm.createNode('transform', n='TempPosition_%s' % self.name, p=self._pivot_node)
        self.obb_transform.attr('pivot').connect(self._pivot_node.attr('translate'))
        self.obb_transform.attr('translate').connect(position_node.attr('translate'))
        self.obb_transform.attr('rotate').connect(position_node.attr('rotate'))
        self.obb_transform.attr('scale').connect(position_node.attr('scale'))
        self.position_node = position_node

        # Read the transform nodes.  These transforms will move with the rigidity group.
        transform_nodes = rigidity_group.get_property('transform_nodes', None)
        if transform_nodes:
            top_node = loaded_mesh.geom.find_top_node()
            self.target_transforms = [top_node.get_modifier_url(transform_node) for transform_node in transform_nodes.array_children]
        else:
            self.target_transforms = []

    def delete(self):
        """
        Delete the nodes created by this RigidTransform.
        """
        pm.delete(self._pivot_node)

    def __repr__(self):
        return 'RigidityGroup(%s)' % self.name

class MeshSet(object):
    """
    A MeshSet contains a set of LoadedMeshes.

    DSON geometry is split into sub-meshes, usually by material, and grouped into a MeshSet.
    Except for grafted geometry, all LoadedMeshes in the same MeshSet come from the same DSON
    geometry.
    """
    def __init__(self, geom):
        self.env = geom.env
        self.dson_geometry = geom

        # If we create a group to hold Maya meshes created for self.meshes, we'll store it here.
        self.maya_group_node = None

        # We'll share geometry across mesh sets if it shares the same asset, doesn't override geometry
        # and doesn't have any modifiers applied to it.  This improves import speed in scenes with a
        # lot of simple props.
        if not hasattr(self.env, 'shared_geometries'):
            self.env.shared_geometries = {}

        sharable_geometry = True
        for dson_morph in self.dson_geometry.children:
            if dson_morph.node_source == 'modifier':
                log.debug('Can\'t share geometry %s because it has modifiers', self.dson_geometry)
                sharable_geometry = False
                break

        if sharable_geometry and ('polylist' in self.dson_geometry._data or 'vertices' in self.dson_geometry._data):
            log.debug('Can\'t share geometry %s because it sets geometry on the instance', self.dson_geometry)
            sharable_geometry = False

        self.shared_geometry = None

        shared_geometry_url = self.dson_geometry.asset.url
        if sharable_geometry and shared_geometry_url in self.env.shared_geometries:
            # Another mesh has already parsed out this geometry.  Just point our mesh list at
            # the same data.
            log.debug('Mesh %s is using shared geometry (%s)', self.dson_geometry, self.dson_geometry.asset.url)
            self.shared_geometry = self.env.shared_geometries[shared_geometry_url]
            self.meshes = self.shared_geometry.loaded_meshes
            return

        # The list of polys in this mesh:
        polys = geom['polylist/values']
        assert geom['polylist/count'].value == len(polys.value)

        # poly_groups = geom['polygon_groups/values']
        material_groups = geom['polygon_material_groups/values'].value

        # Extract polys, grouping them by mesh group.  Each mesh group will become a final output mesh
        # (unless it's grafted onto another mesh).

        # The DSON material used by each mesh group.
        dson_materials_by_mesh_name = {}

        self.meshes = {}

        mesh_group_per_material = {}
        for material_group in material_groups:
            mesh_group_per_material[material_group] = self._get_mesh_group(geom, material_group)

        # We access the underlying array instead of using the DSON library's iteration here
        # for speed.  Groups polys by mesh group to create LoadedMeshes, and make a list of
        # all polys.
        polys_by_mesh_name = {}
        all_polys = []
        for poly_idx, face in enumerate(polys.value):
            poly_group_idx = face[0]
            material_group_idx = face[1]
            vertex_indices = face[2:]

            material_group = material_groups[material_group_idx]
            mesh_name = mesh_group_per_material[material_group]

            dson_material = self.dson_geometry.materials[material_group]
            dson_materials_by_mesh_name.setdefault(mesh_name, set()).add(dson_material)

            this_poly = {
                'source_poly_idx': poly_idx,
                'vertex_indices': vertex_indices,
                'dson_material': dson_material,

                # Remember the DSON mesh that each poly came from, so we can find materials later,
                # even if polys from different objects are grafted.
                'dson_geometry': self.dson_geometry,
            }

            info = polys_by_mesh_name.setdefault(mesh_name, [])
            info.append(this_poly)

            all_polys.append(dict(this_poly))

        log.debug('MeshSet %s creating meshes', self)
        for mesh_name, polys in polys_by_mesh_name.items():
            self.meshes[mesh_name] = LoadedMesh(self, self.dson_geometry, mesh_name, polys, dson_materials_by_mesh_name[mesh_name])
            log.debug('    Created %s for %s', self.meshes[mesh_name], mesh_name)

        # Create a combined mesh, which is simply the original unsplit mesh.
        self.combined_loaded_mesh = LoadedMesh(self, self.dson_geometry, 'Combined_' + self.dson_geometry.parent.get_label(), all_polys, self.dson_geometry.materials.values())

        # If this geometry is sharable, save the LoadedMesh set for other MeshSets to use.
        if sharable_geometry:
            self.env.shared_geometries[shared_geometry_url] = self.shared_geometry = SharedGeometry(self.meshes)

    def _get_mesh_group(self, dson_geometry, material_group):
        if config.get('mesh_split_mode') == 2:
            # "Never split"
            return 'Body'

#        return 'Body'
#        return material_group

        # Skip this in mesh_split_mode 1 ("don't split props").
        if config.get('mesh_split_mode') != 1:
            # We can either split material groups into their own mesh, or group them together and create
            # a single mesh.  Separating meshes is generally better, except when a mesh is continuous, where
            # separating the mesh at a material boundary will create a lighting seam at the boundary.  This
            # is most important with skin materials.
            #
            # Another detail is that if we have meshes to graft to this one, we'll only graft if there's only
            # one possible 

            # First, just split prop assets.  These are mostly hard-edged objects that are safe to split.
            if dson_geometry.asset and dson_geometry.asset.dson_file.asset_type == 'prop':
                # Don't split for now.  Splitting these makes sculpting blend shapes harder.
                # return 'Body'
                return material_group

            # Split if this geometry is conforming.  Most hair and clothing should be split.
            if dson_geometry.parent.get('conform_target'):
                return material_group

        # Try to separate out parts of the mesh that are distinct and easily disconnected from each
        # other.  This gives simpler materials, and allows us to turn on transparency selectively for
        # small objects (eg. eye parts) without turning it on for the whole body, which requires slow
        # depth peeling to render in the viewport.  However, don't separate out parts of the skin, since
        # that'll cause problems: seams due to mismatched normals, SSS seams and smoothing gaps.
        if material_group in ('Sclera', 'EyeMoisture', 'Eyelashes', 'Cornea', 'EyeSocket', 'Teeth', 'Mouth', 'Irises','Pupils', 'Fingernails', 'Toenails'):
            return material_group

        return 'Body'


    def __repr__(self):
        return 'MeshSet(%s)' % self.dson_geometry.parent

    @property
    def name(self):
        return self.dson_geometry.parent.get_value('id')

    def remove_group_node_if_empty(self):
        """
        If we've split meshes apart, we've grouped them in a "Meshes" group inside the
        transform.  This leads to a hierarchy like

        House
          Meshes
            Walls
            Floors
          Door  
            Meshes
              Doorknob
              Door

        Move meshes out of "Meshes" and into its parent transform if there are no other
        children of the parent.  This changes the above to:

        House
          Meshes
            Walls
            Floors
          Door  
            Doorknob
            Door

        We leave the "Walls" and "Floors" meshes in a group, so we keep sub-meshes of House
        grouped distinctly from other children of House.

        This should never affect transforms, since Meshes groups should always have a null transform.
        """
        # Only do this with props.  Figures should stay grouped.
        if self.dson_geometry.file_asset_type != 'prop':
            return

        if not self.maya_group_node:
            return

        log.debug('Unparenting mesh set %s' % self)

        parent = self.maya_group_node.getParent()
        if pm.listRelatives(parent, children=True) != [self.maya_group_node]:
            # Don't unparent Meshes if there are other siblings of the Meshes node.
            return

        # Move the meshes up to the parent of the group node.
        for child in pm.listRelatives(self.maya_group_node, children=True):
            pm.parent(child, parent)

        # Delete the empty Meshes node.
        pm.delete(self.maya_group_node)
        self.maya_group_node = None


class LoadedMesh(object):
    def __init__(self, mesh_set, geom, name, polys, dson_materials):
        """
        Load geometry from a DSON node.
        """
        self.mesh_set = mesh_set
        self.name = name
        self.geom = geom
        self.materials = set(dson_materials)
        self.grafted_meshes = []

        # The vertex list is maintained, and kept around if meshes are merged.
        self.verts = geom['vertices/values'].value
        assert geom['vertices/count'].value == len(self.verts)

        # Load UV sets for each material.  Usually these will be the same, but materials
        # can individually override UV sets.
        self.uv_sets = {}
        self.dson_material_to_dson_uv_set = {}

        for dson_material in dson_materials:
            if 'uv_set' in dson_material:
                dson_uv_set = dson_material['uv_set'].load_url()
            else:
                dson_uv_set = geom['default_uv_set'].load_url()

            # Remember which DSON UV set each material uses.
            self.dson_material_to_dson_uv_set[dson_material] = dson_uv_set

            # When multiple materials use the same UV set, we don't need to parse it out twice.
            if dson_uv_set not in self.uv_sets:
                self.uv_sets[dson_uv_set] = uvset.UVSet.create_from_dson_uvset(dson_uv_set, polys)

        # Remember one of our current UV sets as the default, so we'll make it the default
        # Maya map (map1).
        self.default_uv_set_dson_node = sorted(self.uv_sets.keys())[0]

        # Vertices originally come from a single mesh, but after grafting, vertices will be merged.  We
        # need to remember the mapping from vertices in graft geometry to the combined geometry, so we
        # can apply skins and morphs later.  For each LoadedMesh that makes up this one (including ourself),
        # keep a sorted list of (our index, their index), mapping from our mesh's indices to theirs.  Note
        # that this isn't needed when merging MeshGeometry from the same LoadedMesh (they already have the
        # same vertex list), only when grafting between different LoadedMeshes.
        self.submesh_info = {}
        mapping = util.VertexMapping()
        for idx in xrange(len(self.verts)):
            mapping[idx] = idx

        self.submesh_info[self.geom] = {
            'orig_to_new_vertex_mappings': mapping,
        }

        self.polys = polys
        self.maya_mesh = None

    def __repr__(self):
        dson_transform = self.mesh_set.dson_geometry.parent
        return 'LoadedMesh(%s:%s)' % (dson_transform.node_id, self.name)

    def create_uv_sets(self):
        # Remove unused UVs before creating UV sets.
        for uv_set in self.uv_sets.values():
            uv_set.cull_unused(self.polys)

        # Stop if we didn't actually create the mesh.
        if self.maya_mesh is None:
            return

        # An array containing the number of vertices per face:
        vertex_counts_array = om.MIntArray()
        for poly_idx, poly in enumerate(self.polys):
            vertex_indices = poly['vertex_indices']
            vertex_counts_array.append(len(vertex_indices))

        # Create each UV set.
        default_uv_set = self.uv_sets[self.default_uv_set_dson_node]

        meshFn = om.MFnMesh(self.maya_mesh.__apimobject__())
        pm.select(self.maya_mesh)
        for uv_set in self.uv_sets.values():
            uv_set.name = mh.cleanup_node_name(uv_set.name)
            if uv_set is default_uv_set:
                meshFn.renameUVSet('map1', uv_set.name)
            else:
                meshFn.createUVSetWithName(uv_set.name)

            # Create the UV list.
            uvs_u = om.MFloatArray()
            uvs_v = om.MFloatArray()

            for u, v in uv_set.uv_values:
                uvs_u.append(u)
                uvs_v.append(v)

            meshFn.setUVs(uvs_u, uvs_v, uv_set.name)

            uv_indices_array = om.MIntArray()
            for poly_idx, poly in enumerate(self.polys):
                vertex_indices = poly['vertex_indices']
                uv_indices = uv_set.uv_indices_per_poly[poly_idx]
                assert len(vertex_indices) == len(uv_indices)

                for idx, vertex_idx in enumerate(vertex_indices):
                    uv_idx = uv_indices[idx]
                    uv_indices_array.append(uv_idx)


            meshFn.assignUVs(vertex_counts_array, uv_indices_array, uv_set.name)

        meshFn.updateSurface()

    def create_mesh(self):
        """
        Create a Maya mesh for this LoadedMesh.

        If base_only is true, the mesh will be assigned as the maya_mesh for this LoadedMesh,
        and UVs will be assigned.  This is used for creating the final mesh.

        If base_only if false, we'll only create the mesh and return it.  UVs won't be assigned,
        and this LoadedMesh won't be modified.
        """
        self.cull_unused()

        # Create the vertex list.
        vertex_array = om.MPointArray()
        vertex_array.setLength(len(self.verts))
        for idx, vert in enumerate(self.verts):
            point = om.MPoint(*vert)
            vertex_array.set(point, idx)

        # Create the index lists for faces.
        faces = om.MIntArray()
        vertex_counts_array = om.MIntArray()
        for poly_idx, poly in enumerate(self.polys):
            vertex_indices = poly['vertex_indices']
            vertex_counts_array.append(len(vertex_indices))

            # The same vertex should never be used in the same face more than once.  This would trigger
            # automatic mesh cleanup in create(), which would change the vertex order.
            # XXX: meshes with triangles repeat the first vertex twice?

            if len(set(vertex_indices)) != len(vertex_indices):
                log.warning('Skipped mesh %s (vertex repeats in face #%i)', self, poly_idx)
                return
            assert len(set(vertex_indices)) == len(vertex_indices), '%i != %i' % (len(set(vertex_indices)), len(vertex_indices))

            for idx, vertex_idx in enumerate(vertex_indices):
                faces.append(vertex_idx)

        # Create the shape.
        meshFn = om.MFnMesh()
        meshMObj = meshFn.create(vertex_array.length(), vertex_counts_array.length(), vertex_array, vertex_counts_array, faces)
        transformFn = om.MFnTransform(meshMObj)
        transformFn.setName(self.name)
        meshFn.setName(self.name + 'Shape')

        mesh_node = pm.ls(meshFn.name(), l=True)[0]

        # OpenSubDiv Catmull-Clark breaks materials in the viewport.  Switch to Maya CC.  This doesn't
        # turn on smooth mesh preview, it just sets it up in case it's turned on later.
        pm.setAttr(mesh_node.attr('useGlobalSmoothDrawType'), 0)
        pm.setAttr(mesh_node.attr('smoothDrawType'), 0)

        # Assign lambert1 for now.  We haven't created materials yet.
        pm.sets('initialShadingGroup', edit=True, forceElement=mesh_node)

        # Reduce the viewport smoothing level from 2 to 1, which is faster and enough for most
        # meshes.  We might increase this later if we enable viewport displacement maps, and
        # the renderer should set its own smoothing.
        mesh_node.attr('smoothLevel').set(1)

        # Add an attribute pointing back to the geometry.  Note that this will always point
        # to the base geometry, and doesn't say anything about geometry grafted onto it.
        pm.addAttr(mesh_node, longName='dson_geometry_url', dt='string', niceName='DSON geometry URL')
        mesh_node.attr('dson_geometry_url').set(self.geom.url)

        return mesh_node

    @property
    def polys(self):
        return self._polys

    @polys.setter
    def polys(self, new_polys):
        if not isinstance(new_polys, tuple):
            # Always save a tuple, to make sure we don't accidentally modify this in-place.
            # We should always make a copy, modify the copy and set it, to ensure we clear
            # cached_used_vertex_indices.
            new_polys = tuple(new_polys)

        self._polys = new_polys
        # When the vertex list changes, invalidate the source map.
        self._invalidate_cache()

    def _invalidate_cache(self):
        # unused
        pass

    def cull_unused(self):
        """
        Remove vertices and UVs that aren't used.

        This needs to be done before we can create Maya meshes, since if we include unused
        data when we create the mesh, Maya will retain it.  This will bloat the file a lot,
        and cause problems with mesh smoothing.
        """
        # Make a list of used vertex and UV indices.
        used_vertex_indices = []
        for poly_idx, poly in enumerate(self.polys):
            for idx, vertex_idx in enumerate(poly['vertex_indices']):
                used_vertex_indices.append(vertex_idx)

        # Remove duplicates, sort the list, and map the old UV indices to new ones.
        used_vertex_indices = list(set(used_vertex_indices))
        used_vertex_indices.sort()

        old_vertex_indices_to_new = {old_vertex_idx: new_vertex_idx for new_vertex_idx, old_vertex_idx in enumerate(used_vertex_indices)}
        new_vertex_indices_to_old = {b: a for a, b in old_vertex_indices_to_new.items()}


        self.verts = [self.verts[new_vertex_indices_to_old[idx]] for idx in xrange(len(new_vertex_indices_to_old))]

        # Remap vertex and UV indices to their new positions.
        for poly in self.polys:
            poly['vertex_indices'] = [old_vertex_indices_to_new[vertex_idx] for vertex_idx in poly['vertex_indices']]

        # Update the mapping of source vertex indices to our indices.
        for submesh_info in self.submesh_info.values():
            new_mapping = util.VertexMapping()

            for orig_vertex_idx, new_vertex_idx in submesh_info['orig_to_new_vertex_mappings'].items():
                try:
                    remapped_new_vertex_idx = old_vertex_indices_to_new[new_vertex_idx]
                    new_mapping[orig_vertex_idx] = remapped_new_vertex_idx
                except KeyError:
                    # This vertex doesn't exist anymore, so remove it from orig_to_new_vertex_mappings too.
                    continue

            submesh_info['orig_to_new_vertex_mappings'] = new_mapping

    def get_dson_geometries(self):
        """
        Return a list of DSON geometry nodes that are included in this one.

        This includes grafts.
        """
        return self.submesh_info.keys()

class DSONImporterConfig(object):
    def __init__(self):
        # This is set to a AssetCacheResults.
        self.asset_cache_results = None

        # This is a set of modifiers to create, by asset URL.  Modifiers that aren't included in
        # here won't be created, even if they're instanced in the scene.
        self.modifier_asset_urls = None

        # This is a dictionary of preferences.  These will override the defaults in config.py.
        self.prefs = {}

class DSONImporter(object):
    def __init__(self, dson_importer_config):
        """
        asset_cache_results is a dson.AssetCacheResults(), which is the result of the
        user configuration UI.
        """
        mh.load_plugin('matrixNodes.mll')

        self.config = dson_importer_config
        config.set(self.config.prefs)

        self.env = DSON.DSONEnv()
        log.debug('DSON search path: %s', ', '.join(self.env._search_path))

        self.env.set_is_property_dynamic_handler(self.is_property_dynamic)

        mh.flush_channel_cache()
        self.progress = mh.ProgressWindowMaya()

    def create_maya_geometry(self):
        if not config.get('geometry'):
            return

        self.apply_grafts()

        # Create Maya geometry.
        for mesh_set in self.all_mesh_sets.values():
            self.create_maya_geometry_for_mesh_set(mesh_set)

    def create_maya_geometry_for_mesh_set(self, mesh_set):
        mesh_group = mesh_set.dson_geometry.parent.maya_node

        if len(mesh_set.meshes) > 1:
            # All of these meshes are from the same geometry, which has a transform as a parent.
            # Group them inside a "Meshes" group under the transform.
            mesh_group = pm.createNode('transform', n='Meshes', p=mesh_group)
            mh.hide_common_attributes(mesh_group, transforms=True)

            # Remember if this LoadedMesh is inside a Meshes group.
            mesh_set.maya_group_node = mesh_group

        if mesh_group is None:
            mesh_group = mesh_set.dson_geometry.parent.maya_node

        for loaded_mesh in mesh_set.meshes.values():
            if mesh_set.shared_geometry:
                # This mesh can share geometry.  If another mesh sharing geometry with the same
                # asset has already created a shape, just duplicate it.
                loaded_mesh.maya_mesh = mesh_set.shared_geometry.create_maya_mesh_instance(loaded_mesh)
                log.debug('Created instance for %s: %s', loaded_mesh, loaded_mesh.maya_mesh)

            # If we didn't instance a mesh (or we're not instancing for this mesh), create the mesh.
            if loaded_mesh.maya_mesh is None:
                log.info('Creating Maya mesh for %s' % loaded_mesh)
                loaded_mesh.cull_unused()
                mesh = loaded_mesh.create_mesh()
                loaded_mesh.maya_mesh = mesh

            if loaded_mesh.maya_mesh is None:
                # create_mesh didn't create a mesh for some reason.
                continue

            # Bind-time transforms on the mesh don't move the base mesh.  If there's a center_point
            # on the transform, the mesh is still at the origin.  So, we don't use r=True here.
            shape_transform = loaded_mesh.maya_mesh.getParent()
            pm.parent(shape_transform, mesh_group)

            if mesh_set.shared_geometry:
                # Store this mesh for other meshes to duplicate.
                mesh_set.shared_geometry.created_maya_mesh(loaded_mesh, loaded_mesh.maya_mesh)
            
        #pm.refresh()

    def apply_grafts(self):
        if not config.get('grafts'):
            return

        # Grafting geometry nodes never actually say which geometry to graft to.  They only point to
        # a transform (eg. #Genesis3Female), and not geometry (#Genesis3Female-1).  It seems like it
        # just takes the first geometry in the list, and although "geometry" is an array there isn't
        # actually more than one of them in a transform.
        for mesh_set in self.all_mesh_sets.itervalues():
            # It's possible for a node that isn't a graft to contain an empty "graft" dictionary.
            if not mesh_set.dson_geometry.is_graft:
                continue

            target_transform = mesh_set.dson_geometry.get_conform_target()
            if target_transform is None:
                continue
                
            # This is a graft with conform_target enabled.
            target_geometry = target_transform.first_geometry
            if target_geometry is None:
                log.debug('Mesh %s conforms to %s, but that node has no geometry', dson_node, target_transform)
                continue

            # Find the MeshSet of the geometry we're grafting onto.
            # target_geometry is the DSONNode of the geometry.
            graft_onto_mesh_set = self.all_mesh_sets[target_geometry]

            # Make a list of vertices in each LoadedMesh in the target, so we can search quickly below.
            vertex_indices_per_loaded_mesh = {}
            for loaded_mesh in graft_onto_mesh_set.meshes.itervalues():
                indices = set()
                for poly_idx, poly in enumerate(loaded_mesh.polys):
                    if poly['dson_geometry'] is loaded_mesh.geom:
                        indices.add(poly['source_poly_idx'])

                vertex_indices_per_loaded_mesh[loaded_mesh] = indices

            def find_target_mesh(graft_mesh):
                # Return the target submesh that graft_mesh grafts to.
                graft = graft_mesh.geom.get_property('graft')
                hidden_polys = set(graft['hidden_polys/values'].value)
                best_match = None
                for loaded_mesh in graft_onto_mesh_set.meshes.itervalues():
                    # See if there's any overlap between the vertices in this mesh and the vertices that this
                    # graft hides.
                    if not (vertex_indices_per_loaded_mesh[loaded_mesh] & hidden_polys):
                        continue

                    if best_match is not None:
                        log.warning('Mesh "%s" grafts onto more than one target mesh (%s, %s).', graft_mesh, best_match, loaded_mesh)
                        return None

                    best_match = loaded_mesh
                if best_match is None:
                    log.warning('Can\'t find mesh to graft %s onto' % graft_mesh)

                return best_match

            log.info('Applying graft: %s onto %s', mesh_set.dson_geometry.parent, target_geometry)
            for graft_mesh in mesh_set.meshes.itervalues():
                # That tells us which original geometry node to graft to, but we've split the geometry apart
                # into pieces (body, eyes) and it doesn't tell us which part to graft onto.  Search through
                # the meshes in the target MeshSet for one that matches all of the vertices in the hidden_polys
                # list.  If we're trying to graft to geometry that has been split across where we're trying
                # to graft, reject the graft.
                graft_onto_mesh = find_target_mesh(graft_mesh)
                if graft_onto_mesh is None:
                    continue

                self._graft_geometry(graft_mesh, graft_onto_mesh)

    def _get_mesh_sets_in_conform_dependency_order(self, loaded_meshes):
        # Tricky: When a mesh is conforming to another mesh, both the conforming and target
        # mesh can have morphs.  We'll have a wrap deformer on the conforming mesh.  We need
        # to apply morphs to target meshes before meshes that conform to them.  If we have
        # a body and clothing conforming to it, we do this:
        #
        # - Apply a wrap deformer on the clothing, targetting the body.  (This is already done
        # when we get here.)
        # - Apply morphs to the body.  The clothing will follow due to the wrap.  This must
        # be done before the clothing.
        # - Bake the wrap deformer that's on the clothing.  If we don't do this before applying
        # blend shapes, the blend shapes will be very slow since they'll cause the wrap deformer
        # to reevaluate whenever they change.
        # - Apply morphs to the clothing.
        #
        # First, group LoadedMeshes by the mesh they conform to, or None for ones that don't
        # conform to anything.
        # Make a mapping of LoadedMeshes and the LoadedMeshes they're conforming to.
        all_mesh_sets_by_target = {}
        for mesh_set in self.all_mesh_sets.values():
            conformed_mesh_set, target_mesh_set = self.get_conforming_to_geometry(mesh_set.dson_geometry.parent)
            all_mesh_sets_by_target.setdefault(target_mesh_set, set()).add(mesh_set)

        # If we have any at all, we'll always have at least one mesh in None.
        if all_mesh_sets_by_target:
            assert all_mesh_sets_by_target[None]

        while all_mesh_sets_by_target:
            # Get the list of meshes that don't conform to a LoadedMesh that we haven't processed yet.
            mesh_sets = all_mesh_sets_by_target[None]
            del all_mesh_sets_by_target[None]

            # Sort the meshes, so we always process things in the same order.
            mesh_sets = list(mesh_sets)
            mesh_sets.sort(key=lambda item: item.name)

            for mesh_set in mesh_sets:
                yield mesh_set

            # Move all LoadedMeshes that depended on ones we just processed into the None group, so they'll
            # be handled next.
            next_list = []
            for mesh_set in mesh_sets:
                if mesh_set not in all_mesh_sets_by_target:
                    continue

                deps = all_mesh_sets_by_target[mesh_set]
                del all_mesh_sets_by_target[mesh_set]
                next_list.extend(deps)

            if not next_list:
                # There are no new meshes to process.  The list should be empty.
                assert not all_mesh_sets_by_target, pformat(all_mesh_sets_by_target)
                break

            all_mesh_sets_by_target[None] = next_list

    def get_conforming_to_geometry(self, dson_node):
        target_transform = dson_node.get('conform_target')
        if target_transform is None:
            return None, None

        # Ignore target_geometry if there's no geometry.  We'll still conform any joints.
        conformed_geometry = dson_node.first_geometry
        if conformed_geometry is None:
            return None, None

        # Load the conform target.
        target_transform = target_transform.load_url()
        target_geometry = target_transform.first_geometry
        if target_geometry is None:
            log.debug('Mesh %s conforms to %s, but that node has no geometry', dson_node, target_transform)
            return None, None

        # Find the MeshSet for the geometry we're conforming to.
        target_mesh_set = self.all_mesh_sets.get(target_geometry)
        if target_mesh_set is None:
            log.debug('No meshes loaded for conform target %s', target_geometry)
            return None, None

        # Find the MeshSet for the geometry being conformed.
        conformed_mesh_set = self.all_mesh_sets.get(conformed_geometry)
        if not conformed_mesh_set:
            # There are no LoadedMeshes for this geometry.  Not all conformed geometry
            # will have a mesh, eg. if it's grafted into another mesh.
            return None, None

        return conformed_mesh_set, target_mesh_set

    def create_conform_wraps(self, conformed_mesh_set, target_mesh_set):
        """
        Create wrap deformers for meshes that are conforming to other meshes.
        These are used by apply_morphs to create conformed morphs.

        This isn't used for grafts (which are handled when the graft is applied),
        and conforming joints is handled separately in apply_transform_conform.

        Return a list of nodes that should be deleted to remove this wrap.
        """
        self.progress.set_main_progress('Applying conform wraps...')

        conformed_mesh_group = pm.createNode('transform', n='TempConformedMeshes')
        conformed_mesh_group.attr('visibility').set(0)

        if not config.get('conform_meshes'):
            return conformed_mesh_group, []

        # Copy off each conformed mesh, and wrap the copy to the target.  We'll use these when applying
        # morphs to create corresponding morphs in the conformed mesh.  Note that although sometimes a
        # few meshes may have matching morphs (in which case this won't be used), it's not really worth
        # trying to avoid creating these wraps, since we'll usually still need it for other morphs, such
        # as corrective blend shapes.
        target_loaded_mesh = target_mesh_set.combined_loaded_mesh

        conformed_loaded_mesh = conformed_mesh_set.combined_loaded_mesh

        message = 'Wrapping %s to %s' % (conformed_loaded_mesh, target_loaded_mesh.geom.parent.get_label())
        log.debug('------------ %s', message)
        self.progress.set_task_progress(message)

        wrapped_mesh = pm.duplicate(conformed_loaded_mesh.maya_mesh)[0]
        pm.parent(wrapped_mesh, conformed_mesh_group)

        # Remember which LoadedMesh this one's geometry targets.
        conformed_loaded_mesh.geometry_conform_wrapped_mesh = wrapped_mesh

        use_cvwrap = config.get('use_cvwrap')
        if not use_cvwrap and target_loaded_mesh.maya_mesh.vtx.count() >= 10000:
            log.info('Using cvwrap for %s because of its high vertex count: %i', target_loaded_mesh.maya_mesh, target_loaded_mesh.maya_mesh)
            use_cvwrap = True

        # Create the wrap deformer to do the main non-rigid conforming for this mesh to the mesh
        # it's conforming to.
        deformer_node = mh.wrap_deformer(target_loaded_mesh.maya_mesh, wrapped_mesh, max_distance=10, falloff_mode=0, use_cvwrap_if_available=use_cvwrap)
        pm.refresh()

        # Create rigidity transforms for this conformed mesh.
        reference_target_mesh = deformer_node.attr('input[0].inputGeometry')
        deformed_target_mesh = deformer_node.attr('outputGeometry[0]')

        # If mesh_set contains any rigidity groups, set up their rigid transforms.
        rigidity_transforms = []
        if 'rigidity/groups' in conformed_loaded_mesh.geom:
            for rigidity_group in conformed_loaded_mesh.geom['rigidity/groups']:
                try:
                    rigidity_transform = RigidityTransform(rigidity_group, conformed_loaded_mesh, reference_target_mesh, deformed_target_mesh)
                except MeshUnaffected:
                    continue

                rigidity_transforms.append(rigidity_transform)

        # Use a blend shape to make the mesh follow the rigidity transforms.
        blend_shape = mh.BlendShapeDeformer([wrapped_mesh], 'BS_Rigid_%s' % conformed_loaded_mesh.name, origin='world')
        for rigidity_transform in rigidity_transforms:
            log.debug('Transforms on rigidity group %s: %s', rigidity_transform, rigidity_transform.target_transforms)

            # Make a copy of the mesh for this rigidity group.  We need to copy the original
            # geometry, not the currently deformed geometry, so this copy doesn't include the
            # output of the wrap.  Connect a mesh to the input of the blendShape to get this.
            transform = pm.createNode('transform', n='RigidFollow_' + rigidity_transform.name, p=conformed_mesh_group)
            rigid_mesh = pm.createNode('mesh', p=transform)
            rigidity_transform.reference_mesh.connect(rigid_mesh.attr('inMesh'))

            # Constrain the copy to the rigidity transform.
            parent_constraint = pm.parentConstraint(rigidity_transform.position_node, transform, mo=True)
            scale_constraint = pm.scaleConstraint(rigidity_transform.position_node, transform, mo=True)

            # Save the weight attributes for the constraints.  If these are disabled, the faces in this rigidity
            # group will go back to their rest position.  This is used by type_filters.
            weights = (parent_constraint.attr('target[0].targetWeight'), scale_constraint.attr('target[0].targetWeight'))
            rigidity_transform.constraint_weights = [weight.connections(p=True, s=True, d=False)[0] for weight in weights]

            # Add the copy as a blend shape target, set its weights, and enable the target.
            morph_info = blend_shape.add_blend_shape(wrapped_mesh, rigid_mesh, rigidity_transform.name)
            blend_shape.set_blend_shape_weights(morph_info, wrapped_mesh, rigidity_transform.rigidity_weights)
            morph_info.weight_maya_attr.set(1)

        return conformed_mesh_group, rigidity_transforms

    def save_rigid_transform_matrix(self, rigidity_transforms, enable_morphs):
        """
        Rigidity groups can affect transforms.  Save the effect of the given rigidity groups to
        self.rigidity_transform_matrices.
        
        enable_morphs is a dictionary of Maya properties and values to set to enable the blend
        shape driving the rigidity group.
        """
        # Make sure all blend shapes are turned off.
        for maya_attr, value in enable_morphs.iteritems():
            maya_attr.set(0)

        joint_to_rigidity_transform_target = {}
        for rigidity_transform in rigidity_transforms:
            position_node = rigidity_transform.position_node

            for transform_node in rigidity_transform.target_transforms:
                joint_to_rigidity_transform_target.setdefault(transform_node.maya_node, []).append(position_node)

                # If this transform has an end joint, add that too.
                end_joint_maya_node = getattr(transform_node, 'end_joint_maya_node', None)
                if end_joint_maya_node is not None:
                    joint_to_rigidity_transform_target.setdefault(end_joint_maya_node, []).append(position_node)

        joint_position_nodes = {}
        scale_nodes = {}

        initial_translations = {}
        initial_rotations = {}
        for transform_node, position_nodes in joint_to_rigidity_transform_target.iteritems():
            # Create a transform at the same position as each transform_node.  We'll constrain this to the rigid
            # transform and read the change, so we don't change the actual transform yet.
            joint_position_node = pm.createNode('transform', n='TempRigidPosition_%s' % transform_node.nodeName())
            pm.parent(joint_position_node, transform_node, r=True)
            pm.parent(joint_position_node, None, r=False)
            joint_position_nodes[transform_node] = joint_position_node

            # Parent constrain joint_position_node to all of the rigid transforms that affect it, so it'll
            # follow their average.
            args = position_nodes + [joint_position_node]
            pm.parentConstraint(*args, mo=True)

            # We don't want the joint positions to have scale applied, since they should just move around
            # but stay at 1:1 scale.  Create a separate node to constrain scale.  We'll only use this to
            # apply scale to joint radius, not the transform.
            scale_node = pm.createNode('transform', n='TempRigidScale_%s' % transform_node.nodeName(), p=joint_position_node)
            scale_nodes[transform_node] = scale_node

            args = position_nodes + [scale_node]
            pm.scaleConstraint(*args, mo=True)

            # Record the initial translation and rotation.  We'll subtract these out to get the delta.
            initial_translations[transform_node] = joint_position_node.getTranslation(ws=True)
            initial_rotations[transform_node] = joint_position_node.getRotation().asQuaternion()
            self.rigidity_translation[transform_node] = [0,0,0]
            self.rigidity_rotation[transform_node] = dt.Quaternion()

        # We can't just turn on all of the blend shapes and look at the result, since when the blend shapes
        # and resulting wraps are actually applied, they'll be applied separately and then added together
        # as blend shapes.  We need to do the same: turn each blend shape on one by one, and add the resulting
        # changes to the transform.  This is because applying two blend shapes doesn't have a linear, additive
        # result on the mesh wrapped *to* that blend shape.
        # Enable blend shapes that we'll conform transforms to.  This will move the position_nodes.
        for maya_attr, value in enable_morphs.iteritems():
            if abs(value) < 0.0001:
                continue

            maya_attr.set(value)

            # Save the change to the world matrix of each positioning transform.
            for transform_node in joint_to_rigidity_transform_target.iterkeys():
                joint_position_node = joint_position_nodes[transform_node]

                new_translation = joint_position_node.getTranslation(ws=True)
                translation_delta = new_translation - initial_translations[transform_node]
                self.rigidity_translation[transform_node] += translation_delta

                new_rotation = joint_position_node.getRotation().asQuaternion()
                rotation_delta = new_rotation * initial_rotations[transform_node].inverse()
                self.rigidity_rotation[transform_node] = rotation_delta * self.rigidity_rotation[transform_node]

                # log.debug('%s (%s): initial %s, now %s', transform_node, joint_position_node, inverse_matrix, world_matrix)

                # We're only collecting transforms at this point and not applying them, but apply the scale
                # to the joint radius now.  It won't affect anything else and it's easier to do it here, so
                # we don't have to carry the scale around.
                if transform_node.type() == 'joint':
                    scale_node = scale_nodes[transform_node]
                    scale = scale_node.attr('scaleX').get()
                    radius = transform_node.attr('radius').get() * scale
                    transform_node.attr('radius').set(radius)

            # Turn the blend shape back off.
            maya_attr.set(0)

    def apply_morphs(self):
        # Apply morphs and conforming.  Do this before applying skinning.
        self.progress.set_main_progress('Applying morphs...')

        if not config.get('morphs'):
            return

        blend_shape_per_combined_mesh = {}

        # Create a mesh from mesh_set.combined_loaded_mesh, which is the complete mesh not broken
        # out by material, and apply blend shapes to those first.  Conforming will be done on these
        # meshes.
        # XXX: need to create meshes that are targets for conform, even if they don't have morphs themselves
        for mesh_set in self.all_mesh_sets.itervalues():
            if not hasattr(mesh_set, 'combined_loaded_mesh'):
                continue

            log.debug('Process morphs for: %s', mesh_set)

            # Note that we don't cull geometry here.  This is only temporary geometry that will be
            # deleted, so by not culling we don't need to do vertex index lookups.
            combined_mesh = mesh_set.combined_loaded_mesh.create_mesh()
            mesh_set.combined_loaded_mesh.maya_mesh = combined_mesh

            # Create a BlendShapeDeformer to manage blend shapes on this object.  Note that this won't
            # actually create a blendShape node until we have targets to add.
            blend_shape = mh.BlendShapeDeformer([combined_mesh], 'BS_Temp_%s' % mesh_set.name, origin='world')
            blend_shape_per_combined_mesh[mesh_set] = blend_shape

            # Create blend shapes for this LoadedMesh.  Morphs are usually children of the geometry, but they
            # can be children of the transform node too (eg. PBMMeiLin).
            for dson_morph in mesh_set.dson_geometry.parent._get_nodes_within_figure():
                if dson_morph.node_source != 'modifier':
                    continue
            
                morph = dson_morph.get_property('morph', default=None)
                if morph is None:
                    continue

                morph_info = self._create_morph_for_mesh(blend_shape, mesh_set.combined_loaded_mesh, dson_morph)
                if morph_info is None:
                    continue

                # Remember which DSON morph this came from.
                morph_info.dson_morph = dson_morph

        # Turn on morphs that are enabled by modifiers that are enabled by default.  This will
        # turn on base clothing morphs.  For example, if a clothing mesh has a modifier to fit it
        # to the base character it's conforming to, this will turn that on.  We need to do this
        # before creating conform blend shapes, so if we're conforming from the base mesh to a
        # character morph, the clothing mesh will start out fitted to the base mesh.  Don't turn
        # on all morphs (which would happen if use_default_values was false), since that would
        # turn on the character morph too.
        #
        # By turning these on now, they'll be included in original_vertex_data, so the blend shapes
        # we create will be relative to the fitted version, rather than to the clothing mesh's base
        # shape.
        #
        # use_default_values will cause morphs that auto_follow the figure's FID_ channel to be turned
        # on (which have a default value of 1), without turning on character morphs, which have a
        # default value of 0.
        for blend_shape in blend_shape_per_combined_mesh.itervalues():
            for morph_info in blend_shape.morphs:
                value_property = morph_info.dson_morph['value']   
                value = value_property.evaluate(use_default_values=True)
                if value == 0:
                    continue

                log.debug('Enable blend shape before conform: %s on %s (%s)', value_property, value_property.node, value)
                morph_info.weight_maya_attr.set(value)

        # Create conform blend shapes.  For each conformed mesh, turn on each blend shape on its target, and
        # create a blend shape from its wrapped mesh.  Note that if we want to support recursive conform, with
        # a mesh conforming to a mesh conforming to another mesh, we'd need to do this recursively, but I don't
        # know of any case of that.
        for mesh_set in self._get_mesh_sets_in_conform_dependency_order(self.all_mesh_sets):
            _, conforming_to_mesh_set = self.get_conforming_to_geometry(mesh_set.dson_geometry.parent)
            if conforming_to_mesh_set is None:
                continue

            # Create conform wraps and rigidity groups for this MeshSet.
            log.debug('Creating conform shapes for: %s', mesh_set)
            conform_node, rigidity_transforms = self.create_conform_wraps(mesh_set, conforming_to_mesh_set)

            blend_shape = blend_shape_per_combined_mesh[mesh_set]

            # Find blend shapes on this mesh's conform target.  This will always be created first, since we're
            # doing this in dependency order.
            target_blend_shape = blend_shape_per_combined_mesh[conforming_to_mesh_set]

            # Make a copy of the original vertex positions of each mesh.  We'll compare the conformed
            # mesh to this to get the blend shape deltas.
            original_vertex_data = cmds.getAttr('%s.vt[*]' % mesh_set.combined_loaded_mesh.maya_mesh)

            blend_shapes_to_create = []

            for morph_shape_info in target_blend_shape.morphs:
                # Don't create conform morphs for modifiers that have auto_follow set to false.
                if not morph_shape_info.dson_morph.get_value('channel/auto_follow'):
                    continue

                dson_morph = morph_shape_info.dson_morph

                # If a mesh has a morph, and a conforming mesh has a morph with the same name, it's
                # a baked morph.  We don't need to use our wrap deformer for this one, we can just
                # use the existing morph.
                morph_info = blend_shape.get_blend_shape_info_by_name(dson_morph.get_value('name'))
                if morph_info is not None:
                    if not morph_info.dson_morph.get_value('channel/auto_follow'):
                        # Actually, if there's a matching morph but auto_follow is true, the morph doesn't
                        # follow at all.  I'm not sure if this is important.
                        morph_info = None

                if morph_info is not None:
                    # We have a baked matching morph, so we don't have to create our own.
                    log.debug('Using existing conform morph for %s on %s', dson_morph.get_value('name'), mesh_set)
                    morph_info.conform_to_blend_shape = morph_shape_info
                    continue

                log.debug('Creating conform morph for %s on %s', dson_morph.get_value('name'), mesh_set)

                # We don't have a morph to conform this mesh to the target mesh's morph.  Use our wrap
                # deformer to create one.  Turn this blend shape on.
                old_morph_weight = morph_shape_info.weight_maya_attr.get()
                morph_shape_info.weight_maya_attr.set(1)

                # If a rigidity group has a type_filter list, it's a list of modifier types (eg. "Modifier/Shape")
                # that the rigidity group shouldn't move with.  For example, a hair morph may include Modifier/Corrective
                # to prevent that morph's rigid parts from moving with corrective blend shapes on the target shape.
                # Check each rigidity group, and disable the constraint for the rigidity group target if it's filtered
                # for this morph.  The transform will go back to the rest position.
                morph_type = dson_morph.get_value('presentation/type')
                for rigidity_transform in rigidity_transforms:
                    type_filters = rigidity_transform.rigidity_group.get_value('type_filters', [])

                    enable_rigid_transform = morph_type not in type_filters
                    for weight_attr in rigidity_transform.constraint_weights:
                        weight_attr.set(1 if enable_rigid_transform else 0)

                try:
                    # This mesh is following the blend shape that we have enabled:
                    wrap_deformed_mesh = mesh_set.combined_loaded_mesh.geometry_conform_wrapped_mesh
                    # log.debug('Reading deformed mesh: %s for %s', wrap_deformed_mesh, mesh_set.combined_loaded_mesh)
                    new_vertex_data = cmds.getAttr('%s.vt[*]' % mesh_set.combined_loaded_mesh.geometry_conform_wrapped_mesh)

                    # log.debug('Adding %s to blend shape on recipient: %s', mesh_set.combined_loaded_mesh.geometry_conform_wrapped_mesh, mesh_set.combined_loaded_mesh.maya_mesh)

                    # The vertex indices we get back should always match up with the mesh we created.
                    assert len(new_vertex_data) == len(original_vertex_data), (new_vertex_data, original_vertex_data)
                
                    # Calculate the deltas for this blend shape.
                    deltas = []
                    delta_vertex_indices = []
                    for idx in xrange(len(new_vertex_data)):
                        delta = subtract_vector3(new_vertex_data[idx], original_vertex_data[idx])
                        if vector_length(delta) < 0.001:
                            continue

                        deltas.append((delta[0], delta[1], delta[2]))
                        delta_vertex_indices.append(idx)

                    # See if this blend shape had any effect on the wrapped mesh by comparing the wrapped
                    # mesh to the original one.  If it didn't, we don't need to create a blend shape on the
                    # conformed mesh.  For example, shoulder corrective shapes won't affect conforming shoes.
                    if not deltas:
                        log.debug('Conformed mesh %s is unaffected by target morph %s', mesh_set.combined_loaded_mesh, dson_morph)
                        continue

                    # Add this mesh as a blend shape on the conformed geometry.  If loaded_mesh is a graft, this
                    # is adding to a blend shape that already exists, and this will only overwrite the deltas that
                    # are present in delta_vertex_indices, leaving the existing morph for the rest of the shape
                    # unchanged.
                    morph_name = dson_morph.get_value('name')
                    blend_shapes_to_create.append((dson_morph, mesh_set.combined_loaded_mesh.maya_mesh, deltas, delta_vertex_indices, morph_name))
                finally:
                    # Turn this blend shape back off.
                    morph_shape_info.weight_maya_attr.set(old_morph_weight)

                # Reenable all rigidity transforms.
                for rigidity_transform in rigidity_transforms:
                    for weight_attr in rigidity_transform.constraint_weights:
                        weight_attr.set(1)

            # Save rigidity group transforms for transforms.  We don't apply this now, but we save it now
            # while the rigidity conform still exists.
            enable_morphs = {morph_shape_info.weight_maya_attr: morph_shape_info.dson_morph['value'].evaluate()
                    for morph_shape_info in target_blend_shape.morphs}
            self.save_rigid_transform_matrix(rigidity_transforms, enable_morphs)

            # Delete conforming before creating blend shapes.  We're finished with it, and if we don't delete
            # this first, wrap deformers will spend a lot of time reevaluating when we modify the blend shape.
            pm.delete(conform_node)
            for rigidity_transform in rigidity_transforms:
                rigidity_transform.delete()

            # Create the conform blend shapes.
            log.debug('Creating %i blend shapes on %s', len(blend_shapes_to_create), blend_shape)
            for args in blend_shapes_to_create:
                dson_morph = args[0]
                morph_info = blend_shape.add_blend_shape_from_deltas(*args[1:])
                morph_info.dson_morph = dson_morph

            for morph_shape_info in target_blend_shape.morphs:
                dson_morph = morph_shape_info.dson_morph
                morph_info = blend_shape.get_blend_shape_info_by_name(dson_morph.get_value('name'))
                if morph_info is not None:
                    # Connect this shape's weight to the target weight.
                    morph_info.conform_to_blend_shape = morph_shape_info

        # We now have each mesh created, with its blend shapes and conform blend shapes created.
        # Now apply these blend shapes to the real meshes.  This will merge blend shapes from each
        # graft piece, and then split them apart into the submeshes, which are split by material.
        # We also bake non-dynamic blend shapes to the mesh.
        source_morph_info_for_retargeted_morph = {}
        for mesh_set in self._get_mesh_sets_in_conform_dependency_order(self.all_mesh_sets):
            self.retarget_combined_morphs(mesh_set, blend_shape_per_combined_mesh, source_morph_info_for_retargeted_morph)

        # Delete the combined meshes that we created earlier.  We only used those to apply morphs
        # and conforming.
        for mesh_set in self.all_mesh_sets.itervalues():
            loaded_mesh = getattr(mesh_set, 'combined_loaded_mesh', None)
            if loaded_mesh is not None and loaded_mesh.maya_mesh is not None:
                pm.delete(loaded_mesh.maya_mesh.getParent())
                mesh_set.combined_loaded_mesh = None

        # Delete the grafted meshes, and remove them from all_mesh_sets.
        for geom, mesh_set in self.all_mesh_sets.items():
            if not mesh_set.dson_geometry.is_graft:
                continue

            for loaded_mesh in mesh_set.meshes.itervalues():
                if loaded_mesh.maya_mesh is not None:
                    pm.delete(loaded_mesh.maya_mesh)

            # Remove the MeshSets that we grafted.
            del self.all_mesh_sets[geom]

    def retarget_combined_morphs(self, mesh_set, blend_shape_per_combined_mesh, source_morph_info_for_retargeted_morph):
        if mesh_set not in blend_shape_per_combined_mesh:
            # This mesh doesn't have any blend shapes, so we don't need to recreate it.
            # XXX: need to check graft_meshes too; this optimization may not be needed now
            return

        if mesh_set.dson_geometry.is_graft:
            return

        log.debug('Retargeting to %s', mesh_set)

        # Make a list of the combined (not split by material) LoadedMeshes that have been grafted into any
        # submesh, including the main mesh itself.
        all_source_meshes = set()
        all_source_meshes.add(mesh_set.combined_loaded_mesh)
        for loaded_mesh in mesh_set.meshes.itervalues():
            all_source_meshes.update(mesh.mesh_set.combined_loaded_mesh for mesh in loaded_mesh.grafted_meshes)

        # Read all blend shapes from each mesh contributing to this final combined mesh.
        all_morphs = {}
        for source_mesh in all_source_meshes:
            source_mesh_blend_shape = blend_shape_per_combined_mesh[source_mesh.mesh_set]
            source_morph_infos = source_mesh_blend_shape.morphs
            for source_morph_info in source_morph_infos:
                # log.debug('Reading morph from: %s', source_morph_info)
                deltas, delta_vertex_idx = source_mesh_blend_shape.get_blend_shape_deltas(source_mesh.maya_mesh, source_morph_info.name)
                all_morphs[source_morph_info] = (deltas, delta_vertex_idx)

        blend_shape = mh.BlendShapeDeformer([loaded_mesh.maya_mesh for loaded_mesh in mesh_set.meshes.itervalues()], 'BS_%s' % mesh_set.name, origin='world')

        # For each submesh, create each blend shape from each source mesh.
        for loaded_mesh in mesh_set.meshes.itervalues():
            
            # Make a smaller list of the combined LoadedMeshes that have been combined into this submesh.  For
            # character figures, many morphs won't affect many submeshes.
            graft_meshes = {mesh.mesh_set.combined_loaded_mesh for mesh in loaded_mesh.grafted_meshes}
            graft_meshes.add(mesh_set.combined_loaded_mesh)

            log.debug('Merging into %s: %s', loaded_mesh, ', '.join(str(s) for s in graft_meshes))

            blend_shape_deltas = {}

            # Gather the morphs on each mesh that's been combined.
            for graft_mesh in graft_meshes:
                log.debug('Retarget mesh %s (%s) onto %s', graft_mesh, graft_mesh.mesh_set, loaded_mesh)

                # Map the old VertexMapping to the new one to map the original vertex indices to the new ones,
                # after grafting and culling.
                graft_orig_to_new_vertex_mapping = mesh_set.combined_loaded_mesh.submesh_info[mesh_set.combined_loaded_mesh.geom]['orig_to_new_vertex_mappings']
                target_orig_to_new_vertex_mapping = loaded_mesh.submesh_info[graft_mesh.geom]['orig_to_new_vertex_mappings']
                vertex_mapping = util.VertexMapping.map_destination_indices(graft_orig_to_new_vertex_mapping, target_orig_to_new_vertex_mapping)

                graft_morph_infos = blend_shape_per_combined_mesh[graft_mesh.mesh_set].morphs
                for graft_morph_info in graft_morph_infos:
                    assert hasattr(graft_morph_info, 'dson_morph'), (blend_shape_per_combined_mesh[graft_mesh.mesh_set], graft_morph_info)

                    # log.debug('Reading morph on graft: %s', graft_morph_info)
                    deltas, delta_vertex_idx = all_morphs[graft_morph_info]
                    assert deltas is not None

                    # Map the deltas in this blend shape from the graft to the target that they're grafted onto.
                    remapped_delta_vertex_idx, remapped_deltas = vertex_mapping.remap_indexed_data(delta_vertex_idx, deltas)
                    if not remapped_deltas:
                        # This morph doesn't apply to this sub-mesh.
                        continue

                    # If set, conform_to_blend_shape is the morph_info that this blend shape is conforming to.  If this is
                    # a graft, add this morph to the graft target's morph instead of this one, so we merge it.
                    merge_with_morph_info = getattr(graft_morph_info, 'conform_to_blend_shape', None)
                    if merge_with_morph_info is not None and graft_mesh.geom.is_graft:
                        log.debug('conform: %s -> %s', merge_with_morph_info, graft_morph_info)
                        graft_morph_info = merge_with_morph_info

                    blend_shape_deltas.setdefault(graft_morph_info, []).append((remapped_delta_vertex_idx, remapped_deltas))

            # Each entry in blend_shape_deltas is a list of (delta_vertex_idx, deltas) for that morph, with
            # each entry being deltas from a different graft.  Add them together, giving us a single blend
            # shape.
            for morph_info, shapes in blend_shape_deltas.items():
                delta_vertex_idx, deltas = util.blend_merge_shape_deltas([(delta_vertex_idx, deltas) for delta_vertex_idx, deltas in shapes])
                blend_shape_deltas[morph_info] = delta_vertex_idx, deltas

            # Bake static morphs.  If a morph's property isn't dynamic, bake the morph into the mesh and delete
            # the morph.  This avoids needing to encode the whole character morph into the blend shape, and allows
            # disabling the blendShape deformer and soloing blend shapes without turning the main morph off and
            # breaking everything.
            verts = None
            for morph_info, (delta_vertex_idx, deltas) in blend_shape_deltas.items():
                if not config.get('bake_static_morphs'):
                    continue

                value_property = morph_info.dson_morph['value']
                if value_property.is_dynamic:
                    # log.debug('Not baking dynamic morph %s (%s)', morph_info, value_property)
                    continue

                # Remove the blend shape record for the shape that we're baking.
                del blend_shape_deltas[morph_info]

                value = value_property.evaluate()
                log.debug('Baking static morph %s', morph_info)

                if value == 0:
                    continue

                # Only read the vertices back from the mesh if we actually have something to bake.
                if verts is None:
                    meshFn = om.MFnMesh(loaded_mesh.maya_mesh.__apimobject__())
                    verts = om.MPointArray()
                    meshFn.getPoints(verts, om.MSpace.kObject)
                    
                for vertex_idx, delta in zip(delta_vertex_idx, deltas):
                    delta = (delta[0] * value, delta[1] * value, delta[2] * value)
                    vertex = verts[vertex_idx]
                    vertex = om.MPoint(vertex.x + delta[0], vertex.y + delta[1], vertex.z + delta[2], vertex.w)
                    verts.set(vertex, vertex_idx)

            # If we baked any morphs, write the new vertices to the mesh.
            if verts is not None:
                meshFn.setPoints(verts, om.MSpace.kObject)

            # Sort JCM morphs to the bottom.
            def get_morph_sort_key((morph_info, shape)):
                sort_group = 0
                if morph_info.name.startswith('JCM_') or morph_info.name.startswith('MCM_'):
                    sort_group = 10
                return (sort_group, morph_info.name.lower())

            # Create the blend shape deformer on the split, grafted meshes.
            for graft_morph_info, (delta_vertex_idx, deltas) in sorted(blend_shape_deltas.items(), key=get_morph_sort_key):
                is_new = graft_morph_info.name not in blend_shape.morph_names

                morph_info = blend_shape.add_blend_shape_from_deltas(loaded_mesh.maya_mesh, deltas, delta_vertex_idx, graft_morph_info.name)
                morph_info.dson_morph = graft_morph_info.dson_morph

                if not is_new:
                    # We've already connected this blend shape.
                    continue

                value_property = morph_info.dson_morph['value']

                merge_with_morph_info = getattr(graft_morph_info, 'conform_to_blend_shape', None)
                # log.debug('%s conforms to %s', graft_morph_info, merge_with_morph_info)
                if merge_with_morph_info is not None and not loaded_mesh.mesh_set.dson_geometry.is_graft:
                    if merge_with_morph_info in source_morph_info_for_retargeted_morph:
                        # This is a conformed mesh.  Connect the blend shape weight to the weight of the shape
                        # it's following.  That shape is the one that will be registered as a property.
                        target_morph_info = source_morph_info_for_retargeted_morph[merge_with_morph_info]
                        target_morph_info.weight_maya_attr.connect(morph_info.weight_maya_attr)
                    else:
                        # We're conforming to another morph, but the morph doesn't exist, probably because
                        # it's been baked.  We should probably just bake morphs if they're dynamic but are
                        # conforming to a non-dynamic morph.
                        log.debug('Setting property %s, because it\'s conforming to a morph that has been baked', value_property)
                        morph_info.weight_maya_attr.set(value_property.evaluate())
                else:
                    mayadson.set_attr_to_prop(value_property, morph_info.weight_maya_attr)

                # Save the morph that this morph was retargeted from.
                source_morph_info_for_retargeted_morph[graft_morph_info] = morph_info

    @classmethod
    def _graft_geometry(cls, source_mesh, target_mesh):
        target_mesh.grafted_meshes.append(source_mesh)

        graft = source_mesh.geom['graft']

        # Check that the UV sets in the graft are distinct from the ones in the targets.  This
        # doesn't seem useful, since it could only happen if you were grafting a mesh onto a
        # mesh with the same topology (eg. itself).
        for uv_set in target_mesh.uv_sets.values():
            if uv_set.dson_uv_set in source_mesh.uv_sets:
                raise RuntimeError('Grafted geometry %s uses an overlapping UV set with the target geometry %s' % (source_mesh, target_mesh))

        # Check that the vertex count of the mesh matches what the graft expects.
        # Compare against the original count, since this won't match if this isn't
        # the only graft.
        assert graft['vertex_count'].value == target_mesh.geom['vertices/count'].value

        vertex_pairs = graft['vertex_pairs/values'].value
        hidden_polys = set(graft['hidden_polys/values'].value)

        # Add this mesh's vertices to the target.
        src_to_target_vertex_splices = {v[0]: v[1] for v in vertex_pairs}
        src_to_target_vertex_mapping = {}
        source_mesh_orig_to_new = source_mesh.submesh_info[source_mesh.geom]['orig_to_new_vertex_mappings']
        source_mesh_new_to_orig = source_mesh_orig_to_new.invert()
        target_mesh_orig_to_new = target_mesh.submesh_info[target_mesh.geom]['orig_to_new_vertex_mappings']
        target_mesh_new_to_orig = target_mesh_orig_to_new.invert()
        for src_vertex_idx, src_vert in enumerate(source_mesh.verts):
            # src_to_target_vertex_splices maps original vertex indices, so map back to the source.
            orig_src_vertex_idx = source_mesh_new_to_orig[src_vertex_idx]

            orig_dst_vertex_idx = src_to_target_vertex_splices.get(orig_src_vertex_idx)
            if orig_dst_vertex_idx is not None:
                # This is a spliced vertex.  If the vertex index isn't in this map, it probably means that
                # the vertex was grafted or culled away for some reason, which we don't expect to happen.
                dst_vertex_idx = target_mesh_orig_to_new[orig_dst_vertex_idx]
                src_to_target_vertex_mapping[src_vertex_idx] = dst_vertex_idx
                continue

            # Add this vertex to the mesh.
            src_to_target_vertex_mapping[src_vertex_idx] = len(target_mesh.verts)
            target_mesh.verts.append(src_vert)

        # This mesh will be merged into the target mesh.  Add an entry in submesh_info, so
        # we can remember the original vertices from this mesh after we drop it.
        new_mapping = {}
        for orig_src_vertex_idx, new_src_vertex_idx in source_mesh_orig_to_new.items():
            if orig_src_vertex_idx in src_to_target_vertex_splices:
                # This vertex was spliced into the target mesh.  Remove it from orig_to_new_vertex_mappings,
                # since it doesn't exist anymore.
                continue

            new_dst_vertex_idx = src_to_target_vertex_mapping[new_src_vertex_idx]
            new_mapping[orig_src_vertex_idx] = new_dst_vertex_idx

        target_mesh.submesh_info[source_mesh.geom] = {
            'orig_to_new_vertex_mappings': new_mapping,
        }

        # Remove polys from the target geometry that are in the hidden_polys list.
        # Ignore submeshes that are other grafts.  We're only filtering polys from the
        # original geometry.  If we didn't check this, the source_poly_idx comparison
        # below could match the same poly index from a different graft and remove the
        # wrong geometry.
        updated_polys = []
        
        # Create a new UV set dictionary, containing both the target and graft's UV sets.
        # We keep the UV sets separate instead of merging them, since both meshes can have
        # multiple, distinct UV sets from materials.
        new_uv_sets = copy.deepcopy(target_mesh.uv_sets) 
        new_uv_sets.update(copy.deepcopy(source_mesh.uv_sets))

        # Erase the UV indices from the new UV sets.  We'll repopulate them as we add faces.
        for uv_set in new_uv_sets.values():
            uv_set.uv_indices_per_poly = []

        # Add the mesh's old polys, with hidden faces removed.  As we add faces, also add face
        # data to each UV set.
        for poly_idx, poly in enumerate(target_mesh.polys):
            poly = copy.deepcopy(poly)
            
            # source_poly_idx is this poly's index before we made changes to it.  This way,
            # we still remove the correct polys even if we have more than one graft.
            if poly['dson_geometry'] is target_mesh.geom and poly['source_poly_idx'] in hidden_polys:
                continue

            # Add this original face to the new list.
            updated_polys.append(poly)

            for dson_uv_set, uv_set in new_uv_sets.items():
                uv_indices = []
                if dson_uv_set in target_mesh.uv_sets:
                    # This is a UV set from the target geometry.  Copy over the original UV indices.
                    original_uv_set = target_mesh.uv_sets[dson_uv_set]
                    uv_indices = original_uv_set.uv_indices_per_poly[poly_idx]
                else:
                    # If this UV set wasn't in the target, then it's a new one from the graft.  There are
                    # no UVs for these faces in the target's UV sets, so just add 0s.
                    uv_indices = []
                    for uv_idx in range(len(poly['vertex_indices'])):
                        uv_indices.append(len(uv_set.uv_values))
                        uv_set.uv_values.append((0,0))

                assert len(poly['vertex_indices']) == len(uv_indices)
                uv_set.uv_indices_per_poly.append(uv_indices)

        # Add the polys from the graft geometry.
        for poly_idx, poly in enumerate(source_mesh._polys):
            poly = copy.deepcopy(poly)

            # Remap the grafted geometry's vertex and UVs to the target.  This will point the vertex
            # indices to the corresponding vertices in the target geometry.
            poly['vertex_indices'] = [src_to_target_vertex_mapping[idx] for idx in poly['vertex_indices']]

            updated_polys.append(poly)

            for dson_uv_set, uv_set in new_uv_sets.items():
                uv_indices = []
                if dson_uv_set in source_mesh.uv_sets:
                    # This is a UV set from the graft geometry.  Copy over the original UV indices.
                    original_uv_set = source_mesh.uv_sets[dson_uv_set]
                    uv_indices = original_uv_set.uv_indices_per_poly[poly_idx]
                else:
                    # If this UV set isn't in the graft, then it's a new one from the target mesh.
                    # There are no UVs for these faces in the graft's UV sets, so just add 0s.
                    uv_indices = []
                    for uv_idx in range(len(poly['vertex_indices'])):
                        uv_indices.append(len(uv_set.uv_values))
                        uv_set.uv_values.append((0,0))

                assert len(poly['vertex_indices']) == len(uv_indices)
                uv_set.uv_indices_per_poly.append(uv_indices)

        # Add the graft's material list to the target.
        target_mesh.materials.update(source_mesh.materials)

        target_mesh.polys = updated_polys
        target_mesh.uv_sets = new_uv_sets
        target_mesh.dson_material_to_dson_uv_set.update(source_mesh.dson_material_to_dson_uv_set)

    def _create_morph_for_mesh(self, blend_shape, loaded_mesh, dson_morph):
        # If a morph isn't dynamic, then we're only setting it up for the value stored in the scene.
        # If a morph isn't dynamic and its value is zero, then we should exclude it entirely.  This
        # avoids creating lots of morphs for characters which have been imported but aren't enabled.
        value_property = None

        # If this is an auto-follow property, find the property that we're following to use for the
        # checks below.  If we're following another property, we need to create this morph if that
        # property is used, even if this one wouldn't be without the auto-follow.
        if dson_morph.get_value('channel/auto_follow'):
            conformed_mesh_set, target_mesh_set = self.get_conforming_to_geometry(loaded_mesh.geom.parent)
            if conformed_mesh_set is not None:
                following = target_mesh_set.dson_geometry.parent.find_asset_name(dson_morph.get_value('name'), default=None)
                if following:
                    value_property = following['value']

        if value_property is None:
            value_property = dson_morph['value']

        if not value_property.is_dynamic and value_property.evaluate() == 0:
            log.debug('Skipping morph because it\'s zero and not dynamic: %s', dson_morph)
            return None

        # Corrective morphs for character morphs have their value multiplied by the main character morph,
        # so they turn off when the character turns off.  If the character is turned off and not dynamic,
        # then all of the corrective morphs for that character will also always be zero.  This avoids
        # creating corrective blend shapes for characters that are loaded but not enabled.
        if modifiers.DSONFormula.property_is_always_zero(value_property):
            log.debug('Skipping morph because it\'s always zero: %s', dson_morph)
            return None

        # Apply this morph.  We may have more than one list of deltas, for each geometry
        # which has been grafted into this one.
        deltas = []
        delta_vertex_idx = []

        # Find deltas that apply to this LoadedMesh.
        submesh_info = loaded_mesh.submesh_info[loaded_mesh.geom]
        all_deltas = dson_morph['morph/deltas/values'].value
        for src_vertex_idx, x, y, z in all_deltas:
            dst_vertex_idx = submesh_info['orig_to_new_vertex_mappings'].get(src_vertex_idx)
            if dst_vertex_idx is None:
                continue

            delta_vertex_idx.append(dst_vertex_idx)
            deltas.append((x,y,z))

        if not deltas:
            # This morph doesn't affect this mesh.
            return None

        log.debug('    Apply morph %s', dson_morph)

        return blend_shape.add_blend_shape_from_deltas(loaded_mesh.maya_mesh, deltas, delta_vertex_idx, dson_morph.get_label())

    def apply_skinning(self):
        self.progress.set_main_progress('Applying skinning...')
        if not config.get('skinning'):
            return

        meshes_to_skin = [loaded_mesh for mesh_set in self.all_mesh_sets.values() for loaded_mesh in mesh_set.meshes.values()]

        for idx, loaded_mesh in enumerate(meshes_to_skin):
            self.progress.set_task_progress('Skinning %s' % loaded_mesh.name, float(idx) / len(meshes_to_skin))
            self.create_skinning_for_mesh(loaded_mesh)

    def create_skinning_for_mesh(self, loaded_mesh):
        """
        Apply skin bindings to a loaded_mesh.
        """
        # Skip submeshes that have no Maya shape created.
        if not loaded_mesh.maya_mesh:
            return

        # Whether to use linear or DQ.  It would be more correct do to this on a per-skin basis
        # with weight blended, since we might be a graft from geometries using different skinning
        # methods, but in practice almost everything in DSON uses DQ and grafts are uncommon to
        # begin with, so just use the first one we see.
        skinning_method = -1
        dson_map_modes_to_skinning_method = {
            'Linear': 0,
            'DualQuat': 1,
        }

        # Each LoadedMesh can have more than one underlying geometry.  Combine the SkinBindings for
        # all geometries in this mesh.
        joints_to_apply = {}
        for dson_geometry_node in loaded_mesh.get_dson_geometries():
            dson_transform_node = dson_geometry_node.find_top_node()
        
            # Make a mapping of joints in this hierarchy by their asset.
            joints_by_asset = {}
            for node in DSON.Helpers.find_bones(dson_transform_node):
                joints_by_asset[node] = node
                if node.asset:
                    joints_by_asset[node.asset] = node

            # Find the SkinBindings on this geometry.
            for node in dson_geometry_node.children:
                if node.node_source != 'modifier':
                    continue
                if 'skin' not in node:
                    continue

                skin = node.get_property('skin')
                map_mode = node['extra-type/skin_settings/general_map_mode'].value
                skinning_method = dson_map_modes_to_skinning_method[map_mode]

                # Check that the expected vertex count on the skin matches the geometry.  Use vertices/count
                # rather than len(vertices/values), so we compare against the original size without grafts.
                assert skin['vertex_count'].value == dson_geometry_node['vertices/count'].value, '%i != %i' % (skin['vertex_count'].value, dson_geometry_node['vertices/count'].value)

                joints = skin['joints']

                for binding_joint in joints.array_children:
                    # Look up the joint this is for.
                    joint_asset = dson_transform_node.get_modifier_url(binding_joint['node'])

                    # If the skin binding is an asset, the node attribute in the joint points to an asset.
                    # Search our node instance for a child using that asset.  Only traverse into bones, or
                    # else we could traverse into a different figure using the same asset and find one of
                    # its joints.
                    joint = joints_by_asset[joint_asset]
                    assert binding_joint not in joints_to_apply
                    joints_to_apply[joint] = (binding_joint, dson_geometry_node)

            joint_bindings = []
            for dson_joint, (binding_joint, dson_geometry_node) in joints_to_apply.items():
                # An array of source vertex indices and their weights:
                node_weights = binding_joint['node_weights/values'].value
                assert binding_joint['node_weights/count'].value == len(node_weights)

                # Get the Maya node that we created for this joint.
                maya_node = dson_joint.maya_node

                weights = {}

                submesh_info = loaded_mesh.submesh_info[dson_geometry_node]

                weight_map = { orig_vertex_idx: weight for orig_vertex_idx, weight in node_weights }

                # The indices in joint['weights'] are to the original geometry.
                for src_vertex_idx, weight in weight_map.items():
                    # The vertex index in the weights array is from the original mesh.  Look up where
                    # that index is now.  It may no longer exist, if it was grafted onto another vertex.
                    dst_vertex_idx = submesh_info['orig_to_new_vertex_mappings'].get(src_vertex_idx)
                    if dst_vertex_idx is None:
                        continue
                    weights[dst_vertex_idx] = weight

                # Skip joints with no weights on this mesh.
                if not weights:
                    continue

                # Note that after merging meshes, we can have a single skinCluster with weights from separate SkinBindings
                # to nodes from different assets.
                joint_bindings.append({
                    'dson_joint': dson_joint,
                    'weights': weights,
                    'dson_geometry_node': dson_geometry_node,
                })
                # print 'transform', transform, joint, maya_node

        if not joint_bindings:
            # log.debug('No skins')
            return

        log.info('Creating skinning for mesh: %s' % loaded_mesh)

        # Disable inheritsTransform on the mesh.  The mesh is inside the root transform (which is
        # usually not skinned), so this prevents a double-transform if the root is moved.  We don't
        # do this on initial creation, since we do want the root to move the mesh for unskinned
        # meshes.
        pm.setAttr(loaded_mesh.maya_mesh.getParent().attr('inheritsTransform'), 0)

        # Clear the rotation on the mesh.  This was inherited when the mesh was first parented
        # to keep it world-space-aligned so it ignores the root joint's bone transforms, but
        # since we're disabling transforms for this mesh we need to clear it.
        pm.xform(loaded_mesh.maya_mesh.getParent(), ro=(0,0,0))

        # Create an initial skinCluster.  We'll replace the weights.
        bind_list = [joint['dson_joint'].maya_node for joint in joint_bindings]
        bind_list.append(loaded_mesh.maya_mesh)
        skin_cluster = pm.skinCluster(bind_list, toSelectedBones=True, weight=0)

        skin_cluster.attr('skinningMethod').set(skinning_method)

        influence_indices = range(len(joint_bindings))
        
        weights_array = om.MDoubleArray(len(loaded_mesh.maya_mesh.vtx)*len(joint_bindings))

        for joint in joint_bindings:
            weights = joint['weights']
            maya_joint = joint['dson_joint'].maya_node
            joint_idx = skin_cluster.indexForInfluenceObject(maya_joint)
            for dst_vertex_idx, weight in weights.items():
                weight_idx = dst_vertex_idx * len(joint_bindings) + joint_idx
                weights_array[weight_idx] = weight

        skin_cluster.setWeights(loaded_mesh.maya_mesh, influence_indices, weights_array, True)

        # Turn this on to make non-uniform scaling work with DQ skinning, which is needed by some modifiers.
        skin_cluster.attr('dqsSupportNonRigid').set(1)

        # For dqsSupportNonRigid to work, we need to connect the scale to dqsScale.  Decompose this from
        # the world space scale of the root joint.
        root_joint = loaded_mesh.geom.find_root_joint()
        if root_joint is not None:
            decompose = pm.createNode('decomposeMatrix')
            root_joint.maya_node.attr('worldMatrix[0]').connect(decompose.attr('inputMatrix'))
            decompose.attr('outputScale').connect(skin_cluster.attr('dqsScale'))

    # Map from rotation orders to Maya .rotateOrder values.
    _rotation_orders = {
            'XYZ': 0,
            'YZX': 1,
            'ZXY': 2,
            'XZY': 3,
            'YXZ': 4,
            'ZYX': 5,
    }

    def create_transform(self, dson_node):
        """
        Create a Maya transform for dson_node.

        Apply properties that affect the bind position of joints.  This includes center_point,
        end_point and orientation, but not translation, rotation or scale, which are post-skinning.
        Modifiers that affect these properties are usually related to full-body morphs, to
        align the skeleton with the morph (without actually changing the output), and not things
        like expression morphs, which are on the post-skinning properties and do change the output.
        """
        # log.debug('Creating transform for %s' % dson_node)

        # Create the real joint.
        maya_node = pm.createNode('joint')

        if any(DSON.Helpers.find_ancestor_bone_by_asset_id(dson_node, name) for name in ('lHand', 'rHand', 'lFoot', 'rFoot', 'head')):
            pm.setAttr(maya_node.attr('radius'), 0.5)

        pm.rename(maya_node, mh.cleanup_node_name(dson_node.get_label()))

        # If this isn't actually a joint, hide the bone.  We need a joint node for jointOrient.
        if dson_node.node_type != 'bone':
            pm.setAttr(maya_node.attr('drawStyle'), 2)

        # Quirk: If this is one of the parent bones for all of the face joints, set
        # it to not draw.  We'll still see the face joints themselves, but this keeps
        # it from drawing the useless and ugly joints from the center of the head to
        # each face joint.
        asset_id = dson_node.asset and dson_node.asset.node_id
        if dson_node.asset and asset_id in ('lowerFaceRig', 'upperFaceRig'):
            pm.setAttr(maya_node.attr('drawStyle'), 2)

        if config.get('hide_face_rig') and asset_id in ('lowerFaceRig', 'upperFaceRig', 'upperTeeth'):
            pm.setAttr(maya_node.attr('visibility'), 0)

        rotation_order = dson_node.get('rotation_order', 'XYZ')
        maya_node.attr('rotateOrder').set(self._rotation_orders[rotation_order.value])

        # Keep track of the transform.
        dson_node.maya_node = maya_node

        self.transforms.add(dson_node)

        # Turn off segmentScaleCompensate for the root joint, so we don't cancel out scales on the top-level container.
        if dson_node.parent.node_type != 'bone':
            maya_node.attr('segmentScaleCompensate').set(0)

        # Set the base position of the transform, ignoring modifiers.  We'll apply modifiers
        # after child transforms have been added to us.  This is because the origin of each
        # node is relative to the pre-modifier position of its parent hierarchy.  If you move
        # the static center_point of a node, it doesn't move its children, but if you apply
        # a modifier to center_point it does.
        center_point = dson_node.get_property('center_point').get_vec3(use_modifiers=False)
        pm.xform(maya_node, t=center_point)

        orientation = dson_node.get_property('orientation').get_vec3(use_modifiers=False)
        pm.xform(maya_node, ro=orientation)

        # We don't put this transform under its immediate parent yet.  If a prop is underneath
        # a joint, the prop's initial position is relative to the root of its parent, eg. the
        # whole figure, not the hand itself.  Joints inside a figure that aren't underneath
        # another figure are relative to the world.
        top_level_node = dson_node.find_top_node().parent

        pm.parent(dson_node.maya_node, top_level_node.maya_node, r=False)

    def apply_transform_orientation_modifiers(self, dson_node):
        """
        Apply the modifier portion of joint origins and orientation.

        This is performed after parenting geometry underneath us, which are affected by modifiers
        changing these values, but not by the constant value which is applied in create_transform.

        Note that transforms are still parented under their parent asset, not their final
        parent, eg. hand joints are children of the figure and not the arm.
        """
        # XXX: skip_constant_value doesn't actually make any sense, but it's not clear what's
        # actually happening here.  If we use_default_values and r=False then we'll read the
        # default values of *all* properties, including character modifiers, and we won't apply
        # skeleton changes from the character.
        center_point = dson_node['center_point'].get_vec3(use_modifiers=True, skip_constant_value=True)
        pm.xform(dson_node.maya_node, t=center_point, r=True)

        orientation = dson_node['orientation'].get_vec3(use_modifiers=True, skip_constant_value=True)
        pm.xform(dson_node.maya_node, ro=orientation, r=True)

        mh.bake_joint_rotation_to_joint_orient(dson_node.maya_node)

        # Save the worldInverseMatrix for MayaWorldSpaceTransformProperty later, so we have
        # the matrix before we apply parenting.
        dson_node.bind_inv_matrix = dson_node.maya_node.attr('worldInverseMatrix[0]').get()

    def apply_transform_parenting(self, non_joints_only=False):
        for dson_node in self.env.scene.depth_first():
            if dson_node not in self.transforms:
                continue

            if non_joints_only and dson_node.node_type == 'bone':
                continue

            # Parent this node under its parent transform, unless this is a top-level node.  Do this
            # after setting the above properties, so they'll be adjusted to local space as needed.
            parent_node = dson_node.parent
            if parent_node.node_type == 'scene':
                continue

            if parent_node.node_source == 'geometry':
                # This node is a child of geometry.  We don't create transforms for geometry nodes
                # (they'll become shapes), so put this node under the parent of the geometry.
                parent_maya_node = parent_node.parent.maya_node
            else:
                parent_maya_node = parent_node.maya_node

            if dson_node.maya_node.getParent() == parent_maya_node:
                continue

            pm.parent(dson_node.maya_node, parent_maya_node)

    def create_bone_end_joint(self, dson_node):
        # Only create end joints for bones with no children, eg. fingers and toes.
        # This will also create end joints for facial bones, which we may not really
        # want.
        for child in dson_node.children:
            if child.node_type == 'bone':
                return

        node = mh.createNewNode('joint', nodeName=mh.cleanup_node_name(dson_node.get_label()) + '_End')

        # Set a dummy DSON asset name, so these joints can be identified like their parents.
        pm.addAttr(node, longName='dson_asset_name', dt='string', niceName='DSON asset name')
        node.attr('dson_asset_name').set(dson_node.asset.get_value('name') + '_End')

        pm.addAttr(node, longName='dson_type', dt='string', niceName='DSON type')
        node.attr('dson_type').set('end_bone')

        center_point = dson_node.get_property('end_point').get_vec3()
        node.attr('translate').set(center_point)
        
        # If this seems to be a facial bone, hide the joint and hide it in the outliner.  This
        # gives us bone dimensions for facial bones (so they're not lost under geometry) without
        # them cluttering up the outliner and viewport.
        parent = dson_node
        is_facial = False
        while parent is not None:
            if parent.node_id == 'neckLower':
                is_facial = True
                break
            parent = parent.parent

        if is_facial:
            node.attr('visibility').set(0)
            node.attr('hiddenInOutliner').set(1)

        # log.debug('Creating end joint for %s' % dson_node)
        
        # Relative parenting kept the position we want, but it also adjusted the jointOrient.
        # Reset it to zero, so it points out away from the bone.
        node.attr('jointOrient').set((0, 0, 0))
        node.attr('radius').set(dson_node.maya_node.attr('radius').get())

        # Save the node for HumanIK.
        dson_node.end_joint_maya_node = node

    def apply_transform_conform(self):
        if not config.get('conform_joints'):
            return

        # Apply joint transforms from rigidity groups.  This can contain nodes other than
        # DSON bones, eg. end joints.  Apply translation in world space, and rotation in
        # object space.
        for maya_node, translation in self.rigidity_translation.iteritems():
            log.debug('Applying rigidity translation to %s: %s', maya_node, translation)
            pm.xform(maya_node, t=translation, ws=True, r=True)

        for maya_node, rotation in self.rigidity_rotation.iteritems():
            euler_rotation = maya_node.getRotation()
            quat = euler_rotation.asQuaternion()
            quat *= rotation
            euler = quat.asEulerRotation()

            # What the?  EulerRotation.order is a string like XYZ, but reorder() only takes an integer,
            # and you have to convert yourself.
            euler = euler.reorder(dt.EulerRotation.RotationOrder.getIndex(euler_rotation.order))
            pm.xform(maya_node, ro=(euler.x*180/math.pi, euler.y*180/math.pi, euler.z*180/math.pi))
            if maya_node.type() == 'joint':
                mh.bake_joint_rotation_to_joint_orient(maya_node)

    def apply_joint_parenting(self):
        self.progress.set_main_progress('Applying joint parenting...')

        # Parent the joints on each top node.  
        # xxx: move non-conforming children of conforming joints to the parent, then they're easier
        # to access and we can always hide conforming joints
        transforms = [dson_node for dson_node in self.transforms if dson_node.node_type in ('figure', 'prop')]

        # Do non-conforming figures first, so the main joints of the figure are listed in the outliner
        # before conforming joints.
        transforms.sort(key=lambda dson_node: dson_node.get('conform_target') is not None)

        for dson_node in transforms:
            if dson_node.node_type not in ('figure', 'prop'):
                continue

            # If this figure is conforming, find the target node.
            conform_target = dson_node.get('conform_target')
            if conform_target is not None:
                conform_target = conform_target.load_url()

                # Find the joints on the target node by name.
                target_joints_by_name = { joint['name'].value: joint for joint in DSON.Helpers.find_bones(conform_target) }
            else:
                target_joints_by_name = {}

            parent_for_joint = {}
            for conformed_joint in DSON.Helpers.find_bones(dson_node):
                target_for_joint = None
                if config.get('conform_joints'):
                    # Match each joint in the conformed skeleton to a joint with the same name in
                    # the target.
                    target_for_joint = target_joints_by_name.get(conformed_joint['name'].value)
                parent_for_joint[conformed_joint] = target_for_joint

            for conformed_joint in DSON.Helpers.find_bones(dson_node):
                end_joint_maya_node = getattr(conformed_joint, 'end_joint_maya_node', None)

                target_for_joint = parent_for_joint.get(conformed_joint)
                if target_for_joint is None:
                    # This isn't a conformed joint, or conform_target is turned off.  Just parent it normally
                    # under its parent.  If its parent is a conformed joint, parent us to the conform joint's
                    # target instead.
                    if parent_for_joint.get(conformed_joint.parent) is not None:
                        parent_node = parent_for_joint[conformed_joint.parent]
                    else:
                        parent_node = conformed_joint.parent
                    pm.parent(conformed_joint.maya_node, parent_node.maya_node)

                    # If this joint has end joint, parent it.
                    if end_joint_maya_node is not None:
                        pm.parent(end_joint_maya_node, conformed_joint.maya_node)                
                    continue

                # Don't draw conformed joints.  We don't hide these, since conformed joints often have
                # non-conformed joints as children which are manipulatable, such as hair joints.
                conformed_joint.maya_node.attr('drawStyle').set(2)
                end_joint_maya_node = getattr(conformed_joint, 'end_joint_maya_node', None)
                if end_joint_maya_node:
                    end_joint_maya_node.attr('drawStyle').set(2)

                # For now, we just use simple parent constraints.
                # pm.parentConstraint(target_for_joint.maya_node, conformed_joint.maya_node, maintainOffset=False)
                # pm.scaleConstraint(target_for_joint.maya_node, conformed_joint.maya_node, maintainOffset=False)
                # Parent/scale constraints get confused at segmentScaleCompensate.  When the root joint is
                # scaled, these constraints don't apply the scale correctly to conformed joints.  Just reparent
                # the conforming joints instead.
                pm.parent(conformed_joint.maya_node, target_for_joint.maya_node, r=True)
                conformed_joint.maya_node.attr('translate').set((0,0,0))
                conformed_joint.maya_node.attr('rotate').set((0,0,0))
                conformed_joint.maya_node.attr('jointOrient').set((0,0,0))

                pm.rename(conformed_joint.maya_node, mh.cleanup_node_name('%s_%s' % (dson_node.get_label(), conformed_joint.get_label())))

                if end_joint_maya_node is not None:
                    # Delete the end joint if its parent is conformed.  It doesn't do anything.
                    pm.delete(end_joint_maya_node)
                    conformed_joint.end_joint_maya_node = None

                # Hide these in the outliner.  We won't have non-conformed joints underneath it, since those
                # will be parented under the conform target instead.
                mh.config_internal_control(conformed_joint.maya_node)

                # Add an attribute to mark this as a conformed joint, so rigging scripts can tell which joint
                # is primary.
                pm.addAttr(conformed_joint.maya_node, longName='dson_conformed', at=bool, niceName='Conformed DSON joint', defaultValue=True)

    def is_property_dynamic(self, prop):
        """
        Return true if the given DSONProperty should be created dynamically.

        Note that this will prevent creation of dynamic rigging networks, but it won't affect
        geometry.  Even if we're not rigging morphs, they'll still be created.
        """
        if prop.node.is_modifier:
            # This is a modifer.  Check the user configuration to see if modifiers should be dynamic
            # or static.  dynamic_modifiers is a set of modifier asset URLs, so we need to check the
            # node's asset URL, not the property's.
            asset_url = prop.node.asset_url
            if self.config.asset_cache_results is not None:
                return asset_url in self.config.asset_cache_results.dynamic_modifiers

            # For testing, allow asset_cache_results to be omitted for faster loading.  In
            # this case, make full-body modifiers static, eg. "/People/Real World".
            return '/People' not in prop.node.get_value('group', '')

        # Quirk: don't create formulas from the combined toe joints.  They bind to the real toe joints and
        # prevent them from being moved.  Note that sometimes the constraint is on the lToe/rToe node going
        # to the target node, and sometimes it's on the target node.
        # Currently, we don't create modifiers located on joints.  These seem to all be
        # either bindings from combined toe joints to each toe, which we don't want (that
        # prevents us from attaching a rig to the toe joints without manually disconnecting
        # it), and a bunch of scale constraints that we don't currently need.
        #
        # XXX: We do want the ones on lCollar/rCollar.
#        if prop.node.node_type == 'bone':
            # log.debug('Skipping unwanted joint: %s', dson_modifier)
#            return False

        return True
    
    @classmethod
    def _apply_formulas(cls, prop):
        """
        Create and apply formulas for the given DSONProperty.
        """
        # Make a list of all formulas on the node and its asset (if any).
        formulas = prop.node.formulas.get(prop.path) or []

        # Add the constant value of the property to the result of static modifiers.
        static_value = prop.get_value_with_default()

        # Make a list of (property, weight) values that will be added to the blendWeighted.
        sum_stage_inputs = []
        sum_stage = [formula for formula in formulas if formula.get_stage() == 'sum']
        for formula in sum_stage:
            multiply_by_node, value = formula.is_simple_multiply_by_ref()
            if multiply_by_node is not None:
                # This is a simple "+ (input*constant)" expression, which is very common.  We don't need
                # to create a formula network for this.  Just set blendWeighted's weight to the constant
                # value.
                formula_output = mayadson.get_maya_output_attribute(multiply_by_node)
                sum_stage_inputs.append((formula_output, value))
                continue

            formula_output = modifiers_maya.create_maya_output_for_formula(formula)
            if not formula_output.is_maya_property:
                # This is a constant value.  Just add it to the static value.
                static_value += formula_output.value
                continue

            # Add this input to the sum stage list.
            sum_stage_inputs.append((formula_output.value, 1))

        # Make a list of inputs that will be multiplied together.
        mult_stage_inputs = []
        constant_factor = 1
        mult_stage = [formula for formula in formulas if formula.get_stage() == 'mult']
        for formula in mult_stage:
            formula_output = modifiers_maya.create_maya_output_for_formula(formula)
            if not formula_output.is_maya_property:
                constant_factor *= formula_output.value
            else:
                mult_stage_inputs.append(formula_output.value)

        if not sum_stage:
            # If there are no sum stage inputs, the entire expression is multiplicative.
            # Move the constant factor into the static value.
            static_value *= constant_factor
            constant_factor = 1

        if constant_factor == 0:
            # The constant factor is 0, so the output is always zero.  Discard all of the other
            # values.
            sum_stage_inputs = []
            mult_stage_inputs = []
            static_value = 0

        if constant_factor != 1:
            # If we have a constant factor, add it as a mult stage input.
            mult_stage_inputs.append(constant_factor)

        # We could create user controls for joints, but it's probably not very useful, since it won't
        # follow the output.
        #
        # Additionally, don't create a control if this isn't visible.  For example, don't create a user
        # control for corrective morph values.  By not creating these, we avoid creating a sum node
        # for most of these modifiers.
        #
        # If we're creating a user control for this property, it's treated as an extra value in the
        # sum stage, and it receives the static value of the property (the value in DSON with no
        # modifiers evaluated).  Note that we'll create controls for hidden properties like correctives,
        # to give a place to connect to if needed, but it'll be hidden from the CB.

        create_user_control = prop.is_dynamic and prop.node.is_modifier and prop.get_value('visible', True)
        if create_user_control:
            # Replace the static value with a control, whose default value is the old static value.
            static_value = mayadson.create_attribute_on_control_node(prop, initial_value=static_value)
        output_attr = static_value

        # We've finished creating the underlying formulas and collecting constant values.  We now
        # have sum_stage_inputs (a list of properties to add together), output_attr (an additional
        # property to add), and mult_stage_inputs (a list of values to multiply).
        #
        # If sum_stage_inputs and mult_stage_inputs are empty, this reduces to a constant.
        if len(sum_stage_inputs) == 1 and sum_stage_inputs[0][1] == 1 and output_attr == 0:
            # If there's only one sum stage input with a weight of 1 and the previous value is 0, we don't
            # need a blendWeighted.  We'll just start with the single input.
            output_attr = sum_stage_inputs[0][0]
        elif sum_stage_inputs:
            # Create a blendWeighted to sum together sum_stage_inputs.
            output_node = mh.createNewNode('blendWeighted', nodeName='Sum_%s_%s' % (prop.node.node_id, prop.path))

            next_formula_idx = 0

            # Add the previous value as the first value.
            mh.set_or_connect(output_node.attr('input').elementByLogicalIndex(next_formula_idx), output_attr)
            next_formula_idx += 1

            for formula_output, weight in sum_stage_inputs:
                input_attr = output_node.attr('input').elementByLogicalIndex(next_formula_idx)
                pm.connectAttr(formula_output, input_attr)

                weight_attr = output_node.attr('weight').elementByLogicalIndex(next_formula_idx)
                pm.setAttr(weight_attr, weight)

                next_formula_idx += 1

            output_attr = output_node.attr('output')

        # If we have any mult stage formulas, apply them next.
        for formula_output in mult_stage_inputs:
            # There doesn't seem to be a "multiply N values" node like blendWeighted is "add N nodes", so
            # we have to create a separate multiplyDivide node for each multiply stage formula.  There are
            # usually not many of these.
            mult_node = mh.createNewNode('multiplyDivide', nodeName='DSON_Mult')

            # Connect the previous output to the first 1D input of our multiplyDivide.
            mh.set_or_connect(mult_node.attr('input1X'), output_attr)

            # Connect the formula to the second input if it's a Maya attribute, or set the
            # value if it's a constant.
            mh.set_or_connect(mult_node.attr('input2X'), formula_output)

            output_attr = mult_node.attr('outputX')

        # Write the final result to the property.  This may be a constant or a PyNode attribute.
        mayadson.set_final_property_value(prop, output_attr)

    # The high-level way we handle modifiers is:
    #
    # - Create a DSONModifier for each instanced modifier (we instance all inherited, uninstanced
    # modifiers).  This parses out the modifiers and formulas, and attaches the formulas to the
    # DSONProperties they have as outputs.
    # - All property lookups read the value of channels and any modifiers, using prop.evaluate().
    # This happens as we set up the scene, so everything in the scene includes the current
    # output of all modifiers (in the state the scene was saved in) as we set up the Maya scene.
    # For static properties (properties we don't support dynamic modifiers for), they're done
    # here.
    # - For properties we support dynamic modifiers for, we store the Maya attribute associated
    # with the DSONProperty using add_maya_attribute.
    # 
    # Once the whole scene is set up and we're not adding any more properties, we set up modifiers
    # for attributes added with add_maya_attributes:
    #
    # - Create a sum (plusMinusAverage) node to add sum stage formulas together, as well as its constant
    # value (value or current_value).  The constant value is just entry in plusMinusAverage.
    # - Append multiplyDivide nodes for any divide stage formulas.
    # - We override the value of the Maya property entirely and don't care what it's set to, though
    # since we evaluated the property statically in the same way it should come out to the same value.
    #
    # Formula inputs that are properties with connected Maya attributes connect directly to the
    # property.  This way, if there are no formulas outputting to that attribute, we don't need to
    # do anything else to it.  For example, this allows corrective shapes that use joint rotations
    # as inputs to work, without attaching an input to the joint and preventing it from being moved.
    #
    # If we don't set up modifiers at all, the scene is set up statically the same way it would
    # have been with modifiers.
    def create_modifiers(self):
        self.progress.set_main_progress('Creating modifiers...')

        if not config.get('modifiers'):
            return

        # We don't support dynamic rigging for every property.  For example, we don't rig joint
        # orientation and center_point.  As a result, we don't need to create formulas for those
        # properties, and properties that recursively only depend on them.
        #
        # Make a list of DSONProperties that we do need to create formulas for.  Start with the
        # ones that have Maya attributes associated with them, then recursively find their
        # dependencies.
        #
        # Note that this is just a list of properties that we need to create dynamic properties
        # for.  We'll still calculate formulas for other properties, but they'll only happen
        # statically as we create the formulas that use them, rather than creating a node network
        # to calculate it dynamically.
        # if a modifier wants to output to a non-dynamic property.
        dynamic_properties = set()

        for node in self.env.scene.depth_first():
            # Iterate over all properties with assocaited Maya attributes.
            for prop_path in node.property_maya_attributes.keys():
                prop = node[prop_path]
                dynamic_properties.add(prop)

        properties_to_check = set(dynamic_properties)
        while properties_to_check:
            next_property = properties_to_check.pop()

            # We need to create formulas for all properties that are inputs to this property.
            for input_formulas in next_property.node.formulas.values():
                for input_formula in input_formulas:
                    inputs = input_formula.input_properties

                    # Ignore properties that we've already processed.
                    inputs = inputs.difference(dynamic_properties)

                    properties_to_check.update(inputs)
                    dynamic_properties.update(inputs)

        # Make a list of properties that need their formulas created.
        properties_to_create_formulas = []
        for node in self.env.scene.depth_first():
            # The set of properties on this node that have formulas to create:
            properties_with_modifiers = set(node.formulas.keys())

            # Remove properties that aren't dynamic.
            dynamic_properties.difference_update(dynamic_properties)

            # If this is a modifier, always apply its value, even if the value doesn't have any
            # modifiers affecting it.  This way, we always create the control.
            if node.is_modifier and node.get_value('channel/type') != 'alias':
                # Modifiers usually have one channel.  Its name is usually "value", but probably doesn't
                # have to be.  (Formulas that are on joints don't have channels.)  Look up the name, then
                # get the channel property.
                modifier_channel_name = node.get_value('channel/id', None)
                if modifier_channel_name is not None:
                    properties_with_modifiers.add(modifier_channel_name)            

            for prop_path in properties_with_modifiers:
                properties_to_create_formulas.append(node[prop_path])

        # Maya will show properties in the CB in the order we create them, and there's no way to
        # reorder it.  Create property formulas alphabetically by the property label, so controls
        # will be alphabetical.
        for idx, prop in enumerate(sorted(properties_to_create_formulas, key=lambda prop: prop.get_label())):
            self.progress.set_task_progress(prop.node.node_id, float(idx) / len(properties_to_create_formulas))
            self._apply_formulas(prop)

        # Hide the ControlProperties group by default.  It's not very useful except for troubleshooting.
        # This node won't exist if we didn't create any modifiers.
        if pm.ls('ControlProperties'):
            mh.config_internal_control('ControlProperties')
            for child in pm.listRelatives('ControlProperties', children=True):
                mh.config_internal_control(child)

    def apply_post_skinning_transforms(self):
        class MayaNormalizedRotationProperty(mayadson.MayaPropertyAssociation):
            def __init__(self, maya_node, *args, **kwargs):
                super(MayaNormalizedRotationProperty, self).__init__(*args, **kwargs)
                self.maya_node = maya_node
                self.output = None

            def _create_output_attr(self):
                if self.output is not None:
                    return self.output

                self.output = mh.normalize_rotation(self.maya_node)
                return self.output

        # DSON's translate orientation is different from Maya's, which is a pain: they're
        # relative to world space at bind time.  MayaWorldSpaceTransformProperty takes translation
        # in bind time world space and converts it to local space.
        class MayaWorldSpaceTransformProperty(mayadson.MayaPropertyAssociation):
            def __init__(self, maya_node, *args, **kwargs):
                super(MayaWorldSpaceTransformProperty, self).__init__(*args, **kwargs)

                self.translate_matrix = dt.Matrix(1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1)

                # Translations from modifiers are in world space at bind time.  If we have a translation
                # of (1,0,0), and we were rotated at bind time by 90 degrees on the Y axis, then we need
                # to move by (0,0,1) instead.  dson_node.bind_matrix is the world to object space transform
                # at bind time, and dson_node.bind_inv_matrix is object space to world space at bind time.
                bind_matrix_inverse = dt.Matrix(dson_node.bind_inv_matrix)

                # We only want the rotation part of the matrix.  Clear out transforms.
                bind_matrix_inverse[3,0] = 0
                bind_matrix_inverse[3,1] = 0
                bind_matrix_inverse[3,2] = 0

                create_local_matrix = dson_node.maya_node.attr('matrix').get()
                self.transform_matrix = bind_matrix_inverse * create_local_matrix

                # The transform
                self.maya_node = maya_node
                self.input = None

            def set_value(self, prop, value):
                # We set one axis at a time.  Update self._value with the changed input value.
                maya_channels = mh.get_channels_cached(self._input_attr)
                maya_channel_idx = self._property_map.index(prop.last_path)

                if not isinstance(value, pm.PyNode) and self.input is None:
                    # Save the new constant value to translate_matrix.
                    self.translate_matrix[3,maya_channel_idx] = value

                    # We haven't connected any dynamic properties yet, so we're just applying the result
                    # directly to the Maya translate.
#                    log.debug('Set %s to const %s', prop, value)
                    result = self.translate_matrix * self.transform_matrix

#                    log.debug('%s: bind_matrix_inverse %s', prop, self.bind_matrix_inverse)
#                    log.debug('%s: create_local_matrix %s', prop, self.create_local_matrix)
#                    log.debug('%s: T %s', prop, T)
#                    log.debug('%s: result %s', prop, result)

                    new_translate = (result[3,0], result[3,1], result[3,2])

                    self.maya_node.attr('translate').set(new_translate)
                    return

                # This is a dynamic input, or another axis has already set a dynamic input.  Create
                # a network to do the math, if we haven't yet.
                if self.input is None:
                    # If any translate axis is being set, we connect all axes, since changing one axis before
                    # the transform affects all axes.
                    self.compose = pm.createNode('composeMatrix') # T

                    multiply = pm.createNode('multMatrix')
                    self.compose.attr('outputMatrix').connect(multiply.attr('matrixIn[0]'))
                    pm.setAttr(multiply.attr('matrixIn[1]'), self.transform_matrix, type='matrix')

                    decompose = pm.createNode('decomposeMatrix')
                    multiply.attr('matrixSum').connect(decompose.attr('inputMatrix'))
                    decompose.attr('outputTranslate').connect(self.maya_node.attr('translate'))

                    self.input = self.compose.attr('inputTranslate')

                attr = self.compose.attr('inputTranslate' + prop.last_path.upper())
                # log.debug('Set %s (%s) to %s', prop, attr, value)
                mh.set_or_connect(attr, value)

        for dson_node in self.transforms:
            prop = dson_node.get_property('translation')
            translate_attr = dson_node.maya_node.attr('translate')
            translate_props = ('x', 'y', 'z')

            assoc = MayaWorldSpaceTransformProperty(dson_node.maya_node, translate_attr, property_map=translate_props)
            translate_channels = mh.get_channels_cached(translate_attr)
            for maya_channel_idx, channel_prop_name in enumerate(translate_props):
                mayadson.set_attr_to_prop(prop[channel_prop_name], translate_channels[maya_channel_idx], property_association=assoc)

            prop = dson_node['rotation']
            rotate_attr = dson_node.maya_node.attr('rotate')
            rotation_props = ('x', 'y', 'z')

            def needs_normalized_rotations(node):
                # Don't create these on face joints or on fingers and toes, since it's not needed there
                # and it slows things down.  Do create it on the head and hands themselves, only skip
                # it on their descendants.
                while not node.is_top_node:
                    node = node.parent

                    if node.asset_id in ('head', 'lHand', 'rHand'):
                        return False

                return True

            if needs_normalized_rotations(dson_node):
                assoc = MayaNormalizedRotationProperty(dson_node.maya_node, rotate_attr, property_map=rotation_props)
                rotate_channels = mh.get_channels_cached(rotate_attr)
                for maya_channel_idx, channel_prop_name in enumerate(rotation_props):
                    mayadson.set_attr_to_prop(prop[channel_prop_name], rotate_channels[maya_channel_idx], property_association=assoc)
            else:
                # Connect rotation normally.
                mayadson.set_attr_to_prop_vector(prop, rotate_attr, dson_to_maya_attrs=rotation_props)

            prop = dson_node.get_property('scale')
            mayadson.set_attr_to_prop_vector(prop, dson_node.maya_node.attr('scale'), dson_to_maya_attrs=('x', 'y', 'z'), dynamic=True)

    def post_cleanup(self):
        """
        Run final cleanup.
        """
        # We initially created all transforms as joints, to get access to jointOrient.  Go through
        # figure transforms and look for ones that can be simple transforms, and replace them where
        # possible.  It's weird to have the root node be a joint.  Do this before unparenting children,
        # or we'll delete the node we're constraining to.
        self.progress.set_main_progress('Cleanup: replace root joints with transforms')
        for dson_node in self.transforms:
            if dson_node.node_type not in ('figure', 'prop', 'node'):
                continue
            if pm.getAttr(dson_node.maya_node.attr('jointOrient')).distanceTo((0,0,0)) > 0.001:
                log.debug('Leaving a joint on %s, because it has a nonzero orientation', dson_node)
                continue

            # Don't do this if any modifiers care about the root transform.  This is uncommon.
            if dson_node.has_modifier_dependants():
                continue
            
            log.debug('Replacing %s with a transform', dson_node.maya_node)

            # Create a transform to replace the joint, position it at the same place as the joint,
            # then put it in its parent.
            replacement_node = pm.createNode('transform')
            pm.parent(replacement_node, dson_node.maya_node, r=True)
            pm.parent(replacement_node, dson_node.maya_node.getParent())

            # Move the joint's children into the new node.
            for child in pm.listRelatives(dson_node.maya_node, children=True):
                pm.parent(child, replacement_node)

            # Delete the empty old node.
            name = dson_node.maya_node.nodeName()
            pm.delete(dson_node.maya_node)
            pm.rename(replacement_node, name)

            # Record the replacement node.
            dson_node.maya_node = replacement_node

        # DSON files often have whole hierarchies which are children of joints.  This
        # is messy in Maya.  Find these and try to move them out of the skeleton and
        # into the containing node, and constrain the node to its old parent.
        self.progress.set_main_progress('Cleanup: unparent children of joints')
        for dson_node in self.transforms:
            if dson_node.node_type == 'bone':
                continue
            if not dson_node.parent or dson_node.parent.node_type != 'bone':
                continue

            # Find the parent of the bone this node is inside.
            parent_figure = dson_node.parent.find_top_node()
            log.debug('Moving %s (%s) %s out of the skeleton and into %s', dson_node, dson_node.maya_node, id(dson_node), parent_figure)

            # XXX: twist rigs are breaking this?
            old_parent = dson_node.maya_node.getParent()
            pm.parent(dson_node.maya_node, parent_figure.maya_node)
            pm.parentConstraint(old_parent, dson_node.maya_node, maintainOffset=True)
            pm.scaleConstraint(old_parent, dson_node.maya_node, maintainOffset=True)

        self.progress.set_main_progress('Cleanup: unparent meshes')
        for mesh_set in self.all_mesh_sets.values():
            mesh_set.remove_group_node_if_empty()

        # Quirk: Create an extra joint in between head joints and their children.  This will group
        # all of the face parts.  We don't want the head joint itself to do this, since it's convenient
        # to be able to hide the head rigging parts or its bone drawing, without hiding the head joint
        # itself which is a body joint and not a facial joint.
        for dson_node in self.transforms:
            if not dson_node.asset or dson_node.asset.node_id != 'head':
                continue

            face_group = pm.createNode('joint', name='Face', parent=dson_node.maya_node)

            # Don't draw bones from this group to the face rig.
            pm.setAttr(face_group.attr('drawStyle'), 2)

            for child in pm.listRelatives(dson_node.maya_node, children=True):
                if child == face_group:
                    continue
                pm.parent(child, face_group)

    def load_scene(self, filename):
        self.env.load_user_scene(filename)

    def _run_import_with_progress(self):
        self.rigidity_translation = {}
        self.rigidity_rotation = {}

        # Load the files containing modifers listed in the configuration.  If this is None, we'll
        # just load referenced modifiers recursively with no filtering.
        self.progress.set_main_progress('Loading modifiers...')
        if self.config.modifier_asset_urls is not None:
            for idx, url in enumerate(self.config.modifier_asset_urls):
                self.progress.set_task_progress(os.path.basename(DSONURL(url).path), float(idx) / len(self.config.modifier_asset_urls))
                self.env.scene.get_url(url)
 
            # Delete any modifier instances in the scene that aren't active in the configuration.
            # Don't remove nodes while traversing.
            nodes_to_remove = set()
            for node in self.env.scene.depth_first():
                if node.asset is None:
                    continue

                # Only delete instances whose asset comes from a file whose asset_type is "modifier".
                # There are modifiers like SkinBindings in figure assets, which AssetCache doesn't index.
                # Those will never be listed as modifiers, so don't delete them.
                if node.asset.dson_file.asset_info['type'] != 'modifier':
                    continue

                if node.asset_url in self.config.modifier_asset_urls:
                    continue

                # Remove both the library asset and all instances.
                nodes_to_remove.add(node)
                nodes_to_remove.add(node.asset)

            for node in nodes_to_remove:
                node.delete()

        # This won't do much, since we should have been given all of the modifiers we depend on.  This
        # will load aliases.
        modifiers.recursively_load_modifier_dependencies(self.env)

        # If we have any modifiers in the library that we didn't delete above but which aren't instanced
        # on eligible nodes, create instances.
        DSON.Helpers.recursively_instance_assets(self.env.scene)

        # Create implicit FID_* modifier channels for each modifier, and add formulas for auto_follow
        # modifiers.
        DSON.Helpers.create_fid_modifiers(self.env)

        # Create DSONModifiers for all modifiers in the scene.
        modifiers.Helpers.load_all_modifiers(self.env)

        modifiers.Helpers.add_auto_follow_formulas(self.env)

        # Load all meshes.  Note that mesh UVs can come from materials, so we need to have MaterialLoader
        # set up by now.
        self.progress.set_main_progress('Loading meshes and UV sets...')
        self.all_mesh_sets = {}
        for figure in self.env.scene.depth_first():
            if 'geometries' not in figure:
                continue

            # Find the geometries that are an immediate child of this figure.
            geometry = figure.first_geometry
            if not geometry:
                continue

            assert geometry not in self.all_mesh_sets
            self.all_mesh_sets[geometry] = MeshSet(geometry)

        # Create all objects with transforms.  This includes joints.
        self.progress.set_main_progress('Creating transforms...')
        self.transforms = set()
        for dson_node in self.env.scene.depth_first():
            if dson_node is self.env.scene:
                continue
            if dson_node.node_type not in ('bone', 'figure', 'node'):
                continue
            if dson_node.node_type == 'figure':
                # Some nodes get lumped in as "figure", since they have that in their asset_info
                # and don't specify a type on each node.  Don't create a transform unless there's
                # actually a geometry node for it.
                if not dson_node.first_geometry:
                    continue
            
            self.create_transform(dson_node)

        # We're probably in a new scene.  Fit the current view to show the transforms, since
        # we usually end up looking at feet in a default configuration if we leave the camera
        # alone.
        #
        # Note that refreshing doesn't just give the user better feedback while we're loading,
        # it avoids viewport bugs.  If we don't refresh for the whole process, VP2.0 tends to
        # lose shading engine connections somehow and renders the mesh green when we're done.
        pm.viewFit(all=True)
        pm.refresh()

        self.create_maya_geometry()
        
        # Create materials.
        self.progress.set_main_progress('Creating materials...')
        if config.get('materials'):
            materials.load_and_assign_materials(self.env, self.all_mesh_sets, self.progress.set_task_progress)

        self.progress.set_main_progress('Creating end joints...')
        if config.get('end_joints'):
            # DSON bones have a start and an end point.  Maya bones have just a position, and there's
            # an extra stub bone at the end of a chain to tell where fingers, etc. end.  Create stub
            # bones.
            for dson_node in self.transforms:
                if dson_node.node_type != 'bone':
                    continue

                self.create_bone_end_joint(dson_node)

        # Apply morphs.  This is done before skinning, while the character is in bind pose.
        self.apply_morphs()
        pm.refresh()

        # Apply modifiers to joint positions after creating geometry, so geometry follows.
        for dson_node in self.env.scene.depth_first():
            if dson_node not in self.transforms:
                continue
            self.apply_transform_orientation_modifiers(dson_node)

        # Should we temporarily drop to shaded if the viewport is in textured, so we don't stop
        # here while textures are loaded?
        pm.refresh()            

        # Parent everything to their parent except for joints.  Props and other figures follow the
        # upcoming transforms, but joints don't.
        self.apply_transform_parenting(non_joints_only=True)

        # Apply conform_target to transforms.  Do this before applying skin clusters, so any changes
        # to the base mesh's bind-time skeleton are applied to the skeletons of graft geometry
        # too before we bind skins to everything.
        self.apply_transform_conform()

        # Apply parenting to joints.
        self.apply_joint_parenting()

        # Create skinClusters for each SkinBinding.
        self.apply_skinning()

        # Apply post-skinning properties to transforms.  These are ones that cause the mesh to be
        # moved, like translation.  This must be done after attaching skinClusters.
        self.apply_post_skinning_transforms()
        pm.refresh()            

        # Optimize DSON modifiers before creating Maya modifiers.  We depend on this being optimized
        # in create_modifiers, in order to avoid creating a ton of extra math nodes.
        modifiers.Helpers.optimize_all_modifiers(self.env)

        # Apply modifiers to Maya nodes.  This needs to be done late.  Modifiers need to know where
        # to output their values.  We stash the Maya property corresponding to each supported DSON
        # property in the DSONNodes, and if a property has a modifier outputting to it but has no
        # associated Maya attribute, a property node will be created to hold it.  So, we need to do
        # this after anything that might create Maya nodes for controls.
        self.create_modifiers()

        self.post_cleanup()

        # Straighten t-poses.  Do this before creating twist rigs.
        rigging.straighten_poses(self.env)

        # Store info about each Maya node that corresponds to a DSON node.  We do this late, so we
        # don't have to fix this up in places where we replace one Maya node with another.
        for dson_node in self.env.scene.depth_first():
            mesh_set = self.all_mesh_sets.get(dson_node.first_geometry)
            if dson_node.maya_node:
                mayadson.set_maya_transform_attrs(dson_node, mesh_set)

    def run_import(self):
        main_progress_sections = 14
        self.progress.show('Importing...', main_progress_sections)
        try:
            pm.waitCursor(state=True)

            self._run_import_with_progress()

            # Log if the progress count is inconsistent, so it's easy to see what the above value
            # should be.
            if self.progress.main_progress_value != main_progress_sections:
                log.debug('Expected %i progress sections, got %i', main_progress_sections, self.progress.main_progress_value)
        except util.CancelledException as e:
            pass
        except BaseException as e:
            # Log the exception, then swallow it, since throwing an exception up to Maya will just
            # log it again.
            log.exception(e)
        finally:
            self.progress.hide()
            pm.waitCursor(state=False)


