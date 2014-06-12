#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward" <cward@redhat.com>

# FIXME: add to *Container a 'sync' command which will export
# across the network all data, persist to some other container
# and enable future 'delta' syncs.

'''
metrique.sqlalchemy
~~~~~~~~~~~~~~~~~~~
'''

from __future__ import unicode_literals, absolute_import

import logging
logger = logging.getLogger('sqlalchemy')

from copy import deepcopy
from datetime import datetime
from getpass import getuser
try:
    from lockfile import LockFile
    HAS_LOCKFILE = True
except ImportError:
    HAS_LOCKFILE = False
import os
from operator import add

try:
    import psycopg2
    psycopg2  # avoid pep8 'imported, not used' lint error
    HAS_PSYCOPG2 = True
except ImportError as e:
    logger.warn('psycopg2 not installed! (%s)' % e)
    HAS_PSYCOPG2 = False

import re

try:
    import simplejson as json
except ImportError:
    import json

try:
    from sqlalchemy import create_engine, MetaData, Table
    from sqlalchemy import Index, Column, Integer
    from sqlalchemy import Float, BigInteger, Boolean, UnicodeText
    from sqlalchemy import TypeDecorator
    from sqlalchemy import select, update, desc
    from sqlalchemy import inspect
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.sql.expression import func
    from sqlalchemy.exc import OperationalError
    import sqlalchemy.dialects.sqlite as sqlite
    import sqlalchemy.dialects.postgresql as pg

    from metrique.utils import dt2ts

    HAS_SQLALCHEMY = True

    # for py2.7 to ensure all strings are unicode
    class CoerceUTF8(TypeDecorator):
        """Safely coerce Python bytestrings to Unicode
        before passing off to the database."""

        impl = UnicodeText

        def process_bind_param(self, value, dialect):
            if not (value is None or isinstance(value, unicode)):
                value = to_encoding(value)
            return value

        def python_type(self):
            return unicode

    class JSONDict(TypeDecorator):
        impl = pg.JSON

        def python_type(self):
            return dict

    class JSONTypedLite(TypeDecorator):
        impl = sqlite.VARCHAR

        def process_bind_param(self, value, dialect):
            e = to_encoding
            return None if value is None else e(json.dumps(value))

        def process_result_value(self, value, dialect):
            return {} if value is None else json.loads(value)

        def python_type(self):
            return dict

    class UTCEpoch(TypeDecorator):
        impl = Float

        def process_bind_param(self, value, dialect):
            return dt2ts(value)

        def python_type(self):
            return float

    TYPE_MAP = {
        None: CoerceUTF8,
        type(None): CoerceUTF8,
        int: Integer,
        float: Float,
        long: BigInteger,
        str: CoerceUTF8,
        unicode: CoerceUTF8,
        bool: Boolean,
        datetime: UTCEpoch,
        list: JSONTypedLite, tuple: JSONTypedLite, set: JSONTypedLite,
        dict: JSONTypedLite,
    }
except ImportError as e:
    logger.warn('sqlalchemy not installed! (%s)' % e)
    HAS_SQLALCHEMY = False
    TYPE_MAP = {}
    CoerceUTF8 = None

from time import time
import warnings

from metrique._version import __version__
from metrique import parse
from metrique.utils import batch_gen, configure, to_encoding, autoschema
from metrique.utils import debug_setup, is_true, str2list, list2str
from metrique.utils import validate_roles, validate_password, validate_username
from metrique.result import Result

ETC_DIR = os.environ.get('METRIQUE_ETC')
CACHE_DIR = os.environ.get('METRIQUE_CACHE') or '/tmp'
DEFAULT_CONFIG = os.path.join(ETC_DIR, 'metrique.json')


class SQLAlchemyProxy(object):
    _object_cls = None
    config = None
    config_key = 'proxy'
    config_file = DEFAULT_CONFIG
    RESERVED_USERNAMES = {'admin', 'test', 'metrique'}
    # these keys are already set, no overrides!
    RESTRICTED_KEYS = ('id', '_id', '_hash', '_start', '_end',
                       '_v', '__v__', '_e')
    TYPE_MAP = TYPE_MAP
    VALID_SHARE_ROLES = ['SELECT', 'INSERT', 'UPDATE', 'DELETE']
    _Base = None
    _engine = None
    _engine_uri = None
    _lock_required = True
    _meta = None
    _schema_name = None
    _session = None
    _sessionmaker = None

    def __init__(self, db=None, table=None, debug=None, config=None,
                 config_key='proxy', config_file=DEFAULT_CONFIG,
                 dialect='sqlite', driver=None, host='127.0.0.1',
                 port='5432', username=None, password=None,
                 connect_args=None, batch_size=999,
                 cache_dir=CACHE_DIR, **kwargs):
        '''
        Accept additional kwargs, but ignore them.
        '''
        is_true(HAS_SQLALCHEMY, '`pip install sqlalchemy` required')
        # use copy of class default value
        self.RESERVED_USERNAMES = deepcopy(SQLAlchemyProxy.RESERVED_USERNAMES)
        self.TYPE_MAP = deepcopy(SQLAlchemyProxy.TYPE_MAP)
        # default _start, _end is epoch timestamp

        options = dict(
            batch_size=batch_size,
            cache_dir=cache_dir,
            connect_args=connect_args,
            db=db,
            dialect=dialect,
            debug=debug,
            driver=driver,
            host=host,
            password=password,
            port=None,
            table=table,
            username=username)
        defaults = dict(
            batch_size=999,
            cache_dir=CACHE_DIR,
            connect_args=None,
            db=None,
            debug=logging.INFO,
            dialect='sqlite',
            driver=None,
            host='127.0.0.1',
            password=None,
            port=5432,
            table=None,
            username=getuser())
        self.config = deepcopy(config or self.config or {})
        self.config_file = config_file or SQLAlchemyProxy.config_file
        self.config_key = config_key or SQLAlchemyProxy.config_key
        self.config = configure(options, defaults,
                                config_file=self.config_file,
                                section_key=self.config_key,
                                section_only=True,
                                update=self.config)
        # db is required; default db is db username else local username
        self.config['db'] = self.config['db'] or self.config['username']
        is_true(self.config.get('db'), 'db can not be null')
        # setup sqlalchemy logging; redirect to metrique logger
        self._debug_setup_sqlalchemy_logging()

        if not self._object_cls:
            from metrique.core_api import MetriqueObject
            self._object_cls = MetriqueObject

    def _debug_setup_sqlalchemy_logging(self):
        level = self.config.get('debug')
        debug_setup(logger='sqlalchemy', level=level)

    def _exec_transaction(self, cmd, params=None, session=None):
        session = session or self.session_new()
        try:
            session.execute(cmd, params)
            session.commit()
        except Exception as e:
            logger.error('Insert Error: %s' % e)
            session.rollback()
            raise

    @staticmethod
    def _index_default_name(columns, name=None):
        if name:
            ix = name
        elif isinstance(columns, basestring):
            ix = columns
        elif isinstance(columns, (list, tuple)):
            ix = '_'.join(columns)
        else:
            raise ValueError(
                "unable to get default name from columns: %s" % columns)
        # prefix ix_ to all index names
        ix = re.sub('^ix_', '', ix)
        ix = 'ix_%s' % ix
        return ix

    def _load_sql(self, sql):
        # load sql kwargs from instance config
        rows = self.session_auto.execute(sql)
        objects = [dict(row) for row in rows]
        return objects

    def _parse_fields(self, table=None, fields=None, reflect=False, **kwargs):
        table = self.get_table(table)
        fields = parse.parse_fields(fields)
        if fields in ([], {}):
            fields = [c.name for c in table.columns]
        if reflect:
            fields = [c for c in table.columns if c.name in fields]
        return fields

    def _parse_query(self, table=None, query=None, fields=None, date=None,
                     alias=None, distinct=None, limit=None, sort=None,
                     descending=None):
        _table = self.get_table(table, except_=True)
        parser = parse.SQLAlchemyMQLParser(_table)
        query = parser.parse(query=query, date=date,
                             fields=fields, distinct=distinct,
                             alias=alias)
        if sort:
            order_by = parse.parse_fields(fields=sort)[0]
            if descending:
                query = query.order_by(desc(order_by))
            else:
                query = query.order_by(order_by)
        return query

    def _sqla_sqlite3(self, uri, isolation_level="READ UNCOMMITTED"):
        isolation_level = isolation_level or "READ UNCOMMITTED"
        kwargs = dict(isolation_level=isolation_level)
        return uri, kwargs

    @property
    def _sqlite_path(self):
        db = self.config.get('db')
        is_true(db, "db can not be null!")
        cache_dir = self.config.get('cache_dir')
        suffix = '.sqlite'
        fname = '%s%s' % (db, suffix)
        return os.path.join(cache_dir, fname)

    def _sqla_teiid(self, uri, version=None, isolation_level="AUTOCOMMIT"):
        uri = re.sub('^.*://', 'postgresql+psycopg2://', uri)
        # version normally comes "'Teiid 8.5.0.Final'", which sqlalchemy
        # failed to parse
        version = version or (8, 2)
        r_none = lambda *i: None
        pg.base.PGDialect.description_encoding = str('utf8')
        pg.base.PGDialect._check_unicode_returns = lambda *i: True
        pg.base.PGDialect._get_server_version_info = lambda *i: version
        pg.base.PGDialect.get_isolation_level = lambda *i: isolation_level
        pg.base.PGDialect._get_default_schema_name = r_none
        pg.psycopg2.PGDialect_psycopg2.set_isolation_level = r_none
        return self._sqla_postgresql(uri=uri, version=version,
                                     isolation_level=isolation_level)

    def _sqla_postgresql(self, uri, version=None,
                         isolation_level="READ COMMITTED"):
        '''
        expected uri form:
        postgresql+psycopg2://%s:%s@%s:%s/%s' % (
            username, password, host, port, vdb)
        '''
        isolation_level = isolation_level or "READ COMMITTED"
        kwargs = dict(isolation_level=isolation_level)
        # override default dict and list column types
        types = {list: pg.ARRAY, tuple: pg.ARRAY, set: pg.ARRAY,
                 dict: JSONDict, datetime: UTCEpoch}
        self.TYPE_MAP.update(types)
        bs = self.config['batch_size']
        # 999 batch_size is default for sqlite, postgres handles more at once
        self.config['batch_size'] = 5000 if bs == 999 else bs
        self._lock_required = False
        self._schema_name = 'public'
        return uri, kwargs

    # ######################## Cube API ################################
    def autoschema(self, objects, **kwargs):
        ''' wrapper around utils.autoschema function '''
        return autoschema(objects=objects, exclude_keys=self.RESTRICTED_KEYS,
                          **kwargs)

    def autotable(self, name=None, schema=None, objects=None, create=False,
                  except_=False, **kwargs):
        name = name or self.config.get('table')
        is_true(name, 'table name must be defined')
        if name not in self.meta_tables:
            # load a sqla.Table into metadata so sessions act as expected
            # unless it's already there, of course.
            if schema is None:
                schema = autoschema(objects=objects,
                                    exclude_keys=self.RESTRICTED_KEYS,
                                    **kwargs)
            table = schema2table(name=name, schema=schema, Base=self.Base,
                                 exclude_keys=self.RESTRICTED_KEYS)
        try:
            if create and name not in self.table_names:
                table.__table__.create()
        except Exception as e:
            logger.error('Create Table %s: FAIL (%s)' % (name, e))
            if except_:
                raise
        else:
            logger.error('Create Table %s: OK' % name)
            table = self.get_table(name, except_=except_)
        return table

    @property
    def Base(self):
        if not self._Base:
            metadata = MetaData(bind=self.engine)
            self._Base = declarative_base(metadata=metadata)
            self.meta_reflect(self._Base)
        return self._Base

    def columns(self, table=None, columns=None, reflect=False):
        table = self.get_table(table)
        columns = sorted(str2list(columns))
        if not columns:
            columns = sorted(c.name for c in table.columns)
        if reflect:
            columns = [c for c in table.columns if c.name in columns]
        return columns

    @property
    def engine(self):
        if not self._engine:
            # setup engine, session, Base and metadata
            self.initialize()
        return self._engine

    def engine_dispose(self):
        if self._engine:
            if self.session:
                self.session_dispose()
            self._engine.dispose()
            return True
        else:
            return None

    def engine_init(self, **kwargs):
        self.engine_dispose()
        db = self.config.get('db')
        host = self.config.get('host')
        port = self.config.get('port')
        driver = self.config.get('driver')
        dialect = self.config.get('dialect') or 'sqlite'
        username = self.config.get('username')
        password = self.config.get('password')
        connect_args = self.config.get('connect_args')
        cache_dir = self.config.get('cache_dir')
        uri = get_engine_uri(db=db, host=host, port=port,
                             driver=driver, dialect=dialect,
                             username=username, password=password,
                             connect_args=connect_args, cache_dir=cache_dir)
        self._engine_uri = uri
        if re.search('sqlite', uri):
            uri, _kwargs = self._sqla_sqlite3(uri)
        elif re.search('teiid', uri):
            uri, _kwargs = self._sqla_teiid(uri)
        elif re.search('postgresql', uri):
            uri, _kwargs = self._sqla_postgresql(uri)
        else:
            raise NotImplementedError("Unsupported engine: %s" % uri)
        _kwargs.update(kwargs)
        self._engine = create_engine(uri, echo=False, **_kwargs)
        return self._engine

    def execute(self, query):
        return self.session_auto.execute(query)

    def get_table(self, table=None, except_=True, as_cls=False):
        if table is None:
            table = self.config.get('table')
        if isinstance(table, Table):
            # this is already the table we're looking for...
            _table = table
        else:
            is_true(table, 'table must be defined!')
            _table = self.meta_tables.get(table)
        if except_:
            is_true(isinstance(_table, Table),
                    'table (%s) not found! Got: %s' % (table, _table))
        if as_cls:
            defaults = dict(__tablename__=table, autoload=True)
            _table = type(str(table), (self.Base,), defaults)
        return _table

    def meta_reflect(self, Base=None, except_=False):
        Base = Base or self.Base
        try:
            Base.metadata.reflect()
        except OperationalError as e:
            logger.warn('Failed to reflect db: %s' % e)
            if except_:
                raise

    @property
    def meta_tables(self):
        return self.Base.metadata.tables

    @property
    def table_names(self):
        return self.inspector.get_table_names(self._schema_name)

    def initialize(self):
        self.engine_init()
        self.session_init()
        # clear existing Base, since we have bound connections, etc
        # which need to be abandonded for new initialization
        self._Base = None

    @property
    def inspector(self):
        return inspect(self.engine)

    @property
    def proxy(self):
        return self.engine

    @property
    def session_auto(self):
        return self.session_new(autocommit=True)

    def session_dispose(self):
        self._session.close()
        self._session = None

    def session_init(self, autoflush=True, autocommit=False,
                     expire_on_commit=True, **kwargs):
        if not self._engine:
            raise RuntimeError("engine is not initiated")
        self._sessionmaker = sessionmaker(
            bind=self._engine, autoflush=autoflush, autocommit=autocommit,
            expire_on_commit=expire_on_commit)
        self._session = self._sessionmaker(**kwargs)
        return self._session

    @property
    def session(self):
        if not self._session:
            self.session_init()
        return self._session

    def session_new(self, **kwargs):
        if not self._sessionmaker:
            self.initialize()
        return self._sessionmaker(**kwargs)

    def count(self, query=None, date=None, table=None):
        '''
        Run a pql mongodb based query on the given cube and return only
        the count of resulting matches.

        :param query: The query in pql
        :param date: date (metrique date range) that should be queried
                    If date==None then the most recent versions of the
                    objects will be queried.
        :param collection: cube name
        :param owner: username of cube owner
        '''
        table = table or self.config.get('table')
        sql_count = select([func.count()])
        query = self._parse_query(table=table, query=query, date=date,
                                  fields='id', alias='anon_x')

        if query is not None:
            query = sql_count.select_from(query)
        else:
            table = self.get_table(table)
            query = sql_count
            query = query.select_from(table)
        return self.session_auto.execute(query).scalar()

    def deptree(self, field, oids, date=None, level=None, table=None):
        '''
        Dependency tree builder. Recursively fetchs objects that
        are children of the initial set of parent object ids provided.

        :param field: Field that contains the 'parent of' data
        :param oids: Object oids to build depedency tree for
        :param date: date (metrique date range) that should be queried.
                    If date==None then the most recent versions of the
                    objects will be queried.
        :param level: limit depth of recursion
        '''
        table = self.get_table(table)
        fringe = str2list(oids)
        checked = set(fringe)
        loop_k = 0
        while len(fringe) > 0:
            if level and loop_k == abs(level):
                break
            query = '_oid in %s' % list(fringe)
            docs = self.find(table=table, query=query, fields=[field],
                             date=date, raw=True)
            fringe = {id for doc in docs for oid in doc[field]
                      if oid not in checked}
            checked |= fringe
            loop_k += 1
        return sorted(checked)

    def distinct(self, fields, query=None, date='~', table=None):
        '''
        Return back a distinct (unique) list of field values
        across the entire cube dataset

        :param field: field to get distinct token values from
        :param query: query to filter results by
        '''
        date = date or '~'
        query = self._parse_query(table=table, query=query, date=date,
                                  fields=fields, alias='anon_x',
                                  distinct=True)
        ret = [r[0] for r in self.session_auto.execute(query)]
        if ret and isinstance(ret[0], list):
            ret = reduce(add, ret, [])
        return sorted(set(ret))

    def drop(self, tables=None, quiet=True):
        if tables is True:
            tables = self.table_names
        elif isinstance(tables, basestring):
            tables = str2list(tables)
        elif self.config.get('table'):
            # drop bound table, if available
            tables = [self.config.get('table')]
        else:
            raise RuntimeError("table to drop must be defined!")
        nulls = lambda t: False if t is None else True
        _tables = filter(nulls,
                         [self.get_table(n, except_=False) for n in tables])
        if _tables:
            logger.warn("Permanently dropping %s" % tables)
            [t.drop() for t in _tables]
            # clear out existing 'cached' metadata
            self._Base = None
        else:
            logger.warn("No tables found to drop, got %s" % tables)
        return

    def exists(self, table=None):
        table = table or self.config.get('table')
        return table in self.table_names

    def find(self, query=None, fields=None, date=None, sort=None,
             descending=False, one=False, raw=False, limit=None,
             as_cursor=False, scalar=False, table=None):
        table = self.get_table(table)
        limit = limit if limit and limit >= 1 else 0
        fields = parse.parse_fields(fields)
        query = self._parse_query(table, query=query, fields=fields,
                                  date=date, limit=limit, sort=sort,
                                  descending=descending)
        rows = self.session_auto.execute(query)
        if scalar:
            return rows.scalar()
        elif as_cursor:
            return rows
        elif one or limit == 1:
            row = dict(rows.first())
            # implies raw
            return row
        elif limit > 1:
            rows = rows.fetchmany(limit)
        else:
            rows = rows.fetchall()
        rows = [dict(r) for r in rows]
        if not rows:
            return None
        elif raw:
            return rows
        else:
            return Result(rows, date)

    def get_last_field(self, field, table=None):
        '''Shortcut for querying to get the last field value for
        a given owner, cube.

        :param field: field name to query
        '''
        table = self.get_table(table, except_=False)
        if table is None:
            last = None
        else:
            is_true(field is not None, 'field must be defined!')
            last = self.find(table=table, fields=field, scalar=True,
                             sort=field, limit=1, descending=True, date='~')
        logger.debug("last %s.%s: %s" % (table, field, last))
        return last

    def index(self, fields, name=None, table=None, **kwargs):
        '''
        Build a new index on a cube.

        Examples:
            + index('field_name')

        :param fields: A single field or a list of (key, direction) pairs
        :param name: (optional) Custom name to use for this index
        :param background: MongoDB should create in the background
        :param collection: cube name
        :param owner: username of cube owner
        '''
        table = self.get_table(table)
        name = self._index_default_name(fields, name)
        fields = parse.parse_fields(fields)
        fields = self.columns(table, fields, reflect=True)
        session = self.session_new()
        index = Index(name, *fields)
        logger.info('Writing new index %s: %s' % (name, fields))
        result = index.create(self.engine)
        session.commit()
        return result

    def index_list(self):
        '''
        List all cube indexes

        :param collection: cube name
        :param owner: username of cube owner
        '''
        logger.info('Listing indexes')
        _ix = {}
        _i = self.inspector
        for tbl in _i.get_table_names():
            _ix.setdefault(tbl, [])
            for ix in _i.get_indexes(tbl):
                _ix[tbl].append(ix)
        return _ix

    def insert(self, objects, session=None, table=None):
        objects = objects.values() if isinstance(objects, dict) else objects
        is_true(isinstance(objects, list), 'objects must be a list')
        table = self.get_table(table)
        if self._lock_required:
            with LockFile(self._sqlite_path):
                self._exec_transaction(cmd=table.insert(), params=objects,
                                       session=session)
        else:
            self._exec_transaction(cmd=table.insert(), params=objects,
                                   session=session)

    def ls(self, startswith=None):
        '''
        List all cubes available to the calling client.

        :param startswith: string to use in a simple "startswith" query filter
        :returns list: sorted list of cube names
        '''
        logger.info('Listing cubes starting with "%s")' % startswith)
        startswith = unicode(startswith or '')
        tables = sorted(name for name in self.table_names
                        if name.startswith(startswith))
        return tables

    def share(self, with_user, roles=None, table=None):
        '''
        Give cube access rights to another user

        Not, this method is NOT supported by SQLite3!
        '''
        table = self.get_table(table)
        is_true(table is not None, 'invalid table: %s' % table)
        with_user = validate_username(with_user)
        roles = roles or ['SELECT']
        roles = validate_roles(roles, self.VALID_SHARE_ROLES)
        roles = list2str(roles)
        logger.info('Sharing cube %s with %s (%s)' % (table, with_user, roles))
        sql = 'GRANT %s ON %s TO %s' % (roles, table, with_user)
        result = self.session_auto.execute(sql)
        return result

    def upsert(self, objects, autosnap=None, batch_size=None, table=None):
        objects = objects.values() if isinstance(objects, dict) else objects
        is_true(isinstance(objects, list), 'objects must be a list')
        table = self.get_table(table)
        if autosnap is None:
            # assume autosnap:True if all objects have _end:None
            # otherwise, false (all objects have _end:non-null or
            # a mix of both)
            autosnap = all(o['_end'] is None for o in objects)
            logger.warn('AUTOSNAP auto-set to: %s' % autosnap)

        batch_size = batch_size or self.config.get('batch_size')
        _ids = [o['_id'] for o in objects]
        dups = {}
        query = '_id in %s'
        t1 = time()
        # FIXME: fields should accept {'id': -1'} to include all but 'id'
        for batch in batch_gen(_ids, batch_size):
            q = query % batch
            dups.update({o._id: dict(o) for o in self.find(
                table=table, query=q, fields='~', date='~', as_cursor=True)})

        diff = int(time() - t1)
        logger.debug(
            'dup query completed in %s seconds (%s dups skipped)' % (
                diff, len(dups)))

        session = self.session_new()
        dup_k, snap_k = 0, 0
        inserts = []
        u = update(table)
        for i, o in enumerate(objects):
            dup = dups.get(o['_id'])
            if dup:
                # remove dup primary key, it will get a new one
                # FIXME: see FIXME above; should use find(fields={'id': -1})
                # so we don't have to pull then delete 'id'
                del dup['id']
                if o['_hash'] == dup['_hash']:
                    dup_k += 1
                elif o['_end'] is None and autosnap:
                    # set existing objects _end to new objects _start
                    dup['_end'] = o['_start']
                    # update _id, _hash, etc
                    dup = self._object_cls(**dup)
                    _ids.append(dup['_id'])
                    # insert the new object
                    inserts.append(dup)
                    # replace the existing _end:None object with new values
                    _id = o['_id']
                    session.execute(
                        u.where(table.c._id == _id).values(**o))
                    snap_k += 1

                else:
                    o = dict(self._object_cls(**o))
                    # don't try to set _id
                    _id = o.pop('_id')
                    assert _id == dup['_id']
                    session.execute(
                        u.where(table.c._id == _id).values(**o))
            else:
                inserts.append(o)

        if inserts:
            t1 = time()
            self.insert(objects=inserts, session=session, table=table)
            diff = int(time() - t1)
            logger.debug('%s inserts in %s seconds' % (len(inserts), diff))
        logger.debug('%s existing objects snapshotted' % snap_k)
        logger.debug('%s duplicates not re-saved' % dup_k)
        session.flush()
        session.commit()
        return sorted(map(unicode, _ids))

    def user_register(self, username, password):
        # FIXME: enable setting roles at creation time...
        is_true((username and password), 'username and password required!')
        u = validate_username(username, self.RESERVED_USERNAMES)
        p = validate_password(password)
        logger.info('Registering new user %s' % u)
        # FIXME: make a generic method which runs list of sql statements
        sql = ("CREATE USER %s WITH PASSWORD '%s';" % (u, p),
               "CREATE DATABASE %s WITH OWNER %s;" % (u, u))
        # can't run in a transaction...
        cnx = self.engine.connect()
        cnx.execution_options(isolation_level='AUTOCOMMIT')
        result = [cnx.execute(s) for s in sql]
        return result

    def user_disable(self, username, table=None):
        table = self.get_table(table)
        is_true(username, 'username required')
        logger.info('Disabling existing user %s' % username)
        u = update('pg_database')
        #update pg_database set datallowconn = false where datname = 'applogs';
        sql = u.where(
            "datname = '%s'" % username).values({'datallowconn': 'false'})
        result = self.session_auto.execute(sql)
        return result


def get_engine_uri(db, host='127.0.0.1', port=5432, dialect='sqlite',
                   driver=None, username=None, password=None,
                   connect_args=None, cache_dir=None):
    cache_dir = cache_dir or CACHE_DIR
    is_true(db, 'db can not be null')
    is_true(bool(dialect in [None, 'postgresql', 'sqlite', 'teiid']),
            'invalid dialect: %s' % dialect)
    if dialect and driver:
        dialect = '%s+%s' % (dialect, driver)
    elif dialect:
        pass
    else:
        dialect = 'sqlite'
        dialect = dialect.replace('://', '')

    if dialect == 'sqlite':
        # db is expected to be an absolute path to where
        # sqlite db will be saved
        db = os.path.join(cache_dir, '%s.sqlite' % db)
        uri = '%s:///%s' % (dialect, db)
    else:
        if username and password:
            u_p = '%s:%s@' % (username, password)
        elif username:
            u_p = '%s@' % username
        else:
            u_p = ''

        uri = '%s://%s%s:%s/%s' % (dialect, u_p, host, port, db)
        if connect_args:
            args = ['%s=%s' % (k, v) for k, v in connect_args.iteritems()]
            args = '?%s' % '&'.join(args)
            uri += args
    _uri = re.sub(':[^:]+@', ':***@', uri)
    logger.info("Engine URI: %s" % _uri)
    return uri


def schema2table(name, schema, Base=None, type_map=None, exclude_keys=None):
    is_true(name, "table name must be defined!")
    is_true(schema, "schema must be defined!")
    if Base:
        logger.debug('Reusing existing Base (%s)' % Base)
    Base = Base or declarative_base()
    type_map = type_map or deepcopy(TYPE_MAP)
    logger.debug("Attempting to create Table class: %s..." % name)
    logger.debug(" ... Schema: %s" % schema)
    logger.debug(" ... Type Map: %s" % type_map)

    __repr__ = lambda s: '%s(%s)' % (
        s.__tablename__,
        ', '.join(['%s=%s' % (k, v) for k, v in s.__dict__.iteritems()
                   if k != '_sa_instance_state']))

    #_ignore_keys = set(['_id', '_hash'])
    #__init__ = lambda s, kw: [setattr(s, k, v) for k, v in kw.iteritems()
    #                          if k not in _ignore_keys]
    defaults = {
        '__tablename__': name,
        '__table_args__': ({'extend_existing': True}),
        'id': Column('id', Integer, primary_key=True),
        '_id': Column(CoerceUTF8, nullable=False, unique=True, index=True),
        '_oid': Column(CoerceUTF8, nullable=False, index=True,
                       unique=False),
        '_hash': Column(CoerceUTF8, nullable=False, index=True),
        '_start': Column(type_map[datetime], index=True,
                         nullable=False),
        '_end': Column(type_map[datetime], index=True),
        '_v': Column(Integer, default=0, nullable=False),
        '__v__': Column(CoerceUTF8, default=__version__, nullable=False),
        '_e': Column(type_map[dict]),
        # '__init__': __init__,
        '__repr__': __repr__,
    }

    schema_items = schema.items()
    for k, v in schema_items:
        if k in exclude_keys:
            ekeys = exclude_keys
            warnings.warn(
                'non-null restricted keys detected %s; ignoring!' % ekeys)
            continue
        __type = v.get('type')
        if __type is None:
            __type = type(None)
        _type = type_map.get(__type)
        if v.get('container', False):
            _list_type = type_map[list]
            if _list_type is pg.ARRAY:
                _list_type = _list_type(_type)
            schema[k] = Column(_list_type)
        elif k == '_oid':
            # in case _oid is defined in the schema,
            # make sure we index it and it's unique
            schema[k] = Column(_type, nullable=False, index=True,
                               unique=False)
        else:
            schema[k] = Column(_type, name=k)
    defaults.update(schema)

    # in case _oid isn't set yet, default to big int column
    defaults.setdefault('_oid', Column(BigInteger, nullable=False,
                                       index=True, unique=False))

    _table = type(str(name), (Base,), defaults)
    return _table
