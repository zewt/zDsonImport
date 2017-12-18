import logging, math
import pymel.core as pm

log = logging.getLogger('DSONImporter.Materials')

# http://therenderblog.com/fresnel-schlicks-approximation-in-maya/
# http://therenderblog.com/custom-fresnel-curves-in-maya-part-2/

def schlick_aprox(reflt0):
    reflt0 = reflt0
    theta_deg = 0
 
    RefltResult = []
 
    while theta_deg <= 90:
        theta = math.radians(theta_deg)
        refVal = reflt0 + (float(1-reflt0)) * (1-math.cos(theta))**5
        RefltResult.append(round(refVal,6))
        theta_deg += 1
 
    return RefltResult

def _vec2d_dist(p1, p2):
    return (p1[0] - p2[0])**2 + (p1[1] - p2[1])**2
 
def _vec2d_sub(p1, p2):
    return (p1[0]-p2[0], p1[1]-p2[1])
 
def _vec2d_mult(p1, p2):
    return p1[0]*p2[0] + p1[1]*p2[1]
 
def ramerdouglas(line, dist):
    if len(line) < 3:
        return line
 
    begin, end = (line[0], line[-1]) if line[0] != line[-1] else (line[0], line[-2])
 
    distSq = []
    for curr in line[1:-1]:
        tmp = (_vec2d_dist(begin, curr) - _vec2d_mult(_vec2d_sub(end, begin), _vec2d_sub(curr, begin)) ** 2 / _vec2d_dist(begin, end))
        distSq.append(tmp)
 
    maxdist = max(distSq)
    if maxdist < dist ** 2:
        return [begin, end]
 
    pos = distSq.index(maxdist)
    return (ramerdouglas(line[:pos + 2], dist) + ramerdouglas(line[pos + 1:], dist)[1:])

def create_ramp_for_schlick(normal_reflectivity, max_points=100):
    """
    Create a ramp approximating Schlick's approximation.  max_points is the maximum number
    of points to place on the ramp.
    """
    remap_node = pm.shadingNode('remapValue', asUtility=True)

    sampler_info = pm.shadingNode('samplerInfo', asUtility=True)
    sampler_info.attr('facingRatio').connect(remap_node.attr('inputValue'))
 
    # Calculate Fresnel curve
    schlick_list = schlick_aprox(normal_reflectivity)
 
    # Compensate for non-linear facingRatio
    linearValues = [float(i)/90 for i in range(91)]
    raw_values = [math.sin(linearValues[i]*90*math.pi/180) for i in range(91)]
    raw_values.reverse()

    # Keep decreasing precision until we reduce the curve to the maximum number of points. 
    myline = zip(raw_values, schlick_list)
    precision = 0.00005
    simplified = None
    while simplified is None or len(simplified) > max_points:
        simplified = ramerdouglas(myline, dist=precision)
        precision *= 2
 
    # Remove default values
    pm.removeMultiInstance(remap_node.attr('value[0]'), b=1)
    pm.removeMultiInstance(remap_node.attr('value[1]'), b=1)
 
    for i in simplified:
        currentSize = pm.getAttr(remap_node.attr('value'), size=1)
 
        # First and last values with linear interpolation, others with spline interpolation.
        if simplified.index(i) == 0 or simplified.index(i) == len(simplified)-1:
            interp = 1
        else:
            interp = 3

        attr = remap_node.attr('value[%i]' % (currentSize+1))
        pm.setAttr(attr, i[0],i[1], interp, type='double3')

    return remap_node.attr('outValue')
 


