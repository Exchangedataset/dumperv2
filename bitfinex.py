import dumpv2
from common import DIR

import websocket
import urllib.request
import json
import logging

logger = logging.getLogger('Bitfinex')

# number of channels Bitfinex allows to open at maximum
BITFINEX_CHANNEL_LIMIT = 30

class BitfinexState():
    def __init__(self):
        # map of id versus channel
        self.idvch = dict()
        self.orderbooks = dict()

    def send(self, message: str):
        obj = json.loads(message)
        return '%s_%s' % (obj['channel'], obj['symbol'])

    def msg(self, message: str):
        obj = json.loads(message)

        if type(obj) == dict:
            if obj['event'] == 'subscribed':
                # response to subscribe
                event_channel = obj['channel']
                symbol = obj['symbol']
                chanId = obj['chanId']

                channel = self.idvch[chanId] = '%s_%s' % (event_channel, symbol)
                
                return channel

            elif obj['event'] == 'info':
                # information message
                return 'info'
            elif obj['event'] == 'error':
                return '%s_%s' % (obj['channel'], obj['symbol'])
            else:
                return dumpv2.CHANNEL_UNKNOWN
        else:
            # obj must be an array
            # normal channel message
            chanId = obj[0]
            channel = self.idvch[chanId]

            if not channel.startswith('book'):
                return channel
            if type(obj[1]) == str and obj[1] == 'hb':
                # heartbeat, ignore
                return channel

            if chanId not in self.orderbooks:
                # first time to get data for this orderbook channel
                self.orderbooks[chanId] = dict()
            orderbook = self.orderbooks[chanId]

            orders = obj[1]
            # no orders, probably because maintainance
            if len(orders) == 0:
                return channel

            if type(orders[0]) != list:
                # if there is only one order, bitfinex api server will return
                # only that order and bigger array is abbreviated
                orders = [orders]
            
            for order in orders:
                price = order[0]
                count = order[1]
                amount = order[2]
                if count == 0:
                    if price in orderbook:
                        del orderbook[price]
                else:
                    # remove all orders with logical error
                    dueToRemove = set()
                    if amount < 0:
                        for memPrice in orderbook:
                            if memPrice >= price and orderbook[memPrice]['amount'] >= 0:
                                dueToRemove.add(memPrice)
                    else:
                        for memPrice in orderbook:
                            if memPrice <= price and orderbook[memPrice]['amount'] <= 0:
                                dueToRemove.add(memPrice)

                    for remove in dueToRemove:
                        del orderbook[remove]

                    orderbook[price] = { 'count': count, 'amount': amount }
                    
            return channel

    # snapshot contains
    # CHANNEL_SUBSCRIBED: a map of subscribed channel ids vs its name
    # book_[symbol]: a snapshot of the orderbook of [symbol] in the raw format
    def snapshot(self):
        states = []
        chvid = dict()
        for chanId, channel in self.idvch.items():
            chvid[channel] = chanId
        states.append((dumpv2.CHANNEL_SUBSCRIBED, json.dumps(chvid)))

        for chanId, memOrders in self.orderbooks.items():
            orders = []
            for price, elem in sorted(memOrders.items()):
                orders.append([price, elem['count'], elem['amount']])
            states.append((self.idvch[chanId], json.dumps(orders)))
        return states


def subscribe_gen():
    # before start dumping, bitfinex has too much currencies so it has channel limitation
    # we must cherry pick the best one to observe its trade
    # we can determine this by retrieving trading volumes for each symbol and pick coins which volume is in the most

    logger.info('Retrieving market volumes')

    sub_symbols = None

    request = urllib.request.Request('https://api.bitfinex.com/v2/tickers?symbols=ALL')
    with urllib.request.urlopen(request, timeout=1) as response:
        tickers = json.load(response)

        # take only normal exchange symbol which starts from 't', not funding symbol, 'f'
        # symbol name is located at index 0
        tickers = list(filter(lambda arr: arr[0].startswith('t'), tickers))

        # volume is NOT in USD, example, tETHBTC volume is in BTC
        # must convert it to USD in order to sort them by USD volume
        # for this, let's make a price table
        # last price are located at index 7
        price_table = {arr[0]: arr[7] for arr in tickers}

        # convert raw volume to USD volume
        # tXXXYYY (volume in XXX, price in YYY)
        # if tXXXUSD exist, then volume is (volume of tXXXYYY) * (price of tXXXUSD)
        def usd_mapper(arr):
            # symbol name
            symbol_name = arr[0]
            # raw volume
            volume_raw = arr[8]
            # volume in USD
            volume = 0

            # take XXX of tXXXYYY
            pair_base = arr[0][1:4]

            if 't%sUSD' % pair_base in price_table:
                volume = volume_raw * price_table['t%sUSD' % pair_base]
            else:
                print('could not find proper market to calculate volume for symbol: ' + symbol_name)

            # map to this array format
            return [symbol_name, volume]
        # map using usd_mapper function above
        itr = map(usd_mapper, tickers)
        # now itr (Iterator) has format of
        # [ ['tXXXYYY', 10000], ['tZZZWWW', 20000], ... ]

        # sort iterator by USD volume using sorted().
        # note it requires reverse option, since we are looking for symbols
        # which have the most largest volume
        itr = sorted(itr, key=lambda arr: arr[1], reverse=True)

        # take only symbol, not an object
        itr = map(lambda ticker: ticker[0], itr)

        # trim it down to fit a channel limit
        sub_symbols = list(itr)[:BITFINEX_CHANNEL_LIMIT//2]

    logger.info('Retrieving Done')

    def subscribe(ws: dumpv2.WebSocketDumper):
        subscribe_obj = dict(
            event='subscribe',
            channel=None,
            symbol=None,
        )

        # Subscribe to trades channel
        subscribe_obj['channel'] = 'trades'

        for symbol in sub_symbols:
            subscribe_obj['symbol'] = symbol
            ws.send(json.dumps(subscribe_obj))

        subscribe_obj['channel'] = 'book'
        # set precision to raw
        subscribe_obj['prec'] = 'P0'
        # set frequency to the most frequent == realtime
        subscribe_obj['freq'] = 'F0'
        # set limit to big number
        subscribe_obj['len'] = '100'

        for symbol in sub_symbols:
            subscribe_obj['symbol'] = symbol
            ws.send(json.dumps(subscribe_obj))

    return subscribe

def gen():
    subscribe = subscribe_gen()
    state = BitfinexState()
    return dumpv2.WebSocketDumper(DIR, 'bitfinex', 'wss://api-pub.bitfinex.com/ws/2', subscribe, state)

def main():
    dumpv2.Reconnecter(gen).do()

if __name__ == '__main__':
    main()