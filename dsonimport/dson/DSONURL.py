# A hack of urlparse to parse DSON "URLs", which are actually not valid URLs at
# all.  They can have invalid characters in the scheme, and the fragment and query
# are backwards.
from collections import namedtuple
import urllib

__all__ = ["DSONURL"]

# Characters valid in scheme names
_scheme_chars = ('abcdefghijklmnopqrstuvwxyz'
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                '0123456789'
                '+-._%/')

_cache = {}
class DSONURL(object):
    """
    >>> url = DSONURL('scheme:path#fragment?query')
    >>> str(url)
    'scheme:path#fragment?query'
    >>> url.scheme = 'test'
    >>> str(url)
    'test:path#fragment?query'

    >>> url = DSONURL('scheme:path#fragment?query')
    >>> url.scheme = 'test%'
    >>> url.escaped_scheme
    'test%25'
    >>> url.escaped_scheme = '%70 test'
    >>> url.scheme
    'p test'
    >>> url.escaped_scheme
    '%70 test'
    >>> str(url)
    '%70 test:path#fragment?query'

    # Escaping is preserved for each part.
    >>> url = DSONURL('scheme:path#fragment?query')
    >>> url.path = 'test%'
    >>> url.escaped_path
    'test%25'
    >>> url.escaped_path = '%70 test'
    >>> url.path
    'p test'
    >>> url.escaped_path
    '%70 test'
    >>> str(url)
    'scheme:%70 test#fragment?query'

    >>> url = DSONURL('scheme:path#fragment?query')
    >>> url.fragment = 'test%'
    >>> url.escaped_fragment
    'test%25'
    >>> url.escaped_fragment = '%70 test'
    >>> url.fragment
    'p test'
    >>> url.escaped_fragment
    '%70 test'
    >>> str(url)
    'scheme:path#%70 test?query'

    >>> url = DSONURL('scheme:path#fragment?query')
    >>> url.query = 'test%'
    >>> url.escaped_query
    'test%25'
    >>> url.escaped_query = '%70 test'
    >>> url.query
    'p test'
    >>> url.escaped_query
    '%70 test'
    >>> str(url)
    'scheme:path#fragment?%70 test'

    >>> url = DSONURL('sch%20eme:pa%20th#frag%20ment?que%20ry')
    >>> str(url)
    'sch%20eme:pa%20th#frag%20ment?que%20ry'
    >>> url.scheme
    'sch eme'
    >>> url.path
    'pa th'
    >>> url.fragment
    'frag ment'
    >>> url.query
    'que ry'
    >>> DSONURL('/path') == DSONURL('/path')
    True
    
    # Escaping is preserved, so these URLs aren't the same.
    >>> DSONURL('/path') == DSONURL('/%70ath')
    False
    >>> hash(DSONURL('/path')) == hash(DSONURL('/%70ath'))
    False
    """
    def __init__(self, url):
        self._cached_unquoted_scheme = None
        self._cached_unquoted_path = None
        self._cached_unquoted_fragment = None
        self._cached_unquoted_query = None

        # We don't expire cache.
        key = url
        cached = _cache.get(key)
        if cached is not None:
            self._scheme, self._path, self._fragment, self._query = cached
            return

        key = url
        self._fragment = self._query = self._scheme = ''
        i = url.find(':')
        if i > 0:
            for c in url[:i]:
                if c not in _scheme_chars:
                    break
            else:
                rest = url[i+1:]
                self._scheme, url = url[:i], rest
                self._scheme, urllib.unquote(self._scheme)


        if '?' in url:
            url, self._query = url.split('?', 1)
            self._query = self._query
        if '#' in url:
            url, self._fragment = url.split('#', 1)
            self._fragment = self._fragment
        self._path = url
        _cache[key] = self.scheme, self._path, self._fragment, self._query

    def __repr__(self):
        url = self._path
        if self.scheme:
            url = self._scheme + ':' + url
        if self._fragment:
            url = url + '#' + self._fragment
        if self._query:
            url = url + '?' + self._query
        return url

    @property
    def scheme(self):
        if self._cached_unquoted_scheme is None:
            self._cached_unquoted_scheme = urllib.unquote(self._scheme)
        
        return self._cached_unquoted_scheme

    @scheme.setter
    def scheme(self, value):
        self._cached_unquoted_scheme = value
        self._scheme = urllib.quote(value)

    @property
    def escaped_scheme(self):
        return self._scheme

    @escaped_scheme.setter
    def escaped_scheme(self, value):
        self._scheme = value
        self._cached_unquoted_scheme = None

    @property
    def path(self):
        if self._cached_unquoted_path is None:
            self._cached_unquoted_path = urllib.unquote(self._path)
        
        return self._cached_unquoted_path

    @path.setter
    def path(self, value):
        self._cached_unquoted_path = value
        self._path = urllib.quote(value)

    @property
    def escaped_path(self):
        return self._path

    @escaped_path.setter
    def escaped_path(self, value):
        self._cached_unquoted_path = None
        self._path = value

    @property
    def fragment(self):
        if self._cached_unquoted_fragment is None:
            self._cached_unquoted_fragment = urllib.unquote(self._fragment)

        return self._cached_unquoted_fragment

    @fragment.setter
    def fragment(self, value):
        self._fragment = urllib.quote(value)
        self._cached_unquoted_fragment = value

    @property
    def escaped_fragment(self):
        return self._fragment

    @escaped_fragment.setter
    def escaped_fragment(self, value):
        self._fragment = value
        self._cached_unquoted_fragment = None

    @property
    def query(self):
        if self._cached_unquoted_query is None:
            self._cached_unquoted_query = urllib.unquote(self._query)

        return self._cached_unquoted_query

    @query.setter
    def query(self, value):
        self._query = urllib.quote(value)
        self._cached_unquoted_query = value

    @property
    def escaped_query(self):
        return self._query

    @escaped_query.setter
    def escaped_query(self, value):
        self._query = value
        self._cached_unquoted_query = None

    # We're mutable but also hashable.  Don't modify a DSONURL if it's in a set
    # or the key of a dictionary.
    def __hash__(self):
        return hash(self._scheme) + hash(self._path) + hash(self._fragment) + hash(self._query)

    def __eq__(self, other):
        return isinstance(other, DSONURL) and self._scheme == other._scheme and self._path == other._path and \
            self._fragment == other.fragment and self._query == other.query


if __name__ == "__main__":
    import doctest
    doctest.testmod()

