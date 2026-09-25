# AstrBot QQ 群管理工具

面向 QQ 个人号 OneBot (`aiocqhttp`) 的群管理插件。当前提供群内容去重，后续功能可在同一插件中扩展。

## 内容去重

同一机器人、同一群内出现重复内容时，撤回整条消息，并在群内发送“重复内容已撤回。”。提醒不包含首次发送者或发送时间。

- 图片：只处理 OneBot 标记为普通图片 (`sub_type=0`) 的消息，按文件字节比较。QQ 系统表情、商城表情、自定义表情及分类不明的图片不参与去重。
- 视频：处理 `video` 消息段和常见视频格式的群文件，按文件字节比较。
- 合并转发：按节点顺序比较文字、普通图片和视频内容，忽略发送者与时间；含表情或无法完整解析的内容时跳过去重。
- 同条消息包含多项内容时，任意一项重复就撤回整条消息。图片或视频经过压缩、缩放、转码后，字节变化则不视为重复。

## 安装

将本目录放到 `AstrBot/data/plugins/astrbot_plugin_qq_group_tools`，在 AstrBot WebUI 重载插件。需要 AstrBot 4.28.1 或更新的 4.x 版本，以及机器人撤回群成员消息的权限；不需要额外 Python 依赖。

`enabled_groups` 留空时监控所有群；填写群号后只监控指定群。指纹存储在 `AstrBot/data/plugin_data/astrbot_plugin_qq_group_tools/fingerprints.sqlite3`，不保存媒体文件或发送者 QQ 号。
