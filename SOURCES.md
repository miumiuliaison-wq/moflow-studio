# 来源与复用记录

记录日期：2026-10-22。此次实现不加载原版或修改版 Better Douyin 二进制。

| 来源 | 用途 | 许可证/说明 |
|---|---|---|
| [F2](https://github.com/Johnserf-Seed/f2) | 作品详情、作者、收藏和评论读取适配 | Apache-2.0；固定提交 `f6be8c0ffba9a127075bbeafe4838716650b6325`；许可证保存在vendor |
| [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) | 独立本地请求辅助模块 | Apache-2.0；固定提交`0661234421fb8e1df24f97eb30958671f7a2ee08`；只复用abogus、sm3、websign三个纯Python模块；相对导入改动记录见vendor/dtk-source.json，保留LICENSE与NOTICE |
| [curl-cffi](https://github.com/lexiforest/curl_cffi) | 本地HTTP兼容请求 | 0.16.3，MIT；复用已存在依赖并显式锁定 |
| [UIArc](https://uiarc.dev/docs/introduction) | 免费基础主题、活动热力图、可排序表格 | 免费源组件标记为MIT；保存原始registry JSON，调整导入路径及灰阶主题；未使用Pro源组件 |
| [ascii-magic-art](https://github.com/kailashsr38/ascii-magic-art) | Moonlit Sea，转为单色字符 | npm 1.0.2，MIT；上游提交 `de88962156057cbef583bd265bf55e23d6c5e30d`；保留许可证。未使用两幅Unsplash例外作品 |
| [Tauri](https://v2.tauri.app/develop/sidecar/) | 桌面窗口、托盘、独立服务打包 | MIT / Apache-2.0，具体版本见Cargo.lock |
| [Tauri权限](https://v2.tauri.app/security/permissions/) | 本机服务页面调用目录选择与素材打开 | 自定义命令单独定义permission，仅授权主窗口的127.0.0.1:7861来源 |
| [Tauri多网页视图](https://docs.rs/tauri/latest/tauri/window/struct.Window.html#method.add_child) | 在主窗口中显示RunningHub原网页 | 使用锁定2.12.4的unstable多webview接口；网页使用独立持久化数据目录，不使用iframe。远程网页只获收藏元数据报告权限，没有本地文件、会话导出或网页控制权限 |
| [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) | 独立Chrome登录、搜索及网页操作，减少自动化兼容问题 | 1.64.1，Apache-2.0；官方PyPI Windows wheel，精确哈希在uv.lock；驱动及上游附带许可证一并保留；使用已安装Chrome，不另下载Chromium |
| [Playwright](https://github.com/microsoft/playwright-python) | 原有依赖及桌面验收脚本 | Apache-2.0；生产账号浏览器使用Patchright |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 可恢复的单规划器流程 | MIT；本地SQLite检查点，不启用云追踪 |
| [APScheduler](https://github.com/agronholm/apscheduler) | 定时触发 | MIT，3.x |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | Streamable HTTP本地工具接口 | MIT |
| [FFmpeg](https://ffmpeg.org/) | 媒体探测、完整解码校验、取帧 | 复用本机Gyan发行文件；构建许可证见媒体工具的 `-L` 输出 |
| [Windows SDK NuGet](https://learn.microsoft.com/en-us/windows/apps/windows-sdk/downloads) | 本机桌面编译 | Microsoft官方开发工具，只用于构建，不作为账号/下载引擎 |

Better Douyin的公开功能说明和先前取得的工具契约用于功能对齐；真实采集、队列、数据库、账号浏览器、AI及桌面服务均为本项目独立实现。未把其公开mock后端接成真实功能。

RunningHub集成使用其原网站：收藏列表接口`/api/likeOrCollect/webapp/list`来自真实已登录页面，属于网站适配而非公开API稳定性承诺。原网页在独立WebView2视图中运行。没有复制第三方应用工作流代码，没有部署官方付费API客户端，也没有自动提交收费测试。

2026-10-23的打开性能优化继续使用锁定的Tauri 2.12.4和WebView2：[Microsoft用户数据目录说明](https://github.com/MicrosoftDocs/edge-developer/blob/main/microsoft-edge/webview2/concepts/user-data-folder.md)用于查证持久Cookie、缓存和登录存储；[Tauri现有WebView API](https://docs.rs/tauri/latest/tauri/webview/struct.Webview.html)用于复用已有视图、读取当前URL和报告加载事件。不增加浏览器池或第三方代理；同一应用不重新导航，收藏同步复用原只读适配并改为后台任务。

发现页推荐优化对比了F2的现有读取能力、已下载TikTokDownloader的搜索排序/时间过滤定义，以及[TanStack Query缓存流程](https://github.com/TanStack/query/blob/main/docs/framework/react/guides/caching.md)。F2继续用于原有读取；推荐复用本机已有请求适配器和真实浏览器，独立实现按账号缓存、后台更新与进行中请求合并，没有新增TanStack依赖或复制其源代码。TikTokDownloader仅用于查证参数含义，没有复制实现。推荐不是抖音全站热榜或AI视觉审核。

比较并下载了 [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 和 [TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader)。前者完整服务引入PostgreSQL、Redis等组件；本机版仅复用独立请求辅助模块，其余继续使用本地SQLite和自有调度。后者仅作源码比较，没有整体引入。具体对比见`爬取方案对比.md`。

UIArc原始响应文件保存在vendor目录，组件变更可与这些文件对照。Python、npm、Cargo依赖由锁文件记录，不以浮动“最新版”作为交付依据。

交付目录 `licenses` 保留原始许可证、FFmpeg GPLv3构建信息、三个锁文件，以及Python环境103项包的版本/许可证记录和144份许可证文件。FFmpeg来自本机Gyan 8.1完整发行构建，未修改；实际组件构建参数见 `licenses/FFmpeg-build.txt`。


## 2026-10-23 DeepSeek与生产适配

独立实现DeepSeek兼容JSON请求、RunningHub AI App上传/提交/查询和生产状态机，继续使用已锁定的httpx、Pydantic、LangGraph、SQLite、FFmpeg。未新增依赖。

参考官方资料：
- https://api-docs.deepseek.com/quick_start/pricing/
- https://api-docs.deepseek.com/guides/vision/
- https://api-docs.deepseek.com/guides/json_mode/
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761096
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761097
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761034
- https://github.com/HM-RunningHub/OpenClaw_RH_Skills （仅核对接口，无复制其curl脚本）

调研未引入的项目：liguobao/ds-harness-remote（MIT）、Fei2411/DeepSeek-Bot（Apache-2.0）、BotHarness/DeepSeekBot（MIT）。未安装其插件、代码或远程服务；比较见自动生产与远程开发说明。

## 资料库与原生选材桥接（2026-10-28）

- 继续使用现有 rfd 0.15.4，调用 `AsyncFileDialog.set_directory` 设置选择器起始目录，并由用户在原生窗口确认文件。来源：[rfd 文档](https://docs.rs/rfd/0.15.4/rfd/struct.AsyncFileDialog.html)，MIT。
- 复用 Tauri 既有 WebView2 依赖，显式声明 webview2-com 0.39.1 和 windows 0.62.2（与现有锁定版本一致），通过 WebView2 内部 `CallDevToolsProtocolMethod` 对已点击的文件输入执行 `DOM.setFileInputFiles`。来源：[webview2-com](https://github.com/wravery/webview2-rs)、[Chromium 协议](https://chromedevtools.github.io/devtools-protocol/tot/DOM/#method-setFileInputFiles)。不开放远程调试端口，不向网页提供任意路径读取接口。
- 分类、后台增量索引和页面改造使用墨流现有 SQLite、FastAPI 与 React；未引入额外文件管理框架。现有实现已满足索引与筛选需求，无需复制或整体引入外部文件管理器。



## 2026-10-28 聊天与插件

独立实现 SQLite 持久聊天、固定工具规划及插件注册；没有复制 DSH/Bot 项目代码、没有恢复多 agent。参考 DeepSeek Harness 的清单/生命周期/工具注册分离（MIT）：https://github.com/deepseek-ai/deepseek-harness ，特别是 packages/extensions/tool-cordis/README.md。交互参考 liguobao/ds-harness-remote、Fei2411/DS-Bot、BotHarness/DeepSeekBot，链接及适用条件见《聊天与插件说明》。复用已锁定的 MCP Python SDK、Pydantic 与 jsonschema，无新依赖。MCP 插件按其自身许可证提供独立程序；不安装 dsh-purge。


## 2026-10-30 工作台与创作中心

界面组织参考Pamba官方工作台、首页帮助及用户提供的Pamba项目调研.zip中六张官方截图：https://pamba.app/docs/the-studio 、https://pamba.app/docs/home 。仅参考布局和交互，没有复制网站源码、品牌或付费素材。前端与SQLite草稿服务为独立实现；继续复用项目已有React、Lucide、Tauri、FastAPI、Pydantic、FFmpeg等锁定依赖，无新增第三方依赖。

## 搜索与推荐研究（2026-10-30）

参考 Pamba 公开 API/工具说明的语料优先、实时补充和新鲜度思路；比较 MomentSearch 与 Qwen3-VL-Embedding 后，本次使用现有 SQLite 与自编轻量规则，未引入其代码、模型或额外服务。来源及边界见 [搜索与推荐说明](./搜索与推荐说明.md)。
