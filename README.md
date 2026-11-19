# 压缩包阅读助手

让普通成员通过专用工具查看自己在当前会话发送的 ZIP 压缩包，无需命令执行权限。

发送 ZIP 文件后，请机器人查看压缩包；群聊需要 @机器人。多个压缩包可以连续发送后统一要求总结。

支持查看 ZIP 目录及其中的文本文件。传统加密 ZIP 可提供密码，AES ZIP 暂不支持。插件不会处理单独发送的 TXT、代码、图片或其他非 ZIP 文件。

## 配置

插件设置只有三项：单个压缩包大小、workspace 总容量、保留时间。

附件 workspace 位于 `data/plugin_data/astrbot_plugin_attachment_reader/cache/`。每个附件使用独立目录，原包保存在 `original.blob`，ZIP 中按需读取的文件流式解压到 `extracted/`。原包和解压文件都计入 workspace 容量，到期后一起删除。

读写使用小块流处理，内存仅保留数据块和当前发给模型的一页文本；不执行附件中的程序或脚本。重启或重载插件后，需要重新发送文件。

## 安装

要求 AstrBot 4.29.x 和支持工具调用的模型。在插件管理页上传插件 ZIP，或将目录放入 `data/plugins/` 后加载。

MIT License，作者 ahuai。
