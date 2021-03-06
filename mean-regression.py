import alpaca_trade_api as tradeapi
import requests
import time
from ta.trend import macd
import numpy as np
from datetime import datetime, timedelta, date
from pytz import timezone
from dotenv import load_dotenv

load_dotenv()
import os

import logging

logger = logging.getLogger()

# Replace these with your API connection info from the dashboard
base_url = os.getenv("BASE_URL")
api_key_id = os.getenv("API_KEY_ID")
api_secret = os.getenv("API_SECRET")
ws_base_url = os.getenv("WS_BASE_URL")
polygon_key_id = os.getenv("POLYGON_KEY_ID")

TODAY = datetime.today().strftime('%Y-%m-%d')
YESTERDAY = datetime.strftime(datetime.now() - timedelta(1), '%Y-%m-%d')

#print('POLYGON_KEY_ID', os.getenv("POLYGON_KEY_ID"))

api = tradeapi.REST()

session = requests.session()

# We only consider stocks with per-share prices inside this range
min_share_price = 2.0
max_share_price = 13.0
# Minimum previous-day dollar volume for a stock we might consider
min_last_dv = 500000
# Stop limit to default to
default_stop = .95
# How much of our portfolio to allocate to any one position
risk = 0.001

# Max take Profit percentage. Want to lock in 0.035
take_profit = .0055
daily_take_profit = 0

# We want there to be a minimum dollar profit amount
minimum_absolute_dollar_profit = 100
current_dollar_profit = 0


def get_1000m_history_data(symbols):
    print('Getting historical data...')
    logger.info('Getting historical data...')
    minute_history = {}
    c = 0
    for symbol in symbols:
        try:
            minute_history[symbol] = api.polygon.historic_agg_v2(
                timespan="minute", symbol=symbol, multiplier=1, limit=1000, _from=YESTERDAY, to=TODAY, unadjusted=False
            ).df
            c += 1
            if 'vwap' in minute_history[symbol]:
                minute_history[symbol].drop('vwap', axis=1, inplace=True)

            print('{}/{}'.format(c, len(symbols)))
            logger.info('{}/{}'.format(c, len(symbols)))
        except Exception as ex:
            print(ex)
            print('Failure.')
            logger.info('Failure: {}', ex)
    return minute_history


def get_tickers():
    print('Getting current ticker data...')
    logger.info('Getting current ticker data...')
    tickers = api.polygon.all_tickers()
    print('Success.')
    logger.info('Success.')
    assets = api.list_assets()
    symbols = [asset.symbol for asset in assets if asset.tradable]
    return [ticker for ticker in tickers if (
        ticker.ticker in symbols and
        ticker.lastTrade['p'] >= min_share_price and
        ticker.lastTrade['p'] <= max_share_price and
        ticker.prevDay['v'] * ticker.lastTrade['p'] > min_last_dv and
        ticker.todaysChangePerc >= 3.5
    )]

def liquite_positions_and_orders():
    url = "https://paper-api.alpaca.markets/v2/positions"
    order_url = "https://paper-api.alpaca.markets/v2/orders"

    payload = {}
    headers = {
        'APCA-API-KEY-ID': 'PKW2OS09URIGNRTL1I63',
        'APCA-API-SECRET-KEY': 'ae4VxQeqVeWJsKVlH0QlLkY5ThsQ6MjtI7RczCLm'
    }

    response = requests.request("DELETE", url, headers=headers, data=payload)
    print(response)
    orders_response = requests.request("DELETE", order_url, headers=headers, data=payload)
    print(orders_response)
    return

def find_stop(current_value, minute_history, now):
    series = minute_history['low'][-100:] \
                .dropna().resample('5min').min()
    series = series[now.floor('1D'):]
    diff = np.diff(series.values)
    low_index = np.where((diff[:-1] <= 0) & (diff[1:] > 0))[0] + 1
    if len(low_index) > 0:
        return series[low_index[-1]] - 0.01
    return current_value * default_stop

def find_stop_short(current_value, minute_history, now):
    series = minute_history['low'][-100:] \
                .dropna().resample('5min').min()
    series = series[now.floor('1D'):]
    diff = np.diff(series.values)
    low_index = np.where((diff[:-1] <= 0) & (diff[1:] > 0))[0] + 1
    if len(low_index) > 0:
        return series[low_index[-1]] + 0.01
    return current_value * default_stop


def run(tickers, market_open_dt, market_close_dt):
    # Establish streaming connection
    conn = tradeapi.StreamConn(data_stream='polygon')

    # Update initial state with information from tickers
    volume_today = {}
    prev_closes = {}
    for ticker in tickers:
        symbol = ticker.ticker
        prev_closes[symbol] = ticker.prevDay['c']
        volume_today[symbol] = ticker.day['v']

    symbols = [ticker.ticker for ticker in tickers]
    print('Tracking {} symbols.'.format(len(symbols)))
    logger.info('Tracking {} symbols.'.format(len(symbols)))
    minute_history = get_1000m_history_data(symbols)

    portfolio_value = float(api.get_account().portfolio_value)

    print(api.get_account())
    print('portfolio_value', portfolio_value)

    open_orders = {}
    positions = {}

    # Cancel any existing open orders on watched symbols
    existing_orders = api.list_orders(limit=500)
    for order in existing_orders:
        if order.symbol in symbols:
            api.cancel_order(order.id)

    stop_prices = {}
    latest_cost_basis = {}

    # Track any positions bought during previous executions
    existing_positions = api.list_positions()
    for position in existing_positions:
        if position.symbol in symbols:
            positions[position.symbol] = float(position.qty)
            # Recalculate cost basis and stop price
            latest_cost_basis[position.symbol] = float(position.cost_basis)
            stop_prices[position.symbol] = (
                float(position.cost_basis) * default_stop
            )

    # Keep track of what we're buying/selling
    target_prices = {}
    partial_fills = {}

    # Handle account updates here (as opposed to in the second tick)
    @conn.on(r'account_update')
    async def handle_account_update(conn, channel, data):
        print(data)
        # Check the aggregated profit for the entire day
        current_value = float(api.get_account().portfolio_value)
        previous_value = float(api.get_account().last_equity)

        daily_take_profit = ((float(current_value) - float(api.get_account().last_equity)) / previous_value) * 100
        daily_take_profit = float("{:.3f}".format(daily_take_profit))
        print('handle account update')


    # Use trade updates to keep track of our portfolio
    @conn.on(r'trade_update')
    async def handle_trade_update(conn, channel, data):
        symbol = data.order['symbol']
        last_order = open_orders.get(symbol)
        if last_order is not None:
            event = data.event
            if event == 'partial_fill':
                qty = int(data.order['filled_qty'])
                if data.order['side'] == 'sell':
                    qty = qty * -1
                positions[symbol] = (
                    positions.get(symbol, 0) - partial_fills.get(symbol, 0)
                )
                partial_fills[symbol] = qty
                positions[symbol] += qty
                open_orders[symbol] = data.order
            elif event == 'fill':
                qty = int(data.order['filled_qty'])
                if data.order['side'] == 'sell':
                    qty = qty * -1
                positions[symbol] = (
                    positions.get(symbol, 0) - partial_fills.get(symbol, 0)
                )
                partial_fills[symbol] = 0
                positions[symbol] += qty
                open_orders[symbol] = None
            elif event == 'canceled' or event == 'rejected':
                partial_fills[symbol] = 0
                open_orders[symbol] = None



    @conn.on(r'A$')
    async def handle_second_bar(conn, channel, data):
        symbol = data.symbol
        # First, aggregate 1s bars for up-to-date MACD calculations
        ts = data.start
        ts -= timedelta(seconds=ts.second, microseconds=ts.microsecond)
        try:
            current = minute_history[data.symbol].loc[ts]
        except KeyError:
            current = None
        new_data = []
        if current is None:
            new_data = [
                data.open,
                data.high,
                data.low,
                data.close,
                data.volume
            ]
        else:
            new_data = [
                current.open,
                data.high if data.high > current.high else current.high,
                data.low if data.low < current.low else current.low,
                data.close,
                current.volume + data.volume
            ]
        minute_history[symbol].loc[ts] = new_data

        # Next, check for existing orders for the stock
        existing_order = open_orders.get(symbol)
        if existing_order is not None:
            # Make sure the order's not too old
            submission_ts = existing_order.submitted_at.astimezone(
                timezone('America/New_York')
            )
            order_lifetime = ts - submission_ts
            if order_lifetime.seconds // 60 > 1:
                # Cancel it so we can try again for a fill
                api.cancel_order(existing_order.id)
            return

        # Now we check to see if it might be time to buy or sell
        since_market_open = ts - market_open_dt
        until_market_close = market_close_dt - ts
        #print('percentage_value', daily_take_profit)

        if (
            since_market_open.seconds // 60 > 15 and
            since_market_open.seconds // 60 < 60
        ):
            # Check for buy signals

            # See if we've already bought in first
            position = positions.get(symbol, 0)
            if position > 0:
                return
            # See how high the price went during the first 15 minutes
            lbound = market_open_dt
            ubound = lbound + timedelta(minutes=15)
            high_15m = 0
            try:
                high_15m = minute_history[symbol][lbound:ubound]['high'].max()
            except Exception as e:
                # Because we're aggregating on the fly, sometimes the datetime
                # index can get messy until it's healed by the minute bars
                return

            # Get the change since yesterday's market close
            daily_pct_change = (
                (data.close - prev_closes[symbol]) / prev_closes[symbol]
            )
            if (
                daily_pct_change > .04 and
                data.close > high_15m and
                volume_today[symbol] > 30000
            ):
                # check for a positive, increasing MACD
                hist = macd(
                    minute_history[symbol]['close'].dropna(),
                    n_fast=12,
                    n_slow=26
                )
                if (
                    hist[-1] < 0 or
                    not (hist[-3] < hist[-2] < hist[-1])
                ):
                    return
                hist = macd(
                    minute_history[symbol]['close'].dropna(),
                    n_fast=40,
                    n_slow=60
                )
                if hist[-1] < 0 or np.diff(hist)[-1] < 0:
                    return

                # Stock has passed all checks; figure out how much to buy
                stop_price = find_stop(
                    data.close, minute_history[symbol], ts
                )
                stop_prices[symbol] = stop_price
                target_prices[symbol] = data.close + (
                    (data.close - stop_price) * 3
                )
                shares_to_buy = portfolio_value * risk // (
                    data.close - stop_price
                )
                if shares_to_buy == 0:
                    shares_to_buy = 1
                shares_to_buy -= positions.get(symbol, 0)
                if shares_to_buy <= 0:
                    return

                print('Submitting buy for {} shares of {} at {}'.format(
                    shares_to_buy, symbol, data.close
                ))
                logger.info('Submitting buy for {} shares of {} at {}'.format(
                    shares_to_buy, symbol, data.close
                ))
                try:
                    o = api.submit_order(
                        symbol=symbol, qty=str(shares_to_buy), side='sell',
                        type='limit', time_in_force='day',
                        limit_price=str(data.close)
                    )
                    open_orders[symbol] = o
                    latest_cost_basis[symbol] = data.close
                except Exception as e:
                    print(e)
                return
        # if daily_take_profit >= take_profit :
        #     # daily take profit has been achieved. We can wind down our positions as quickly as possible.
        #     logger.info('Take Profit', daily_take_profit)
        #     liquite_positions_and_orders()
        #     # Liquidate remaining positions on watched symbols at market
        #     try:
        #         position = api.get_position(symbol)
        #     except Exception as e:
        #         # Exception here indicates that we have no position
        #         if len(symbols) <= 0:
        #             conn.close()
        #         conn.deregister([
        #             'A.{}'.format(symbol),
        #             'AM.{}'.format(symbol)
        #         ])
        #         return
        #     logger.info('Take profit achieved. Liquidating remaining position in {}'.format(
        #         symbol))
        #     print('Take profit achieved. Liquidating remaining position in {}'.format(
        #         symbol)
        #     )
        #     api.submit_order(
        #         symbol=symbol, qty=position.qty, side='sell',
        #         type='market', time_in_force='day'
        #     )
        #     api.cancel_order()
        #     symbols.remove(symbol)
        #     if len(symbols) <= 0:
        #         conn.close()
        #     conn.deregister([
        #         'A.{}'.format(symbol),
        #         'AM.{}'.format(symbol)
        #     ])

        if(
            since_market_open.seconds // 60 >= 24 and
            until_market_close.seconds // 60 > 15
        ):
            # Check for liquidation signals

            # We can't liquidate if there's no position
            position = positions.get(symbol, 0)
            if position == 0:
                return

            # Sell for a loss if it's fallen below our stop price
            # Sell for a loss if it's below our cost basis and MACD < 0
            # Sell for a profit if it's above our target price
            hist = macd(
                minute_history[symbol]['close'].dropna(),
                n_fast=13,
                n_slow=21
            )
            if (
                data.close <= stop_prices[symbol] or
                (data.close >= target_prices[symbol] and hist[-1] <= 0) or
                (data.close <= latest_cost_basis[symbol] and hist[-1] <= 0)
            ):
                print('Submitting sell for {} shares of {} at {}'.format(
                    position, symbol, data.close
                ))
                logger.info('Submitting sell for {} shares of {} at {}'.format(
                    position, symbol, data.close
                ))
                try:
                    o = api.submit_order(
                        symbol=symbol, qty=str(position), side='sell',
                        type='limit', time_in_force='day',
                        limit_price=str(data.close)
                    )
                    open_orders[symbol] = o
                    latest_cost_basis[symbol] = data.close
                except Exception as e:
                    print(e)
            return
        elif (
            until_market_close.seconds // 60 <= 15
        ):
            logger.info('Take Profit', daily_take_profit)
            # Liquidate remaining positions on watched symbols at market
            try:
                position = api.get_position(symbol)
            except Exception as e:
                # Exception here indicates that we have no position
                return
            logger.info('Trading day over. Liquidating remaining position in {}'.format(
                symbol))
            print('Trading day over. Liquidating remaining position in {}'.format(
                symbol)
            )
            api.submit_order(
                symbol=symbol, qty=position.qty, side='buy',
                type='market', time_in_force='day'
            )
            api.cancel_order()
            symbols.remove(symbol)
            if len(symbols) <= 0:
                conn.close()
            conn.deregister([
                'A.{}'.format(symbol),
                'AM.{}'.format(symbol)
            ])

    # Replace aggregated 1s bars with incoming 1m bars
    @conn.on(r'AM$')
    async def handle_minute_bar(conn, channel, data):
        ts = data.start
        ts -= timedelta(microseconds=ts.microsecond)
        minute_history[data.symbol].loc[ts] = [
            data.open,
            data.high,
            data.low,
            data.close,
            data.volume
        ]
        volume_today[data.symbol] += data.volume

    channels = ['trade_updates', 'account_updates']
    for symbol in symbols:
        symbol_channels = ['A.{}'.format(symbol), 'AM.{}'.format(symbol)]
        channels += symbol_channels

    print('Watching {} symbols.'.format(len(symbols)))
    logger.info('Watching {} symbols.'.format(len(symbols)))
    run_ws(conn, channels)


# Handle failed websocket connections by reconnecting
def run_ws(conn, channels):
    try:
        conn.run(channels)
    except Exception as e:
        print(e)
        conn.close()
        run_ws(conn, channels)


if __name__ == "__main__":
    # Get when the market opens or opened today
    nyc = timezone('America/New_York')
    today = datetime.today().astimezone(nyc)
    today_str = datetime.today().astimezone(nyc).strftime('%Y-%m-%d')
    calendar = api.get_calendar(start=today_str, end=today_str)[0]
    market_open = today.replace(
        hour=calendar.open.hour,
        minute=calendar.open.minute,
        second=0
    )
    market_open = market_open.astimezone(nyc)
    market_close = today.replace(
        hour=calendar.close.hour,
        minute=calendar.close.minute,
        second=0
    )
    market_close = market_close.astimezone(nyc)

    # Wait until just before we might want to trade
    current_dt = datetime.today().astimezone(nyc)
    since_market_open = current_dt - market_open

    # Get the current daily value
    while since_market_open.seconds // 60 <= 7:
        time.sleep(1)
        since_market_open = current_dt - market_open

    fmt = '%(asctime)s:%(filename)s:%(lineno)d:%(levelname)s:%(name)s:%(message)s'
    logging.basicConfig(level=logging.INFO, format=fmt)
    today = date.today()
    date_log_name = today.strftime("%d/%m/%Y")
    fh = logging.FileHandler('console.log')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt))
    logger.addHandler(fh)
    run(get_tickers(), market_open, market_close)