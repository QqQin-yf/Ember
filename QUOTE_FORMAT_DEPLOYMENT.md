# quote_spans 格式容错部署回执

2026-10-25，按用户授权部署 `/opt/ember1`，重启 `ember-sidecar.service` 成功。未提交新 Git 版本；线上 HEAD 仍为 `0d5c1f7`，本次以文件指纹标识交付。

只替换三个文件，保留线上已有 service.py、sidecar.py、runs.py 和日常配置、数据：

| 文件 | SHA-256 |
| --- | --- |
| ember/turn_span.py | a5236ec0d47689fb905d37c6337a59ef532cc923e5f6b5d31c30e83fa711cb0b |
| ember/source_paths.py | 91d88bdee4915be7d8d291c7d5d332ee87ef2849842f8f63a81f95ea9658ccdc |
| ember/writer.py | 73ad10c33da1283d1e71361dfaa2c8f6781a55a511a9ded47bafbd5d476c65f7 |

备份：`/opt/ember-deploy-backups/quote-format-20261025-112659`，仅含这三个旧文件。部署前核对其指纹未变化，替换后失败会自动恢复并重启。本次未发生回滚。

验证：以线上现有源码建立临时副本、叠加三个新文件和当前回归测试，禁用日常原文配置，302 项 unittest、8 项 turn_span、38 项 sidecar 验收全部通过。重启后真实线上 MCP tools/list 已返回 emoji 格式容错及 quote_ambiguous 描述，并保留前端刚对齐的场景区间说明；从部署目录加载的定位代码已验证唯一命中接受、多候选拒绝并返回字段和数量。

没有为自检向日常记忆库写入测试 Event。实际写入路径已由临时库回归覆盖。App 可能缓存 MCP 清单，新的描述在其重新获取清单后显示；服务器匹配逻辑在本次重启后生效。
