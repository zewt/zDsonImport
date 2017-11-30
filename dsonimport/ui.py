import glob, logging, os, sys, time, traceback
from pprint import pprint, pformat
from functools import partial
from fnmatch import fnmatch
import maya_helpers as mh
import prefs, util

import maya.OpenMayaUI as omui
from maya.OpenMaya import MGlobal
from dson import DSON, content
import Qt
from dsonimport import DSONImporter, DSONImporterConfig

log = logging.getLogger('DSONImporter')

# The directory we store settings, cached, and other persistent files.  Is there a better
# place to put this?
_storage_path = '%s/dsonimport' % os.environ['MAYA_APP_DIR']

UserSortRole = Qt.Qt.UserRole

class UIState(object):
    def __init__(self):
        # These are the modifier URLs that have been explicitly enabled by the user.  This odesn't
        # include modifiers that are enabled due to dependencies.
        self.active_modifiers = set()

        # These are the modifier URLs that have been marked dynamic by the user.
        self.dynamic_modifiers = set()

        self.prefs = prefs.load_prefs()

        self.current_modifier_info = None

    def save_prefs(self):
        """
        If true, we'll force modifiers enabled if we think they're required by another modifier.
        """
        prefs.save_prefs(self.prefs)

    @property
    def enforce_requirements(self):
        return self.prefs.get('enforce_requirements', True)

    @enforce_requirements.setter
    def enforce_requirements(self, value):
        self.prefs['enforce_requirements'] = value
        self.save_prefs()

    @property
    def hide_unused_figures(self):
        return self.prefs.get('hide_unused_figures', True)

    @hide_unused_figures.setter
    def hide_unused_figures(self, value):
        self.prefs['hide_unused_figures'] = value
        self.save_prefs()

    def update_dynamic(self, modifier):
        dynamic_check_state = self.modifier_url_dynamic_state(modifier.asset_url)
        dynamic_checked = dynamic_check_state != Qt.Qt.Unchecked

        modifier_check_state = self.modifier_url_checked_state(modifier.asset_url)
        enabled_checked = modifier_check_state != Qt.Qt.Unchecked

        # Configure a modifier as dynamic if it's both configured as dynamic and enabled.
        dynamic = dynamic_checked and enabled_checked

        if self.asset_config.get_dynamic(modifier) == dynamic:
            return False
        self.asset_config.set_dynamic(modifier, dynamic)
        return True

    def update_modifier_info(self):
        # Set the dynamic state of all modifiers to what they're set to in the UI.  This marks
        # modifiers as dynamic that were partially checked because they had to be dynamic.
        for modifier in self.asset_cache_resolver.all_modifiers.itervalues():
            self.update_dynamic(modifier)

        self.current_modifier_info = self.asset_cache_resolver.get_modifier_info(self.asset_config)

    def modifier_url_checked_state(self, modifier_url):
        """
        If a modifier is enabled by the user, return Qt.Qt.Checked.
        If a modifier is enabled because another modifier depends on it, return Qt.PartiallyChecked.
        If a modifier is disabled, return Qt.Qt.Unchecked.
        """
        enabled_by_user = modifier_url in self.active_modifiers
        if enabled_by_user:
            return Qt.Qt.Checked 

        if self.enforce_requirements:
            # See if any modifiers which depend on this modifier are active.
            required_by_urls = self.asset_cache_resolver.modifiers_required_by.get(modifier_url, set())
            for required_by_url in required_by_urls:
                if self.modifier_url_checked_state(required_by_url) != Qt.Qt.Unchecked:
                    return Qt.Qt.PartiallyChecked
                
        return Qt.Qt.Unchecked

    def modifier_url_dynamic_state(self, modifier_url):
        """
        If a modifier is dynamic because the user enabled it, return Qt.Qt.Checked.
        If a modifier is dynamic because it would have no effect if it was static, return Qt.PartiallyChecked.
        If a modifier is static, return Qt.Qt.Unchecked.
        """
        enabled_by_user = modifier_url in self.dynamic_modifiers
        if enabled_by_user:
            return Qt.Qt.Checked 

        if self.current_modifier_info is not None:
            if modifier_url in self.current_modifier_info.available_for_dynamic or modifier_url in self.current_modifier_info.unused_modifiers:
                return Qt.Qt.PartiallyChecked
        
        return Qt.Qt.Unchecked

def coalesce_messages(target, handler):
    """
    If target is called more than once per update, collect the calls and call handler
    with all of the targets.
    """
    pending_updates = []
    pending_update_ids = set()
    def proxy(item):
        if not pending_updates:
            def process_updates():
                updates = list(pending_updates)
                pending_updates[:] = []
                pending_update_ids.clear()

                try:
                    handler(updates)
                except Exception as e:
                    # Exceptions out of here can hard lock the UI, so catch them and log them.
                    log.error('%s', traceback.format_exc())

            Qt.QTimer.singleShot(0, process_updates)

        # Why is QStandardItem unhashable?
        if id(item) in pending_update_ids:
            return

        pending_update_ids.add(id(item))
        pending_updates.append(item)

    target.connect(proxy)

class Filter(Qt.QSortFilterProxyModel):
    """
    This handles a few things:

    - We receive a list of items to always filter out.  This hides items that aren't currently
    available in the view.  This allows us to update the displayed list without rebuilding it.
    - Checking an item with children checks all of its visible children.  Hidden children won't
    be changed, so you can enter a search filter, then check the parent to check only the visible
    children.
    - A parent with only some visible children checked will be partially checked.  Hidden entries
    don't affect this.  An item with one child checked can be partially checked, then change to
    fully checked if the filter changes and only displays the one child.
    """
    def __init__(self):
        super(Filter, self).__init__()
        self.modifier_urls_to_display = None
        self.filterString = None

        # We need to know if there are visible children of a group to figure out if the group
        # is visible, but filterAcceptsRow won't be called in the right order.  Cache the results,
        # so we don't evaluate nodes repeatedly.
        self.filter_cache = {}

        self.last_seen_checked = {}

    def setSourceModel(self, source):
        # We don't expect to be set twice.
        assert self.sourceModel() is None

        super(Filter, self).setSourceModel(source)

        coalesce_messages(self.sourceModel().itemChanged, self._changed)

    def invalidateFilter(self):
        self.filter_cache = {}
        return Qt.QSortFilterProxyModel.invalidateFilter(self)

    def setFilterFixedString(self, s, *args, **kwargs):
        self.filter_cache = {}
        self.filterString = s
        return Qt.QSortFilterProxyModel.setFilterFixedString(self, s, *args, **kwargs)

    def _filterAcceptsRow(self, sourceRow, sourceParent):
        src_index = self.sourceModel().index(sourceRow, 0, sourceParent)
        item = self.sourceModel().itemFromIndex(src_index)

        if getattr(item, 'is_group', False):
            # Show groups if there are any visible children.
            num_children = self.sourceModel().rowCount(src_index)
            has_any_visible_children = False
            for row in xrange(num_children):
                child = src_index.child(row, 0)
                child_row = child.row()
                if self.filterAcceptsRow(child_row, src_index):
                    has_any_visible_children = True
                    break

            if not has_any_visible_children:
                return False

            return True

        modifier = getattr(item, 'modifier', None)
        if modifier is not None:
            # Hide modifiers that are explicitly hidden via self.modifier_urls_to_display.  These
            # are modifiers that aren't available with the current configuration, but which might
            # become available if dynamic flags are changed.
            if self.modifier_urls_to_display is not None and modifier.asset_url not in self.modifier_urls_to_display:
                return False

            if self.filterString is not None:
                return modifier.substring_matches(self.filterString)

        return super(Filter, self).filterAcceptsRow(sourceRow, sourceParent)

    def filterAcceptsRow(self, sourceRow, sourceParent):
        key = (sourceRow, sourceParent)
        cached_result = self.filter_cache.get(key)
        if cached_result is not None:
            return cached_result

        result = self._filterAcceptsRow(sourceRow, sourceParent)

        self.filter_cache[key] = result
        return result

    def _changed(self, items):
        # This is received when a source item changes.  If an item changes, all of its parent items
        # may change, so signal them too.
        parents = {}
        for item in items:
            checked = item.data(Qt.Qt.CheckStateRole)

            parent = item.parent()
            while parent is not None:
                if not (parent.flags() & Qt.Qt.ItemIsTristate):
                    break
                
                parents[id(parent)] = parent
                parent = parent.parent()

        for parent in parents.itervalues():
            # XXX: What's the third parameter that QT5 added?
            if MGlobal.apiVersion() >= 201700:
                self.dataChanged.emit(parent, parent, [])
            else:
                self.dataChanged.emit(parent, parent)
      
    def setData(self, index, value, role):
        result = Qt.QSortFilterProxyModel.setData(self, index, value, role)

        if role == Qt.Qt.CheckStateRole:
            # If an entry is checked or unchecked and it has children, propagate that state to its children.
            # Note that we're working on the proxied indexes, so we'll only change children who are visible
            # and not filtered out.
            for row in xrange(self.rowCount(index)):
                child = index.child(row, 0)
                super(Qt.QSortFilterProxyModel, self).setData(child, value, Qt.Qt.CheckStateRole)

        return result

    def _show_partially_checked(self, index):
        if not (self.flags(index) & Qt.Qt.ItemIsTristate):
            return None

        # If we're a leaf, we just use our own state.
        if self.rowCount(index) == 0:
            return None

        any_partially_checked = False
        all_checked = True
        for row in xrange(self.rowCount(index)):
            # Partially checked children mean different things depending on if they're leaves or
            # not.  If a child has children of its own, it works like us: partially checked means
            # something inside it is checked.  If a child is a leaf and it's partially checked,
            # it actually means it's checked due to a dependency on another item.  In the former
            # case we should show ourselves as partially checked too.  In the latter case, we
            # should act as if it's fully checked and show ourselves as fully checked.
            child = index.child(row, 0)
            
            state = self.data(child, Qt.Qt.CheckStateRole)
            has_children = self.rowCount(child) > 0

            if not has_children and state == Qt.Qt.PartiallyChecked:
                state = Qt.Qt.Checked

            if state == Qt.Qt.Checked:
                any_partially_checked = True
            else:
                all_checked = False
        
        if all_checked:
            return Qt.Qt.Checked
        if any_partially_checked:
            return Qt.Qt.PartiallyChecked
        return Qt.Qt.Unchecked

    def data(self, index, role):
        if role == Qt.Qt.CheckStateRole:
            partially_checked = self._show_partially_checked(index)
            if partially_checked is not None:
                return partially_checked
        
        return Qt.QSortFilterProxyModel.data(self, index, role)

    def lessThan(self, index1, index2):
        def data(item, col):
            return item.sibling(item.row(), col).data(Qt.Qt.DisplayRole)

        # Sort by group first, name second.  Add an arbitrary tiebreaker last: for some reason
        # this isn't a stable sort, so if we have ties items will shift around as they're changed.
        data1 = [data(index1, col) for col in (2, 0)]
        data1.append(index1.sibling(index1.row(), 0).data(UserSortRole))
        data2 = [data(index2, col) for col in (2, 0)]
        data2.append(index2.sibling(index2.row(), 0).data(UserSortRole))
        return data1 < data2

class ModifierListItem(Qt.QStandardItem):
    def __init__(self, modifier, ui_state, main_window):
        super(ModifierListItem, self).__init__(modifier.label)

        self.ui_state = ui_state
        self.main_window = main_window

        self.modifier = modifier
        self.path = modifier.absolute_path

        self.active_list = self.ui_state.active_modifiers

        self.view = None

    def set_view(self, view):
        self.view = view

    def available_modifiers_required(self):
        """
        Return the ModifierAssets that we require, which are currently available to select.

        We may still be available if we require a modifier which is unavailable.  A corrective
        pose in a base figure may require a corrective pose which is only available in a morph
        figure.  If the morph figure is enabled then its corrective morph will be required, but
        if it's not, the base corrective will be used on its own.
        """
        result = []

        required_urls = self.ui_state.asset_cache_resolver.modifiers_required.get(self.modifier.asset_url, [])

        for modifier_url in required_urls:
            if modifier_url in self.ui_state.current_modifier_info.unavailable_modifiers:
                continue
            modifier_data = self.ui_state.asset_cache_resolver.all_modifiers[modifier_url]
            result.append(modifier_data)
        return result
        
    def available_modifiers_required_by(self):
        """
        Return the ModifierAssets that we require us, which are currently available to select.
        """
        result = []
        required_by_urls = self.ui_state.asset_cache_resolver.modifiers_required_by.get(self.modifier.asset_url, [])
        for modifier_url in required_by_urls:
            if modifier_url in self.ui_state.current_modifier_info.unavailable_modifiers:
                continue
            modifier_data = self.ui_state.asset_cache_resolver.all_modifiers[modifier_url]
            result.append(modifier_data)
        return result

    def data(self, role):
        if role == Qt.Qt.CheckStateRole:
            return self.ui_state.modifier_url_checked_state(self.modifier.asset_url)

        if role == UserSortRole:
            return id(self)

        return super(ModifierListItem, self).data(role)

    def setData(self, value, role):
        if role == Qt.Qt.CheckStateRole:
            # The user has selected or deselected a modifier to be used.
            any_changed = False

            state = Qt.Qt.CheckState(value)

            # PartiallyChecked means the entry has been forced on by another modifier depending
            # on it.  Don't change active_modifiers in this case, since it's a dependency that
            # changed it and not the user.
            if state != Qt.Qt.PartiallyChecked:
                checked = state == Qt.Qt.Checked
                if checked != (self.modifier.asset_url in self.active_list):
                    any_changed = True
                    if checked:
                        self.active_list.add(self.modifier.asset_url)
                    else:
                        self.active_list.discard(self.modifier.asset_url)

            if any_changed:
                self.emitDataChanged()

                # If we've changed, the value of our requirements may have changed too.
                required_urls = self.ui_state.asset_cache_resolver.modifiers_required.get(self.modifier.asset_url, [])
                for required_url in required_urls:
                    modifier_item = self.ui_state.modifier_items_by_url[required_url]
                    # XXX: this won't invoke setData on the dependency, so it won't recursively emit its dependencies
                    modifier_item.emitDataChanged()

            if self.view is not None:
                self.view.checkbox_toggled(self, value)

            return
        
        super(ModifierListItem, self).setData(value, role)

    def __repr__(self):
        return 'ModifierListItem(%s)' % self.modifier

class DynamicCheckItem(Qt.QStandardItem):
    def __init__(self, modifier, ui_state, main_window):
        super(DynamicCheckItem, self).__init__('')
           
        self.modifier = modifier
        self.ui_state = ui_state
        self.main_window = main_window
           
        self.setCheckable(True)
        self.setEditable(False)
        self.view = None

    def set_view(self, view):
        self.view = view

    def data(self, role):
        if role == Qt.Qt.CheckStateRole:
            return self.ui_state.modifier_url_dynamic_state(self.modifier.asset_url)

        return super(DynamicCheckItem, self).data(role)

    def setData(self, value, role):
        if role == Qt.Qt.CheckStateRole:
            log.debug('set checked')
            modifier_url = self.modifier.asset_url
            # The user has selected or deselected a modifier to be used.
            any_changed = False

            state = Qt.Qt.CheckState(value)

            checked = state == Qt.Qt.Checked
            if checked != (modifier_url in self.ui_state.dynamic_modifiers):
                any_changed = True
                if checked:
                    self.ui_state.dynamic_modifiers.add(modifier_url)
                else:
                    self.ui_state.dynamic_modifiers.discard(modifier_url)

            if any_changed:
                self.emitDataChanged()

            if self.view is not None:
                self.view.checkbox_toggled(self, value)

            return

        super(DynamicCheckItem, self).setData(value, role)

class ModifierList(object):
    """
    The modifier list logic shared between the modifier and dynamic tabs.
    """
    def __init__(self, ui_state, parent, main_window):
        super(ModifierList, self).__init__()

        self.ui_state = ui_state
        self.main_window = main_window

        from qtpy import modifier_list
        self.ui = modifier_list.Ui_Form()
        self.ui.setupUi(parent)

        if MGlobal.apiVersion() >= 201700:
            # XXX 2017 crashes when we do this.
            pass
            #self.ui.treeView.header().setSectionResizeMode(0, Qt.QHeaderView.Stretch)
            #self.ui.treeView.header().setSectionResizeMode(1, Qt.QHeaderView.ResizeToContents)
            #self.ui.treeView.header().setSectionResizeMode(2, Qt.QHeaderView.Interactive)
        else:
            self.ui.treeView.header().setResizeMode(0, Qt.QHeaderView.Stretch)
            self.ui.treeView.header().setResizeMode(1, Qt.QHeaderView.ResizeToContents)
            self.ui.treeView.header().setResizeMode(2, Qt.QHeaderView.Interactive)
        
        self.model = Qt.QStandardItemModel()
        self.model.setColumnCount(3)
        self.model.setHeaderData(0, Qt.Qt.Horizontal, 'Item', Qt.Qt.DisplayRole)
        self.model.setHeaderData(1, Qt.Qt.Horizontal, 'D', Qt.Qt.DisplayRole)
        self.model.setHeaderData(2, Qt.Qt.Horizontal, 'Group', Qt.Qt.DisplayRole)

        self.model_filter = Filter()
        self.model_filter.setSourceModel(self.model)
        self.ui.treeView.setModel(self.model_filter)
        self.ui.treeView.setSortingEnabled(True)

        self.model_filter.setFilterCaseSensitivity(Qt.Qt.CaseInsensitive)

        self.ui.searchBox.textChanged.connect(self.model_filter.setFilterFixedString)
        self.ui.searchBox.textChanged.connect(lambda text: self.ui.clearButton.setEnabled(len(text)))
        self.ui.clearButton.setEnabled(False)
        self.ui.clearButton.clicked.connect(lambda: self.ui.searchBox.clear())

        self.ui.treeView.setSelectionMode(Qt.QAbstractItemView.ExtendedSelection)
        self.ui.treeView.setContextMenuPolicy(Qt.Qt.CustomContextMenu)        
        self.ui.treeView.customContextMenuRequested.connect(lambda position: self.open_menu(self.ui.treeView, position))

    def open_menu(self, widget, position):
        idx = self.ui.treeView.indexAt(position)
        if idx.model() is not None:
            idx = idx.model().mapToSource(idx)
            item = idx.model().itemFromIndex(idx)
        else:
            item = None

        menu = Qt.QMenu()

        self.create_menu(menu, item)
        menu.exec_(self.ui.treeView.viewport().mapToGlobal(position))

    def create_menu(self, menu, item):
        enforce_requirements = Qt.QAction('Enforce requirements', menu, checkable=True)
        enforce_requirements.setChecked(self.ui_state.enforce_requirements)
        enforce_requirements.triggered.connect(self.toggle_enforce_requirements)
        menu.addAction(enforce_requirements)

        hide_unused_figures = Qt.QAction('Hide unused figures', menu, checkable=True)
        hide_unused_figures.setChecked(self.ui_state.hide_unused_figures)
        hide_unused_figures.triggered.connect(self.toggle_hide_unused_figures)
        menu.addAction(hide_unused_figures)

        menu.addSeparator()

        if item is None:
            return

        if hasattr(item, 'path'):
            menu.addAction('Browse to file', lambda: util.browse_to_file(item.path))

        if isinstance(item, ModifierListItem):
            modifier_url = item.modifier.asset_url

            # Create a submenu showing which modifiers this modifier requires.
            requirements_menu = Qt.QMenu()
            requirements_menu.setTitle('Requirements')
            menu.addMenu(requirements_menu)

            modifiers_required = item.available_modifiers_required()
            modifiers_required_by = item.available_modifiers_required_by()

            if not modifiers_required:
                requirements_menu.setEnabled(False)

            for modifier_data in modifiers_required:
                requirements_menu.addAction(modifier_data.label, partial(self.select_modifier, modifier_data))

            # Create a submenu showing which modifiers require this one.
            required_by_menu = Qt.QMenu()
            required_by_menu.setTitle('Required by')
            menu.addMenu(required_by_menu)

            if not modifiers_required_by:
                required_by_menu.setEnabled(False)

            for modifier_data in modifiers_required_by:
                required_by_menu.addAction(modifier_data.label, partial(self.select_modifier, modifier_data))

    def select_modifier(self, modifier):
        modifier_item = self.ui_state.modifier_items_by_url[modifier.asset_url]

        # Select a modifier chosen from the context menu.  We have to map back to the filter first.
        index = self.model.indexFromItem(modifier_item)
        index = self.model_filter.mapFromSource(index)
        self.ui.treeView.selectionModel().select(index, Qt.QItemSelectionModel.ClearAndSelect)
        self.ui.treeView.scrollTo(index)

    def toggle_enforce_requirements(self):
        # We should have to tell the model that everyone's checked state may have changed,
        # but it doesn't seem to be necessary.
        self.ui_state.enforce_requirements = not self.ui_state.enforce_requirements

    def toggle_hide_unused_figures(self):
        self.ui_state.hide_unused_figures = not self.ui_state.hide_unused_figures
        self.main_window.refresh()

pref_booleans = {
    # Settings for internal controls.  These are implementation details that usually
    # just clutter the outliner.  We set a dimmer color, and hide them by default.  You
    # can see these without recreating the scene by selecting "Ignore Hidden In Outliner"
    # in the outliner menu.
    'internal_control_outliner_color': (.7,.6,0),
    'internal_control_hide_in_outliner': True,

    # Setting for joints that are at least partially keyed.  These might have things
    # that the user might want to control on them, and more importantly might have children
    # that the user might want to see (hidden in outliner hides the whole hierarchy), but
    # which are often just driven controls.  We set a different color to make it easier
    # to skim over these (or to find them).
    'driven_control_outliner_color': (.6,.75,.3),

    # Most figures are in a relaxed T-pose on export.  If true, they'll be adjusted
    # to a full T-pose, which is easier for rigging.
    'straighten_pose': True,

    # If true, hide the twist joints on arms and legs and replace them with a twist
    # rig to make them act like a simple joint.
    'create_twist_rigs': True,

    # If true, a HumanIK rig will be created.
    'create_hik_rig': True,

    # If true, hide the face joints by default.  Controlling them directly is not
    # very useful.
    'hide_face_rig': True,
    
    # If true, static blend shapes (blend shapes not connected to controls) will be baked
    # to the mesh.  This bakes character shapes to the base mesh, which allows the blendShape
    # deformer to be disabled and blend shapes to be solo'd without the whole character
    # reverting to the base mesh and everything breaking.
    'bake_static_morphs': True,

    # If available, use cvWrap instead of the built-in wrap deformer for conforming meshes.
    # The results are sometimes not quite as good, but it's significantly faster and more
    # reliable.
    'use_cvwrap': True,

    # These will disable parts of the import.  This is mostly for debugging, since
    # disabling things can cause others to behave unexpectedly.  For example, if you
    # disable morphs on a morphed character, the skeleton adjustments will still be
    # applied.
    'geometry': True,
    'morphs': True,
    'skinning': True,
    'modifiers': True,
    'grafts': True,

    # This only disables auto-parenting of conforming joints, not graft conforming.
    'conform_joints': True,

    # This only disables creating wraps for non-grafted geometry.  Grafted geometry
    # is wrapped separately.
    'conform_meshes': True,
    'materials': True,
    
    'end_joints': True,

    # 0: Smart.  Use the default splitting mode, and try to split meshes across materials
    # where it makes sense.  Non-skin body parts will be separated, props will be separated,
    # and all conforming meshes will be separated.
    #
    # 1: Never split.  Each mesh will be output as a single mesh, possibly with lots of
    # materials assigned.  This is mostly for debugging.
    #
    # 2: Don't split props and conforming meshes.  Body parts will still be split.  This
    # is useful if splitting clothing is making blend shapes harder to work with.  In
    # general, splitting props and clothing is useful and gives more manageable meshes.
    #
    # 3: Don't split meshes.  Each mesh will be output into a single matching mesh, possibly
    # with lots of materials assigned.  This is mostly for debugging.
    'mesh_split_mode': 0,
}

class PrefsTab(object):
    def __init__(self, parent, ui_state):
        self.ui_state = ui_state

        from qtpy import prefs_tab
        self.ui = prefs_tab.Ui_Form()
        self.ui.setupUi(parent)
        self.load_from_state()
        self.save_to_state()

        for name, default in pref_booleans.iteritems():
            widget = getattr(self.ui, name, None)
            if widget is None:
                continue

            if isinstance(widget, Qt.QComboBox):
                widget.currentIndexChanged.connect(self.save_to_state)
            elif isinstance(default, bool):
                widget.stateChanged.connect(self.save_to_state)
            else:
                raise RuntimeError('Unknown prefs type for %s: %s', name, default)

    def load_from_state(self):
        for name, default in pref_booleans.iteritems():
            value = self.ui_state.prefs.get(name, default)
            widget = getattr(self.ui, name, None)
            if widget is None:
                continue

            if isinstance(widget, Qt.QComboBox):
                assert isinstance(default, int)
                widget.setCurrentIndex(value)
                
            elif isinstance(default, bool):
                widget.setChecked(value)
            else:
                raise RuntimeError('Unknown prefs type for %s: %s' % (name, default))

    def save_to_state(self):
        for name, default in pref_booleans.iteritems():
            widget = getattr(self.ui, name, None)
            if widget is None:
                # This is a preference that we don't have a UI widget for yet.
                self.ui_state.prefs[name] = default
                continue

            if isinstance(widget, Qt.QComboBox):
                value = widget.currentIndex()
            elif isinstance(default, bool):
                value = widget.isChecked()
            else:
                raise RuntimeError('Unknown prefs type for %s: %s', name, default)
            self.ui_state.prefs[name] = value

        self.ui_state.save_prefs()

class ControlMainWindow(Qt.QDialog):
    def __init__(self, scene_path, parent):
        super(ControlMainWindow, self).__init__(parent)
        self.setWindowFlags(Qt.Qt.Tool)

        self.ui_state = UIState()
        self.ui_state.scene_path = scene_path

        from qtpy import modifier_list, import_window, prefs_tab
        reload(modifier_list)
        reload(import_window)
        reload(prefs_tab)

        self.ui = import_window.Ui_Dialog()
        self.ui.setupUi(self)

        self.modifier_widget = ModifierList(self.ui_state, self.ui.modifiers, self)
        coalesce_messages(self.modifier_widget.model.itemChanged, self.dynamic_changed)

        self.prefs_tab = PrefsTab(self.ui.prefs, self.ui_state)

        self.ui.buttonBox.accepted.connect(self.run_import)

        # Set this explicitly.  Designer saves the last tab selected when editing for some reason.
        self.ui.tabs.setCurrentWidget(self.ui.modifiers)

        style = r'''
        /* Maya's checkbox style makes the checkbox invisible when it's deselected,
         * which makes it impossible to tell that there's even a checkbox there to
         * click.  Adjust the background color to fix this. */
        QTreeView::indicator:unchecked {
            background-color: #000;
        }
        '''
        self.setStyleSheet(style)

        self.load()

    def load(self):
        env = DSON.DSONEnv()

        progress = mh.ProgressWindowMaya()
        progress.show('Loading...', 3)

        try:
            progress.set_main_progress('Loading scene...')
            
            env.load_user_scene(self.ui_state.scene_path)
            DSON.Helpers.recursively_instance_assets(env.scene)
            DSON.Helpers.create_fid_modifiers(env)

            self.ui_state.asset_config = content.AssetConfig()

            progress.set_main_progress('Loading asset cache...')

            cache = content.AssetCache('%s/content.js' % prefs.storage_path)

            progress.set_main_progress('Resolving assets...')
            self.ui_state.asset_cache_resolver = cache.get_resolver(env)
        finally:
            progress.hide()

        self.setup_modifiers()

        # Get the list of modifier URLs that are marked as favorites.
        self.ui_state.favorite_modifiers = self.ui_state.asset_cache_resolver.get_favorite_modifiers_in_scene(env)
        log.debug('Favorite modifiers: %s', pformat(self.ui_state.favorite_modifiers))

        # Set which modifiers are set dynamic by default.  Note that we're not necessarily enabling them
        # here, we're just selecting which modifiers are already marked dynamic if they're enabled.  For
        # example, if you enable a corrective morph, you probably want it to be dynamic.
        for modifier in self.ui_state.asset_cache_resolver.all_modifiers.itervalues():
            make_dynamic = False

            if self.ui_state.asset_config.is_corrective_morph(modifier) and not self.ui_state.asset_config.is_figure_morph(modifier):
                make_dynamic = True

            # If a modifier is a favorite, always make it dynamic.
            if modifier.asset_url in self.ui_state.favorite_modifiers:
                make_dynamic = True

            if not make_dynamic:
                continue
            self.ui_state.dynamic_modifiers.add(modifier.asset_url)

        # Do an extra update, to get the initial used_modifiers value.
        self.ui_state.update_modifier_info()

        # Select modifiers that are active in the scene by default.  This includes modifiers that
        # are used in the scene, and ones that are marked as favorites.
        self.ui_state.active_modifiers.update(self.ui_state.current_modifier_info.used_modifiers.keys())
        self.ui_state.active_modifiers.update(self.ui_state.favorite_modifiers)

        self.refresh()

        self.modifier_widget.model_filter.sort(0)
        self.modifier_widget.model_filter.setDynamicSortFilter(True)

        self.modifier_widget.ui.treeView.header().resizeSections(Qt.QHeaderView.ResizeToContents)

    def refresh(self):
        self.ui_state.update_modifier_info()

        displayed_modifiers = set(self.ui_state.current_modifier_info.available_modifiers.keys())

        if self.ui_state.hide_unused_figures:
            # Filter out figure morphs that aren't used in the scene.  This would show all figures that the
            # user has for this base mesh, and enabling them is generally not useful since we don't support
            # dynamic skeleton adjustments.
            for modifier in self.ui_state.asset_cache_resolver.all_modifiers.itervalues():
                if not self.ui_state.asset_config.is_figure_morph(modifier):
                    continue
                if modifier.asset_url in self.ui_state.current_modifier_info.used_modifiers:
                    continue
                if modifier.asset_url in self.ui_state.favorite_modifiers:
                    continue
                if modifier.asset_url in displayed_modifiers:
                    displayed_modifiers.remove(modifier.asset_url)
        
        self.modifier_widget.model_filter.modifier_urls_to_display = displayed_modifiers
        self.modifier_widget.model_filter.invalidateFilter()

    def setup_modifiers(self):
        groups = {}

        self.ui_state.modifier_items_by_url = {}
        for modifier in sorted(self.ui_state.asset_cache_resolver.all_modifiers.itervalues(), key=lambda item: item.relative_path.lower()):
            path = modifier.relative_path
            parent_dir = os.path.dirname(modifier.relative_path)
            parent_group = groups.get(parent_dir)
            if parent_group is None:
                parent_group = Qt.QStandardItem(parent_dir)
                parent_group.is_group = True
                parent_group.modifier = None
                parent_group.setCheckable(True)
                parent_group.setTristate(True)
                parent_group.setEditable(False)
                parent_group.path = os.path.dirname(modifier.absolute_path)
                groups[parent_dir] = parent_group
                self.modifier_widget.model.appendRow(parent_group)

                # This doesn't work, because QT is bad.
                # index = self.modifier_widget.model_filter.mapFromSource(parent_group.index())
                # self.modifier_widget.ui.treeView.setFirstColumnSpanned(index.row(), index.parent(), True)

            item = ModifierListItem(modifier, self.ui_state, self)
            item.setCheckable(True)
            item.set_view(self.modifier_widget.ui.treeView)
            item.setTristate(True)
            item.setEditable(False)

            dynamic_check = DynamicCheckItem(modifier, self.ui_state, self)
            dynamic_check.set_view(self.modifier_widget.ui.treeView)

            item_group = Qt.QStandardItem(modifier.group)
            item_group.setCheckable(False)
            item_group.setEditable(False)

            parent_group.appendRow([item, dynamic_check, item_group])

            self.ui_state.modifier_items_by_url[modifier.asset_url] = item

        self.modifier_widget.ui.treeView.setFirstColumnSpanned(0, self.modifier_widget.ui.treeView.rootIndex(), True)
        self.modifier_widget.ui.treeView.setFirstColumnSpanned(1, self.modifier_widget.ui.treeView.rootIndex(), True)

    def dynamic_changed(self, items):
        # Changing which modifiers are dynamic can affect which other modifiers are available.  Update
        # the AssetConfig and refresh the list if any entries have changed.
        any_changed = False
        for item in items:
            if item.modifier is None:
                continue
            if self.ui_state.update_dynamic(item.modifier):
                any_changed = True

        if any_changed:
            self.refresh()

    def run_import(self):
        config = DSONImporterConfig()
        config.asset_cache_results = self.ui_state.current_modifier_info
        config.prefs = self.ui_state.prefs

        # Find all of the modifiers that should be loaded by the import.
        config.modifier_asset_urls = set()

        for modifier in self.ui_state.current_modifier_info.all_modifiers.itervalues():
            modifier_url = modifier.asset_url
            if modifier_url in self.ui_state.current_modifier_info.unavailable_modifiers:
                continue

            modifier_state = self.ui_state.modifier_url_checked_state(modifier_url)
            if modifier_state == Qt.Qt.Unchecked:
                continue

            config.modifier_asset_urls.add(modifier_url)

        # Set the dynamic state of all modifiers to what they're set to in the UI.  This marks
        # modifiers as dynamic that were partially checked because they had to be dynamic.
#        for modifier in self.ui_state.current_modifier_info.all_modifiers.itervalues():
#            dynamic = self.ui_state.modifier_url_dynamic_state(modifier.asset_url)
#            self.ui_state.asset_config.set_dynamic(modifier, dynamic)

        log.debug('Asset URLs to load:')
        log.debug(pformat(config.modifier_asset_urls))
       
        importer = DSONImporter(config)
        importer.load_scene(self.ui_state.scene_path)
        importer.run_import()
 
def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0

def import_file(path):
    main_window_ptr = omui.MQtUtil.mainWindow()
    main_window = Qt.wrapInstance(long(main_window_ptr), Qt.QWidget)

    window = ControlMainWindow(path, parent=main_window)
    window.show()
    Qt.QApplication.setActiveWindow(window)

def go():
    mh.setup_logging()
    qt_path = os.path.dirname(__file__) + '/qt/'
    qtpy_path = os.path.dirname(__file__) + '/qtpy/'
    for fn in os.listdir(qt_path):
        if not fnmatch(fn, '*.ui'):
            continue

        input_file = qt_path + fn
        output_file = qtpy_path + fn.replace('.ui', '.py')
        if mtime(input_file) < mtime(output_file):
            continue

        with open(input_file) as input:
            with open(output_file, 'w') as output:
                Qt.pysideuic.compileUi(input, output)

    main_window_ptr = omui.MQtUtil.mainWindow()
    main_window = Qt.wrapInstance(long(main_window_ptr), Qt.QWidget)

    current_prefs = prefs.load_prefs()

    picker = Qt.QFileDialog(main_window, caption='Open DSON scene', filter='DUF (*.duf)')

    # Restore the last directory the user was in.
    if 'last_path' in current_prefs:
        picker.setDirectory(current_prefs['last_path'])

    def import_file_queued(path):
        # Save the directory the user was in.
        current_prefs['last_path'] = picker.directory().absolutePath()
        prefs.save_prefs(current_prefs)

        # Wait for an event loop cycle, or the open dialog will stay on screen while we load
        # data.
        def run_import():
            import_file(path)
        Qt.QTimer.singleShot(0, run_import)

    picker.fileSelected.connect(import_file_queued)
    picker.setAcceptMode(Qt.QFileDialog.AcceptOpen)
    picker.setFileMode(Qt.QFileDialog.ExistingFile)
    picker.show()

def refresh_cache():
    """
    Refresh the asset cache.  This takes a while on larger libraries, so we only do it on request.
    """
    env = DSON.DSONEnv()

    progress = mh.ProgressWindowMaya()
    progress.show('Loading...', 1)
    progress.set_main_progress('Refreshing asset cache...')

    try:
        cache = content.AssetCache('%s/content.js' % prefs.storage_path)
        cache.scan(env, progress)
    finally:
        progress.hide()


