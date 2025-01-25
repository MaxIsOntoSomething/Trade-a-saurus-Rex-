import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objs as go
import pandas as pd
from datetime import datetime, timedelta

class TradingDashboard:
    def __init__(self, bot, host='localhost', port=8050):
        self.bot = bot
        self.host = host
        self.port = port
        self.app = dash.Dash(__name__)
        self.setup_layout()
        
    def setup_layout(self):
        self.app.layout = html.Div([
            html.H1('Binance Trading Bot Dashboard'),
            
            # Price Charts
            dcc.Graph(id='price-chart'),
            
            # Trade History
            html.Div(id='trade-history'),
            
            # Portfolio Stats
            html.Div(id='portfolio-stats'),
            
            # Auto-refresh
            dcc.Interval(
                id='interval-component',
                interval=5*1000,  # 5 seconds
                n_intervals=0
            )
        ])
        
        self.setup_callbacks()
        
    def setup_callbacks(self):
        @self.app.callback(
            Output('price-chart', 'figure'),
            Input('interval-component', 'n_intervals')
        )
        def update_price_chart(_):
            # Get price data from bot
            prices = self.bot.ws_manager.last_prices
            
            traces = []
            for symbol in self.bot.valid_symbols:
                if symbol in prices:
                    trace = go.Scatter(
                        x=[prices[symbol]['timestamp']],
                        y=[prices[symbol]['price']],
                        name=symbol
                    )
                    traces.append(trace)
                    
            return {'data': traces}
            
    def run(self):
        try:
            self.app.run_server(
                host=self.host,
                port=self.port,
                debug=False
            )
        except Exception as e:
            self.bot.logger.error(f"Dashboard error: {e}")
