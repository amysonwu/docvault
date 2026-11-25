# 中文字体目录（可选，但强烈建议配置）

OnlyOffice 官方镜像只自带西文字体。**不配置本目录时系统照常运行**，
但中文文档转 PDF 预览会出现掉字、字形回退（宋体变黑体、标点错位）。

## 配置方法

1. 下载任一分发版中文字体（.ttf / .otf / .ttc 均可），放入本目录，例如：

   - 思源黑体 / 思源宋体（开源，SIL OFL 许可）：https://github.com/adobe-fonts
   - Noto Sans CJK / Noto Serif CJK（同源，Google 发行）

2. 重启文档服务器使字体生效：

   ```bash
   docker compose restart oo
   ```

3. 首次启动后字体文件已挂载进容器的
   `/usr/share/fonts/truetype/custom`，OnlyOffice 会在转换时自动使用。

## 许可提醒

请只放入**允许再分发的字体**（思源 / Noto 系列为 SIL OFL，可自由使用）。
Windows 自带的中宋、仿宋等是微软商业字体，禁止随开源项目分发，
请自行在本机获取后使用 `FONT_DIR` 环境变量指向其它目录：

```bash
FONT_DIR=/path/to/my/fonts docker compose up -d
```
