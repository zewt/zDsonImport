import inspect, os, sys

from maya import OpenMaya as om
import pymel.core as pm
from pprint import pprint

gMainFileMenu = pm.language.melGlobals['gMainFileMenu']

# Import only when a menu item is actually used, so we don't spend time during Maya
# load importing scripts that aren't being used.
def do_import(unused):
    from dsonimport import ui
    ui.go()

def do_update_library(unused):
    from dsonimport import ui
    ui.refresh_cache()

def do_apply_materials(unused):
    from dsonimport.materials import create_materials
    create_materials.go()

def do_apply_rigging(unused):
    from dsonimport import auto_rig
    auto_rig.go(humanik=False)

def do_apply_humanik(unused):
    from dsonimport import auto_rig
    auto_rig.go(humanik=True)

def do_ik_fk(unused):
    from dsonimport import rig_tools
    rig_tools.ik_to_fk()
def do_fk_ik_angle(unused):
    from dsonimport import rig_tools
    rig_tools.fk_to_ik('distance')
def do_fk_ik_distance(unused):
    from dsonimport import rig_tools
    rig_tools.fk_to_ik('angle')
def do_coordinate_space(unused):
    from dsonimport import rig_tools
    rig_tools.switch_selected_coordinate_spaces()

def setup():
    # Work around Python forgetting to define __file__ for files run with execfile().
    filename = os.path.abspath(inspect.getsourcefile(lambda: 0))
    installation_path = os.path.dirname(filename)

    # Add this directory to the Python path.
    if installation_path not in sys.path:
        sys.path.append(installation_path)

    # Make sure the file menu is built.
    pm.mel.eval('buildFileMenu')

    pm.setParent(gMainFileMenu, menu=True)

    # In case this has already been created, remove the old one.  Maya is a little silly
    # here and throws an error if it doesn't exist, so just ignore that if it happens.
    try:
        pm.deleteUI('zDsonImport_Top', menuItem=True)
    except RuntimeError:
        pass

    # Add menu items.
    pm.menuItem('zDsonImport_Top', label='zDsonImport', subMenu=True, tearOff=True, insertAfter='exportActiveFileOptions')
    pm.menuItem('zDsonImport_Import', label='Import DUF', command=do_import)
    pm.menuItem('zDsonImport_UpdateLibrary', label='Update library', command=do_update_library)
    pm.menuItem('zDsonImport_Materials', label='Apply materials', command=do_apply_materials)
    pm.menuItem('zDsonImport_Rigging', label='Apply rigging', command=do_apply_rigging)
    pm.menuItem('zDsonImport_HIK', label='Apply HumanIK', command=do_apply_humanik)

    pm.menuItem(divider=True, label='Rig tools')
    pm.menuItem('zDsonImport_IK_FK', label='Match IK -> FK', command=do_ik_fk,
            annotation='Match IK to FK for selected rig controls.')
    pm.menuItem('zDsonImport_FK_IK1', label='Match FK -> IK (match angle)', command=do_fk_ik_angle,
            annotation='Match FK to IK for selected rig controls.')
    pm.menuItem('zDsonImport_FK_IK2', label='Match FK -> IK (match distance)', command=do_fk_ik_distance,
            annotation='Match FK to IK for selected rig controls.')
    pm.menuItem('zDsonImport_CoordinateSpace', label='Switch coordinate space', command=do_coordinate_space,
            annotation='Switch the coordinate space for the selected rig controls.')
    pm.setParent("..", menu=True)

setup()
