# Python imports
import math

# Project imports
from dexbot.strategies.base import StrategyBase, ConfigElement
from dexbot.qt_queue.idle_queue import idle_add

# Third party imports
from bitshares.market import Market

STRATEGY_NAME = 'Support Price Strategy'


class Strategy(StrategyBase):
    """ Support Price Strategy

        This strategy is supposed to place a buy order just below the lowest sell order on the market.
        This will hopefully resist the price moving down and encourage it to move up.
    """

    @classmethod
    def configure(cls, return_base_config=True):

        return StrategyBase.configure(return_base_config) + [
            ConfigElement('order_size_quote', 'float', 1, 'Order Size',
                          'THe size of the only order to be maintained, in units of quote asset',
                          (0, 10000000, 8, '')),
            ConfigElement('distance', 'float', 1, 'Keep Distance',
                          'How far below the lowest sell order should we maintain our buy order. Price measured in base',
                          (0, 10000000, 8, '')),
        ]

    def __init__(self, *args, **kwargs):
        # Initializes StrategyBase class
        super().__init__(*args, **kwargs)

        """ Using self.log.info() you can print text on the GUI to inform user on what is the bot currently doing. This
            is also written in the dexbot.log file.
        """
        self.log.info("Initializing {}...".format(STRATEGY_NAME))

        # Tick counter
        self.counter = 0

        # Define Callbacks
        self.onMarketUpdate += self.maintain_strategy
        self.onAccount += self.maintain_strategy
        self.ontick += self.tick

        self.error_ontick = self.error
        self.error_onMarketUpdate = self.error
        self.error_onAccount = self.error
        """ Define what strategy does on the following events
           - Bitshares account has been modified = self.onAccount
           - Market has been updated = self.onMarketUpdate

           These events are tied to methods which decide how the loop goes, unless the strategy is static, which
           means that it will only do one thing and never do 
       """

        # Get view
        self.view = kwargs.get('view')

        """ Worker parameters

            There values are taken from the worker's config file.
            Name of the worker is passed in the **kwargs.
        """
        self.worker_name = kwargs.get('name')

        self.order_size_quote = float(self.worker.get('order_size_quote'))
        self.keep_distance = float(self.worker.get('distance'))

        """ Strategy variables

            These variables are for the strategy only and should be initialized here if wanted into self's scope.
        """
        self.market_center_price = 0.0
        self.supporting_order = None
        self.market_ask = None
        self.last_market_ask_price = 0.0

        if self.view:
            self.update_gui_slider()

        self.log.info("{} initialized.".format(STRATEGY_NAME))

    def maintain_strategy(self):
        """ Strategy main loop
        """
        self.log.info("Starting {}".format(STRATEGY_NAME))
        self.log.info("Checking where lowest market ask is")
        self.market_ask = self.get_lowest_market_sell_order()
        market_bid = self.get_market_sell_price()
        self.log.info('Lowest market ask: {} @ {:.8f}'.format(
            self.market_ask['base']['amount'], self.market_ask['price']))
        self.supporting_order = self.get_highest_own_buy_order()
        if self.supporting_order == None:
            self.log.info("We don't have a buy order. Must place one...")
            support_price = float(self.market_ask['price']) - self.keep_distance
            if self.balance(base) < (self.order_size_quote * support_price):
                self.log.info("Not enough funds to place order. Waiting for more.")
                return
            self.place_market_buy_order(self.order_size_quote,support_price)
            self.log.info("Placed a buy order")
            self.last_market_ask_price = float(self.market_ask['price'])
            return
        elif self.supporting_order:
            self.log.info("Our order is still there. Checking if market ask has changed place.")
            if self.last_market_ask_price == market_bid:
                self.log.info("Market situation hasn't changed. Nothing to do.")
                return
            elif self.last_market_ask_price != market_bid:
                self.log.info('Market situation changed. Must reset order')
                self.cancel_all_orders()
                support_price = market_bid - self.keep_distance
                self.place_market_buy_order(self.order_size_quote,support_price)
                self.log.info('Order replaced')
                return




    def error(self, *args, **kwargs):
        """ Defines what happens when error occurs """
        self.disabled = False

    def tick(self, d):
        """ Ticks come in on every block """
        if not (self.counter or 0) % 3:
            self.maintain_strategy()
        self.counter += 1

    def update_gui_slider(self):
        """ Updates GUI slider on the workers list """
        latest_price = self.ticker().get('latest', {}).get('price', None)
        if not latest_price:
            return

        order_ids = None
        orders = self.get_own_orders

        if orders:
            order_ids = [order['id'] for order in orders if 'id' in order]

        total_balance = self.count_asset(order_ids)
        total = (total_balance['quote'] * latest_price) + total_balance['base']

        if not total:  # Prevent division by zero
            percentage = 50
        else:
            percentage = (total_balance['base'] / total) * 100
        idle_add(self.view.set_worker_slider, self.worker_name, percentage)
        self['slider'] = percentage
