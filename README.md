zDsonImport
-----------

Note
----

I'm releasing this in case somebody finds it useful, but I don't plan to do further
work on it.  [DazToMaya](https://www.DAZ3D.com/daz-to-maya) (untested) might be better
maintained and supported.  This is only tested with Maya 2018.

Introduction
------------

zDsonImport imports DSON files from DAZ3D into Maya.  

Some features:

- Mesh skinning
- Corrective blend shapes
- Rigged facial expressions
- Automatic rigging
- HumanIK rigging
- Character modifiers
- Cloth/prop fitting
- Rigidity groups
- Grafts
- Arnold materials
- Multiple UV sets

Automatic rigging requires the [zRigHandle plugin](https://github.com/zewt/zRigHandle).

Installation
------------

In your Maya scripts directory, eg. **Documents\maya\2018\scripts**, create userSetup.mel if
necessary, and add:

    python "execfile('C:/Users/me/Documents/maya/zDsonImport/load_zDsonImport.py')";

supplying the path to zDsonImport.  This will add menu items to the File menu.

Basic usage
-----------

All menu items are in **File > zDsonImport**.  First, select **Update library**.  This must be
done once before the first import, and if any new assets have been added to your DAZ3D
library.

Create a character (or import other assets) normally in DAZ3D, and save the result to
a .DUF file.  If you're importing a character, be sure that the DUF file contains only
a single character.

Run **Import DUF**, and select your .DUF file from the file open dialog.  Modifiers
required by the scene will be automatically selected.  You usually also want corrective
blend shapes, so click the checkbox next to "Base Correctives".  Click OK to import the
character.

**Apply materials** to create materials.

Finally, apply either custom rigging with **Apply rigging**, or a HumanIK rig with **Apply HumanIK**.

Tiled textures
--------------

If textures don't load in the viewport, they may need to be loaded manually.  Open
**Renderer > Viewport 2.0 > Options box** and click **Regenerate All UV Tile Preview Textures**.

Favorite modifiers
------------------

Facial expressions and other modifiers can be imported.  It's tedious to select these every
time on import.  Instead, mark any modifiers that you want to import as favorites in DAZ3D.
This will be saved to the .DUF file, and all favorite modifiers will be selected for import
automatically when you open the file.

Materials
---------

Viewport and Arnold materials are supported, and both will be created when materials
are created.  This gives a clean appearance in the viewport, and a best-effort attempt
at replicating the original materials in Arnold renders.  Note that the renderers
and materials in DAZ3D are different from Arnold, so this is only an approximation.

Textures will point directly to the original copy in the DAZ3D library.  In some cases
a converted copy of textures will be made, to deal with UV tiled textures.  Textures will
load with absolute paths, which can be adjusted using the Maya File Path Editor if necessary.

DSON files can use a lot of different basic material types.  Only a few of the most
common ones are supported.

Limitations
-----------

General limitations:

- Characters can only be exported.  If you want to make changes, like adding another
clothing option, re-export the asset.  There's no way to export accessories as a separate
referenced scene file.
- DSON corrective pose formulas are based on euler rotation angles.  This is an
simplistic way to do pose reading, and doesn't work well when joints are controlled
by quaternion-based constraints.  A best effort is made to deal with this.
- Imported meshes sometimes have unused components, which result in warnings when loading
the scene.

There are a lot of unsupported DSON features.  A partial list:

- "HD" modifiers.  These use an opaque file format that I haven't tried to parse.  They're
probably just displacement maps.
- Rigid follow nodes.
- Meshes with shared geometry can be instanced, but we won't assign separate materials to
each instance.  If your scene has instances of the same geometry with different materials,
turn off instancing.
- A scene can have a node in node_library with "source" pointing at a library asset, and
it'll inherit modifiers for that asset.  This seems like inheritance for assets.  Assets
are complicated enough as is, and this is rarely used.
- Conformed meshes seem to have their skinning adjusted or smoothed when they're fit to a mesh.
- Smoothing with base mesh collision detection.  This is helpful for posing, but would
probably not work for animation.
- Only X, Y and Z scale is supported, not "overall" scale.
- Dynamic properties which affect the skeleton is unsupported, since this would complicate
the rigging significantly.
- Geometry shells.  These are sometimes used for layering things like tattoos over the skin
of a character.  This is rarely used, and not a good approach for layering (this should be
done with layered textures or layered materials, not with actual geometry).

Cloth fitting
-------------

DAZ3D clothing is modelled against the base character, and a fitting algorithm is used to
fit (conform) it to characters with different shapes.  We handle this by using a wrap deformer.
DAZ3D does this dynamically, but to give a simpler final scene, we store the result of the wrap
as a blend shape and then delete the wrap, since wrap deformers can be very slow, especially
with dense meshes like mesh hair.

In addition to conforming clothing to the character, a blend shape is created for each corrective
blend shape on the character that affects the prop.  For example, if an elbow corrective shape
exists, the sleeves of a shirt will have a matching blend shape created, which will activate when
the body corrective shape activates.

CVWrap
------

Clothing props are applied using a wrap deformer.  This is only used at import time, and
the deformer is baked and removed so these deformers don't slow down the scene.  Maya's wrap
deformer gives good results, but is very slow, especially with very dense meshes such as
hair.  It can also fail silently and corrupt the mesh if it runs out of memory.

If cvWrap is installed, it can be used instead of Maya's wrap deformer.  It's much faster
and doesn't fail silently.  The results aren't always quite as good, and it can be disabled
in the options tab.

https://github.com/chadmv/cvwrap

Auto-rigging
============

Characters can either be auto-rigged directly, or HumanIK can be applied.

If any facial expressions and other modifiers are imported, they can be controlled with
nodes inside "Controls".  Once a rig is attached, facial handles will be visible in the
viewport, which can be selected for quick access.

An eye control is created.  This can be rotated, which is usually the most convenient way
to manipulate it.  It can also be translated, or parented to something the eyes should be
following.  By default, the eyes are pointing straight forward and rotate together.
"Eyes Focused" can be used to pull the eyes together or push them apart.  "Soft Eyes"
can be used to adjust how much eye motion affects the eyelids.

HumanIK
-------

If a HumanIK rig is applied, the character can be animated normally with HIK, or
motion capture data can be retargetted using HIK retargetting.

Direct rigging
--------------

A custom direct rig can be applied as an alternative to HumanIK.  

The arms and legs can be in IK or FK mode.  To switch modes, select an IK or
FK handle and adjust the "IK/FK" attribute.  This can be animated to transition
between IK and FK.  When IK/FK is 1 (IK only), only the IK handle and pole vector
control will be visible.  When it's 0 (FK only), only the FK controls will be visible.
This way, only the controls that are active are visible.

**Match IK -&gt; FK** sets the FK position for a limb to its IK position.  Select
any control on a limb first.

**Match FK -&gt; IK** sets the IK position to its FK position.  Since not every FK
pose can be exactly matched with IK, two algorithms are available.  In "distance" mode, we try
to match the elbow and hand position as closely as possible.  In "angle" mode, we try
to match their angles.

Each control has a list of coordinate spaces they can be in.  For example, by default
the head control is in the "chestUpper" coordinate space, so it follows the movements of
the upper chest.  Changing this to WorldSpace will cause the head to stay in place as the
body moves.

To cleanly change coordinate spaces, select the coordinate space to switch to in
"Change Coordinate Space" and select "Switch coordinate space".  "Coordinate Space"
will switch to the new coordinate space, and the rotation (and position for IK
controls) will be updated to keep the control in the same place.

"CustomSpace1" and "CustomSpace2" use the position of the transforms in ExtraCoordinateSpaces
as a coordinate space.  These controls can be parent constrained to other objects.

Advanced
========

Options
-------

The options panel can generally be left at its defaults.  Options include:

- Straigten pose

DAZ3D characters are in a relaxed T-pose, which is convenient for modelling, but not
what you want when you're rigging a character.  Straighten Pose will adjust the default
pose to a strict T-pose.  This is required for auto-rigging.

- Hide face rig

Hide joints associated with the face.  These are normally controlled with pose modifiers,
and since there are a huge number of joints in the face it's very busy in the viewport.

- Create twist rigs

DAZ3D characters have twist joints on the arms and legs.  "Create twist rigs" will automatically
rig these to follow the corresponding twist.  This is recommended over using HumanIK twist
joints and should be left enabled.

- Create end joints

DAZ3D character skeletons define bones, with a starting point and a length, rather than defining
joints.  "Create end joints" will create joints at the end of bones based on their length.  This
should be left enabled.

- Conform joints

Enable conforming the joints of meshes to their target.

- Conform meshes

Enable conforming meshes to their target, such as conformed clothing.

- Geometry

Import geometry.

- Materials

Create base materials and import UVs.  These materials are later replaced by the material
import.

- Morphs

Import morphs (blend shapes).

- Skinning

Import mesh skinning.

- Modifiers

Import modifier formulas.  This is the rigging that controls dynamic effects, especially
corrective blend shapes.

- Grafts

Apply grafts.

- Hide internal controls in outliner

A lot of internal helper controls are created, which can clutter the outliner.  Mark these
as "hidden in outliner".

- Bake static morphs

Bake morphs (blend shapes) in their configured pose if they're set as static rather than dynamic.
For example, this applies the main character blend shape to the mesh directly.

- Use cvWrap instead of Maya wrap if available

If cvWrap is installed, use it for mesh conforming rather than builtin Maya wrap.  cvWrap generally
doesn't give quite as good results, but it's much faster, and Maya wrap tends to fail silently
if it runs out of memory, which gives corrupted meshes.

- Mesh splitting

DAZ3D meshes tend to be one big complex mesh with a lot of materials.  In Maya it's generally preferable
to split meshes.  For example, since the eyes contain transparent elements, splitting them into a
separate mesh lets Maya know that the entire mesh doesn't need to be rendered in the viewport as
a transparent object, which improves the viewport significantly.  However, splitting incorrectly, such
as splitting arms from the body, can result in lighting seams at the boundaries.

"Smart split" attempts to split meshes correctly, splitting body parts other than skin (eg. eyes
and tongue) into their own meshes, leaving the body itself as one mesh.  Props (clothing) will be
split by material.

"Don't split props" is like "smart split", but props will be left alone.

"Don't split meshes" imports meshes as-is.

Modifier dependencies
---------------------

Modifiers can depend on other modifiers.  If a modifier is required by another modifier,
it will be selected and can't be disabled.  To see the modifiers a modifier depends on,
or is depended on by, right click it in the list and see "Requirements" and "Required by".
If a modifier is checked in dark grey, that means it's enabled as a requirement, and will
go away if it's no longer needed.  It can still be checked, turning white, which will
cause it to stay enabled even if it's no longer required, like any other modifier.

Static vs Dynamic modifiers
---------------------------

Modifiers can either be applied statically or dynamically.  Dynamic modifiers are rigged
according to their DSON formulas, and static modifiers are permanently baked into the
character at import time.

To select whether a modifier is dynamic, check or uncheck the "D" column in the modifier
list.  The default will be automatically guessed based on the type of modifier, so this
usually doesn't need to be changed.

For example, corrective blend shapes are always dynamic.  An arm bend corrective is connected
to the elbow angle.

Modifiers that change the size of the character can only be applied statically.  These
affect the shape of the skeleton.  Changing the base skeleton dynamically isn't supported,
since it would complicate the resulting scene too much and isn't useful most of the time.
For example, major character modifiers generally must be applied statically.

Caching
-------

DAZ3D caches its library using a Postgresql database, which is needed to find and load
assets quickly.  zDsonImport also caches data about the library, which is stored as
a simple JSON file in **Documents/maya/dsonimport**.  This isn't as fast, but it's simple
and doesn't have any external dependencies.

Asset references
----------------

Meshes and other resources imported into Maya have their DSON URLs and other data stored
as properties.  This allows scripts to find the source file for assets.  For example, the
material importer uses this so it can load materials.  Note that these contain absolute
paths to the imported DUF file, so they won't work if the file is moved.

Implementation notes
--------------------

DSON files use a URL-like scheme to refer to things like other assets and formulas.
This isn't correctly documented anywhere, and resolving these is fairly complex.
The strings also aren't actually URLs: they can't be correctly parsed with a regular URL
parser.  dson/DSONURL.py parses these, and dson/DSON.py handles resolving them against
each other.

Applying DSON modifiers with Maya nodes results in a huge number of nodes.  It can create
so many nodes that graphing it in the node editor fails.  Using expressions, or having a
helper plugin to reduce these expressions to one node might have given more manageable
results.  This doesn't matter if you're just using the output, but trying to make changes
to the scene isn't much fun.

DAZ3D's transforms are very different from Maya's: transforms are in world space rather
than object space, which complicates applying formulas.  This is handled by MayaWorldSpaceTransformProperty.

Some rigged controls won't do anything.  For example, tongue controls which are replaced with
a normal set of viewport handles, but the original sliders won't be removed and simply won't
do anything.

