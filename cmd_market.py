"""
cmd_market.py — Market data + portfolio commands extracted from bot_commands.py.
"""

import threading

from telegram_api import send_message
from config_manager import get_config, get_user_config, load_picks, load_user_trade_log, get_allowed_users
from formatters import format_daily_message, format_confirmation_message
from cmd_helpers import _fetch_live_price, _resolve_ticker_candidates
from cmd_trade_exec import _send_chart
from cmd_settings import _prompt_for_param
from cmd_nlp import _explain_pick


def _cmd_market(text: str, original: str, chat_id: str) -> "str | None":
    """Market data & portfolio commands."""
    if text == "TODAY":
        picks = load_picks()
        if not picks:
            return "📭 No picks for today yet. Check back after 8 AM ET."
        config = {**get_config(), **get_user_config(chat_id)}
        # Re-generate personal notes on demand (same Haiku call as morning send)
        personal_notes: dict = {}
        try:
            from ai_analyzer import personalize_picks
            log            = load_user_trade_log(chat_id)
            open_positions = log.get("open", [])
            risk_profile   = config.get("risk_profile", "moderate")
            personal_notes = personalize_picks(picks, open_positions, risk_profile)
        except Exception as pn_exc:
            print(f"[telegram] /today personal notes failed (non-critical): {pn_exc}")
        buy_counts: dict = {}
        if config.get("show_buy_counts"):
            try:
                from config_manager import load_buy_counts
                buy_counts = load_buy_counts()
            except Exception:
                pass
        return format_daily_message(picks, config, personal_notes=personal_notes,
                                    buy_counts=buy_counts)

    if text == "EXPLAIN":
        _prompt_for_param("explain", chat_id)
        return ""

    if text.startswith("EXPLAIN "):
        # Preserve original casing for the query — strip the command prefix
        raw   = original.lstrip("/")
        query = raw.split(" ", 1)[1].strip() if " " in raw else raw
        # Fire in background — Haiku can take 2-4s; returning "" lets webhook
        # respond instantly (200 OK) while the answer arrives a moment later.
        send_message("💬 <i>Thinking…</i>", chat_id=chat_id)
        def _explain_async():
            reply = _explain_pick(query)
            send_message(reply, chat_id=chat_id)
        threading.Thread(target=_explain_async, daemon=True).start()
        return None   # already sent "Thinking…", don't send a second message

    if text == "CHART":
        _prompt_for_param("chart", chat_id)
        return ""

    if text.startswith("CHART "):
        raw_chart = text.split(" ", 1)[1].strip()
        if not raw_chart:
            return "⚠️ Please provide a ticker, e.g. /chart AAPL"
        # Resolve full phrase so "chart avery dennison" → AVY, not "AVERY DENNISON"
        _chart_cands = _resolve_ticker_candidates(raw_chart)
        ticker = _chart_cands[0]["ticker"].upper() if _chart_cands else raw_chart.upper()
        from chart_generator import is_crypto
        asset_type = "crypto" if is_crypto(ticker) else "stock"
        send_message("📊 <i>Generating chart…</i>", chat_id=chat_id)
        threading.Thread(
            target=_send_chart, args=(ticker, asset_type, chat_id), daemon=True
        ).start()
        return None   # already sent "Generating…"

    if text == "PRICES":
        picks = load_picks()
        if not picks:
            return "📭 No picks found for today yet. Check back after 8 AM ET."
        try:
            from price_checker import get_current_prices
            current_prices = get_current_prices(picks)
            buy_counts: dict = {}
            if get_config().get("show_buy_counts"):
                try:
                    from config_manager import load_buy_counts
                    buy_counts = load_buy_counts()
                except Exception:
                    pass
            return format_confirmation_message(picks, current_prices, buy_counts=buy_counts)
        except Exception as exc:
            return f"⚠️ Could not fetch prices: {exc}"

    if text == "STATS":
        try:
            from trade_logger import get_performance_stats
            from performance_tracker import get_recent_stats
            stats = get_performance_stats(chat_id)
            if not stats or stats["count"] == 0:
                # Check if they have any open trades at all
                log        = load_user_trade_log(chat_id)
                open_count = len(log.get("open", []))
                if open_count > 0:
                    tickers_str = ", ".join(
                        f"<b>{t['ticker']}</b>" for t in log["open"][:3]
                    )
                    return (
                        "📊 <b>Your Stats</b>\n\n"
                        f"You have {open_count} open position{'s' if open_count > 1 else ''} "
                        f"({tickers_str}) but no closed trades yet.\n\n"
                        "When you're ready to exit, send <code>/sold TICKER</code> — "
                        "that's when the P&amp;L and win rate get recorded.\n\n"
                        "<i>Stats appear here after your first closed trade.</i>"
                    )
                return (
                    "📊 <b>Your Stats</b>\n\n"
                    "Nothing here yet — your win rate, expectancy, and P&amp;L build up as you trade.\n\n"
                    "<b>How to start:</b>\n"
                    "1. Check today's picks — /today\n"
                    "2. Place a trade and log it — <code>/bought TICKER</code>\n"
                    "3. Exit when ready — <code>/sold TICKER</code>\n\n"
                    "After your first closed trade, this page fills in automatically."
                )
            _s = lambda x: ("+" if x >= 0 else "") + str(x)
            streak_str = (
                f"\n🔥 Current streak: <b>{stats['streak']} wins</b>" if stats["streak"] >= 2
                else ("\n❄️ <i>Last trade was a loss</i>" if stats["losses"] > 0 and stats["streak"] == 0 else "")
            )
            outcome_str = ""
            if stats["targets_hit"] or stats["stops_hit"]:
                outcome_str = (
                    f"\n🎯 Targets hit: <b>{stats['targets_hit']}</b>  "
                    f"🛑 Stops hit: <b>{stats['stops_hit']}</b>"
                )
            msg = (
                f"📊 <b>Your Performance</b>\n\n"
                f"<b>Closed trades:</b>  {stats['count']}\n"
                f"<b>Win rate:</b>  {stats['win_rate']}%  ({stats['wins']}W / {stats['losses']}L)\n"
                f"<b>Avg gain on winners:</b>  <b>+{stats['avg_gain']}%</b>\n"
                f"<b>Avg loss on losers:</b>  {stats['avg_loss']}%\n"
                f"<b>Expectancy:</b>  <b>{_s(stats['expectancy'])}% per trade</b>\n"
                f"<b>Avg return:</b>  {_s(stats['avg_return'])}%"
                f"{outcome_str}{streak_str}"
            )
            if stats.get("best"):
                bt, br = stats["best"]
                wt, wr = stats["worst"]
                msg += f"\n\n🏆 Best:  <b>{bt}</b>  {_s(round(br,1))}%"
                msg += f"\n💔 Worst: <b>{wt}</b>  {_s(round(wr,1))}%"
            if stats["total_gain_usd"]:
                msg += f"\n\n💵 Total P&L: <b>${stats['total_gain_usd']:+.2f}</b>"

            # ── Community 30-day bar ───────────────────────────────────────────
            try:
                users = get_allowed_users()
                logs  = [load_user_trade_log(u) for u in users]
                rs    = get_recent_stats(logs, days=30)
                if rs and rs.get("total"):
                    t   = rs["total"]
                    spy = rs.get("spy_return")
                    spy_str = f"  ·  SPY {_s(spy)}%" if spy is not None else ""
                    msg += (
                        f"\n\n<b>📈 Community — last 30d</b>\n"
                        f"{t['wins']}W/{t['losses']}L ({t['win_rate']}%)  ·  "
                        f"avg {_s(t['avg_return'])}%  ·  "
                        f"exp {_s(t['expectancy'])}%/trade{spy_str}"
                    )
            except Exception:
                pass

            msg += "\n\n<i>⚠️ Past performance doesn't guarantee future results.</i>  📋 /help"
            return msg
        except Exception as exc:
            return f"⚠️ Could not load stats: {exc}"

    if text == "COMMUNITY":
        try:
            from performance_tracker import build_community_stats
            users = get_allowed_users()
            logs  = []
            for uid in users:
                try:
                    logs.append(load_user_trade_log(uid))
                except Exception:
                    pass
            stats = build_community_stats(logs)
            if not stats or stats["total_trades"] == 0:
                return (
                    "📭 <b>StockPulz Community</b>\n\n"
                    "Not enough closed trades yet to show community stats.\n"
                    "Close your first trade via /sold to see results here."
                )
            alpha_str = ""
            if stats.get("alpha") is not None:
                sign = "+" if stats["alpha"] >= 0 else ""
                alpha_str = f"\n<b>Alpha vs S&P:</b>  <b>{sign}{stats['alpha']}%</b>"
            spy_str = ""
            if stats.get("spy_return_30d") is not None:
                s = stats["spy_return_30d"]
                spy_str = f"\n<b>S&P 500 (30d):</b>  {'+' if s >= 0 else ''}{s}%"
            best_str = worst_str = ""
            if stats.get("best_pick"):
                b, br = stats["best_pick"]
                best_str = f"\n🏆 Best pick:   <b>{b}</b> {'+' if br >= 0 else ''}{br}%"
            if stats.get("worst_pick"):
                w, wr = stats["worst_pick"]
                worst_str = f"\n💔 Worst pick:  <b>{w}</b> {'+' if wr >= 0 else ''}{wr}%"
            streak_str = ""
            if stats.get("hot_streak_users", 0) > 0:
                streak_str = f"\n🔥 {stats['hot_streak_users']} user(s) on a 3+ win streak!"
            return (
                f"🌍 <b>StockPulz Community</b>\n\n"
                f"<b>Users tracked:</b>  {stats['total_users']}\n"
                f"<b>Closed trades:</b>  {stats['total_trades']}\n"
                f"<b>Win rate:</b>  {stats['win_rate']}%  "
                f"({stats['total_wins']}W / {stats['total_losses']}L)\n"
                f"<b>Avg return/trade:</b>  {'+' if stats['avg_return'] >= 0 else ''}{stats['avg_return']}%"
                f"{spy_str}{alpha_str}"
                f"{best_str}{worst_str}{streak_str}\n\n"
                f"<i>Based on actual closed trades by StockPulz users.</i>"
            )
        except Exception as exc:
            return f"⚠️ Could not load community stats: {exc}"

    # ── Bot performance track record ─────────────────────────────────────────
    if text == "PERFORMANCE":
        try:
            from performance_context import get_performance_context
            from performance_tracker import build_community_stats
            users = get_allowed_users()
            logs  = [load_user_trade_log(uid) for uid in users]
            stats = build_community_stats(logs)

            if not stats or stats.get("total_trades", 0) < 3:
                return (
                    "📊 <b>StockPulz Track Record</b>\n\n"
                    "Not enough closed trades yet to show performance stats.\n"
                    "<i>Log your first trade with /bought, then /sold to start tracking.</i>"
                )

            wr      = stats["win_rate"]
            avg_ret = stats["avg_return"]
            trades  = stats["total_trades"]
            wins    = stats["total_wins"]
            losses  = stats["total_losses"]

            # SPY comparison
            spy_str = ""
            if stats.get("spy_return_30d") is not None:
                s = stats["spy_return_30d"]
                spy_str = f"\n📈 <b>S&P 500 (30d):</b>  {'+' if s >= 0 else ''}{s:.1f}%"
            alpha_str = ""
            if stats.get("alpha") is not None:
                sign = "+" if stats["alpha"] >= 0 else ""
                alpha_str = f"\n⚡ <b>Alpha vs S&P:</b>  <b>{sign}{stats['alpha']:.1f}%</b>"

            # Best / worst
            best_str = worst_str = ""
            if stats.get("best_pick"):
                b, br = stats["best_pick"]
                best_str = f"\n🏆 Best:  <b>{b}</b>  {'+' if br >= 0 else ''}{br:.1f}%"
            if stats.get("worst_pick"):
                w, wr2 = stats["worst_pick"]
                worst_str = f"\n💔 Worst: <b>{w}</b>  {'+' if wr2 >= 0 else ''}{wr2:.1f}%"

            # 30/60/90 day context from performance_context
            ctx30  = get_performance_context(lookback_days=30)
            ctx90  = get_performance_context(lookback_days=90)
            wr_line = f"✅ Win rate looks strong" if wr >= 60 else ("⚠️ Win rate below 60% — learning phase" if wr < 50 else "")

            msg = (
                f"📊 <b>StockPulz Track Record</b>\n\n"
                f"<b>Closed trades:</b>  {trades}  ({wins}W / {losses}L)\n"
                f"<b>Win rate:</b>  <b>{wr:.0f}%</b>\n"
                f"<b>Avg return/trade:</b>  {'+' if avg_ret >= 0 else ''}{avg_ret:.1f}%"
                f"{spy_str}{alpha_str}"
                f"{best_str}{worst_str}\n"
            )
            if wr_line:
                msg += f"\n<i>{wr_line}</i>\n"
            msg += "\n<i>Based on trades logged by StockPulz users. Past performance does not guarantee future results.</i>"
            return msg
        except Exception as exc:
            return f"⚠️ Could not load performance data: {exc}"

    # ── Market regime ─────────────────────────────────────────────────────────
    if text == "REGIME":
        try:
            from market_regime import get_market_regime, regime_emoji
            r = get_market_regime()
            emoji = regime_emoji(r["regime"])
            return (
                f"{emoji} <b>MARKET REGIME: {r['regime'].upper()}</b>\n\n"
                f"<b>VIX:</b> {r['vix'] or 'N/A'}\n"
                f"<b>SPY vs 50-day MA:</b> {'Above ✅' if r['spy_above_50ma'] else 'Below ⚠️' if r['spy_above_50ma'] is not None else 'N/A'}\n"
                f"<b>SPY vs 200-day MA:</b> {'Above ✅' if r['spy_above_200ma'] else 'Below 🔴' if r['spy_above_200ma'] is not None else 'N/A'}\n\n"
                f"<i>{r['note']}</i>"
            )
        except Exception as exc:
            return f"⚠️ Could not fetch market regime: {exc}"

    # ── /dividends ────────────────────────────────────────────────────────────
    if text == "DIVIDENDS":
        log     = load_user_trade_log(chat_id)
        open_t  = log.get("open", [])
        tickers = [t["ticker"] for t in open_t if t.get("asset_type", "stock") == "stock"]
        if not tickers:
            return "📭 You have no open stock positions to check dividends for.\n\nLog a position with /bought first."
        send_message("💰 <i>Fetching dividend info…</i>", chat_id=chat_id)
        try:
            from dividends_checker import get_dividend_info, format_dividends_message
            info = get_dividend_info(tickers)
            return format_dividends_message(info)
        except Exception as exc:
            return f"⚠️ Could not fetch dividend data: {exc}"

    return None
