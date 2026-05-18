"""
cmd_market.py — Market data + portfolio commands extracted from bot_commands.py.
"""

import threading

from telegram_api import send_message, typing_until_done, send_typing_action
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
            with typing_until_done(chat_id):
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
            send_typing_action(chat_id)   # fires while live prices load (8-12 yfinance calls)
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
            with typing_until_done(chat_id):
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
            with typing_until_done(chat_id):
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
            from telegram_api import send_inline_keyboard
            community_msg = (
                f"🌍 <b>StockPulz Community</b>\n\n"
                f"<b>Users tracked:</b>  {stats['total_users']}\n"
                f"<b>Closed trades:</b>  {stats['total_trades']}\n"
                f"<b>Win rate:</b>  {stats['win_rate']}%  "
                f"({stats['total_wins']}W / {stats['total_losses']}L)\n"
                f"<b>Avg return/trade:</b>  {'+' if stats['avg_return'] >= 0 else ''}{stats['avg_return']}%"
                f"{spy_str}{alpha_str}"
                f"{best_str}{worst_str}{streak_str}\n\n"
                f"<i>Based on actual closed trades by StockPulz users.</i>\n\n"
                f"📊 Curious how <b>you</b> compare? Tap below:"
            )
            send_inline_keyboard(
                community_msg,
                [[{"text": "📊 My Accuracy", "callback_data": "cmd|ACCURACY"},
                  {"text": "📋 My History",  "callback_data": "cmd|HISTORY"}]],
                chat_id=chat_id,
            )
            return None
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
            with typing_until_done(chat_id):
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

    # ── /swap [TICKER] ────────────────────────────────────────────────────────
    # Give user an alternative pick — replacing a specific ticker or next best.
    if text == "SWAP" or text.startswith("SWAP "):
        parts = text.split(maxsplit=1)
        skip_ticker = parts[1].upper() if len(parts) > 1 else None

        picks = load_picks()
        if not picks:
            return "📭 No picks for today yet. Check back after the morning run."

        # Build set of already-picked tickers
        picked = set()
        for cat in ("short_term", "long_term"):
            for p in picks.get("stocks", {}).get(cat, []):
                picked.add(p.get("ticker", "").upper())
            for p in picks.get("etfs", {}).get(cat, []):
                picked.add(p.get("ticker", "").upper())
            for p in picks.get("commodities", {}).get(cat, []):
                picked.add(p.get("ticker", "").upper())

        # Determine category of the ticker being swapped (default: short_term)
        category = "short_term"
        if skip_ticker:
            if any(p.get("ticker", "").upper() == skip_ticker
                   for p in picks.get("stocks", {}).get("long_term", [])):
                category = "long_term"
            if skip_ticker not in picked:
                return f"⚠️ <b>{skip_ticker}</b> isn't in today's picks — nothing to swap."
            picked.add(skip_ticker)

        # Load screener cache to find alternatives
        try:
            from config_manager import load_screener_cache
            cache = load_screener_cache()
        except Exception:
            cache = None

        if not cache:
            return (
                "⚠️ No screener data available for today.\n\n"
                "The midnight screener cache may have expired. Try again after the next morning run."
            )

        pool = cache.get("stocks", {}).get(category, [])
        alternatives = [c for c in pool if c.get("ticker", "").upper() not in picked]
        alternatives.sort(key=lambda c: c.get("score", 0), reverse=True)

        if not alternatives:
            other_cat = "long_term" if category == "short_term" else "short_term"
            alternatives = [c for c in cache.get("stocks", {}).get(other_cat, [])
                            if c.get("ticker", "").upper() not in picked]
            alternatives.sort(key=lambda c: c.get("score", 0), reverse=True)
            if alternatives:
                category = other_cat

        if not alternatives:
            return (
                "😕 No strong alternatives found in today's screener pool.\n\n"
                "All remaining candidates were below the conviction threshold."
            )

        send_typing_action(chat_id)
        send_message(
            f"🔄 Analysing <b>{alternatives[0].get('ticker','?').upper()}</b> as your alternative pick…",
            chat_id=chat_id,
        )

        try:
            from ai_analyzer import get_swap_pick
            ucfg = get_user_config(chat_id) or {}
            cfg  = {**get_config(), **ucfg}
            pick = get_swap_pick(alternatives[0], category=category, config=cfg)
        except Exception as exc:
            return f"⚠️ Couldn't generate alternative pick: {exc}"

        if not pick and len(alternatives) > 1:
            next_t = alternatives[1].get("ticker", "?").upper()
            send_message(f"🔄 First alternative didn't clear the bar — trying <b>{next_t}</b>…", chat_id=chat_id)
            try:
                pick = get_swap_pick(alternatives[1], category=category, config=cfg)
            except Exception:
                pick = None

        if not pick:
            return (
                "😕 Couldn't find a high-conviction alternative today.\n\n"
                "The remaining candidates didn't clear the conviction threshold. "
                "Consider revisiting /today's picks or waiting until tomorrow."
            )

        # Format the swap pick
        cat_label = "📈 Short-Term" if pick.get("_category", category) == "short_term" else "🏦 Long-Term"
        t   = pick.get("ticker", "?")
        co  = pick.get("company", "")
        ent = pick.get("entry_price", 0)
        tgt = pick.get("target_price", 0)
        stp = pick.get("stop_loss")
        con = pick.get("conviction", 3)
        stars = "★" * con + "☆" * (5 - con)
        upside   = f" (+{((tgt - ent) / ent * 100):.1f}%)" if ent and tgt and ent > 0 else ""
        stop_ln  = f"\n🛑 Stop: <code>${stp:.2f}</code>" if stp else ""
        thesis   = pick.get("thesis", "")
        catalyst = pick.get("catalyst", "")
        inv      = pick.get("invalidation", "")
        swap_note = f" instead of {skip_ticker}" if skip_ticker else ""

        msg = (
            f"🔄 <b>SWAP PICK</b> · {cat_label}\n\n"
            f"<b>{t}</b>  <i>{co}</i>\n"
            f"Entry <code>${ent:.2f}</code> → Target <code>${tgt:.2f}</code>{upside}{stop_ln}\n"
            f"Conviction: {stars}\n\n"
        )
        if thesis:    msg += f"📝 {thesis}\n"
        if catalyst:  msg += f"⚡ <i>{catalyst}</i>\n"
        if inv:       msg += f"❌ <i>Invalidated if: {inv}</i>\n"
        msg += f"\n<i>⚠️ Not financial advice. Next-best pick{swap_note} from today's screener pool.</i>"
        return msg

    # ── /accuracy — personal win rate breakdown ───────────────────────────────
    if text == "ACCURACY":
        try:
            from datetime import datetime, timedelta
            log     = load_user_trade_log(chat_id)
            closed  = log.get("closed", [])
            if not closed:
                return (
                    "📊 <b>Your Accuracy</b>\n\n"
                    "You haven't closed any trades yet.\n\n"
                    "📌 How to track:\n"
                    "  1️⃣ /bought AAPL 182 — log a buy\n"
                    "  2️⃣ /sold AAPL 197 — log the sale\n"
                    "StockPulz will calculate your win rate automatically.\n\n"
                    "<i>Tip: /history shows all your open &amp; closed trades.</i>"
                )

            now = datetime.utcnow()
            def _period_stats(trades, days=None):
                subset = trades
                if days:
                    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
                    subset = [t for t in trades if (t.get("closed_date") or "") >= cutoff]
                if not subset:
                    return None
                returns  = [float(t.get("return_pct", 0)) for t in subset]
                wins     = [r for r in returns if r > 0]
                losses   = [r for r in returns if r <= 0]
                win_rate = round(len(wins) / len(returns) * 100) if returns else 0
                avg_gain = round(sum(wins) / len(wins), 1) if wins else 0
                avg_loss = round(sum(losses) / len(losses), 1) if losses else 0
                best     = max(subset, key=lambda t: float(t.get("return_pct", 0)))
                worst    = min(subset, key=lambda t: float(t.get("return_pct", 0)))
                return {
                    "n": len(returns), "wins": len(wins), "losses": len(losses),
                    "win_rate": win_rate, "avg_gain": avg_gain, "avg_loss": avg_loss,
                    "best": best, "worst": worst,
                }

            s7  = _period_stats(closed, 7)
            s30 = _period_stats(closed, 30)
            sAll= _period_stats(closed)

            def _row(label, s):
                if not s:
                    return f"<b>{label}:</b>  No closed trades yet\n"
                wr_emoji = "✅" if s["win_rate"] >= 60 else "⚠️" if s["win_rate"] >= 50 else "❌"
                return (
                    f"<b>{label}:</b>  {s['wins']}W / {s['losses']}L  →  "
                    f"<b>{s['win_rate']}%</b> {wr_emoji}\n"
                )

            msg  = "📊 <b>Your Accuracy</b>\n\n"
            msg += _row("Last 7 days",  s7)
            msg += _row("Last 30 days", s30)
            msg += _row("All time",     sAll)

            if sAll:
                sign = lambda n: "+" if n >= 0 else ""
                msg += (
                    f"\n<b>Avg gain on wins:</b>   {sign(sAll['avg_gain'])}{sAll['avg_gain']}%\n"
                    f"<b>Avg loss on stops:</b>  {sign(sAll['avg_loss'])}{sAll['avg_loss']}%\n"
                )
                best_t  = sAll["best"]
                worst_t = sAll["worst"]
                b_ret   = float(best_t.get("return_pct", 0))
                w_ret   = float(worst_t.get("return_pct", 0))
                msg += (
                    f"\n🏆 Best pick:   <b>{best_t.get('ticker','?')}</b>  "
                    f"{sign(b_ret)}{round(b_ret,1)}%\n"
                    f"💔 Worst pick:  <b>{worst_t.get('ticker','?')}</b>  "
                    f"{round(w_ret,1)}%\n"
                )

            msg += "\n<i>Based on trades you logged with /bought &amp; /sold.</i>"
            msg += "\n📈 See full history: /history"
            return msg
        except Exception as exc:
            return f"⚠️ Could not load accuracy: {exc}"

    # ── /define — plain-English glossary via Haiku ────────────────────────────
    if text == "DEFINE":
        return (
            "📖 <b>What term would you like explained?</b>\n\n"
            "Examples:\n"
            "  <code>/define RSI</code>\n"
            "  <code>/define MACD</code>\n"
            "  <code>/define stop loss</code>\n"
            "  <code>/define options</code>\n"
            "  <code>/define market cap</code>\n\n"
            "<i>I'll explain it in plain English — no jargon.</i>"
        )

    if text.startswith("DEFINE "):
        raw   = original.lstrip("/")
        term  = raw.split(" ", 1)[1].strip() if " " in raw else raw
        if not term:
            return "⚠️ Please provide a term, e.g. /define RSI"
        send_message(f"📖 <i>Looking up <b>{term}</b>…</i>", chat_id=chat_id)
        def _define_async():
            try:
                import anthropic as _ant
                import os as _os
                client = _ant.Anthropic(api_key=_os.environ["ANTHROPIC_API_KEY"])
                resp   = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    system=(
                        "You are a friendly financial educator helping everyday investors. "
                        "Explain the term in 2-3 short sentences of plain English — "
                        "zero jargon, zero disclaimers. "
                        "Format: first sentence = one-line definition. "
                        "Second sentence = why it matters for trading. "
                        "Third sentence (optional) = a simple real-world example with numbers. "
                        "End with: 'StockPulz uses this to: ...' (one short line)."
                    ),
                    messages=[{"role": "user", "content": f"Explain this financial term: {term}"}],
                )
                explanation = resp.content[0].text.strip()
                send_message(
                    f"📖 <b>{term.upper()}</b>\n\n{explanation}\n\n"
                    f"<i>💡 Try another: /define options · /define RSI · /define MACD</i>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_message(f"⚠️ Couldn't look that up right now: {exc}", chat_id=chat_id)
        threading.Thread(target=_define_async, daemon=True).start()
        return None

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

    # ── /size ─────────────────────────────────────────────────────────────────
    if text == "SIZE":
        return (
            "💰 <b>Position Sizing</b>\n\n"
            "Usage: <code>/size TICKER</code>  — e.g. <code>/size NVDA</code>\n\n"
            "I'll calculate how many shares to buy based on your budget and risk profile."
        )

    if text.startswith("SIZE "):
        ticker = text[5:].strip().upper()
        if not ticker:
            return "Usage: <code>/size TICKER</code>"

        cfg         = {**get_config(), **get_user_config(chat_id)}
        stock_budget = float(cfg.get("stock_budget") or cfg.get("budget_per_trade") or 500)
        crypto_budget= float(cfg.get("crypto_budget") or 100)
        stop_pct     = float(cfg.get("stop_loss_pct") or 7)
        risk_profile = cfg.get("risk_profile", "moderate")

        # Detect if crypto
        is_crypto = len(ticker) <= 5 and ticker.isalpha() and ticker in (
            "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","LINK",
            "UNI","ATOM","LTC","BCH","ALGO","XLM","ICP","FIL","HYPE","SUI","ARB","OP"
        )
        budget = crypto_budget if is_crypto else stock_budget

        send_typing_action(chat_id)
        price = _fetch_live_price(ticker)
        if not price or price <= 0:
            return f"⚠️ Couldn't fetch a live price for <b>{ticker}</b>. Try again shortly."

        # Risk-based position size: risk $ = budget * stop%
        # Shares = risk_amount / (price * stop_pct/100)
        risk_multiplier = {"conservative": 0.5, "moderate": 1.0, "aggressive": 1.5}.get(risk_profile, 1.0)
        max_risk_amt    = budget * (stop_pct / 100) * risk_multiplier
        shares          = max_risk_amt / (price * stop_pct / 100)
        shares          = max(1, round(shares))
        total_cost      = round(shares * price, 2)
        stop_price      = round(price * (1 - stop_pct / 100), 2)
        target_price    = round(price * (1 + (stop_pct * 2) / 100), 2)   # 2:1 reward/risk

        # Cap to budget
        if total_cost > budget:
            shares     = max(1, int(budget / price))
            total_cost = round(shares * price, 2)

        risk_amt   = round(shares * price * stop_pct / 100, 2)
        reward_amt = round(shares * (target_price - price), 2)

        return (
            f"💰 <b>Position Size — {ticker}</b>\n\n"
            f"Live price:    <code>${price:,.2f}</code>\n"
            f"Suggested:     <b>{shares} share{'s' if shares != 1 else ''}</b>  (${total_cost:,.2f})\n\n"
            f"🛑 Stop loss:  <code>${stop_price:,.2f}</code>  (−{stop_pct}%)\n"
            f"🎯 Target:     <code>${target_price:,.2f}</code>  (+{stop_pct*2}%)\n\n"
            f"Max risk:      <b>${risk_amt:,.2f}</b>  ·  Reward: <b>${reward_amt:,.2f}</b>\n"
            f"Risk/Reward:   1 : 2  ({risk_profile} profile)\n\n"
            f"<i>Based on your ${budget:,.0f} budget &amp; {stop_pct}% stop. "
            f"Adjust with /set_budget &amp; /set_thresholds.</i>"
        )

    return None
