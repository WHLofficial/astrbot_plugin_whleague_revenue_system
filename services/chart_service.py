"""展示图：轮次预告图（对阵+天气+比分）、轮次统计图（club 上座映射）、
赛季走势图（积分折线 × 场均上座柱双轴）。

风格参照 S8 计算器「展示」工作表：微软雅黑字体、薰衣草紫(#B19CD9)表头与
合计行（白字粗体）、细网格线、黑色粗体标题、千分位数字。
Pillow 懒加载：未安装时不拖垮插件初始化，命令层返回明确错误。
"""

import os
import re
from pathlib import Path

from astrbot.api import logger

from . import formula
from .tour_sync import effective_round, load_round_map

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
    # Windows（微软雅黑优先，贴近展示表风格）
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simkai.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\Dengb.ttf",
    r"C:\Windows\Fonts\msjh.ttc",
    r"C:\Windows\Fonts\msjhbd.ttc",
    r"C:\Windows\Fonts\YuGothR.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\batang.ttc",
    r"C:\Windows\Fonts\gulim.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux（Noto / 文泉驿 / Droid / 文鼎）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
]

# 常见 CJK 字体目录（目录扫描兜底用）
_SYSTEM_FONT_DIRS = [
    r"C:\Windows\Fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
]

# 文件名含这些特征视为 CJK 字体（避免误选纯拉丁字体渲染豆腐块）
_CJK_FILE_RE = re.compile(
    r"(msyh|simhei|simsun|simkai|deng|yahei|noto|cjk|wqy|zenhei|microhei|droid|"
    r"hei|song|ming|gothic|pingfang|hiragino|malgun|batang|gulim|uming|ukai)"
    r"|(arial\s*unicode)",
    re.I,
)

# 插件内置字体目录（随仓库分发，服务器零依赖兜底）
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLED_FONT_DIR = os.path.join(_PLUGIN_ROOT, "fonts")

# 图表字体解析：chart_font_path 覆盖优先（不缓存），其余路径按 need-bold 缓存
_FONT_OVERRIDE = ""
_FOUND_FONT: dict = {}


def set_font_override(path: str) -> None:
    """设置/清除图表字体路径覆盖（由 ChartService 从配置注入）。"""
    global _FONT_OVERRIDE
    _FONT_OVERRIDE = (path or "").strip()


_RESULT_CN = {"W": "胜", "D": "平", "L": "负", "C": "取消"}


def match_sort_key(m: dict) -> tuple:
    """轮次图行序键：按 开球周→日→时间 升序。

    match_time 已是零填充 HH:MM（v15 同步/迁移保证），字典序即时间序；
    缺失的周/日/时间垫底；Python 排序稳定，同键保持导入原序。
    """
    week = m.get("week_no")
    day = m.get("day_no")
    return (
        int(week) if week else 0,
        int(day) if day else 99,
        m.get("match_time") or "99:99",
    )


def build_totals_strip(total: int, avg: float) -> list[tuple[str, str, int, int]]:
    """合计行四格条带：(合计 跨周/天/星期)(总上座 居中于时间~客队)(场均 对齐球场列)(场均值)。"""
    return [
        ("合计", "label", 0, 3),
        (f"{int(total):,}", "value", 3, 8),
        ("场均", "label", 8, 9),
        (f"{float(avg):,.0f}", "value", 9, 11),
    ]

# 超采样倍率：按 2x 画布绘制再 LANCZOS 缩回设计尺寸，文字边缘更清晰
_SUPERSAMPLE = 2


class ChartError(Exception):
    pass


def _list_plugin_fonts() -> list[str]:
    """插件自带 fonts/ 目录内的字体文件（随仓库分发，保证零依赖可用）。"""
    if not os.path.isdir(_BUNDLED_FONT_DIR):
        return []
    try:
        entries = os.listdir(_BUNDLED_FONT_DIR)
    except OSError:
        return []
    return [os.path.join(_BUNDLED_FONT_DIR, n)
            for n in sorted(entries) if n.lower().endswith((".ttf", ".otf", ".ttc"))]


def _scan_system_font_dirs() -> list[str]:
    """递归扫描系统字体目录，仅收集文件名含 CJK 特征的字体。"""
    dirs = list(_SYSTEM_FONT_DIRS)
    for key in ("USERPROFILE", "HOME"):
        home = os.environ.get(key)
        if home:
            dirs.extend([os.path.join(home, ".fonts"),
                         os.path.join(home, ".local", "share", "fonts")])
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            for root, _subs, files in os.walk(d):
                for name in files:
                    if name.lower().endswith((".ttf", ".otf", ".ttc")) \
                            and _CJK_FILE_RE.search(name):
                        out.append(os.path.join(root, name))
        except OSError:
            continue
    return out


def _resolve_font_path(bold: bool) -> str:
    """四级兜底：chart_font_path → 已知系统名单 → 插件内置 fonts/ → 系统目录扫描。"""
    if _FONT_OVERRIDE and os.path.isfile(_FONT_OVERRIDE):
        return _FONT_OVERRIDE
    if bold in _FOUND_FONT:
        return _FOUND_FONT[bold]
    from PIL import ImageFont

    order = []
    if bold:
        order += [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\Dengb.ttf",
                  r"C:\Windows\Fonts\malgunbd.ttf",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]
    order += list(_FONT_CANDIDATES) + _list_plugin_fonts() + _scan_system_font_dirs()
    for p in order:
        if not os.path.isfile(p):
            continue
        try:
            ImageFont.truetype(p, 10, index=0)
        except Exception:
            continue
        _FOUND_FONT[bold] = p
        return p
    return ""


def _apply_weight(font, bold: bool) -> None:
    """内置 Noto Sans SC 为变量字体默认 Thin：粗体切 Bold、常规切 Regular（失败静默）。

    静态粗体字体（如 msyhbd.ttc）不支持 set_variation_by_name，静默跳过保持原字重。
    """
    try:
        font.set_variation_by_name("Bold" if bold else "Regular")
    except Exception:
        pass


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    path = _resolve_font_path(bold)
    if not path:
        raise ChartError(
            "未找到可用中文字体（已搜索内置字体与系统字体目录）。"
            "请安装中文字体，或在 AstrBot 插件配置中设置 chart_font_path。"
        )
    try:
        font = ImageFont.truetype(path, size, index=0)
    except Exception as e:
        raise ChartError(f"字体加载失败（{path}）：{e}") from e
    _apply_weight(font, bold)
    return font


def _text_w(dr, text: str, font) -> int:
    return int(dr.textlength(text, font=font))


def _draw_cell_text(dr, text, x, y, w, h, font, fill, align: str, scale: int = 1,
                    bold: bool = False) -> None:
    """单元格文字：文本超宽时自动缩小字号（下限 11px），仍超则截断加省略号，避免压到相邻列。"""
    inset = 12 * scale
    avail = w - 2 * inset
    bbox = dr.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    if tw > avail:
        floor = max(11 * scale, int(font.size * avail / max(1, tw)))
        if floor < font.size:
            font = _font(floor, bold=bold)
            bbox = dr.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        if tw > avail:
            s = text
            while len(s) > 1 and dr.textlength(s + "…", font=font) > avail:
                s = s[:-1]
            text = s + "…"
            bbox = dr.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    if align == "right":
        tx = x + w - inset - tw
    elif align == "center":
        tx = x + (w - tw) / 2
    else:
        tx = x + inset
    ty = y + (h - th) / 2 - bbox[1]
    dr.text((tx, ty), text, font=font, fill=fill)


def _draw_table_block(dr, y_base: int, width: int, title: str, subtitle: str,
                      headers, rows, totals_strip, header_fill, header_text,
                      row_colors=None, scale: int = 1) -> int:
    """在画布 y_base（已按 scale 缩放的设备坐标）起绘制一个表格区块，返回区块结束的 y（同坐标系）。

    headers: (文本, 列宽, 对齐 l/c/r)；totals_strip 为 None（不画合计行）或
    [(文本, kind, 起始列, 结束列)]——"label" 格填表头色白字、"value" 格白底黑粗体，
    文字均居中；条带自起始列左缘横排至结束列左缘（结束列越界取行右缘）。
    row_colors: 与 rows 同构的每格颜色（None 用默认深色）。
    """
    pad, title_h, sub_h, gap = 24 * scale, 48 * scale, 30 * scale, 8 * scale
    header_h, row_h, total_h = 46 * scale, 42 * scale, 46 * scale
    W = width * scale
    col_x = []
    x = pad
    for _, w, _ in headers:
        col_x.append(x)
        x += w * scale
    f_title = _font(32 * scale, bold=True)
    f_sub = _font(20 * scale)
    f_head = _font(22 * scale, bold=True)
    f_row = _font(22 * scale)
    f_tot = _font(22 * scale, bold=True)

    y = y_base + pad
    dr.text((pad, y), title, font=f_title, fill=TEXT_DARK)
    y += title_h
    dr.text((pad + 2 * scale, y), subtitle, font=f_sub, fill=(120, 120, 120))
    y += sub_h + gap

    dr.rectangle([pad, y, W - pad, y + header_h], fill=header_fill)
    for i, (cell, (_, w, align)) in enumerate(zip([h[0] for h in headers], headers)):
        _draw_cell_text(dr, cell, col_x[i], y, w * scale, header_h, f_head, header_text, align, scale, bold=True)
    y += header_h

    for ri, row in enumerate(rows):
        dr.rectangle([pad, y, W - pad, y + row_h], outline=GRID)
        colors = row_colors[ri] if row_colors else None
        for i, (cell, (_, w, align)) in enumerate(zip(row, headers)):
            fill = (colors[i] if colors and colors[i] else TEXT_DARK)
            _draw_cell_text(dr, cell, col_x[i], y, w * scale, row_h, f_row, fill, align, scale)
        y += row_h

    if totals_strip is not None:
        dr.line([pad, y, W - pad, y], fill=GRID, width=scale)
        n_cols = len(headers)
        for text, kind, c0, c1 in totals_strip:
            x0 = col_x[c0]
            x1 = col_x[c1] if c1 < n_cols else W - pad
            if kind == "label":
                dr.rectangle([x0, y, x1, y + total_h], fill=header_fill)
                _draw_cell_text(dr, text, x0, y, x1 - x0, total_h, f_tot,
                                header_text, "center", scale, bold=True)
            else:
                _draw_cell_text(dr, text, x0, y, x1 - x0, total_h, f_tot,
                                TEXT_DARK, "center", scale, bold=True)
        y += total_h
        dr.rectangle([pad, y, W - pad, y + 3 * scale], fill=ACCENT)
    return y


def _draw_table(title: str, subtitle: str, headers, rows, totals_strip, out_path: str,
                header_fill=HEADER_FILL, header_text=HEADER_TEXT) -> str:
    """单表格图（headers/rows: (文本, 列宽, 对齐 l/c/r)；totals_strip 见 _draw_table_block）。"""
    from PIL import Image, ImageDraw

    S = _SUPERSAMPLE
    pad, title_h, sub_h, gap = 24, 48, 30, 8
    header_h, row_h, total_h = 46, 42, 46
    width = sum(w for _, w, _ in headers) + pad * 2
    has_totals = totals_strip is not None
    height = pad + title_h + sub_h + gap + header_h + len(rows) * row_h \
        + (total_h + 3 if has_totals else 0) + pad

    img = Image.new("RGB", (width * S, height * S), BG)
    dr = ImageDraw.Draw(img)
    _draw_table_block(dr, 0, width, title, subtitle, headers, rows, totals_strip,
                      header_fill, header_text, scale=S)
    img = img.resize((width, height), Image.LANCZOS)
    img.save(out_path)
    return out_path


WEATHER_COLORS = {
    "晴": (230, 120, 30),
    "多云": (120, 120, 120),
    "雨": (70, 110, 200),
    "雪": (90, 150, 220),
}


def _round_rows(matches: list, fx_by_mid: dict, stadiums: dict) -> tuple:
    """轮次统计图行构建（纯函数）：只画已赛行，上座按 matchId 从 club fixtures 映射。

    返回 (rows, total, counted, played_count)：上座缺失显示「—」且不丢行；
    弃权行显示「弃权」不计入合计；总行数恒等于已赛行数。
    """
    played = sorted(
        (dict(m) for m in matches if m["tour_status"] == "finished"),
        key=match_sort_key,
    )
    rows, total, counted = [], 0, 0
    for m in played:
        fx = fx_by_mid.get(m["tour_match_id"]) if m["tour_match_id"] else None
        name = (fx.get("stadium_name") if fx else None) or stadiums.get(m["home_team"]) \
            or f"{m['home_team']}主场"
        head = [
            f"W{m['week_no']}" if m.get("week_no") else "",
            f"D{m['day_no']}" if m.get("day_no") else "",
            formula.weekday_name(m.get("day_no")),
            m.get("match_time") or "",
            m["weather"] or "？",
            m["home_team"],
        ]
        if m["result"] == "C":
            # 弃权：观众/上座率显示 -，不计入合计与场均
            rows.append(head + ["弃权", m["away_team"], name, "-", "-"])
            continue
        attendance = fx.get("attendance") if fx else None
        if attendance is None:
            # club 上座缺失：显示 — 不丢行，不计入合计
            rows.append(head + [m["score"] or "—", m["away_team"], name, "—", "—"])
            continue
        attendance = int(attendance)
        total += attendance
        counted += 1
        rate = "—"
        if fx and fx.get("capacity"):
            rate = f"{attendance / int(fx['capacity']) * 100:.1f}%"
        rows.append(head + [m["score"] or _RESULT_CN.get(m["result"], "?"),
                            m["away_team"], name, f"{attendance:,}", rate])
    return rows, total, counted, len(played)


def _season_points(rows: list) -> tuple[dict, int]:
    """按轮累计积分（纯函数）：主队视角 result → W3/D1/L0，客队镜像，C 不计。

    返回 ({队名: [(轮, 累计积分), ...]}, 最大轮次)；次级无带入分，各队从首轮起累计。
    """
    cum: dict[str, int] = {}
    series: dict[str, list] = {}
    max_round = 0
    for m in sorted(rows, key=lambda r: (r["round_no"], r["leg"], r["id"])):
        rnd = int(m["round_no"])
        max_round = max(max_round, rnd)
        res = m["result"]
        home, away = m["home_team"], m["away_team"]
        if res and res != "C":
            hp = {"W": 3, "D": 1, "L": 0}.get(res, 0)
            ap = {"W": 0, "D": 1, "L": 3}.get(res, 0)
            cum[home] = cum.get(home, 0) + hp
            cum[away] = cum.get(away, 0) + ap
        for team, pts in ((home, cum.get(home, 0)), (away, cum.get(away, 0))):
            series.setdefault(team, []).append((rnd, pts))
    return series, max_round


def _fs_safe(text: str) -> str:
    """competition 等外部文本入文件名：只保留词字符与连字符（\\w 含 CJK）。"""
    return re.sub(r"[^\w-]+", "_", str(text)).strip("_")[:24] or "x"


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

    S = _SUPERSAMPLE
    W, H = 1180 * S, 640 * S
    margin_t, margin_b = 96 * S, 64 * S
    names = [n for n, _ in series]
    legend_cols = 2 if len(series) > 14 else 1
    per_col = math.ceil(len(series) / legend_cols)
    legend_row_h = 28 * S
    name_w = (max(len(n) for n in names) if names else 4) * 24 * S
    legend_pitch = name_w + 60 * S
    # 图例按实际列数预留宽度：双列时若仍按单列上限预留，第二列会画出画布
    margin_r = min(legend_cols * legend_pitch + 36 * S, 870 * S)
    margin_l = 110 * S
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
    f_title = _font(30 * S, bold=True)
    f_axis = _font(20 * S)

    # 网格与 y 轴刻度
    for i in range(6):
        yy = margin_t + plot_h * i / 5
        val = y_top * (5 - i) / 5
        dr.line([margin_l, yy, W - margin_r, yy], fill=GRID, width=S)
        tick = _fmt_k(int(round(val)))
        tw = _text_w(dr, tick, f_axis)
        dr.text((margin_l - 12 * S - tw, yy - 13 * S), tick, font=f_axis, fill=(110, 110, 110))

    # x 轴刻度
    step = max(1, (x_max - x_min + 1) // 10)
    for xv in range(x_min, x_max + 1, step):
        xx = sx(xv)
        t = str(xv)
        tw = _text_w(dr, t, f_axis)
        dr.text((xx - tw / 2, margin_t + plot_h + 8 * S), t, font=f_axis, fill=(110, 110, 110))
    dr.line([margin_l, margin_t + plot_h, W - margin_r, margin_t + plot_h], fill=TEXT_DARK, width=2 * S)

    # 折线
    for idx, (name, pts) in enumerate(series):
        color = PALETTE[idx % len(PALETTE)]
        coords = [(sx(x), sy(y)) for x, y in sorted(pts)]
        dr.line(coords, fill=color, width=4 * S)
        for cx, cy in coords:
            dr.ellipse([cx - 5 * S, cy - 5 * S, cx + 5 * S, cy + 5 * S], fill=color, outline=BG)

    # 图例（右侧，最多两列）
    lx = W - margin_r + 16 * S
    for i, name in enumerate(names):
        col = i // per_col
        row = i % per_col
        ly = margin_t - 20 * S + row * legend_row_h
        px = lx + col * legend_pitch
        color = PALETTE[i % len(PALETTE)]
        dr.rectangle([px, ly + 4 * S, px + 16 * S, ly + 20 * S], fill=color)
        dr.text((px + 24 * S, ly), name, font=f_axis, fill=TEXT_DARK)

    # 轴标签与标题
    dr.text((margin_l - 10 * S, H - 34 * S), x_label, font=f_axis, fill=(110, 110, 110))
    dr.text((16 * S, margin_t + plot_h // 2), y_label, font=f_axis, fill=(110, 110, 110))

    dr.text((margin_l, 24 * S), title, font=f_title, fill=TEXT_DARK)
    dr.text((margin_l, 64 * S), subtitle, font=f_axis, fill=(120, 120, 120))

    img = img.resize((W // S, H // S), Image.LANCZOS)
    img.save(out_path)
    return out_path


def _draw_season_chart(title: str, subtitle: str, series, bars: dict,
                       out_path: str, max_round: int, note: str | None = None) -> str:
    """赛季走势双轴图：左轴按轮累计积分折线（series: [(队名, [(轮, 累计), ...])]），
    右轴当轮场均上座柱（bars: {轮: 场均}，缺失轮为 0）。"""
    import math

    from PIL import Image, ImageDraw

    S = _SUPERSAMPLE
    W, H = 1180 * S, 640 * S
    margin_t, margin_b = 96 * S, 64 * S
    names = [n for n, _ in series]
    legend_cols = 2 if len(series) > 14 else 1
    per_col = math.ceil(len(series) / legend_cols) if series else 1
    legend_row_h = 28 * S
    name_w = (max((len(n) for n in names), default=4)) * 24 * S
    legend_pitch = name_w + 60 * S
    margin_r = min(legend_cols * legend_pitch + 80 * S, 900 * S)
    margin_l = 90 * S
    plot_w = W - margin_l - margin_r
    plot_h = H - margin_t - margin_b

    x_max = max(1, max_round)
    max_pts = max((max(p for _, p in pts) for _, pts in series), default=3)
    pts_top = _nice_ceil(max(max_pts, 3))
    max_bar = max((int(v) for v in bars.values()), default=0)
    bar_top = _nice_ceil(max_bar)

    def sx(rnd):
        return margin_l + (rnd - 1) / max(1, x_max - 1) * plot_w

    def sy_pts(v):
        return margin_t + plot_h - v / pts_top * plot_h

    def sy_bar(v):
        return margin_t + plot_h - v / bar_top * plot_h

    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    f_title = _font(30 * S, bold=True)
    f_axis = _font(20 * S)

    # 柱（当轮场均上座，右轴）：先画柱再画线，线在上层
    bw = plot_w / max(1, x_max) * 0.55
    base = margin_t + plot_h
    for rnd, val in sorted(bars.items()):
        if val <= 0:
            continue
        x0 = sx(rnd) - bw / 2
        y0 = sy_bar(val)
        dr.rectangle([x0, y0, x0 + bw, base], fill=(206, 196, 226))

    # 左轴刻度（积分）
    for i in range(6):
        yy = margin_t + plot_h * i / 5
        val = pts_top * (5 - i) / 5
        dr.line([margin_l, yy, W - margin_r, yy], fill=GRID, width=S)
        tick = str(int(round(val)))
        tw = _text_w(dr, tick, f_axis)
        dr.text((margin_l - 12 * S - tw, yy - 13 * S), tick, font=f_axis, fill=(110, 110, 110))
    # 右轴刻度（场均上座）
    for i in range(6):
        yy = margin_t + plot_h * i / 5
        val = bar_top * (5 - i) / 5
        tick = _fmt_k(int(round(val)))
        tw = _text_w(dr, tick, f_axis)
        dr.text((W - margin_r + 10 * S, yy - 13 * S), tick, font=f_axis, fill=(130, 110, 170))

    # x 轴刻度（轮次）
    step = max(1, (x_max + 1) // 10)
    for xv in range(1, x_max + 1, step):
        xx = sx(xv)
        t = str(xv)
        tw = _text_w(dr, t, f_axis)
        dr.text((xx - tw / 2, margin_t + plot_h + 8 * S), t, font=f_axis, fill=(110, 110, 110))
    dr.line([margin_l, base, W - margin_r, base], fill=TEXT_DARK, width=2 * S)

    # 积分折线（每队一条，从第 1 轮起累计）
    for idx, (name, pts) in enumerate(series):
        color = PALETTE[idx % len(PALETTE)]
        coords = [(sx(x), sy_pts(p)) for x, p in sorted(pts)]
        dr.line(coords, fill=color, width=4 * S)
        for cx, cy in coords:
            dr.ellipse([cx - 5 * S, cy - 5 * S, cx + 5 * S, cy + 5 * S], fill=color, outline=BG)

    # 图例（右侧，最多两列）
    lx = W - margin_r + 60 * S
    for i, name in enumerate(names):
        col = i // per_col
        row = i % per_col
        ly = margin_t - 20 * S + row * legend_row_h
        px = lx + col * legend_pitch
        color = PALETTE[i % len(PALETTE)]
        dr.rectangle([px, ly + 4 * S, px + 16 * S, ly + 20 * S], fill=color)
        dr.text((px + 24 * S, ly), name, font=f_axis, fill=TEXT_DARK)

    # 轴标签/标题/副标题/标注
    dr.text((margin_l - 10 * S, H - 34 * S), "轮次", font=f_axis, fill=(110, 110, 110))
    dr.text((16 * S, margin_t + plot_h // 2), "累计积分", font=f_axis, fill=(110, 110, 110))
    dr.text((W - margin_r + 10 * S, margin_t - 26 * S), "场均上座", font=f_axis, fill=(130, 110, 170))
    dr.text((margin_l, 24 * S), title, font=f_title, fill=TEXT_DARK)
    dr.text((margin_l, 64 * S), subtitle, font=f_axis, fill=(120, 120, 120))
    if note:
        nw = _text_w(dr, note, f_axis)
        dr.text((W - margin_r - nw - 8 * S, 64 * S), note, font=f_axis, fill=(200, 90, 40))

    img = img.resize((W // S, H // S), Image.LANCZOS)
    img.save(out_path)
    return out_path


class ChartService:
    def __init__(self, db, dao, cfg):
        self._db = db
        self._dao = dao
        self._cfg = cfg
        set_font_override(cfg.get("chart_font_path"))

    @property
    def charts_dir(self) -> Path:
        d = Path(self._db.db_path).parent / "charts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cleanup_oldest(self, keep: int = 50) -> int:
        entries = []
        for p in self.charts_dir.glob("*.png"):
            try:
                if p.is_file():
                    entries.append((p.stat().st_mtime, p))
            except OSError:
                continue  # 并发下文件可能在列出与 stat 之间消失
        entries.sort(key=lambda t: t[0])
        files = [p for _, p in entries]
        removed = 0
        while len(files) > keep:
            old = files.pop(0)
            try:
                old.unlink()
                removed += 1
            except OSError as e:
                logger.warning(f"Failed to remove old chart {old}: {e}")
        return removed

    # ─── 轮次查询（v15：无 window 过滤，直查赛季+轮次+赛事） ──

    async def _season(self) -> int:
        state = await self._dao.get_league_state()
        if state is None or state["season_number"] is None:
            raise ChartError("无法确定当前赛季（本地 league_state 为空，先执行 /主场赛程同步）")
        return int(state["season_number"])

    async def _round_matches(self, season: int, round_no: int,
                             competition: str | None) -> list:
        matches = await self._dao.get_round_matches(season, round_no, competition)
        if not matches:
            raise ChartError(f"第 {round_no} 轮({competition or '全部赛事'})还没有赛程")
        return matches

    async def _fixtures_for(self, matches: list, round_no: int) -> dict:
        """按行上的 tournament_id 拉一轮 club 上座/球场，{matchId: fixture} 映射。

        club /api/fixtures 按 tour stage 内轮次（m.round）定位，本地 round_no 带
        stage offset（次级 15+/冠军杯 7+），先经 tour_round_map 换算再拉。
        无 tour_match_id 或 club 不可用时返回空映射（展示「—」，不阻塞出图）。
        """
        from .club_sync import ClubSyncClient
        tids = {int(m["tournament_id"]) for m in matches if m["tournament_id"] is not None}
        if not tids:
            return {}
        round_map = await load_round_map(self._dao)
        client = ClubSyncClient(self._cfg)
        fx_by_mid: dict = {}
        for tid in tids:
            for f in await client.fetch_fixtures(tid, effective_round(round_map, tid, round_no)):
                fx_by_mid[f["matchId"]] = f
        return fx_by_mid

    async def _stadium_names(self) -> dict:
        """本地球场名映射（club fixtures 缺球场名时回落）。"""
        return {s["team_name"]: s["name"] for s in await self._dao.list_stadiums()}

    async def render_round_chart(self, round_no: int, competition: str | None = None) -> str:
        """轮次统计图：本地已赛行 + club fixtures 按 matchId 映射上座。

        上座缺失显示「—」且不丢行；取消行显示「取消」不计入合计；
        全部未赛抛 ChartError「还没有赛果」；不按 window 过滤。
        """
        season = await self._season()
        matches = await self._round_matches(season, round_no, competition)
        fx_by_mid = await self._fixtures_for(matches, round_no)
        stadiums = await self._stadium_names()
        comp_label = competition or "全部赛事"

        rows, total, counted, played_count = _round_rows(matches, fx_by_mid, stadiums)
        if played_count == 0:
            raise ChartError(f"第 {round_no} 轮({competition or '全部赛事'})还没有赛果")
        avg = total / counted if counted else 0
        season_label = await self._dao.season_label(season)
        subtitle = f"{season_label} · 共 {played_count} 场"
        title = f"WHL {season_label} {comp_label}现场观众统计（第{round_no}轮）"
        header_fill, header_text = COMPETITION_COLORS.get(competition, DEFAULT_COMPETITION_COLOR)
        headers = [
            ("周", 90, "c"), ("天", 70, "c"), ("星期", 80, "c"), ("时间", 110, "c"),
            ("天气", 90, "c"), ("主队", 180, "l"), ("比分", 130, "c"),
            ("客队", 180, "l"), ("球场", 250, "l"),
            ("观众人数", 140, "r"), ("上座率", 110, "r"),
        ]
        totals_strip = build_totals_strip(total, avg)
        out = self.charts_dir / f"round_s{season}_{_fs_safe(comp_label)}_r{round_no}.png"
        try:
            _draw_table(title, subtitle, headers, rows, totals_strip, str(out),
                        header_fill=header_fill, header_text=header_text)
        except ChartError:
            raise
        except Exception as e:
            logger.error(f"Round chart render error: {e}")
            raise ChartError("图表生成失败，已记录错误")
        self._cleanup_oldest()
        return str(out)

    # ─── 轮次对阵 + 天气预报合成图（上半对阵、下半天气） ─────

    async def render_round_preview_chart(self, round_no: int, competition: str | None = None) -> str:
        """轮次预告图：本地 matches+stadium，不调 club fixtures；已赛行追加比分列。"""
        season = await self._season()
        matches = await self._round_matches(season, round_no, competition)
        matches = sorted((dict(m) for m in matches), key=match_sort_key)
        header_fill, header_text = COMPETITION_COLORS.get(competition, DEFAULT_COMPETITION_COLOR)
        comp_label = competition or "全部赛事"
        stadiums = await self._stadium_names()

        sched_rows, wx_rows, wx_colors = [], [], []
        for m in matches:
            name = stadiums.get(m["home_team"]) or f"{m['home_team']}主场"
            sched_rows.append([
                f"W{m['week_no']}" if m.get("week_no") else "",
                f"D{m['day_no']}" if m.get("day_no") else "",
                formula.weekday_name(m.get("day_no")),
                m.get("match_time") or "",
                m["home_team"], m["away_team"], name,
                m["score"] if m["tour_status"] == "finished" else "",
            ])
            weather = m["weather"] or "待预报"
            wx_rows.append([m.get("match_time") or "", m["home_team"], m["away_team"], weather])
            color = WEATHER_COLORS.get(m["weather"]) if m["weather"] else (150, 150, 150)
            wx_colors.append([None, None, None, color])

        sched_headers = [
            ("周", 90, "c"), ("天", 70, "c"), ("星期", 80, "c"), ("时间", 110, "c"),
            ("主队", 180, "l"), ("客队", 180, "l"), ("球场", 240, "l"), ("比分", 110, "c"),
        ]
        wx_headers = [
            ("时间", 110, "c"), ("主队", 200, "l"), ("客队", 200, "l"), ("天气", 200, "c"),
        ]
        pad, title_h, sub_h, gap = 24, 48, 30, 8
        header_h, row_h = 46, 42
        n = len(matches)
        width = max(
            sum(w for _, w, _ in sched_headers) + pad * 2,
            sum(w for _, w, _ in wx_headers) + pad * 2,
        )
        # 高度按实际绘制范围计算（两区块 + 间隙），不额外留底
        content = pad + title_h + sub_h + gap + header_h + n * row_h
        height = content + 10 + content

        from PIL import Image, ImageDraw

        S = _SUPERSAMPLE
        img = Image.new("RGB", (width * S, height * S), BG)
        dr = ImageDraw.Draw(img)
        season_label = await self._dao.season_label(season)
        title1 = f"WHL {season_label} {comp_label} 第{round_no}轮 对阵表"
        subtitle1 = f"{season_label} · 共 {n} 场"
        y = _draw_table_block(dr, 0, width, title1, subtitle1, sched_headers,
                              sched_rows, None, header_fill, header_text, scale=S)
        title2 = "天气预报"
        subtitle2 = f"第 {round_no} 轮({comp_label}) · 未预报显示灰色「待预报」"
        _draw_table_block(dr, y + 10 * S, width, title2, subtitle2, wx_headers,
                          wx_rows, None, header_fill, header_text, row_colors=wx_colors, scale=S)
        img = img.resize((width, height), Image.LANCZOS)

        out = self.charts_dir / f"preview_s{season}_{_fs_safe(comp_label)}_r{round_no}.png"
        try:
            img.save(out)
        except ChartError:
            raise
        except Exception as e:
            logger.error(f"Preview chart render error: {e}")
            raise ChartError("图表生成失败，已记录错误")
        self._cleanup_oldest()
        return str(out)

    # ─── 赛季走势图（积分双轴） ───────────────────────────

    async def render_season_chart(self, competition: str) -> str:
        """赛季走势图：仅顶级/次级；左轴按轮累计积分（W=3/D=1/L=0，C 不计），
        右轴当轮联赛场均上座柱（club fixtures，缺失轮柱为 0）。"""
        if competition not in ("顶级联赛", "次级联赛"):
            raise ChartError("赛季走势图仅支持 顶级/次级 联赛（冠军杯无积分走势）")
        season = await self._season()
        all_rows = await self._dao.list_matches_by_season(season)
        # 库内文本等价集匹配：v2 迁移行「联赛」= v3「顶级联赛」（competition_texts）
        comp_texts = set(formula.competition_texts(competition))
        rows = [dict(m) for m in all_rows if m["competition"] in comp_texts]
        if not rows:
            raise ChartError(f"{season} 赛季 {competition} 还没有赛程，先执行 /主场赛程同步")

        series, max_round = _season_points(rows)
        if not series:
            raise ChartError(f"{season} 赛季 {competition} 还没有赛果")

        # 当轮场均上座柱：club fixtures 每轮一次（fetch_fixtures 内 10 分钟缓存）；
        # 拉取轮次经 tour_round_map 换算成 tour stage 内轮次（club 的定位键），
        # 且只统计 matchId 命中本轮本地行的场次——换算后 base 轮 3 与 final 轮 3
        # 都拉 (tid,3)，不按本轮行过滤会把另一阶段的同轮场次混进场均
        from .club_sync import ClubSyncClient
        tids = {int(m["tournament_id"]) for m in rows if m["tournament_id"] is not None}
        tmid_by_round: dict[int, set] = {}
        for m in rows:
            if m["tour_match_id"] is not None:
                tmid_by_round.setdefault(int(m["round_no"]), set()).add(int(m["tour_match_id"]))
        round_map = await load_round_map(self._dao)
        client = ClubSyncClient(self._cfg)
        bars: dict[int, float] = {}
        for rnd in range(1, max_round + 1):
            ids = tmid_by_round.get(rnd) or set()
            atts = []
            for tid in tids:
                for f in await client.fetch_fixtures(
                    tid, effective_round(round_map, tid, rnd)
                ):
                    if f.get("attendance") is None:
                        continue
                    if ids and f["matchId"] not in ids:
                        continue
                    atts.append(int(f["attendance"]))
            bars[rnd] = sum(atts) / len(atts) if atts else 0

        season_label = await self._dao.season_label(season)
        title = f"WHL {season_label} {competition} 积分走势 × 场均上座"
        note = "次级联赛积分不含带入分，从 0 起累计" if competition == "次级联赛" else None
        out = self.charts_dir / f"season_s{season}_{_fs_safe(competition)}.png"
        try:
            _draw_season_chart(
                title,
                f"{season_label} · 左轴按轮累计积分（W=3/D=1/L=0，弃权不计）｜右轴当轮场均上座",
                [(name, pts) for name, pts in sorted(series.items(), key=lambda kv: -kv[1][-1][1])],
                bars,
                str(out),
                max_round=max_round,
                note=note,
            )
        except ChartError:
            raise
        except Exception as e:
            logger.error(f"Season chart render error: {e}")
            raise ChartError("图表生成失败，已记录错误")
        self._cleanup_oldest()
        return str(out)