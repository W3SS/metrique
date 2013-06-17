#!/usr/bin/env python
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward" <cward@redhat.com>

''' "Metrique ETL" related funtions '''

import logging
logger = logging.getLogger(__name__)

CMD = 'admin/etl'


def index_warehouse(self, fields=None):
    '''
    Index particular fields of a given cube, assuming
    indexing is enabled for the cube.fields

    Paremeters
    ----------
    cube : str
        Name of the cube you want to query
    fields : str, or list of str, or str of comma-separated values
        Fields that should be indexed
    '''
    if not fields:
        fields = '__all__'

    return self._get(CMD, 'index/warehouse', cube=self.name,
                     fields=fields)


def snapshot(self, ids=None):
    '''
    Run a warehouse -> timeline (datetimemachine) snapshot
    of the data as it existed in the warehouse and dump
    copies of objects into the timeline, one new object
    per unique state in time.

    Paremeters
    ----------
    cube : str
        Name of the cube you want to query
    ids : list of cube object ids or str of comma-separated ids
        Specificly run snapshot for this list of object ids
    '''
    return self._get(CMD, 'snapshot', cube=self.name, ids=ids)


def activity_import(self, ids=None):
    '''
    Run the activity import for a given cube, if the
    cube supports it.

    Essentially, recreate object histories from
    a cubes 'activity history' table row data,
    and dump those pre-calcultated historical
    state object copies into the timeline.

    Paremeters
    ----------
    cube : str
        Name of the cube you want to query
    ids : list of cube object ids or str of comma-separated ids
        Specificly run snapshot for this list of object ids
    '''
    return self._get(CMD, 'activityimport', cube=self.name, ids=ids)


def save_objects(self, objects):
    '''
    Save a list of objects the given metrique.cube

    Paremeters
    ----------
    cube : str
        Name of the cube you want to query
    objs : list of dicts with 1+ field:value and _id defined
    '''
    return self._post(CMD, 'saveobjects', cube=self.name, objects=objects)