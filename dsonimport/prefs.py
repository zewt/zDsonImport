import errno, logging, json, os
import util

log = logging.getLogger('DSONImporter')

# The directory we store settings, cached, and other persistent files.  Is there a better
# place to put this?
storage_path = '%s/dsonimport' % os.environ['MAYA_APP_DIR']

def _prefs_file():
    return '%s/prefs.js' % storage_path

def load_prefs():
    try:
        with open(_prefs_file()) as f:
            data = f.read()
    except IOError as e:
        # Don't warn if the file doesn't exist.
        if e.errno == errno.ENOENT:
            return {}

    try:
        return json.loads(data)
    except ValueError as e:
        log.warning('Error parsing %s: %s', _prefs_file(), e)
        return {}

def save_prefs(prefs):
    util.mkdir_p(storage_path)
    data = json.dumps(prefs, indent=4)
    try:
        with open(_prefs_file(), 'w') as f:
            f.write(data)
    except IOError as e:
        log.warning('Error saving %s: %s', _prefs_file(), e)


