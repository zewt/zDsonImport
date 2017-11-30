# Based on:
#
# https://github.com/Kitware/VTK/blob/master/Common/ComputationalGeometry/vtkKochanekSpline.cxx
# https://github.com/bbattey/KochanekBartelsSpline/blob/master/KochanekBartelsSpline.m

import bisect

class KochanekBartelsSpline(object):
    """
    >>> kbs = KochanekBartelsSpline([(0, 0, 0.5, 0.5, 0.5), \
                                     (1, 10, 0.5, 0.5, 0.5), \
                                     (2, 20, 0.5, 0.5, 0.5)])
    >>> kbs.evaluate(-1)
    0.0
    >>> kbs.evaluate(0)
    0.0
    >>> kbs.evaluate(1)
    10.0
    >>> kbs.evaluate(2)
    20.0
    >>> kbs.evaluate(3)
    20.0
    >>> kbs.evaluate(1.5)
    15.3125
    """

    def __init__(self, keys):
        self.input_points = []

        for key in keys:
            assert len(key) == 5
            self.input_points.append(tuple(float(x) for x in key))

        self.input_points.sort()

        DSvectorsX = []
        DDvectorsX = []

        # Calculate incoming and outgoing tangent vectors
        # for each point (Kochanek and Bartels Equations 8 & 9)
        self.coef = []
        for idx in xrange(len(self.input_points)):
            next_idx = max(0, min(len(self.input_points)-1, idx + 1))
            prev_idx = max(0, min(len(self.input_points)-1, idx - 1))
            x0, y0, _, _, _ = self.input_points[prev_idx]
            x1, y1, t, c, b = self.input_points[idx]
            x2, y2, _, _, _ = self.input_points[next_idx]

            cs = y1-y0
            cd = y2-y1

            # tension/continuity/bias equations
            ds = cs*((1-t) * (1-c) * (1+b)) / 2.0 + cd*((1-t) * (1+c) * (1-b)) / 2.0
            dd = cs*((1-t) * (1+c) * (1+b)) / 2.0 + cd*((1-t) * (1-c) * (1-b)) / 2.0

            # adjust deriviatives for non uniform spacing between nodes
            n1 = x2 - x1
            n0 = x1 - x0
            ds *= (2 * n0 / (n0 + n1))
            dd *= (2 * n1 / (n0 + n1))

            # DS "source derivative" (incoming vector) for this point
            DSvectorsX.append(ds)
            # DD "desination derivative" (outgoing vector) for this point
            DDvectorsX.append(dd)

        for idx in xrange(len(self.input_points)-1):
            _, y1, _, _, _ = self.input_points[idx]
            _, y2, _, _, _ = self.input_points[idx+1]

            d0 = DDvectorsX[idx]
            d1 = DSvectorsX[idx+1]

            coef = []
            coef.append(y1)
            coef.append(d0)
            coef.append(-3.0*y1 + 3.0*y2 - 2.0*d0 - d1)
            coef.append( 2.0*y1 - 2.0*y2 +     d0 + d1)
            self.coef.append(coef)

    def evaluate(self, f):
        f = float(f)

        # Find the keyframes that f lies between.
        idx = bisect.bisect([p[0] for p in self.input_points], f)-1
        idx = max(0, min(idx, len(self.input_points)-2))

        x1 = self.input_points[idx][0]
        x2 = self.input_points[idx+1][0]

        # Figure out the position between idx and idx+1.
        f = (f - x1) / (x2 - x1)
        f = min(1, max(0, f))

        coef = self.coef[idx]
        return coef[3]*f*f*f + coef[2]*f*f + coef[1]*f + coef[0]

if __name__ == "__main__":
    import doctest
    doctest.testmod()

#for x in xrange(0, 20):
#    f = x / 10.0
#    print f, kbs.evaluate(f)


# The MIT License (MIT)
# 
# Copyright (c) 2015 Bret Battey
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Copyright (c) 1993-2015 Ken Martin, Will Schroeder, Bill Lorensen
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
#  * Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 
#  * Neither name of Ken Martin, Will Schroeder, or Bill Lorensen nor the names
#    of any contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS ``AS IS''
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE AUTHORS OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

