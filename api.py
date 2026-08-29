"""Flask REST API and WebSocket server for trading bot control."""

import logging
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from typing import Dict, Any

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit, disconnect

import config
from strategy_manager import MultiStrategyBot

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "lumibot-trading-bot-secret"
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Global strategy instance
strategy_bot: MultiStrategyBot = None
connected_clients = set()

# Backtest job tracking
BACKTEST_DIR = "backtests"
os.makedirs(BACKTEST_DIR, exist_ok=True)
backtest_jobs: Dict[str, Dict[str, Any]] = {}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def format_position(position):
    """Format a position object for JSON response."""
    return {
        "symbol": position.symbol,
        "quantity": float(position.quantity),
        "avg_fill_price": float(position.avg_fill_price),
        "current_price": float(strategy_bot.get_last_price(position.symbol) or 0),
        "unrealized_pnl": float(
            (strategy_bot.get_last_price(position.symbol) - position.avg_fill_price)
            * position.quantity
            if strategy_bot.get_last_price(position.symbol)
            else 0
        ),
    }


def format_order(order):
    """Format an order object for JSON response."""
    return {
        "id": str(order.id),
        "symbol": order.asset.symbol if order.asset else "UNKNOWN",
        "side": order.side,
        "quantity": float(order.quantity),
        "price": float(order.price) if order.price else None,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "filled_quantity": float(order.filled_quantity) if order.filled_quantity else 0,
    }


def broadcast_update():
    """Broadcast bot status update to all connected clients."""
    if not strategy_bot or not connected_clients:
        return

    try:
        data = {
            "timestamp": datetime.now().isoformat(),
            "status": "trading" if strategy_bot.is_trading else "stopped",
            "portfolio_value": float(strategy_bot.get_portfolio_value()),
            "cash": float(strategy_bot.get_cash()),
            "positions": [format_position(p) for p in strategy_bot.get_positions()],
            "open_orders": len(strategy_bot.get_orders()),
            "bracket_orders": len(strategy_bot.bracket_orders),
        }
        socketio.emit("bot_update", data)
    except Exception as e:
        logger.error(f"Error broadcasting update: {e}")


def _run_backtest_subprocess(job_id, symbol, start, end):
    """Run backtest_runner.py as a subprocess and record the result.

    Runs as a separate process (not a thread in this app) so that scoping
    config.py's symbol universes for the backtest never touches the live
    bot's in-memory config.
    """
    output_path = os.path.join(BACKTEST_DIR, f"{job_id}.json")
    backtest_jobs[job_id]["status"] = "running"
    try:
        proc = subprocess.run(
            [
                sys.executable, "backtest_runner.py",
                "--symbol", symbol,
                "--start", start,
                "--end", end,
                "--output", output_path,
            ],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute safety timeout
        )
        if proc.returncode != 0:
            backtest_jobs[job_id]["status"] = "failed"
            backtest_jobs[job_id]["error"] = proc.stderr[-2000:] or "Backtest process exited with an error"
            return

        with open(output_path, "r") as f:
            data = json.load(f)

        if data.get("success"):
            backtest_jobs[job_id]["status"] = "completed"
            backtest_jobs[job_id]["result"] = data
        else:
            backtest_jobs[job_id]["status"] = "failed"
            backtest_jobs[job_id]["error"] = data.get("error", "Unknown error")

    except subprocess.TimeoutExpired:
        backtest_jobs[job_id]["status"] = "failed"
        backtest_jobs[job_id]["error"] = "Backtest timed out after 10 minutes"
    except Exception as e:
        backtest_jobs[job_id]["status"] = "failed"
        backtest_jobs[job_id]["error"] = str(e)


# ============================================================================
# REST API ENDPOINTS
# ============================================================================

@app.route("/api/status", methods=["GET"])
def get_status():
    """Get current bot status and portfolio state."""
    try:
        # Buying power isn't exposed on the Strategy class directly in this
        # Lumibot version — pull it from the underlying Alpaca account,
        # falling back to cash if that call fails for any reason.
        try:
            buying_power = float(strategy_bot.broker.api.get_account().buying_power)
        except Exception as bp_error:
            logger.warning(f"Could not fetch buying power from broker, falling back to cash: {bp_error}")
            buying_power = float(strategy_bot.get_cash())

        return jsonify({
            "status": "trading" if strategy_bot.is_trading else "stopped",
            "portfolio_value": float(strategy_bot.get_portfolio_value()),
            "cash": float(strategy_bot.get_cash()),
            "buying_power": buying_power,
            "positions_count": len(strategy_bot.get_positions()),
            "open_orders_count": len(strategy_bot.get_orders()),
            "bracket_orders_count": len(strategy_bot.bracket_orders),
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions", methods=["GET"])
def get_positions():
    """Get all open positions."""
    try:
        positions = [format_position(p) for p in strategy_bot.get_positions()]
        return jsonify({"positions": positions})
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders", methods=["GET"])
def get_orders():
    """Get all orders (open and closed)."""
    try:
        orders = [format_order(o) for o in strategy_bot.get_orders()]
        return jsonify({"orders": orders})
    except Exception as e:
        logger.error(f"Error getting orders: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders", methods=["POST"])
def place_order():
    """Place a new order."""
    try:
        data = request.get_json()
        symbol = data.get("symbol")
        quantity = float(data.get("quantity", 1))
        side = data.get("side", "buy")

        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400

        # Create and submit order
        order = strategy_bot.create_order(symbol, int(quantity), side)
        strategy_bot.submit_order(order)

        return jsonify({
            "order_id": str(order.id),
            "status": "submitted",
            "symbol": symbol,
            "quantity": quantity,
            "side": side,
        }), 201

    except Exception as e:
        logger.error(f"Error placing order: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/start", methods=["POST"])
def start_bot():
    """Start the trading bot."""
    try:
        strategy_bot.start_trading()
        return jsonify({"status": "started"})
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    """Stop the trading bot and liquidate positions."""
    try:
        strategy_bot.stop_trading()
        return jsonify({"status": "stopping", "message": "Liquidating positions..."})
    except Exception as e:
        logger.error(f"Error stopping bot: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategies", methods=["GET"])
def get_strategies():
    """Get strategy performance metrics."""
    try:
        metrics = strategy_bot.get_performance_metrics()
        return jsonify(metrics)
    except Exception as e:
        logger.error(f"Error getting strategies: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bracket-orders", methods=["GET"])
def get_bracket_orders():
    """Get all bracket orders."""
    try:
        bracket_data = []
        for symbol, bracket in strategy_bot.bracket_orders.items():
            bracket_data.append({
                "symbol": bracket.symbol,
                "quantity": bracket.quantity,
                "entry_price": float(bracket.entry_price),
                "stop_loss_price": float(bracket.stop_loss_price),
                "take_profit_price": float(bracket.take_profit_price),
                "current_price": float(getattr(bracket, 'current_price', bracket.entry_price)),
                "entry_time": bracket.entry_time.isoformat(),
            })
        return jsonify({"bracket_orders": bracket_data})
    except Exception as e:
        logger.error(f"Error getting bracket orders: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest", methods=["POST"])
def start_backtest():
    """Kick off a backtest for a specific symbol in a background subprocess."""
    try:
        data = request.get_json()
        symbol = (data.get("symbol") or "").strip().upper()
        start = data.get("start")  # "YYYY-MM-DD"
        end = data.get("end")      # "YYYY-MM-DD"

        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400
        if not start or not end:
            return jsonify({"error": "Missing start or end date (YYYY-MM-DD)"}), 400

        job_id = str(uuid.uuid4())
        backtest_jobs[job_id] = {"status": "queued", "symbol": symbol, "start": start, "end": end}

        thread = threading.Thread(
            target=_run_backtest_subprocess,
            args=(job_id, symbol, start, end),
            daemon=True,
        )
        thread.start()

        return jsonify({"job_id": job_id, "status": "queued"}), 202

    except Exception as e:
        logger.error(f"Error starting backtest: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest/<job_id>", methods=["GET"])
def get_backtest_status(job_id):
    """Poll the status/result of a backtest job."""
    job = backtest_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/", methods=["GET"])
def index():
    """Serve the web dashboard."""
    return render_template("dashboard.html")


# ============================================================================
# WEBSOCKET EVENTS
# ============================================================================

@socketio.on("connect")
def handle_connect():
    """Handle client connection."""
    client_id = request.sid
    connected_clients.add(client_id)
    logger.info(f"Client connected: {client_id}")
    emit("connect_response", {"data": "Connected to trading bot"})


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    client_id = request.sid
    connected_clients.discard(client_id)
    logger.info(f"Client disconnected: {client_id}")


@socketio.on("request_update")
def handle_update_request():
    """Handle client request for status update."""
    broadcast_update()


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500


def init_bot(bot_instance: MultiStrategyBot):
    """Initialize the API with a strategy bot instance."""
    global strategy_bot
    strategy_bot = bot_instance
    logger.info("API initialized with strategy bot")
