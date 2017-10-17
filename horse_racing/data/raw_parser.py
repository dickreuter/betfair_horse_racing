from functools import partial

__author__ = 'richardjeffries'

import tarfile
import re, os
import bz2
import json

import pandas as pd

from dateutil.parser import parse as jsondate2dt
import datetime, pytz
from tqdm import tqdm

market_re = re.compile(r'P:/data/xds/historic/BASIC/[0-9]{1,10}/1\.[0-9]{1,}.bz2')
runner_re = re.compile(r'P:/data/xds/historic/BASIC-1\.[0-9]{1,}.bz2')
epoch = datetime.datetime(2016, 1, 1).replace(tzinfo=pytz.UTC)
interested_times = [5, 4, 3, 2, 1]

class BetfairTarFile(object):

    def __init__(self, tar_file):
        self.tar_file = tar_file
        with tarfile.open(tar_file, 'r') as tf:
            self.prefix = os.path.split(tf.firstmember.name)[0]

    def get_single(self, market_id):
        market = os.path.join(self.prefix,'BASIC-{}.bz2'.format(market_id))
        with tarfile.open(self.tar_file, 'r') as tf:
            self.prefix = os.path.split(tf.firstmember.name)[0]
            try:
                return bz2.decompress(tf.extractfile(market).read())
            except ValueError:
                return {}

    @staticmethod
    def check_market(data):
        id = data['id']
        market = data['marketDefinition']
        runners = None
        win_market = None
        if market['countryCode'] in ('US') and market['marketType'] == 'WIN':
            if jsondate2dt(market['openDate']) > epoch:
                # Extract runners into seperate frame
                runners = market['runners']
                map(lambda x: x.update({u'market_id': id}), runners)
                # Save market definitions (everything except runners
                win_market = ({k: v for k, v in market.iteritems() if k != 'runners'})
        return id, win_market, runners

    @staticmethod
    def search_event(event_id, data):
        id = data['id']
        market = data['marketDefinition']
        runners = None
        win_market = None
        if id == event_id:
            if jsondate2dt(market['openDate']) > epoch:
                # Extract runners into seperate frame
                runners = market['runners']
                map(lambda x: x.update({u'market_id': id}), runners)
                # Save market definitions (everything except runners
                win_market = ({k: v for k, v in market.iteritems() if k != 'runners'})
        return id, win_market, runners

    def search_for_market(self, event_id):
        return self._itterate_markets(partial(self.search_event, event_id))

    def get_runners_and_markets(self):
        return self._itterate_markets(self.check_market)

    def _itterate_markets(self, apply_function):
        win_markets = {}
        runners = []
        with tarfile.open(self.tar_file, 'r') as tf:
            members = tf.getmembers()
            pbar = tqdm(total=len(members))
            for m in members:
                pbar.update()
                # if market_re.match(os.path.split(m.name)[1]):
                try:
                    data = bz2.decompress(tf.extractfile(m).read())
                except ValueError:
                    continue
                for s in data.split('\n'):
                    try:
                        data_j = json.loads(s)
                    except ValueError:
                        continue
                    try:
                        for id, market, market_runners in map(apply_function, data_j["mc"]):
                            if market:
                                win_markets[id] = market
                                runners.extend(market_runners)
                    except KeyError:
                        continue

        markets_df = pd.DataFrame.from_dict(win_markets)
        runners_df = pd.DataFrame.from_records(runners)

        self.markets = markets_df.T.index
        return markets_df, runners_df


    def get_timesseries(self):
        timeseries = []
        if not self.markets:
            print("No markets set, have they been processed? Call .get_runners_and_markets")
            return pd.DataFrame()

        pbar = tqdm(total=len(self.markets))
        with tarfile.open(self.tar_file, 'r') as tf:
            for m in self.markets:
                pbar.update()
                try:
                    data = bz2.decompress(tf.extractfile(os.path.join(self.prefix, 'BASIC-{}.bz2'.format(m))).read())
                except (ValueError, KeyError):
                    continue
                for s in data.split('\n'):
                    try:
                        data_j = json.loads(s)
                    except ValueError:
                        continue
                    try:
                        for r in data_j['mc']:
                            for runner, ltp in map(lambda x: (x['id'], x['ltp']), r['rc']):
                                timeseries.append(tuple([data_j['pt'], m, runner, ltp]))
                    except (KeyError, TypeError):
                        continue
        return pd.DataFrame.from_records(timeseries)


def process_tarfile(fname, store_file):
    bft = BetfairTarFile(fname)
    store = pd.HDFStore(store_file)
    markets_df, runners_df = bft.get_runners_and_markets()
    store['markets-US'] = markets_df
    store['runners-US'] = runners_df
    timeseries_df = bft.get_timesseries()
    store['timeseries-US'] = timeseries_df


def get_from_store(store_file):
    store = pd.HDFStore(store_file)
    markets_df = store['markets']
    runners_df = store['runners']
    timeseries_df = store['timeseries']
    return markets_df, runners_df, timeseries_df


def get_runners_prerace(timeseries, time):
    pivoted = timeseries.pivot_table(values=3, columns=2, index=0).fillna(method='pad')
    pivoted.index = pd.Index(map(datetime.datetime.fromtimestamp, pivoted.index / 1000))
    race_start = datetime.datetime.strptime(time, "%Y-%m-%dT%H:%M:%S.%fZ")
    times = []
    for t in interested_times:
        times.append(race_start - datetime.timedelta(minutes=t))
    try:
        records = pivoted.iloc[map(lambda t: pivoted.index.get_loc(t, method='pad'), times)].T
    except:
        return pd.DataFrame()
    records.columns = map(lambda x: "price_{}_mins".format(x), interested_times)
    return records


def process_markets(timeseries, markets, runners):
    price_dfs = []
    pbar = tqdm(total=len(timeseries[1].unique()))
    for i, m in timeseries.groupby(1):
        pbar.update()
        market_start = markets[i]['marketTime']
        prices = get_runners_prerace(m, market_start)
        prices['selection_id'] = prices.index
        prices['market_id'] = i
        prices['race_time'] = market_start
        full_df = prices.merge(runners, left_on=['market_id', 'selection_id'], right_on=['market_id', 'id'])
        full_df['market_id'] = full_df['market_id'].map(lambda x: x.split('.')[1])
        try:
            price_dfs.append(full_df[['market_id', 'selection_id', 'race_time', 'name', 'price_5_mins', 'price_4_mins',
                                      'price_3_mins', 'price_2_mins', 'price_1_mins']])
        except KeyError:
            continue
    return pd.DataFrame(pd.concat(price_dfs))

if __name__ == '__main__':
    #process_tarfile(r'/Users/richardjeffries/Downloads/horses.tar', './bdfata.h5')
    process_tarfile(r'/Users/richardjeffries/Downloads/data (1).tar', './bspdata.h5')
    markets, runners, timeseries = get_from_store('./bfdata.h5')
    results = process_markets(timeseries, markets, runners)
    results.to_csv(r'results.csv')