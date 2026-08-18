# 内置字体说明

`fonts/` 目录存放插件随附的中文字体，用于图表渲染，保证任意云服务器/容器**零依赖**即可绘制中文。

- `NotoSansSC-Regular.ttf`：**Noto Sans SC**（Google）变量字体，绘制时按字重轴取 Regular（400）。
  - 协议：SIL Open Font License 1.1（OFL）——允许随软件再分发，署名见字体内置信息。
  - 来源：Google Fonts / notofonts 官方仓库，`feedback.googlefonts.com` 与 GitHub `google/fonts` 分发本文件的构建。

管理员也可向此目录放入任意中文字体文件（`.ttf/.otf/.ttc`），插件会自动扫描使用；
或通过 `/主场设置 chart_font_path` 指定独立字体文件。
