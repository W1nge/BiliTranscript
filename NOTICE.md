# Notices

BiliTranscript 的提取顺序、B站公开视频/字幕/音频接口处理方式，以及“让专用登录页面自行请求 AI 字幕、不复制 Cookie”的设计，参考了 MIT 许可的 **Bili Note** skill。独立 Edge / Chrome / Brave 启动器和原生 CDP 客户端由本项目实现，不依赖 Codex 运行环境。

- Bili Note copyright: Copyright (c) 2026 Bili Note contributors
- Bili Note license: MIT
- Local source used during development: `~/.codex/skills/bili-note`

BiliTranscript 是非官方工具，不隶属于哔哩哔哩。Bilibili、哔哩哔哩及相关标识归其权利人所有。

Windows 安装包使用 Inno Setup 构建。`installer/Languages/ChineseSimplified.isl` 是 Inno Setup 官方维护的简体中文翻译，保留了原文件中的维护者声明。

- Inno Setup source: https://github.com/jrsoftware/issrc
- Chinese translation source blob: `0fced9759dd20e250101768985af511153133cdc`

Windows Acrylic 背景桥接层使用 Microsoft Win2D 1.28.3，并随程序分发 Microsoft VCRT 140 App-Local DLL Forwarders 1.1.0。它们只负责 Windows Composition 的 GPU 合成效果，不参与视频或文稿处理。

- Win2D project: https://github.com/microsoft/Win2D
- Win2D license: https://www.microsoft.com/web/webpi/eula/eula_win2d_10012014.htm
- VCRT Forwarders project and license: https://github.com/microsoft/vcrt-forwarders
