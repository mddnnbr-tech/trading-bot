"""
weekly_reporter.py
──────────────────
Generates and emails a weekly performance summary every Monday at 7:00 AM ET
(covering the prior Mon–Fri trading week).

Email sent via Gmail SMTP using an App Password (no OAuth needed).

Setup — add these to your .env file:
    GMAIL_ADDRESS=your@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   ← 16-char App Password
    REPORT_TO_EMAIL=your@gmail.com           ← where to send the report (can be same)

To generate + send immediately (manual trigger):
    python weekly_reporter.py --send-now

To run as a weekly cron job (add to crontab on your cloud VM):
    0 7 * * 1  /usr/bin/python3 /home/user/trading-bot/weekly_reporter.py --send-now
"""

from __future__ import annotations

import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv
from agent_evaluator import AgentEvaluator
from performance_logger import PerformanceLogger, LOGS_DIR

load_dotenv()
ET = ZoneInfo("America/New_York")


# ── Email credentials from .env ───────────────────────────────────────────────
GMAIL_ADDRESS    = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PW     = os.getenv("GMAIL_APP_PASSWORD", "")
REPORT_TO_EMAIL  = os.getenv("REPORT_TO_EMAIL", GMAIL_ADDRESS)


# ── Report builder ─────────────────────────────────────────────────────────────

class WeeklyReporter:

    def __init__(self):
        self.logger    = PerformanceLogger()
        self.evaluator = AgentEvaluator()

    def build_report(self, week_end: datetime | None = None) -> dict:
        """
        Build the weekly report data.
        week_end defaults to last Friday 16:00 ET.
        """
        now      = datetime.now(ET)
        week_end = week_end or self._last_friday_close(now)
        week_start = week_end - timedelta(days=5)

        # Pull week's trades
        trades_7d = self.logger.get_trades(last_n_days=7)

        # Per-agent breakdown
        from performance_logger import ENSEMBLE_AGENTS
        agent_breakdown = {}
        for agent in ENSEMBLE_AGENTS:
            agent_trades = [t for t in trades_7d if t["agent"] == agent]
            pnl   = sum(t["gross_pnl"] for t in agent_trades)
            wins  = sum(1 for t in agent_trades if t["gross_pnl"] >= 0)
            total = len(agent_trades)
            agent_breakdown[agent] = {
                "pnl":       round(pnl, 2),
                "trades":    total,
                "wins":      wins,
                "losses":    total - wins,
                "win_rate":  f"{wins/total*100:.0f}%" if total else "—",
            }

        total_pnl    = sum(t["gross_pnl"] for t in trades_7d)
        total_trades = len(trades_7d)
        total_wins   = sum(1 for t in trades_7d if t["gross_pnl"] >= 0)

        # Best and worst single trade
        best_trade  = max(trades_7d, key=lambda t: t["gross_pnl"], default=None)
        worst_trade = min(trades_7d, key=lambda t: t["gross_pnl"], default=None)

        # Eval snapshot
        report = self.evaluator.evaluate()

        # Benchmark comparison — SPY and QQQ weekly return
        benchmarks    = self._get_benchmark_returns(week_start, week_end)
        rotation_log  = self._get_rotation_events(last_n_days=7)
        regime_summary = self._get_regime_summary()

        # Running portfolio equity (all-time cumulative P&L by week)
        all_trades    = self.logger.get_trades(last_n_days=365)
        equity_curve  = self._build_equity_curve(all_trades)

        # All-time totals
        all_time_pnl   = sum(t["gross_pnl"] for t in all_trades)
        all_time_trades = len(all_trades)

        # Portfolio return % this week
        account_balance = float(os.getenv("ACCOUNT_BALANCE", "16000"))
        weekly_return_pct = (total_pnl / account_balance * 100) if account_balance else 0

        return {
            "week_start":         week_start.strftime("%b %d"),
            "week_end":           week_end.strftime("%b %d, %Y"),
            "generated_at":       now.strftime("%Y-%m-%d %H:%M ET"),
            "total_pnl":          round(total_pnl, 2),
            "total_trades":       total_trades,
            "total_wins":         total_wins,
            "total_losses":       total_trades - total_wins,
            "overall_win_rate":   f"{total_wins/total_trades*100:.0f}%" if total_trades else "—",
            "agent_breakdown":    agent_breakdown,
            "top_agent":          report.top_agent,
            "flagged_agents":     report.flagged_agents,
            "best_trade":         best_trade,
            "worst_trade":        worst_trade,
            "benchmarks":         benchmarks,
            "equity_curve":       equity_curve,
            "all_time_pnl":       round(all_time_pnl, 2),
            "all_time_trades":    all_time_trades,
            "weekly_return_pct":  round(weekly_return_pct, 2),
            "account_balance":    account_balance,
            "rotation_log":       rotation_log,
            "regime_summary":     regime_summary,
        }

    @staticmethod
    def _get_rotation_events(last_n_days: int = 7) -> list[dict]:
        """Read rotation_log.jsonl for events this week."""
        try:
            path = LOGS_DIR / "rotation_log.jsonl"
            if not path.exists():
                return []
            cutoff = datetime.now(ZoneInfo("America/New_York")) - timedelta(days=last_n_days)
            events = []
            for line in path.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    ts  = datetime.fromisoformat(rec.get("timestamp", ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=ZoneInfo("America/New_York"))
                    if ts >= cutoff:
                        events.append(rec)
                except Exception:
                    pass
            return events
        except Exception:
            return []

    @staticmethod
    def _get_regime_summary() -> dict:
        """Read latest_eval.json for regime hints."""
        try:
            path = LOGS_DIR / "latest_eval.json"
            if not path.exists():
                return {}
            import json as _json
            return _json.loads(path.read_text())
        except Exception:
            return {}

    @staticmethod
    def _get_benchmark_returns(week_start: datetime, week_end: datetime) -> dict:
        """Fetch SPY and QQQ returns for the same week period."""
        results = {}
        for ticker in ["SPY", "QQQ"]:
            try:
                df = yf.Ticker(ticker).history(
                    start=week_start.strftime("%Y-%m-%d"),
                    end=(week_end + timedelta(days=1)).strftime("%Y-%m-%d"),
                )
                if len(df) >= 2:
                    open_price  = float(df["Close"].iloc[0])
                    close_price = float(df["Close"].iloc[-1])
                    ret_pct     = (close_price - open_price) / open_price * 100
                    results[ticker] = round(ret_pct, 2)
                else:
                    results[ticker] = None
            except Exception:
                results[ticker] = None
        return results

    @staticmethod
    def _build_equity_curve(trades: list[dict]) -> list[dict]:
        """Build a weekly cumulative P&L curve from all historical trades."""
        if not trades:
            return []
        # Sort trades by timestamp
        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", ""))
        # Group by ISO week
        from collections import defaultdict
        weekly: dict[str, float] = defaultdict(float)
        for t in sorted_trades:
            ts  = t.get("timestamp", "")
            try:
                dt  = datetime.fromisoformat(ts[:10])
                key = dt.strftime("%Y-W%W")
            except Exception:
                continue
            weekly[key] += t.get("gross_pnl", 0)
        # Build cumulative curve
        curve     = []
        cumulative = 0.0
        for week, pnl in sorted(weekly.items()):
            cumulative += pnl
            curve.append({"week": week, "weekly_pnl": round(pnl, 2), "cumulative": round(cumulative, 2)})
        return curve

    def format_email_html(self, data: dict) -> str:
        """Render the report as a clean HTML email."""
        pnl_color = "#22c55e" if data["total_pnl"] >= 0 else "#ef4444"
        pnl_sign  = "+" if data["total_pnl"] >= 0 else ""

        # Agent table rows
        agent_rows = ""
        for name, stats in sorted(
            data["agent_breakdown"].items(),
            key=lambda x: x[1]["pnl"], reverse=True
        ):
            row_color = "#f0fdf4" if stats["pnl"] >= 0 else "#fef2f2"
            pnl_s = f"+${stats['pnl']:,.2f}" if stats["pnl"] >= 0 else f"-${abs(stats['pnl']):,.2f}"
            agent_rows += f"""
            <tr style="background:{row_color}">
                <td style="padding:8px 12px;font-weight:600">{name}</td>
                <td style="padding:8px 12px;text-align:right;font-weight:700;color:{'#22c55e' if stats['pnl']>=0 else '#ef4444'}">{pnl_s}</td>
                <td style="padding:8px 12px;text-align:center">{stats['trades']}</td>
                <td style="padding:8px 12px;text-align:center">{stats['win_rate']}</td>
            </tr>"""

        best_str  = (
            f"{data['best_trade']['symbol']} +${data['best_trade']['gross_pnl']:,.2f} "
            f"({data['best_trade']['agent']})"
            if data["best_trade"] else "—"
        )
        worst_str = (
            f"{data['worst_trade']['symbol']} -${abs(data['worst_trade']['gross_pnl']):,.2f} "
            f"({data['worst_trade']['agent']})"
            if data["worst_trade"] else "—"
        )
        flagged_str = (
            f"⚠️  {', '.join(data['flagged_agents'])} flagged and rotated out this week."
            if data["flagged_agents"] else
            "✅  All agents performed within threshold — no rotations triggered."
        )
        top_agent_str = data["top_agent"] or "N/A"

        # Benchmark comparison rows
        bm = data.get("benchmarks", {})
        spy_ret  = bm.get("SPY")
        qqq_ret  = bm.get("QQQ")
        port_ret = data.get("weekly_return_pct", 0)

        def fmt_ret(r):
            if r is None:
                return "—"
            color = "#22c55e" if r >= 0 else "#ef4444"
            sign  = "+" if r >= 0 else ""
            return f'<span style="color:{color};font-weight:700">{sign}{r:.2f}%</span>'

        benchmark_rows = f"""
        <tr><td style="padding:8px 12px;font-weight:600">Your Bot</td>
            <td style="padding:8px 12px;text-align:right">{fmt_ret(port_ret)}</td>
            <td style="padding:8px 12px;font-size:12px;color:#64748b">${data['account_balance']:,.0f} base</td></tr>
        <tr style="background:#f8fafc"><td style="padding:8px 12px;font-weight:600">S&P 500 (SPY)</td>
            <td style="padding:8px 12px;text-align:right">{fmt_ret(spy_ret)}</td>
            <td style="padding:8px 12px;font-size:12px;color:#64748b">Buy &amp; hold benchmark</td></tr>
        <tr><td style="padding:8px 12px;font-weight:600">Nasdaq (QQQ)</td>
            <td style="padding:8px 12px;text-align:right">{fmt_ret(qqq_ret)}</td>
            <td style="padding:8px 12px;font-size:12px;color:#64748b">Tech-heavy benchmark</td></tr>
        """

        # Equity curve (last 8 weeks)
        curve = data.get("equity_curve", [])[-8:]
        curve_rows = ""
        for pt in curve:
            c_color = "#22c55e" if pt["cumulative"] >= 0 else "#ef4444"
            w_color = "#22c55e" if pt["weekly_pnl"] >= 0 else "#ef4444"
            curve_rows += f"""
            <tr>
              <td style="padding:6px 12px;font-size:13px">{pt['week']}</td>
              <td style="padding:6px 12px;text-align:right;color:{w_color};font-size:13px">
                {'+'if pt['weekly_pnl']>=0 else ''}${pt['weekly_pnl']:,.2f}</td>
              <td style="padding:6px 12px;text-align:right;color:{c_color};font-weight:700;font-size:13px">
                {'+'if pt['cumulative']>=0 else ''}${pt['cumulative']:,.2f}</td>
            </tr>"""
        if not curve_rows:
            curve_rows = '<tr><td colspan="3" style="padding:12px;color:#94a3b8;text-align:center">Building equity history — check back next week</td></tr>'

        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background:#f8fafc; margin:0; padding:0; color:#1e293b; }}
  .wrapper {{ max-width:620px; margin:32px auto; background:#fff;
              border-radius:12px; box-shadow:0 2px 12px rgba(0,0,0,.08); overflow:hidden; }}
  .header {{ background:#0f172a; padding:28px 32px; }}
  .header h1 {{ color:#fff; margin:0; font-size:20px; font-weight:700; }}
  .header p  {{ color:#94a3b8; margin:4px 0 0; font-size:13px; }}
  .body {{ padding:28px 32px; }}
  .kpi-row {{ display:flex; gap:12px; margin-bottom:24px; }}
  .kpi {{ flex:1; background:#f1f5f9; border-radius:8px; padding:16px; text-align:center; }}
  .kpi .val {{ font-size:22px; font-weight:700; }}
  .kpi .lbl {{ font-size:12px; color:#64748b; margin-top:4px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ background:#1e293b; color:#fff; padding:9px 12px; text-align:left; font-weight:600; }}
  th:not(:first-child) {{ text-align:center; }}
  tr:last-child td {{ border-bottom:none; }}
  td {{ border-bottom:1px solid #e2e8f0; }}
  .section-title {{ font-size:15px; font-weight:700; color:#1e293b; margin:24px 0 10px; }}
  .note {{ background:#fffbeb; border-left:4px solid #f59e0b; padding:12px 16px;
           border-radius:4px; font-size:13px; color:#92400e; margin-top:20px; }}
  .footer {{ background:#f1f5f9; padding:16px 32px; font-size:12px; color:#94a3b8; text-align:center; }}
</style></head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>📊 Weekly Trading Performance Report</h1>
    <p>Week of {data['week_start']} – {data['week_end']}  •  Generated {data['generated_at']}</p>
  </div>
  <div class="body">
    <div class="kpi-row">
      <div class="kpi">
        <div class="val" style="color:{pnl_color}">{pnl_sign}${data['total_pnl']:,.2f}</div>
        <div class="lbl">Net P&amp;L</div>
      </div>
      <div class="kpi">
        <div class="val">{data['total_trades']}</div>
        <div class="lbl">Trades Executed</div>
      </div>
      <div class="kpi">
        <div class="val">{data['total_wins']}W / {data['total_losses']}L</div>
        <div class="lbl">Win / Loss</div>
      </div>
      <div class="kpi">
        <div class="val">{top_agent_str}</div>
        <div class="lbl">Top Agent (20d)</div>
      </div>
    </div>

    <div class="section-title">Agent Performance Breakdown</div>
    <table>
      <thead>
        <tr><th>Agent</th><th>P&amp;L</th><th>Trades</th><th>Win %</th></tr>
      </thead>
      <tbody>{agent_rows}</tbody>
    </table>

    <div class="section-title">Notable Trades</div>
    <table>
      <thead><tr><th>Metric</th><th>Trade</th></tr></thead>
      <tbody>
        <tr style="background:#f0fdf4">
          <td style="padding:8px 12px;font-weight:600">Best Trade</td>
          <td style="padding:8px 12px">{best_str}</td>
        </tr>
        <tr style="background:#fef2f2">
          <td style="padding:8px 12px;font-weight:600">Worst Trade</td>
          <td style="padding:8px 12px">{worst_str}</td>
        </tr>
      </tbody>
    </table>

    <div class="section-title">📊 Performance vs. Benchmarks (This Week)</div>
    <table>
      <thead>
        <tr><th>Portfolio / Index</th><th style="text-align:right">Weekly Return</th><th>Notes</th></tr>
      </thead>
      <tbody>{benchmark_rows}</tbody>
    </table>

    <div class="section-title">📈 Cumulative Equity Curve</div>
    <table>
      <thead>
        <tr><th>Week</th><th style="text-align:right">Weekly P&amp;L</th><th style="text-align:right">All-Time Total</th></tr>
      </thead>
      <tbody>{curve_rows}</tbody>
    </table>
    <p style="font-size:12px;color:#64748b;margin:8px 0 0">
      All-time P&amp;L: <strong>${data['all_time_pnl']:+,.2f}</strong> across {data['all_time_trades']} trades
    </p>

    <div class="section-title">🔄 Agent Rotation This Week</div>
    {self._format_rotation_html(data.get('rotation_log', []), flagged_str)}

    <div class="section-title">🧠 What Happens Next Week (Automatic)</div>
    <p style="font-size:13px;color:#475569;margin:0 0 8px">
      No action needed from you. The system will automatically:
    </p>
    <ul style="font-size:13px;color:#475569;margin:0;padding-left:20px">
      <li>Boost agents that performed best this week (profit-weighted)</li>
      <li>Detect market regime each tick and activate matching specialists</li>
      <li>Bench underperformers at 10 AM and 3:30 PM ET daily</li>
      <li>Restore benched agents after 3 days if conditions improve</li>
    </ul>
  </div>
  <div class="footer">
    Automated Trading Bot  •  E*TRADE/Morgan Stanley  •  This report is auto-generated.<br>
    Check your E*TRADE account for confirmed position details.
  </div>
</div>
</body>
</html>"""

    @staticmethod
    def _format_rotation_html(events: list[dict], fallback_str: str) -> str:
        if not events:
            return f'<div class="note"><strong>Agent Rotation:</strong><br>{fallback_str}</div>'
        rows = ""
        for e in events:
            ts      = e.get("timestamp", "")[:10]
            event   = e.get("event", "")
            agent   = e.get("agent", "")
            rep     = e.get("replacement")
            color   = "#f0fdf4" if event == "REACTIVATED" else "#fef2f2"
            icon    = "✅" if event == "REACTIVATED" else "⏸"
            detail  = f"→ promoted {rep}" if rep else ""
            rows += f'<tr style="background:{color}"><td style="padding:7px 12px">{icon} {ts}</td><td style="padding:7px 12px;font-weight:600">{agent}</td><td style="padding:7px 12px">{event} {detail}</td></tr>'
        return f"""
        <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:16px">
          <thead><tr style="background:#1e293b;color:#fff">
            <th style="padding:8px 12px;text-align:left">Date</th>
            <th style="padding:8px 12px;text-align:left">Agent</th>
            <th style="padding:8px 12px;text-align:left">Action</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    def send(self, html: str, subject: str | None = None) -> bool:
        """Send the HTML email via Gmail SMTP. Returns True on success."""
        if not GMAIL_ADDRESS or not GMAIL_APP_PW:
            print("ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set in .env")
            return False

        now     = datetime.now(ET)
        subject = subject or f"Trading Bot Weekly Report — Week ending {now.strftime('%b %d, %Y')}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = REPORT_TO_EMAIL
        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PW)
                server.sendmail(GMAIL_ADDRESS, REPORT_TO_EMAIL, msg.as_string())
            print(f"✅  Weekly report sent to {REPORT_TO_EMAIL}")
            return True
        except Exception as e:
            print(f"❌  Failed to send email: {e}")
            return False

    def save_html(self, html: str) -> str:
        """Save HTML report to disk (always done, regardless of email success)."""
        filename = LOGS_DIR / f"weekly_report_{datetime.now(ET).strftime('%Y-%m-%d')}.html"
        with open(filename, "w") as f:
            f.write(html)
        print(f"Report saved → {filename}")
        return str(filename)

    @staticmethod
    def _last_friday_close(now: datetime) -> datetime:
        """Return the most recent Friday 16:00 ET before 'now'."""
        days_since_friday = (now.weekday() - 4) % 7
        if days_since_friday == 0 and now.hour < 16:
            days_since_friday = 7
        last_friday = now - timedelta(days=days_since_friday)
        return last_friday.replace(hour=16, minute=0, second=0, microsecond=0)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    reporter = WeeklyReporter()
    data     = reporter.build_report()
    html     = reporter.format_email_html(data)
    reporter.save_html(html)

    if "--send-now" in sys.argv:
        reporter.send(html)
    else:
        print("Report generated (HTML saved). Pass --send-now to email it.")
