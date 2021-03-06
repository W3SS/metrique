#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward" <cward@redhat.com>


from datetime import datetime
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
etc_dir = os.environ.get('METRIQUE_ETC')

default_config = os.path.join(etc_dir, 'metrique.json')


def db_tester(proxy):
    from metrique.utils import ts2dt
    from metrique import metrique_object as O

    _start = ts2dt("2001-01-01 00:00:00")
    _start_plus = ts2dt("2001-01-01 00:00:01")
    _end = ts2dt("2001-01-02 00:00:00")
    _before = ts2dt("2000-12-31 00:00:00")
    _after = ts2dt("2001-01-03 00:00:00")
    _date = ts2dt("2014-01-01 00:00:00")
    TABLE = 'bla'
    p = proxy

    # Clear out ALL tables in the database!
    p.drop(True)

    assert p.ls() == []

    # must pass _oid as kwarg
    obj = {'col_1': 1, 'col_3': _date}
    try:
        O(**obj)
    except TypeError:
        pass
    else:
        assert False

    # _oid can't be null
    obj = {'_oid': None, 'col_1': 1, 'col_3': _date}
    try:
        O(**obj)
    except ValueError:
        pass
    else:
        assert False

    _obj_1 = {'_oid': 1, 'col_1': 1, 'col_3': _date}
    obj_1 = [O(**_obj_1)]

    schema = {
        '_oid': {'type': int},
        'col_1': {'type': int},
        'col_3': {'type': datetime},
    }

    autoschema = p.autoschema(obj_1)
    assert dict(autoschema) == dict(schema)

    table = p.autotable(name=TABLE, schema=schema, create=True)
    assert table is not None

    assert p.count() == 0

    expected_fields = ['__v__', '_e', '_end', '_hash', '_id',
                       '_start', '_v', 'id']

    _exp = expected_fields + _obj_1.keys()
    assert sorted(p.columns()) == sorted(_exp)

    print 'Inserting %s' % obj_1
    p.insert(obj_1)

    assert p.count() == 1
    assert p.find('_oid == 1', raw=True, date=None)
    # should be one object with col_1 == 1 (_oids: 1)
    assert p.count('col_1 == 1', date='~') == 1

    _obj_2 = {'_oid': 2, 'col_1': 1, 'col_3': _date,
              '_start': _start, '_end': _end}
    obj_2 = [O(**_obj_2)]
    print 'Inserting %s' % obj_2
    p.insert(obj_2)
    assert p.count('_oid == 2') == 0
    assert p.count('_oid == 2', date=None) == 0
    assert p.count('_oid == 2', date='%s~' % _start) == 1
    # ~DATE does NOT include objects existing on DATE, but only UP TO/BEFORE
    assert p.count('_oid == 2', date='~%s' % _start) == 0
    assert p.count('_oid == 2', date='~%s' % _start_plus) == 1
    assert p.count('_oid == 2', date='~') == 1
    assert p.count('_oid == 2', date='~%s' % _before) == 0
    assert p.count('_oid == 2', date='%s~' % _after) == 0
    # should be two objects with col_1 == 1 (_oids: 1, 2)
    assert p.count('col_1 == 1', date='~') == 2

    assert p.distinct('_oid') == [1, 2]

    # insert new obj, then update col_3's values
    # note, working with the obj individually, but passing as a sigleton list
    # to insert(), etc
    _obj_3 = {'_oid': 3, 'col_1': 1, 'col_3': _date,
              '_start': _start, '_end': None}
    obj_3 = O(**_obj_3)
    print 'Inserting %s' % obj_3
    p.insert([obj_3])
    assert p.count('_oid == 3', date='~') == 1

    obj_3['col_1'] = 42
    print '... Update 1: %s' % obj_3
    obj_3 = O(**obj_3)
    p.upsert([obj_3])

    # should be two versions of _oid:3
    assert p.count('_oid == 3', date='~') == 2
    # should be three objects with col_1 == 1 (_oids: 1, 2, 3)
    assert p.count('col_1 == 1', date='~') == 3
    assert p.count('col_1 == 42', date='~') == 1

    # should be four object versions in total at this point
    assert p.count(date='~') == 4

    # last _oid should be 3
    assert p.get_last_field('_oid') == 3
    try:
        p.insert([obj_3])
    except Exception:
        pass
    else:
        assert False, "shouldn't be able to insert same object twice"

    _obj_4 = {'_oid': -1}
    obj_4 = O(**_obj_4)
    print '... Update 2: %s' % obj_4
    p.insert([obj_4])
    # 3 should still be highest _oid
    assert p.get_last_field('_oid') == 3

    _obj_5 = {'_oid': 42}
    obj_5 = O(**_obj_5)
    p.insert([obj_5])
    # now, 42 should be highest
    assert p.get_last_field('_oid') == 42

    assert p.ls() == [TABLE]

    # Indexes
    ix = [i['name'] for i in p.index_list().get(TABLE)]
    assert 'ix_col_1' not in ix
    p.index('col_1')
    ix = [i['name'] for i in p.index_list().get(TABLE)]
    assert 'ix_bla_col_1' in ix


def test_get_engine_uri():
    from metrique.sqlalchemy import get_engine_uri

    _expected_db_path = os.path.join(cache_dir, 'test.sqlite')

    DB = 'test'
    assert get_engine_uri(DB) == 'sqlite:///%s' % _expected_db_path


def test_sqlite3():
    from metrique.utils import remove_file
    from metrique.sqlalchemy import SQLAlchemyProxy

    _expected_db_path = os.path.join(cache_dir, 'test.sqlite')
    remove_file(_expected_db_path)

    DB = 'test'
    TABLE = 'bla'
    p = SQLAlchemyProxy(db=DB, table=TABLE)
    p.initialize()
    assert p._engine_uri == 'sqlite:///%s' % _expected_db_path

    db_tester(p)

    p.drop()
    try:
        assert p.count() == 0
    except RuntimeError:
        pass
    else:
        assert False

    assert p.ls() == []

    remove_file(_expected_db_path)


# test container type!
#schema.update({'col_2': {'type': unicode, 'container': True}})

def test_postgresql():
    from metrique.sqlalchemy import SQLAlchemyProxy
    from metrique.utils import rand_chars, configure

    config = configure(config_file=default_config, section_key='proxy',
                       section_only=True)
    _db = config['db'] = 'test'
    _table = config['table'] = 'bla'
    config['dialect'] = 'postgresql'
    p = SQLAlchemyProxy(**config)
    _u = p.config.get('username')
    _p = p.config.get('password')
    _po = p.config.get('port')
    _expected_engine = 'postgresql://%s:%s@127.0.0.1:%s/%s' % (_u, _p,
                                                               _po, _db)
    p.initialize()
    assert p._engine_uri == _expected_engine

    # FIXME: DROP ALL

    db_tester(p)

    schema = {
        'col_1': {'type': int},
        'col_3': {'type': datetime},
    }

    # FIXME: remove user! before test completes

    # new user
    new_u = rand_chars(chars='asdfghjkl')
    new_p = rand_chars(8)
    p.user_register(username=new_u, password=new_p)

    assert p.user_exists(new_u) is True

    # Sharing
    p.share(new_u)

    _new_table = 'blabla'
    # switch to the new users db
    p.config['username'] = new_u
    p.config['password'] = new_p
    p.config['db'] = new_u
    p.config['table'] = _new_table
    p.initialize()
    assert p.ls() == []

    q = 'SELECT current_user;'
    assert p.execute(q)[0] == {'current_user': new_u}

    p.autotable(name=_new_table, schema=schema, create=True)
    assert p.ls() == [_new_table]

    # switch to admin's db
    p.config['username'] = _u
    p.config['password'] = _p
    p.config['db'] = _u
    p.config['table'] = _table
    p.initialize()
    p.autotable(schema=schema)
    assert p.ls() == [_table]

    p.drop()
    try:
        assert p.count()
    except RuntimeError:
        pass
    else:
        assert False

    assert p.ls() == []
