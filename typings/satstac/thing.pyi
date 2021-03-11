"""
This type stub file was generated by pyright.
"""

from logging import getLogger

logger = getLogger(__name__)
class STACError(Exception):
    ...


class Thing(object):
    def __init__(self, data, filename=...) -> None:
        """ Initialize a new class with a dictionary """
        ...
    
    def __repr__(self):
        ...
    
    @classmethod
    def open_remote(self, url, headers=...):
        """ Open remote file """
        ...
    
    @classmethod
    def open(cls, filename):
        """ Open an existing JSON data file """
        ...
    
    def __getitem__(self, key):
        """ Get key from properties """
        ...
    
    @property
    def id(self):
        """ Return id of this entity """
        ...
    
    @property
    def path(self):
        """ Return path to this catalog file (None if no filename set) """
        ...
    
    def links(self, rel=...):
        """ Get links for specific rel type """
        ...
    
    def root(self):
        """ Get root link """
        ...
    
    def parent(self):
        """ Get parent link """
        ...
    
    def add_link(self, rel, link, type=..., title=...):
        """ Add a new link """
        ...
    
    def clean_hierarchy(self):
        """ Clean links of self, parent, and child links (for moving and publishing) """
        ...
    
    def save(self, filename=...):
        """ Write a catalog file """
        ...
    


