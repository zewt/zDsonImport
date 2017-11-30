global _config

def get(key, default=None):
    return _config.get(key, default)

def set(config):
    global _config
    _config = config

_default_config = {
}
set(_default_config)
