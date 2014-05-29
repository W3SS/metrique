#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward" <cward@redhat.com>

from copy import deepcopy
import os

from utils import set_env
from metrique.utils import debug_setup

logger = debug_setup('metrique', level=10, log2stdout=True, log2file=False)

env = set_env()
exists = os.path.exists

testroot = os.path.dirname(os.path.abspath(__file__))
cubes = os.path.join(testroot, 'cubes')
fixtures = os.path.join(testroot, 'fixtures')
cache_dir = env['METRIQUE_CACHE']


def test_api():
    from metrique import MetriqueContainer, MetriqueObject
    from metrique.utils import utcnow, remove_file, dt2ts

    _expected_db_path = os.path.join(cache_dir, 'container_test.sqlite')
    remove_file(_expected_db_path)

    now = utcnow()
    a = {'_oid': 1, 'col_1': 1, 'col_2': now, '_start': now}
    b = {'_oid': 2, 'col_1': 2, 'col_2': now, '_start': now}
    ma = MetriqueObject(**a)
    mb = MetriqueObject(**b)
    objs_list = [a, b]
    objs_dict = {'1': a, '2': b}
    r_objs_dict = {'1': ma, '2': mb}

    # must pass in name
    try:
        MetriqueContainer()
    except TypeError:
        pass

    # must pass in non-null value for name
    try:
        MetriqueContainer(name=None)
    except RuntimeError:
        pass

    # check various forms of passing in objects results in expected
    # container contents
    name = 'container_test'
    c = MetriqueContainer(name=name)
    assert c == {}
    assert MetriqueContainer(name=name, objects=c) == {}
    assert MetriqueContainer(name=name, objects=objs_list) == r_objs_dict
    assert MetriqueContainer(name=name, objects=objs_dict) == r_objs_dict
    mc = MetriqueContainer(name=name, objects=objs_list)
    assert MetriqueContainer(name=name, objects=mc) == r_objs_dict

    # setting version should result in all objects added having that version
    # note: version -> _v in MetriqueObject
    assert mc._version == 0
    assert mc['1']['_v'] == 0
    mc = MetriqueContainer(name=name, objects=objs_list, _version=3)
    assert mc._version == 3
    assert mc['1']['_v'] == 3

    # setting converts key to _id of value after being passed
    # through MetriqueObject(); notice key int(5) -> str('5')
    mc[5] = {'_oid': 5}
    assert mc['5']['_oid'] == 5

    # should have 3 objects, first two, plus the last one
    assert len(mc) == 3
    assert len(mc.values()) == 3
    assert sorted(mc._ids) == ['1', '2', '5']

    assert sorted(mc._oids) == [1, 2, 5]
    try:
        mc.ls()
    except NotImplementedError:
        pass

    mc.extend([{'_oid': 6}, {'_oid': 7}])
    assert sorted(mc._oids) == [1, 2, 5, 6, 7]

    mc.add({'_oid': 8, '_start': now, '_end': utcnow(), 'col_1': True})
    mc.add({'_oid': 8, '_end': None, 'col_1': False})
    assert sorted(mc._oids) == [1, 2, 5, 6, 7, 8]

    r = mc.filter(where={'_oid': 8})
    assert len(r) == 2
    assert sorted(mc._oids) == [1, 2, 5, 6, 7, 8]

    assert sorted(mc._oids) == [1, 2, 5, 6, 7, 8]
    mc.pop('7')
    assert sorted(mc._oids) == [1, 2, 5, 6, 8]
    mc.pop(6)
    assert sorted(mc._oids) == [1, 2, 5, 8]
    del mc[5]
    assert sorted(mc._oids) == [1, 2, 8]

    assert '1' in mc

    mc.clear()
    assert mc == {}

    mc = MetriqueContainer(name=name, objects=objs_list)
    assert mc.df() is not None

    # local persistence; filter method queries .objects buffer
    # .persist dumps data to proxy db; but leaves the data in the buffer
    # .flush dumps data and removes all objects dumped
    # count queries proxy db
    mc = MetriqueContainer(name=name, objects=objs_list)
    _store = deepcopy(mc.store)

    assert len(mc.filter({'col_1': 1})) == 1
    _ids = mc.persist()
    assert _ids == ['1', '2']
    assert mc.store == _store
    assert len(mc.filter({'col_1': 1})) == 1
    assert mc.count('col_1 == 1') == 1
    assert mc.count() == 2

    # persisting again shouldn't result in new rows
    _ids = mc.persist()
    assert _ids == ['1', '2']
    assert mc.store == _store
    assert len(mc.filter({'col_1': 1})) == 1
    assert mc.count('col_1 == 1') == 1
    assert mc.count() == 2

    # flushing now shouldn't result in new rows; but store should be empty
    _ids = mc.flush()
    assert _ids == ['1', '2']
    assert mc.store == {}
    assert len(mc.filter({'col_1': 1})) == 0
    assert mc.count('col_1 == 1') == 1
    assert mc.count() == 2

    # adding the same object shouldn't result in new rows
    a.update({'col_1': 42})
    mc.add(a)
    assert len(mc.filter({'col_1': 1})) == 0
    assert len(mc.filter({'col_1': 42})) == 1
    _id = '1:%s' % dt2ts(mc.filter({'col_1': 42})[0].get('_start'))
    _ids = mc.flush()
    logger.debug('**** %s' % '\n'.join(map(str, mc.find(date='~', raw=True))))
    assert mc.count(date='~') == 3
    assert mc.count(date=None) == 2
    assert mc.count('col_1 == 1', date=None) == 0
    assert mc.count('col_1 == 1', date='~') == 1
    assert mc.count('col_1 == 42') == 1
    assert mc.count('col_1 == 42', date='~') == 1
    assert _ids == ['1', _id]

    # remove the db
    remove_file(_expected_db_path)
