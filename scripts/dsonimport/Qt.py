# QT5 moved a bunch of stuff around, moving some things from QtGui into QtWidgets. 
# This means that in order to make their API slightly "prettier", QT created a ton
# of useless busywork for thousands of developers, and makes compatibility with Qt4
# and Qt5 painful.  I couldn't care less about which module these are in, so work
# around this by flattening everything into one module.
from maya.OpenMaya import MGlobal
if MGlobal.apiVersion() >= 201700:
    from PySide2.QtCore import *
    from PySide2.QtGui import *
    from PySide2.QtWidgets import *
    from shiboken2 import wrapInstance
    import pyside2uic as pysideuic
else:
    from PySide.QtCore import *
    from PySide.QtGui import *
    from shiboken import wrapInstance
    import pysideuic as pysideuic

