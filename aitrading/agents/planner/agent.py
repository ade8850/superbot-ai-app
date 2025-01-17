# aitrading/agents/planner/agent.py

from typing import Dict, List
from datetime import datetime, timedelta
from pathlib import Path
from jinja2 import Template
import os
import time
from rich.console import Console
import logging

from .anthropic import AnthropicClient
from .gemini import GeminiClient
from .openai import OpenAIClient
from ...models import (
    TradingParameters, TradingPlan, ExistingOrder,
    OrderCancellation, generate_uuid_short,
)
from ...tools.bybit.market_data import MarketDataTool
from ...tools.bybit.orders import OrdersTool
from ...tools.charts import ChartGeneratorTool

console = Console()
logger = logging.getLogger("trader")

class TradingPlanner:
    def __init__(
            self,
            market_data: MarketDataTool,
            orders: OrdersTool,
            chart_generator: ChartGeneratorTool,
            provider_name: str,
            api_key: str
    ):
        self.market_data = market_data
        self.orders = orders
        self.chart_generator = chart_generator

        # Select appropriate client based on provider name
        if provider_name == "anthropic":
            self.ai_client = AnthropicClient(api_key)
        elif provider_name == "openai":
            self.ai_client = OpenAIClient(api_key)
        elif provider_name == "gemini":
            self.ai_client = GeminiClient(api_key)
        else:
            raise ValueError(f"Unsupported AI provider: {provider_name}")

        template_dir = Path(__file__).parent
        self.system_template = Template((template_dir / "system_prompt.j2").read_text())
        self.user_template = Template((template_dir / "user_prompt.j2").read_text())

    def create_trading_plan(self, params: TradingParameters) -> TradingPlan:
        """Create a new trading plan with analysis and planned orders."""
        try:
            # Generate plan and session IDs
            plan_id = generate_uuid_short(8)
            session_id = generate_uuid_short(4)

            # Get current market data
            current_price = self.market_data.get_current_price(params.symbol)
            timeframes = self.market_data.get_analysis_timeframes()  # Rimosso il parametro timeframe

            console.print(">> timeframes: ", timeframes, style="bold green")

            # Get active orders
            try:
                active_orders = self.orders.get_active_orders(params.symbol)
                existing_orders = [ExistingOrder(**order) for order in active_orders]
            except Exception as e:
                console.print(f"[yellow]Warning: Error fetching active orders: {str(e)}[/yellow]")
                existing_orders = []

            # Get current positions
            try:
                current_positions = self.orders.get_positions(params.symbol)
            except Exception as e:
                console.print(f"[yellow]Warning: Error fetching positions: {str(e)}[/yellow]")
                current_positions = []

            # Generate analysis charts
            charts = self._generate_analysis_charts(params.symbol, timeframes)
            if not charts:
                raise ValueError("Failed to generate analysis charts")

            # Prepare template variables
            template_vars = {
                "plan_id": plan_id,
                "session_id": session_id,
                "current_price": current_price,
                "symbol": params.symbol,
                "budget": params.budget,
                "leverage": params.leverage,
                "strategy_instructions": params.strategy_instructions,
                "existing_orders": existing_orders,
                "current_positions": current_positions
            }

            # Generate prompts
            system_prompt = self.system_template.render(**template_vars)
            user_prompt = self.user_template.render(**template_vars)

            # Get AI response
            response_dict = self.ai_client.generate_strategy(system_prompt, user_prompt, charts)

            # Create plan from response
            if 'plan' not in response_dict:
                raise ValueError("AI response missing plan data")

            # Initialize trading plan
            plan_data = response_dict['plan']
            plan_data['id'] = plan_id
            plan_data['session_id'] = session_id
            plan_data['parameters'] = params
            trading_plan = TradingPlan(**plan_data)

            return trading_plan

        except Exception as e:
            raise Exception(f"Error creating trading plan: {str(e)}")

    def _generate_analysis_charts(self, symbol: str, timeframes: List[str]) -> List[bytes]:
        """Generate technical analysis charts for each timeframe."""
        generated_charts = []
        dump_charts = os.getenv('DUMP_CHARTS', '').lower() in ('true', '1', 'yes')

        try:
            # Create .graphs directory if it doesn't exist
            graphs_dir = Path('.graphs')
            graphs_dir.mkdir(exist_ok=True)

            # Clean existing files
            if graphs_dir.exists():
                for file in graphs_dir.glob('*'):
                    try:
                        if file.is_file():
                            file.unlink()
                    except Exception as e:
                        logger.warning(f"Could not remove file {file}: {str(e)}")

            logger.debug("Cleaned/created .graphs directory")

            for timeframe in timeframes:
                try:
                    # Fetch historical data
                    df = self.market_data.fetch_historical_data(symbol, timeframe)
                    
                    # Generate multiple charts for this timeframe
                    timeframe_charts = self.chart_generator.create_charts_for_timeframe(df, timeframe)
                    if timeframe_charts:
                        generated_charts.extend(timeframe_charts)

                        # Save charts if DUMP_CHARTS is enabled
                        if dump_charts:
                            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                            for i, chart in enumerate(timeframe_charts):
                                filename = f"{symbol}_{timeframe}_view{i}_{timestamp}.png"
                                filepath = graphs_dir / filename
                                try:
                                    with open(filepath, 'wb') as f:
                                        f.write(chart)
                                    logger.debug(f"Saved chart: {filename}")
                                except Exception as e:
                                    logger.warning(f"Could not save chart {filename}: {str(e)}")

                except Exception as e:
                    logger.error(f"Error generating charts for {timeframe}: {str(e)}")

            return generated_charts

        except Exception as e:
            logger.error(f"Error in chart generation: {str(e)}")
            return generated_charts  # Return any charts we managed to generate

    def execute_plan(self, plan: TradingPlan) -> Dict:
        """Execute all operations in the trading plan on the exchange."""
        try:
            results = {
                "plan_id": plan.id,
                "cancellations": [],
                "position_updates": [],
                "orders": []
            }

            # Execute cancellations first if any
            if plan.cancellations:
                console.print("\n[yellow]Executing order cancellations...[/yellow]")
                cancellation_results = self._execute_cancellations(plan.cancellations)
                results["cancellations"] = cancellation_results
                time.sleep(1)

            # Execute position updates if any
            if plan.position_updates:
                console.print("\n[yellow]Updating position TP/SL levels...[/yellow]")
                for update in plan.position_updates:
                    try:
                        result = self.orders.set_trading_stops(
                            symbol=update.symbol,
                            takeProfit=update.take_profit,
                            stopLoss=update.stop_loss
                        )
                        results["position_updates"].append({
                            "symbol": update.symbol,
                            "success": True,
                            "result": result
                        })
                    except Exception as e:
                        console.print(f"[red]Error updating {update.symbol}: {str(e)}[/red]")
                        results["position_updates"].append({
                            "symbol": update.symbol,
                            "success": False,
                            "error": str(e)
                        })
                time.sleep(1)

            # Then execute new orders if any
            if plan.orders:
                console.print("\n[yellow]Executing new orders...[/yellow]")
                for order in plan.orders:
                    try:
                        if not self.orders.check_strategy_validity(order):
                            raise ValueError(f"Order {order.id} in plan {plan.id} is no longer valid")

                        self.orders.set_position_settings(
                            symbol=order.symbol,
                            leverage=order.order.entry.leverage
                        )

                        result = self.orders.place_strategy_orders(order)
                        if result.get("errors"):
                            raise ValueError(f"Order execution failed for {order.id}: {result['errors']}")

                        results["orders"].append({
                            "order_id": order.id,
                            "order_link_id": order.order_link_id,
                            "result": result
                        })

                    except Exception as e:
                        console.print(f"[red]Error executing order {order.id}: {str(e)}[/red]")
                        results["orders"].append({
                            "order_id": order.id,
                            "order_link_id": order.order_link_id,
                            "error": str(e)
                        })

            return results

        except Exception as e:
            raise Exception(f"Error executing trading plan: {str(e)}")

    def _execute_cancellations(self, cancellations: List[OrderCancellation]) -> List[Dict]:
        """Execute a list of order cancellations."""
        results = []

        for cancel in cancellations:
            try:
                console.print(f"Cancelling order {cancel.order_link_id} - Reason: {cancel.reason}")
                result = self.orders.cancel_order(
                    symbol=cancel.symbol,
                    order_id=cancel.id,
                    order_link_id=cancel.order_link_id
                )
                results.append({
                    "order_id": cancel.id,
                    "order_link_id": cancel.order_link_id,
                    "result": result,
                    "status": "success"
                })
            except Exception as e:
                console.print(f"[red]Error cancelling order {cancel.order_link_id}: {str(e)}[/red]")
                results.append({
                    "order_id": cancel.id,
                    "order_link_id": cancel.order_link_id,
                    "error": str(e),
                    "status": "failed"
                })

        return results