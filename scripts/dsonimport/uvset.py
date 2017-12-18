import logging, math
from dson import DSON

log = logging.getLogger('DSONImporter')

class UVSet(object):
    @classmethod
    def create_from_dson_uvset(cls, dson_uv_set, polys):
        """
        Create a UVSet from a DSON UV set node.
        """
        uv_indices = dson_uv_set['polygon_vertex_indices']
        static_uv_indices = dson_uv_set['vertex_count']
        poly_vert_map = { (entry[0], entry[1]): entry[2] for entry in uv_indices.value }

        # DSON compresses UVs by storing a UV per vertex, then having a mapping of vertex/face pairs
        # that have a different UV, for where adjacent faces have different UVs.  We need to send the
        # UVs to Maya per face vertex anyway, and it's more convenient to have a single mapping, so
        # we'll just create a per-face-vertex UV list and throw away the compressed data.
        uv_indices_per_poly = []
        for poly_idx, poly in enumerate(polys):
            # Unless the UV set remaps an individual face vertex, this is the index in the UV set.
            # This will be changed when the mesh is culled or grafted.
            # poly['uv_idx'] = poly_idx
            uv_indices = []
            source_poly_idx = poly['source_poly_idx']
            for vertex_idx in poly['vertex_indices']:
                try:
                    uv_idx = poly_vert_map[source_poly_idx, vertex_idx]
                except KeyError as e:
                    if vertex_idx < static_uv_indices:
                        uv_idx = vertex_idx
                    else:
                        raise RuntimeError('Out of range index: (%i,%i,%i)' % (poly_idx, vertex_idx, static_indices))
                
                uv_indices.append(uv_idx)
            uv_indices_per_poly.append(uv_indices)

        return cls(dson_uv_set.get_label(), dson_uv_set['uvs/values'].value, uv_indices_per_poly, dson_uv_set=dson_uv_set)

    def __init__(self, name, uv_values, uv_indices_per_poly, dson_uv_set=None):
        if dson_uv_set is not None:
            assert isinstance(dson_uv_set, DSON.DSONNode), dson_uv_set

        self.dson_uv_set = dson_uv_set
        self.name = name

        # Be sure to make a copy of this, since we can modify it when remapping and we don't
        # want to modify it in the source data.
        self.uv_values = list(uv_values)
        self.uv_indices_per_poly = uv_indices_per_poly

    def __repr__(self):
        return 'UVSet(%s)' % self.name

    def cull_unused(self, polys):
        """
        Remove unused UVs.  polys is a face list from LoadedMesh.polys.
        """
        used_uv_indices = []

        uv_indices_per_poly = self.uv_indices_per_poly
        for poly_idx, poly in enumerate(polys):
            uv_indices = uv_indices_per_poly[poly_idx]
            for idx, vertex_idx in enumerate(poly['vertex_indices']):
                used_uv_indices.append(uv_indices[idx])

        used_uv_indices = list(set(used_uv_indices))
        used_uv_indices.sort()

        # XXX array instead of dict?
        old_uv_indices_to_new = {old_uv_idx: new_uv_idx for new_uv_idx, old_uv_idx in enumerate(used_uv_indices)}
        new_uv_indices_to_old = {b: a for a, b in old_uv_indices_to_new.items()}

        self.uv_values = [self.uv_values[new_uv_indices_to_old[idx]] for idx in xrange(len(new_uv_indices_to_old))]

        for poly_idx, poly in enumerate(polys):
            uv_indices_per_poly[poly_idx] = [old_uv_indices_to_new[uv_idx] for uv_idx in uv_indices_per_poly[poly_idx]]

    def get_uv_bounds(self, face_indices):
        """
        Return (min_u, min_v), (max_u_ max_v) for UVs used by faces in face_indices.
        """
        minimum = [10e10, 10e10]
        maximum = [-10e10, -10e10]
        for face_idx in face_indices:
            uv_indices = self.uv_indices_per_poly[face_idx]
            for uv_index in uv_indices:
                value = self.uv_values[uv_index]
            
                minimum[0] = min(minimum[0], value[0])
                minimum[1] = min(minimum[1], value[1])
                maximum[0] = max(maximum[0], value[0])
                maximum[1] = max(maximum[1], value[1])

        return minimum, maximum

    def get_uv_tile_bounds(self, face_indices):
        """
        Return (min_u, min_v), (max_u_ max_v) in this UVSet, rounded outwards to an integer.

        If this UVSet only uses UVs within 0-1, return (0,0), (1,1).
        """
        minimum, maximum = self.get_uv_bounds(face_indices)
        minimum[0] = int(math.floor(minimum[0]))
        minimum[1] = int(math.floor(minimum[1]))
        maximum[0] = int(math.ceil(maximum[0]))
        maximum[1] = int(math.ceil(maximum[1]))
        return minimum, maximum

