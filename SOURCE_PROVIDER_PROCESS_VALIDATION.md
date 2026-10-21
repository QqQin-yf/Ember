# App §13：隔离原文提供方服务进程联验回执

2026-10-21。App f239b21，地址 http://127.0.0.1:8792；Ember 8878 的确切加载提交与当前 PID 以 D:/phase2-secrets/ember-readiness.json 为准。本次只做隔离联验及只读现状核对，未推送、未部署 VPS、未轮换日常凭据、未开启生产开关。

## 实际服务进程与证据边界

隔离启动器新增显式 `--source-env-file`；只从指定仓库外文件加载 EMBER_SOURCE_URL/TOKEN/TIMEOUT，拒绝通用令牌等其他键，不继承日常 Ember 或模型凭据，不读取仓库 .env。源端基址只能是 loopback，已加载本次指定的 8792，timeout=5 秒。原文令牌只在本地内存读取，不出现在命令行、日志或本回执。

`--source-sample/--source-report` 运行一次显式隔离自检：载入实际 sidecar 与实例注册表，在**即将监听 8878 的同一个 OS PID 内、开始监听前**发送样本请求，使用实际 source_client，再核对实际 registry 服务的读工具。不是在 App/探针另一个 Python 进程里 import 客户端冒充服务进程。

开始监听后，从外部经标准 `/mcp/` tools/call/read_memory_source 再读 10 个显式合成出处夹具，核对正文或 not_archived；外部驱动不直接调用 Ember 对象/客户端。核对启动自检报告 PID 与 8878 实际监听进程相同。诊断入口只在隔离启动器里，不新增公共 HTTP/MCP 调试接口。

启用了隔离 second 实例以覆盖其自有会话；现有 default/phase2 数据保留。显式测试夹具不伪造用户开轮、不调用写记忆工具、模型或 Heart，不训练耳朵。127.0.0.1/localhost/::1 已显式写入子进程 no_proxy/NO_PROXY，自检确认 loopback_proxy_bypass=true；实际正确/跨实例结果也核对了实例头生效。

## 18 条 App 样本

| 样本 | 实际 HTTP | code | 结果 |
|---|---|---|---|
| 当前分支 | 200 | — | 通过 |
| 重新生成的旧分支 | 200 | — | 通过 |
| 编辑分支 | 200 | — | 通过 |
| 不连续来源 | 200 | — | 通过 |
| 另一个实例读自己的会话 | 200 | — | 通过 |
| 缺实例头 | 403 | source_instance_unknown | 通过 |
| 未知实例 | 403 | source_instance_unknown | 通过 |
| 跨实例（second 读 A） | 404 | source_session_unknown | 通过 |
| 跨实例（default 读 C） | 404 | source_session_unknown | 通过 |
| 会话不存在 | 404 | source_session_unknown | 通过 |
| 跨会话（A 里请求 B 的 seq） | 409 | source_path_unknown | 通过 |
| 兄弟混合路径 | 409 | source_path_unknown | 通过 |
| 取消的回复 | 409 | source_path_unknown | 通过 |
| sleep 旁支 | 409 | source_path_unknown | 通过 |
| 不存在的编号 | 409 | source_unavailable | 通过 |
| 缺编号 | 400 | bad_request | 通过 |
| 旧接口：歧义区间 | 409 | source_path_unknown | 通过 |
| 旧接口：无歧义区间 | 200 | — | 通过 |

所有成功行的 seq、顺序、角色及 content 逐字等于提供的 source-sample.json；所有拒绝响应均无 messages，不含样本正文。旧歧义区间确为 409，无歧义旧区间仍可按原范围读；Ember 生产客户端没有旧接口 fallback，旧接口两条仅由显式测试线请求。

另有 14 条可由生产 source_client 表达的精确路径用例通过：5 条正确原文，其余按 HTTP 403/404/409 抛 SourceUnavailable。缺实例头/空编号/两个旧接口样本只在 HTTP 测试线覆盖，不把客户端本身不会发的请求伪装成业务调用。无令牌/错误令牌 2 条均 401；原文令牌访问 7 个非授权或变体路径均 401。本方没有读取 App 那份隔离通用凭据；通用令牌拒读原文的专门检查仍依 App §13.4 的自测记录，不冒称本方读取那把令牌后复验。

外部标准 MCP 10 条通过：当前来源、旧分支、second 自有来源均为 ok 且正文准确；default/second 跨实例、兄弟混合、跨会话编号、取消、sleep、缺失编号均为 not_archived 且无 transcript。例：旧分支返回 seq 3/5/6/7 对应原句，其中“合成A-2回（重新生成的旧版）：明天大概放晴。”，不会取当前版“明天可能下雨。”。

完整非秘密结果：D:/phase2-secrets/source-provider-process.json、source-provider-mcp.json。前者含 18 条状态/code、合成原文与同 PID 客户端结果；后者记录外部 MCP 结果。凭据文件仍为用户指定的 C:/Users/秦雨菲/.deliz-secrets/deliz-source-isolated.ember.env，仅本地读取。

## 会话归属边界结论

**满足本轮按“会话当前 ember_instance”校验来源的要求，可以按这个范围验收。** 切换实例后旧实例 404，Ember 如实不可用，不偷偷回退旧实例/当前分支。未启用过 Ember 的会话若当前归 default，default 可读，是当前归属模型的授权边界，不是历史已投递证明。

不能将其扩大成“只有确曾投递给某实例的会话可读”或“Ember 开关关闭就撤销原文权限”；本轮未要求、也未实现这两种更严策略。共享原文令牌授权这组已知实例，实例头属于归属路由核验，不是每个实例独立令牌的认证模型。生产接受该边界应在开启决策中保留披露，本次不代替生产批准。

## 日常通用令牌和 19:43–19:50 核对

仅本地读取比较，未输出值/摘要、未复制秘密、未改配置：

- 本机本仓库 .env 的 EMBER_SOURCE_TOKEN 与日常 App server/.env 的 DELIZ_TOKEN 相同，原文地址指向日常 App 的 8790。
- VPS vps / Tailscale 100.98.125.95 的 /opt/ember1/.env 同样配置了这把通用令牌；ember-sidecar.service 仍为 a5a7c8f。确认过“配置使用了通用令牌”，不把配置比较冒充某一历史请求一定实际携带它的抓包证据。
- VPS 标准输出为 journal，stderr 随 stdout，没有另发现 /opt/ember1/*.log。查询台北时间 2026-10-21 19:43:00–19:50:59（11:43:00–11:50:59 UTC），journal 返回 0 条记录。
- 已查的本地仓库日志和此前隔离运行日志均无覆盖该窗口的记录（最新相关日志在 15:39 左右或更早），也未找到原文读取错误行。

**结论是未找到该窗口可核对的错误记录，不是证明没有请求/没有失败。** 旧 source 客户端存在捕获错误后返回不可用的静默降级，缺少逐次原文请求审计，不能从空 journal 重建那 7 分钟的读取次数、状态或业务影响。凭据是否曾配置已确认；日常通用令牌撤回/轮换及客户端更新仍按 App §13.3 单独安排，本轮没有执行。

## 回归与测试边界

完整 228 项 unittest 通过（以 Python 中显式置空原文提供方环境后运行）。新增 4 项隔离配置校验；强杀子进程显式清除继承的日常原文/模型配置。本地 HTTP 夹具补齐 Content-Length，避免 Windows 以连接重置结束响应造成假失败。业务读取、召回门槛及生产协议未因此更改。

## 隔离 / 生产分别结论

- 隔离：指定 App、专用原文令牌、实际 Ember 服务进程、正常 MCP 工具链路及实例头已联验通过；旧歧义 409、实例归属缺口可按本范围关闭。
- 生产：**未实配实测。** 本次 VPS 仅只读配置/日志核对，未升级日常 a5a7c8f、未切令牌、未验证生产原文通路、未重启生产服务；更没有批准或开启开关。继续按 App §13.3 顺序安排升级、独立令牌切换、旧通用令牌轮换与生产验证。
