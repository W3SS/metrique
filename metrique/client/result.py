#!/usr/bin/env python
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward" <cward@redhat.com>

import logging
logger = logging.getLogger(__name__)

from decorator import decorator
import simplejson as json
import os
from datetime import datetime

from pandas import DataFrame, Series
import pandas.tseries.offsets as off
from pandas.tslib import Timestamp
import pandas as pd
import numpy as np


def get_colors():
    ''' Some nice colors, stored here for convenience.'''
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
              '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
    alphas = ['#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5',
              '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5']
    return colors, alphas


def mask_filter(f):
    '''
        Generic function for getting back filtered frame
        data according to True/False mask filter frame matching

        :param Pandas.DataFrame mask_frame:
            DataFrame that maps index:True/False where True means it
            matches the filter and False means it does not
        :param Boolean filter_ :
            True will return back a DataFrame that contains only items
            which matched the mask_frame filter. False returns back the
            opposite.
    '''
    return decorator(_mask_filter, f)


def _mask_filter(f, self, *args, **kwargs):
    filter_ = args[-1]  # by convention, filter_ expected as last arg
    mask_frame = f(self, *args, **kwargs)
    if filter_ is None:
        return mask_frame
    else:
        return type(self)(self[mask_frame == filter_])


def filtered(f):
    def _filter(f, self, *args, **kwargs):
        frame = f(self, *args, **kwargs)
        ret = type(self)(frame)
        ret._lbound = self._lbound
        ret._rbound = self._rbound
        return ret

    return decorator(_filter, f)


class Result(DataFrame):
    ''' Custom DataFrame implementation for Metrique '''
    def __init__(self, data=None):
        super(Result, self).__init__(data)
        self._result_data = data
        # The converts are here so that None is converted to NaT
        if '_start' in self:
            self._start = pd.to_datetime(self._start)
        if '_end' in self:
            self._end = pd.to_datetime(self._end)
        self._lbound = self._rbound = None

    @classmethod
    def from_result_file(cls, path):
        ''' Load saved json data from file '''
        path = os.path.expanduser(path)
        with open(path) as f:
            data = json.load(f)
            return Result(data)

    def to_result_file(self, path):
        ''' Save json data to file '''
        path = os.path.expanduser(path)
        with open(path, 'w') as f:
            json.dump(self._result_data, f)

    def set_date_bounds(self, date):
        '''
        Pass in the date used in the original query.

        :param String date:
            Date (date range) that was queried:
                date -> 'd', '~d', 'd~', 'd~d'
                d -> '%Y-%m-%d %H:%M:%S,%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'
        '''
        if date is not None:
            split = date.split('~')
            if len(split) == 1:
                self._lbound = Timestamp(date)
                self._rbound = Timestamp(date)
            elif len(split) == 2:
                if split[0] != '':
                    self._lbound = Timestamp(split[0])
                if split[1] != '':
                    self._rbound = Timestamp(split[1])
            else:
                raise Exception('Date %s is not in the correct format' % date)

    def check_in_bounds(self, date):
        ''' Check that left and right bounds are sane '''
        dt = Timestamp(date)
        return ((self._lbound is None or dt >= self._lbound) and
                (self._rbound is None or dt <= self._rbound))

    def on_date(self, date, only_count=False):
        '''
        Filters out only the rows that match the spectified date.
        Works only on a Result that has _start and _end columns.

        :param String date:
            date can be anything Pandas.Timestamp supports parsing
        '''
        if not self.check_in_bounds(date):
            raise ValueError('Date %s is not in the queried range.' % date)
        date = Timestamp(date)
        after_start = self._start <= date
        before_end = (self._end > date) | self._end.isnull()
        if only_count:
            return np.sum(before_end & after_start)
        else:
            return self.filter(before_end & after_start)

    def _auto_select_scale(self, dts, start=None, end=None, ideal=300):
        '''
        Guess what a good timeseries scale might be,
        given a particular data set, attempting to
        make the total number of x values as close to
        `ideal` as possible

        This is a helper for plotting
        '''
        start = min(dts) if start is None else start
        end = max(dts) if end is None else end
        maximum_count = len(filter(lambda dt: start <= dt and dt <= end, dts))
        daily_count = (end - start).days
        if maximum_count <= ideal:
            return 'maximum'
        elif daily_count <= ideal:
            return 'daily'
        elif daily_count / 7 <= ideal:
            return 'weekly'
        elif daily_count / 30 <= ideal:
            return 'monthly'
        elif daily_count / 91 <= ideal:
            return 'quarterly'
        else:
            return 'yearly'

    def history(self, dates=None, counts=True):
        '''
        Works only on a Result that has _start and _end columns.
        most_recent=False should be set for this to work

        :param: List dates:
            List of dates
        :param Boolean counts:
            If True counts will be returned
            If False ids will be returned
        '''
        if dates is None:
            dates = self.get_dates_range()

        idx, vals = [], []
        for dt in dates:
            idx.append(dt)
            if counts:
                vals.append(self.on_date(dt, only_count=True))
            else:
                vals.append(list(self.on_date(dt)._oid))
        ret = Series(vals, index=idx)
        return ret.sort_index()

    def get_dates_range(self, scale='auto', start=None, end=None):
        '''
        Returns a list of dates sampled according to the specified parameters.

        :param String scale: {'auto', 'maximum', 'daily', 'weekly', 'monthly',
                'quarterly', 'yearly'}
            Scale specifies the sampling intervals.
            'auto' will heuritically choose such scale that will give you
            fast results.
        :param String start:
            First date that will be included.
        :param String end:
            Last date that will be included
        :param Boolean include_bounds:
            Include start and end in the result if they are not included yet.
        '''
        if scale not in ['auto', 'daily', 'weekly', 'monthly', 'quarterly',
                         'yearly']:
            raise ValueError('Incorrect scale: %s' % scale)
        start = self._start.min() if start is None else Timestamp(start)
        end = max(self._end.dropna().max(),
                  self._start.max()) if end is None else Timestamp(end)
        start = start if self.check_in_bounds(start) else self._lbound
        end = end if self.check_in_bounds(end) else self._rbound

        if scale == 'auto' or scale == 'maximum':
            start_dts = list(self._start.dropna().values)
            end_dts = list(self._end.dropna().values)
            dts = map(Timestamp, set(start_dts + end_dts))
            dts = filter(lambda ts: self.check_in_bounds(ts) and
                         ts >= start and ts <= end, dts)
        if scale == 'auto':
            scale = self._auto_select_scale(dts, start, end)
        if scale == 'maximum':
            return dts

        freq = dict(daily='D', weekly='W', monthly='M', quarterly='3M',
                    yearly='12M')
        offset = dict(daily=off.Day(), weekly=off.Week(),
                      monthly=off.MonthEnd(), quarterly=off.QuarterEnd(),
                      yearly=off.YearEnd())
        ret = list(pd.date_range(start + offset[scale], end, freq=freq[scale]))
        ret = [start] + ret + [end]
        ret = filter(lambda ts: self.check_in_bounds(ts), ret)
        return ret

    ######################## FILTERS ##########################

    def group_size(self, column, to_dict=False):
        '''
            Simply group items by the given column and return
            dictionary (or Pandas Series) with each bucket size
        '''
        gby = self.groupby(column).size()
        return gby.to_dict() if to_dict else gby

    @mask_filter
    def ids(self, ids, _filter=True):
        ''' filter for only objects with matching object ids '''
        # there *should* be an easier way to do this, without lambda...
        return self['_id'].map(lambda x: True if x in ids else False)

    @filtered
    def unfinished(self):
        '''
        Leaves only those entities that has some version with _end equal None
        or with _end larger than the right cutoff.
        '''
        mask = self._end.isnull()
        if self._rbound is not None:
            mask = mask | (self._end > self._rbound)
        oids = set(self[mask]._oid.tolist())
        return self[self._oid.apply(lambda oid: oid in oids)]

    @filtered
    def last_versions_with_age(self, col_name='age'):
        '''
        Leaves only the latest version for each object.
        Adds a new column which represents age.
        The age is computed by subtracting _start of the oldest version
        from one of these possibilities:
            if self._rbound is None:
                if latest_version._end is pd.NaT:
                    current_time is used
                else:
                    min(current_time, latest_version._end) is used
            else:
                if latest_version._end is pd.NaT:
                    self._rbound is used
                else:
                    min(self._rbound, latest_version._end) is used

        Parameters
        ----------
        col_name: str
            Name of the new column.
        '''
        def prep(df):
            ends = set(df._end.tolist())
            end = pd.NaT if pd.NaT in ends else max(ends)
            if end is pd.NaT:
                age = cut_ts - df._start.min()
            else:
                age = min(cut_ts, end) - df._start.min()
            last = df[df._end == end].copy()
            last[col_name] = age
            return last

        cut_ts = datetime.utcnow() if self._rbound is None else self._rbound
        res = pd.concat([prep(df) for _, df in self.groupby(self._oid)])
        return res

    @filtered
    def last_chain(self):
        '''
        Leaves only the last chain for each object.
        Chain is a series of consecutive versions
            (_end of one is _start of another) .
        '''
        def prep(df):
            ends = df._end.tolist()
            maxend = pd.NaT if pd.NaT in ends else max(ends)
            ends = set(df._end.tolist()) - set(df._start.tolist() + [maxend])
            if len(ends) == 0:
                return df
            else:
                cutoff = max(ends)
                return df[df._start > cutoff]

        return pd.concat([prep(df) for _, df in self.groupby(self._oid)])

    @filtered
    def one_version(self, index=0):
        '''
        Leaves only one version for each object.

        :param int index:
            List-like index of the version.
            0 means first version, -1 means last.
        '''
        def prep(df):
            start = sorted(df._start.tolist())[index]
            return df[df._start == start]

        return pd.concat([prep(df) for _, df in self.groupby(self._oid)])

    def first_version(self):
        '''
        Leaves only the first version for each object.
        '''
        return self.one_version(0)

    def last_version(self):
        '''
        Leaves only the last version for each object.
        '''
        return self.one_version(-1)

    @filtered
    def started_after(self, dt):
        '''
        Leaves only those entities whose first version started after the
        specified date.
        '''
        starts = self._start.groupby(self._oid).min()
        oids = starts[starts > dt].index.tolist()
        return self[self._oid.apply(lambda v: v in oids)]

    @filtered
    def filter(self, mask):
        return self[mask]

    @filtered
    def object_apply(self, function):
        '''
        Groups by _oid, then applies the function to each group
        and finally concatenates the results.

        :param (DataFrame -> DataFrame) function:
            function that takes a DataFrame and returns a DataFrame
        '''
        return pd.concat([function(df) for _, df in self.groupby(self._oid)])

    ######################## Plotting #####################

    def plot_column(self, column, **kwargs):
        x = self.group_size(column)
        return x.plot(**kwargs)
