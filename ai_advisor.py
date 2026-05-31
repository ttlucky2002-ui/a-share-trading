"""
AI选股顾问模块 - AI Stock Advisor
==================================
整合财经新闻、市场数据、技术指标，通过 DeepSeek AI 提供智能选股建议。
"""
import json
import os
import re
import time
import requests
from datetime import datetime
from typing import Optional

from config import AI_CONFIG
from data_feed import DataFeed


class AIAdvisor:
    """AI选股顾问"""

    def __init__(self):
        self.df = DataFeed()
        self.api_key = AI_CONFIG["api_key"] or os.environ.get("DEEPSEEK_API_KEY", "")
        self.api_url = AI_CONFIG["api_url"]
        self.model = AI_CONFIG["model"]
        self.max_tokens = AI_CONFIG["max_tokens"]
        self.temperature = AI_CONFIG["temperature"]
        self.system_prompt = AI_CONFIG["system_prompt"]

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _call_ai(self, messages: list, max_tokens: int = None,
                 temperature: float = None) -> str:
        if not self.api_key:
            return "❌ 未配置 DeepSeek API Key。请设置环境变量 DEEPSEEK_API_KEY，或在 Streamlit Cloud 的 Secrets 中配置同名变量。"

        for attempt in range(3):
            try:
                resp = requests.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "max_tokens": max_tokens or self.max_tokens,
                        "temperature": self.temperature if temperature is None else temperature,
                    },
                    timeout=60,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "")
                    return "AI 返回了空响应"

                if resp.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue

                err = resp.json()
                return f"❌ API调用失败: {err.get('error', {}).get('message', str(resp.status_code))}"

            except requests.Timeout:
                return "❌ API请求超时，请稍后重试"
            except Exception as e:
                if attempt < 2:
                    time.sleep(1)
                    continue
                return f"❌ 网络错误: {str(e)}"

        return "❌ 多次重试失败，请检查网络连接"

    def analyze_market(self) -> dict:
        """综合市场分析（新闻+概念+资金）"""
        if not self.is_configured:
            return {"error": "API Key 未配置", "response": ""}

        context = self.df.get_market_context()
        news = []
        if AI_CONFIG.get("enable_news", True):
            news = self.df.get_financial_news(AI_CONFIG.get("news_count", 30))

        user_prompt = self._build_market_prompt(context, news)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        response = self._call_ai(messages)

        return {
            "response": response,
            "context_summary": {
                "market": f"{context['market_stats']['total']}只, 涨{context['market_stats']['up']}跌{context['market_stats']['down']}, 涨停{context['market_stats']['limit_up']}",
                "hot_concepts": [c["name"] for c in context.get("hot_concepts", [])[:5]],
                "news_count": len(news),
            },
        }

    def chat_with_context(self, user_message: str) -> dict:
        """携带市场上下文的AI对话"""
        if not self.is_configured:
            return {"error": "API Key 未配置", "response": ""}

        context = self.df.get_market_context()
        news = []
        if AI_CONFIG.get("enable_news", True):
            news = self.df.get_financial_news(AI_CONFIG.get("news_count", 20))

        context_prompt = self._build_context_injection(context, news)
        full_prompt = f"{context_prompt}\n\n---\n用户问题：{user_message}\n\n请基于以上市场数据，给出专业的选股分析和建议。"

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": full_prompt},
        ]
        response = self._call_ai(messages)
        return {"response": response}

    def analyze_stock(self, code: str) -> dict:
        """个股中长期分析，提示词中的基本面字段均来自财务 API。"""
        if not self.is_configured:
            return {"error": "API Key 未配置", "response": ""}

        stocks = self.df.get_stock_list()
        stock_info = {}
        if not stocks.empty:
            m = stocks[stocks["code"] == code]
            if not m.empty:
                s = m.iloc[0]
                stock_info = {
                    "code": code, "name": s.get("name", ""),
                    "price": round(s.get("price") or 0, 2),
                    "change_pct": round(s.get("change_pct") or 0, 2),
                    "market_cap": round(s.get("market_cap") or 0, 1),
                    "turnover_rate": round(s.get("turnover_rate") or 0, 2),
                    "pe": round(s.get("pe") or 0, 2),
                    "board": s.get("board", ""),
                }

        financial = self.df.get_financial_data(code)
        if financial.get("available"):
            stock_info.update(financial)

        kline = self.df.get_kline(code, count=80)
        tech_summary = ""
        if not kline.empty:
            latest = kline.iloc[-1]
            tech_summary = (
                f"MA5:{latest.get('MA5','N/A')} MA10:{latest.get('MA10','N/A')} MA20:{latest.get('MA20','N/A')} "
                f"MACD_DIF:{latest.get('MACD_DIF','N/A')} DEA:{latest.get('MACD_DEA','N/A')} "
                f"RSI:{latest.get('RSI','N/A')} KDJ_K:{latest.get('KDJ_K','N/A')}"
            )

        fund_flow = self.df.get_fund_flow(code)

        prompt = f"""请对以下A股股票进行面向至少持仓一周的中长期研究分析，给出进入观察池/继续跟踪/回避的结论：

股票代码: {stock_info.get('code', code)}
股票名称: {stock_info.get('name', '未知')}
当前价格: {stock_info.get('price', 'N/A')}
涨跌幅: {stock_info.get('change_pct', 'N/A')}%
市值: {stock_info.get('market_cap', 'N/A')}亿
换手率: {stock_info.get('turnover_rate', 'N/A')}%
市盈率: {stock_info.get('pe', 'N/A')}
所属板块: {stock_info.get('board', 'N/A')}

财务数据源: {stock_info.get('data_source', '未取得财务API数据')}
财务报告期: {stock_info.get('report_date', 'N/A')}（披露日 {stock_info.get('notice_date', 'N/A')}）
ROE: {stock_info.get('roe', 'N/A')}%（年化参考 {stock_info.get('annualized_roe', 'N/A')}%）
营收同比/归母净利同比: {stock_info.get('revenue_growth', 'N/A')}% / {stock_info.get('profit_growth', 'N/A')}%
毛利率: {stock_info.get('gross_margin', 'N/A')}%
每股收益/每股经营现金流: {stock_info.get('eps', 'N/A')} / {stock_info.get('operating_cf_per_share', 'N/A')}
市净率: {stock_info.get('pb', 'N/A')}

技术指标摘要: {tech_summary}
资金流向: {json.dumps(fund_flow, ensure_ascii=False) if fund_flow else '暂无数据'}

请从以下几个维度给出分析：
1. 基本面质量与增长可持续性（只依据上方报告字段）
2. 估值与现金流风险
3. 一周以上持有需要关注的催化剂/核验项
4. 趋势仅作为进入时点参考，不得压过基本面
5. 风险提示；若财务字段不足，明确说明无法判断"""

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]
        response = self._call_ai(messages)
        return {"response": response, "stock": stock_info}

    def explain_long_term_candidates(self, candidates: list) -> list:
        """让 AI 在确定性评分之后补充摘要，不允许改写评分或虚构指标。"""
        if not self.is_configured or not candidates:
            return candidates
        rows = []
        for item in candidates[:20]:
            rows.append(
                f"{item['code']} {item.get('name', '')} | 报告期:{item.get('report_date', '')} | "
                f"量化基本面分:{item.get('fundamental_score', 0)} | "
                f"ROE年化参考:{item.get('annualized_roe', 0)}% | "
                f"营收同比:{item.get('revenue_growth', 0):+.1f}% 净利同比:{item.get('profit_growth', 0):+.1f}% | "
                f"PE:{item.get('pe', 0):.1f} PB:{item.get('pb', 0):.1f} | "
                f"每股经营现金流:{item.get('operating_cf_per_share', 0):.3f} | "
                f"规则风险:{item.get('risk', '')}"
            )
        prompt = f"""以下是由财务API逐股取得、并由固定规则评分后的中长期候选股票。
目标持仓周期至少一周。你只负责为每只股票补充简短研究摘要和需要核验的风险，不得修改评分，不得引用未提供的财报或公告。

{chr(10).join(rows)}

每只股票严格输出一行 JSON：
{{"code":"000001","summary":"基于所列指标的40字内摘要","risk_note":"需要进一步核验的风险，30字内"}}"""
        response = self._call_ai([
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ])
        additions = {}
        for line in response.strip().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
                additions[str(item.get("code", ""))] = item
            except json.JSONDecodeError:
                continue
        for candidate in candidates:
            addition = additions.get(candidate["code"])
            if addition:
                candidate["ai_summary"] = str(addition.get("summary", ""))
                candidate["ai_risk_note"] = str(addition.get("risk_note", ""))
        return candidates

    def suggest_backtest_parameters(self, current: dict, summary: dict,
                                    trials: list) -> dict:
        """Ask DeepSeek for one bounded parameter candidate; backtests choose the winner."""
        if not self.is_configured:
            raise ValueError("未配置 DeepSeek API Key")
        concise_trials = [{
            "round": trial.get("round"),
            "parameters": trial.get("parameters"),
            "summary": trial.get("summary"),
        } for trial in trials[-4:]]
        prompt = f"""你正在为唯一的“小资金稳健策略”提出下一组回测参数。
所有评估均针对同一批核心股，并以每只股票同期买入持有为基准。只提出参数候选，不得宣称未来有效。

当前最优参数:
{json.dumps(current, ensure_ascii=False)}

当前最优汇总指标:
{json.dumps(summary, ensure_ascii=False)}

已测试方案:
{json.dumps(concise_trials, ensure_ascii=False)}

目标函数重点提高平均超额收益，同时抑制最大回撤。请在以下范围内给出一组与已测方案不同、可解释的候选：
- stop_loss: -0.20 至 -0.005
- take_profit: 0.005 至 0.50
- trailing_activation: 0.005 至 0.50
- trailing_distance: 0.005 至 0.20，且小于 take_profit
- max_hold_days: 3 至 120 的整数
- min_signals: 1 至 4 的整数
- position_size_pct: 0.01 至 0.50

严格只输出一个 JSON 对象：
{{"parameters":{{"stop_loss":-0.03,"take_profit":0.06,"trailing_activation":0.03,"trailing_distance":0.025,"max_hold_days":25,"min_signals":2,"position_size_pct":0.2}},"reason":"参数调整理由，50字内"}}"""
        response = self._call_ai([
            {"role": "system", "content": "你是谨慎的量化参数研究助手，只输出可解析 JSON，不承诺收益。"},
            {"role": "user", "content": prompt},
        ])
        if response.startswith("❌"):
            raise ValueError(response)
        match = re.search(r"\{[\s\S]*\}", response)
        if not match:
            raise ValueError("DeepSeek 未返回可解析参数 JSON")
        try:
            proposal = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError("DeepSeek 参数 JSON 解析失败") from exc
        parameters = proposal.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("DeepSeek 未返回 parameters 对象")
        return {
            "parameters": parameters,
            "reason": str(proposal.get("reason", "")).strip(),
        }

    def _build_market_prompt(self, context: dict, news: list) -> str:
        prompt = f"""当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}

=== 今日市场概况 ===
全市场{context['market_stats']['total']}只股票，上涨{context['market_stats']['up']}只，下跌{context['market_stats']['down']}只，
涨停{context['market_stats']['limit_up']}只，跌停{context['market_stats']['limit_down']}只，平均涨跌幅{context['market_stats']['avg_change']}%

=== 涨幅榜 TOP10 ===
"""
        for s in context.get("top_gainers", []):
            prompt += f"  {s['code']} {s['name']} {s['change_pct']:+.2f}% [{s['board']}]\n"

        prompt += "\n=== 热门概念板块（资金+涨幅综合） ===\n"
        for c in context.get("hot_concepts", []):
            prompt += f"  {c['name']}: 涨幅{c['change_pct']:+.2f}% 主力净流入{c['main_net_inflow']:.2f}亿\n"

        prompt += "\n=== 行业板块资金流向 TOP10 ===\n"
        for s in context.get("sector_flow", []):
            prompt += f"  {s['name']}: 涨幅{s['change_pct']:+.2f}% 净流入{s['main_net_inflow']:.2f}亿\n"

        if news:
            prompt += f"\n=== 最新财经新闻（{len(news)}条） ===\n"
            for n in news[:15]:
                prompt += f"  [{n['time']}] {n['title']}\n"

        prompt += """\n请基于以上数据，从以下角度给出选股建议：
1. 大盘情绪判断（偏多/偏空/中性，依据涨跌比和涨停数）
2. 当前市场主线热点题材是什么？（从涨停股和概念板块中归纳）
3. 资金在往哪些方向流动？持续性如何？
4. 结合新闻事件，预判接下来可能轮动的题材方向
5. 给出3-5只值得关注的标的（含股票代码 + 简要逻辑）

请用中文回复，分析务实、有数据支撑，避免空泛喊单。"""
        return prompt

    def _build_context_injection(self, context: dict, news: list) -> str:
        """构建注入对话的市场上下文"""
        ctx = f"[当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}]\n"
        ctx += f"[市场概况: {context['market_stats']['total']}只股票, 涨{context['market_stats']['up']}跌{context['market_stats']['down']}, 涨停{context['market_stats']['limit_up']}, 跌停{context['market_stats']['limit_down']}, 均涨幅{context['market_stats']['avg_change']}%]\n"

        gainers = [f"{g['code']}{g['name']}({g['change_pct']:+.1f}%)" for g in context.get("top_gainers", [])[:5]]
        ctx += f"[涨幅TOP5: {', '.join(gainers)}]\n"

        concepts = [c['name'] for c in context.get("hot_concepts", [])[:8]]
        ctx += f"[热门概念: {', '.join(concepts)}]\n"

        if news:
            headlines = [n['title'][:40] for n in news[:8]]
            ctx += f"[最新新闻: {'; '.join(headlines)}]\n"

        ctx += "\n请基于以上实时市场数据回答用户的问题。如果用户询问个股，请结合这些大盘背景给出有洞察力的建议。"
        return ctx

    def get_international_context(self) -> str:
        """让AI总结当前国际局势对A股的潜在影响"""
        if not self.is_configured:
            return ""
        prompt = """请简要分析当前国际局势中对A股市场有重大影响的因素，包括但不限于：
1. 中美关系最新动向（关税、科技制裁、地缘政治）
2. 全球主要央行货币政策预期（美联储、欧央行）
3. 大宗商品价格趋势（原油、黄金、铜等）
4. 全球供应链/芯片/新能源等重要产业动态
5. 亚太地区地缘政治风险

请用200字以内的摘要，重点突出对A股的具体影响方向（利好哪些板块、利空哪些板块）。"""
        messages = [
            {"role": "system", "content": "你是一位国际宏观分析师，擅长将全球事件映射到A股投资机会。"},
            {"role": "user", "content": prompt},
        ]
        return self._call_ai(messages)

    def score_fundamentals_batch(self, candidates: list, international_ctx: str = "") -> list:
        """批量基本面+真实业务+国际局势综合评分，返回 [{code, score, reason}]"""
        if not self.is_configured or not candidates:
            return []

        finance_map = self.df.get_financials_batch([c["code"] for c in candidates])

        stock_lines = []
        for c in candidates:
            code = c["code"]
            fin = finance_map.get(code, {})
            stock_lines.append(
                f"{code} {c.get('name','')} | {fin.get('board','')} | "
                f"PE:{fin.get('pe',0):.1f} 市值:{fin.get('market_cap',0):.0f}亿 | "
                f"现价:{fin.get('price',0)} 涨幅:{fin.get('change_pct',0):+.1f}% | "
                f"换手:{fin.get('turnover_rate',0):.1f}% 主力净流:{fin.get('main_net',0):.2f}亿 | "
                f"技术面得分:{c.get('tech_score',c.get('score',0))}"
            )

        intl_section = ""
        if international_ctx:
            intl_section = f"\n【国际局势背景】\n{international_ctx}\n"

        prompt = f"""请对以下A股候选标的进行深度基本面评估，结合你对该公司的业务了解、财报质量和国际局势影响，给出0-100分的综合评分。

{intl_section}
【候选标的参考数据】
{chr(10).join(stock_lines)}

评估维度（每只股票综合出一个0-100的评分）：
1. 公司业务质地（25分）：主营业务护城河/行业地位/国家扶持方向（请基于你对这家公司业务的了解）
2. 财报与估值（25分）：PE/市值是否合理，结合你对这家公司近两年财报表现的了解
3. 国际局势影响（25分）：当前国际环境对该公司的业务是利好还是利空？（如制裁/关税/供应链/大宗商品）
4. 大环境与景气度（25分）：政策面/行业景气度/当前市场风格是否支持

请严格按照以下JSON格式返回（不要任何额外文字），每只股票一行：
{{"code":"000001","score":75,"reason":"银行龙头低估值受益宽松","risk":"地产敞口"}}
{{"code":"600519","score":82,"reason":"消费龙头高ROE抗周期","risk":"消费税改革"}}
..."""

        messages = [
            {"role": "system", "content": "你是一位CFA持证的资深基本面分析师。请严格按照JSON格式返回评分，不要输出任何额外内容。"},
            {"role": "user", "content": prompt},
        ]
        response = self._call_ai(messages)

        results = []
        for line in response.strip().split("\n"):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    results.append(obj)
                except json.JSONDecodeError:
                    import re
                    m = re.search(r'"code"\s*:\s*"(\d+)".*"score"\s*:\s*(\d+)', line)
                    if m:
                        results.append({"code": m.group(1), "score": int(m.group(2)), "reason": ""})

        score_map = {}
        for r in results:
            score_map[r["code"]] = r
        final = []
        for c in candidates:
            code = c["code"]
            fin = finance_map.get(code, {})
            ai = score_map.get(code, {})
            final.append({
                "code": code,
                "name": c.get("name", ""),
                "tech_score": c.get("tech_score", c.get("score", 0)),
                "fundamental_score": ai.get("score", 50),
                "fundamental_reason": ai.get("reason", ""),
                "risk": ai.get("risk", ""),
                "pe": fin.get("pe", 0),
                "market_cap": fin.get("market_cap", 0),
                "board": fin.get("board", ""),
                "composite_score": round(
                    c.get("tech_score", c.get("score", 0)) * 0.4 +
                    ai.get("score", 50) * 0.6
                ),
            })
        final.sort(key=lambda x: x["composite_score"], reverse=True)
        return final

    def analyze_portfolio(self, portfolio: dict, tail_candidates: list = None) -> dict:
        """基于市场数据对持仓进行 DeepSeek 诊断分析。"""
        if not self.is_configured:
            return {"error": "API Key 未配置", "response": ""}

        context = self.df.get_market_context()
        news = []
        if AI_CONFIG.get("enable_news", True):
            news = self.df.get_financial_news(AI_CONFIG.get("news_count", 20))

        positions = portfolio.get("positions", [])
        summary = portfolio.get("summary", {})
        capital = portfolio.get("capital", 0)

        # 构建提示词
        prompt = f"""当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}

=== 账户概况 ===
总资产: {summary.get('total_asset', 0):.0f} 元
可用资金: {summary.get('available_cash', 0):.0f} 元
持仓市值: {summary.get('market_value', 0):.0f} 元
总盈亏: {summary.get('total_pnl', 0):+.0f} 元 ({summary.get('total_pnl_pct', 0):+.1f}%)
仓位比例: {summary.get('position_ratio', 0):.1f}%
持仓数量: {summary.get('position_count', 0)} 只（赢: {summary.get('win_count', 0)} 亏: {summary.get('loss_count', 0)}）

=== 持仓明细 ===
"""
        for p in positions:
            tech = p.get("technical", {}) or {}
            fund = p.get("fund_flow", {}) or {}
            prompt += (
                f"  {p['code']} {p['name']} | "
                f"数量:{p['quantity']:.0f}股 | "
                f"成本:{p['cost_price']:.2f} | "
                f"现价:{p['current_price']:.2f} | "
                f"盈亏:{p['pnl']:+.0f}({p['pnl_pct']:+.1f}%) | "
                f"仓位占比:{p.get('weight', 0):.0f}% | "
                f"今日:{p.get('change_pct', 0):+.1f}% | "
                f"规则止损:{p.get('stop_loss_price', 0):.2f} | "
                f"规则止盈:{p.get('take_profit_price', 0):.2f} | "
                f"保盈线:{p.get('protect_profit_price') or 'N/A'} | "
                f"规则状态:{p.get('rule_action', '')} | "
                f"技术:{tech.get('summary', 'N/A')} | "
                f"RSI:{tech.get('rsi', 'N/A')} | "
                f"主力净流:{fund.get('main_net', 0):+.0f} 主力占比:{fund.get('main_net_pct', 0):+.1f}%\n"
            )

        prompt += f"\n=== 今日市场概况 ===\n"
        prompt += f"全市场{context['market_stats']['total']}只, 涨{context['market_stats']['up']}跌{context['market_stats']['down']}, 涨停{context['market_stats']['limit_up']}跌停{context['market_stats']['limit_down']}\n"

        prompt += "\n=== 热门概念板块 TOP10 ===\n"
        for c in context.get("hot_concepts", [])[:10]:
            prompt += f"  {c['name']}: 涨幅{c['change_pct']:+.2f}% 净流入{c['main_net_inflow']:.2f}亿\n"

        prompt += "\n=== 行业板块资金流向 TOP10 ===\n"
        for s in context.get("sector_flow", [])[:10]:
            prompt += f"  {s['name']}: 涨幅{s['change_pct']:+.2f}% 净流入{s['main_net_inflow']:.2f}亿\n"

        if tail_candidates:
            prompt += "\n=== 尾盘潜伏精选候选股 ===\n"
            for tc in tail_candidates[:8]:
                prompt += f"  {tc['code']} {tc['name']}: S1={tc.get('stage1_score',0)} S2={tc.get('stage2_score',0)} 综合={tc.get('final_score',0)} | {tc.get('today_confirmation','')}\n"

        if news:
            prompt += f"\n=== 最近财经新闻（{len(news)}条） ===\n"
            for n in news[:10]:
                prompt += f"  [{n['time']}] {n['title']}\n"

        prompt += """
请基于以上数据输出“可直接执行”的简明操作摘要。

硬性要求：
- 总字数不超过 600 字。
- 不写长篇分析，不复述行情背景。
- 每只持仓最多 2 行：第一行说怎么操作，第二行解释原因。
- 原因必须引用上方提供的盈亏、技术、资金、仓位或板块数据。
- 不承诺收益，不编造公告或财务数据。

请严格按以下格式输出：

【操作摘要】
1. 代码 名称：动作（增持/持有/减仓/清仓/观察）；止损价 X；止盈价 Y；补仓条件 Z。
   原因：一句话，20-35字。

【买入建议】
- 今日是否新增：是/否。原因：一句话。
- 可观察方向/标的：最多 3 个；每个写“买入条件 / 无效条件 / 仓位上限”。

【执行顺序】
1. 最先处理什么。
2. 其次处理什么。
3. 最后观察什么。"""

        response = self._call_ai([
            {"role": "system", "content": "你是一位审慎的A股交易助理。只输出简短、可执行的操作摘要；不要长篇分析；必须明确动作、止损、止盈和原因。"},
            {"role": "user", "content": prompt},
        ], max_tokens=1200, temperature=0.2)

        return {
            "response": response,
            "context_summary": {
                "market": f"{context['market_stats']['up']}涨{context['market_stats']['down']}跌",
                "hot_concepts": [c["name"] for c in context.get("hot_concepts", [])[:5]],
                "position_count": len(positions),
                "total_pnl": summary.get("total_pnl", 0),
                "news_count": len(news),
            },
        }


if __name__ == "__main__":
    advisor = AIAdvisor()
    if not advisor.is_configured:
        print("请在 config.py 的 AI_CONFIG 中设置 DeepSeek API Key")
        print("或设置环境变量: set DEEPSEEK_API_KEY=your-key")
    else:
        print("正在获取市场数据进行AI分析...")
        result = advisor.analyze_market()
        print("\n" + "=" * 60)
        print(result["response"])
