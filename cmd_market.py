"""
cmd_market.py — Market data + portfolio commands extracted from bot_commands.py.
"""
from __future__ import annotations

import threading

import os

from telegram_api import send_message, typing_until_done, send_typing_action, send_inline_keyboard
from config_manager import get_config, get_user_config, load_picks, load_user_trade_log, get_allowed_users
from formatters import format_daily_message, format_confirmation_message, _p
from cmd_helpers import _fetch_live_price, _resolve_ticker_candidates
from cmd_trade_exec import _send_chart
from cmd_settings import _prompt_for_param
from cmd_nlp import _explain_pick


def _picks_tickers(picks: dict) -> list[str]:
    """Return all unique tickers/symbols from a picks dict."""
    tickers = []
    stocks = picks.get("stocks", picks)
    for p in stocks.get("short_term", []) + stocks.get("long_term", []):
        t = p.get("ticker", "")
        if t: tickers.append(t)
    for p in picks.get("crypto", {}).get("short_term", []) + picks.get("crypto", {}).get("long_term", []):
        t = p.get("symbol", "") or p.get("ticker", "")
        if t: tickers.append(t)
    for section in (picks.get("etfs", {}), picks.get("commodities", {})):
        for p in section.get("short_term", []) + section.get("long_term", []):
            t = p.get("ticker", "") or p.get("symbol", "")
            if t: tickers.append(t)
    return list(dict.fromkeys(t.upper() for t in tickers if t))   # deduplicated, order-preserved


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
        company_names: dict = {}
        try:
            from market_data import get_company_names as _gcn
            company_names = _gcn(_picks_tickers(picks))
        except Exception:
            pass
        return format_daily_message(picks, config, personal_notes=personal_notes,
                                    buy_counts=buy_counts, company_names=company_names)

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
            company_names: dict = {}
            try:
                from market_data import get_company_names as _gcn
                company_names = _gcn(_picks_tickers(picks))
            except Exception:
                pass
            return format_confirmation_message(picks, current_prices, buy_counts=buy_counts,
                                               company_names=company_names)
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

            # ── SPY benchmark comparison ──────────────────────────────────────
            try:
                import yfinance as yf
                from datetime import date as _date
                log_spy = load_user_trade_log(chat_id)
                all_trades_spy = log_spy.get("closed", []) + log_spy.get("open", [])
                if all_trades_spy:
                    date_strs = []
                    for _t in all_trades_spy:
                        _ds = _t.get("opened_date") or _t.get("opened_at", "")
                        if _ds:
                            date_strs.append(str(_ds)[:10])
                    if date_strs:
                        first_trade_date = min(date_strs)
                        spy_data = yf.download("SPY", start=first_trade_date, progress=False)["Close"]
                        if not spy_data.empty and len(spy_data) >= 2:
                            spy_return = float((spy_data.iloc[-1] / spy_data.iloc[0] - 1) * 100)
                            # User total return: sum pnl_usd / sum invested
                            closed_spy = log_spy.get("closed", [])
                            total_pnl_usd = sum(float(t.get("gain_usd") or 0) for t in closed_spy)
                            total_invested = 0.0
                            for t in closed_spy:
                                ep = t.get("entry_price")
                                sh = t.get("shares") or t.get("quantity")
                                alloc = t.get("allocation") or t.get("budget")
                                if ep and sh:
                                    total_invested += float(ep) * float(sh)
                                elif alloc:
                                    total_invested += float(alloc)
                            user_return = (total_pnl_usd / total_invested * 100) if total_invested > 0 else 0.0
                            if user_return > spy_return + 2:
                                verdict = "🏆 You're beating the market — the picks are working."
                            elif user_return > spy_return - 2:
                                verdict = "⚖️ Roughly in line with the market."
                            else:
                                verdict = "📉 The market has outperformed your picks so far. Stay patient or review your strategy with /backtest."
                            msg += (
                                f"\n\n<b>📊 vs S&amp;P 500</b>\n"
                                f"Since your first trade ({first_trade_date}):\n"
                                f"Your picks: {user_return:+.1f}%\n"
                                f"SPY (S&amp;P 500): {spy_return:+.1f}%\n"
                                f"{verdict}"
                            )
            except Exception:
                pass

            msg += "\n\n<i>⚠️ Past performance doesn't guarantee future results.</i>  📋 /help"
            return msg
        except Exception as exc:
            return f"⚠️ Could not load stats: {exc}"

    if text == "STREAK":
        try:
            from trade_logger import get_performance_stats
            stats = get_performance_stats(chat_id)
            if not stats or stats["count"] == 0:
                return (
                    "🔥 <b>Your Streak</b>\n\n"
                    "No closed trades yet — streaks build up as you trade.\n\n"
                    "Log your first exit with <code>/sold TICKER</code> to start tracking."
                )

            log    = load_user_trade_log(chat_id)
            closed = sorted(log.get("closed", []),
                            key=lambda t: t.get("closed_date", ""))
            results = [float(t["return_pct"]) for t in closed]

            # Current streak (from most recent trade backwards)
            current_streak     = 0
            current_direction  = None   # "win" | "loss"
            for r in reversed(results):
                direction = "win" if r > 0 else "loss"
                if current_direction is None:
                    current_direction = direction
                if direction == current_direction:
                    current_streak += 1
                else:
                    break

            # Best win streak (all-time)
            best_win_streak = 0
            run = 0
            for r in results:
                if r > 0:
                    run += 1
                    best_win_streak = max(best_win_streak, run)
                else:
                    run = 0

            # Worst loss streak (all-time)
            worst_loss_streak = 0
            run = 0
            for r in results:
                if r <= 0:
                    run += 1
                    worst_loss_streak = max(worst_loss_streak, run)
                else:
                    run = 0

            # Build message
            if current_direction == "win" and current_streak >= 2:
                streak_line = f"🔥 <b>Active win streak:  {current_streak}</b>"
            elif current_direction == "win" and current_streak == 1:
                streak_line = "✅ Last trade was a <b>win</b> — streak of 1."
            elif current_direction == "loss" and current_streak >= 2:
                streak_line = f"❄️ <b>Active losing streak:  {current_streak}</b>"
            else:
                streak_line = "📉 Last trade was a <b>loss</b>."

            msg = (
                f"🔥 <b>Your Streaks</b>\n\n"
                f"{streak_line}\n\n"
                f"🏆 Best win streak ever:     <b>{best_win_streak}</b>\n"
                f"💔 Worst losing streak ever: <b>{worst_loss_streak}</b>\n\n"
                f"<i>Based on {stats['count']} closed trade{'s' if stats['count'] != 1 else ''}. "
                f"Overall win rate: {stats['win_rate']}%.</i>"
            )
            return msg
        except Exception as exc:
            return f"⚠️ Could not compute streaks: {exc}"

    if text.startswith("COMPARE"):
        parts = text.split()
        if len(parts) < 3:
            return (
                "📊 Usage: <code>/compare TICKER1 TICKER2</code>\n"
                "Example: <code>/compare AAPL MSFT</code>"
            )
        t1, t2 = parts[1].upper(), parts[2].upper()
        try:
            import yfinance as yf
            with typing_until_done(chat_id):
                info1 = yf.Ticker(t1).info
                info2 = yf.Ticker(t2).info

            def _fv(v, fmt=None):
                if v is None:
                    return "N/A"
                if fmt == "pct":
                    return f"{v:+.1f}%"
                if fmt == "money":
                    return f"${v:,.2f}"
                if fmt == "big":
                    if v >= 1e12: return f"${v/1e12:.1f}T"
                    if v >= 1e9:  return f"${v/1e9:.1f}B"
                    if v >= 1e6:  return f"${v/1e6:.1f}M"
                    return f"${v:,.0f}"
                if isinstance(v, float):
                    return f"{v:.1f}"
                return str(v)

            def _row(label, v1, v2, fmt=None, higher_is_better=True):
                """Return a Telegram HTML row with ◀ ▶ winner marker."""
                s1, s2 = _fv(v1, fmt), _fv(v2, fmt)
                winner = ""
                try:
                    if v1 is not None and v2 is not None:
                        is1 = float(v1) > float(v2) if higher_is_better else float(v1) < float(v2)
                        winner = f" ◀" if is1 else f" ▶"
                        if is1:
                            s1 = f"<b>{s1}</b>"
                        else:
                            s2 = f"<b>{s2}</b>"
                except Exception:
                    pass
                return f"<code>{label:<18}</code>  {s1}  /  {s2}{winner}"

            # Extract metrics
            p1   = info1.get("currentPrice") or info1.get("regularMarketPrice")
            p2   = info2.get("currentPrice") or info2.get("regularMarketPrice")
            hi1  = info1.get("fiftyTwoWeekHigh");  hi2 = info2.get("fiftyTwoWeekHigh")
            lo1  = info1.get("fiftyTwoWeekLow");   lo2 = info2.get("fiftyTwoWeekLow")
            gap1 = round((p1-hi1)/hi1*100,1) if (p1 and hi1) else None
            gap2 = round((p2-hi2)/hi2*100,1) if (p2 and hi2) else None
            pe1  = info1.get("trailingPE");   pe2  = info2.get("trailingPE")
            fwd1 = info1.get("forwardPE");    fwd2 = info2.get("forwardPE")
            mc1  = info1.get("marketCap");    mc2  = info2.get("marketCap")
            m1w1 = info1.get("fiftyTwoWeekChangePercent")
            m1w2 = info2.get("fiftyTwoWeekChangePercent")
            if m1w1: m1w1 = round(m1w1*100,1)
            if m1w2: m1w2 = round(m1w2*100,1)
            div1 = info1.get("dividendYield"); div2 = info2.get("dividendYield")
            if div1: div1 = round(div1*100,2)
            if div2: div2 = round(div2*100,2)
            tgt1 = info1.get("targetMeanPrice"); tgt2 = info2.get("targetMeanPrice")
            ups1 = round((tgt1-p1)/p1*100,1) if (tgt1 and p1) else None
            ups2 = round((tgt2-p2)/p2*100,1) if (tgt2 and p2) else None
            ar1  = info1.get("recommendationKey","").replace("_"," ").title()
            ar2  = info2.get("recommendationKey","").replace("_"," ").title()

            lines = [
                f"📊 <b>{t1} vs {t2}</b>",
                f"<i>◀ = {t1} wins that metric  ▶ = {t2} wins</i>",
                "",
                _row("Price",          p1,   p2,   fmt="money"),
                _row("Market Cap",     mc1,  mc2,  fmt="big"),
                _row("1yr Return",     m1w1, m1w2, fmt="pct"),
                _row("vs 52w High",    gap1, gap2, fmt="pct",  higher_is_better=False),
                _row("P/E trailing",   pe1,  pe2,  higher_is_better=False),
                _row("P/E forward",    fwd1, fwd2, higher_is_better=False),
                _row("Analyst Upside", ups1, ups2, fmt="pct"),
                _row("Dividend Yield", div1, div2, fmt="pct"),
                "",
                f"<code>{'Analyst':18}</code>  {ar1 or 'N/A'}  /  {ar2 or 'N/A'}",
                "",
                "<i>Data: Yahoo Finance.</i>",
            ]
            return "\n".join(lines)
        except Exception as exc:
            return f"⚠️ Compare failed: {exc}"

    if text == "RISK":
        try:
            log         = load_user_trade_log(chat_id)
            open_trades = log.get("open", [])
            if not open_trades:
                return (
                    "🛡 <b>Risk Report</b>\n\n"
                    "You have no open positions. Nothing at risk right now."
                )

            total_risk_usd   = 0.0
            total_deployed   = 0.0
            unprotected      = []
            rows             = []
            _s = lambda x: ("+" if x >= 0 else "") + str(x)

            for t in open_trades:
                ticker = t.get("ticker", "?")
                entry  = t.get("entry_price")
                stop   = t.get("stop_loss")
                shares = t.get("shares")
                alloc  = t.get("allocation") or (float(entry) * float(shares) if entry and shares else None)

                if alloc:
                    total_deployed += float(alloc)

                if entry and stop and shares:
                    risk_per_share = float(entry) - float(stop)
                    risk_usd       = risk_per_share * float(shares)
                    risk_pct_pos   = risk_per_share / float(entry) * 100
                    if risk_usd > 0:
                        total_risk_usd += risk_usd
                        rows.append(
                            f"  <b>{ticker}</b>  risk <code>${risk_usd:,.0f}</code>  "
                            f"<i>({risk_pct_pos:.1f}% of position)</i>"
                        )
                    elif risk_usd < 0:
                        # Stop is above entry — locked in a gain
                        locked = abs(risk_usd)
                        rows.append(
                            f"  <b>{ticker}</b>  🔒 <i>stop above entry — "
                            f"${locked:,.0f} locked in</i>"
                        )
                elif not stop:
                    unprotected.append(ticker)

            # Build message
            lines = ["🛡 <b>Position Risk Report</b>\n"]

            if rows:
                lines.append("\n".join(rows))

            if unprotected:
                lines.append(
                    f"\n⚠️ <b>No stop set:</b> {', '.join(f'<b>{t}</b>' for t in unprotected)}"
                    f"\n<i>These positions have unlimited downside until a stop is set.</i>"
                )

            if total_risk_usd > 0:
                pct_of_deployed = (total_risk_usd / total_deployed * 100) if total_deployed else 0
                risk_label = (
                    "🟢 well within normal range"    if pct_of_deployed <= 5  else
                    "🟡 moderate — watch closely"     if pct_of_deployed <= 10 else
                    "🔴 high — consider tightening stops"
                )
                lines.append(
                    f"\n💵 <b>Total $ at risk:</b>  <code>${total_risk_usd:,.0f}</code>"
                )
                if total_deployed > 0:
                    lines.append(
                        f"📊 <b>% of deployed capital:</b>  "
                        f"<b>{pct_of_deployed:.1f}%</b>  {risk_label}"
                    )
            elif not unprotected:
                lines.append("\n✅ All stops are above entry — all gains are protected.")

            lines.append(
                "\n<i>Risk = (entry − stop) × shares per position. "
                "Use <code>/updatestop TICKER PRICE</code> to adjust.</i>"
            )
            return "\n".join(lines)
        except Exception as exc:
            return f"⚠️ Could not compute risk: {exc}"

    if text == "TAX":
        try:
            from datetime import date as _date
            log    = load_user_trade_log(chat_id)
            closed = log.get("closed", [])
            year   = _date.today().year

            # Filter to current year
            ytd = []
            for t in closed:
                ds = t.get("closed_date") or t.get("closed_at", "")
                try:
                    if str(ds)[:4] == str(year):
                        ytd.append(t)
                except Exception:
                    pass

            if not ytd:
                return (
                    f"📋 <b>Tax Summary {year}</b>\n\n"
                    "No closed trades yet this year.\n\n"
                    "<i>P&amp;L from closed trades will appear here. "
                    "Use <code>/sold TICKER PRICE</code> to record exits.</i>"
                )

            st_gains = st_losses = lt_gains = lt_losses = 0.0
            st_trades = lt_trades = []
            st_trades, lt_trades = [], []

            for t in ytd:
                gain = float(t.get("gain_usd") or 0)
                # Determine holding period
                opened = t.get("opened_date") or t.get("opened_at", "")
                closed_d = t.get("closed_date") or t.get("closed_at", "")
                long_term = False
                try:
                    days = (_date.fromisoformat(str(closed_d)[:10]) -
                            _date.fromisoformat(str(opened)[:10])).days
                    long_term = days >= 365
                except Exception:
                    pass

                if long_term:
                    lt_trades.append(t)
                    if gain >= 0: lt_gains  += gain
                    else:         lt_losses += abs(gain)
                else:
                    st_trades.append(t)
                    if gain >= 0: st_gains  += gain
                    else:         st_losses += abs(gain)

            st_net = st_gains - st_losses
            lt_net = lt_gains - lt_losses
            total_net = st_net + lt_net

            # Rough federal tax estimates (2024 brackets, single filer, gains only)
            # ST taxed as ordinary income — use 22% as mid-bracket estimate
            # LT: 0% ≤$47k, 15% ≤$518k, 20% above
            st_tax_est  = max(0, st_net) * 0.22
            lt_tax_rate = 0.15  # most retail traders land here
            lt_tax_est  = max(0, lt_net) * lt_tax_rate
            total_tax   = st_tax_est + lt_tax_est

            sign = lambda x: f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}"

            msg = (
                f"📋 <b>Tax Summary {year}</b>\n\n"
                f"<b>Short-term</b>  <i>(&lt;1 year, {len(st_trades)} trades)</i>\n"
                f"  Gains:   <code>${st_gains:,.0f}</code>\n"
                f"  Losses:  <code>-${st_losses:,.0f}</code>\n"
                f"  Net:     <b>{sign(st_net)}</b>\n"
                f"  Est. tax: ~<code>${st_tax_est:,.0f}</code>  <i>(~22% ordinary income rate)</i>\n\n"
                f"<b>Long-term</b>  <i>(≥1 year, {len(lt_trades)} trades)</i>\n"
                f"  Gains:   <code>${lt_gains:,.0f}</code>\n"
                f"  Losses:  <code>-${lt_losses:,.0f}</code>\n"
                f"  Net:     <b>{sign(lt_net)}</b>\n"
                f"  Est. tax: ~<code>${lt_tax_est:,.0f}</code>  <i>(~15% preferential rate)</i>\n\n"
                f"<b>Total net P&amp;L:</b>  <b>{sign(total_net)}</b>\n"
                f"<b>Total estimated tax:</b>  ~<code>${total_tax:,.0f}</code>\n\n"
            )

            # Tax-loss harvesting opportunity
            if st_net > 0 and lt_losses == 0:
                msg += "💡 <i>Tip: Losses offset gains dollar-for-dollar. If you have underwater positions, realizing them before year-end can reduce your tax bill.</i>\n\n"
            elif st_net > 500:
                msg += f"💡 <i>Tip: Your short-term gains are taxed at your income rate. Consider holding new positions &gt;1yr to qualify for the lower long-term rate.</i>\n\n"

            msg += (
                "<i>⚠️ Estimates only — uses ~22% for short-term and ~15% for long-term. "
                "Actual rates depend on your income, state taxes, and other factors. "
                "Consult a tax professional for your filing.</i>"
            )
            return msg
        except Exception as exc:
            return f"⚠️ Could not compute tax summary: {exc}"

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
            import os as _os
            _app_url = _os.environ.get("APP_URL", "").rstrip("/")
            def _cm_btn(label, tab, fb):
                if _app_url:
                    return {"text": label, "web_app": {"url": f"{_app_url}/miniapp?tab={tab}"}}
                return {"text": label, "callback_data": f"cmd|{fb}"}
            send_inline_keyboard(
                community_msg,
                [[_cm_btn("📊 My Accuracy", "performance", "ACCURACY"),
                  _cm_btn("📋 My History",  "portfolio",   "HISTORY")]],
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
            from datetime import datetime, timedelta, timezone
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

            now = datetime.now(timezone.utc)
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

    # ── /earnings — upcoming earnings for open positions + watchlist ─────────
    if text == "EARNINGS":
        log      = load_user_trade_log(chat_id)
        open_t   = log.get("open", [])
        watchlist = log.get("watchlist", [])

        # Collect stock tickers from open positions + watchlist (deduplicated)
        seen     = set()
        tickers  = []
        for t in open_t:
            sym = t.get("ticker", "").upper()
            if sym and t.get("asset_type", "stock") == "stock" and sym not in seen:
                tickers.append(sym); seen.add(sym)
        for sym in watchlist:
            sym = sym.upper()
            if sym and sym not in seen:
                tickers.append(sym); seen.add(sym)

        if not tickers:
            return (
                "📅 <b>Upcoming Earnings</b>\n\n"
                "You have no open stock positions or watchlist tickers to check.\n\n"
                "Log a position with <code>/bought</code> or track a ticker with <code>/track TICKER</code>."
            )

        send_message("📅 <i>Fetching earnings dates…</i>", chat_id=chat_id)

        try:
            import yfinance as yf
            from datetime import datetime, timezone

            rows = []
            for sym in tickers:
                try:
                    info = yf.Ticker(sym).info
                    ts   = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
                    if not ts:
                        continue
                    dt       = datetime.fromtimestamp(ts, tz=timezone.utc)
                    today    = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                    days_out = (dt.replace(hour=0, minute=0, second=0, microsecond=0) - today).days
                    if days_out < -1:
                        continue   # already reported, skip
                    is_open  = any(t.get("ticker", "").upper() == sym for t in open_t)
                    rows.append({
                        "ticker":    sym,
                        "date":      dt.strftime("%b %d"),
                        "days_out":  days_out,
                        "is_open":   is_open,
                    })
                except Exception:
                    continue

            if not rows:
                return (
                    "📅 <b>Upcoming Earnings</b>\n\n"
                    "No upcoming earnings found for your positions/watchlist in the next 90 days.\n"
                    "<i>Data sourced from Yahoo Finance — dates may shift.</i>"
                )

            rows.sort(key=lambda r: r["days_out"])

            lines = []
            for r in rows[:15]:
                d  = r["days_out"]
                if d == 0:
                    when = "🔴 <b>Today</b>"
                elif d == 1:
                    when = "🟠 Tomorrow"
                elif d < 0:
                    when = "✅ Reported"
                else:
                    when = f"📆 in {d}d"
                badge = " 💼" if r["is_open"] else " 👁"
                lines.append(f"  <b>{r['ticker']}</b>{badge}  {r['date']}  —  {when}")

            msg = (
                "📅 <b>Upcoming Earnings</b>\n\n"
                + "\n".join(lines)
                + "\n\n<i>💼 = open position  ·  👁 = watchlist\n"
                "Dates sourced from Yahoo Finance — subject to change.</i>"
            )
            return msg

        except Exception as exc:
            return f"⚠️ Could not fetch earnings dates: {exc}"

    # ── /gainers, /losers, /movers ────────────────────────────────────────────
    if text in ("GAINERS", "LOSERS", "MOVERS"):
        try:
            import yfinance as yf
            # Representative liquid universe (~40 names across sectors)
            _UNIVERSE = [
                "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","AVGO","JPM","V",
                "UNH","JNJ","XOM","WMT","PG","MA","HD","CVX","MRK","ABBV",
                "LLY","KO","PEP","COST","BAC","NFLX","AMD","CRM","ORCL","ADBE",
                "INTC","QCOM","TXN","GS","MS","SPY","QQQ","IWM","DIA","GLD",
                "BTC-USD","ETH-USD",
            ]
            with typing_until_done(chat_id):
                data = yf.download(
                    _UNIVERSE, period="2d", interval="1d",
                    progress=False, auto_adjust=True, threads=True,
                )["Close"]

            if data.empty or len(data) < 2:
                return "⚠️ Could not fetch market data right now. Try again shortly."

            prev  = data.iloc[-2]
            curr  = data.iloc[-1]
            moves = []
            for sym in _UNIVERSE:
                try:
                    p, c = float(prev[sym]), float(curr[sym])
                    if p > 0 and c > 0:
                        moves.append((sym, c, round((c - p) / p * 100, 2)))
                except Exception:
                    pass

            gainers = sorted(moves, key=lambda x: -x[2])[:5]
            losers  = sorted(moves, key=lambda x:  x[2])[:5]

            def _rows(items, up: bool) -> str:
                lines = []
                for sym, price, pct in items:
                    icon = "🟢" if up else "🔴"
                    lines.append(
                        f"  {icon} <b>{sym}</b>  <code>${price:,.2f}</code>  "
                        f"<b>{'+' if pct>=0 else ''}{pct:.1f}%</b>"
                    )
                return "\n".join(lines)

            if text == "GAINERS":
                return f"📈 <b>Today's Top Gainers</b>\n\n{_rows(gainers, True)}\n\n<i>From a universe of ~40 liquid names. Use /movers for both.</i>"
            if text == "LOSERS":
                return f"📉 <b>Today's Top Losers</b>\n\n{_rows(losers, False)}\n\n<i>From a universe of ~40 liquid names. Use /movers for both.</i>"
            # MOVERS — both
            return (
                f"🔥 <b>Today's Movers</b>\n\n"
                f"📈 <b>Top Gainers</b>\n{_rows(gainers, True)}\n\n"
                f"📉 <b>Top Losers</b>\n{_rows(losers, False)}\n\n"
                f"<i>From a universe of ~40 liquid names.</i>"
            )
        except Exception as exc:
            return f"⚠️ Could not fetch movers: {exc}"

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

    # ── /heatmap — sector ETF performance grid ──────────────────────────────
    if text == "HEATMAP":
        import yfinance as _yf
        _SECTORS = [
            ("XLK",  "Tech"),
            ("XLF",  "Finance"),
            ("XLE",  "Energy"),
            ("XLV",  "Health"),
            ("XLC",  "Comm"),
            ("XLY",  "Cons Disc"),
            ("XLP",  "Cons Stpl"),
            ("XLI",  "Industrl"),
            ("XLB",  "Materials"),
            ("XLRE", "Real Est"),
            ("XLU",  "Utilities"),
        ]
        syms = [s for s, _ in _SECTORS]
        try:
            df = _yf.download(syms, period="2d", interval="1d",
                              auto_adjust=True, progress=False)
            closes = df["Close"] if "Close" in df else df
            results = {}
            for sym in syms:
                try:
                    col   = closes[sym].dropna()
                    prev, curr = float(col.iloc[-2]), float(col.iloc[-1])
                    results[sym] = round((curr - prev) / prev * 100, 2)
                except Exception:
                    results[sym] = None
        except Exception as exc:
            return f"⚠️ Could not fetch sector data: {exc}"

        def _dot(pct):
            if pct is None: return "⚪"
            if pct >= 1.5:  return "🟢"
            if pct >= 0.3:  return "🟩"
            if pct >= -0.3: return "🟡"
            if pct >= -1.5: return "🟧"
            return "🔴"

        lines = ["📊 <b>Sector Heatmap</b>  <i>(1-day % change)</i>\n"]
        for sym, label in _SECTORS:
            pct = results.get(sym)
            dot = _dot(pct)
            pct_str = f"{pct:+.2f}%" if pct is not None else "—"
            lines.append(f"{dot} <code>{label:<10}</code> <b>{pct_str}</b>")

        lines.append("\n<i>🟢 &gt;+1.5%   🟩 +0.3–1.5%   🟡 flat   🟧 −0.3–1.5%   🔴 &lt;−1.5%</i>")
        return "\n".join(lines)

    # ── /nextearnings — earnings dates for open positions ────────────────────
    if text == "NEXTEARNINGS":
        from earnings_checker import get_upcoming_earnings
        log = load_user_trade_log(chat_id)
        open_pos = log.get("open", [])
        stock_tickers = [t["ticker"] for t in open_pos if t.get("asset_type") != "crypto"]
        if not stock_tickers:
            return (
                "📭 No open stock positions.\n"
                "Log one with /bought — I'll track earnings dates for you."
            )
        try:
            upcoming = get_upcoming_earnings(stock_tickers, days_ahead=45)
        except Exception as exc:
            return f"⚠️ Could not fetch earnings data: {exc}"

        if not upcoming:
            return (
                f"📅 <b>No earnings in the next 45 days</b> for your open positions.\n"
                f"<i>Checked: {', '.join(stock_tickers)}</i>"
            )

        sorted_earn = sorted(upcoming.items(), key=lambda x: x[1])
        lines = ["📅 <b>Upcoming Earnings — your open positions</b>\n"]
        import datetime as _dt
        today_dt = _dt.date.today()
        for ticker, date_str in sorted_earn:
            try:
                d          = _dt.date.fromisoformat(date_str)
                days       = (d - today_dt).days
                days_label = f"in {days}d" if days > 1 else ("tomorrow" if days == 1 else "today")
                urgency    = "🔴" if days <= 3 else ("🟡" if days <= 7 else "🟢")
            except Exception:
                days_label = date_str
                urgency    = "📅"
            lines.append(f"  {urgency} <b>{ticker}</b> — {date_str}  <i>({days_label})</i>")

        lines.append(
            "\n<i>🔴 ≤3 days  🟡 ≤7 days  🟢 &gt;7 days\n"
            "Consider closing/trimming before earnings to avoid volatility.</i>"
        )
        return "\n".join(lines)

    # ── /digest — personalized morning check-in for open positions ───────────
    if text == "DIGEST":
        from cmd_helpers import _fetch_live_price
        from earnings_checker import get_upcoming_earnings

        log    = load_user_trade_log(chat_id)
        open_t = log.get("open", [])
        if not open_t:
            return (
                "📭 No open positions to digest.\n"
                "<i>Log one with /bought — I'll give you a morning brief on it each day.</i>"
            )

        send_message("🌅 <i>Building your morning digest…</i>", chat_id=chat_id)

        tickers = [t["ticker"] for t in open_t if t.get("ticker")]
        lines   = ["🌅 <b>Morning Digest</b>  ·  your positions\n"]

        # Live prices + vs entry
        for t in open_t:
            tk    = t.get("ticker")
            if not tk:
                continue
            entry = t.get("entry_price")
            stop  = t.get("stop_loss")
            tgt   = t.get("target_price")
            live  = _fetch_live_price(tk)

            parts_row = [f"<b>{tk}</b>"]
            if live:
                parts_row.append(f"<code>${_p(live)}</code>")
                if entry:
                    try:
                        pct  = (float(live) - float(entry)) / float(entry) * 100
                        sign = "+" if pct >= 0 else ""
                        parts_row.append(f"<b>{sign}{pct:.1f}%</b> vs entry")
                    except Exception:
                        pass
            # Proximity warnings
            badges = []
            if live and stop:
                try:
                    if float(live) <= float(stop) * 1.02:
                        badges.append("⚠️ near stop")
                except Exception:
                    pass
            if live and tgt:
                try:
                    if float(live) >= float(tgt) * 0.97:
                        badges.append("🎯 near target")
                except Exception:
                    pass
            if badges:
                parts_row.append("  ".join(badges))
            lines.append("  " + "  ·  ".join(parts_row))

        # Earnings in the next 7 days
        try:
            stock_tickers = [tk for tk in tickers if not tk.endswith("-USD")]
            earn_map = get_upcoming_earnings(stock_tickers, days_ahead=7)
            if earn_map:
                lines.append("")
                lines.append("⚠️ <b>Earnings this week:</b>")
                for tk, date_str in earn_map.items():
                    lines.append(f"  🗓 <b>{tk}</b> — {date_str}")
        except Exception:
            pass

        lines.append(
            "\n<i>Use /mymovers for a full ranked view  ·  /positions for P&amp;L details</i>"
        )
        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard("\n".join(lines), [[{"text": "📊 Open Dashboard", "web_app": {"url": f"{_app_url}/miniapp?tab=portfolio"}}]], chat_id=chat_id)
            return ""
        return "\n".join(lines)

    return None
