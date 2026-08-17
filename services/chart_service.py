"""比赛统计图：轮次上座表格图 + 赛季上座走势图。

风格参照 S8 计算器「展示」工作表：微软雅黑字体、薰衣草紫(#B19CD9)表头与
合计行（白字粗体）、细网格线、黑色粗体标题、千分位数字。
Pillow 懒加载：未安装时不拖垮插件初始化，命令层返回明确错误。
"""

import os
from pathlib import Path

from astrbot.api import logger

from . import formula

# 展示工作表配色
HEADER_FILL = (177, 156, 217)
HEADER_TEXT = (249, 247, 255)
TEXT_DARK = (20, 20, 20)
GRID = (205, 205, 205)
BG = (255, 255, 255)
ACCENT = (148, 120, 208)

# 赛事配色（参照展示工作表三节）：
# 次级联赛=薰衣草紫 #B19CD9｜顶级联赛=橙 #E97132｜其余（联赛/冠军杯/未知）=绿 #5CB48E
COMPETITION_COLORS = {
    "次级联赛": ((177, 156, 217), (249, 247, 255)),
    "顶级联赛": ((233, 113, 50), (255, 248, 245)),
}
DEFAULT_COMPETITION_COLOR = ((92, 180, 142), (245, 255, 252))

PALETTE = [
    (78, 121, 167), (242, 142, 43), (225, 87, 89), (118, 183, 178),
    (89, 161, 79), (237, 201, 72), (176, 122, 161), (255, 157, 167),
    (156, 95, 89), (186, 176, 172),
]

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simkai.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

_RESULT_CN = {"W": "胜", "D": "平", "L": "负"}


class ChartError(Exception):
    pass


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = _FONT_CANDIDATES
    if bold:
        candidates = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"] + candidates
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size, index=0)
            except Exception:
                continue
    raise ChartError("未找到中文字体，无法生成图表")


def _text_w(dr, text: str, font) -> int:
    return int(dr.textlength(text, font=font))


def _draw_cell_text(dr, text, x, y, w, h, font, fill, align: str) -> None:
    bbox = dr.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    if align == "right":
        tx = x + w - 12 - tw
    elif align == "center":
        tx = x + (w - tw) / 2
    else:
        tx = x + 12
    ty = y + (h - th) / 2 - bbox[1]
    dr.text((tx, ty), text, font=font, fill=fill)


def _draw_table(title: str, subtitle: str, headers, rows, totals, out_path: str,
                header_fill=HEADER_FILL, header_text=HEADER_TEXT) -> str:
    """headers/rows/totals: 每格为 (文本, 列宽, 对齐 l/c/r)。"""
    from PIL import Image, ImageDraw

    pad = 24
    title_h, sub_h, gap = 48, 30, 8
    header_h, row_h, total_h = 46, 42, 46
    col_x = []
    x = pad
    for _, w, _ in headers:
        col_x.append(x)
        x += w
    width = x + pad
    height = pad + title_h + sub_h + gap + header_h + len(rows) * row_h + total_h + pad

    img = Image.new("RGB", (width, height), BG)
    dr = ImageDraw.Draw(img)
    f_title = _font(32, bold=True)
    f_sub = _font(20)
    f_head = _font(22, bold=True)
    f_row = _font(22)
    f_tot = _font(22, bold=True)

    y = pad
    dr.text((pad, y), title, font=f_title, fill=TEXT_DARK)
    y += title_h
    dr.text((pad + 2, y), subtitle, font=f_sub, fill=(120, 120, 120))
    y += sub_h + gap

    # 表头（赛事底色白字粗体）
    dr.rectangle([pad, y, width - pad, y + header_h], fill=header_fill)
    for i, (cell, (_, w, align)) in enumerate(zip([h[0] for h in headers], headers)):
        _draw_cell_text(dr, cell, col_x[i], y, w, header_h, f_head, header_text, align)
    y += header_h

    # 数据行（细网格线）
    for row in rows:
        dr.rectangle([pad, y, width - pad, y + row_h], outline=GRID)
        for i, (cell, (_, w, align)) in enumerate(zip(row, headers)):
            _draw_cell_text(dr, cell, col_x[i], y, w, row_h, f_row, TEXT_DARK, align)
        y += row_h

    # 合计行（赛事底色白字粗体）
    dr.rectangle([pad, y, width - pad, y + total_h], fill=header_fill)
    for i, (cell, (_, w, align)) in enumerate(zip(totals, headers)):
        _draw_cell_text(dr, cell, col_x[i], y, w, total_h, f_tot, header_text, align)
    y += total_h
    dr.rectangle([pad, y, width - pad, y + 3], fill=ACCENT)

    img.save(out_path)
    return out_path


def _fmt_k(v: int) -> str:
    if v >= 1000:
        return f"{v / 1000:g}k"
    return str(v)


def _nice_ceil(v: int) -> int:
    v = int(v)
    if v <= 0:
        return 10000
    mag = 10 ** (len(str(v)) - 1)
    for m in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0):
        if v <= m * mag:
            return int(m * mag)
    return int(10 * mag)


def _draw_trend(title: str, subtitle: str, series, out_path: str,
                x_label: str = "主场场次", y_label: str = "观众人数") -> str:
    """series: [(队名, [(x, y), ...]), ...]，按出场顺序绘制折线。"""
    import math

    from PIL import Image, ImageDraw

    W, H = 1180, 640
    margin_t, margin_b = 96, 64
    names = [n for n, _ in series]
    legend_cols = 2 if len(series) > 14 else 1
    per_col = math.ceil(len(series) / legend_cols)
    legend_row_h = 28
    margin_r = min(340, 70 + max(len(n) for n in names) * 24 + 20)
    margin_l = 110
    plot_w = W - margin_l - margin_r
    plot_h = H - margin_t - margin_b

    max_x = max((max(x for x, _ in pts) for _, pts in series), default=1)
    max_y = max((max(y for _, y in pts) for _, pts in series), default=1000)
    y_top = _nice_ceil(max_y)
    x_min, x_max = 1, max(1, max_x)

    def sx(xv):
        return margin_l + (xv - x_min) / max(1, x_max - x_min) * plot_w

    def sy(yv):
        return margin_t + plot_h - yv / y_top * plot_h

    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    f_title = _font(30, bold=True)
    f_axis = _font(20)

    # 网格与 y 轴刻度
    for i in range(6):
        yy = margin_t + plot_h * i / 5
        val = y_top * (5 - i) / 5
        dr.line([margin_l, yy, W - margin_r, yy], fill=GRID, width=1)
        tick = _fmt_k(int(round(val)))
        tw = _text_w(dr, tick, f_axis)
        dr.text((margin_l - 12 - tw, yy - 13), tick, font=f_axis, fill=(110, 110, 110))

    # x 轴刻度
    step = max(1, (x_max - x_min + 1) // 10)
    for xv in range(x_min, x_max + 1, step):
        xx = sx(xv)
        t = str(xv)
        tw = _text_w(dr, t, f_axis)
        dr.text((xx - tw / 2, margin_t + plot_h + 8), t, font=f_axis, fill=(110, 110, 110))
    dr.line([margin_l, margin_t + plot_h, W - margin_r, margin_t + plot_h], fill=TEXT_DARK, width=2)

    # 折线
    for idx, (name, pts) in enumerate(series):
        color = PALETTE[idx % len(PALETTE)]
        coords = [(sx(x), sy(y)) for x, y in sorted(pts)]
        dr.line(coords, fill=color, width=4)
        for cx, cy in coords:
            dr.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=color, outline=BG)

    # 图例（右侧，最多两列）
    lx = W - margin_r + 16
    for i, name in enumerate(names):
        col = i // per_col
        row = i % per_col
        ly = margin_t - 20 + row * legend_row_h
        px = lx + col * (max(len(n) for n in names) * 24 + 60)
        color = PALETTE[i % len(PALETTE)]
        dr.rectangle([px, ly + 4, px + 16, ly + 20], fill=color)
        dr.text((px + 24, ly), name, font=f_axis, fill=TEXT_DARK)

    # 轴标签与标题
    dr.text((margin_l - 10, H - 34), x_label, font=f_axis, fill=(110, 110, 110))
    dr.text((16, margin_t + plot_h // 2), y_label, font=f_axis, fill=(110, 110, 110))

    dr.text((margin_l, 24), title, font=f_title, fill=TEXT_DARK)
    dr.text((margin_l, 64), subtitle, font=f_axis, fill=(120, 120, 120))

    img.save(out_path)
    return out_path


class ChartService:
    def __init__(self, db, dao, cfg):
        self._db = db
        self._dao = dao
        self._cfg = cfg

    @property
    def charts_dir(self) -> Path:
        d = Path(self._db.db_path).parent / "charts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cleanup_oldest(self, keep: int = 50) -> int:
        files = sorted(
            (p for p in self.charts_dir.glob("*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        removed = 0
        while len(files) > keep:
            old = files.pop(0)
            try:
                old.unlink()
                removed += 1
            except OSError as e:
                logger.warning(f"Failed to remove old chart {old}: {e}")
        return removed

    # ─── 轮次表格图（展示工作表风格） ─────────────────────

    async def render_round_chart(self, round_no: int, competition: str = "联赛") -> str:
        state = await self._dao.get_league_state()
        season = state["season_number"] if state else 1
        window_seq = state["window_seq"] if state else 1
        matches = await self._dao.get_round_matches(season, window_seq, round_no, competition)
        played = [m for m in matches if m["attendance"] is not None]
        if not played:
            raise ChartError(f"第 {round_no} 轮({competition})还没有已录入赛果的比赛")
        rows, total = [], 0
        for raw in played:
            m = dict(raw)
            st = await self._dao.get_stadium(m["home_team"])
            capacity = int(st["capacity"]) if st else 0
            name = st["name"] if st else f"{m['home_team']}主场"
            total += int(m["attendance"])
            rate = f"{int(m['attendance']) / capacity * 100:.1f}%" if capacity else "—"
            result_cn = _RESULT_CN.get(m["result"], "?")
            score_text = f"{m['score']} {result_cn}" if m.get("score") else result_cn
            rows.append([
                f"W{m['week_no']}" if m.get("week_no") else "",
                f"D{m['day_no']}" if m.get("day_no") else "",
                formula.weekday_name(m.get("day_no")),
                m.get("match_time") or "",
                m["weather"] or "？",
                m["home_team"],
                score_text,
                m["away_team"],
                name,
                f"{int(m['attendance']):,}",
                rate,
            ])
        avg = total / len(played)
        subtitle = f"第 {season} 赛季 · 窗口 {window_seq} · 共 {len(played)} 场"
        title = f"WHL 第{season}赛季{competition}现场观众统计（第{round_no}轮）"
        header_fill, header_text = COMPETITION_COLORS.get(competition, DEFAULT_COMPETITION_COLOR)
        headers = [
            ("周", 90, "c"), ("天", 70, "c"), ("星期", 80, "c"), ("时间", 110, "c"),
            ("天气", 90, "c"), ("主队", 180, "l"), ("比分", 130, "c"),
            ("客队", 180, "l"), ("球场", 250, "l"),
            ("观众人数", 140, "r"), ("上座率", 110, "r"),
        ]
        totals = ["合计", "", "", "", "", "", "", "", "", f"{total:,}", f"场均 {avg:,.0f}"]
        out = self.charts_dir / f"round_s{season}_w{window_seq}_{competition}_r{round_no}.png"
        try:
            _draw_table(title, subtitle, headers, rows, totals, str(out),
                        header_fill=header_fill, header_text=header_text)
        except ChartError:
            raise
        except Exception as e:
            logger.error(f"Round chart render error: {e}")
            raise ChartError("图表生成失败，已记录错误")
        self._cleanup_oldest()
        return str(out)

    # ─── 赛季走势图 ───────────────────────────────────────

    async def render_season_chart(self) -> str:
        state = await self._dao.get_league_state()
        season = state["season_number"] if state else 1
        stadiums = await self._dao.list_stadiums()
        series = []
        for s in stadiums:
            matches = await self._dao.get_home_matches_season(s["team_name"], season)
            played = [m for m in matches if m["attendance"] is not None]
            if played:
                pts = [(i + 1, int(m["attendance"])) for i, m in enumerate(played)]
                series.append((s["team_name"], pts))
        if not series:
            raise ChartError("本赛季还没有已录赛果的上座数据")
        series.sort(key=lambda item: sum(y for _, y in item[1]) / len(item[1]), reverse=True)
        title = f"WHL 第 {season} 赛季 各队主场观众走势"
        subtitle = "横轴为球队本赛季按顺序的第几次主场 · 图例按场均上座排序"
        out = self.charts_dir / f"season_s{season}_trend.png"
        try:
            _draw_trend(title, subtitle, series, str(out))
        except ChartError:
            raise
        except Exception as e:
            logger.error(f"Season chart render error: {e}")
            raise ChartError("图表生成失败，已记录错误")
        self._cleanup_oldest()
        return str(out)