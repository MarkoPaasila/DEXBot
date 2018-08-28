import datetime
import logging
import collections
import time
import math
import copy

from .storage import Storage
from .statemachine import StateMachine
from .config import Config
from .helper import truncate

from events import Events
import bitsharesapi
import bitsharesapi.exceptions
import bitshares.exceptions
from bitshares.amount import Amount
from bitshares.amount import Asset
from bitshares.market import Market
from bitshares.account import Account
from bitshares.price import FilledOrder, Order, UpdateCallOrder
from bitshares.instance import shared_bitshares_instance

MAX_TRIES = 3

ConfigElement = collections.namedtuple('ConfigElement', 'key type default title description extra')
# Strategies need to specify their own configuration values, so each strategy can have
# a class method 'configure' which returns a list of ConfigElement named tuples.
# Tuple fields as follows:
# - Key: the key in the bot config dictionary that gets saved back to config.yml
# - Type: one of "int", "float", "bool", "string", "choice"
# - Default: the default value. must be right type.
# - Title: name shown to the user, preferably not too long
# - Description: comments to user, full sentences encouraged
# - Extra:
#       For int: a (min, max, suffix) tuple
#       For float: a (min, max, precision, suffix) tuple
#       For string: a regular expression, entries must match it, can be None which equivalent to .*
#       For bool, ignored
#       For choice: a list of choices, choices are in turn (tag, label) tuples.
#       labels get presented to user, and tag is used as the value saved back to the config dict


class Worker(Storage, StateMachine, Events):
    """ A worker based on this class is intended to work in one market. This class contains
    most common methods needed by a strategy. All prices are passed and returned as BASE/QUOTE
    (In the BREAD:USD market that would be USD/BREAD, 2.5 usd / 1 bread). Sell orders reserve QUOTE,
    and buy orders reserve BASE.

        Worker inherits:

        * :class:`dexbot.storage.Storage`
        * :class:`dexbot.statemachine.StateMachine`
        * ``Events``

        Available attributes:

         * ``worker.bitshares``: instance of ´`bitshares.BitShares()``
         * ``worker.add_state``: Add a specific state
         * ``worker.set_state``: Set finite state machine
         * ``worker.get_state``: Change state of state machine
         * ``worker.account``: The Account object of this worker
         * ``worker.market``: The market used by this worker
         * ``worker.orders``: List of open orders of the worker's account in the worker's market
         * ``worker.balance``: List of assets and amounts available in the worker's account
         * ``worker.log``: a per-worker logger (actually LoggerAdapter) adds worker-specific context:
            worker name & account (Because some UIs might want to display per-worker logs)

        Also, Worker inherits :class:`dexbot.storage.Storage`
        which allows to permanently store data in a sqlite database
        using:

        ``worker["key"] = "value"``

        .. note:: This applies a ``json.loads(json.dumps(value))``!

        Workers must never attempt to interact with the user, they must assume they are running unattended.
        They can log events. If a problem occurs they can't fix they should set self.disabled = True and
        throw an exception. The framework catches all exceptions thrown from event handlers and logs appropriately.
    """

    __events__ = [
        'ontick',
        'onMarketUpdate',
        'onAccount',
        'error_ontick',
        'error_onMarketUpdate',
        'error_onAccount',
        'onOrderMatched',
        'onOrderPlaced',
        'onUpdateCallOrder',
    ]

    @classmethod
    def configure(cls, return_base_config=True):
        """
        Return a list of ConfigElement objects defining the configuration values for 
        this class
        User interfaces should then generate widgets based on this values, gather
        data and save back to the config dictionary for the worker.

        NOTE: when overriding you almost certainly will want to call the ancestor
        and then add your config values to the list.
        """
        # These configs are common to all bots
        base_config = [
            ConfigElement("account", "string", "", "Account", "BitShares account name for the bot to operate with", ""),
            ConfigElement("market", "string", "USD:BTS", "Market",
                          "BitShares market to operate on, in the format ASSET:OTHERASSET, for example \"USD:BTS\"",
                          r"[A-Z\.]+[:\/][A-Z\.]+"),
            ConfigElement('fee_asset', 'string', 'BTS', 'Fee asset', 'Asset to be used to pay transaction fees',
                          r'[A-Z\.]+')
        ]
        if return_base_config:
            return base_config
        return []

    def __init__(
        self,
        name,
        config=None,
        onAccount=None,
        onOrderMatched=None,
        onOrderPlaced=None,
        onMarketUpdate=None,
        onUpdateCallOrder=None,
        ontick=None,
        bitshares_instance=None,
        *args,
        **kwargs
    ):
        # BitShares instance
        self.bitshares = bitshares_instance or shared_bitshares_instance()

        # Storage
        Storage.__init__(self, name)

        # Statemachine
        StateMachine.__init__(self, name)

        # Events
        Events.__init__(self)

        if ontick:
            self.ontick += ontick
        if onMarketUpdate:
            self.onMarketUpdate += onMarketUpdate
        if onAccount:
            self.onAccount += onAccount
        if onOrderMatched:
            self.onOrderMatched += onOrderMatched
        if onOrderPlaced:
            self.onOrderPlaced += onOrderPlaced
        if onUpdateCallOrder:
            self.onUpdateCallOrder += onUpdateCallOrder

        # Redirect this event to also call order placed and order matched
        self.onMarketUpdate += self._callbackPlaceFillOrders

        if config:
            self.config = config
        else:
            self.config = config = Config.get_worker_config_file(name)

        self.worker = config["workers"][name]
        self._account = Account(
            self.worker["account"],
            full=True,
            bitshares_instance=self.bitshares
        )
        self._market = Market(
            config["workers"][name]["market"],
            bitshares_instance=self.bitshares
        )

        # Recheck flag - Tell the strategy to check for updated orders
        self.recheck_orders = False

        # Set fee asset
        fee_asset_symbol = self.worker.get('fee_asset')
        if fee_asset_symbol:
            try:
                self.fee_asset = Asset(fee_asset_symbol)
            except bitshares.exceptions.AssetDoesNotExistsException:
                self.fee_asset = Asset('1.3.0')
        else:
            self.fee_asset = Asset('1.3.0')

        # Settings for bitshares instance
        self.bitshares.bundle = bool(self.worker.get("bundle", False))

        # Disabled flag - this flag can be flipped to True by a worker and
        # will be reset to False after reset only
        self.disabled = False

        # Order expiration time in seconds
        self.expiration = 60 * 60 * 24 * 365 * 5

        # A private logger that adds worker identify data to the LogRecord
        self.log = logging.LoggerAdapter(
            logging.getLogger('dexbot.per_worker'),
            {'worker_name': name,
             'account': self.worker['account'],
             'market': self.worker['market'],
             'is_disabled': lambda: self.disabled}
        )

        self.orders_log = logging.LoggerAdapter(
            logging.getLogger('dexbot.orders_log'), {}
        )

    def get_lowest_market_sell(self, refresh=True):
        """Returns the lowest sell order that is not own, regardless of order size.

        :param refresh:
        :return:
        """

    def get_highest_market_buy(self, refresh=True):
        """ Returns the highest buy order not owned by worker account, regardless of order size.

        :param refresh:
        :return:
        """

    def get_lowest_own_sell(self, refresh=True):
        """ Returns lowest own sell order.

        :param refresh:
        :return:
        """

    def get_highest_own_buy(self, refresh=True):
        """ Returns highest own buy order.

        :param refresh:
        :return:
        """

    def get_price_for_amount_buy(self, amount=None, refresh=True):
        """ Returns the cumulative price for which you could buy the specified amount of QUOTE.
        This method must take into account market fee.

        :param amount:
        :param refresh:
        :return:
        """

    def get_price_for_amount_sell(self, amount=None, refresh=True):
        """ Returns the cumulative price for which you could sell the specified amount of QUOTE

        :param amount:
        :param refresh:
        :return:
        """

    def get_market_center_price(self, depth=0, refresh=True):
        """ Returns the center price of market including own orders.

        :param depth: 0 = calculate from closest opposite orders. non-zero = calculate from specified depth (quote or base?)
        :param refresh:
        :return:
        """

    def get_external_price(self, source):

    def enhance_center_price(self, reference=None, manual_offset=False, balance_based_offset=False, moving_average=0, weighted_average=0):
        """ Returns the passed reference price shifted up or down based on arguments.

        :param reference: Center price to enhance
        :param manual_offset:
        :param balance_based_offset:
        :param moving_average:
        :param weighted_average:
        :return:
        """

    def get_market_spread(self, method, refresh=True):
        """ Get spread from closest opposite orders, including own.

        :param method:
        :param refresh:
        :return:
        """

    def get_own_spread(self, method, refresh=True):
        """ Returns the difference between own closest opposite orders.
        lowest_own_sell_price / highest_own_buy_price - 1

        :param method:
        :param refresh:
        :return:
        """

    def get_order_creation_fee(self, fee_asset):
        """ Returns the cost of creating an order in the asset specified

        :param fee_asset: QUOTE, BASE, BTS, or any other
        :return:
        """

    def get_order_cancellation_fee(self, fee_asset):
        """ Returns the order cancellation fee in the specified asset.

        :param fee_asset:
        :return:
        """

    def get_market_fee(self, asset):
        """ Returns the fee percentage for buying specified asset.

        :param asset:
        :return: Fee percentage in decimal form (0.025)
        """

    def restore_order(self, order):
        """ If an order is partially or completely filled, this will make a new order of original size and price.

        :param order:
        :return:
        """

    @property
    def get_own_market_orders(self, refresh=True):
        """ Return the account's open orders in the current market
        """
        self.account.refresh()
        return [o for o in self.account.openorders if self.worker["market"] == o.market and self.account.openorders]

    @property
    def get_all_own_orders(self, refresh=True):
        """ Return the worker's open orders in all markets
        """
        self.account.refresh()
        return [o for o in self.account.openorders]

    def get_own_buy_orders(self, sort=None, orders=None, refresh=True):
        """ Return ownbuy orders
            :param str sort: DESC or ASC will sort the orders accordingly, default None.
            :param list orders: List of orders. If None given get all orders from Blockchain.
            :return list buy_orders: List of buy orders only.
        """
        buy_orders = []

        if not orders:
            orders = self.orders

        # Find buy orders
        for order in orders:
            if not self.is_sell_order(order):
                buy_orders.append(order)
        if sort:
            buy_orders = self.sort_orders(buy_orders, sort)

        return buy_orders

    def get_own_sell_orders(self, sort=None, orders=None, refresh=True):
        """ Return own sell orders
            :param str sort: DESC or ASC will sort the orders accordingly, default None.
            :param list orders: List of orders. If None given get all orders from Blockchain.
            :return list sell_orders: List of sell orders only.
        """
        sell_orders = []

        if not orders:
            orders = self.orders

        # Find sell orders
        for order in orders:
            if self.is_sell_order(order):
                sell_orders.append(order)

        if sort:
            sell_orders = self.sort_orders(sell_orders, sort)

        return sell_orders

    def is_sell_order(self, order):
        """ Checks if the order is Sell order. Returns False if Buy order
            :param order: Buy / Sell order
            :return: bool: True = Sell order, False = Buy order
        """
        if order['base']['symbol'] != self.market['base']['symbol']:
            return True
        return False

    def is_buy_order(self, order):
        """ Checks if the order is a buy order. Returns False if not.

        :param order:
        :return:
        """

    @staticmethod
    def sort_orders(orders, sort='DESC'):
        """ Return list of orders sorted ascending or descending
            :param list orders: list of orders to be sorted
            :param str sort: ASC or DESC. Default DESC
            :return list: Sorted list of orders.
        """
        if sort.upper() == 'ASC':
            reverse = False
        elif sort.upper() == 'DESC':
            reverse = True
        else:
            return None

        # Sort orders by price
        return sorted(orders, key=lambda order: order['price'], reverse=reverse)

    @staticmethod
    def get_original_order(order_id, return_none=True):
        """ Returns the Order object for the order_id

            :param str|dict order_id: blockchain object id of the order
                can be an order dict with the id key in it
            :param bool return_none: return None instead of an empty
                Order object when the order doesn't exist
        """
        if not order_id:
            return None
        if 'id' in order_id:
            order_id = order_id['id']
        order = Order(order_id)
        if return_none and order['deleted']:
            return None
        return order

    def get_updated_order(self, order_id):
        """ Tries to get the updated order from the API
            returns None if the order doesn't exist

            :param str|dict order_id: blockchain object id of the order
                can be an order dict with the id key in it
        """
        if isinstance(order_id, dict):
            order_id = order_id['id']

        # Get the limited order by id
        order = None
        for limit_order in self.account['limit_orders']:
            if order_id == limit_order['id']:
                order = limit_order
                break
        else:
            return order

        order = self.get_updated_limit_order(order)
        return Order(order, bitshares_instance=self.bitshares)

    @property
    def get_updated_orders(self):
        """ Returns all open orders as updated orders
        todo: What exactly? When orders are needed who wants out of date info?
        """
        self.account.refresh()

        limited_orders = []
        for order in self.account['limit_orders']:
            base_asset_id = order['sell_price']['base']['asset_id']
            quote_asset_id = order['sell_price']['quote']['asset_id']
            # Check if the order is in the current market
            if not self.is_current_market(base_asset_id, quote_asset_id):
                continue

            limited_orders.append(self.get_updated_limit_order(order))

        return [
            Order(o, bitshares_instance=self.bitshares)
            for o in limited_orders
        ]

    @staticmethod
    def get_updated_limit_order(limit_order):
        """ Returns a modified limit_order so that when passed to Order class,
            will return an Order object with updated amount values
            :param limit_order: an item of Account['limit_orders']
            :return: dict
            todo: unify naming. And when would we not want an updated order?
        """
        o = copy.deepcopy(limit_order)
        price = o['sell_price']['base']['amount'] / o['sell_price']['quote']['amount']
        base_amount = o['for_sale']
        quote_amount = base_amount / price
        o['sell_price']['base']['amount'] = base_amount
        o['sell_price']['quote']['amount'] = quote_amount
        return o

    @property
    def market(self):
        """ Return the market object as :class:`bitshares.market.Market`
        """
        return self._market

    @property
    def account(self):
        """ Return the full account as :class:`bitshares.account.Account` object!

            Can be refreshed by using ``x.refresh()``
        """
        return self._account

    def balance(self, asset, fee_reservation=False):
        """ Return the balance of your worker's account for a specific asset
        """
        return self._account.balance(asset)

    @property
    def balances(self):
        """ Return the balances of your worker's account
        """
        return self._account.balances

    def _callbackPlaceFillOrders(self, d):
        """ This method distinguishes notifications caused by Matched orders
            from those caused by placed orders
            todo: can this be renamed to _instantFill()?
        """
        if isinstance(d, FilledOrder):
            self.onOrderMatched(d)
        elif isinstance(d, Order):
            self.onOrderPlaced(d)
        elif isinstance(d, UpdateCallOrder):
            self.onUpdateCallOrder(d)
        else:
            pass

    def execute_bundle(self):
        """ Execute a bundle of operations
        """
        self.bitshares.blocking = "head"
        r = self.bitshares.txbuffer.broadcast()
        self.bitshares.blocking = False
        return r

    def _cancel_orders(self, orders):
        try:
            self.retry_action(
                self.bitshares.cancel,
                orders, account=self.account, fee_asset=self.fee_asset['id']
            )
        except bitsharesapi.exceptions.UnhandledRPCError as e:
            if str(e).startswith('Assert Exception: maybe_found != nullptr: Unable to find Object'):
                # The order(s) we tried to cancel doesn't exist
                self.bitshares.txbuffer.clear()
                return False
            else:
                self.log.exception("Unable to cancel order")
        except bitshares.exceptions.MissingKeyError:
            self.log.exception('Unable to cancel order(s), private key missing.')

        return True

    def cancel_orders(self, orders):
        """ Cancel specific order(s)
        """
        if not isinstance(orders, (list, set, tuple)):
            orders = [orders]

        orders = [order['id'] for order in orders if 'id' in order]

        success = self._cancel(orders)
        if not success and len(orders) > 1:
            # One of the order cancels failed, cancel the orders one by one
            for order in orders:
                self._cancel(order)

    def cancel_all_orders(self):
        """ Cancel all orders of the worker's account
        """
        self.log.info('Canceling all orders')
        if self.orders:
            self.cancel(self.orders)
        self.log.info("Orders canceled")

    def pause_worker(self):
        """ Pause the worker
        """
        # By default, just call cancel_all(); strategies may override this method
        self.cancel_all()
        self.clear_orders()

    def buy(self, amount, price, return_none=False, *args, **kwargs):
        """ Places a buy order in the market

        :param amount:
        :param price:
        :param return_none:
        :param args:
        :param kwargs:
        :return:
        """
        symbol = self.market['base']['symbol']
        precision = self.market['base']['precision']
        base_amount = truncate(price * amount, precision)

        # Don't try to place an order of size 0
        if not base_amount:
            self.log.critical('Trying to buy 0')
            self.disabled = True
            return None

        # Make sure we have enough balance for the order
        if self.balance(self.market['base']) < base_amount:
            self.log.critical(
                "Insufficient buy balance, needed {} {}".format(
                    base_amount, symbol)
            )
            self.disabled = True
            return None

        self.log.info(
            'Placing a buy order for {} {} @ {}'.format(
                base_amount, symbol, round(price, 8))
        )

        # Place the order
        buy_transaction = self.retry_action(
            self.market.buy,
            price,
            Amount(amount=amount, asset=self.market["quote"]),
            account=self.account.name,
            expiration=self.expiration,
            returnOrderId="head",
            fee_asset=self.fee_asset['id'],
            *args,
            **kwargs
        )

        self.log.debug('Placed buy order {}'.format(buy_transaction))
        buy_order = self.get_order(buy_transaction['orderid'], return_none=return_none)
        if buy_order and buy_order['deleted']:
            # The API doesn't return data on orders that don't exist
            # We need to calculate the data on our own
            buy_order = self.calculate_order_data(buy_order, amount, price)
            self.recheck_orders = True

        return buy_order

    def sell(self, amount, price, return_none=False, *args, **kwargs):
        """ Places a sell order in the market

        :param amount:
        :param price:
        :param return_none:
        :param args:
        :param kwargs:
        :return:
        """
        symbol = self.market['quote']['symbol']
        precision = self.market['quote']['precision']
        quote_amount = truncate(amount, precision)

        # Don't try to place an order of size 0
        if not quote_amount:
            self.log.critical('Trying to sell 0')
            self.disabled = True
            return None

        # Make sure we have enough balance for the order
        if self.balance(self.market['quote']) < quote_amount:
            self.log.critical(
                "Insufficient sell balance, needed {} {}".format(
                    amount, symbol)
            )
            self.disabled = True
            return None

        self.log.info(
            'Placing a sell order for {} {} @ {}'.format(
                quote_amount, symbol, round(price, 8))
        )

        # Place the order
        sell_transaction = self.retry_action(
            self.market.sell,
            price,
            Amount(amount=amount, asset=self.market["quote"]),
            account=self.account.name,
            expiration=self.expiration,
            returnOrderId="head",
            fee_asset=self.fee_asset['id'],
            *args,
            **kwargs
        )

        self.log.debug('Placed sell order {}'.format(sell_transaction))
        sell_order = self.get_order(sell_transaction['orderid'], return_none=return_none)
        if sell_order and sell_order['deleted']:
            # The API doesn't return data on orders that don't exist
            # We need to calculate the data on our own
            sell_order = self.calculate_order_data(sell_order, amount, price)
            sell_order.invert()
            self.recheck_orders = True

        return sell_order

    def calculate_order_data(self, order, amount, price):
        quote_asset = Amount(amount, self.market['quote']['symbol'])
        order['quote'] = quote_asset
        order['price'] = price
        base_asset = Amount(amount * price, self.market['base']['symbol'])
        order['base'] = base_asset
        return order

    def is_current_market(self, base_asset_id, quote_asset_id):
        """ Returns True if given asset id's are of the current market
        """
        if quote_asset_id == self.market['quote']['id']:
            if base_asset_id == self.market['base']['id']:
                return True
            return False
        # todo: should we return true if market is opposite?
        if quote_asset_id == self.market['base']['id']:
            if base_asset_id == self.market['quote']['id']:
                return True
            return False
        return False

    def purge(self):
        """ Clear all the worker data from the database and cancel all orders
        todo: rename to purge_all_data or similar
        """
        self.clear_orders()
        self.cancel_all()
        self.clear()

    @staticmethod
    def purge_worker_data(worker_name):
        Storage.clear_worker_data(worker_name)

    def count_asset(self, order_ids=None, return_asset=False, refresh=True):
        """ Returns the combined amount of the given order ids and the account balance
            The amounts are returned in quote and base assets of the market

            :param order_ids: list of order ids to be added to the balance
            :param return_asset: true if returned values should be Amount instances
            :return: dict with keys quote and base
        todo: When would we want the sum of a subset of orders? Why order_ids? Maybe just specify asset?
        """
        quote = 0
        base = 0
        quote_asset = self.market['quote']['id']
        base_asset = self.market['base']['id']

        # Total balance calculation
        for balance in self.balances:
            if balance.asset['id'] == quote_asset:
                quote += balance['amount']
            elif balance.asset['id'] == base_asset:
                base += balance['amount']

        if order_ids is None:
            # Get all orders from Blockchain
            order_ids = [order['id'] for order in self.orders]
        if order_ids:
            orders_balance = self.orders_balance(order_ids)
            quote += orders_balance['quote']
            base += orders_balance['base']

        if return_asset:
            quote = Amount(quote, quote_asset)
            base = Amount(base, base_asset)

        return {'quote': quote, 'base': base}

    def account_total_value(self, return_asset, refresh=True):
        """ Returns the total value of the account in given asset
            :param str return_asset: Asset which is wanted as return
            :return: float: Value of the account in one asset
        """
        total_value = 0

        # Total balance calculation
        for balance in self.balances:
            if balance['symbol'] != return_asset:
                # Convert to asset if different
                total_value += self.convert_asset(balance['amount'], balance['symbol'], return_asset)
            else:
                total_value += balance['amount']

        # Orders balance calculation
        for order in self.all_orders:
            updated_order = self.get_updated_order(order['id'])

            if not order:
                continue
            if updated_order['base']['symbol'] == return_asset:
                total_value += updated_order['base']['amount']
            else:
                total_value += self.convert_asset(
                    updated_order['base']['amount'],
                    updated_order['base']['symbol'],
                    return_asset
                )

        return total_value

    @staticmethod
    def convert_asset(from_value, from_asset, to_asset, refresh=True):
        """ Converts asset to another based on the latest market value
            :param from_value: Amount of the input asset
            :param from_asset: Symbol of the input asset
            :param to_asset: Symbol of the output asset
            :return: Asset converted to another asset as float value
        """
        market = Market('{}/{}'.format(from_asset, to_asset))
        ticker = market.ticker()
        latest_price = ticker.get('latest', {}).get('price', None)
        return from_value * latest_price

    def get_allocated_assets(self, order_ids, return_asset=False, refresh=True):
        """ Returns the amount of QUOTE and BASE allocated in orders, and that do not show up in available balance

        :param order_ids:
        :param return_asset:
        :param refresh:
        :return:
        """
        if not order_ids:
            order_ids = []
        elif isinstance(order_ids, str):
            order_ids = [order_ids]

        quote = 0
        base = 0
        quote_asset = self.market['quote']['id']
        base_asset = self.market['base']['id']

        for order_id in order_ids:
            order = self.get_updated_order(order_id)
            if not order:
                continue
            asset_id = order['base']['asset']['id']
            if asset_id == quote_asset:
                quote += order['base']['amount']
            elif asset_id == base_asset:
                base += order['base']['amount']

        if return_asset:
            quote = Amount(quote, quote_asset)
            base = Amount(base, base_asset)

        return {'quote': quote, 'base': base}

    def calculate_worker_value(self, unit_of_measure, refresh=True):
        """ Returns the combined value of allocated and available QUOTE and BASE, measured in "unit_of_measure".

        :param unit_of_measure:
        :param refresh:
        :return:
        """

    def retry_action(self, action, *args, **kwargs):
        """
        Perform an action, and if certain suspected-to-be-spurious graphene bugs occur,
        instead of bubbling the exception, it is quietly logged (level WARN), and try again
        tries a fixed number of times (MAX_TRIES) before failing
        """
        tries = 0
        while True:
            try:
                return action(*args, **kwargs)
            except bitsharesapi.exceptions.UnhandledRPCError as e:
                if "Assert Exception: amount_to_sell.amount > 0" in str(e):
                    if tries > MAX_TRIES:
                        raise
                    else:
                        tries += 1
                        self.log.warning("Ignoring: '{}'".format(str(e)))
                        self.bitshares.txbuffer.clear()
                        self.account.refresh()
                        time.sleep(2)
                elif "now <= trx.expiration" in str(e):  # Usually loss of sync to blockchain
                    if tries > MAX_TRIES:
                        raise
                    else:
                        tries += 1
                        self.log.warning("retrying on '{}'".format(str(e)))
                        self.bitshares.txbuffer.clear()
                        time.sleep(6)  # Wait at least a BitShares block
                else:
                    raise

    def write_order_log(self, worker_name, order):
        operation_type = 'TRADE'

        if order['base']['symbol'] == self.market['base']['symbol']:
            base_symbol = order['base']['symbol']
            base_amount = -order['base']['amount']
            quote_symbol = order['quote']['symbol']
            quote_amount = order['quote']['amount']
        else:
            base_symbol = order['quote']['symbol']
            base_amount = order['quote']['amount']
            quote_symbol = order['base']['symbol']
            quote_amount = -order['base']['amount']

        message = '{};{};{};{};{};{};{};{}'.format(
            worker_name,
            order['id'],
            operation_type,
            base_symbol,
            base_amount,
            quote_symbol,
            quote_amount,
            datetime.datetime.now().isoformat()
        )

        self.orders_log.info(message)
